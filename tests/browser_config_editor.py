"""Optional real-browser config editor regression checks against disposable data.

Requires Playwright and Chrome; no live Minecraft server or credentials are used.
Run: python tests/browser_config_editor.py --artifacts <screenshot-directory>
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import threading

from flask import abort, request
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler
from test_accounts import AccountIntegrationTests as Fixture


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', default=str(Path(tempfile.gettempdir()) / 'byte-panel-editor-qa'))
    args = parser.parse_args()
    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    Fixture.setUpClass()
    fixture = Fixture()
    fixture.setUp()
    server = None
    checks = []
    errors = []

    def check(condition, message):
        if not condition:
            raise AssertionError(message)
        checks.append(message)

    try:
        source = ('# Example server configuration\n'
                  'enabled: true\nport: 25565\nrate: 1.5\n'
                  'message: "<img src=x onerror=window.editorXss=1>"\n'
                  'unicode: "Minecraft \u2665 \U0001f600"\n'
                  + ''.join('setting-' + str(i) + ': false\n' for i in range(180))
                  + 'long-message: "' + 'wide text ' * 90 + '"\n'
                  'search-target: end-of-file\n')
        roots = [fixture.root / 'plugins' / 'EditorQA', fixture.root / 'config']
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
            (root / 'config.yml').write_bytes(source.replace('\n', '\r\n').encode('utf-8'))
            (root / 'second.json').write_text('{"name": "<script>window.editorXss=1</script>", "count": 42, "enabled": true}\n', encoding='utf-8')
            (root / 'notes.txt').write_text('Unformatted notes <script>window.editorXss=1</script>\n', encoding='utf-8')
            (root / 'large.yml').write_text('message: "' + 'x' * 20000 + '"\n', encoding='utf-8')
        app = fixture.app

        @app.before_request
        def prevent_operational_writes():
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.path not in (
                    '/login', '/api/plugins/file', '/api/files/file'):
                abort(418)

        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get(
                'BYTE_PANEL_TEST_BROWSER', r'C:\Program Files\Google\Chrome\Application\chrome.exe'))
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/login')
            page.locator('[name=username]').fill('owner')
            page.locator('[name=password]').fill('Owner-password-123')
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.wait_for_url('**/overview')

            for kind in ('plugins', 'files'):
                plugin = kind == 'plugins'
                prefix = 'plugin' if plugin else 'files'
                editor_id = '#' + prefix + '-content'
                list_id = '#plugin-file-list' if plugin else '#files-list'
                reload_id = '#reload-config' if plugin else '#files-reload'
                search_id = '#config-search' if plugin else '#files-search'
                next_id = '#search-next' if plugin else '#files-search-next'
                save_id = '#save-config' if plugin else '#files-save'
                endpoint = '/api/plugins/file' if plugin else '/api/files/file'
                root = roots[0] if plugin else roots[1]
                page.goto(base + ('/plugins' if plugin else '/files?path=config'))
                if plugin:
                    page.locator(list_id).get_by_role('button', name='EditorQA', exact=True).click()
                editor = page.locator(editor_id)
                wrapper = page.locator('.panel-config-editor')

                def open_file(name):
                    page.locator(list_id).get_by_role('button', name=name, exact=True).click()
                    page.wait_for_function('(id) => !document.querySelector(id).disabled', arg=editor_id)

                def at_top():
                    return editor.evaluate('(el) => el.selectionStart === 0 && el.selectionEnd === 0 && el.scrollTop === 0 && el.scrollLeft === 0')

                open_file('config.yml')
                check(at_top(), kind + ': long config opens at the beginning')
                check(wrapper.get_attribute('data-language') == 'yaml', kind + ': YAML detected')
                check(wrapper.get_attribute('data-highlighted') == 'true', kind + ': colors enabled')
                check(wrapper.locator('code span').count() > 10, kind + ': tokens rendered')
                check(wrapper.locator('pre').get_attribute('aria-hidden') == 'true', kind + ': decorative code hidden from assistive technology')
                check(wrapper.locator('img, script').count() == 0 and page.evaluate('window.editorXss === undefined'), kind + ': markup remains literal text')
                check(editor.input_value() == source, kind + ': highlighting preserves text')
                metrics = editor.evaluate("""el => {
                    const a = getComputedStyle(el), b = getComputedStyle(el.parentElement.querySelector('pre'));
                    return ['fontFamily','fontSize','lineHeight','paddingTop','paddingLeft','tabSize','whiteSpace']
                        .every(key => a[key] === b[key]);
                }""")
                check(metrics, kind + ': code and input typography/padding match')
                editor.scroll_into_view_if_needed()
                page.screenshot(path=str(artifacts / (kind + '-syntax-desktop.png')))

                page.locator(search_id).fill('search-target')
                page.locator(next_id).click()
                check(editor.evaluate("el => el.value.slice(el.selectionStart, el.selectionEnd) === 'search-target' && el.scrollTop > 0"),
                      kind + ': search selects and scrolls to the last line')
                page.wait_for_function("""(id) => {
                    const el = document.querySelector(id), pre = el.parentElement.querySelector('pre');
                    return pre.scrollTop === el.scrollTop;
                }""", arg=editor_id)
                check(True, kind + ': highlighted layer follows search scrolling')
                editor.evaluate("el => {el.scrollLeft = 120; el.dispatchEvent(new Event('scroll'));}")
                check(editor.evaluate("el => el.parentElement.querySelector('pre').scrollLeft === el.scrollLeft"),
                      kind + ': highlighted layer follows horizontal scrolling')
                page.locator(reload_id).click()
                page.wait_for_function('(id) => !document.querySelector(id).disabled', arg=editor_id)
                check(at_top(), kind + ': reload resets search/caret and both scroll axes')
                check(page.locator(search_id).input_value() == 'search-target', kind + ': reload retains search query without jumping to match')

                open_file('second.json')
                check(at_top() and wrapper.get_attribute('data-language') == 'json', kind + ': switching files resets view and language')
                check(page.evaluate('window.editorXss === undefined'), kind + ': JSON strings cannot execute')
                open_file('config.yml')
                # Native keyboard input and undo remain native textarea behavior.
                editor.focus()
                editor.press('Control+Home')
                editor.press('End')
                editor.press('X')
                editor.press('Control+z')
                check(editor.input_value() == source, kind + ': native typing/undo preserves source')
                edited = source.replace('enabled: true', 'enabled: false')
                editor.fill(edited)
                page.wait_for_function("document.querySelector('.panel-config-editor').dataset.highlighted === 'true'")
                with page.expect_response(lambda response: response.url.endswith(endpoint) and response.request.method == 'PUT'):
                    page.locator(save_id).click()
                page.wait_for_function('(id) => !document.querySelector(id).disabled', arg=editor_id)
                check((root / 'config.yml').read_bytes() == edited.replace('\n', '\r\n').encode('utf-8'), kind + ': save preserves CRLF and exact text')
                editor.fill('unsaved: true\n')
                page.once('dialog', lambda dialog: dialog.dismiss())
                page.locator(reload_id).click()
                check(editor.input_value() == 'unsaved: true\n', kind + ': cancelled reload retains draft')
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator(reload_id).click()
                page.wait_for_function('(id) => !document.querySelector(id).disabled', arg=editor_id)
                check(at_top(), kind + ': confirmed reload opens at top')

                for width in (900, 390):
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.locator(reload_id).click()
                    page.wait_for_function('(id) => !document.querySelector(id).disabled', arg=editor_id)
                    check(at_top(), kind + ': top preserved at width ' + str(width))
                    check(not page.evaluate('document.documentElement.scrollWidth > innerWidth'), kind + ': no page overflow at width ' + str(width))
                    editor.scroll_into_view_if_needed()
                    page.screenshot(path=str(artifacts / (kind + '-syntax-' + str(width) + '.png')))
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.locator('body').evaluate("el => { el.classList.remove('theme-dark', 'theme-black'); el.classList.add('theme-light'); }")
                editor.scroll_into_view_if_needed()
                check(wrapper.locator('.config-token-key').first.evaluate("el => getComputedStyle(el).color") != 'rgb(139, 207, 240)',
                      kind + ': light theme uses a darker, legible token palette')
                page.screenshot(path=str(artifacts / (kind + '-syntax-light.png')))
                check(editor.evaluate("""el => {
                    const background = getComputedStyle(el.parentElement.querySelector('pre')).backgroundColor;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    return getComputedStyle(el).backgroundColor === background;
                }"""), kind + ': typing in light theme does not flash a dark background')
                page.wait_for_function("document.querySelector('.panel-config-editor').dataset.highlighted === 'true'")
                page.emulate_media(forced_colors='active')
                check(editor.evaluate("el => getComputedStyle(el).color !== 'rgba(0, 0, 0, 0)'"), kind + ': high-contrast mode keeps native text visible')
                page.emulate_media(forced_colors='none')
                open_file('notes.txt')
                check(wrapper.get_attribute('data-highlighted') == 'false' and at_top(), kind + ': plain text fallback opens at top')
                open_file('large.yml')
                check(wrapper.get_attribute('data-highlighted') == 'false' and at_top(), kind + ': pathological long-line fallback remains readable/editable')
                check(editor.input_value() == (root / 'large.yml').read_text(encoding='utf-8'), kind + ': fallback does not truncate')

            check(not errors, 'No browser JavaScript errors: ' + repr(errors))
            browser.close()
        print(json.dumps({'checks_passed': len(checks), 'artifacts': str(artifacts)}, indent=2))
        return 0
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()
        Fixture.tearDownClass()


if __name__ == '__main__':
    raise SystemExit(main())
