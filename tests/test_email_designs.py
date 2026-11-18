"""JSON design rendering and invitation privacy; no network or real passwords."""
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
from io import StringIO
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from email_designs import EmailTemplateError, load_email_design, render_email_design
from panel_mail import (DEFAULT_SMTP, PanelMailer, WELCOME_DESIGN, PASSWORD_DESIGN,
                        smtp_failure)


class Tags(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class EmailDesignTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(username='player', servername='Byte & Friends',
                           panel_name='Byte & Friends', login_url='https://panel.example.com/login',
                           password='A9<&"secret!', temporary_password='A9<&"secret!', expires='2033-05-18')
        self.design = {
            'root': {'type': 'EmailLayout', 'data': {'childrenIds': ['text']}},
            'text': {'type': 'Text', 'data': {'props': {'text': '{{servername}}\n{{password}}'}}}}
        self.store = Mock()
        self.store.get_setting.return_value = {**DEFAULT_SMTP, 'enabled': True,
                                               'panel_url': 'https://panel.example.com'}
        self.store.invitation_is_current.return_value = True
        self.mailer = PanelMailer(self.store, lambda: {'panel_name': self.values['servername']})
        self.user = dict(username='player', email='player@example.com',
                         temporary_password_expires=2000000000)
        delay = patch('panel_mail.sleep')
        self.delay = delay.start()
        self.addCleanup(delay.stop)

    def welcome_icon_design(self):
        design = json.loads(WELCOME_DESIGN.read_text(encoding='utf-8-sig'))
        icon_ids = [block_id for block_id in design['root']['data']['childrenIds']
                    if design[block_id]['type'] == 'Image' and re.fullmatch(
                        r'\{\{\s*server_icon\s*\}\}',
                        design[block_id]['data']['props']['url'].strip())]
        self.assertEqual(len(icon_ids), 1)
        return design, icon_ids[0]

    def test_supplied_designs_render_order_styles_artwork_unicode_and_tokens(self):
        welcome = load_email_design(WELCOME_DESIGN, {**self.values, 'password': '', 'temporary_password': ''})
        password = load_email_design(PASSWORD_DESIGN, self.values)
        self.assertLess(welcome.index('Hi player'), welcome.index('Welcome to Byte &amp; Friends'))
        self.assertLess(welcome.index('Your login details:'), welcome.index('Username: player'))
        self.assertIn('\U0001f44b', welcome)
        self.assertIn('you\u2019ll', welcome)
        self.assertIn('background-color:#000000', welcome)
        self.assertIn('background-color:#0284C7', welcome)
        self.assertIn('padding:0px 24px 16px 24px', welcome)
        self.assertNotIn('{{', welcome + password)
        self.assertIn('Courier New', password)
        self.assertIn('<h1', password)
        self.assertIn('overflow-wrap:anywhere', password)
        self.assertIn('table-layout:fixed', password)
        self.assertIn(escape(self.values['password']), password)
        tags = Tags(welcome).tags
        images = [attrs['src'] for tag, attrs in tags if tag == 'img']
        self.assertEqual(images, ['https://imgur.com/z8evHHi.jpg', 'https://imgur.com/k0XccP9.jpg'])
        links = [attrs['href'] for tag, attrs in tags if tag == 'a']
        design, _ = self.welcome_icon_design()
        expected_links = []
        for block_id in design['root']['data']['childrenIds']:
            block = design[block_id]
            props = block['data'].get('props', {})
            link = (props.get('linkHref') if block['type'] == 'Image'
                    else props.get('url') if block['type'] == 'Button' else None)
            if link:
                expected_links.append(re.sub(r'\{\{\s*login_url\s*\}\}',
                                             self.values['login_url'], link))
        self.assertEqual(len(links), 3)
        self.assertEqual(links, expected_links)
        self.assertIn('https://github.com/NotSpookyGhost/Byte-Panel', links)
        self.assertIn('- Panel: ' + self.values['login_url'], ''.join(Tags(welcome).text))

    def test_text_and_password_are_escaped_and_replaced_only_once(self):
        self.values['servername'] = '{{password}} <img src=x onerror=alert(1)>'
        result = render_email_design(self.design, self.values)
        self.assertIn('{{password}} &lt;img', result)
        self.assertIn(escape(self.values['password']), result)
        self.assertNotIn('<img', result)
        self.assertIn('<br>', result)
        self.assertIn(self.values['password'], ''.join(Tags(result).text))

    def test_welcome_server_icon_is_centered_before_heading(self):
        design, icon_id = self.welcome_icon_design()
        source = 'cid:abc.123_test-456@byte-panel.local'
        welcome = render_email_design(design, {**self.values, 'server_icon': source})
        self.assertLess(welcome.index('Hi player'), welcome.index(source))
        self.assertLess(welcome.index(source), welcome.index('Welcome to '))
        tags = Tags(welcome).tags
        images = [attrs for tag, attrs in tags if tag == 'img']
        self.assertEqual([image['src'] for image in images], [
            'https://imgur.com/z8evHHi.jpg', source, 'https://imgur.com/k0XccP9.jpg'])
        icon = images[1]
        for dimension in ('width', 'height'):
            configured = design[icon_id]['data']['props'].get(dimension)
            if configured is None:
                self.assertNotIn(dimension, icon)
            else:
                self.assertEqual(icon[dimension], f'{configured:g}')
                self.assertIn(f'{dimension}:{configured:g}px', icon['style'])
        icon_index = next(index for index, (tag, attrs) in enumerate(tags)
                          if tag == 'img' and attrs['src'] == source)
        self.assertEqual(tags[icon_index - 1][0], 'td')
        self.assertIn('text-align:center', tags[icon_index - 1][1]['style'])

    def test_server_icon_dimensions_and_dynamic_alt_are_rendered_safely(self):
        design, icon_id = self.welcome_icon_design()
        design[icon_id]['data']['props'].update(
            width=64, height=64, alt='{{servername}} server icon')
        name = 'Byte <Friends> "& Team"'
        source = 'cid:abc.123_test-456@byte-panel.local'
        welcome = render_email_design(design, {
            **self.values, 'servername': name, 'server_icon': source})
        icon = next(attrs for tag, attrs in Tags(welcome).tags
                    if tag == 'img' and attrs['src'] == source)
        self.assertEqual(icon['width'], '64')
        self.assertEqual(icon['height'], '64')
        self.assertEqual(icon['alt'], name + ' server icon')
        self.assertIn('width:64px', icon['style'])
        self.assertIn('height:64px', icon['style'])
        self.assertIn('alt="' + escape(name + ' server icon', quote=True) + '"', welcome)

    def test_configured_login_url_token_is_supported_in_image_and_button_links(self):
        design = deepcopy(self.design)
        design['root']['data']['childrenIds'] = ['image', 'button']
        design['image'] = {'type': 'Image', 'data': {'props': {
            'url': 'https://example.com/banner.png', 'linkHref': ' {{ login_url }} '}}}
        design['button'] = {'type': 'Button', 'data': {'props': {
            'url': '{{login_url}}', 'text': 'Sign in'}}}
        result = render_email_design(design, self.values)
        self.assertEqual([attrs['href'] for tag, attrs in Tags(result).tags if tag == 'a'],
                         [self.values['login_url'], self.values['login_url']])

    def test_missing_server_icon_omits_entire_block_without_empty_spacing(self):
        design, icon_id = self.welcome_icon_design()
        without_icon = deepcopy(design)
        without_icon['root']['data']['childrenIds'].remove(icon_id)
        expected = render_email_design(without_icon, self.values)
        for values in (self.values, {**self.values, 'server_icon': None},
                       {**self.values, 'server_icon': ''}):
            with self.subTest(value=values.get('server_icon')):
                actual = render_email_design(design, values)
                self.assertEqual(actual, expected)
                self.assertEqual(len([tag for tag, attrs in Tags(actual).tags if tag == 'img']), 2)
                self.assertNotIn('{{server_icon}}', actual)

    def test_server_icon_accepts_only_exact_token_and_safe_cid_value(self):
        design = deepcopy(self.design)
        design['text'] = {'type': 'Image', 'data': {'props': {'url': ' {{ server_icon }} '}}}
        source = 'cid:abc.DEF_123-456@byte-panel.local'
        self.assertIn('src="' + source + '"', render_email_design(
            design, {**self.values, 'server_icon': source}))
        for bad in ('https://example.com/icon.png', 'javascript:alert(1)',
                    'data:image/png;base64,a', '{{password}}', '{{username}}',
                    'cid:', 'cid:a/b', 'cid:a%20b', 'cid:a b', 'cid:a\nb',
                    'cid:a" onerror="alert(1)', 'cid:\u00e9', '<cid:icon>',
                    ' CID:icon', ' ', False, 12, [], {}):
            with self.subTest(source_type=type(bad).__name__, source=bad):
                with self.assertRaises(EmailTemplateError):
                    render_email_design(design, {**self.values, 'server_icon': bad})
        for bad in ('cid:literal@byte-panel.local', 'https://example.com/{{server_icon}}',
                    '{{server_icon}}/suffix', '{{server_icon}}{{username}}',
                    '{{username}}', '{{password}}'):
            design['text']['data']['props']['url'] = bad
            with self.subTest(template_url=bad):
                with self.assertRaises(EmailTemplateError):
                    render_email_design(design, {**self.values, 'server_icon': source})

    def test_inline_server_icon_cannot_be_used_as_a_link(self):
        for kind, field in (('Button', 'url'), ('Image', 'linkHref')):
            for bad in ('{{server_icon}}', ' {{ server_icon }} ', 'cid:icon@byte-panel.local'):
                for source in ('cid:valid@byte-panel.local', ''):
                    design = deepcopy(self.design)
                    design['text'] = {'type': kind, 'data': {'props': {
                        'url': '{{server_icon}}', field: bad, 'text': 'Open'}}}
                    with self.subTest(kind=kind, link=bad, icon=source):
                        with self.assertRaises(EmailTemplateError):
                            render_email_design(design, {**self.values, 'server_icon': source})

    def test_unsupported_blocks_markdown_and_invalid_styles_fail_closed(self):
        cases = []
        for kind in ('Html', 'ColumnsContainer', 'Unknown'):
            item = deepcopy(self.design)
            item['text']['type'] = kind
            cases.append(item)
        for style in ({'color': '#fff;url(https://example.com)'},
                      {'padding': {'top': -1}}, {'fontSize': float('nan')},
                      {'fontFamily': 'evil'}, {'textAlign': 'left;position:absolute'}):
            item = deepcopy(self.design)
            item['text']['data']['style'] = style
            cases.append(item)
        item = deepcopy(self.design)
        item['text']['data']['props']['markdown'] = True
        cases.append(item)
        for item in cases:
            with self.subTest(item=item):
                with self.assertRaises(EmailTemplateError):
                    render_email_design(item, self.values)

    def test_invalid_references_cycles_and_expanding_trees_are_bounded(self):
        cases = [{}, [], {'root': None}]
        for ids in (['missing'], ['root'], 'text', [None]):
            item = deepcopy(self.design)
            item['root']['data']['childrenIds'] = ids
            cases.append(item)
        cycle = deepcopy(self.design)
        cycle['text'] = {'type': 'Container', 'data': {'props': {'childrenIds': ['text']}}}
        cases.append(cycle)
        # Reusing child IDs cannot create exponentially large output.
        expanding = deepcopy(self.design)
        expanding['root']['data']['childrenIds'] = ['container'] * 40
        expanding['container'] = {'type': 'Container', 'data': {'props': {'childrenIds': ['text'] * 40}}}
        cases.append(expanding)
        for item in cases:
            with self.subTest(item=item):
                with self.assertRaises(EmailTemplateError):
                    render_email_design(item, self.values)

    def test_urls_never_interpolate_passwords_or_user_details(self):
        for kind, field in (('Image', 'url'), ('Image', 'linkHref'), ('Button', 'url')):
            for bad in ('javascript:alert(1)', 'data:text/html,test', '//example.com/img',
                        'https://example.com/{{password}}', 'https://example.com/{{temporary_password}}',
                        'https://example.com/{{username}}', 'https://user:pass@example.com',
                        'https://example.com:\n80', 'https://example.com:bad'):
                item = deepcopy(self.design)
                item['text'] = {'type': kind, 'data': {'props': {'url': 'https://example.com/image.jpg',
                                                                 field: bad, 'text': 'Button'}}}
                with self.subTest(kind=kind, field=field, bad=bad):
                    with self.assertRaises(EmailTemplateError) as caught:
                        render_email_design(item, self.values)
                    self.assertNotIn(self.values['password'], str(caught.exception))

    def test_html_privacy_for_both_password_tokens_in_welcome(self):
        custom = deepcopy(self.design)
        custom['text']['data']['props']['text'] = '{{username}} {{password}} {{ temporary_password }}'
        with patch('panel_mail.WELCOME_DESIGN') as path, patch.object(self.mailer, 'send') as send:
            path.open.return_value = StringIO(json.dumps(custom))
            self.mailer.welcome(self.user, self.values['password'])
        first, second = send.call_args_list
        self.assertNotIn(escape(self.values['password']), first.kwargs['html'])
        self.assertNotIn(self.values['password'], first.args[2])
        self.assertIn(escape(self.values['password']), second.kwargs['html'])
        self.assertIn(self.values['password'], second.args[2])
        for call in send.call_args_list:
            self.assertNotIn(self.values['password'], call.args[1])
            for tag, attrs in Tags(call.kwargs['html']).tags:
                for key in ('src', 'href'):
                    self.assertNotIn(self.values['password'], attrs.get(key, ''))
        self.delay.assert_called_once_with(12)

    def test_invalid_or_missing_second_design_prevents_both_messages(self):
        for source in ('{"private source":', '{"root": null}', 'x' * (1024 * 1024 + 1), None):
            with self.subTest(source_type=type(source).__name__):
                with patch('panel_mail.PASSWORD_DESIGN') as path, patch.object(self.mailer, 'send') as send:
                    if source is None:
                        path.open.side_effect = FileNotFoundError('private source')
                    else:
                        path.open.return_value = StringIO(source)
                    with self.assertRaises(EmailTemplateError) as caught:
                        self.mailer.welcome(self.user, self.values['password'])
                    self.assertNotIn('private source', str(caught.exception))
                    diagnostic = smtp_failure(caught.exception)
                    self.assertEqual(diagnostic['code'], 'email_template_invalid')
                    self.assertIn('No message was submitted', diagnostic['message'])
                send.assert_not_called()
        self.delay.assert_not_called()

    def test_template_is_read_as_utf8_and_again_for_each_invitation(self):
        with patch('panel_mail.WELCOME_DESIGN') as path, patch.object(self.mailer, 'send') as send:
            path.open.side_effect = [StringIO(json.dumps(self.design)), StringIO(json.dumps({
                **self.design, 'text': {'type': 'Text', 'data': {'props': {'text': 'Updated design'}}}}))]
            self.mailer.welcome(self.user, 'first-password')
            self.mailer.welcome(self.user, 'second-password')
        self.assertEqual(path.open.call_count, 2)
        path.open.assert_called_with('r', encoding='utf-8-sig')
        self.assertIn('Updated design', send.call_args_list[2].kwargs['html'])
        self.assertNotIn('Updated design', send.call_args_list[0].kwargs['html'])

    def test_docker_copies_the_root_design_directory(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(WELCOME_DESIGN, root / 'templates' / 'welcomeTemplate.json')
        self.assertEqual(PASSWORD_DESIGN, root / 'templates' / 'passwordTemplate.json')
        self.assertIn('COPY templates/ /templates/', (root / 'Dockerfile').read_text())
        self.assertNotIn('templates', (root / '.dockerignore').read_text().splitlines())


if __name__ == '__main__':
    unittest.main()
