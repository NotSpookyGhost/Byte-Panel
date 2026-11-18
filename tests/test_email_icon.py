"""Optional invitation icons, using temporary files and mocked email delivery."""
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from panel_mail import DEFAULT_SMTP, MAX_EMAIL_ICON_BYTES, PanelMailer


def png_bytes(color='red', size=(64, 64), metadata=False):
    stream = BytesIO()
    image = Image.new('RGBA', size, color)
    info = PngInfo()
    if metadata:
        info.add_text('Comment', 'PRIVATE-IMPORTED-METADATA')
    image.save(stream, format='PNG', pnginfo=info)
    return stream.getvalue()


class EmailIconTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='byte-panel-email-icon-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server = self.root / 'server'
        self.server.mkdir()
        self.icon_path = self.server / 'server-icon.png'
        self.store = Mock()
        self.store.get_setting.return_value = {
            **DEFAULT_SMTP, 'enabled': True, 'panel_url': 'https://panel.example.com'}
        self.store.invitation_is_current.return_value = True
        self.mailer = PanelMailer(self.store, lambda: {'server_name': 'Test & Friends'}, self.server)
        self.user = {'username': 'player', 'email': 'player@example.com',
                     'temporary_password_expires': 2000000000}
        self.password = 'A9<&temporary!'
        sleep_patch = patch('panel_mail.sleep')
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def invite(self):
        with patch.object(self.mailer, 'send') as send:
            self.assertEqual(self.mailer.welcome(self.user, self.password), 'sent')
        self.assertEqual(send.call_count, 2)
        return send.call_args_list

    def assert_no_icon(self, calls):
        for call in calls:
            self.assertFalse(call.kwargs.get('inline_images'))
            self.assertNotIn('cid:', call.kwargs['html'])
            self.assertNotIn('{{server_icon}}', call.kwargs['html'])

    def test_icon_normalizes_png_to_64_square_and_strips_metadata(self):
        self.icon_path.write_bytes(png_bytes(size=(128, 32), metadata=True))
        normalized = self.mailer.server_icon_bytes()
        self.assertTrue(normalized.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertNotIn(b'PRIVATE-IMPORTED-METADATA', normalized)
        self.assertLess(len(normalized), MAX_EMAIL_ICON_BYTES)
        with Image.open(BytesIO(normalized)) as image:
            self.assertEqual(image.size, (64, 64))
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.info, {})
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((32, 32)), (255, 0, 0, 255))

    def test_only_active_server_icon_is_loaded_not_legacy_branding(self):
        branding = self.root / 'branding'
        branding.mkdir()
        (branding / 'logo.png').write_bytes(png_bytes('blue'))
        (branding / 'server-icon.png').write_bytes(png_bytes('blue'))
        self.assertIsNone(self.mailer.server_icon_bytes())
        self.assert_no_icon(self.invite())
        self.icon_path.write_bytes(png_bytes('red'))
        with Image.open(BytesIO(self.mailer.server_icon_bytes())) as image:
            self.assertEqual(image.getpixel((32, 32)), (255, 0, 0, 255))

    def test_optional_constructor_without_server_directory_has_no_icon(self):
        self.icon_path.write_bytes(png_bytes())
        self.mailer = PanelMailer(self.store)
        self.assertIsNone(self.mailer.server_icon_bytes())
        self.assert_no_icon(self.invite())

    def test_missing_icon_does_not_block_or_delay_invitation_extra(self):
        self.assertIsNone(self.mailer.server_icon_bytes())
        self.assert_no_icon(self.invite())
        self.sleep.assert_called_once_with(12)

    def test_malformed_empty_and_non_png_icons_are_omitted(self):
        jpeg = BytesIO()
        Image.new('RGB', (64, 64), 'red').save(jpeg, format='JPEG')
        for content in (b'', b'not an image', b'\x89PNG\r\n\x1a\ncorrupt', jpeg.getvalue()):
            with self.subTest(content_length=len(content)):
                self.icon_path.write_bytes(content)
                self.assertIsNone(self.mailer.server_icon_bytes())
                self.assert_no_icon(self.invite())

    def test_oversized_icon_is_omitted_without_attempting_to_decode_it(self):
        self.icon_path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * MAX_EMAIL_ICON_BYTES)
        with patch('panel_mail.normalize_icon') as normalize:
            self.assertIsNone(self.mailer.server_icon_bytes())
            self.assert_no_icon(self.invite())
        normalize.assert_not_called()

    def test_icon_read_is_bounded_to_limit_plus_one(self):
        path = Mock()
        stream = Mock()
        stream.read.return_value = png_bytes()
        path.open.return_value.__enter__ = Mock(return_value=stream)
        path.open.return_value.__exit__ = Mock(return_value=False)
        with patch('panel_mail.server_icon_path', return_value=path):
            self.assertIsNotNone(self.mailer.server_icon_bytes())
        path.open.assert_called_once_with('rb')
        stream.read.assert_called_once_with(MAX_EMAIL_ICON_BYTES + 1)

    def test_unreadable_icon_does_not_block_invitation(self):
        path = Mock()
        path.open.side_effect = PermissionError('fixture is unreadable')
        with patch('panel_mail.server_icon_path', return_value=path):
            self.assertIsNone(self.mailer.server_icon_bytes())
            self.assert_no_icon(self.invite())
        with patch('panel_mail.server_icon_path', side_effect=PermissionError('cannot stat fixture')):
            self.assertIsNone(self.mailer.server_icon_bytes())
            self.assert_no_icon(self.invite())

    def test_symlink_and_directory_icons_are_omitted(self):
        self.icon_path.write_bytes(png_bytes())
        # Mock the symlink predicate so this runs without Windows symlink privileges.
        with patch.object(Path, 'is_symlink', autospec=True,
                          side_effect=lambda path: path == self.icon_path):
            self.assertIsNone(self.mailer.server_icon_bytes())
            self.assert_no_icon(self.invite())
        self.icon_path.unlink()
        self.icon_path.mkdir()
        self.assertIsNone(self.mailer.server_icon_bytes())
        self.assert_no_icon(self.invite())

    def test_only_welcome_attaches_icon_and_password_message_stays_separate(self):
        self.icon_path.write_bytes(png_bytes())
        first, second = self.invite()
        images = first.kwargs['inline_images']
        self.assertEqual(len(images), 1)
        cid, image = next(iter(images.items()))
        self.assertRegex(cid, r'^[A-Za-z0-9._@-]+@byte-panel\.local$')
        self.assertEqual(image, self.mailer.server_icon_bytes())
        self.assertIn('src="cid:' + cid + '"', first.kwargs['html'])
        self.assertNotIn('cid:', first.args[2])
        self.assertNotIn(self.password, first.args[2])
        self.assertIn(self.password, second.args[2])
        self.assertFalse(second.kwargs.get('inline_images'))
        self.assertNotIn('cid:', second.kwargs['html'])
        self.assertNotIn('cid:', second.args[2])
        self.sleep.assert_called_once_with(12)

    def test_icon_and_cid_are_refreshed_for_each_invitation(self):
        self.icon_path.write_bytes(png_bytes('red'))
        first_image = self.invite()[0].kwargs['inline_images']
        self.icon_path.write_bytes(png_bytes('blue'))
        second_image = self.invite()[0].kwargs['inline_images']
        first_cid, first_data = next(iter(first_image.items()))
        second_cid, second_data = next(iter(second_image.items()))
        self.assertNotEqual(first_cid, second_cid)
        self.assertNotEqual(first_data, second_data)
        with Image.open(BytesIO(second_data)) as image:
            self.assertEqual(image.getpixel((32, 32)), (0, 0, 255, 255))
        self.icon_path.unlink()
        self.assert_no_icon(self.invite())

    def test_custom_welcome_without_icon_does_not_attach_unused_artwork(self):
        self.icon_path.write_bytes(png_bytes())
        design = {
            'root': {'type': 'EmailLayout', 'data': {'childrenIds': ['message']}},
            'message': {'type': 'Text', 'data': {'props': {'text': 'Hello {{username}}'}}}}
        with patch('panel_mail.WELCOME_DESIGN') as path:
            path.open.return_value = StringIO(json.dumps(design))
            first, second = self.invite()
        self.assertIn('Hello player', first.kwargs['html'])
        self.assert_no_icon((first, second))

    def test_password_template_icon_token_is_omitted_even_with_active_icon(self):
        self.icon_path.write_bytes(png_bytes())
        design = {
            'root': {'type': 'EmailLayout', 'data': {'childrenIds': ['icon', 'message']}},
            'icon': {'type': 'Image', 'data': {'props': {'url': '{{server_icon}}'}}},
            'message': {'type': 'Text', 'data': {'props': {'text': '{{password}}'}}}}
        with patch('panel_mail.PASSWORD_DESIGN') as path:
            path.open.return_value = StringIO(json.dumps(design))
            first, second = self.invite()
        self.assertTrue(first.kwargs['inline_images'])
        self.assert_no_icon((second,))
        self.assertNotIn('<img', second.kwargs['html'])


if __name__ == '__main__':
    unittest.main()
