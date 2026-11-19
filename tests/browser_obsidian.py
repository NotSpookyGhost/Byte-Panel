"""Optional headless layout/interaction check. Creates temporary data; never uses /data.

Run with playwright installed and Chrome available:
    python tests/browser_obsidian.py --artifacts <screenshot-directory>
No Minecraft process or real email is started.
"""
import argparse
import base64
from contextlib import closing
import io
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
import zipfile

from flask import abort, request
from werkzeug.serving import make_server, WSGIRequestHandler
from playwright.sync_api import sync_playwright
from test_accounts import AccountIntegrationTests as Fixture
from test_bluemap import BlueMapProxyTests as MapFixture
from email_designs import load_email_design
from panel_mail import WELCOME_DESIGN


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def check_setup_build_progress(page, base, check):
    def mock_setup(route):
        if route.request.method == 'POST':
            route.fulfill(json={'status': 'started'})
        else:
            route.continue_()

    page.route(base + '/api/bootstrap', lambda route: route.fulfill(json={'status': 'success'}))
    page.route(base + '/setup', mock_setup)
    page.route(base + '/api/setup/progress', lambda route: route.fulfill(json={
        'status': 'running', 'percent': 60, 'msg': 'Building Server Files'}))
    page.get_by_label('Panel Administrator Password').fill('Setup-test-password1!')
    page.get_by_role('button', name='Proceed', exact=True).click()
    page.get_by_role('button', name='Create New Server', exact=True).click()
    page.get_by_label('Server Name', exact=True).fill('Setup Progress QA')
    page.get_by_role('button', name='Create Server', exact=True).click()
    page.wait_for_function("document.getElementById('progress-percent').innerText === '60%'")
    check(page.locator('#stage-4').is_visible(), 'Setup displays install progress')
    fill = page.locator('#progress-fill')
    check(fill.evaluate('(el) => el.style.width') == '60%', 'Setup fill tracks the worker percentage')
    accent = page.locator('.stage-btn-primary').first.evaluate('(el) => getComputedStyle(el).backgroundColor')
    check(fill.evaluate('(el) => getComputedStyle(el).backgroundColor') == accent,
          'Setup fill retains the visible accent color after submitting server creation')


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
    MapFixture.setUpClass()
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
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.path not in ('/login', '/settings', '/api/backup/rename') and not request.path.startswith(('/api/plugins/', '/api/files/')):
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
            # Every updater request is intercepted: browser QA never contacts
            # Modrinth, downloads a JAR, or starts a real update worker.
            update_state = dict(enabled=False, projects=[], addons=[], status='idle', busy=False,
                                message='Updates are disabled.', last_checked=None, last_applied=None)
            update_phase = {'value': None}
            update_requests = []
            update_filename = 'QA <svg onload=window.qaAddonXss=1>.jar'
            def mock_addon_updates(route):
                req = route.request
                update_requests.append((req.method, req.url))
                if req.method == 'PUT':
                    values = req.post_data_json
                    update_state.update(enabled=values['enabled'], projects=values['projects'],
                                        message='Mock update preferences saved.')
                elif req.method == 'POST' and req.url.endswith('/check'):
                    update_state.update(status='checking', busy=True, message='Mock check in progress.')
                    update_phase['value'] = 'check'
                elif req.method == 'POST' and req.url.endswith('/apply'):
                    update_state.update(status='applying', busy=True, message='Mock update in progress.')
                    update_phase['value'] = 'apply'
                elif req.method == 'GET' and update_phase['value']:
                    phase = update_phase['value']
                    update_phase['value'] = None
                    update_state.update(status='complete', busy=False, last_checked='2026-11-18T10:00:00Z',
                                        message='Mock check complete.' if phase == 'check' else 'Mock update completed.')
                    update_state['addons'] = [dict(filename=update_filename, project_id='qa-project',
                        installed_version='1.0' if phase == 'check' else '2.0', available_version='2.0',
                        status='update_available' if phase == 'check' else 'updated',
                        reason='Verified project. Literal <img src=x onerror=window.qaAddonXss=1> text.')]
                    if phase == 'apply':
                        update_state['last_applied'] = '2026-11-18T10:01:00Z'
                route.fulfill(status=200, content_type='application/json', body=json.dumps(update_state))
            page.route('**/api/addon-updates**', mock_addon_updates)
            page.goto(base + '/login')
            check(page.locator('#panel-sidebar').count() == 0, 'Login has no sidebar')
            check(page.locator('.login-card .login-server-identity h1').inner_text() == 'Obsidian Realm', 'Login heading uses server name')
            check(page.locator('.login-server-icon svg').count() == 1, 'Login uses server glyph without uploaded icon')
            check(page.locator('.login-brand').count() == 0, 'Login has no separate product branding row')
            page.screenshot(path=str(artifacts / 'login.png'))
            page.locator('[name=username]').fill('owner')
            page.locator('[name=password]').fill('Owner-password-123')
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.wait_for_url('**/overview')
            # Browser input accepts bare commands; backend integration tests verify
            # the engine-specific prefix. Stub transport: never send live commands.
            submitted_commands = []
            def capture_command(route):
                submitted_commands.append(route.request.post_data_json['command'])
                route.fulfill(status=200, content_type='application/json', body='{"status":"success"}')
            page.route('**/api/command', capture_command)
            page.goto(base + '/console')
            command_input = page.locator('#commandInput')
            check('no / needed' in command_input.get_attribute('placeholder'), 'Console explains optional slash')
            for command in ('help', 'plugins', '/help', '//wand', 'say https://example.com/a/b'):
                command_input.fill(command)
                with page.expect_response('**/api/command'):
                    if command == 'help':
                        command_input.press('Enter')
                    else:
                        page.get_by_role('button', name='Send', exact=True).click()
                check(submitted_commands[-1] == command, 'Console submits command unchanged for backend normalization')
                check(command_input.input_value() == '', 'Console clears submitted command')
            command_input.fill('   ')
            command_input.press('Enter')
            check(len(submitted_commands) == 5, 'Console ignores blank input')
            draft = 'say Unfinished draft'
            command_input.fill(draft)
            for command in reversed(submitted_commands):
                command_input.press('ArrowUp')
                check(command_input.input_value() == command, 'Up recalls older submitted commands')
                check(command_input.evaluate('(el) => el.selectionStart === el.value.length && el.selectionEnd === el.value.length'),
                      'Recalled command caret sits at the end')
            command_input.press('ArrowUp')
            check(command_input.input_value() == 'help', 'History stops at the oldest entry')
            for command in submitted_commands[1:] + [draft]:
                command_input.press('ArrowDown')
                check(command_input.input_value() == command, 'Down recalls newer entries and restores the draft')
            command_input.press('ArrowDown')
            check(command_input.input_value() == draft, 'Down past newest preserves the draft')
            check(len(submitted_commands) == 5, 'History navigation never sends commands')
            command_input.press('ArrowUp')
            page.screenshot(path=str(artifacts / 'console-history-desktop.png'))
            command_input.fill('say Edited recalled command')
            with page.expect_response('**/api/command'):
                command_input.press('Enter')
            check(submitted_commands[-1] == 'say Edited recalled command', 'Edited recall sends only on Enter')
            check(command_input.input_value() == '', 'Sending a recalled command clears the input')
            for command in ('say Edited recalled command', 'say https://example.com/a/b', '//wand'):
                command_input.press('ArrowUp')
                check(command_input.input_value() == command, 'Editing recalled text does not mutate older entries')
            page.set_viewport_size({'width':390,'height':844})
            command_input.press('ArrowDown')
            check(command_input.input_value() == 'say https://example.com/a/b', 'History navigation works at mobile width')
            check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Console history fits mobile layout')
            page.screenshot(path=str(artifacts / 'console-history-mobile.png'))
            page.set_viewport_size({'width':1440,'height':1000})
            page.reload()
            command_input.fill(draft)
            command_input.press('ArrowUp')
            check(command_input.input_value() == draft, 'Page reload clears private command history')
            check(len(submitted_commands) == 6, 'Arrow navigation never submits a command after refresh')
            page.unroute('**/api/command', capture_command)
            paths = ['overview','console','logs','players','plugins','files','properties','backups','settings','users','account']
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
                    check(page.locator('.sidebar-brand .brand-mark path').evaluate('(el) => getComputedStyle(el).stroke') == 'rgb(255, 255, 255)',
                          f'{path}: product logo stays white at {width}')
                    version = page.locator('.sidebar-version')
                    check(version.inner_text() == fixture.module.inject_version()['app_version'], f'{path}: footer contains version only')
                    check(abs(version.locator('span').bounding_box()['x'] - version.bounding_box()['x'] - 5) < 1,
                          f'{path}: version sits at bottom-left at {width}')
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
            # Theme/accent and link interaction states must never recolor the product mark.
            original_brand_theme = page.locator('body').get_attribute('class')
            original_brand_accent = page.locator('body').get_attribute('data-accent')
            for theme in ('dark', 'black', 'light'):
                for accent in ('lime', 'yellow', 'blue'):
                    page.evaluate("""([theme, accent]) => {
                        document.body.classList.remove('theme-dark', 'theme-black', 'theme-light');
                        document.body.classList.add('theme-' + theme);
                        document.body.dataset.accent = accent;
                    }""", [theme, accent])
                    brand_link = page.locator('.sidebar-brand a')
                    for state in ('normal', 'hover', 'focus'):
                        page.mouse.move(500, 500)
                        brand_link.evaluate('(el) => el.blur()')
                        if state == 'hover':
                            brand_link.hover()
                        elif state == 'focus':
                            brand_link.focus()
                        check(page.locator('.sidebar-brand .brand-mark path').evaluate('(el) => getComputedStyle(el).stroke') == 'rgb(255, 255, 255)',
                              f'Product logo stays white: {theme}/{accent}/{state}')
                    if accent == 'lime':
                        brand_link.evaluate('(el) => el.blur()')
                        page.locator('.sidebar-brand').screenshot(path=str(artifacts / f'white-logo-{theme}.png'))
            page.evaluate("""([classes, accent]) => {
                document.body.className = classes;
                if (accent === null) delete document.body.dataset.accent;
                else document.body.dataset.accent = accent;
            }""", [original_brand_theme, original_brand_accent])
            logo = fixture.data / 'branding' / 'logo.png'
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'app' / 'static' / 'bytedev_logo.png', logo)
            page.reload()
            check(page.locator('.sidebar-brand .brand-mark').count() == 1, 'Legacy logo cannot replace panel branding')
            check(page.locator('.sidebar-brand a').get_attribute('href') == '/overview', 'Brand home link')
            check(page.locator('.sidebar-brand .brand-logo').count() == 0, 'Panel does not use uploaded server icon')
            page.screenshot(path=str(artifacts / 'backups-custom-logo.png'))
            check(active_backup.read_bytes() == active_bytes and other_backup.read_bytes() == other_bytes, 'Existing backup bytes unchanged')
            # Exercise real rename requests on disposable archives, never live backups.
            rename_requests = []
            page.on('request', lambda req: rename_requests.append(req) if req.url.endswith('/api/backup/rename') else None)
            for width, key in ((1440, None), (390, 'Enter')):
                page.set_viewport_size({'width': width, 'height': 1000 if width == 1440 else 844})
                row = page.locator('.roster-row').filter(has_text=active_backup.name)
                row.get_by_role('button', name='Rename', exact=True).click()
                entry = page.get_by_role('textbox', name='Backup name', exact=True)
                check(entry.input_value() == active_backup.stem, 'Rename pre-fills the current archive name')
                check(entry.evaluate('(el) => el === document.activeElement'), 'Rename input receives focus')
                new_base = "Builder's & friends " + str(width)
                entry.fill(new_base)
                page.screenshot(path=str(artifacts / f'backup-rename-{width}.png'))
                with page.expect_response('**/api/backup/rename') as response:
                    if key:
                        entry.press(key)
                    else:
                        page.get_by_role('button', name='Save', exact=True).click()
                check(response.value.json()['status'] == 'success', 'Rename endpoint reports success')
                renamed_backup = active_backup.with_name(new_base + '.zip')
                page.wait_for_function("name => document.getElementById('backups-container').textContent.includes(name)", arg=renamed_backup.name)
                check(page.locator('.modal-overlay').count() == 0, 'Successful rename closes the dialog')
                check(not active_backup.exists(), 'Successful rename removes the old filename')
                check(renamed_backup.read_bytes() == active_bytes, 'Rename preserves every archive byte')
                check(other_backup.read_bytes() == other_bytes, 'Rename leaves other server archives untouched')
                active_backup = renamed_backup
                count_before_cancel = len(rename_requests)
                for action in ('cancel', 'escape', 'unchanged', 'blank'):
                    page.locator('.roster-row').filter(has_text=active_backup.name).get_by_role('button', name='Rename', exact=True).click()
                    check(entry.input_value() == new_base, 'Renamed punctuation survives reopening the dialog')
                    if action == 'cancel':
                        entry.fill('Do not save')
                        page.get_by_role('button', name='Cancel', exact=True).click()
                    elif action == 'escape':
                        entry.press('Escape')
                    else:
                        if action == 'blank':
                            entry.fill('   ')
                        page.get_by_role('button', name='Save', exact=True).click()
                    page.wait_for_function("!document.querySelector('.modal-overlay')")
                check(len(rename_requests) == count_before_cancel, 'Cancelled, blank and unchanged names do not submit')
            # An existing filename shows a dismissible error and never overwrites data.
            collision = active_backup.with_name('Existing backup.zip')
            collision.write_bytes(b'disposable existing archive')
            page.locator('.roster-row').filter(has_text=active_backup.name).get_by_role('button', name='Rename', exact=True).click()
            page.get_by_role('textbox', name='Backup name', exact=True).fill(collision.stem)
            page.get_by_role('button', name='Save', exact=True).click()
            page.wait_for_function("document.getElementById('modal-msg-text')?.textContent.includes('already exists')")
            check(page.locator('.modal-overlay').count() == 1, 'Only the error dialog remains after rejected rename')
            page.get_by_role('button', name='Okay', exact=True).click()
            check(active_backup.read_bytes() == active_bytes, 'Rejected rename preserves source archive')
            check(collision.read_bytes() == b'disposable existing archive', 'Rejected rename preserves destination archive')
            page.set_viewport_size({'width':1440,'height':1000})
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
            check(context.request.get(base + '/branding-assets/server-icon.png').body() == canonical, 'Panel serves exact Minecraft icon')
            check(page.locator('.sidebar-brand .brand-mark').count() == 1, 'Panel brand unchanged after server icon upload')
            check(page.locator('.sidebar-server .server-icon img').evaluate('(el) => el.naturalWidth') == 64, 'Server badge uses converted icon')
            page.screenshot(path=str(artifacts / 'server-icon-general.png'))
            # Offline HTML preview: resolve the email's CID just as a mail client would.
            # Use the repository banner for remote artwork; no external image requests.
            email_context = browser.new_context(offline=True, reduced_motion='reduce')
            email_page = email_context.new_page()
            artwork = 'data:image/jpeg;base64,' + base64.b64encode((Path(__file__).resolve().parents[1] / 'BP.jpg').read_bytes()).decode()
            icon_source = 'data:image/png;base64,' + base64.b64encode(canonical).decode()
            try:
                for include_icon in (True, False):
                    email_html = load_email_design(WELCOME_DESIGN, dict(
                        servername='Obsidian Realm', username='QAPlayer',
                        login_url='https://panel.example.com/login', server_icon='cid:preview@byte-panel.local' if include_icon else '',
                        panel_name='Byte Panel', password='', temporary_password='', expires='2030-01-01'))
                    email_html = email_html.replace('cid:preview@byte-panel.local', icon_source)
                    for remote in ('https://imgur.com/z8evHHi.jpg', 'https://imgur.com/k0XccP9.jpg'):
                        email_html = email_html.replace(remote, artwork)
                    for width in (640, 390):
                        email_page.set_viewport_size({'width': width, 'height': 1100})
                        email_page.set_content(email_html)
                        email_page.locator('img').evaluate_all('(images) => Promise.all(images.map(img => img.decode()))')
                        icon = email_page.locator('img[src^="data:image/png"]')
                        check(icon.count() == int(include_icon), 'Welcome email omits missing icon')
                        if include_icon:
                            icon_box = icon.bounding_box()
                            heading = email_page.get_by_role('heading', name='Welcome to Obsidian Realm').bounding_box()
                            check(icon_box['width'] == 64 and icon_box['height'] == 64, 'Email icon is 64 by 64')
                            check(icon_box['y'] + icon_box['height'] <= heading['y'], 'Email icon appears above welcome heading')
                            check(abs(icon_box['x'] + 32 - width / 2) < 1, 'Email icon centered')
                        check(not email_page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Welcome email fits viewport')
                        email_page.screenshot(path=str(artifacts / f'email-welcome-{int(include_icon)}-{width}.png'), full_page=True)
            finally:
                email_context.close()
            page.set_viewport_size({'width':390,'height':844})
            check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Server icon setting fits mobile')
            page.screenshot(path=str(artifacts / 'server-icon-mobile.png'))
            page.set_viewport_size({'width':1440,'height':1000})
            # Automation is opt-in, independent, and never launches a process on save.
            page.locator('[data-settings-tab=runtime]').click()
            check(not page.locator('#autoStart').is_checked(), 'Automatic startup defaults off')
            check(not page.locator('#autoRestart').is_checked(), 'Crash recovery defaults off')
            page.locator('#autoStart').check()
            page.locator('#autoRestart').check()
            page.locator('#settingsForm button[type=submit]').click()
            page.wait_for_load_state()
            page.locator('[data-settings-tab=runtime]').click()
            check(page.locator('#autoStart').is_checked() and page.locator('#autoRestart').is_checked(), 'Automation settings persist')
            check(fixture.module.MINECRAFT_PROCESS is None, 'Saving automation does not launch Minecraft')
            page.screenshot(path=str(artifacts / 'server-automation-desktop.png'))
            page.set_viewport_size({'width':390,'height':844})
            check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Automation settings fit mobile')
            page.screenshot(path=str(artifacts / 'server-automation-mobile.png'))
            page.locator('#autoStart').uncheck()
            page.locator('#autoRestart').uncheck()
            page.locator('#settingsForm button[type=submit]').click()
            page.wait_for_load_state()
            check(fixture.module.load_config()['auto_start'] is False, 'Automatic startup can be disabled')
            check(fixture.module.load_config()['auto_restart'] is False, 'Crash recovery can be disabled')
            page.set_viewport_size({'width':1440,'height':1000})
            # Persist offline-backup mode through its actual settings control.
            page.locator('[data-settings-tab=backups]').click()
            check(not page.locator('#backupShutdown').is_checked(), 'Shutdown backups are opt-in')
            page.locator('#backupShutdown').check()
            page.locator('#settingsForm button[type=submit]').click()
            page.wait_for_load_state()
            page.locator('[data-settings-tab=backups]').click()
            check(page.locator('#backupShutdown').is_checked(), 'Shutdown backup option persists')
            check(fixture.module.load_config()['backup_shutdown'] is True, 'Backend saved shutdown backup mode')
            page.screenshot(path=str(artifacts / 'shutdown-backups-desktop.png'))
            page.set_viewport_size({'width':390,'height':844})
            check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Shutdown backup setting fits mobile')
            page.screenshot(path=str(artifacts / 'shutdown-backups-mobile.png'))
            page.locator('#backupShutdown').uncheck()
            page.locator('#settingsForm button[type=submit]').click()
            page.wait_for_load_state()
            check(fixture.module.load_config()['backup_shutdown'] is False, 'Shutdown backup option can be disabled')
            page.set_viewport_size({'width':1440,'height':1000})
            page.locator('[data-settings-tab=general]').click()
            check(page.locator('#settings-general #bluemapEnabled').count() == 0, 'BlueMap is no longer in General')
            check(page.locator('#settings-addons #bluemapEnabled').count() == 1, 'BlueMap controls belong to Addons')
            page.locator('[data-settings-tab=backups]').focus()
            page.locator('[data-settings-tab=backups]').press('ArrowRight')
            check(page.locator('#settings-addons').is_visible(), 'Keyboard navigation opens Addons')
            check(page.locator('[data-settings-tab=addons]').get_attribute('aria-selected') == 'true', 'Addons tab is selected accessibly')
            check(page.locator('#settingsForm button[type=submit]').is_visible(), 'Addons uses the normal Save Settings button')
            # Invalid controls in a hidden Addons panel must reveal their tab, not submit.
            saved_map_port = fixture.module.load_config()['bluemap_port']
            page.locator('#bluemapPort').fill('80')
            page.locator('[data-settings-tab=general]').click()
            page.locator('#settingsForm button[type=submit]').click()
            check(page.locator('#settings-addons').is_visible(), 'Validation reveals Addons when its port is invalid')
            check(fixture.module.load_config()['bluemap_port'] == saved_map_port, 'Invalid Addons port does not change saved settings')
            page.locator('#btnRevertChanges').click()
            check(page.locator('#bluemapPort').input_value() == str(saved_map_port), 'Revert restores saved Addons settings')
            # Enable BlueMap using its disposable loopback upstream; map is public.
            page.locator('#bluemapPort').fill(str(MapFixture.upstream.server_port))
            page.locator('#bluemapEnabled').check()
            page.locator('#settingsForm button[type=submit]').click()
            page.wait_for_load_state()
            check(page.locator('#settings-addons').is_visible(), 'Addons selection persists after saving')
            check(page.locator('#bluemapEnabled').is_checked(), 'BlueMap enable setting persists in Addons')
            check(page.locator('#bluemapPort').input_value() == str(MapFixture.upstream.server_port), 'BlueMap port persists in Addons')
            check(page.locator('nav a[href="/map/"]').count() == 1, 'Map navigation appears after enabling')
            public_map_context = browser.new_context()
            map_page = public_map_context.new_page()
            map_page.goto(base + '/map?world=world')
            map_page.wait_for_function('window.mapLoaded === true')
            check('/map/?world=world' in map_page.url, 'Public map canonical prefix and query')
            check(public_map_context.request.get(base + '/api/stats').status == 401, 'Public map grants no panel permissions')
            check(map_page.evaluate("fetch('maps/world/live/players.json').then(r => r.json()).then(data => Array.isArray(data.players))"), 'Relative BlueMap live-data URL works')
            public_map_context.close()
            page.screenshot(path=str(artifacts / 'bluemap-settings.png'))
            page.set_viewport_size({'width':390,'height':844})
            check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), 'Addons settings fit mobile')
            page.screenshot(path=str(artifacts / 'addons-settings-mobile.png'))
            page.set_viewport_size({'width':1440,'height':1000})
            # Mocked-only updater workflow: identify, explicitly approve, opt in,
            # save, apply, then poll completion without touching installed JARs.
            check(not page.locator('#addon-updates-enabled').is_checked(), 'Automatic add-on updates default off')
            page.wait_for_function("!document.getElementById('addon-updates-check').disabled")
            with page.expect_response('**/api/addon-updates/check'):
                page.locator('#addon-updates-check').click()
            page.wait_for_function("document.querySelector('#addon-updates-rows input[data-project=qa-project]') !== null")
            check(page.locator('#addon-updates-rows strong').inner_text() == update_filename,
                  'Updater renders untrusted filenames as literal text')
            check(page.locator('#addon-updates-rows svg, #addon-updates-rows img').count() == 0,
                  'Updater filenames and descriptions cannot inject markup')
            check(page.evaluate('window.qaAddonXss === undefined'), 'Updater metadata cannot execute scripts')
            approval = page.locator('#addon-updates-rows input[data-project="qa-project"]')
            check(not approval.is_checked(), 'Verified project still requires explicit approval')
            approval.check()
            page.locator('#addon-updates-enabled').check()
            check(page.locator('#addon-updates-check').is_disabled() and page.locator('#addon-updates-apply').is_disabled(),
                  'Unsaved updater choices block check and apply')
            with page.expect_response(lambda response: response.url.endswith('/api/addon-updates') and response.request.method == 'PUT'):
                page.locator('#addon-updates-save').click()
            page.wait_for_function("!document.getElementById('addon-updates-apply').disabled")
            check(update_state['enabled'] and update_state['projects'] == ['qa-project'], 'Updater saves only the approved project')
            with page.expect_response('**/api/addon-updates/apply'):
                page.locator('#addon-updates-apply').click()
            page.wait_for_function("document.getElementById('addon-updates-message').textContent === 'Mock update completed.'")
            check(any(method == 'POST' and url.endswith('/apply') for method, url in update_requests),
                  'Updater apply uses the mocked worker endpoint')
            check('2.0' in page.locator('#addon-updates-rows').inner_text(), 'Updater polling displays completed version')
            for width, height in ((1440, 1000), (390, 844)):
                page.set_viewport_size({'width': width, 'height': height})
                check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'Updater controls fit at {width}')
                page.locator('#addon-updates-apply').scroll_into_view_if_needed()
                page.screenshot(path=str(artifacts / f'addon-updates-{width}.png'))
            page.set_viewport_size({'width':1440,'height':1000})
            # Exercise all Settings sections and persist every accent using the real form.
            for accent in ('lime','yellow','blue'):
                page.goto(base + '/settings')
                for key in ('general','runtime','backups','addons','appearance','email','operations'):
                    page.locator('[data-settings-tab=' + key + ']').click()
                    check(page.locator('#settings-' + key).is_visible(), 'Settings panel ' + key)
                page.locator('[data-settings-tab=appearance]').click()
                page.locator('#accentSelect').select_option(accent)
                page.locator('#settingsForm button[type=submit]').click()
                page.wait_for_load_state()
                check(fixture.module.load_config()['accent'] == accent, 'Saved accent ' + accent)
                check(fixture.module.load_config()['bluemap_enabled'] is True, 'Saving another section preserves BlueMap enablement')
                check(fixture.module.load_config()['bluemap_port'] == MapFixture.upstream.server_port, 'Saving another section preserves BlueMap port')
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
            # Anonymous sign-in identity follows the current server, including long names.
            login_context = browser.new_context(reduced_motion='reduce')
            login_page = login_context.new_page()
            login_page.on('pageerror', lambda error: errors.append(str(error)))
            saved_config = fixture.module.load_config()
            try:
                for name, suffix in [('Renamed QA Realm', 'server-icon'), ('R' * 80, 'long-name')]:
                    fixture.module.save_config({**saved_config, 'server_name': name})
                    for width, height in [(1440, 1000), (390, 844)]:
                        login_page.set_viewport_size({'width': width, 'height': height})
                        login_page.goto(base + '/login')
                        login_page.wait_for_function("document.querySelector('.login-server-icon img').naturalWidth === 64")
                        login_page.locator('.login-server-icon img').evaluate('(img) => img.decode()')
                        identity = login_page.locator('.login-card .login-server-identity')
                        check(identity.locator('h1').inner_text() == name, 'Sign-in identity follows current server name')
                        check(identity.locator('img').get_attribute('src') == '/branding-assets/server-icon.png', 'Sign-in uses Minecraft server icon')
                        icon_box = identity.locator('img').bounding_box()
                        check(icon_box['width'] == 40 and icon_box['height'] == 40, 'Sign-in icon keeps its visible size with long names')
                        check(login_page.locator('.login-brand').count() == 0, 'No branding row above sign-in card')
                        check(login_page.get_by_role('heading', name='Welcome back', exact=True).count() == 0, 'Server identity replaces Welcome back')
                        check(not login_page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'Sign-in {suffix} fits at {width}')
                        check(identity.bounding_box()['width'] <= login_page.locator('.login-card').bounding_box()['width'], 'Server identity fits login card')
                        login_page.screenshot(path=str(artifacts / f'login-{suffix}-{width}.png'))
            finally:
                fixture.module.save_config(saved_config)
                login_context.close()
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
            # Files operations use only a disposable subfolder. Never operate on
            # Minecraft runtime files or the real data volume during browser QA.
            page.goto(base + '/files')
            check(page.locator('nav a[href="/files"]').count() == 1, 'Files appears in the owner navigation')
            page.once('dialog', lambda dialog: dialog.accept('qa-files'))
            with page.expect_response('**/api/files/mkdir') as response:
                page.locator('#files-new-folder').click()
            check(response.value.ok, 'New folder request succeeds')
            qa_folder = fixture.root / 'qa-files'
            page.get_by_role('button', name='qa-files', exact=True).wait_for()
            check(qa_folder.is_dir(), 'New folder created in disposable server root')
            check(page.locator('#files-list tr[data-path="qa-files"] .folder-icon').count() == 1,
                  'File browser labels folders with a folder icon')
            page.get_by_role('button', name='qa-files', exact=True).click()
            page.wait_for_url('**/files?path=qa-files')
            page.once('dialog', lambda dialog: dialog.accept('notes.txt'))
            with page.expect_response('**/api/files/create') as response:
                page.locator('#files-new-file').click()
            check(response.value.ok, 'New text file request succeeds')
            page.get_by_role('button', name='notes.txt', exact=True).click()
            page.locator('#files-content').wait_for(state='visible')
            page.wait_for_function("!document.getElementById('files-content').disabled")
            file_content = 'temporary: true\n' * 160 + 'search-target: preserved\n'
            page.locator('#files-content').fill(file_content)
            with page.expect_response(lambda response: response.url.endswith('/api/files/file') and response.request.method == 'PUT') as response:
                page.locator('#files-save').click()
            check(response.value.ok, 'File editor saves through the real API')
            page.wait_for_function("document.getElementById('files-editor-state').textContent === 'Saved'")
            check((qa_folder / 'notes.txt').read_text(encoding='utf-8') == file_content,
                  'Text editing changes only the disposable file')
            page.locator('#files-search').fill('search-target')
            page.locator('#files-search-next').click()
            check('1 of 1' in page.locator('#files-search-status').inner_text(), 'Files editor searches long text')
            for width, height in ((1440, 1000), (390, 844)):
                page.set_viewport_size({'width': width, 'height': height})
                check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'),
                      f'Files editor fits at {width}')
                page.locator('section[aria-label="File editor"]').screenshot(path=str(artifacts / f'files-editor-{width}.png'))
            page.set_viewport_size({'width':1440,'height':1000})
            page.locator('#files-content').fill('unsaved: true\n')
            page.once('dialog', lambda dialog: dialog.dismiss())
            page.locator('#files-close').click()
            check(page.locator('#files-content').input_value() == 'unsaved: true\n',
                  'Files dirty-close confirmation can cancel')
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#files-close').click()
            check(page.locator('#files-content').is_disabled(), 'Files close resets the editor')
            uploaded_content = b'{"preset":"disposable QA data"}\n'
            page.locator('#files-upload').set_input_files({
                'name':'preset.json', 'mimeType':'application/json', 'buffer':uploaded_content})
            with page.expect_response('**/api/files/upload') as response:
                page.locator('#files-upload-button').click()
            check(response.value.ok, 'Files upload succeeds')
            page.wait_for_function("document.getElementById('files-message').textContent.includes('1 file(s) uploaded')")
            check((qa_folder / 'preset.json').read_bytes() == uploaded_content, 'Upload uses the open subfolder')
            page.locator('#files-filter').fill('preset')
            check(page.locator('#files-list tr').count() == 1, 'Files filename filter narrows the current directory')
            page.locator('#files-filter').fill('')
            page.once('dialog', lambda dialog: dialog.accept('qa-files/renamed notes.txt'))
            with page.expect_response('**/api/files/move') as response:
                page.locator('#files-list tr[data-path="qa-files/notes.txt"]').get_by_role('button', name='Rename / move', exact=True).click()
            check(response.value.ok, 'Files rename succeeds')
            page.locator('#files-list tr[data-path="qa-files/renamed notes.txt"]').wait_for()
            check(not (qa_folder / 'notes.txt').exists(), 'Rename removes the old disposable path')
            check((qa_folder / 'renamed notes.txt').read_text(encoding='utf-8') == file_content,
                  'Rename preserves the file contents')
            page.once('dialog', lambda dialog: dialog.accept('wrong-path'))
            page.locator('#files-list tr[data-path="qa-files/preset.json"]').get_by_role('button', name='Delete', exact=True).click()
            page.wait_for_function("document.getElementById('files-message').textContent.includes('Nothing was deleted')")
            check((qa_folder / 'preset.json').exists(), 'Incorrect deletion confirmation preserves the file')
            page.once('dialog', lambda dialog: dialog.accept('qa-files/preset.json'))
            with page.expect_response(lambda response: response.url.endswith('/api/files/entry') and response.request.method == 'DELETE') as response:
                page.locator('#files-list tr[data-path="qa-files/preset.json"]').get_by_role('button', name='Delete', exact=True).click()
            deleted = response.value.json()
            check(response.value.ok and not (qa_folder / 'preset.json').exists(), 'Confirmed file deletion succeeds')
            check((fixture.data / 'file-manager-trash' / deleted['recovery_id'] / 'content').read_bytes() == uploaded_content,
                  'Deleted file remains recoverable outside the server folder')
            page.locator('#files-breadcrumbs').get_by_role('button', name='server/', exact=True).click()
            page.wait_for_url('**/files')
            page.once('dialog', lambda dialog: dialog.accept('qa-files'))
            with page.expect_response(lambda response: response.url.endswith('/api/files/entry') and response.request.method == 'DELETE') as response:
                page.locator('#files-list tr[data-path="qa-files"]').get_by_role('button', name='Delete', exact=True).click()
            deleted_folder = response.value.json()
            check(response.value.ok and not qa_folder.exists(), 'Confirmed folder deletion succeeds')
            recovered_folder = fixture.data / 'file-manager-trash' / deleted_folder['recovery_id'] / 'content'
            check((recovered_folder / 'renamed notes.txt').read_text(encoding='utf-8') == file_content,
                  'Deleted folder contents remain recoverable')

            # Modded engines reuse the permission-gated add-on tools but never
            # display or write the Paper/Purpur plugins folder.
            saved_engine_config = fixture.module.load_config()
            mod_config = fixture.root / 'config'
            mod_config.mkdir(exist_ok=True)
            (mod_config / 'qa-mod.toml').write_text('qa = true\n', encoding='utf-8')
            try:
                for engine, descriptor, metadata in (
                        ('Forge', 'META-INF/mods.toml', 'modLoader="javafml"\n'),
                        ('Fabric', 'fabric.mod.json', '{"schemaVersion":1,"id":"qa","version":"1"}')):
                    fixture.module.save_config({**saved_engine_config, 'server_type': engine})
                    page.goto(base + '/mods')
                    check(page.title() == 'Mods - Byte Panel', engine + ' page uses the Mods title')
                    check(page.locator('nav a[href="/mods"]').inner_text() == 'Mods', engine + ' navigation uses Mods')
                    check(page.locator('nav a[href="/plugins"]').count() == 0, engine + ' hides Plugins navigation')
                    check(page.locator('#plugin-manager').get_attribute('data-root-name') == 'mods', engine + ' manager uses mods root')
                    check(page.get_by_role('button', name='LuckPerms', exact=True).count() == 0,
                          engine + ' does not display plugin folders')
                    jar = io.BytesIO()
                    with zipfile.ZipFile(jar, 'w') as archive:
                        archive.writestr(descriptor, metadata)
                    jar_name = 'qa-' + engine.lower() + '.jar'
                    page.locator('#plugin-upload').set_input_files({
                        'name':jar_name, 'mimeType':'application/java-archive', 'buffer':jar.getvalue()})
                    with page.expect_response('**/api/plugins/upload') as response:
                        page.locator('#upload-button').click()
                    check(response.value.ok, engine + ' accepts its own mod descriptor')
                    page.get_by_role('button', name='Delete ' + jar_name, exact=True).wait_for()
                    check((fixture.root / 'mods' / jar_name).read_bytes() == jar.getvalue(), engine + ' JAR stored in mods only')
                    check(not (fixture.root / 'plugins' / jar_name).exists(), engine + ' never writes plugin JAR path')
                    for width, height in ((1440, 1000), (390, 844)):
                        page.set_viewport_size({'width': width, 'height': height})
                        check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), engine + f' Mods fits at {width}')
                        page.screenshot(path=str(artifacts / f'mods-{engine.lower()}-{width}.png'))
                        page.locator('.plugins-browser').screenshot(path=str(artifacts / f'mods-{engine.lower()}-files-{width}.png'))
                    page.set_viewport_size({'width':1440,'height':1000})
                    page.get_by_role('link', name='Open mod configs in Files', exact=True).click()
                    page.wait_for_url('**/files?path=config')
                    page.get_by_role('button', name='qa-mod.toml', exact=True).wait_for()
                    check(page.locator('#files-breadcrumbs').inner_text().endswith('config'), engine + ' config link opens separate config folder')
                    page.goto(base + '/mods')
                    page.once('dialog', lambda dialog: dialog.accept())
                    with page.expect_response(lambda response: response.url.endswith('/api/plugins/delete') and response.request.method == 'DELETE') as response:
                        page.get_by_role('button', name='Delete ' + jar_name, exact=True).click()
                    deleted_mod = response.value.json()
                    check(response.value.ok and not (fixture.root / 'mods' / jar_name).exists(), engine + ' mod deletion succeeds')
                    check((fixture.data / 'mod-config-backups' / deleted_mod['backup']).read_bytes() == jar.getvalue(),
                          engine + ' deleted mod has a separate recovery backup')
                    check((mod_config / 'qa-mod.toml').read_text(encoding='utf-8') == 'qa = true\n', engine + ' deletion preserves mod configs')
            finally:
                fixture.module.save_config(saved_engine_config)
            page.goto(base + '/plugins')
            check(page.get_by_role('button', name='LuckPerms', exact=True).count() == 1,
                  'Restoring Purpur shows the unchanged plugin directory')
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
                check_setup_build_progress(first_run, base, check)
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
        MapFixture.tearDownClass()
        fixture.doCleanups()
        Fixture.tearDownClass()


if __name__ == '__main__':
    raise SystemExit(main())

