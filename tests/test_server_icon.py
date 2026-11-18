"""Server-icon conversion and publication tests; temporary files only."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from server_icon import normalize_icon, save_server_icon, server_icon_path, ServerIconError, MAX_UPLOAD_BYTES


def image_bytes(size=(120, 60), mode='RGBA', color=(20, 80, 120, 128), format='PNG', **kwargs):
    stream = io.BytesIO()
    Image.new(mode, size, color).save(stream, format=format, **kwargs)
    return stream.getvalue()


class ServerIconTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / 'server'
        self.branding = self.root / 'branding'
        self.server.mkdir()
        self.branding.mkdir()

    def test_formats_are_real_64_square_pngs_and_keep_aspect_and_transparency(self):
        for format in ('PNG', 'JPEG', 'WEBP'):
            with self.subTest(format=format):
                raw = image_bytes(mode='RGB', color=(20, 80, 120), format=format)
                result = normalize_icon(io.BytesIO(raw))
                self.assertTrue(result.startswith(b'\x89PNG\r\n\x1a\n'))
                with Image.open(io.BytesIO(result)) as image:
                    self.assertEqual(image.size, (64, 64))
                    self.assertEqual(image.mode, 'RGBA')
                    self.assertEqual(image.getpixel((32, 0))[3], 0)
                    self.assertEqual(image.getpixel((32, 32))[3], 255)
                    self.assertEqual(image.getbbox(), (0, 16, 64, 48))
        with Image.open(io.BytesIO(normalize_icon(io.BytesIO(image_bytes())))) as image:
            self.assertEqual(image.getpixel((32, 32))[3], 128)

    def test_camera_orientation_is_applied_and_metadata_is_stripped(self):
        exif = Image.Exif()
        exif[274] = 6
        raw = image_bytes(mode='RGB', color=(20, 80, 120), format='JPEG', exif=exif)
        with Image.open(io.BytesIO(normalize_icon(io.BytesIO(raw)))) as image:
            self.assertEqual(image.getbbox(), (16, 0, 48, 64))
            self.assertEqual(dict(image.getexif()), {})

    def test_invalid_unsupported_truncated_and_oversized_files_are_rejected(self):
        for raw in (b'', b'<svg><script>alert(1)</script></svg>', b'not an image',
                    image_bytes()[:45], image_bytes(format='GIF'), b'x' * (MAX_UPLOAD_BYTES + 1)):
            with self.subTest(length=len(raw)), self.assertRaises(ServerIconError):
                normalize_icon(io.BytesIO(raw))
        with patch('server_icon.MAX_PIXELS', 100), self.assertRaisesRegex(ServerIconError, 'million pixels'):
            normalize_icon(io.BytesIO(image_bytes()))
        with patch('server_icon.Image.MAX_IMAGE_PIXELS', 100), self.assertRaises(ServerIconError):
            normalize_icon(io.BytesIO(image_bytes()))

    def test_animation_is_rejected(self):
        stream = io.BytesIO()
        Image.new('RGBA', (32, 32), 'red').save(stream, format='PNG', save_all=True,
            append_images=[Image.new('RGBA', (32, 32), 'blue')], duration=100, loop=0)
        with self.assertRaisesRegex(ServerIconError, 'still image'):
            normalize_icon(io.BytesIO(stream.getvalue()))

    def test_atomic_replacement_preserves_existing_file_on_failure(self):
        target = save_server_icon(io.BytesIO(image_bytes()), self.server)
        original = target.read_bytes()
        with patch('server_icon.os.replace', side_effect=PermissionError('cannot replace')):
            with self.assertRaises(PermissionError):
                save_server_icon(io.BytesIO(image_bytes(color='red')), self.server)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.server.glob('.server-icon-*.tmp')), [])
        with self.assertRaises(ServerIconError):
            save_server_icon(io.BytesIO(b'invalid'), self.server)
        self.assertEqual(target.read_bytes(), original)
        save_server_icon(io.BytesIO(image_bytes(color='red')), self.server)
        self.assertNotEqual(target.read_bytes(), original)

    def test_server_icon_is_independent_of_legacy_panel_logo(self):
        self.assertIsNone(server_icon_path(self.server))
        legacy = self.branding / 'logo.png'
        legacy.write_bytes(image_bytes())
        self.assertIsNone(server_icon_path(self.server))
        self.assertFalse((self.server / 'server-icon.png').exists())
        active = save_server_icon(io.BytesIO(image_bytes(color='red')), self.server)
        self.assertEqual(server_icon_path(self.server).resolve(), active)
        self.assertEqual(legacy.read_bytes(), image_bytes())

    def test_non_file_icon_target_is_rejected_without_changing_it(self):
        target = self.server / 'server-icon.png'
        target.mkdir()
        with self.assertRaisesRegex(ServerIconError, 'regular file'):
            save_server_icon(io.BytesIO(image_bytes()), self.server)
        self.assertTrue(target.is_dir())


if __name__ == '__main__':
    unittest.main()
