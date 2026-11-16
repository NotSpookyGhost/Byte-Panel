"""Plugin routes tested against temporary server data, without booting Minecraft."""

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from flask import Flask, render_template

APP_DIR = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP_DIR))
import plugin_manager


class PluginManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.server = self.data / 'server'
        self.server.mkdir()
        self.root = self.server / 'plugins'
        self.root.mkdir()
        self.config = dict(is_setup=True, server_type='Paper', mc_version='1.21.1',
                           theme='dark', panel_name='Test panel', admin_user='admin')
        self.app = Flask(__name__, template_folder=str(APP_DIR / 'templates'),
                         static_folder=str(APP_DIR / 'static'))
        self.app.config.update(SECRET_KEY='test-only', TESTING=True)
        plugin_manager.register_plugin_manager(
            self.app, self.server, self.data, lambda: self.config, lambda: {},
        )
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True
            session['plugin_csrf_token'] = 'test-csrf'
        self.headers = {'X-Plugin-CSRF-Token': 'test-csrf'}

    def jar(self, descriptor='plugin.yml'):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, 'w') as archive:
            archive.writestr(descriptor, 'name: Demo\nversion: 1.0\nmain: example.Demo\n')
        return contents.getvalue()

    def upload(self, content, name='Demo.jar', path='', **kwargs):
        return self.client.post('/api/plugins/upload', headers=self.headers,
                                data={'file': (io.BytesIO(content), name), 'path': path, **kwargs})

    def config_file(self, content=b'enabled: true\r\n'):
        folder = self.root / 'Demo'
        folder.mkdir(exist_ok=True)
        target = folder / 'config.yml'
        target.write_bytes(content)
        return target

    def test_plugin_upload_list_download_for_both_engines(self):
        for engine, descriptor in [('Paper', 'paper-plugin.yml'), ('Purpur', 'plugin.yml')]:
            with self.subTest(engine=engine):
                self.config['server_type'] = engine
                content = self.jar(descriptor)
                name = engine + '.jar'
                response = self.upload(content, name)
                self.assertEqual(response.status_code, 201, response.json)
                self.assertEqual((self.root / name).read_bytes(), content)
                entries = self.client.get('/api/plugins/files').json['entries']
                entry = next(entry for entry in entries if entry['name'] == name)
                self.assertFalse(entry['editable'])
                download = self.client.get('/api/plugins/download', query_string={'path': name})
                self.assertEqual(download.data, content)
                self.assertIn('attachment', download.headers['Content-Disposition'])
                download.close()

    def test_config_edit_backup_and_stale_save(self):
        target = self.config_file()
        response = self.client.get('/api/plugins/file?path=Demo/config.yml')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['content'], 'enabled: true\r\n')
        payload = dict(path='Demo/config.yml', revision=response.json['revision'], content='enabled: false\r\n')
        saved = self.client.put('/api/plugins/file', json=payload, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(target.read_bytes(), b'enabled: false\r\n')
        backup = self.data / 'plugin-config-backups' / saved.json['backup']
        self.assertEqual(backup.read_bytes(), b'enabled: true\r\n')
        self.assertEqual(self.client.put('/api/plugins/file', json=payload, headers=self.headers).status_code, 409)
        self.assertFalse(any(p.name.startswith('.panel-') for p in target.parent.iterdir()))

    def test_external_edit_is_not_overwritten(self):
        target = self.config_file()
        revision = self.client.get('/api/plugins/file?path=Demo/config.yml').json['revision']
        target.write_text('changed by plugin', encoding='utf-8')
        response = self.client.put('/api/plugins/file', headers=self.headers,
                                   json=dict(path='Demo/config.yml', revision=revision, content='stale editor'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(target.read_text(encoding='utf-8'), 'changed by plugin')

    def test_confirmed_upload_replacement_keeps_backup(self):
        original = self.jar()
        newer = self.jar('paper-plugin.yml')
        self.assertEqual(self.upload(original).status_code, 201)
        conflict = self.upload(newer)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual((self.root / 'Demo.jar').read_bytes(), original)
        saved = self.upload(newer, replace_revision=conflict.json['revision'])
        self.assertEqual(saved.status_code, 201, saved.json)
        self.assertEqual((self.data / 'plugin-config-backups' / saved.json['backup']).read_bytes(), original)
        self.assertEqual((self.root / 'Demo.jar').read_bytes(), newer)

    def test_delete_jar_keeps_backup_and_config_folder_for_both_engines(self):
        config = self.config_file()
        for engine in ['Paper', 'Purpur']:
            with self.subTest(engine=engine):
                self.config['server_type'] = engine
                content = self.jar()
                self.assertEqual(self.upload(content, 'Demo.JAR').status_code, 201)
                preview = self.client.get('/api/plugins/delete?path=Demo.JAR')
                self.assertEqual(preview.status_code, 200)
                self.assertTrue((self.root / 'Demo.JAR').exists())
                response = self.client.delete('/api/plugins/delete', json=preview.json, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertFalse((self.root / 'Demo.JAR').exists())
                self.assertEqual(config.read_bytes(), b'enabled: true\r\n')
                backup = self.data / 'plugin-config-backups' / response.json['backup']
                self.assertEqual(backup.read_bytes(), content)
                self.assertNotIn('Demo.JAR', [entry['name'] for entry in self.client.get('/api/plugins/files').json['entries']])
                self.assertEqual(self.client.delete('/api/plugins/delete', json=preview.json, headers=self.headers).status_code, 404)

    def test_delete_requires_current_confirmed_revision(self):
        self.upload(self.jar())
        preview = self.client.get('/api/plugins/delete?path=Demo.jar').json
        target = self.root / 'Demo.jar'
        target.write_bytes(self.jar('paper-plugin.yml'))
        response = self.client.delete('/api/plugins/delete', json=preview, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(target.exists())
        self.assertEqual(self.client.delete('/api/plugins/delete', json={'path': 'Demo.jar'}, headers=self.headers).status_code, 409)
        for payload in (None, [], 'Demo.jar', {'path': None}):
            self.assertEqual(self.client.delete('/api/plugins/delete', json=payload, headers=self.headers).status_code, 400)
        self.assertTrue(target.exists())

    def test_delete_cannot_remove_configs_folders_or_paths_outside_plugins(self):
        config = self.config_file()
        outside = self.data / 'outside.jar'
        outside.write_bytes(self.jar())
        for relative in ['', 'Demo', 'Demo/config.yml', '../outside.jar', '/outside.jar', 'C:/outside.jar', '..\\outside.jar']:
            with self.subTest(path=relative):
                for method in ('GET', 'DELETE'):
                    response = (self.client.get('/api/plugins/delete', query_string={'path': relative}) if method == 'GET'
                                else self.client.delete('/api/plugins/delete', json={'path': relative, 'revision': 'bad'}, headers=self.headers))
                    self.assertIn(response.status_code, (400, 403))
        self.assertTrue(config.exists())
        self.assertTrue(outside.exists())

    def test_failed_backup_does_not_delete_jar(self):
        original = self.jar()
        self.upload(original)
        preview = self.client.get('/api/plugins/delete?path=Demo.jar').json
        with patch.object(plugin_manager.tempfile, 'mkstemp', side_effect=OSError('Disk full')):
            with self.assertLogs(self.app.logger, level='ERROR'):
                response = self.client.delete('/api/plugins/delete', json=preview, headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertEqual((self.root / 'Demo.jar').read_bytes(), original)

    def test_delete_rejects_external_update_during_backup(self):
        original = self.jar()
        self.upload(original)
        preview = self.client.get('/api/plugins/delete?path=Demo.jar').json
        target = self.root / 'Demo.jar'
        real_fsync = plugin_manager.os.fsync
        def replace_during_backup(fd):
            real_fsync(fd)
            target.write_bytes(b'external replacement')
        with patch.object(plugin_manager.os, 'fsync', side_effect=replace_during_backup):
            response = self.client.delete('/api/plugins/delete', json=preview, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(target.read_bytes(), b'external replacement')

    def test_delete_rejects_symbolic_links(self):
        outside = self.data / 'outside.jar'
        outside.write_bytes(self.jar())
        try:
            (self.root / 'linked.jar').symlink_to(outside)
        except OSError:
            self.skipTest('This host does not permit symbolic links')
        self.assertEqual(self.client.get('/api/plugins/delete?path=linked.jar').status_code, 403)
        response = self.client.delete('/api/plugins/delete', json={'path': 'linked.jar', 'revision': 'bad'}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(outside.exists())

    def test_configs_upload_to_subfolder_and_jars_only_to_root(self):
        self.config_file()
        response = self.upload(b'locale: en\n', 'messages.yml', 'Demo')
        self.assertEqual(response.status_code, 201)
        self.assertTrue((self.root / 'Demo' / 'messages.yml').exists())
        self.assertEqual(self.upload(self.jar(), path='Demo').status_code, 400)
        listing = self.client.get('/api/plugins/files?path=Demo').json
        self.assertEqual({entry['name'] for entry in listing['entries']}, {'config.yml', 'messages.yml'})

    def test_authentication_engine_gate_and_csrf(self):
        guest = self.app.test_client()
        self.assertEqual(guest.get('/api/plugins/files').status_code, 401)
        self.assertEqual(guest.post('/api/plugins/upload').status_code, 401)
        self.assertEqual(guest.get('/api/plugins/delete?path=Demo.jar').status_code, 401)
        self.assertEqual(guest.delete('/api/plugins/delete').status_code, 401)
        for engine in ['Forge', 'Fabric', 'Vanilla', None]:
            self.config['server_type'] = engine
            self.assertEqual(self.client.get('/plugins').status_code, 403)
            self.assertEqual(self.upload(self.jar()).status_code, 403)
            self.assertEqual(self.client.delete('/api/plugins/delete', json={'path': 'Demo.jar'}, headers=self.headers).status_code, 403)
        self.config['server_type'] = 'Paper'
        self.assertEqual(self.client.put('/api/plugins/file', json={}).status_code, 403)
        self.assertEqual(self.client.delete('/api/plugins/delete', json={}).status_code, 403)
        self.config['is_setup'] = False
        self.assertEqual(self.client.get('/api/plugins/files').status_code, 403)

    def test_traversal_absolute_paths_and_links_are_blocked(self):
        outside = self.data / 'private.yml'
        outside.write_text('private', encoding='utf-8')
        for path in ['../private.yml', '/private.yml', 'C:/private.yml', '..\\private.yml',
                     'Demo/../../private.yml', '.hidden/config.yml']:
            with self.subTest(path=path):
                response = self.client.get('/api/plugins/file', query_string={'path': path})
                self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.upload(self.jar(), '../escape.jar').status_code, 400)
        try:
            (self.root / 'linked.yml').symlink_to(outside)
        except OSError:
            self.skipTest('This host does not permit symbolic links')
        response = self.client.get('/api/plugins/file?path=linked.yml')
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('linked.yml', [entry['name'] for entry in self.client.get('/api/plugins/files').json['entries']])

    def test_binary_invalid_jar_and_config_size_limits(self):
        self.assertEqual(self.upload(b'not a jar').status_code, 400)
        self.assertEqual(self.upload(self.jar('README.txt')).status_code, 400)
        self.assertEqual(self.upload(b'\x00bad', 'config.yml').status_code, 415)
        self.assertEqual(self.upload(b'\xff', 'config.yml').status_code, 415)
        self.assertEqual(self.upload(b'data', 'world.db').status_code, 415)
        (self.root / 'test.jar').write_bytes(self.jar())
        self.assertEqual(self.client.get('/api/plugins/file?path=test.jar').status_code, 415)
        self.config_file(b'a' * 100)
        with patch.object(plugin_manager, 'MAX_CONFIG_BYTES', 32):
            self.assertEqual(self.client.get('/api/plugins/file?path=Demo/config.yml').status_code, 413)
            self.assertEqual(self.upload(b'a' * 100, 'big.yml').status_code, 413)
        self.assertFalse((self.root / 'big.yml').exists())
        self.assertFalse(any(p.name.startswith('.panel-') for p in self.root.iterdir()))

    def test_upload_request_limit_preserves_other_route_limits(self):
        with patch.object(plugin_manager, 'MAX_UPLOAD_BYTES', 8):
            response = self.client.post('/api/plugins/upload', headers=self.headers,
                                       data=b'x' * (1024 * 1024 + 100), content_type='multipart/form-data')
            self.assertEqual(response.status_code, 413)
        with self.app.test_request_context('/api/import'):
            from flask import request
            self.assertIsNone(request.max_content_length)

    def test_invalid_unicode_input_is_rejected_without_writing(self):
        target = self.config_file()
        response = self.client.put('/api/plugins/file', headers=self.headers,
                                   json=dict(path='Demo/config.yml', content='\ud800', revision='invalid'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(target.read_bytes(), b'enabled: true\r\n')
        self.assertEqual(self.client.put('/api/plugins/file', json={},
                         headers={'X-Plugin-CSRF-Token': '\u00e9'}).status_code, 403)

    def test_page_templates_and_conditional_navigation(self):
        response = self.client.get('/plugins')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(b'data-csrf-token="test-csrf"', response.data)
        self.assertIn(b'id="plugin-content"', response.data)
        for control in ('config-search', 'search-previous', 'search-next', 'search-match-case', 'search-status'):
            self.assertIn(('id="' + control + '"').encode(), response.data)
        self.assertLess(response.data.index(b'plugin_search.js'), response.data.index(b'plugins.js'))
        for template in (APP_DIR / 'templates').glob('*.html'):
            self.app.jinja_env.get_template(template.name)
        with self.app.test_request_context('/overview'):
            for engine in ['Paper', 'Purpur', 'Forge', 'Vanilla', 'Fabric', None]:
                markup = render_template('plugins_nav.html', config={'server_type': engine})
                self.assertEqual('/plugins' in markup, engine in ('Paper', 'Purpur'))


if __name__ == '__main__':
    unittest.main()
