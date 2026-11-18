"""Optional headless layout/interaction check. Creates temporary data; never uses /data.

Run with playwright installed and Chrome available:
    python tests/browser_obsidian.py --artifacts <screenshot-directory>
No Minecraft process or real email is started.
"""
import argparse
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

from flask import abort, request
from werkzeug.serving import make_server, WSGIRequestHandler
from playwright.sync_api import sync_playwright
from test_accounts import AccountIntegrationTests as Fixture


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifacts', default=str(Path(tempfile.gettempdir()) / 'byte-panel-obsidian-qa'))
    args = parser.parse_args()
    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    Fixture.setUpClass()
    fixture = Fixture()
    fixture.setUp()
    app = fixture.app
    failures = []
    widths = {}
    errors = []
    server = None
    try:
        fixture.config.update(server_name='Obsidian Realm', server_type='Purpur', ram_gb=8)
        fixture.module.save_config(fixture.config)
        props = {
            'server-port':'25565', 'max-players':'20', 'motd':'Temporary QA realm', 'online-mode':'true',
            'level-name':'world', 'generate-structures':'true', 'difficulty':'normal', 'gamemode':'survival',
            'pvp':'true', 'white-list':'false', 'resource-pack':'', 'require-resource-pack':'false',
            'view-distance':'10', 'simulation-distance':'10'}
        (fixture.root / 'server.properties').write_text('\n'.join(k + '=' + v for k, v in props.items()), encoding='utf-8')
        (fixture.root / 'banned-players.json').write_text(json.dumps([
            {'uuid':'00000000-0000-0000-0000-000000000002','name':'QAVisitor','reason':'Temporary QA ban','source':'QA'}
        ]), encoding='utf-8')
        plugin = fixture.root / 'plugins' / 'LuckPerms'
        plugin.mkdir(parents=True)
        (plugin / 'config.yml').write_text('test-key: true\n' * 160 + 'search-target: value\n', encoding='utf-8')
        logs = '\n'.join('[12:30:00] [Server thread/INFO]: Temporary QA log line ' + str(i) for i in range(500))
        (fixture.root / 'console.log').write_text(logs, encoding='utf-8')
        fixture.module.CONSOLE_HISTORY[:] = logs.splitlines()
        with closing(sqlite3.connect(fixture.module.PLAYER_DB_FILE)) as conn:
            for number, name, online, banned in [(1, 'QABuilder', 1, 0), (2, 'QAVisitor', 0, 1)]:
                uuid = '00000000-0000-0000-0000-' + str(number).zfill(12)
                conn.execute('INSERT INTO users (uuid,username,first_seen,last_seen,online_status,ban_status) VALUES (?,?,?,?,?,?)',
                             (uuid, name, '2026-11-16 10:00:00', '2026-11-17 12:30:00', online, banned))
                conn.execute('INSERT INTO login_events (uuid,timestamp,ip,country) VALUES (?,?,?,?)',
                             (uuid, '2026-11-17 12:30:00', '192.0.2.' + str(number), 'Test'))
            conn.commit()

        # Real archives in the disposable fixture: spaced names normalize on disk.
        active_backup = Path(fixture.module.create_backup('Obsidian Realm', is_manual=True))
        other_backup = Path(fixture.module.create_backup('Other Realm', is_manual=True))
        active_bytes, other_bytes = active_backup.read_bytes(), other_backup.read_bytes()

        @app.before_request
        def prevent_operational_writes():
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.path not in ('/login', '/settings') and not request.path.startswith('/api/plugins/'):
                abort(418)

        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)

        def check(condition, message):
            if not condition:
                failures.append(message)

        with sync_playwright() as p:
            executable = os.environ.get('BYTE_PANEL_TEST_BROWSER', r'C:\Program Files\Google\Chrome\Application\chrome.exe')
            browser = p.chromium.launch(executable_path=executable, headless=True)
            context = browser.new_context(viewport={'width':1440, 'height':1000}, reduced_motion='reduce')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/login')
            check(page.locator('#panel-sidebar').count() == 0, 'Login has no sidebar')
            page.screenshot(path=str(artifacts / 'login.png'))
            page.locator('[name=username]').fill('owner')
            page.locator('[name=password]').fill('Owner-password-123')
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.wait_for_url('**/overview')
            paths = ['overview','console','logs','players','plugins','properties','backups','settings','users','account']
            for width, height in [(1440,1000),(900,900),(390,844)]:
                page.set_viewport_size({'width':width,'height':height})
                widths[width] = {}
                for path in paths:
                    page.goto(base + '/' + path)
                    page.wait_for_function("document.querySelector('[data-status-label]').textContent !== 'Checking status'")
                    box = page.locator('#panel-sidebar').bounding_box()
                    widths[width][path] = box['width']
                    expected = 225 if width == 1440 else 200 if width == 900 else 240
                    check(abs(box['width'] - expected) < 1, f'{path}: sidebar width {width}: {box}')
                    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                    check(not overflow, f'{path}: horizontal page overflow at {width}')
                    if width == 390:
                        check(page.locator('#panel-sidebar').evaluate('(el) => el.inert'), f'{path}: hidden mobile nav inert')
                        page.locator('#menu-toggle').click()
                        check(page.locator('#panel-sidebar').bounding_box()['x'] == 0, f'{path}: mobile drawer opens')
                        page.keyboard.press('Escape')
                        check(page.locator('#menu-toggle').get_attribute('aria-expanded') == 'false', f'{path}: mobile drawer closes')
                    if path in ('console','logs'):
                        output = page.locator('#serverConsole' if path == 'console' else '#log-box').bounding_box()
                        check(output['height'] > height * .58, f'{path}: output area too short at {width}: {output["height"]}')
                    if path == 'backups':
                        page.wait_for_function("document.getElementById('backups-container').textContent.includes('Current Server')")
                        current = page.locator('#backups-container > div').first
                        check('Obsidian Realm' in current.inner_text(), 'Active backup display name')
                        check(active_backup.name in current.inner_text(), 'Active archive stays in current group')
                        check(page.locator('#backups-container details').count() == 1, 'Only other server is collapsible')
                        other = page.locator('#backups-container details')
                        check('Other_Realm' in other.locator('summary').inner_text(), 'Other server label')
                        check(active_backup.name not in other.text_content(), 'Active archive not in other server group')
                        check(other_backup.name in other.text_content(), 'Other archive preserved')
                        download = current.get_by_role('button', name='Download', exact=True)
                        check('Obsidian_Realm/' in download.get_attribute('onclick'), 'Backup download uses storage key')
                    if width in (1440,390):
                        page.screenshot(path=str(artifacts / f'{path}-{width}.png'))
            page.set_viewport_size({'width':1440,'height':1000})
            # Verify the default mark is closed, then render a real configured logo.
            page.goto(base + '/backups')
            check(page.locator('.sidebar-brand .brand-mark path').get_attribute('d').endswith('Z'), 'Default brand mark is closed')
            logo = fixture.data / 'branding' / 'logo.png'
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'app' / 'static' / 'bytedev_logo.png', logo)
            page.reload()
            page.wait_for_function("document.querySelector('.sidebar-brand .brand-logo').naturalWidth > 0")
            check(page.locator('.sidebar-brand a').get_attribute('href') == '/overview', 'Brand home link')
            check(page.locator('.sidebar-brand .brand-mark').count() == 0, 'Uploaded logo replaces fallback')
            page.screenshot(path=str(artifacts / 'backups-custom-logo.png'))
            check(active_backup.read_bytes() == active_bytes and other_backup.read_bytes() == other_bytes, 'Existing backup bytes unchanged')
            # Upload through General, using the real form and decoder (temporary data only).
            page.goto(base + '/settings')
            page.locator('[data-settings-tab=general]').click()
            check(page.locator('#settings-general input[name=server_icon]').count() == 1, 'Server icon is in General')
            check(page.locator('#settings-appearance input[name=custom_logo]').count() == 0, 'Custom Logo removed from Appearance')
            page.locator('#serverIconInput').set_input_files(str(Path(__file__).resolve().parents[1] / 'app' / 'static' / 'bytedev_logo.png'))
            page.locator('#settingsForm button[type=submit]').click()
            page.wait_for_load_state()
            check('Restart Minecraft' in page.locator('.main-content > p[role=status]').inner_text(), 'Minecraft restart notice')
            page.wait_for_function("document.querySelector('.server-icon-preview').naturalWidth === 64")
            canonical = (fixture.root / 'server-icon.png').read_bytes()
            check(context.request.get(base + '/branding-assets/logo.png').body() == canonical, 'Panel serves exact Minecraft icon')
            check(page.locator('.sidebar-brand .brand-logo').evaluate('(el) => el.naturalWidth') == 64, 'Sidebar uses converted icon')
            check(page.locator('.sidebar-server .server-icon img').evaluate('(el) => el.naturalWidth') == 64, 'Server badge uses converted icon')
            page.screenshot(path=str(artifacts / 'server-icon-general.png'))
            page.set_viewport_size({'width':390,'height':844})
            check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Server icon setting fits mobile')
            page.screenshot(path=str(artifacts / 'server-icon-mobile.png'))
            page.set_viewport_size({'width':1440,'height':1000})
            # Exercise all Settings sections and persist every accent using the real form.
            for accent in ('lime','yellow','blue'):
                page.goto(base + '/settings')
                for key in ('general','runtime','backups','appearance','email','operations'):
                    page.locator('[data-settings-tab=' + key + ']').click()
                    check(page.locator('#settings-' + key).is_visible(), 'Settings panel ' + key)
                page.locator('[data-settings-tab=appearance]').click()
                page.locator('#accentSelect').select_option(accent)
                page.locator('#settingsForm button[type=submit]').click()
                page.wait_for_load_state()
                check(fixture.module.load_config()['accent'] == accent, 'Saved accent ' + accent)
                page.goto(base + '/overview')
                check(page.locator('body').get_attribute('data-accent') == accent, 'Persisted accent ' + accent)
                for status, state in [('Online','online'),('Restarting','transition'),('Offline','offline'),('Status unavailable','unknown')]:
                    page.evaluate('(status) => BytePanelShell.renderStatus(status)', status)
                    dot = page.locator('.sidebar-server [data-server-dot]')
                    check(dot.get_attribute('data-state') == state, 'Status mapping ' + status)
                    color = dot.evaluate('(el) => getComputedStyle(el).backgroundColor')
                    expected_colors = {'online':'rgb(113, 214, 155)', 'transition':'rgb(239, 188, 96)',
                                       'offline':'rgb(238, 119, 119)', 'unknown':'rgb(166, 166, 166)'}
                    check(color == expected_colors[state], 'Status color independent of ' + accent + ': ' + state)
                page.screenshot(path=str(artifacts / ('accent-' + accent + '.png')))
            page.goto(base + '/settings')
            page.locator('[data-settings-tab=general]').click()
            page.locator('#serverNameInput').fill('Renamed QA Realm')
            page.locator('#settingsForm button[type=submit]').click()
            page.wait_for_load_state()
            check(fixture.module.load_config()['server_name'] == 'Renamed QA Realm', 'Server name persistence')
            check(page.locator('.sidebar-brand').inner_text().startswith('Byte Panel'), 'Product brand unchanged')
            # All Properties tabs/values/types, search and unsaved reset (no POST).
            page.goto(base + '/properties')
            for tab in page.locator('.tab-btn').all():
                tab.click()
                target = tab.get_attribute('data-target')
                check(page.locator('#tab_' + target).is_visible(), 'Properties tab ' + target)
            for key, value in props.items():
                check(page.locator('[name="' + key + '"]').input_value() == value, 'Property value ' + key)
            check(page.locator('[name=difficulty]').evaluate('(el) => el.tagName') == 'SELECT', 'Difficulty select')
            page.locator('#propertySearch').fill('difficulty')
            page.locator('#propertySearch').dispatch_event('input')
            check(page.locator('[name=difficulty]').is_visible(), 'Property search')
            page.locator('[name=difficulty]').select_option('hard')
            check(page.locator('#unsavedChangesBar').is_visible(), 'Properties unsaved warning')
            page.locator('#btnRevertChanges').click()
            check(page.locator('[name=difficulty]').input_value() == 'normal', 'Properties revert')
            # Existing Players filters, search, sorting and moderation dialogs; no actions submitted.
            page.goto(base + '/players')
            check(page.locator('#rosterTable th').count() == 8, 'Players fields retained')
            page.get_by_role('button', name='Banned', exact=True).click()
            check(page.locator('#rosterTable tbody tr:visible').count() == 1, 'Banned filter')
            page.get_by_role('button', name='All', exact=True).click()
            page.locator('#playerSearch').fill('QABuilder')
            page.locator('#playerSearch').dispatch_event('keyup')
            check(page.locator('#rosterTable tbody tr:visible').count() == 1, 'Player search')
            page.get_by_role('button', name='Kick', exact=True).first.click()
            check(page.locator('#kickModal').is_visible(), 'Kick dialog')
            page.evaluate('closeKickModal()')
            # Plugin directory, editor search, dirty confirmation, save and data upload into a subfolder.
            page.goto(base + '/plugins')
            page.get_by_role('button', name='LuckPerms', exact=True).click()
            page.get_by_role('button', name='config.yml', exact=True).click()
            page.locator('#config-search').fill('search-target')
            page.locator('#search-next').click()
            check('1 of 1' in page.locator('#search-status').inner_text(), 'Config search')
            page.locator('#plugin-content').fill('test-key: false\nsearch-target: updated\n')
            page.locator('#save-config').click()
            page.wait_for_function("document.getElementById('editor-state').textContent === 'Saved'")
            check('updated' in (plugin / 'config.yml').read_text(), 'Config saved to isolated folder')
            page.locator('#plugin-content').fill('unsaved: true\n')
            page.once('dialog', lambda dialog: dialog.dismiss())
            page.locator('#back-to-files').click()
            check(page.locator('#plugin-content').input_value() == 'unsaved: true\n', 'Dirty back navigation can cancel')
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#back-to-files').click()
            check(page.locator('#plugin-content').is_disabled(), 'Back resets editor')
            page.locator('#plugin-upload').set_input_files({'name':'preset.json.gz','mimeType':'application/gzip','buffer':b'isolated-qa-data'})
            page.locator('#upload-button').click()
            page.wait_for_function("document.getElementById('plugins-message').textContent.includes('1 file(s) uploaded')")
            check((plugin / 'preset.json.gz').read_bytes() == b'isolated-qa-data', 'Subfolder data upload')
            page.screenshot(path=str(artifacts / 'plugin-editor.png'))
            # First-run pages remain standalone; only the disposable fixture is reset.
            fixture.config.update(is_setup=False, welcome_seen=False)
            fixture.module.save_config(fixture.config)
            fixture.store.reset()
            public = browser.new_context(viewport={'width':390,'height':844}, reduced_motion='reduce')
            first_run = public.new_page()
            first_run.goto(base + '/welcome')
            check(first_run.locator('h1').text_content().strip() == 'Welcome to Byte Panel', 'Welcome content retained')
            check(first_run.locator('h1').evaluate('(el) => getComputedStyle(el).color') == 'rgb(255, 255, 255)', 'Welcome white heading')
            check(first_run.locator('#panel-sidebar').count() == 0, 'Welcome has no authenticated navigation')
            first_run.screenshot(path=str(artifacts / 'welcome-mobile.png'))
            fixture.config['welcome_seen'] = True
            fixture.module.save_config(fixture.config)
            with patch.object(fixture.module.release_service, 'catalog', return_value={
                'latest':'1.21.1','versions':[{'version':'1.21.1','engines':['Paper','Purpur']}],'warnings':[]}):
                first_run.goto(base + '/setup')
                check(first_run.locator('#panel-sidebar').count() == 0, 'Setup stays standalone')
                check(first_run.locator('form').count() > 0, 'Setup form retained')
                first_run.screenshot(path=str(artifacts / 'setup-mobile.png'))
            public.close()
            check(not errors, 'Browser JavaScript errors: ' + repr(errors))
            browser.close()
        report = {'failures':failures,'javascript_errors':errors,'sidebar_widths':widths,'artifacts':str(artifacts)}
        (artifacts / 'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))
        return 1 if failures else 0
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()
        Fixture.tearDownClass()


if __name__ == '__main__':
    raise SystemExit(main())

