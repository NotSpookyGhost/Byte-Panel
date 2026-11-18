"""Validated Minecraft server icons, shared by the panel and the server list."""
import io
import os
from pathlib import Path
import tempfile
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000


class ServerIconError(ValueError):
    pass


def panel_icon_path(server_dir, branding_dir):
    """Prefer the active server's icon, including icons from imported backups.

    Old panel-only logos remain a display fallback until the user uploads an icon.
    Never copy that fallback into a newly created or restored Minecraft server.
    """
    for path in (Path(server_dir) / 'server-icon.png', Path(branding_dir) / 'logo.png'):
        if not path.is_symlink() and path.is_file():
            return path
    return None


def normalize_icon(stream):
    raw = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ServerIconError('Server icon must be 5 MiB or smaller.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ('PNG', 'JPEG', 'WEBP'):
                    raise ServerIconError('Choose a PNG, JPEG or WebP image for the server icon.')
                if source.width * source.height > MAX_PIXELS:
                    raise ServerIconError('Server icon must contain no more than 16 million pixels.')
                if getattr(source, 'n_frames', 1) != 1:
                    raise ServerIconError('Server icon must be a still image, not an animation.')
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                # Respect phone-camera orientation; retain the full image without stretching.
                oriented = ImageOps.exif_transpose(source).convert('RGBA')
                fitted = ImageOps.contain(oriented, (64, 64), Image.Resampling.LANCZOS)
                icon = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
                icon.paste(fitted, ((64 - fitted.width) // 2, (64 - fitted.height) // 2))
                output = io.BytesIO()
                # The fresh canvas strips uploaded metadata and always emits a genuine PNG.
                icon.save(output, format='PNG')
                return output.getvalue()
    except ServerIconError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError, SyntaxError, ValueError) as error:
        raise ServerIconError('Server icon is invalid or cannot be safely decoded. Choose a PNG, JPEG or WebP image.') from error


def save_server_icon(stream, server_dir):
    """Publish one canonical file atomically; caller holds the server-files lock."""
    data = normalize_icon(stream)
    root = Path(server_dir).resolve(strict=True)
    target = root / 'server-icon.png'
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ServerIconError('The server icon path is not a regular file. Check the server files before uploading.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix='.server-icon-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target
