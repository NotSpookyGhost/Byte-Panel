"""Account/SMTP regression tests. All runtime data lives in a temporary directory."""

import importlib.util
import html
from html.parser import HTMLParser
from contextlib import closing, contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from flask import Flask

APP_DIR = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP_DIR))
from panel_accounts import (AccountStore, PERMISSIONS, grants, hash_password, TEMPORARY_PASSWORD_TTL,
                            PASSWORD_RULES, PASSWORD_PATTERN, validate_password)
from panel_mail import DEFAULT_SMTP
from panel_security import PanelSecurity, ROUTE_PERMISSIONS, SETUP_ROUTES
from bluemap import MAP_ENDPOINTS


class AccountIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name)
        spec = importlib.util.spec_from_file_location('panel_integration_test_app', APP_DIR / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        # Never contact the GeoIP download service or start Minecraft during tests.
        with patch.dict(os.environ, {'PANEL_DATA_DIR': cls.temp.name}), patch('urllib.request.urlretrieve'):
            spec.loader.exec_module(cls.module)
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True)
        cls.security = cls.module.security
        cls.store = cls.security.store

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        delay = patch('panel_mail.sleep')
        self.mail_delay = delay.start()
        self.addCleanup(delay.stop)
        self.module.MINECRAFT_PROCESS = None
        self.module.MINECRAFT_LOG_THREAD = None
        self.module.server_automation.stop()
        self.module.SETUP_PROGRESS.update(percent=0, msg='Ready', status='idle')
        self.module.clear_player_data()
        self.store.reset()
        self.store.set_setting('smtp', DEFAULT_SMTP)
        self.security.failures.clear()
        self.security.socket_clients.clear()
        self.owner = self.store.bootstrap('owner', 'Owner-password-123')
        self.config = {**self.module.DEFAULT_CONFIG, 'is_setup': True, 'server_type': 'Paper',
                       'mc_version': '1.21.1', 'server_name': 'Test Server', 'ram_gb': 2,
                       'admin_user': 'owner', 'java_path': ''}
        self.module.save_config(self.config)
        self.client = self.client_for(self.owner)
        self.root = self.data / 'server'
        for child in self.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    @contextmanager
    def exported_backup(self, path):
        with tempfile.TemporaryFile() as output:
            self.module.server_backups.restic.export(path, output)
            with zipfile.ZipFile(output) as archive:
                yield archive

    def client_for(self, user):
        client = self.app.test_client()
        with client.session_transaction() as cookie:
            self.store.sign_in(cookie, user)
            cookie['plugin_csrf_token'] = 'plugin-test'
        return client

    def headers(self, client=None):
        with (client or self.client).session_transaction() as cookie:
            return {'X-Panel-CSRF': cookie['panel_csrf'], 'X-Plugin-CSRF-Token': 'plugin-test'}

    def create(self, username='viewer', permissions=None, **kwargs):
        return self.store.save_user(dict(username=username, password='User-password-123', email='',
                                         role='custom', permissions=permissions or [], **kwargs), self.owner)

    def smtp(self, **overrides):
        values = {**DEFAULT_SMTP, 'enabled': True, 'host': 'smtp.example.com', 'from_email': 'panel@example.com',
                  'username': 'smtp-user', 'password': 'smtp-private-secret', 'panel_url': 'https://panel.example.com', **overrides}
        return self.security.mailer.save(values)

    def test_obsidian_shell_is_shared_and_login_stands_alone(self):
        for path in ('/overview', '/console', '/logs', '/players', '/plugins', '/properties',
                     '/backups', '/settings', '/users', '/account'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                page = response.get_data(as_text=True)
                self.assertEqual(page.count('id="panel-sidebar"'), 1)
                self.assertEqual(page.count('class="panel-header"'), 1)
                self.assertIn('data-accent="lime"', page)
                nav = page.split('<nav class="nav-group"')[1].split('</nav>')[0]
                self.assertNotIn('href="/account"', nav)
                self.assertIn('class="profile-control', page)
                version_markup = page.split('<div class="sidebar-version">')[1].split('</div>')[0]
                class VersionText(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.text = []

                    def handle_data(self, text):
                        self.text.append(text)
                version = VersionText()
                version.feed(version_markup)
                expected = self.module.inject_version()['app_version']
                self.assertEqual(''.join(version.text).strip(), expected)
                self.assertNotIn('Byte Panel', version_markup)
        login = self.app.test_client().get('/login').get_data(as_text=True)
        self.assertIn('page-login', login)
        self.assertNotIn('id="panel-sidebar"', login)
        self.assertNotIn('class="panel-header"', login)
        self.assertNotIn('class="login-brand"', login)
        self.assertNotIn('Welcome back', login)
        card = login.split('<section class="card login-card">')[1].split('</section>')[0]
        self.assertIn('class="login-server-identity"', card.split('<form')[0])
        self.assertIn('<h1>Test Server</h1>', card)

    def test_sidebar_brand_is_fixed_and_login_ignores_legacy_panel_logo(self):
        logo = self.data / 'branding' / 'logo.png'
        self.assertFalse(logo.exists(), 'Test fixture must start without a custom logo')
        page = self.client.get('/overview').get_data(as_text=True)
        brand = page.split('<div class="sidebar-brand">')[1].split('<div class="sidebar-server">')[0]
        self.assertIn('class="brand-mark"', brand)
        self.assertIn('d="M16 3 29 16 16 29 3 16Z"', brand)
        self.assertIn('href="/overview"', brand)
        logo.write_bytes((APP_DIR / 'static' / 'bytedev_logo.png').read_bytes())
        self.addCleanup(logo.unlink)
        for path in ('/overview', '/backups', '/settings', '/account'):
            with self.subTest(path=path):
                page = self.client.get(path).get_data(as_text=True)
                brand = page.split('<div class="sidebar-brand">')[1].split('<div class="sidebar-server">')[0]
                self.assertIn('class="brand-mark"', brand)
                self.assertNotIn('branding-assets', brand)
                self.assertIn('Byte Panel', brand)
        login = self.app.test_client().get('/login').get_data(as_text=True)
        self.assertNotIn('class="brand-mark"', login)
        self.assertNotIn('/branding-assets/logo.png', login)
        self.assertNotIn('/branding-assets/server-icon.png', login)
        identity = login.split('class="login-server-identity"')[1].split('</h1>')[0]
        self.assertIn('M3 3h18v7H3z M3 14h18v7H3z', identity)
        self.assertIn('<h1>Test Server', identity)

    def test_login_server_identity_escapes_name_inside_the_card(self):
        name = 'Friends <img src=x onerror=alert(1)> & "Players"'
        self.config['server_name'] = name
        self.module.save_config(self.config)
        login = self.app.test_client().get('/login').get_data(as_text=True)
        before_card, card = login.split('<section class="card login-card">', 1)
        self.assertNotIn('class="login-server-identity"', before_card)
        self.assertNotIn('class="login-brand"', login)
        heading = card.split('<h1>')[1].split('</h1>')[0]
        self.assertEqual(html.unescape(heading), name)
        self.assertNotIn('<img', heading)
        self.assertIn('&lt;img', heading)
        self.assertNotIn('Welcome back', card)

    def test_server_icon_control_is_in_general_not_appearance(self):
        page = self.client.get('/settings').get_data(as_text=True)
        general = page.split('id="settings-general"')[1].split('</section>')[0]
        appearance = page.split('id="settings-appearance"')[1].split('</section>')[0]
        self.assertIn('for="serverIconInput"', general)
        self.assertIn('name="server_icon"', general)
        self.assertIn('Server icon', general)
        self.assertNotIn('name="server_icon"', appearance)
        self.assertNotIn('name="custom_logo"', page)
        self.assertIn('name="custom_favicon"', appearance)
        self.assertIn('name="custom_bg"', appearance)

    def upload_test_icon(self, raw, field='server_icon', client=None, **form):
        client = client or self.client
        return client.post('/settings', data={field: (io.BytesIO(raw), '../../chosen.jpg'), **form},
                           headers=self.headers(client))

    def test_server_icon_upload_is_shared_and_follows_backup_restore(self):
        from PIL import Image
        from test_server_icon import image_bytes
        result = self.upload_test_icon(image_bytes())
        self.assertEqual(result.status_code, 200)
        self.assertIn('Restart Minecraft', result.get_data(as_text=True))
        icon = self.root / 'server-icon.png'
        original = icon.read_bytes()
        with Image.open(icon) as image:
            self.assertEqual(image.size, (64, 64))
            self.assertEqual(image.format, 'PNG')
        self.assertTrue(self.module.get_custom_assets_state()['server_icon'])
        self.assertFalse((self.data / 'branding' / 'logo.png').exists())
        response = self.client.get('/branding-assets/server-icon.png')
        try:
            self.assertEqual(response.data, original)
            self.assertIn('no-store', response.headers['Cache-Control'])
        finally:
            response.close()
        for path in ('/overview', '/settings'):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn('/branding-assets/server-icon.png', page)
            brand = page.split('<div class="sidebar-brand">')[1].split('<div class="sidebar-server">')[0]
            self.assertIn('class="brand-mark"', brand)
            self.assertNotIn('branding-assets', brand)
        login = self.app.test_client().get('/login').get_data(as_text=True)
        card_heading = login.split('<section class="card login-card">')[1].split('<form')[0]
        identity = card_heading.split('class="login-server-identity"')[1].split('</h1>')[0]
        self.assertIn('src="/branding-assets/server-icon.png"', identity)
        self.assertIn('<h1>Test Server', identity)
        self.assertNotIn('<svg', identity)
        self.assertNotIn('class="login-brand"', login)
        backup = self.client.post('/api/backup/create', headers=self.headers()).json
        with self.exported_backup(self.data / 'backups' / backup['server_key'] / backup['file']) as archive:
            self.assertEqual(archive.read('server-icon.png'), original)
            self.assertFalse(any(name.endswith('.tmp') for name in archive.namelist()))
        self.assertEqual(self.upload_test_icon(image_bytes(color='red')).status_code, 200)
        self.assertNotEqual(icon.read_bytes(), original)
        config = self.module.load_config()
        config['bluemap_enabled'] = True
        self.module.save_config(config)
        restored = self.client.post('/api/backup/restore', json={
            'server_name': backup['server_key'], 'filename': backup['file']}, headers=self.headers())
        self.assertEqual(restored.status_code, 200, restored.json)
        self.assertFalse(self.module.load_config()['bluemap_enabled'])
        self.assertEqual(icon.read_bytes(), original)
        response = self.client.get('/branding-assets/server-icon.png')
        try:
            self.assertEqual(response.data, original)
        finally:
            response.close()

    def test_invalid_server_icon_and_busy_backup_leave_files_and_settings_unchanged(self):
        from test_server_icon import image_bytes
        self.assertEqual(self.upload_test_icon(image_bytes()).status_code, 200)
        icon = self.root / 'server-icon.png'
        original = icon.read_bytes()
        config = self.module.load_config()
        result = self.upload_test_icon(b'<svg>not a raster image</svg>', server_name='Must not save')
        self.assertEqual(result.status_code, 400)
        self.assertIn('Server icon is invalid', result.get_data(as_text=True))
        self.assertEqual(icon.read_bytes(), original)
        self.assertEqual(self.module.load_config(), config)
        with self.module.server_backups.exclusive():
            result = self.upload_test_icon(image_bytes(color='red'), server_name='Must not save')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(icon.read_bytes(), original)
        self.assertEqual(self.module.load_config(), config)

    def test_server_icon_storage_failure_keeps_previous_image(self):
        from test_server_icon import image_bytes
        self.assertEqual(self.upload_test_icon(image_bytes()).status_code, 200)
        original = (self.root / 'server-icon.png').read_bytes()
        with patch('server_icon.os.replace', side_effect=OSError('storage unavailable')), \
             self.assertLogs(self.app.logger, level='ERROR'):
            result = self.upload_test_icon(image_bytes(color='red'))
        self.assertEqual(result.status_code, 500)
        self.assertIn('Check disk space', result.get_data(as_text=True))
        self.assertEqual((self.root / 'server-icon.png').read_bytes(), original)
        self.assertEqual(list(self.root.glob('.server-icon-*.tmp')), [])

    def test_server_icon_requires_settings_permission_and_csrf_even_for_legacy_upload(self):
        from test_server_icon import image_bytes
        viewer = self.client_for(self.create())
        for field in ('server_icon', 'custom_logo'):
            self.assertEqual(self.upload_test_icon(image_bytes(), field=field, client=viewer).status_code, 403)
            no_csrf = self.client.post('/settings', data={field: (io.BytesIO(image_bytes()), 'icon.png')})
            self.assertEqual(no_csrf.status_code, 403)
            self.assertFalse((self.root / 'server-icon.png').exists())
        self.assertEqual(self.upload_test_icon(image_bytes(), field='custom_logo').status_code, 200)
        self.assertTrue((self.root / 'server-icon.png').is_file())

    def test_bluemap_controls_belong_to_addons_tab_inside_settings_form(self):
        class SettingsElements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms, self.sections, self.nodes = [], [], []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'form':
                    self.forms.append(attrs.get('id'))
                elif tag == 'section':
                    self.sections.append(attrs.get('id'))
                self.nodes.append((tag, attrs, tuple(self.forms), tuple(self.sections)))

            def handle_endtag(self, tag):
                if tag == 'form' and self.forms:
                    self.forms.pop()
                elif tag == 'section' and self.sections:
                    self.sections.pop()

        for enabled, port in ((False, 8100), (True, 8123)):
            with self.subTest(enabled=enabled, port=port):
                self.config.update(bluemap_enabled=enabled, bluemap_port=port)
                self.module.save_config(self.config)
                page = self.client.get('/settings').get_data(as_text=True)
                parsed = SettingsElements()
                parsed.feed(page)
                tabs = [node for node in parsed.nodes if node[1].get('id') == 'settings-tab-addons']
                self.assertEqual(len(tabs), 1)
                self.assertEqual(tabs[0][0], 'button')
                self.assertEqual(tabs[0][1]['role'], 'tab')
                self.assertEqual(tabs[0][1]['aria-controls'], 'settings-addons')
                self.assertEqual(tabs[0][1]['data-settings-tab'], 'addons')
                panels = [node for node in parsed.nodes if node[1].get('id') == 'settings-addons']
                self.assertEqual(len(panels), 1)
                self.assertEqual(panels[0][1]['role'], 'tabpanel')
                self.assertEqual(panels[0][1]['aria-labelledby'], 'settings-tab-addons')
                self.assertEqual(panels[0][1]['data-settings-panel'], 'addons')
                self.assertEqual(panels[0][2], ('settingsForm',))
                for field in ('bluemap_settings_present', 'bluemap_enabled', 'bluemap_port'):
                    nodes = [node for node in parsed.nodes if node[1].get('name') == field]
                    self.assertEqual(len(nodes), 1, field)
                    self.assertEqual(nodes[0][0], 'input')
                    self.assertEqual(nodes[0][2], ('settingsForm',), field)
                    self.assertIn('settings-addons', nodes[0][3], field)
                    self.assertNotIn('settings-general', nodes[0][3], field)
                    if field == 'bluemap_enabled':
                        self.assertEqual('checked' in nodes[0][1], enabled)
                    elif field == 'bluemap_port':
                        self.assertEqual(nodes[0][1]['value'], str(port))
                # The sidebar also links to an enabled map; scope this check to the settings form.
                map_links = [node for node in parsed.nodes
                             if node[0] == 'a' and node[1].get('href') == '/map/' and 'settingsForm' in node[2]]
                self.assertEqual(len(map_links), int(enabled))
                if enabled:
                    self.assertIn('settings-addons', map_links[0][3])
                    self.assertNotIn('settings-general', map_links[0][3])
                general = page.split('id="settings-general"')[1].split('</section>')[0]
                self.assertNotIn('BlueMap', general)

    def test_bluemap_settings_are_explicit_validated_and_permission_protected(self):
        original = self.module.load_config()
        self.assertFalse(original['bluemap_enabled'])
        self.assertNotIn('href="/map/"', self.client.get('/overview').get_data(as_text=True))
        for value in ('5000', '80', '65536', 'http://example.com', '8100/path', '-1'):
            response = self.client.post('/settings', data={'bluemap_port': value,
                'bluemap_settings_present': '1', 'bluemap_enabled': 'true'}, headers=self.headers())
            self.assertEqual(response.status_code, 400, value)
            self.assertEqual(self.module.load_config(), original)
        viewer = self.client_for(self.create())
        response = viewer.post('/settings', data={'bluemap_settings_present': '1',
            'bluemap_enabled': 'true'}, headers=self.headers(viewer))
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/settings', data={'bluemap_port': '8101',
            'bluemap_settings_present': '1', 'bluemap_enabled': 'true'}, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.module.load_config()['bluemap_port'], 8101)
        self.assertTrue(self.module.load_config()['bluemap_enabled'])
        self.assertIn('href="/map/"', self.client.get('/overview').get_data(as_text=True))
        # Older forms/partial updates don't silently disable the public map.
        self.client.post('/settings', data={'accent': 'blue'}, headers=self.headers())
        self.assertTrue(self.module.load_config()['bluemap_enabled'])
        self.client.post('/settings', data={'bluemap_settings_present': '1'}, headers=self.headers())
        self.assertFalse(self.module.load_config()['bluemap_enabled'])

    def test_public_map_is_separate_from_panel_auth_and_disabled_before_setup(self):
        public = self.app.test_client()
        self.assertEqual(public.get('/map').status_code, 404)
        self.config['bluemap_enabled'] = True
        self.module.save_config(self.config)
        response = public.get('/map?world=overworld', base_url='https://mc.bytedev.app')
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers['Location'], '/map/?world=overworld')
        self.assertNotIn('Set-Cookie', response.headers)
        with patch('bluemap.requests.Session.request', side_effect=self.module.requests.ConnectionError):
            response = public.get('/map/maps/world/live/players.json')
        self.assertEqual(response.status_code, 503)  # not a panel-login redirect
        self.assertNotIn('admin_user', response.get_data(as_text=True))
        self.assertEqual(public.get('/api/stats').status_code, 401)
        self.assertEqual(public.get('/settings').status_code, 302)
        self.assertEqual(public.post('/map/').status_code, 405)
        self.config['is_setup'] = False
        self.module.save_config(self.config)
        self.assertEqual(public.get('/map/').status_code, 404)
        self.assertEqual(public.get('/map/assets/main.js').status_code, 404)

    def test_obsidian_settings_persist_accent_and_server_name_not_product_brand(self):
        self.config.update(auto_backup_freq='6_hours', java_path='/configured/java')
        self.module.save_config(self.config)
        for accent in ('lime', 'yellow', 'blue'):
            response = self.client.post('/settings', data={
                'accent': accent, 'server_name': 'Community <Realm>', 'panel_name': 'Not Byte Panel',
                '_csrf': self.headers()['X-Panel-CSRF']})
            self.assertEqual(response.status_code, 200)
            config = self.module.load_config()
            self.assertEqual(config['accent'], accent)
            self.assertEqual(config['server_name'], 'Community <Realm>')
            self.assertEqual(config['panel_name'], 'Byte Panel')
            self.assertEqual(config['auto_backup_freq'], '6_hours')
            self.assertEqual(config['java_path'], '/configured/java')
            page = self.client.get('/overview').get_data(as_text=True)
            self.assertIn('Community &lt;Realm&gt;', page)
            self.assertIn('data-accent="' + accent + '"', page)
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertNotIn('name="panel_name"', page)
        for section in ('general', 'runtime', 'backups', 'addons', 'appearance', 'email', 'operations'):
            self.assertIn('id="settings-' + section + '"', page)

    def test_obsidian_invalid_settings_do_not_write_and_permissions_hold(self):
        original = self.module.load_config()
        for values in ({'accent': 'red'}, {'server_name': ''}, {'server_name': 'x' * 81},
                       {'server_name': 'bad\nname'}):
            response = self.client.post('/settings', data=values, headers=self.headers())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.module.load_config(), original)
        viewer = self.client_for(self.create())
        self.assertEqual(viewer.post('/settings', data={'accent': 'blue'}, headers=self.headers(viewer)).status_code, 403)
        self.config['accent'] = 'legacy-invalid'
        self.module.save_config(self.config)
        self.assertEqual(self.module.load_config()['accent'], 'lime')

    def test_overview_activity_is_real_escaped_and_permission_filtered(self):
        empty = self.client.get('/overview').get_data(as_text=True)
        self.assertIn('No recent activity to show', empty)
        with closing(sqlite3.connect(self.module.PLAYER_DB_FILE)) as conn:
            conn.execute("INSERT INTO users (uuid, username) VALUES (?, ?)", ('activity-id', '<Activity Player>'))
            conn.execute("INSERT INTO login_events (uuid, timestamp, ip) VALUES (?, ?, ?)",
                         ('activity-id', '2026-11-17 12:00:00', '192.0.2.100'))
            conn.commit()
        page = self.client.get('/overview').get_data(as_text=True)
        self.assertIn('&lt;Activity Player&gt;', page)
        self.assertNotIn('192.0.2.100', page)
        self.assertIn('Not measured by this panel', page)
        viewer = self.client_for(self.create())
        restricted = viewer.get('/overview').get_data(as_text=True)
        self.assertNotIn('Activity Player', restricted)
        self.assertIn('Player-history permission is required', restricted)

    def test_all_routes_have_explicit_access_policy(self):
        known = set(ROUTE_PERMISSIONS) | SETUP_ROUTES | MAP_ENDPOINTS | {'static', 'serve_custom_branding', 'login'}
        self.assertEqual({rule.endpoint for rule in self.app.url_map.iter_rules()} - known, set())

    def test_live_versions_endpoint_and_invalid_setup_selection(self):
        catalog = {'latest': '26.3', 'versions': [{'version': '26.3', 'engines': ['Vanilla', 'Forge']}], 'warnings': []}
        self.config['is_setup'] = False
        self.module.save_config(self.config)
        self.module.SETUP_PROGRESS.update(status='idle', percent=0)
        with patch.object(self.module.release_service, 'catalog', return_value=catalog):
            self.assertEqual(self.client.get('/api/minecraft/versions').json, catalog)
            response = self.client.post('/setup', data={'server_type': 'Paper', 'version': '26.3'}, headers=self.headers())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.module.load_config()['mc_version'], '1.21.1')
            self.store.reset()
            self.assertEqual(self.app.test_client().get('/api/minecraft/versions').json, catalog)
        with patch.object(self.module.release_service, 'catalog', side_effect=RuntimeError('Upstream unavailable')):
            self.assertEqual(self.app.test_client().get('/api/minecraft/versions').status_code, 503)

    def test_installer_checksum_failure_never_launches_java(self):
        download = {'url': 'https://example.com/server.jar', 'java_path': '/usr/lib/jvm/java-25-openjdk/bin/java',
                    'forge_version': '', 'forge_full_version': '', 'checksum': ('sha256', 'invalid')}
        self.module.SETUP_PROGRESS.update(status='running', percent=5)
        with patch.object(self.module.release_service, 'resolve_download', return_value=download), \
             patch.object(self.module.requests, 'get') as get, patch.object(self.module.subprocess, 'Popen') as launch:
            get.return_value.iter_content.return_value = [self.zip_bytes()]
            self.module.setup_server_bg('Vanilla', '26.3', self.config)
            launch.assert_not_called()
        self.assertEqual(self.module.SETUP_PROGRESS['status'], 'error')
        self.assertIn('checksum', self.module.SETUP_PROGRESS['msg'])
        self.assertFalse((self.root / 'server.jar').exists())

    def test_owner_can_poll_setup_through_persisted_completion(self):
        self.config['is_setup'] = False
        self.module.save_config(self.config)
        self.module.SETUP_PROGRESS.update(percent=90, msg='Creating Initial Backup/Restore Point', status='running')
        response = self.client.get('/api/setup/progress')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['percent'], 90)
        self.assertEqual(response.json['status'], 'running')
        # Simulate the worker saving success before updating its progress dict.
        self.config['is_setup'] = True
        self.module.save_config(self.config)
        response = self.client.get('/api/setup/progress')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, dict(percent=100, msg='Done', status='done'))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        # The persisted result also survives loss of the in-memory snapshot.
        self.module.SETUP_PROGRESS.update(percent=0, msg='Ready', status='idle')
        self.assertEqual(self.client.get('/api/setup/progress').json, response.json)

    def test_setup_progress_retains_real_worker_errors(self):
        self.config['is_setup'] = False
        self.module.save_config(self.config)
        self.module.SETUP_PROGRESS.update(percent=90, msg='Error: backup failed', status='error')
        response = self.client.get('/api/setup/progress')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, dict(percent=90, msg='Error: backup failed', status='error'))

    def test_setup_progress_remains_owner_only_before_and_after_completion(self):
        admin = self.store.save_user(dict(username='admin', password='Admin-password-123',
                                          role='admin', email=''), self.owner)
        viewer = self.create(permissions=list(PERMISSIONS))
        clients = [(self.app.test_client(), 401), (self.client_for(admin), 403),
                   (self.client_for(viewer), 403)]
        for complete in (False, True):
            self.config['is_setup'] = complete
            self.module.save_config(self.config)
            for client, expected in clients:
                with self.subTest(complete=complete, expected=expected):
                    self.assertEqual(client.get('/api/setup/progress').status_code, expected)

    def test_final_progress_read_does_not_reopen_setup_mutations(self):
        self.assertEqual(self.client.get('/api/setup/progress').json['status'], 'done')
        with patch.object(self.module.threading, 'Thread') as thread:
            response = self.client.post('/setup', data={'server_type': 'Paper', 'version': '1.21.1'},
                                        headers=self.headers())
            self.assertEqual(response.status_code, 409)
            thread.assert_not_called()
        self.assertEqual(self.client.get('/setup').status_code, 409)
        bootstrap_path = next(rule.rule for rule in self.app.url_map.iter_rules()
                              if rule.endpoint == 'accounts.bootstrap')
        self.assertEqual(self.client.post(bootstrap_path, json={}, headers=self.headers()).status_code, 409)
        self.assertEqual(self.client.post('/setup', data={}).status_code, 403)

    def test_latest_forge_installer_uses_java25_and_argument_launch(self):
        full = '26.3-66.1.8'
        args = self.root / 'libraries' / 'net' / 'minecraftforge' / 'forge' / full / 'unix_args.txt'
        args.parent.mkdir(parents=True)
        args.write_text('net.minecraftforge.bootstrap.ForgeBootstrap')
        runtime = '/usr/lib/jvm/java-25-openjdk/bin/java'
        download = {'url': 'https://example.com/installer.jar', 'java_path': runtime,
                    'forge_version': '66.1.8', 'forge_full_version': full, 'checksum': None}
        self.config.update(mc_version='26.3', server_type='Forge')
        with patch.object(self.module.release_service, 'resolve_download', return_value=download), \
             patch.object(self.module.requests, 'get') as get, patch.object(self.module.subprocess, 'run') as install, \
             patch.object(self.module.subprocess, 'Popen') as launch, patch('time.sleep'), \
             patch.object(self.module, 'create_backup') as initial_backup:
            get.return_value.iter_content.return_value = [self.zip_bytes()]
            install.return_value.returncode = 0
            launch.return_value.stdout.readline.side_effect = ['Done (1s)! For help, type help\n']
            self.module.setup_server_bg('Forge', '26.3', self.config)
            initial_backup.assert_called_once_with('Test Server', is_manual=True, is_initial=True)
            self.assertEqual(install.call_args.args[0][0], runtime)
            self.assertEqual(launch.call_args.args[0][0], runtime)
            self.assertTrue(any(value.replace('\\', '/').startswith('@libraries/') for value in launch.call_args.args[0]))
        self.assertEqual(self.module.SETUP_PROGRESS['status'], 'done', self.module.SETUP_PROGRESS)
        self.assertEqual(self.module.load_config()['forge_full_version'], full)

    def test_owner_pages_render_and_secrets_are_not_returned(self):
        self.smtp()
        for page in ['/overview', '/console', '/logs', '/players', '/properties', '/backups', '/settings', '/plugins', '/users', '/account']:
            with self.subTest(page=page):
                result = self.client.get(page)
                self.assertEqual(result.status_code, 200)
                self.assertIn(b'panel-csrf', result.data)
                self.assertNotIn(b'smtp-private-secret', result.data)
                self.assertNotIn(b'Owner-password-123', result.data)
        self.assertNotIn('password', self.client.get('/api/settings/smtp').json['smtp'])
        self.assertNotIn('password_hash', self.client.get('/api/users').json['users'][0])

    def test_logged_in_flag_alone_cannot_authenticate(self):
        client = self.app.test_client()
        with client.session_transaction() as cookie:
            cookie['logged_in'] = True
        self.assertEqual(client.get('/api/backups/list').status_code, 401)
        self.assertEqual(client.get('/api/stats').status_code, 401)

    def test_login_uses_database_and_rate_limits_bad_passwords(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/login').status_code, 200)
        self.assertEqual(client.post('/login', data={'username': 'owner', 'password': 'Owner-password-123'}).status_code, 403)
        response = client.post('/login', data={'username': 'owner', 'password': 'Owner-password-123'}, headers=self.headers(client))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get('/api/stats').status_code, 200)
        client.get('/logout')
        client.get('/login')
        self.security.failures['127.0.0.1'] = (10, __import__('time').monotonic())
        response = client.post('/login', data={'username': 'owner', 'password': 'Owner-password-123'}, headers=self.headers(client))
        self.assertEqual(response.status_code, 401)
        self.assertIn(b'Too many failed attempts', response.data)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for browser-script syntax checks')
    def test_rendered_scripts_and_form_structure(self):
        class PageParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_script = False
                self.scripts = []
                self.form_depth = 0
                self.nested_form = False

            def handle_starttag(self, tag, attrs):
                if tag == 'script':
                    attributes = dict(attrs)
                    self.in_script = not attributes.get('src') and attributes.get('type') != 'application/json'
                    if self.in_script:
                        self.scripts.append('')
                if tag == 'form':
                    self.form_depth += 1
                    self.nested_form |= self.form_depth > 1

            def handle_endtag(self, tag):
                if tag == 'script':
                    self.in_script = False
                if tag == 'form':
                    self.form_depth -= 1

            def handle_data(self, text):
                if self.in_script:
                    self.scripts[-1] += text

        for path in ['/overview', '/console', '/logs', '/players', '/properties', '/settings', '/backups', '/users', '/account', '/plugins', '/login', '/setup']:
            if path == '/setup':
                self.config['is_setup'] = False
                self.module.save_config(self.config)
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200, path)
            parser = PageParser()
            parser.feed(result.get_data(as_text=True))
            self.assertFalse(parser.nested_form, path)
            self.assertEqual(parser.form_depth, 0, path)
            for script in parser.scripts:
                check = subprocess.run(['node', '--check'], input=script, capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(check.returncode, 0, path + ': ' + check.stderr)

    def test_password_forms_share_server_rules_and_do_not_restrict_current_password(self):
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.fields = {}
            def handle_starttag(self, tag, attrs):
                if tag == 'input':
                    values = dict(attrs)
                    self.fields[values.get('name')] = values
        for path, names in (('/account', ['password', 'confirm_password']), ('/setup', ['admin_password'])):
            if path == '/setup':
                self.config['is_setup'] = False
                self.module.save_config(self.config)
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            markup = response.get_data(as_text=True)
            self.assertIn('password_policy.js?v=', markup)
            self.assertIn('password_policy.css?v=', markup)
            self.assertIn('Password requirements', markup)
            self.assertEqual(markup.count('data-password-check='), 4)
            self.assertIn('aria-live="polite"', markup)
            parser = Inputs()
            parser.feed(markup)
            for name in names:
                field = parser.fields[name]
                self.assertEqual(field['pattern'], PASSWORD_PATTERN)
                self.assertEqual(json.loads(field['data-password-rules']), [list(rule) for rule in PASSWORD_RULES])
                self.assertIn('uppercase letter', field['title'])
                self.assertIn('punctuation symbol', field['title'])
                if name == 'confirm_password':
                    self.assertNotIn('data-password-checklist', field)
                else:
                    checklist_id = field['data-password-checklist']
                    self.assertEqual(field['aria-describedby'], checklist_id)
                    self.assertEqual(markup.count('id="' + checklist_id + '"'), 1)
                    self.assertIn('id="' + checklist_id + '-title"', markup)
                if shutil.which('node'):
                    policy = json.dumps(dict(rules=json.loads(field['data-password-rules']), pattern=field['pattern']))
                    check = subprocess.run(['node', 'tests/test_password_policy_ui.js', policy],
                                           capture_output=True, text=True, encoding='utf-8')
                    self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            if path == '/account':
                self.assertNotIn('pattern', parser.fields['current_password'])
                self.assertNotIn('data-password-rules', parser.fields['current_password'])

    def test_password_change_api_rejects_missing_categories_without_changing_credentials(self):
        for value, missing in (('lowercase123!', 'uppercase letter'), ('Uppercaseonly!', 'number'),
                               ('Uppercase1234', 'punctuation symbol')):
            result = self.client.post('/api/account/password', headers=self.headers(), json=dict(
                current_password='Owner-password-123', password=value, confirm_password=value))
            self.assertEqual(result.status_code, 400)
            self.assertIn(missing, result.json['error'])
            self.assertNotIn(value, result.get_data(as_text=True))
            live = self.store.authenticate('owner', 'Owner-password-123')
            self.assertEqual(live['revision'], self.owner['revision'])
            self.assertEqual(self.client.get('/api/stats').status_code, 200)

    def test_bootstrap_api_enforces_the_new_password_policy(self):
        self.store.reset()
        self.config['is_setup'] = False
        self.module.save_config(self.config)
        client = self.app.test_client()
        self.assertEqual(client.get('/setup').status_code, 200)
        for value in ('lowercase123!', 'Uppercaseonly!', 'Uppercase1234'):
            result = client.post('/api/bootstrap', json=dict(username='new-owner', password=value), headers=self.headers(client))
            self.assertEqual(result.status_code, 400)
            self.assertEqual(self.store.list_users(), [])
        result = client.post('/api/bootstrap', json=dict(username='new-owner', password='UPPERCASE123!'), headers=self.headers(client))
        self.assertEqual(result.status_code, 201, result.json)
        self.assertEqual(result.json['user']['role'], 'owner')

    def test_generated_invitations_and_resends_meet_the_password_policy(self):
        user, first = self.invited_user()
        self.assertEqual(validate_password(first), first)
        self.assertEqual(len(first), 24)
        with patch.object(self.security.mailer, 'welcome', return_value='sent') as welcome:
            result = self.client.post('/api/users/' + user['id'] + '/invite', json={}, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        second = welcome.call_args.args[1]
        self.assertEqual(validate_password(second), second)
        self.assertEqual(len(second), 24)
        self.assertNotEqual(first, second)
        self.assertIsNone(self.store.authenticate(user['username'], first))
        self.assertTrue(self.store.authenticate(user['username'], second)['must_change_password'])

    def test_viewer_cannot_bypass_permissions_with_direct_api_calls(self):
        viewer = self.client_for(self.create())
        self.assertEqual(viewer.get('/overview').status_code, 200)
        for path in ['/console', '/logs', '/players', '/plugins', '/settings', '/users', '/properties',
                     '/backups', '/api/console', '/api/players', '/api/backups/list', '/api/settings/smtp', '/api/users']:
            with self.subTest(path=path):
                self.assertEqual(viewer.get(path).status_code, 403)
        for path in ['/start', '/stop', '/api/server/restart', '/api/command', '/api/player/kick',
                     '/api/backup/create', '/api/backup/delete', '/api/backup/restore', '/api/purge', '/api/import',
                     '/api/users', '/api/settings/smtp/test', '/api/plugins/upload', '/api/logs/clear']:
            with self.subTest(path=path):
                self.assertEqual(viewer.post(path, json={}, headers=self.headers(viewer)).status_code, 403)
        html = viewer.get('/overview').get_data(as_text=True)
        self.assertNotIn('href="/settings"', html)
        self.assertIn('href="/account"', html)

    def test_csrf_required_for_form_and_json_mutations(self):
        self.assertEqual(self.client.post('/api/users', json={}).status_code, 403)
        self.assertEqual(self.client.post('/settings', data={'server_name': 'bad'}).status_code, 403)
        self.assertNotEqual(self.module.load_config()['server_name'], 'bad')
        result = self.client.post('/settings', data={'server_name': 'Good', '_csrf': self.headers()['X-Panel-CSRF']})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.module.load_config()['server_name'], 'Good')

    def test_create_duplicate_and_password_hashing(self):
        self.smtp()
        payload = dict(username='new-user', email='user@example.com', role='custom', permissions=['plugins.manage'])
        with patch.object(self.security.mailer, 'welcome', return_value='sent') as welcome:
            response = self.client.post('/api/users', json=payload, headers=self.headers())
        temporary = welcome.call_args.args[1]
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json['email_status'], 'sent')
        self.assertTrue(response.json['user']['must_change_password'])
        self.assertIn('plugins.view', response.json['user']['permissions'])
        self.assertNotIn('password_hash', response.json['user'])
        self.assertTrue(self.store.authenticate('NEW-USER', temporary))
        self.assertNotIn(temporary, response.get_data(as_text=True))
        with self.store.connection() as db:
            encoded = db.execute('SELECT password_hash FROM accounts WHERE username=?', ('new-user',)).fetchone()[0]
        self.assertTrue(encoded.startswith('pbkdf2:'))
        self.assertNotIn(temporary, encoded)
        payload['username'] = 'NEW-USER'
        self.assertEqual(self.client.post('/api/users', json=payload, headers=self.headers()).status_code, 409)

    def test_owner_and_self_protection_and_permission_escalation(self):
        self.smtp()
        manager = self.create('manager', ['users.manage', 'console.view'])
        client = self.client_for(manager)
        admin = self.create('power-user', list(PERMISSIONS))
        for uid in (self.owner['id'], manager['id'], admin['id']):
            self.assertEqual(client.delete('/api/users/' + uid, headers=self.headers(client)).status_code, 400)
        response = client.post('/api/users', json=dict(username='escalated', email='user@example.com', role='admin'), headers=self.headers(client))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.store.list_users()), 3)

    def test_edit_disable_delete_and_password_changes_revoke_sessions(self):
        user = self.create('operator', ['server.control'])
        client = self.client_for(user)
        payload = dict(username='operator', email='', password='', role='custom', permissions=[], enabled=False)
        result = self.client.put('/api/users/' + user['id'], json=payload, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.get('/api/stats').status_code, 401)
        self.assertIsNone(self.store.authenticate('operator', 'User-password-123'))
        payload['enabled'] = True
        updated = self.client.put('/api/users/' + user['id'], json=payload, headers=self.headers()).json['user']
        client = self.client_for(updated)
        self.assertEqual(self.client.delete('/api/users/' + user['id'], headers=self.headers()).status_code, 200)
        self.assertEqual(client.get('/api/stats').status_code, 401)
        other_owner_session = self.client_for(self.owner)
        result = self.client.post('/api/account/password', json=dict(current_password='Owner-password-123', password='Changed-password-123'), headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(other_owner_session.get('/api/stats').status_code, 401)
        self.assertEqual(self.client.get('/api/stats').status_code, 200)
        self.assertTrue(self.store.authenticate('owner', 'Changed-password-123'))

    def invited_user(self, username='invited', role='custom', permissions=None):
        self.smtp()
        with patch.object(self.security.mailer, 'welcome', return_value='sent') as welcome:
            response = self.client.post('/api/users', json=dict(username=username, email='invited@example.com',
                                        role=role, permissions=permissions or []), headers=self.headers())
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['user'], welcome.call_args.args[1]

    def test_temporary_login_blocks_all_panel_routes_and_sockets_until_password_changes(self):
        user, temporary = self.invited_user(role='admin')
        client = self.app.test_client()
        client.get('/login')
        result = client.post('/login', data=dict(username=user['username'], password=temporary), headers=self.headers(client))
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.location, '/account')
        other_session = self.client_for(user)
        for path in ('/overview', '/settings', '/users', '/console', '/plugins', '/setup'):
            self.assertEqual(client.get(path).location, '/account', path)
        for path in ('/api/stats', '/api/users', '/api/console', '/api/players', '/api/backups/list',
                     '/api/settings/smtp', '/api/minecraft/versions'):
            self.assertEqual(client.get(path).status_code, 403, path)
        for path in ('/api/purge', '/api/import', '/api/command', '/api/settings/smtp/test', '/api/users'):
            self.assertEqual(client.post(path, json={}, headers=self.headers(client)).status_code, 403, path)
        page = client.get('/account')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Set your own password', page.data)
        self.assertNotIn(b'href="/overview"', page.data)
        self.assertNotIn(b'href="/settings"', page.data)
        self.assertNotIn(temporary.encode(), page.data)
        socket = self.module.socketio.test_client(self.app, flask_test_client=client)
        self.assertFalse(socket.is_connected())
        bad = [
            dict(current_password=temporary, password=temporary),
            dict(current_password='incorrect', password='My-new-password-123'),
            dict(current_password=temporary, password='short'),
            dict(current_password=temporary, password='lowercase123!'),
            dict(current_password=temporary, password='Uppercaseonly!'),
            dict(current_password=temporary, password='Uppercase1234'),
            dict(current_password=temporary, password='My-new-password-123', confirm_password='different'),
        ]
        for body in bad:
            result = client.post('/api/account/password', json=body, headers=self.headers(client))
            self.assertEqual(result.status_code, 400)
            self.assertTrue(self.store.authenticate(user['username'], temporary)['must_change_password'])
        body = dict(current_password=temporary, password='My-new-password-123', confirm_password='My-new-password-123')
        self.assertEqual(client.post('/api/account/password', json=body).status_code, 403)
        result = client.post('/api/account/password', json=body, headers=self.headers(client))
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['redirect'], '/overview')
        self.assertEqual(client.get('/api/stats').status_code, 200)
        self.assertEqual(other_session.get('/api/stats').status_code, 401)
        self.assertIsNone(self.store.authenticate(user['username'], temporary))
        changed = self.store.authenticate(user['username'], body['password'])
        self.assertFalse(changed['must_change_password'])
        self.assertIsNone(changed['temporary_password_expires'])
        self.assertNotIn(body['password'], result.get_data(as_text=True))
        client.get('/logout')
        client.get('/login')
        result = client.post('/login', data=dict(username=user['username'], password=body['password']), headers=self.headers(client))
        self.assertEqual(result.location, '/overview')

    def test_temporary_password_expiry_also_invalidates_pending_sessions(self):
        user, temporary = self.invited_user()
        self.assertAlmostEqual(user['temporary_password_expires'] - __import__('time').time(),
                               TEMPORARY_PASSWORD_TTL, delta=5)
        pending = self.client_for(user)
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET temporary_password_expires=0 WHERE id=?', (user['id'],))
        expired_headers = self.headers(pending)
        self.assertIsNone(self.store.authenticate(user['username'], temporary))
        self.assertEqual(pending.get('/account').location, '/login')
        self.assertEqual(pending.post('/api/account/password', json=dict(
            current_password=temporary, password='New-password-123'), headers=expired_headers).status_code, 403)

    def test_resending_invitation_replaces_password_and_revokes_sessions(self):
        user, first = self.invited_user()
        pending = self.client_for(user)
        with patch.object(self.security.mailer, 'welcome', return_value='sent') as welcome:
            result = self.client.post('/api/users/' + user['id'] + '/invite', json={}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        second = welcome.call_args.args[1]
        self.assertNotEqual(first, second)
        self.assertIsNone(self.store.authenticate(user['username'], first))
        self.assertTrue(self.store.authenticate(user['username'], second)['must_change_password'])
        self.assertEqual(pending.get('/api/stats').status_code, 401)
        self.assertNotIn(second, result.get_data(as_text=True))
        self.assertNotIn(second, self.client.get('/api/users').get_data(as_text=True))

    def test_temporary_password_reset_revokes_existing_password_and_console_access(self):
        user = self.create('regular', ['console.view', 'console.command'])
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET email=? WHERE id=?', ('regular@example.com', user['id']))
        client = self.client_for(user)
        socket = self.module.socketio.test_client(self.app, flask_test_client=client)
        self.addCleanup(lambda: socket.disconnect() if socket.is_connected() else None)
        self.assertTrue(socket.is_connected())
        self.smtp()
        with patch.object(self.security.mailer, 'welcome', return_value='sent') as welcome:
            result = self.client.post('/api/users/' + user['id'] + '/invite', json={}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        self.assertIsNone(self.store.authenticate('regular', 'User-password-123'))
        self.assertEqual(client.get('/api/stats').status_code, 401)
        self.assertIn('error', socket.emit('send_command', {'command': 'op someone'}, callback=True))
        self.security.emit_console(self.module.socketio, 'must not reach the old session')
        self.assertEqual(socket.get_received(), [])
        self.assertTrue(self.store.authenticate('regular', welcome.call_args.args[1])['must_change_password'])

    def test_invitation_requires_email_smtp_and_notifications_without_creating_accounts(self):
        body = dict(username='needs-email', email='user@example.com', role='custom', permissions=[])
        with patch.object(self.security.mailer, 'welcome') as welcome:
            for config, email in (({'enabled': False}, 'user@example.com'),
                                  ({'notify_new_users': False}, 'user@example.com'),
                                  ({}, ''), ({}, 'invalid')):
                self.smtp(**config)
                result = self.client.post('/api/users', json={**body, 'email': email}, headers=self.headers())
                self.assertEqual(result.status_code, 400, result.json)
                self.assertEqual(len(self.store.list_users()), 1)
            welcome.assert_not_called()

    def test_clients_cannot_preset_passwords_or_bypass_change_requirement(self):
        self.smtp()
        body = dict(username='generated', email='user@example.com', role='custom', permissions=[])
        result = self.client.post('/api/users', json={**body, 'password': 'Admin-chosen-123'}, headers=self.headers())
        self.assertEqual(result.status_code, 400)
        with patch.object(self.security.mailer, 'welcome', return_value='sent'):
            result = self.client.post('/api/users', json={**body, 'must_change_password': False,
                                      'temporary_password_expires': None}, headers=self.headers())
        user = result.json['user']
        self.assertTrue(user['must_change_password'])
        result = self.client.put('/api/users/' + user['id'], json={**body, 'password': 'Admin-chosen-123'}, headers=self.headers())
        self.assertEqual(result.status_code, 400)
        result = self.client.put('/api/users/' + user['id'], json={**body, 'must_change_password': False}, headers=self.headers())
        self.assertTrue(result.json['user']['must_change_password'])
        self.assertIsNone(self.store.authenticate('generated', 'Admin-chosen-123'))

    def test_invitation_permissions_csrf_owner_and_self_protection(self):
        admin, unused = self.invited_user('pending-admin', role='admin')
        manager = self.create('manager', ['users.manage'])
        manager_client = self.client_for(manager)
        viewer = self.client_for(self.create())
        with patch.object(self.security.mailer, 'welcome') as welcome:
            path = '/api/users/' + admin['id'] + '/invite'
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
            self.assertEqual(viewer.post(path, json={}, headers=self.headers(viewer)).status_code, 403)
            self.assertEqual(manager_client.post(path, json={}, headers=self.headers(manager_client)).status_code, 400)
            for target in (self.owner, manager):
                client = self.client if target == self.owner else manager_client
                result = client.post('/api/users/' + target['id'] + '/invite', json={}, headers=self.headers(client))
                self.assertEqual(result.status_code, 400)
            welcome.assert_not_called()

    def test_changing_invitation_email_expires_the_old_temporary_password(self):
        user, temporary = self.invited_user()
        body = {**user, 'email': 'corrected@example.com'}
        result = self.client.put('/api/users/' + user['id'], json=body, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['user']['temporary_password_expires'], 0)
        self.assertIsNone(self.store.authenticate(user['username'], temporary))

    def test_invitation_mail_failure_never_leaks_temporary_password(self):
        self.smtp()
        user, password = self.store.invite_user(dict(username='mail-failed', email='private@example.com'), self.owner)
        with patch.object(self.store, 'invite_user', return_value=(user, password)), \
             patch.object(self.security.mailer, 'welcome', side_effect=OSError(password)), \
             self.assertLogs(self.app.logger, level='WARNING') as captured:
            result = self.client.post('/api/users/' + user['id'] + '/invite', json={}, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['email_status'], 'failed')
        for private in (password, 'private@example.com'):
            self.assertNotIn(private, '\n'.join(captured.output))
        self.assertNotIn(password, result.get_data(as_text=True))
        self.assertTrue(self.store.authenticate(user['username'], password)['must_change_password'])

    def test_disabled_mail_cannot_rotate_an_existing_password(self):
        user, temporary = self.invited_user()
        self.smtp(enabled=False)
        with patch.object(self.security.mailer, 'welcome') as welcome:
            result = self.client.post('/api/users/' + user['id'] + '/invite', json={}, headers=self.headers())
        self.assertEqual(result.status_code, 400)
        self.assertTrue(self.store.authenticate(user['username'], temporary))
        welcome.assert_not_called()

    def test_read_only_plugin_permissions(self):
        plugin_dir = self.root / 'plugins'
        plugin_dir.mkdir()
        (plugin_dir / 'config.yml').write_text('enabled: true\n', encoding='utf-8')
        client = self.client_for(self.create('reader', ['plugins.view']))
        self.assertEqual(client.get('/plugins').status_code, 200)
        result = client.get('/api/plugins/file?path=config.yml')
        self.assertEqual(result.status_code, 200)
        result = client.put('/api/plugins/file', json={'path': 'config.yml', 'content': 'bad'}, headers=self.headers(client))
        self.assertEqual(result.status_code, 403)
        self.assertEqual((plugin_dir / 'config.yml').read_text(), 'enabled: true\n')
        self.assertEqual(client.get('/api/plugins/delete?path=Demo.jar').status_code, 403)
        self.assertEqual(client.delete('/api/plugins/delete', json={'path': 'Demo.jar'}, headers=self.headers(client)).status_code, 403)

    def test_plugin_manager_can_delete_but_requires_both_csrf_tokens(self):
        plugin_dir = self.root / 'plugins'
        plugin_dir.mkdir()
        target = plugin_dir / 'Demo.jar'
        target.write_bytes(b'installed jar')
        client = self.client_for(self.create('manager', ['plugins.manage']))
        preview = client.get('/api/plugins/delete?path=Demo.jar')
        self.assertEqual(preview.status_code, 200, preview.json)
        headers = self.headers(client)
        for missing in ('X-Panel-CSRF', 'X-Plugin-CSRF-Token'):
            without = {key: value for key, value in headers.items() if key != missing}
            response = client.delete('/api/plugins/delete', json=preview.json, headers=without)
            self.assertEqual(response.status_code, 403)
            self.assertTrue(target.exists())
        response = client.delete('/api/plugins/delete', json=preview.json, headers=headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(target.exists())

    def test_plugin_file_creation_requires_management_and_both_csrf_tokens(self):
        directory = self.root / 'plugins' / 'LuckPerms'
        directory.mkdir(parents=True)
        payload = {'path': 'LuckPerms', 'name': 'messages.yml'}
        reader = self.client_for(self.create('reader', ['plugins.view']))
        self.assertEqual(reader.post('/api/plugins/create', json=payload,
                                     headers=self.headers(reader)).status_code, 403)
        manager = self.client_for(self.create('manager', ['plugins.manage']))
        headers = self.headers(manager)
        for missing in ('X-Panel-CSRF', 'X-Plugin-CSRF-Token'):
            without = {key: value for key, value in headers.items() if key != missing}
            self.assertEqual(manager.post('/api/plugins/create', json=payload,
                                          headers=without).status_code, 403)
        self.assertFalse((directory / 'messages.yml').exists())
        result = manager.post('/api/plugins/create', json=payload, headers=headers)
        self.assertEqual(result.status_code, 201, result.json)
        self.assertEqual((directory / 'messages.yml').read_bytes(), b'')

    def test_plugin_data_upload_requires_management_and_both_csrf_tokens(self):
        directory = self.root / 'plugins' / 'LuckPerms'
        directory.mkdir(parents=True)
        def upload(client, headers):
            return client.post('/api/plugins/upload', headers=headers,
                               data={'path': 'LuckPerms', 'file': (io.BytesIO(b'\x1f\x8bdata'), 'preset.json.gz')})
        reader = self.client_for(self.create('reader', ['plugins.view']))
        self.assertEqual(upload(reader, self.headers(reader)).status_code, 403)
        manager = self.client_for(self.create('manager', ['plugins.manage']))
        headers = self.headers(manager)
        for missing in ('X-Panel-CSRF', 'X-Plugin-CSRF-Token'):
            self.assertEqual(upload(manager, {key: value for key, value in headers.items() if key != missing}).status_code, 403)
        self.assertFalse((directory / 'preset.json.gz').exists())
        response = upload(manager, headers)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual((directory / 'preset.json.gz').read_bytes(), b'\x1f\x8bdata')

    def prepare_smtp_mock(self, factory):
        smtp = factory.return_value
        smtp.ehlo.return_value = (250, b'Test SMTP')
        smtp.esmtp_features = {'auth': 'PLAIN LOGIN'}
        smtp.has_extn.return_value = True
        smtp.auth.return_value = (235, b'Authenticated')
        return smtp

    def test_smtp_tls_ssl_password_retention_and_validation(self):
        config = self.smtp()
        self.assertTrue(config['password_set'])
        self.assertNotIn('password', config)
        config['password'] = ''
        self.security.mailer.save(config)
        self.assertEqual(self.security.mailer.config()['password'], 'smtp-private-secret')
        with patch('panel_mail.smtplib.SMTP') as factory:
            smtp = self.prepare_smtp_mock(factory)
            self.security.mailer.send('test@example.com', 'Test', 'body')
            smtp.starttls.assert_called_once()
            smtp.auth.assert_called_once_with('PLAIN', smtp.auth_plain)
            self.assertEqual((smtp.user, smtp.password), ('smtp-user', 'smtp-private-secret'))
            self.assertEqual(factory.call_args.kwargs['timeout'], 30)
            smtp.send_message.assert_called_once()
        self.smtp(security='ssl', port=465)
        with patch('panel_mail.smtplib.SMTP_SSL') as factory:
            smtp = self.prepare_smtp_mock(factory)
            self.security.mailer.send('test@example.com', 'Test', 'body')
            smtp.starttls.assert_not_called()
            self.assertIn('context', factory.call_args.kwargs)
        with self.assertRaises(ValueError):
            self.smtp(security='none')
        with self.assertRaises(ValueError):
            self.smtp(from_email='valid@example.com\r\nBcc: bad@example.com')
        with self.assertRaises(ValueError):
            self.smtp(panel_url='javascript:alert(1)')

    def test_smtp_settings_api_test_delivery_and_clear_password(self):
        data = {**DEFAULT_SMTP, 'enabled': True, 'host': 'smtp.example.com',
                'username': 'smtp-user', 'password': 'smtp-private-secret',
                'from_email': 'panel@example.com', 'panel_url': 'https://panel.example.com'}
        result = self.client.put('/api/settings/smtp', json=data, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('smtp-private-secret', result.get_data(as_text=True))
        with patch('panel_mail.smtplib.SMTP') as smtp:
            connection = self.prepare_smtp_mock(smtp)
            result = self.client.post('/api/settings/smtp/test', json={'recipient': 'test@example.com'}, headers=self.headers())
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json['success'])
            connection.send_message.assert_called_once()
        data.update(enabled=False, password='', clear_password=True)
        result = self.client.put('/api/settings/smtp', json=data, headers=self.headers())
        self.assertFalse(result.json['smtp']['password_set'])

    def test_invitation_and_resend_use_the_latest_saved_server_name(self):
        self.smtp()
        self.config['server_name'] = 'Byte Community'
        self.config['panel_name'] = 'Legacy Panel Branding'
        self.module.save_config(self.config)
        body = dict(username='named-panel', email='new@example.com', role='custom', permissions=[])
        with patch('panel_mail.smtplib.SMTP') as factory:
            smtp = self.prepare_smtp_mock(factory)
            response = self.client.post('/api/users', json=body, headers=self.headers())
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json['email_status'], 'sent')
            self.assertEqual(smtp.send_message.call_count, 2)
            first = smtp.send_message.call_args_list[0].args[0]
            self.assertIn('Byte Community', first['Subject'])
            for kind in ('plain', 'html'):
                self.assertIn('Welcome to Byte Community', first.get_body(preferencelist=(kind,)).get_content())
                self.assertNotIn('Legacy Panel Branding', first.get_body(preferencelist=(kind,)).get_content())
            self.config['server_name'] = 'Renamed Community'
            self.module.save_config(self.config)
            result = self.client.post('/api/users/' + str(response.json['user']['id']) + '/invite',
                                      json={}, headers=self.headers())
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['email_status'], 'sent')
            self.assertEqual(smtp.send_message.call_count, 4)
            resent = smtp.send_message.call_args_list[2].args[0]
            self.assertIn('Renamed Community', resent['Subject'])
            for kind in ('plain', 'html'):
                self.assertIn('Welcome to Renamed Community', resent.get_body(preferencelist=(kind,)).get_content())
                self.assertNotIn('{{servername}}', resent.get_body(preferencelist=(kind,)).get_content())
            self.assertIn('Renamed Community', smtp.send_message.call_args_list[3].args[0]['Subject'])
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('<code>{{servername}}</code>', page)

    def test_welcome_email_and_failure_keep_created_account(self):
        self.smtp()
        body = dict(username='mailed', email='new@example.com', role='custom', permissions=[])
        with patch('panel_mail.smtplib.SMTP') as factory:
            smtp = self.prepare_smtp_mock(factory)
            response = self.client.post('/api/users', json=body, headers=self.headers())
            self.assertEqual(response.json['email_status'], 'sent')
            message = smtp.send_message.call_args.args[0]
            self.assertEqual(smtp.send_message.call_count, 2)
            welcome = smtp.send_message.call_args_list[0].args[0]
            self.assertEqual(welcome.get_content_type(), 'multipart/alternative')
            self.assertEqual(message.get_content_type(), 'multipart/alternative')
            for kind in ('plain', 'html'):
                self.assertIn('https://panel.example.com/login', welcome.get_body(preferencelist=(kind,)).get_content())
                self.assertIn('second email', welcome.get_body(preferencelist=(kind,)).get_content())
            temporary = message.get_body(preferencelist=('plain',)).get_content().split('Temporary password: ')[1].splitlines()[0]
            self.assertGreaterEqual(len(temporary), 24)
            self.assertTrue(self.store.authenticate('mailed', temporary))
            self.assertIn('24 hours', message.get_body(preferencelist=('plain',)).get_content())
            self.assertIn(html.escape(temporary), message.get_body(preferencelist=('html',)).get_content())
            self.assertIn('Your login details:', welcome.get_body(preferencelist=('html',)).get_content())
            self.assertNotIn(temporary, welcome.as_string())
            self.mail_delay.assert_called_once_with(12)
            self.assertNotIn(temporary, response.get_data(as_text=True))
        body['username'] = 'failed-mail'
        with patch('panel_mail.smtplib.SMTP', side_effect=OSError('connection failed')):
            response = self.client.post('/api/users', json=body, headers=self.headers())
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json['email_status'], 'failed')
            self.assertTrue(response.json['user']['must_change_password'])
            self.assertIn('failed-mail', [user['username'] for user in self.store.list_users()])
            result = self.client.post('/api/settings/smtp/test', json={'recipient': 'test@example.com'}, headers=self.headers())
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json['success'])
            self.assertEqual(result.json['error_code'], 'connection_failed')
            self.assertNotIn('smtp-private-secret', result.get_data(as_text=True))

    def test_welcome_embeds_active_server_icon_and_resend_uses_current_icon(self):
        from PIL import Image
        from test_server_icon import image_bytes
        self.smtp()
        self.assertEqual(self.upload_test_icon(image_bytes(size=(64, 64), color='red')).status_code, 200)
        with patch('panel_mail.smtplib.SMTP') as factory:
            smtp = self.prepare_smtp_mock(factory)
            result = self.client.post('/api/users', json=dict(username='iconinvite', email='new@example.com'),
                                      headers=self.headers())
            self.assertEqual(result.status_code, 201)
            self.assertEqual(result.json['email_status'], 'sent')
            welcome, password = [call.args[0] for call in smtp.send_message.call_args_list]
            images = [part for part in welcome.walk() if part.get_content_type() == 'image/png']
            self.assertEqual(len(images), 1)
            cid = str(images[0]['Content-ID'])[1:-1]
            markup = welcome.get_body(preferencelist=('html',)).get_content()
            self.assertIn('src="cid:' + cid + '"', markup)
            self.assertLess(markup.index('src="cid:'), markup.index('Welcome to Test Server'))
            with Image.open(io.BytesIO(images[0].get_payload(decode=True))) as image:
                self.assertEqual(image.size, (64, 64))
                self.assertEqual(image.getpixel((32, 32)), (255, 0, 0, 255))
            self.assertEqual(images[0].get_content_disposition(), 'inline')
            self.assertNotIn('image/png', [part.get_content_type() for part in password.walk()])
            temporary = password.get_body(preferencelist=('plain',)).get_content().split('Temporary password: ')[1].splitlines()[0]
            self.assertNotIn(temporary, welcome.get_body(preferencelist=('plain',)).get_content())
            self.assertNotIn(html.escape(temporary), markup)
            self.assertNotIn('cid:', welcome.get_body(preferencelist=('plain',)).get_content())
            self.assertNotIn('cid:', password.get_body(preferencelist=('html',)).get_content())
            self.mail_delay.assert_called_once_with(12)
            self.assertEqual(self.upload_test_icon(image_bytes(size=(64, 64), color='blue')).status_code, 200)
            resend = self.client.post('/api/users/' + result.json['user']['id'] + '/invite', json={}, headers=self.headers())
            self.assertEqual(resend.status_code, 200)
            resent = smtp.send_message.call_args_list[2].args[0]
            new_icon = next(part for part in resent.walk() if part.get_content_type() == 'image/png')
            self.assertNotEqual(new_icon['Content-ID'], images[0]['Content-ID'])
            with Image.open(io.BytesIO(new_icon.get_payload(decode=True))) as image:
                self.assertEqual(image.getpixel((32, 32)), (0, 0, 255, 255))

    def test_password_email_failure_reports_partial_delivery_without_leaking_secrets(self):
        self.smtp()
        def reject_password(message):
            if 'Temporary password:' in message.get_body(preferencelist=('plain',)).get_content():
                # Providers may echo message content in an error; it must stay private.
                raise smtplib.SMTPDataError(554, message.as_bytes())
        with patch('panel_mail.smtplib.SMTP') as factory, self.assertLogs(self.app.logger, level='WARNING') as captured:
            smtp = self.prepare_smtp_mock(factory)
            smtp.send_message.side_effect = reject_password
            response = self.client.post('/api/users', json=dict(username='partial-email', email='new@example.com'),
                                        headers=self.headers())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['email_status'], 'password_failed')
        self.assertTrue(response.json['user']['must_change_password'])
        self.assertEqual(smtp.send_message.call_count, 2)
        self.assertEqual(factory.call_count, 2)
        welcome, password = [call.args[0] for call in smtp.send_message.call_args_list]
        self.assertNotEqual(welcome['Message-ID'], password['Message-ID'])
        temporary = password.get_body(preferencelist=('plain',)).get_content().split('Temporary password: ')[1].splitlines()[0]
        self.assertNotIn(temporary, welcome.as_string())
        self.assertNotIn(temporary, response.get_data(as_text=True))
        self.assertNotIn(temporary, '\n'.join(captured.output))
        self.assertIn('part=password', '\n'.join(captured.output))
        self.assertIn('smtp_code=554', '\n'.join(captured.output))
        self.assertTrue(self.store.authenticate('partial-email', temporary))
        self.mail_delay.assert_called_once_with(12)

    def test_cancelled_second_email_is_reported_by_the_api(self):
        self.smtp()
        def disable_account(seconds):
            self.assertEqual(seconds, 12)
            target = next(user for user in self.store.list_users() if user['username'] == 'cancelled-email')
            self.store.save_user({**target, 'enabled': False}, self.owner, target['id'])
        self.mail_delay.side_effect = disable_account
        with patch('panel_mail.smtplib.SMTP') as factory:
            smtp = self.prepare_smtp_mock(factory)
            response = self.client.post('/api/users', json=dict(username='cancelled-email', email='new@example.com'),
                                        headers=self.headers())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['email_status'], 'cancelled')
        self.assertEqual(smtp.send_message.call_count, 1)
        self.assertNotIn('Temporary password:', smtp.send_message.call_args.args[0].get_body(preferencelist=('plain',)).get_content())

    def test_delayed_password_rechecks_account_edits_resends_deletion_expiry_and_reset(self):
        self.smtp()
        # Reset is last because it also removes this test's owner account.
        for change in ('rename', 'email', 'disable', 'delete', 'resend', 'password', 'expire', 'reset'):
            with self.subTest(change=change):
                user, temporary = self.store.invite_user(dict(username='delayed-' + change, email='new@example.com'), self.owner)
                self.assertTrue(self.store.invitation_is_current(user))
                def invalidate(seconds):
                    self.assertEqual(seconds, 12)
                    if change in ('rename', 'email', 'disable'):
                        updates = {'rename': {'username': 'renamed-user'}, 'email': {'email': 'new-address@example.com'},
                                   'disable': {'enabled': False}}[change]
                        self.store.save_user({**user, **updates}, self.owner, user['id'])
                    elif change == 'delete':
                        self.store.delete_user(user['id'], self.owner)
                    elif change == 'resend':
                        self.store.invite_user({}, self.owner, user['id'])
                    elif change == 'password':
                        self.store.change_password(user, temporary, 'Chosen-password-123')
                    elif change == 'expire':
                        with self.store.connection() as db:
                            db.execute('UPDATE accounts SET temporary_password_expires=0 WHERE id=?', (user['id'],))
                    else:
                        self.store.reset()
                    self.assertFalse(self.store.invitation_is_current(user))
                self.mail_delay.side_effect = invalidate
                with patch.object(self.security.mailer, 'send') as send:
                    self.assertEqual(self.security.mailer.welcome(user, temporary), 'cancelled')
                self.assertEqual(send.call_count, 1)
                self.assertNotIn(temporary, send.call_args.args[2])

    def test_smtp_test_diagnostics_are_json_and_never_expose_provider_secrets(self):
        self.smtp()
        secret = 'smtp-private-secret'
        failures = [
            (smtplib.SMTPAuthenticationError(535, secret.encode()), 'authentication_failed'),
            (ssl.SSLCertVerificationError(1, secret), 'certificate_failed'),
            (ssl.SSLError(1, secret), 'tls_failed'),
            (socket.gaierror(-2, secret), 'dns_failed'),
            (TimeoutError(secret), 'connection_timeout'),
            (ConnectionRefusedError(secret), 'connection_refused'),
            (smtplib.SMTPSenderRefused(550, secret.encode(), 'private@example.com'), 'sender_rejected'),
            (smtplib.SMTPRecipientsRefused({'private@example.com': (550, secret.encode())}), 'recipient_rejected'),
            (smtplib.SMTPDataError(554, secret.encode()), 'message_rejected'),
            (smtplib.SMTPNotSupportedError(secret), 'smtp_feature_unsupported'),
            (smtplib.SMTPServerDisconnected(secret), 'server_disconnected'),
            (smtplib.SMTPConnectError(421, secret.encode()), 'smtp_rejected'),
            (OSError(secret), 'connection_failed'),
            (RuntimeError(secret), 'delivery_failed'),
        ]
        for error, code in failures:
            with self.subTest(code=code), patch.object(self.security.mailer, 'send', side_effect=error):
                with self.assertLogs(self.app.logger, level='WARNING') as captured:
                    result = self.client.post('/api/settings/smtp/test', json={'recipient': 'test@example.com'}, headers=self.headers())
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.mimetype, 'application/json')
                self.assertFalse(result.json['success'])
                self.assertEqual(result.json['status'], 'error')
                self.assertEqual(result.json['error_code'], code)
                self.assertTrue(result.json['error'])
                # Older cached scripts read message without checking success/error.
                self.assertEqual(result.json['message'], result.json['error'])
                self.assertEqual(result.headers['Cache-Control'], 'no-store')
                for private in (secret, 'private@example.com'):
                    self.assertNotIn(private, result.get_data(as_text=True))
                    self.assertNotIn(private, '\n'.join(captured.output))
                if code == 'authentication_failed':
                    self.assertEqual(result.json['smtp_code'], 535)
                    self.assertIn('SMTP 535', result.json['error'])

    def test_smtp_failure_stage_reaches_the_api_without_leaking_credentials(self):
        self.smtp()
        with patch('panel_mail.smtplib.SMTP') as factory:
            smtp = self.prepare_smtp_mock(factory)
            smtp.auth.side_effect = smtplib.SMTPAuthenticationError(535, b'smtp-private-secret private@example.com')
            smtp.close.side_effect = OSError('cleanup-private-secret')
            with self.assertLogs(self.app.logger, level='WARNING') as captured:
                result = self.client.post('/api/settings/smtp/test', json={'recipient': 'test@example.com'}, headers=self.headers())
        self.assertFalse(result.json['success'])
        self.assertEqual(result.json['error_code'], 'authentication_failed')
        self.assertEqual(result.json['stage'], 'authentication')
        self.assertEqual(result.json['smtp_code'], 535)
        self.assertIsInstance(result.json['elapsed_seconds'], (int, float))
        self.assertIn('authenticating', result.json['message'])
        self.assertIn('No message was submitted', result.json['message'])
        self.assertIn('stage=authentication', '\n'.join(captured.output))
        for private in ('smtp-private-secret', 'private@example.com', 'cleanup-private-secret'):
            self.assertNotIn(private, result.get_data(as_text=True))
            self.assertNotIn(private, '\n'.join(captured.output))
        smtp.send_message.assert_not_called()
        smtp.quit.assert_not_called()

    def test_account_assets_are_versioned_to_avoid_stale_smtp_scripts(self):
        version = self.module.inject_version()['app_version']
        for path in ('/settings', '/users', '/account'):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200)
            markup = result.get_data(as_text=True)
            self.assertIn('accounts.js?v=' + version, markup)
            self.assertIn('accounts.css?v=' + version, markup)
        markup = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('id="smtp-state"', markup)
        self.assertIn('aria-describedby="smtp-state" disabled', markup)

    def test_smtp_invalid_settings_and_recipient_do_not_attempt_delivery(self):
        with patch.object(self.security.mailer, 'send') as send:
            result = self.client.post('/api/settings/smtp/test', json={'recipient': 'test@example.com'}, headers=self.headers())
            self.assertEqual(result.status_code, 400)
            self.assertIn('Enable and save', result.json['error'])
            self.smtp()
            for body in ({'recipient': 'bad address'}, [], None):
                result = self.client.post('/api/settings/smtp/test', json=body, headers=self.headers())
                self.assertEqual(result.status_code, 400)
                self.assertTrue(result.is_json)
            send.assert_not_called()

    def test_reset_removes_all_users_and_old_sessions_but_keeps_smtp(self):
        viewer = self.client_for(self.create())
        self.smtp()
        old_scope = self.store.scope()
        response = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.list_users(), [])
        self.assertNotEqual(self.store.scope(), old_scope)
        self.assertEqual(viewer.get('/api/stats').status_code, 401)
        self.assertTrue(self.security.mailer.config()['enabled'])
        self.assertNotIn('admin_pass', self.module.load_config())
        self.assertEqual(self.client.get('/setup').status_code, 200)
        result = self.client.post('/api/bootstrap', json=dict(username='new-owner', password='Fresh-password-123'), headers=self.headers())
        self.assertEqual(result.status_code, 201, result.json)
        self.assertEqual(result.json['user']['role'], 'owner')

    def seed_player_records(self):
        with closing(sqlite3.connect(self.module.PLAYER_DB_FILE)) as conn, conn:
            conn.execute("INSERT INTO users (uuid, username, online_status, ban_status) VALUES (?, ?, 1, 1)",
                         ('old-player-uuid', 'OldPlayer'))
            conn.execute("INSERT INTO login_events (uuid, timestamp, ip, country) VALUES (?, ?, ?, ?)",
                         ('old-player-uuid', '2026-11-16', '192.0.2.1', 'Test Country'))
            conn.execute("INSERT INTO ban_events (uuid, action, reason) VALUES (?, ?, ?)",
                         ('old-player-uuid', 'ban', 'Old server ban'))
        self.module.save_player_history({'OldPlayer': {'uuid': 'old-player-uuid'}})

    def player_record_counts(self):
        with closing(sqlite3.connect(self.module.PLAYER_DB_FILE)) as conn:
            return [conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                    for table in ('users', 'login_events', 'ban_events')]

    def test_purge_clears_player_list_and_histories_for_the_next_server(self):
        self.seed_player_records()
        marker = self.root / 'server.properties'
        marker.write_text('motd=old server')
        backup = self.data / 'backups' / 'keep-player-purge.zip'
        backup.parent.mkdir(exist_ok=True)
        backup.write_bytes(b'Existing backup')
        branding = self.data / 'branding' / 'keep-player-purge.png'
        branding.write_bytes(b'Existing branding')
        self.assertEqual(len(self.client.get('/api/players?skipSync=1').json), 1)
        response = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.player_record_counts(), [0, 0, 0])
        self.assertFalse((self.data / 'player_history.json').exists())
        self.assertEqual(self.module.load_player_history(), {})
        self.assertFalse(marker.exists())
        self.assertEqual(backup.read_bytes(), b'Existing backup')
        self.assertEqual(branding.read_bytes(), b'Existing branding')

        # Simulate setup of the next server and query the actual player endpoints.
        owner = self.store.bootstrap('next-owner', 'Fresh-password-123')
        self.config['admin_user'] = owner['username']
        self.module.save_config(self.config)
        client = self.client_for(owner)
        self.assertEqual(client.get('/api/players').json, [])
        self.assertEqual(client.get('/api/player/old-player-uuid/history').json, [])
        self.assertEqual(client.get('/api/player/old-player-uuid/ban_history').json, [])
        self.assertNotIn(b'OldPlayer', client.get('/players').data)
        # Tables remain usable, and repeating an empty purge is safe.
        with closing(sqlite3.connect(self.module.PLAYER_DB_FILE)) as conn, conn:
            conn.execute("INSERT INTO users (uuid, username) VALUES ('new-player', 'NewPlayer')")
        self.assertEqual(client.get('/api/players').json[0]['username'], 'NewPlayer')
        self.assertEqual(client.post('/api/purge', headers=self.headers(client)).status_code, 200)
        self.assertEqual(self.player_record_counts(), [0, 0, 0])

    def test_purge_drains_buffered_player_logins_before_clearing(self):
        process = Mock()
        process.poll.return_value = None
        log_thread = Mock()
        log_thread.is_alive.return_value = False
        # Simulate a login already buffered by the old process as its reader exits.
        log_thread.join.side_effect = lambda timeout: self.seed_player_records()
        self.module.MINECRAFT_PROCESS = process
        self.module.MINECRAFT_LOG_THREAD = log_thread
        with patch.object(self.module, 'stop_minecraft_server', return_value=True) as stop:
            response = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.json)
        stop.assert_called_once()
        process.wait.assert_called_once_with(timeout=30)
        log_thread.join.assert_called_once_with(timeout=10)
        self.assertEqual(self.player_record_counts(), [0, 0, 0])
        self.assertFalse((self.data / 'player_history.json').exists())
        self.assertIsNone(self.module.MINECRAFT_LOG_THREAD)

    def test_purge_waits_for_detached_server_process(self):
        self.seed_player_records()
        self.config['server_pid'] = 123456
        self.module.save_config(self.config)
        with patch.object(self.module.psutil, 'pid_exists', return_value=True), \
             patch.object(self.module.psutil, 'Process') as process, \
             patch.object(self.module, 'stop_minecraft_server', return_value=True) as stop:
            response = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.json)
        stop.assert_called_once()
        process.assert_called_once_with(123456)
        process.return_value.wait.assert_called_once_with(timeout=30)
        self.assertEqual(self.player_record_counts(), [0, 0, 0])

    def test_purge_aborts_if_server_or_log_reader_is_still_active(self):
        self.seed_player_records()
        marker = self.root / 'keep.txt'
        marker.write_text('keep')
        scope = self.store.scope()
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired('java', 30)
        self.module.MINECRAFT_PROCESS = process
        with patch.object(self.module, 'stop_minecraft_server', return_value=True):
            response = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.player_record_counts(), [1, 1, 1])
        self.assertTrue(marker.exists())
        self.assertEqual(self.store.scope(), scope)

        self.module.MINECRAFT_PROCESS = None
        log_thread = Mock()
        log_thread.is_alive.return_value = True
        self.module.MINECRAFT_LOG_THREAD = log_thread
        response = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.player_record_counts(), [1, 1, 1])
        self.assertTrue(marker.exists())
        self.assertEqual(self.store.scope(), scope)

    def test_purge_reports_player_cleanup_failure_without_resetting_server(self):
        self.seed_player_records()
        marker = self.root / 'keep.txt'
        marker.write_text('keep')
        scope = self.store.scope()
        with patch.object(self.module, 'clear_player_data', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertLogs(self.app.logger, level='ERROR'):
                response = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(response.status_code, 500)
        self.assertIn('Unable to clear player history', response.json['error'])
        self.assertEqual(self.player_record_counts(), [1, 1, 1])
        self.assertTrue(marker.exists())
        self.assertEqual(self.store.scope(), scope)
        self.assertTrue(self.module.load_config()['is_setup'])

    def test_player_clear_rolls_back_database_if_legacy_file_cannot_be_removed(self):
        self.seed_player_records()
        with patch.object(self.module.os, 'remove', side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):
                self.module.clear_player_data()
        self.assertEqual(self.player_record_counts(), [1, 1, 1])
        self.assertTrue((self.data / 'player_history.json').exists())

    def zip_bytes(self, files=None):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, 'w') as archive:
            for name, value in (files or {'ServerEngineInfo': json.dumps(self.config), 'server.properties': 'motd=Imported'}).items():
                archive.writestr(name, value)
        return contents.getvalue()

    def test_welcome_heading_and_description_are_explicitly_white(self):
        self.store.reset()
        self.config.update(is_setup=False, welcome_seen=False)
        self.module.save_config(self.config)
        page = self.app.test_client().get('/welcome')
        self.assertEqual(page.status_code, 200)
        markup = page.get_data(as_text=True)
        self.assertRegex(markup, r'h1\s*\{\s*color:\s*#fff;')
        self.assertRegex(markup, r'p\s*\{\s*color:\s*#fff;')
        self.assertNotIn('color: var(--text-main)', markup)

    def test_only_setup_owner_can_purge_even_for_full_access_or_legacy_permissions(self):
        admin = self.store.save_user(dict(username='administrator', password='User-password-123',
                                          role='admin', permissions=[]), self.owner)
        full = self.create('full-custom', list(PERMISSIONS))
        forged = self.create('legacy-reset')
        with self.store.connection() as db:
            db.execute('UPDATE accounts SET permissions=? WHERE id=?',
                       (json.dumps(['server.reset', 'server.purge', 'settings.manage']), forged['id']))
        marker = self.root / 'keep.txt'
        marker.write_text('keep')
        scope = self.store.scope()
        self.assertIn('server.purge', grants(self.owner))
        self.assertNotIn('server.purge', PERMISSIONS)
        self.assertIn('onclick="startPurgeWizard()"', self.client.get('/settings').get_data(as_text=True))
        for user in (admin, full, forged):
            with self.subTest(role=user['role'], user=user['username']):
                client = self.client_for(user)
                with patch.object(self.module, 'clear_player_data') as clear, \
                     patch.object(self.store, 'reset') as reset:
                    result = client.post('/api/purge', headers=self.headers(client))
                self.assertEqual(result.status_code, 403)
                self.assertIn('original setup owner', result.json['error'])
                clear.assert_not_called()
                reset.assert_not_called()
                self.assertNotIn('onclick="startPurgeWizard()"', client.get('/settings').get_data(as_text=True))
        self.assertTrue(marker.exists())
        self.assertEqual(self.store.scope(), scope)

    def test_owner_capability_cannot_be_assigned_or_created_through_user_api(self):
        target = self.create('edit-target')
        admin = self.store.save_user(dict(username='administrator', password='User-password-123',
                                          role='admin', permissions=[]), self.owner)
        for client in (self.client, self.client_for(admin)):
            page = client.get('/users').get_data(as_text=True)
            self.assertIn('value="admin"', page)
            self.assertIn('all permissions except purge', page)
            self.assertNotIn('value="server.purge"', page)
            self.assertIn('value="backups.create"', page)
            self.assertIn('value="backups.restore"', page)
            for permissions, role in ((['server.purge'], 'custom'), (['server.reset'], 'custom'), ([], 'owner')):
                result = client.put('/api/users/' + target['id'], json=dict(
                    username=target['username'], role=role, permissions=permissions), headers=self.headers(client))
                self.assertEqual(result.status_code, 400)
        self.assertEqual([user['id'] for user in self.store.list_users() if user['role'] == 'owner'],
                         [self.owner['id']])

    def test_backup_permission_matrix_and_rendered_actions(self):
        for name, permissions in (('creator', ['backups.create']), ('restorer', ['backups.restore']),
                                  ('both', ['backups.create', 'backups.restore']), ('neither', [])):
            with self.subTest(name=name):
                client = self.client_for(self.create(name, permissions))
                may_create = 'backups.create' in permissions
                may_restore = 'backups.restore' in permissions
                allowed = may_create or may_restore
                page = client.get('/backups')
                self.assertEqual(page.status_code, 200 if allowed else 403)
                self.assertEqual(client.get('/api/backups/list').status_code, 200 if allowed else 403)
                for url in ('/overview', '/account'):
                    self.assertEqual('href="/backups"' in client.get(url).get_data(as_text=True), allowed)
                with patch.object(self.module, 'create_backup', return_value=str(self.root / 'dummy.zip')) as create:
                    result = client.post('/api/backup/create', json={'is_restore': True},
                                         headers=self.headers(client))
                    self.assertEqual(result.status_code, 200 if may_create else 403)
                    self.assertEqual(create.call_count, int(may_create))
                for url in ('/api/backup/delete', '/api/backup/rename'):
                    result = client.post(url, json={}, headers=self.headers(client))
                    self.assertEqual(result.status_code != 403, may_create)
                result = client.get('/api/backup/download/Missing/missing.zip')
                self.assertEqual(result.status_code, 404 if may_create else 403)
                result = client.post('/api/backup/restore', json={}, headers=self.headers(client))
                self.assertEqual(result.status_code, 400 if may_restore else 403)
                if allowed:
                    markup = page.get_data(as_text=True)
                    self.assertEqual('onclick="createManualBackup()"' in markup, may_create)
                    self.assertEqual('>Restore</button>' in markup, may_restore)
                    self.assertEqual('>Download ZIP</button>' in markup, may_create)
                    self.assertEqual('>Delete</button>' in markup, may_create)
                    self.assertEqual("{ id: 'backup', text: 'Backup'" in markup, may_create)
                    # Execute the actual rendered table function, including its rename guard.
                    labels = ('function escapeBackupLabel(' + markup.split('function escapeBackupLabel(', 1)[1]
                              .split('function renderServerSection(', 1)[0])
                    script = markup.split('function renderBackupTable(', 1)[1].split('function showModal(', 1)[0]
                    check_js = ("const formatBytesMbGb=()=>'', formatTimestamp=()=>'';\n"
                                + labels + '\n' +
                                "function renderBackupTable(" + script +
                                "\nconst row=renderBackupTable('Test',[{filename:'one.zip'}],true);\n"
                                "if(row.includes('>Rename</button>') !== " + json.dumps(may_create) +
                                ") throw Error('Rename permission mismatch');")
                    checked = subprocess.run(['node'], input=check_js, capture_output=True, text=True, encoding='utf-8')
                    self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_backup_rename_preserves_archive_and_selected_server_group(self):
        creator = self.client_for(self.create('rename-creator', ['backups.create']))
        selected = self.data / 'backups' / 'Rename_Selected'
        other = self.data / 'backups' / 'Rename_Other'
        selected.mkdir(parents=True, exist_ok=True)
        other.mkdir(parents=True, exist_ok=True)
        original = self.zip_bytes({'ServerEngineInfo': json.dumps(self.config), 'world/data.dat': 'saved world'})
        (selected / 'manual.zip').write_bytes(original)
        (other / 'manual.zip').write_bytes(b'another server backup')
        for old_name, requested_name, saved_name in (
                ('manual.zip', 'Before upgrade', 'Before upgrade.zip'),
                ('Before upgrade.zip', 'Verified restore point.zip', 'Verified restore point.zip'),
                ('Verified restore point.zip', "Owner's & friends backup", "Owner's & friends backup.zip")):
            with self.subTest(requested_name=requested_name):
                response = creator.post('/api/backup/rename',
                    data={'server_name': selected.name, 'old_name': old_name, 'new_name': requested_name},
                    headers=self.headers(creator))
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual(response.json['status'], 'success')
                self.assertFalse((selected / old_name).exists())
                self.assertEqual((selected / saved_name).read_bytes(), original)
                self.assertEqual((other / 'manual.zip').read_bytes(), b'another server backup')
                listing = creator.get('/api/backups/list').json['backups'][selected.name]
                self.assertEqual([item['filename'] for item in listing], [saved_name])

    def test_backup_rename_collision_preserves_both_archives(self):
        directory = self.data / 'backups' / 'Rename_Collision'
        directory.mkdir(parents=True, exist_ok=True)
        source, existing = directory / 'source.zip', directory / 'existing.zip'
        source.write_bytes(b'source archive')
        existing.write_bytes(b'existing archive')
        response = self.client.post('/api/backup/rename',
            data={'server_name': directory.name, 'old_name': source.name, 'new_name': 'existing'},
            headers=self.headers())
        self.assertEqual(response.json['status'], 'error')
        self.assertIn('already exists', response.json['message'])
        self.assertEqual(source.read_bytes(), b'source archive')
        self.assertEqual(existing.read_bytes(), b'existing archive')

    def test_backup_rename_rejects_automatic_archives(self):
        directory = self.data / 'backups' / 'Rename_Automatic'
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / 'Server_20261118120000_auto.zip'
        source.write_bytes(b'automatic archive')
        response = self.client.post('/api/backup/rename',
            data={'server_name': directory.name, 'old_name': source.name, 'new_name': 'renamed.zip'},
            headers=self.headers())
        self.assertEqual(response.json['status'], 'error')
        self.assertIn('automatic', response.json['message'].lower())
        self.assertEqual(source.read_bytes(), b'automatic archive')
        self.assertFalse((directory / 'renamed.zip').exists())

    def test_backup_rename_requires_manage_permission_and_csrf(self):
        directory = self.data / 'backups' / 'Rename_Permissions'
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / 'source.zip'
        source.write_bytes(b'protected archive')
        payload = {'server_name': directory.name, 'old_name': source.name, 'new_name': 'renamed.zip'}
        for name, permissions in (('rename-viewer', []), ('rename-restorer', ['backups.restore'])):
            with self.subTest(name=name):
                client = self.client_for(self.create(name, permissions))
                response = client.post('/api/backup/rename', data=payload, headers=self.headers(client))
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/api/backup/rename', data=payload).status_code, 403)
        self.assertEqual(source.read_bytes(), b'protected archive')
        self.assertFalse((directory / 'renamed.zip').exists())

    def test_backup_rename_blocks_traversal_in_every_path_field(self):
        directory = self.data / 'backups' / 'Rename_Paths'
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / 'source.zip'
        source.write_bytes(b'protected archive')
        baseline = {'server_name': directory.name, 'old_name': source.name, 'new_name': 'renamed.zip'}
        invalid = (
            ('server_name', '..'), ('server_name', '../Rename_Paths'),
            ('server_name', '..\\Rename_Paths'), ('server_name', 'C:\\outside'),
            ('old_name', '../source.zip'), ('old_name', '..\\source.zip'), ('old_name', '/source.zip'),
            ('new_name', '../outside.zip'), ('new_name', '..\\outside.zip'),
            ('new_name', '/outside.zip'), ('new_name', 'C:\\outside.zip'),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                response = self.client.post('/api/backup/rename', data={**baseline, field: value}, headers=self.headers())
                self.assertIn(response.status_code, (200, 400))
                if response.status_code == 200:
                    self.assertEqual(response.json['status'], 'error')
                self.assertEqual(source.read_bytes(), b'protected archive')
                self.assertEqual([item.name for item in directory.iterdir()], [source.name])

    def test_backup_rename_missing_fields_or_archive_returns_actionable_error(self):
        directory = self.data / 'backups' / 'Rename_Missing'
        directory.mkdir(parents=True, exist_ok=True)
        payload = {'server_name': directory.name, 'old_name': 'missing.zip', 'new_name': 'renamed.zip'}
        for missing in payload:
            with self.subTest(missing=missing):
                response = self.client.post('/api/backup/rename',
                    data={key: value for key, value in payload.items() if key != missing}, headers=self.headers())
                self.assertEqual(response.json['status'], 'error')
                self.assertTrue(response.json['message'])
        response = self.client.post('/api/backup/rename', data=payload, headers=self.headers())
        self.assertEqual(response.json['status'], 'error')
        self.assertIn('not found', response.json['message'].lower())
        self.assertEqual(list(directory.iterdir()), [])

    def test_background_backup_creation_and_status_require_create_permission(self):
        for name, permissions in (('creator', ['backups.create']), ('restorer', ['backups.restore']),
                                  ('reader', [])):
            client = self.client_for(self.create(name, permissions))
            allowed = name == 'creator'
            job = dict(job_id='a' * 32, status='running', message='Preparing...')
            with patch.object(self.module.server_backups, 'start', return_value=job) as start, \
                 patch.object(self.module.server_backups, 'status', return_value=job) as status:
                result = client.post('/api/backup/create', json={'background': True}, headers=self.headers(client))
                self.assertEqual(result.status_code, 202 if allowed else 403)
                self.assertEqual(start.call_count, int(allowed))
                result = client.get('/api/backup/status/' + 'a' * 32)
                self.assertEqual(result.status_code, 200 if allowed else 403)
                self.assertEqual(status.call_count, int(allowed))
                self.assertEqual(client.post('/api/backup/create', json={'background': True}).status_code, 403)
        self.assertEqual(self.app.test_client().get('/api/backup/status/' + 'a' * 32).status_code, 401)
        self.assertEqual(self.client.get('/api/backup/status/unknown').status_code, 404)
        self.assertEqual(self.client.post('/api/backup/create', json=['invalid'], headers=self.headers()).status_code, 400)

    def test_background_backup_endpoint_creates_verified_zip_with_old_file_date(self):
        marker = self.root / 'old.dat'
        marker.write_bytes(b'old imported file')
        os.utime(marker, (0, 0))
        finished = self.module.threading.Event()
        update = self.module.server_backups._update
        def record(job_id, **values):
            update(job_id, **values)
            if values.get('status') in ('success', 'error'):
                finished.set()
        with patch.object(self.module.server_backups, '_update', side_effect=record):
            result = self.client.post('/api/backup/create', json={'background': True}, headers=self.headers())
            self.assertEqual(result.status_code, 202)
            self.assertTrue(finished.wait(60), 'Backup worker did not complete')
        response = self.client.get('/api/backup/status/' + result.json['job_id'])
        self.assertEqual(response.json['status'], 'success', response.json)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertTrue(response.json['server_key'].startswith('Test_Server'))
        archive_path = self.data / 'backups' / response.json['server_key'] / response.json['file']
        with self.exported_backup(archive_path) as archive:
            self.assertEqual(archive.read('old.dat'), b'old imported file')
            self.assertEqual(archive.getinfo('old.dat').date_time[0], 1980)
            self.assertIsNone(archive.testzip())
        self.assertFalse((self.root / 'panel_config.json').exists())
        # Job status must not leak into a reset/replaced server's account scope.
        with patch.object(self.store, 'scope', return_value='new-scope'):
            self.assertEqual(self.module.server_backups.status(result.json['job_id'], self.store.scope()), None)

    def test_current_backup_key_matches_creation_listing_and_download(self):
        self.config['server_name'] = 'JRock Bottom'
        self.module.save_config(self.config)
        marker = self.root / 'grouping-check.txt'
        marker.write_bytes(b'isolated backup grouping test')
        result = self.client.post('/api/backup/create', headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['server_key'], 'JRock_Bottom')
        archive = self.data / 'backups' / result.json['server_key'] / result.json['file']
        original = archive.read_bytes()
        listing = self.client.get('/api/backups/list').json['backups']
        self.assertIn(result.json['file'], [f['filename'] for f in listing['JRock_Bottom']])
        markup = self.client.get('/backups').get_data(as_text=True)
        self.assertIn('const currentServerName = "JRock Bottom";', markup)
        self.assertIn('const currentServerKey = "JRock_Bottom";', markup)
        response = self.client.get('/api/backup/download/' + result.json['server_key'] + '/' + result.json['file'])
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn('.zip', response.headers['Content-Disposition'])
            with zipfile.ZipFile(io.BytesIO(response.data)) as exported:
                self.assertEqual(exported.read('grouping-check.txt'), marker.read_bytes())
        finally:
            response.close()
        self.assertEqual(archive.read_bytes(), original)

    def test_backup_errors_return_json_and_log_the_actual_failure(self):
        import errno
        with patch.object(self.module, 'create_backup', side_effect=OSError(errno.ENOSPC, 'disk full')), \
             self.assertLogs(self.app.logger, level='ERROR'):
            response = self.client.post('/api/backup/create', headers=self.headers())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json['status'], 'error')
        self.assertIn('free space', response.json['message'])

    def test_active_backups_block_restore_import_purge_and_duplicate_creation(self):
        marker = self.root / 'keep.txt'
        marker.write_text('must remain')
        scope = self.store.scope()
        with self.module.server_backups.exclusive():
            for path in ('/api/backup/restore', '/api/import', '/api/purge', '/api/backup/create'):
                response = self.client.post(path, json={'background': True}, headers=self.headers())
                self.assertEqual(response.status_code, 409, (path, response.json))
                self.assertIn('already running', response.json['message'])
            self.assertEqual(marker.read_text(), 'must remain')
            self.assertEqual(self.store.scope(), scope)
        self.assertEqual(self.client.post('/api/backup/create', headers=self.headers()).status_code, 200)

    def test_backup_scheduler_only_runs_after_setup_and_marks_success(self):
        from server_backups import BackupBusy
        class EndLoop(Exception):
            pass
        self.config.update(auto_backup_enabled=True, auto_backup_freq='hourly', last_auto_backup=0)
        self.module.save_config(self.config)
        for error in (BackupBusy('busy'), OSError('storage failed')):
            with patch.object(self.module, 'create_backup', side_effect=error), \
                 patch.object(self.app.logger, 'exception'), \
                 patch('time.sleep', side_effect=EndLoop):
                with self.assertRaises(EndLoop):
                    self.module.backup_scheduler()
            self.assertEqual(self.module.load_config()['last_auto_backup'], 0)
        with patch.object(self.module, 'create_backup', return_value='backup.zip') as create, \
             patch('time.sleep', side_effect=EndLoop):
            with self.assertRaises(EndLoop):
                self.module.backup_scheduler()
            create.assert_called_once_with('Test Server', is_manual=False)
        self.assertGreater(self.module.load_config()['last_auto_backup'], 0)
        self.config.update(is_setup=False, last_auto_backup=0)
        self.module.save_config(self.config)
        with patch.object(self.module, 'create_backup') as create, patch('time.sleep', side_effect=EndLoop):
            with self.assertRaises(EndLoop):
                self.module.backup_scheduler()
            create.assert_not_called()

    def test_backup_shutdown_setting_is_opt_in_persisted_and_permission_protected(self):
        self.assertFalse(self.module.load_config()['backup_shutdown'])
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('id="backupShutdown"', page.split('id="settings-backups"')[1].split('</section>')[0])
        result = self.client.post('/settings', data={'backup_settings_present': 'true', 'backup_shutdown': 'true'},
                                  headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.module.load_config()['backup_shutdown'])
        self.client.post('/settings', data={'accent': 'blue'}, headers=self.headers())
        self.assertTrue(self.module.load_config()['backup_shutdown'], 'Partial/legacy forms must preserve mode')
        creator = self.client_for(self.create('backupcreator', ['backups.create']))
        denied = creator.post('/settings', data={'backup_settings_present': 'true'}, headers=self.headers(creator))
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(self.module.load_config()['backup_shutdown'])
        self.client.post('/settings', data={'backup_settings_present': 'true'}, headers=self.headers())
        self.assertFalse(self.module.load_config()['backup_shutdown'])

    def test_backup_exclusions_settings_and_restore(self):
        field = 'backup_exclusions'
        form = {field: 'bluemap/web/maps/\r\nlogs\\latest.log\r\nbluemap/web/maps'}
        creator = self.client_for(self.create('zip-creator', ['backups.create']))
        self.assertEqual(creator.post('/settings', data=form, headers=self.headers(creator)).status_code, 403)
        self.assertEqual(self.client.post('/settings', data=form).status_code, 403)
        self.assertEqual(self.module.load_config()[field], [])
        self.assertEqual(self.client.post('/settings', data=form, headers=self.headers()).status_code, 200)
        expected = ['bluemap/web/maps', 'logs/latest.log']
        self.client.post('/settings', data={'backup_settings_present': 'true'}, headers=self.headers())
        for invalid in ('/', '.', '../world', 'logs/../world', '/data/server', 'C:\\world', '*.dat',
                        'ServerEngineInfo', 'panel_config.json', 'player_history.json'):
            with self.subTest(path=invalid):
                self.assertEqual(self.client.post('/settings', data={field: invalid}, headers=self.headers()).status_code, 400)
                self.assertEqual(self.module.load_config()[field], expected)
        maps = self.root / 'bluemap/web/maps'
        maps.mkdir(parents=True)
        (maps / 'tile').write_bytes(b'tile')
        world = self.root / 'world'
        world.mkdir()
        (world / 'level.dat').write_bytes(b'world')
        archive = Path(self.module.create_backup('Test Server', is_manual=True))
        result = self.client.post('/api/backup/restore', json={'server_name': archive.parent.name,
                                  'filename': archive.name}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.data)
        self.assertFalse(maps.exists())
        self.assertEqual((world / 'level.dat').read_bytes(), b'world')
        self.assertEqual(self.module.load_config()[field], expected)
        self.client.post('/settings', data={field: ''}, headers=self.headers())
        self.assertEqual(self.module.load_config()[field], [])

    def test_server_replacements_clear_previous_backup_exclusions(self):
        archive = self.zip_bytes()
        other = self.data / 'backups' / 'Other_Realm'
        other.mkdir(parents=True, exist_ok=True)
        (other / 'source.zip').write_bytes(archive)
        for route, params in [('/api/import', {'data': {'import_zip': (io.BytesIO(archive), 'server.zip')}}),
                              ('/api/backup/restore', {'json': {'server_name': other.name, 'filename': 'source.zip'}}),
                              ('/api/purge', {})]:
            with self.subTest(route=route):
                config = self.module.load_config()
                config['backup_exclusions'] = ['bluemap/web/maps']
                self.module.save_config(config)
                response = self.client.post(route, headers=self.headers(), **params)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(self.module.load_config()['backup_exclusions'], [])

    def test_automation_settings_are_opt_in_explicit_and_permission_protected(self):
        self.assertFalse(self.module.DEFAULT_CONFIG['auto_start'])
        self.assertFalse(self.module.DEFAULT_CONFIG['auto_restart'])
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('name="automation_settings_present"', page)
        self.assertIn('name="auto_start"', page)
        self.assertIn('name="auto_restart"', page)
        form = {'automation_settings_present': 'true', 'auto_start': 'true', 'auto_restart': 'true'}
        viewer = self.client_for(self.create('automation-viewer', ['server.control']))
        self.assertEqual(viewer.post('/settings', data=form, headers=self.headers(viewer)).status_code, 403)
        self.assertEqual(self.client.post('/settings', data=form).status_code, 403)
        self.assertFalse(self.module.load_config()['auto_start'])
        self.assertFalse(self.module.load_config()['auto_restart'])
        with patch.object(self.module, 'start_minecraft_server') as start:
            result = self.client.post('/settings', data=form, headers=self.headers())
            self.assertEqual(result.status_code, 200)
            self.assertTrue(self.module.load_config()['auto_start'])
            self.assertTrue(self.module.load_config()['auto_restart'])
            # Legacy and partial forms cannot silently clear either option.
            result = self.client.post('/settings', data={'accent': 'blue'}, headers=self.headers())
            self.assertEqual(result.status_code, 200)
            self.assertTrue(self.module.load_config()['auto_start'])
            self.assertTrue(self.module.load_config()['auto_restart'])
            for enabled in ('auto_start', 'auto_restart', None):
                with self.subTest(enabled=enabled):
                    form = {'automation_settings_present': 'true'}
                    if enabled:
                        form[enabled] = 'true'
                    result = self.client.post('/settings', data=form, headers=self.headers())
                    self.assertEqual(result.status_code, 200)
                    saved = self.module.load_config()
                    self.assertEqual(saved['auto_start'], enabled == 'auto_start')
                    self.assertEqual(saved['auto_restart'], enabled == 'auto_restart')
            start.assert_not_called()

    def test_automation_manual_stop_cancels_even_when_already_offline(self):
        for path, expected_status in (('/stop', 302), ('/api/server/stop', 200)):
            with self.subTest(path=path), patch.object(self.module.server_automation, 'stop') as cancel:
                response = self.client.post(path, headers=self.headers())
                self.assertEqual(response.status_code, expected_status)
                cancel.assert_called_once_with()

    def test_automation_manual_stop_cancels_before_shutdown_commands(self):
        process = Mock(pid=43210)
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        self.config.update(server_pid=process.pid, is_running=True)
        self.module.save_config(self.config)
        events = []
        process.stdin.write.side_effect = lambda command: events.append(command)
        with patch.object(self.module.server_automation, 'stop', side_effect=lambda: events.append('cancel')), \
             patch.object(self.module.time, 'sleep'):
            response = self.client.post('/api/server/stop', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, ['cancel', 'save-all\n', 'stop\n'])
        self.assertEqual(self.module.load_config()['server_pid'], process.pid,
                         'A requested stop is not evidence that the process has exited')

    def test_automation_start_refuses_previous_live_log_reader(self):
        reader = Mock()
        reader.is_alive.return_value = True
        self.module.MINECRAFT_LOG_THREAD = reader
        with patch.object(self.module.subprocess, 'Popen') as launch, \
             patch.object(self.module.server_automation, 'track') as track:
            self.assertFalse(self.module.start_minecraft_server())
            launch.assert_not_called()
            track.assert_not_called()

    def test_automation_import_and_restore_cancel_policy_and_reset_options(self):
        self.config.update(auto_start=True, auto_restart=True)
        self.module.save_config(self.config)
        archive = self.zip_bytes()
        with patch.object(self.module.server_automation, 'stop') as cancel:
            result = self.client.post('/api/import',
                data={'import_zip': (io.BytesIO(archive), 'automation-server.zip')}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        cancel.assert_called_once_with()
        self.assertFalse(self.module.load_config()['auto_start'])
        self.assertFalse(self.module.load_config()['auto_restart'])
        backup_dir = self.data / 'backups' / 'Automation_Reset'
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / 'enabled.zip').write_bytes(archive)
        saved = self.module.load_config()
        saved.update(auto_start=True, auto_restart=True)
        self.module.save_config(saved)
        with patch.object(self.module.server_automation, 'stop') as cancel:
            result = self.client.post('/api/backup/restore',
                json={'server_name': 'Automation_Reset', 'filename': 'enabled.zip'}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        cancel.assert_called_once_with()
        self.assertFalse(self.module.load_config()['auto_start'])
        self.assertFalse(self.module.load_config()['auto_restart'])

    def test_automation_purge_cancels_policy_and_resets_options(self):
        self.config.update(auto_start=True, auto_restart=True)
        self.module.save_config(self.config)
        with patch.object(self.module.server_automation, 'stop') as cancel:
            result = self.client.post('/api/purge', headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        cancel.assert_called_once_with()
        self.assertFalse(self.module.load_config()['auto_start'])
        self.assertFalse(self.module.load_config()['auto_restart'])
        self.assertFalse(self.module.load_config()['is_setup'])

    def test_automation_invalid_import_does_not_cancel_policy_or_clear_options(self):
        self.config.update(auto_start=True, auto_restart=True)
        self.module.save_config(self.config)
        with patch.object(self.module.server_automation, 'stop') as cancel:
            response = self.client.post('/api/import',
                data={'import_zip': (io.BytesIO(b'not a zip'), 'broken.zip')}, headers=self.headers())
        self.assertEqual(response.status_code, 400)
        cancel.assert_not_called()
        self.assertTrue(self.module.load_config()['auto_start'])
        self.assertTrue(self.module.load_config()['auto_restart'])

    def test_automation_pending_restart_is_cancelled_by_stop_or_settings(self):
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        for action in ('/stop', '/api/server/stop', 'settings', 'http-console', 'socket-console'):
            with self.subTest(action=action):
                self.config.update(auto_restart=True, auto_start=False)
                self.module.save_config(self.config)
                now = [0]
                start = Mock(return_value=True)
                crashed = Mock()
                crashed.poll.return_value = 1
                controller = self.module.ServerAutomation(self.module.load_config, start, lambda: None,
                    lambda: None, self.module.server_backups.exclusive, Mock(), clock=lambda: now[0])
                controller.track(crashed)
                self.assertFalse(controller.tick())
                start.assert_not_called()
                with patch.object(self.module, 'server_automation', controller):
                    if action == 'settings':
                        result = self.client.post('/settings', data={'automation_settings_present': 'true'},
                                                  headers=self.headers())
                        self.assertEqual(result.status_code, 200)
                        # Re-enabling between monitor ticks must not resurrect the cancelled crash.
                        result = self.client.post('/settings',
                            data={'automation_settings_present': 'true', 'auto_restart': 'true'}, headers=self.headers())
                        self.assertEqual(result.status_code, 200)
                    elif action == 'http-console':
                        result = self.client.post('/api/command', json={'command': 'stop'}, headers=self.headers())
                        self.assertEqual(result.json['status'], 'error')
                    elif action == 'socket-console':
                        result = socket_client.emit('send_command', {'command': '/minecraft:stop'}, callback=True)
                        self.assertIn('error', result)
                    else:
                        self.assertIn(self.client.post(action, headers=self.headers()).status_code, (200, 302))
                    now[0] = 20
                    self.assertFalse(controller.tick())
                start.assert_not_called()

    def test_automation_start_tracks_process_before_reader_and_waits_after_stdout_eof(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                self.module.MINECRAFT_PROCESS = None
                self.module.MINECRAFT_LOG_THREAD = None
                self.module.save_config(self.config)
                process = Mock(pid=43210)
                process.poll.return_value = None
                process.stdout = io.StringIO('')
                events, captured = [], {}
                reader = Mock()
                reader.is_alive.return_value = False
                reader.start.side_effect = lambda: events.append('reader-start')
                def make_reader(*, target, args, daemon):
                    self.assertTrue(daemon)
                    captured.update(target=target, args=args)
                    return reader
                def wait_for_exit():
                    self.assertIs(self.module.MINECRAFT_PROCESS, process)
                    self.assertEqual(self.module.load_config()['server_pid'], process.pid)
                    self.assertTrue(self.module.load_config()['is_running'])
                    events.append('wait')
                    process.poll.return_value = 0
                    return 0
                process.wait.side_effect = wait_for_exit
                try:
                    with patch.object(self.module, 'build_server_command', return_value=['java', '-jar', 'server.jar']), \
                         patch.object(self.module.subprocess, 'Popen', return_value=process), \
                         patch.object(self.module.time, 'sleep'), \
                         patch.object(self.module.threading, 'Thread', side_effect=make_reader), \
                         patch.object(self.module.server_automation, 'track',
                                      side_effect=lambda *args, **kwargs: events.append('track')) as track:
                        self.assertTrue(self.module.start_minecraft_server(automatic=automatic))
                    track.assert_called_once_with(process, automatic=automatic)
                    self.assertEqual(events, ['track', 'reader-start'])
                    # Execute the captured reader synchronously; no Java process or background thread is used.
                    captured['target'](*captured['args'])
                    self.assertEqual(events, ['track', 'reader-start', 'wait'])
                    process.wait.assert_called_once_with()
                    self.assertIsNone(self.module.MINECRAFT_PROCESS)
                    self.assertFalse(self.module.load_config()['is_running'])
                    self.assertIsNone(self.module.load_config()['server_pid'])
                finally:
                    if captured:
                        captured['args'][1].close()
                    process.stdout.close()

    def test_automation_import_refuses_shutdown_timeout_or_active_reader_without_wiping(self):
        marker = self.root / 'keep-during-shutdown.txt'
        marker.write_text('unchanged')
        scope = self.store.scope()
        for failure in ('process-timeout', 'reader-active', 'detached-timeout'):
            with self.subTest(failure=failure):
                process = Mock(pid=43210)
                process.poll.return_value = None if failure == 'process-timeout' else 0
                if failure == 'process-timeout':
                    process.wait.side_effect = subprocess.TimeoutExpired('minecraft', 30)
                reader = Mock()
                reader.is_alive.return_value = failure == 'reader-active'
                detached = Mock()
                detached.wait.side_effect = self.module.psutil.TimeoutExpired(30)
                self.module.MINECRAFT_PROCESS = None if failure == 'detached-timeout' else process
                self.module.MINECRAFT_LOG_THREAD = reader
                self.config.update(server_pid=43210 if failure == 'detached-timeout' else None)
                self.module.save_config(self.config)
                with patch.object(self.module, 'stop_minecraft_server', return_value=True), \
                     patch.object(self.module.psutil, 'pid_exists', return_value=failure == 'detached-timeout'), \
                     patch.object(self.module.psutil, 'Process', return_value=detached):
                    result = self.client.post('/api/import',
                        data={'import_zip': (io.BytesIO(self.zip_bytes()), 'server.zip')}, headers=self.headers())
                self.assertEqual(result.status_code, 409, result.json)
                self.assertEqual(marker.read_text(), 'unchanged')
                self.assertFalse((self.root / 'server.properties').exists())
                self.assertEqual(self.store.scope(), scope)
                process.kill.assert_not_called()
                detached.kill.assert_not_called()
                if failure == 'reader-active':
                    reader.join.assert_called_once_with(timeout=10)

    def test_automation_manual_restart_waits_for_exit_then_reader_before_starting(self):
        events = []
        process = Mock(pid=43210)
        process.poll.return_value = None
        def exited(timeout):
            self.assertEqual(timeout, 30)
            events.append('process-exit')
            process.poll.return_value = 0
            return 0
        process.wait.side_effect = exited
        reader = Mock()
        reader.join.side_effect = lambda timeout: events.append('reader-join')
        reader.is_alive.return_value = False
        self.module.MINECRAFT_PROCESS = process
        self.module.MINECRAFT_LOG_THREAD = reader
        def start_after_shutdown():
            self.assertIsNone(self.module.MINECRAFT_PROCESS)
            self.assertIsNone(self.module.MINECRAFT_LOG_THREAD)
            self.assertIsNone(self.module.load_config()['server_pid'])
            events.append('start')
            return True
        with patch.object(self.module, 'stop_minecraft_server', side_effect=lambda: events.append('stop')), \
             patch.object(self.module, 'start_minecraft_server', side_effect=start_after_shutdown) as start:
            result = self.client.post('/api/server/restart', headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(events, ['stop', 'process-exit', 'reader-join', 'start'])
        start.assert_called_once_with()
        reader.join.assert_called_once_with(timeout=10)
        process.kill.assert_not_called()

    def test_automation_manual_restart_failure_never_kills_or_starts_second_process(self):
        for failure in ('process-timeout', 'reader-active'):
            with self.subTest(failure=failure):
                process = Mock(pid=43210)
                process.poll.return_value = None if failure == 'process-timeout' else 0
                if failure == 'process-timeout':
                    process.wait.side_effect = subprocess.TimeoutExpired('minecraft', 30)
                reader = Mock()
                reader.is_alive.return_value = True
                self.module.MINECRAFT_PROCESS = process
                self.module.MINECRAFT_LOG_THREAD = reader
                with patch.object(self.module, 'stop_minecraft_server', return_value=True), \
                     patch.object(self.module, 'start_minecraft_server') as start:
                    result = self.client.post('/api/server/restart', headers=self.headers())
                self.assertEqual(result.status_code, 409, result.json)
                start.assert_not_called()
                process.kill.assert_not_called()
                self.assertIs(self.module.MINECRAFT_PROCESS, process)
                self.assertIs(self.module.MINECRAFT_LOG_THREAD, reader)

    def test_backup_endpoint_uses_saved_shutdown_mode_and_recovers_after_archive(self):
        process = Mock(pid=43210)
        process.poll.return_value = None
        def stopped(timeout):
            self.assertEqual(timeout, 180)
            process.poll.return_value = 0
            return 0
        process.wait.side_effect = stopped
        self.module.MINECRAFT_PROCESS = process
        self.config.update(backup_shutdown=True, server_pid=process.pid, is_running=True, target_state='Online')
        self.module.save_config(self.config)
        original = self.module.server_backups._archive
        def archive(*args, **kwargs):
            self.assertEqual(process.poll(), 0)
            self.assertTrue(self.module.server_backups.lock.locked())
            return original(*args, **kwargs)
        with patch.object(self.module, 'start_minecraft_server', return_value=True) as start, \
             patch.object(self.module.server_backups, '_archive', side_effect=archive):
            result = self.client.post('/api/backup/create', json={'backup_shutdown': False}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        process.stdin.write.assert_called_once_with('stop\n')
        process.kill.assert_not_called()
        start.assert_called_once_with()
        self.assertTrue((self.data / 'backups' / result.json['server_key'] / result.json['file']).is_file())
        self.assertFalse(self.module.server_backups.lock.locked())

    def test_backup_restart_error_reports_completed_archive(self):
        from server_backups import BackupCompletedError
        failure = BackupCompletedError(str(self.data / 'backups' / 'Test_Server' / 'kept.zip'),
                                       RuntimeError('Restart failed. Start Minecraft manually.'))
        with patch.object(self.module, 'create_backup', side_effect=failure), patch.object(self.app.logger, 'exception'):
            result = self.client.post('/api/backup/create', headers=self.headers())
        self.assertEqual(result.status_code, 500)
        self.assertTrue(result.json['backup_created'])
        self.assertEqual(result.json['file'], 'kept.zip')
        self.assertEqual(result.json['server_key'], 'Test_Server')
        self.assertIn('Backup created', result.json['message'])

    def test_shutdown_backup_waits_for_real_child_final_write_and_log_drain(self):
        import threading
        (self.root / 'world-final.dat').write_bytes(b'before shutdown')
        child_code = (
            "import sys\nfrom pathlib import Path\n"
            "for line in sys.stdin:\n"
            " if line.strip() == 'stop':\n"
            "  Path('world-final.dat').write_bytes(b'final saved chunks')\n"
            "  print('Saved final chunks', flush=True)\n"
            "  break\n"
        )
        process = subprocess.Popen([sys.executable, '-u', '-c', child_code], cwd=self.root,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        def drain():
            try:
                output = process.stdout.read()
                (self.root / 'final-console.log').write_text(output, encoding='utf-8')
            finally:
                process.stdout.close()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        self.module.MINECRAFT_PROCESS = process
        self.module.MINECRAFT_LOG_THREAD = reader
        self.config.update(backup_shutdown=True, server_pid=process.pid, is_running=True, target_state='Online')
        self.module.save_config(self.config)
        def recovered():
            self.assertEqual(process.poll(), 0)
            self.assertFalse(reader.is_alive())
            self.assertTrue(self.module.server_backups.lock.locked())
            return True
        try:
            with patch.object(self.module, 'start_minecraft_server', side_effect=recovered) as start:
                result = self.client.post('/api/backup/create', headers=self.headers())
            self.assertEqual(result.status_code, 200, result.json)
            with self.exported_backup(self.data / 'backups' / result.json['server_key'] / result.json['file']) as archive:
                self.assertEqual(archive.read('world-final.dat'), b'final saved chunks')
                self.assertIn(b'Saved final chunks', archive.read('final-console.log'))
            start.assert_called_once_with()
        finally:
            # Only our temporary Python test child, never Minecraft or a saved PID.
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            reader.join(timeout=5)

    def test_backup_lock_blocks_lifecycle_console_and_settings_writes(self):
        with patch.object(self.module, 'start_minecraft_server') as start, \
             patch.object(self.module, 'stop_minecraft_server') as stop, self.module.server_backups.exclusive():
            for path in ('/start', '/stop', '/api/server/start', '/api/server/stop', '/api/server/restart',
                         '/api/command', '/settings'):
                response = self.client.post(path, json={'command': 'restart'}, headers=self.headers())
                self.assertEqual(response.status_code, 409, (path, response.json))
            self.assertEqual(self.client.get('/settings').status_code, 200)
            start.assert_not_called()
            stop.assert_not_called()
            with self.app.test_request_context('/socket.io'):
                with patch.object(self.security, 'current_user', return_value=self.owner):
                    self.assertIn('already running', self.module.handle_command({'command': 'restart'})['error'])

    def test_restore_requires_confirmed_offline_process_and_log_reader(self):
        created = self.client.post('/api/backup/create', headers=self.headers()).json
        marker = self.root / 'keep-until-offline.txt'
        marker.write_text('Do not delete while Minecraft is running')
        process = Mock(pid=43210)
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        payload = {'server_name': created['server_key'], 'filename': created['file']}
        result = self.client.post('/api/backup/restore', json=payload, headers=self.headers())
        self.assertEqual(result.status_code, 409)
        process.stdin.write.assert_not_called()
        process.kill.assert_not_called()
        self.assertTrue(marker.exists())
        self.module.MINECRAFT_PROCESS = None
        reader = self.module.MINECRAFT_LOG_THREAD = Mock()
        reader.is_alive.return_value = True
        result = self.client.post('/api/backup/restore', json=payload, headers=self.headers())
        self.assertEqual(result.status_code, 409)
        self.assertTrue(marker.exists())

    def test_shutdown_backup_scheduler_failure_cooldown_and_completed_archive(self):
        from server_backups import BackupBusy, BackupCompletedError
        class EndLoop(Exception):
            pass
        def tick(error=None):
            with patch.object(self.module, 'create_backup', side_effect=error, return_value='backup.zip') as create, \
                 patch.object(self.app.logger, 'exception'), patch('time.time', return_value=10000), \
                 patch('time.sleep', side_effect=EndLoop):
                with self.assertRaises(EndLoop):
                    self.module.backup_scheduler()
            return create
        self.config.update(auto_backup_enabled=True, auto_backup_freq='hourly', backup_shutdown=True,
                           last_auto_backup=0, last_auto_backup_attempt=0)
        self.module.save_config(self.config)
        tick(BackupBusy('busy')).assert_called_once()
        self.assertEqual(self.module.load_config()['last_auto_backup_attempt'], 0)
        tick(OSError('disk full')).assert_called_once()
        self.assertEqual(self.module.load_config()['last_auto_backup'], 0)
        self.assertEqual(self.module.load_config()['last_auto_backup_attempt'], 10000)
        tick().assert_not_called()
        self.module.save_config(self.config)
        tick(BackupCompletedError('backup.zip', RuntimeError('restart failed'))).assert_called_once()
        self.assertEqual(self.module.load_config()['last_auto_backup'], 10000)

    def test_status_poll_does_not_clear_backup_owned_or_detached_process_state(self):
        self.config.update(server_pid=43210, is_running=True, target_state='Shutting Down')
        self.module.save_config(self.config)
        with patch.object(self.module.psutil, 'pid_exists', return_value=False), self.module.server_backups.exclusive():
            self.client.get('/api/stats')
        self.assertEqual(self.module.load_config()['server_pid'], 43210)
        with patch.object(self.module.psutil, 'pid_exists', return_value=True):
            self.assertEqual(self.client.get('/api/stats').json['status'], 'Online')
        self.assertEqual(self.module.load_config()['server_pid'], 43210)

    def test_partial_archives_are_not_listed_or_downloadable(self):
        directory = self.data / 'backups' / 'Partial_Test'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'unfinished.zip.partial').write_bytes(b'not completed')
        self.assertNotIn('Partial_Test', self.client.get('/api/backups/list').json['backups'])
        self.assertEqual(self.client.get('/api/backup/download/Partial_Test/unfinished.zip.partial').status_code, 400)

    def test_restore_and_import_permissions_cannot_bypass_each_other(self):
        creator = self.client_for(self.create('creator', ['backups.create']))
        importer = self.client_for(self.create('importer', ['server.import']))
        restorer = self.client_for(self.create('restorer', ['backups.restore']))
        # Stored backup import is a restore even though it uses the import endpoint.
        for client in (creator, importer):
            result = client.post('/api/import', data={'import_backup_path': 'Missing/missing.zip'},
                                 headers=self.headers(client))
            self.assertEqual(result.status_code, 403)
        result = restorer.post('/api/import', data={'import_backup_path': 'Missing/missing.zip'},
                               headers=self.headers(restorer))
        self.assertEqual(result.status_code, 404)
        for client in (creator, restorer):
            result = client.post('/api/import', data={}, headers=self.headers(client))
            self.assertEqual(result.status_code, 403)
        self.assertEqual(importer.post('/api/import', data={}, headers=self.headers(importer)).status_code, 400)

    def test_replacements_preserve_original_owner_without_promoting_the_actor(self):
        for mode in ('restore', 'stored-import', 'upload-import', 'admin-restore'):
            with self.subTest(mode=mode):
                self.owner = self.store.authenticate('owner', 'Owner-password-123')
                self.client = self.client_for(self.owner)
                permission = 'server.import' if mode == 'upload-import' else 'backups.restore'
                actor = self.store.save_user(dict(username='actor-' + mode, password='User-password-123',
                    role='admin' if mode == 'admin-restore' else 'custom', permissions=[permission]), self.owner)
                client = self.client_for(actor)
                old_actor_session = self.client_for(actor)
                spectator = self.client_for(self.create('spectator-' + mode))
                backup_dir = self.data / 'backups' / 'Permission_Test'
                backup_dir.mkdir(parents=True, exist_ok=True)
                archive = backup_dir / 'restore.zip'
                archive.write_bytes(self.zip_bytes())
                scope = self.store.scope()
                self.assertEqual(client.post('/api/purge', headers=self.headers(client)).status_code, 403)
                if mode in ('restore', 'admin-restore'):
                    result = client.post('/api/backup/restore',
                        json=dict(server_name='Permission_Test', filename=archive.name), headers=self.headers(client))
                elif mode == 'stored-import':
                    result = client.post('/api/import', data={'import_backup_path': 'Permission_Test/restore.zip'},
                                         headers=self.headers(client))
                else:
                    result = client.post('/api/import', data={'import_zip': (io.BytesIO(self.zip_bytes()), 'server.zip')},
                                         headers=self.headers(client))
                self.assertEqual(result.status_code, 200, result.json)
                users = {user['id']: user for user in self.store.list_users()}
                self.assertEqual(set(users), {self.owner['id'], actor['id']})
                self.assertEqual(users[self.owner['id']]['role'], 'owner')
                self.assertEqual(users[actor['id']]['role'], actor['role'])
                self.assertEqual(users[actor['id']]['permissions'], actor['permissions'])
                self.assertNotIn('server.purge', grants(users[actor['id']]))
                self.assertEqual(self.module.load_config()['admin_user'], 'owner')
                self.assertIsNotNone(self.store.authenticate('owner', 'Owner-password-123'))
                self.assertIsNotNone(self.store.authenticate(actor['username'], 'User-password-123'))
                self.assertNotEqual(self.store.scope(), scope)
                for old_client in (self.client, old_actor_session, spectator):
                    self.assertEqual(old_client.get('/api/stats').status_code, 401)
                self.assertEqual(client.get('/api/stats').status_code, 200)
                self.assertEqual(client.post('/api/purge', headers=self.headers(client)).status_code, 403)

    def test_import_and_restore_create_fresh_account_scope(self):
        viewer = self.client_for(self.create())
        original_id = self.owner['id']
        result = self.client.post('/api/import', data={'import_zip': (io.BytesIO(self.zip_bytes()), 'server.zip')}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.json)
        users = self.store.list_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]['id'], original_id)
        self.assertEqual(users[0]['role'], 'owner')
        self.assertEqual(viewer.get('/api/stats').status_code, 401)
        archive_path = Path(self.module.create_backup('Test Server', True))
        scope = self.store.scope()
        result = self.client.post('/api/backup/restore', json={'server_name': archive_path.parent.name, 'filename': archive_path.name}, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertNotEqual(self.store.scope(), scope)

    def test_backups_exclude_credentials_and_path_traversal_is_blocked(self):
        self.smtp()
        archive_path = Path(self.module.create_backup('Safe Server', True))
        with self.exported_backup(archive_path) as archive:
            self.assertNotIn('panel_accounts.db', archive.namelist())
            config = json.loads(archive.read('panel_config.json'))
            self.assertNotIn('admin_pass', config)
            self.assertNotIn('admin_user', config)
            self.assertNotIn('smtp', config)
        result = self.client.post('/api/backup/rename', data={'server_name': '..', 'old_name': 'panel_accounts.db', 'new_name': 'stolen.zip'}, headers=self.headers())
        self.assertEqual(result.status_code, 400)
        result = self.client.post('/api/import', data={'import_backup_path': '../panel_accounts.db'}, headers=self.headers())
        self.assertEqual(result.status_code, 400)

    def test_invalid_import_never_wipes_server_or_accounts(self):
        marker = self.root / 'keep.txt'
        marker.write_text('keep')
        scope = self.store.scope()
        for contents in (b'not a zip', self.zip_bytes({'../escape.txt': 'bad'})):
            result = self.client.post('/api/import', data={'import_zip': (io.BytesIO(contents), 'bad.zip')}, headers=self.headers())
            self.assertEqual(result.status_code, 400)
            self.assertTrue(marker.exists())
            self.assertEqual(self.store.scope(), scope)

    def test_console_commands_add_optional_slash_once_on_http_and_socket(self):
        self.config.update(server_type='Purpur', mc_version='26.3')
        self.module.save_config(self.config)
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        commands = (
            ('help', '/help'), ('/help', '/help'),
            ('plugins', '/plugins'), ('/plugins', '/plugins'),
            ('  help  ', '/help'), ('  /plugins  ', '/plugins'),
            ('//plugin-command', '//plugin-command'), ('//', '//'), ('///', '///'),
            ('minecraft:help', '/minecraft:help'),
            ('say Visit https://example.com/a/b?x=1  now', '/say Visit https://example.com/a/b?x=1  now'),
            ('/say /help and //other', '/say /help and //other'),
        )
        for command, expected in commands:
            with self.subTest(command=command, transport='http'):
                process.stdin.reset_mock()
                result = self.client.post('/api/command', json={'command': command}, headers=self.headers())
                self.assertEqual(result.status_code, 200, result.json)
                self.assertEqual(result.json['status'], 'success')
                process.stdin.write.assert_called_once_with(expected + '\n')
                process.stdin.flush.assert_called_once_with()
            with self.subTest(command=command, transport='socket'):
                process.stdin.reset_mock()
                result = socket_client.emit('send_command', {'command': command}, callback=True)
                self.assertEqual(result['status'], 'success')
                process.stdin.write.assert_called_once_with(expected + '\n')
                process.stdin.flush.assert_called_once_with()

    def test_console_stop_prevents_backup_from_restarting_intentional_shutdown(self):
        process = Mock(pid=43210)
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        self.config.update(server_pid=process.pid, is_running=True, target_state='Online')
        self.module.save_config(self.config)
        result = self.client.post('/api/command', json={'command': '/stop'}, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.module.load_config()['target_state'], 'Shutting Down')
        with patch.object(self.module, 'start_minecraft_server') as start:
            with self.assertRaisesRegex(RuntimeError, 'already shutting down'):
                with self.module.backup_lifecycle.offline():
                    self.fail('Backup should not claim an intentional shutdown')
            start.assert_not_called()

    def test_console_commands_preserve_legacy_and_other_engine_stdin_syntax(self):
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        cases = [('Paper', '1.21.4', 'help'), ('Purpur', '1.21.5', 'help'),
                 ('Vanilla', '26.3', 'help'), ('Forge', '1.21.1', 'help'),
                 ('Fabric', '26.3', 'help'), ('Paper', '1.21.8', '/help'),
                 ('Paper', '26.3', '/help')]
        for engine, version, expected in cases:
            self.config.update(server_type=engine, mc_version=version)
            self.module.save_config(self.config)
            with self.subTest(engine=engine, version=version, transport='http'):
                process.stdin.reset_mock()
                response = self.client.post('/api/command', json={'command': 'help'}, headers=self.headers())
                self.assertEqual(response.status_code, 200, response.json)
                process.stdin.write.assert_called_once_with(expected + '\n')
            with self.subTest(engine=engine, version=version, transport='socket'):
                process.stdin.reset_mock()
                result = socket_client.emit('send_command', {'command': 'help'}, callback=True)
                self.assertEqual(result['status'], 'success')
                process.stdin.write.assert_called_once_with(expected + '\n')
            # User-supplied slash-sensitive plugin syntax is never rewritten.
            process.stdin.reset_mock()
            response = self.client.post('/api/command', json={'command': '//plugin-command'}, headers=self.headers())
            self.assertEqual(response.status_code, 200, response.json)
            process.stdin.write.assert_called_once_with('//plugin-command\n')

    def test_console_commands_reject_invalid_and_multiline_input_without_writing(self):
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        payloads = [None, [], ['help'], 'help', 42, True, {},
                    {'command': None}, {'command': []}, {'command': 42},
                    {'command': ''}, {'command': '   '}, {'command': '/'}, {'command': ' / '},
                    {'command': 'help\nstop'}, {'command': 'help\rstop'}, {'command': 'help\x00'},
                    {'command': '\nhelp'}, {'command': 'help\r'}]
        for payload in payloads:
            with self.subTest(payload=payload, transport='http'):
                response = self.client.post('/api/command', data=json.dumps(payload),
                    content_type='application/json', headers=self.headers())
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assertEqual(response.json['status'], 'error')
                self.assertTrue(response.json.get('message') or response.json.get('error'))
            with self.subTest(payload=payload, transport='socket'):
                result = socket_client.emit('send_command', payload, callback=True)
                self.assertIn('error', result)
        self.assertIn('error', socket_client.emit('send_command', callback=True))
        process.stdin.write.assert_not_called()
        process.stdin.flush.assert_not_called()

    def test_console_commands_offline_locked_and_unauthorized_never_write(self):
        process = Mock()
        process.poll.return_value = 0
        self.module.MINECRAFT_PROCESS = process
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        response = self.client.post('/api/command', json={'command': 'help'}, headers=self.headers())
        self.assertEqual(response.json['status'], 'error')
        self.assertIn('error', socket_client.emit('send_command', {'command': 'help'}, callback=True))
        process.poll.return_value = None
        with self.module.server_backups.exclusive():
            response = self.client.post('/api/command', json={'command': 'help'}, headers=self.headers())
            self.assertEqual(response.status_code, 409)
            self.assertIn('error', socket_client.emit('send_command', {'command': 'help'}, callback=True))
        reader = self.client_for(self.create('command-reader', ['console.view']))
        reader_socket = self.module.socketio.test_client(self.app, flask_test_client=reader)
        self.addCleanup(lambda: reader_socket.disconnect() if reader_socket.is_connected() else None)
        self.assertEqual(reader.post('/api/command', json={'command': 'help'}, headers=self.headers(reader)).status_code, 403)
        self.assertIn('error', reader_socket.emit('send_command', {'command': 'help'}, callback=True))
        self.assertEqual(self.client.post('/api/command', json={'command': 'help'}).status_code, 403)
        process.stdin.write.assert_not_called()
        process.stdin.flush.assert_not_called()

    def test_console_commands_closed_stdin_reports_failure_without_retrying(self):
        self.config.update(server_type='Purpur', mc_version='26.3')
        self.module.save_config(self.config)
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        for operation in ('write', 'flush'):
            for error_type in (BrokenPipeError, ValueError):
                for transport in ('http', 'socket'):
                    with self.subTest(operation=operation, error_type=error_type, transport=transport):
                        process.stdin.reset_mock(side_effect=True)
                        getattr(process.stdin, operation).side_effect = error_type('closed stream')
                        if transport == 'http':
                            result = self.client.post('/api/command', json={'command': 'help'}, headers=self.headers())
                            self.assertEqual(result.status_code, 409, result.json)
                            self.assertEqual(result.json['status'], 'error')
                        else:
                            result = socket_client.emit('send_command', {'command': 'help'}, callback=True)
                            self.assertIn('error', result)
                        process.stdin.write.assert_called_once_with('/help\n')
                        if operation == 'write':
                            process.stdin.flush.assert_not_called()
                        else:
                            process.stdin.flush.assert_called_once_with()

    def test_automation_console_shutdown_aliases_cancel_before_delivery(self):
        self.config.update(server_type='Purpur', mc_version='26.3')
        self.module.save_config(self.config)
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        aliases = ('stop', 'minecraft:stop', 'bukkit:stop', 'restart', 'bukkit:restart', 'spigot:restart')
        for alias in aliases:
            for prefix in ('', '/'):
                for transport in ('http', 'socket'):
                    command = prefix + alias
                    with self.subTest(command=command, transport=transport):
                        events = []
                        process.stdin.write.side_effect = lambda value: events.append(('write', value))
                        process.stdin.flush.side_effect = lambda: events.append(('flush', None))
                        with patch.object(self.module.server_automation, 'stop',
                                          side_effect=lambda: events.append(('cancel', None))):
                            if transport == 'http':
                                response = self.client.post('/api/command', json={'command': command}, headers=self.headers())
                                self.assertEqual(response.status_code, 200, response.json)
                            else:
                                response = socket_client.emit('send_command', {'command': command}, callback=True)
                                self.assertEqual(response['status'], 'success')
                        self.assertEqual(events, [('cancel', None), ('write', '/' + alias + '\n'), ('flush', None)])

    def test_automation_console_non_shutdown_commands_preserve_policy(self):
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        socket_client = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: socket_client.disconnect() if socket_client.is_connected() else None)
        with patch.object(self.module.server_automation, 'stop') as cancel:
            for command in ('say stop', '/say stop', 'say /restart', '//stop', '//restart', 'stopping', 'plugins'):
                with self.subTest(command=command):
                    response = self.client.post('/api/command', json={'command': command}, headers=self.headers())
                    self.assertEqual(response.status_code, 200, response.json)
                    result = socket_client.emit('send_command', {'command': command}, callback=True)
                    self.assertEqual(result['status'], 'success')
            cancel.assert_not_called()

    def test_automation_rejected_console_commands_never_cancel_policy(self):
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        owner_socket = self.module.socketio.test_client(self.app, flask_test_client=self.client)
        self.addCleanup(lambda: owner_socket.disconnect() if owner_socket.is_connected() else None)
        viewer = self.client_for(self.create('automation-console-reader', ['console.view']))
        viewer_socket = self.module.socketio.test_client(self.app, flask_test_client=viewer)
        self.addCleanup(lambda: viewer_socket.disconnect() if viewer_socket.is_connected() else None)
        with patch.object(self.module.server_automation, 'stop') as cancel:
            response = viewer.post('/api/command', json={'command': 'stop'}, headers=self.headers(viewer))
            self.assertEqual(response.status_code, 403)
            self.assertIn('error', viewer_socket.emit('send_command', {'command': 'stop'}, callback=True))
            self.assertEqual(self.client.post('/api/command', json={'command': 'stop'}).status_code, 403)
            response = self.client.post('/api/command', json={'command': 'stop\nhelp'}, headers=self.headers())
            self.assertEqual(response.status_code, 400)
            self.assertIn('error', owner_socket.emit('send_command', {'command': 'stop\nhelp'}, callback=True))
            with self.module.server_backups.exclusive():
                response = self.client.post('/api/command', json={'command': 'stop'}, headers=self.headers())
                self.assertEqual(response.status_code, 409)
                self.assertIn('error', owner_socket.emit('send_command', {'command': 'stop'}, callback=True))
            process.poll.return_value = 1
            response = self.client.post('/api/command', json={'command': 'help'}, headers=self.headers())
            self.assertEqual(response.json['status'], 'error')
            self.assertIn('error', owner_socket.emit('send_command', {'command': 'help'}, callback=True))
            cancel.assert_not_called()
        process.stdin.write.assert_not_called()

    def test_socket_auth_permissions_and_revocation(self):
        anonymous = self.module.socketio.test_client(self.app, flask_test_client=self.app.test_client())
        self.assertFalse(anonymous.is_connected())
        reader = self.create('console-reader', ['console.view'])
        client = self.client_for(reader)
        socket = self.module.socketio.test_client(self.app, flask_test_client=client)
        self.addCleanup(lambda: socket.disconnect() if socket.is_connected() else None)
        self.assertTrue(socket.is_connected())
        result = socket.emit('send_command', {'command': 'stop'}, callback=True)
        self.assertIn('error', result)
        self.security.emit_console(self.module.socketio, 'before')
        self.assertEqual(socket.get_received()[0]['args'][0]['data'], 'before')
        self.store.delete_user(reader['id'], self.owner)
        self.security.emit_console(self.module.socketio, 'after')
        self.assertEqual(socket.get_received(), [])

    def test_moderation_cannot_inject_console_commands(self):
        moderator = self.client_for(self.create('moderator', ['players.manage']))
        process = Mock()
        process.poll.return_value = None
        self.module.MINECRAFT_PROCESS = process
        result = moderator.post('/api/player/kick', json={'username': 'Steve', 'reason': 'test\nop Steve'}, headers=self.headers(moderator))
        self.assertEqual(result.status_code, 400)
        process.stdin.write.assert_not_called()


class MigrationTests(unittest.TestCase):
    def test_legacy_backup_rights_migrate_without_granting_purge_or_extra_restore_access(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountStore(directory)
            owner = store.bootstrap('owner', 'Owner-password-123')
            cases = (
                ('creator', ['backups.manage'], {'backups.create'}),
                ('importer', ['server.reset'], {'server.import'}),
                ('restorer', ['backups.manage', 'server.reset'],
                 {'backups.create', 'backups.restore', 'server.import'}),
                ('viewer', [], set()),
                ('forged-owner', ['server.purge'], set()),
            )
            previous = {}
            for name, legacy, expected in cases:
                user = store.save_user(dict(username=name, password='User-password-123', permissions=[]), owner)
                with store.connection() as db:
                    db.execute('UPDATE accounts SET permissions=? WHERE id=?', (json.dumps(legacy), user['id']))
                cookie = {}
                store.sign_in(cookie, user)
                previous[name] = (user, cookie)
            migrated = AccountStore(directory)
            revisions = {}
            for name, legacy, expected in cases:
                user = migrated.authenticate(name, 'User-password-123')
                self.assertEqual(set(user['permissions']), expected)
                self.assertEqual(grants(user), expected)
                self.assertNotIn('server.purge', grants(user))
                old_user, cookie = previous[name]
                if legacy:
                    self.assertIsNone(migrated.current_user(cookie))
                    self.assertGreater(user['revision'], old_user['revision'])
                else:
                    self.assertIsNotNone(migrated.current_user(cookie))
                revisions[name] = user['revision']
            owner_again = migrated.authenticate('owner', 'Owner-password-123')
            self.assertEqual(owner_again['id'], owner['id'])
            self.assertIn('server.purge', grants(owner_again))
            # Migration is idempotent; a normal restart does not revoke sessions again.
            restarted = AccountStore(directory)
            for name, revision in revisions.items():
                self.assertEqual(restarted.authenticate(name, 'User-password-123')['revision'], revision)

    def test_existing_database_migrates_without_forcing_existing_users_to_change_password(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'panel_accounts.db'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute("INSERT INTO metadata VALUES ('scope', 'existing-scope')")
                db.execute('''CREATE TABLE accounts (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    email TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
                    permissions TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL)''')
                db.execute('''INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           ('old-id', 'existing', '', hash_password('existing password'),
                            'custom', '[]', 1, 1, '2026-01-01'))
            for attempt in range(2):
                store = AccountStore(directory)
                user = store.authenticate('existing', 'existing password')
                self.assertFalse(user['must_change_password'])
                self.assertIsNone(user['temporary_password_expires'])
                self.assertIsNotNone(store.current_user(dict(account_id='old-id', account_revision=1,
                                                            account_scope='existing-scope')))

    def test_legacy_owner_migrates_once_and_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(is_setup=True, admin_user='legacy admin', admin_pass='old password')
            app = Flask('migration', template_folder=str(APP_DIR / 'templates'))
            security = PanelSecurity(app, directory, lambda: config, lambda value: None, lambda: {})
            user = security.store.authenticate('legacy admin', 'old password')
            self.assertEqual(user['role'], 'owner')
            self.assertNotIn('admin_pass', config)
            again = AccountStore(directory)
            self.assertEqual(again.authenticate('legacy admin', 'old password')['id'], user['id'])
            cookie = {}
            again.sign_in(cookie, user)
            self.assertIsNotNone(again.current_user(cookie))
            again.reset()
            self.assertIsNone(again.current_user(cookie))
            self.assertEqual(again.list_users(), [])


if __name__ == '__main__':
    unittest.main()
