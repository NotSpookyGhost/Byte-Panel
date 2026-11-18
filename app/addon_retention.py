"""Prune only completed, explicitly marked add-on rollback batches.

Callers serialize this helper with update jobs. No recursive deletion is used:
Linux directory descriptors pin every operation; Windows revalidates checked paths.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat

BACKUP_KEEP = 2
BACKUP_MAX_AGE_DAYS = 7
_BATCH = re.compile(r'^[a-f0-9]{32}$')
_ID = re.compile(r'^[A-Za-z0-9]{1,64}$')
_HASH = re.compile(r'^[a-fA-F0-9]{128}$')
_ELIGIBLE = {'complete', 'rolled_back', 'cancelled'}
_MAX_METADATA = 256 * 1024


def _signature(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _directory(info):
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError('Directory is a link, junction or non-directory.')


def _regular(info):
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Backup contents must be ordinary files, without links.')


def _filename(name):
    return (isinstance(name, str) and 0 < len(name) <= 255
            and name.lower().endswith('.jar') and name not in ('.', '..')
            and not any(character in name for character in '/\\:')
            and not any(ord(character) < 32 or ord(character) == 127 for character in name))


class _Folder:
    def __init__(self, path, fd=None):
        self.path, self.fd = path, fd
        self.info = os.fstat(fd) if fd is not None else path.lstat()
        _directory(self.info)

    def verify(self):
        if self.fd is None:
            for ancestor in self.path.parents:
                _directory(ancestor.lstat())
        current = self.path.lstat()
        _directory(current)
        if (current.st_dev, current.st_ino) != (self.info.st_dev, self.info.st_ino):
            raise ValueError('Backup directory changed during cleanup.')

    def info_for(self, name):
        self.verify()
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return (self.path / name).lstat()

    def names(self, limit):
        self.verify()
        result = []
        with os.scandir(self.fd if self.fd is not None else self.path) as entries:
            for entry in entries:
                result.append(entry.name)
                if len(result) > limit:
                    raise ValueError('Backup directory exceeds the safe cleanup entry limit.')
        return result

    @contextmanager
    def child(self, name):
        before = self.info_for(name)
        _directory(before)
        fd = None
        try:
            if self.fd is not None:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
            child = _Folder(self.path / name, fd)
            if (child.info.st_dev, child.info.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('Backup directory changed during cleanup.')
            yield child
        finally:
            if fd is not None:
                os.close(fd)

    def metadata(self):
        before = self.info_for('metadata.json')
        _regular(before)
        if before.st_size > _MAX_METADATA:
            raise ValueError('Backup metadata exceeds its size limit.')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = (os.open('metadata.json', flags, dir_fd=self.fd) if self.fd is not None
              else os.open(self.path / 'metadata.json', flags))
        with os.fdopen(fd, 'rb') as stream:
            if _signature(os.fstat(stream.fileno())) != _signature(before):
                raise ValueError('Backup metadata changed during cleanup.')
            raw = stream.read(_MAX_METADATA + 1)
        if len(raw) > _MAX_METADATA or _signature(self.info_for('metadata.json')) != _signature(before):
            raise ValueError('Backup metadata changed during cleanup.')
        return json.loads(raw, object_pairs_hook=_unique_object), before

    def unlink(self, name):
        self.verify()
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            (self.path / name).unlink()

    def rmdir(self, name):
        self.verify()
        if self.fd is not None:
            os.rmdir(name, dir_fd=self.fd)
        else:
            (self.path / name).rmdir()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate backup metadata field.')
        result[key] = value
    return result


@contextmanager
def _root(path, server):
    # Refuse broad directories and overlap in either direction with Minecraft files.
    path = Path(os.path.abspath(os.fspath(path)))
    server = Path(server).resolve()
    if path.name != 'addon-update-backups':
        raise ValueError('Cleanup is restricted to addon-update-backups.')
    for ancestor in reversed((path, *path.parents)):
        _directory(ancestor.lstat())
    resolved = path.resolve()
    if resolved == server or server in resolved.parents or resolved in server.parents:
        raise ValueError('Update backups must be separate from Minecraft server files.')
    fd = None
    try:
        if os.name != 'nt':
            if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
                raise ValueError('Safe directory-relative cleanup is unavailable.')
            # Pin the entire absolute ancestor chain, not just the final component.
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(path.anchor, flags)
            for component in path.parts[1:]:
                next_fd = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        yield _Folder(path, fd)
    finally:
        if fd is not None:
            os.close(fd)


def _inspect(directory, now):
    metadata, meta_info = directory.metadata()
    if (not isinstance(metadata, dict) or type(metadata.get('retention_version')) is not int
            or metadata['retention_version'] != 1 or metadata.get('outcome') not in _ELIGIBLE):
        raise ValueError('Legacy, pending or failed-recovery backup requires manual review.')
    created = metadata.get('created_at')
    if not isinstance(created, str):
        raise ValueError('Backup timestamp is missing.')
    created = datetime.fromisoformat(created.replace('Z', '+00:00'))
    if created.tzinfo is None or created > now:
        raise ValueError('Backup timestamp is invalid or in the future.')
    addons = metadata.get('addons')
    if (metadata.get('engine') not in ('Paper', 'Purpur', 'Forge', 'Fabric')
            or not isinstance(metadata.get('mc_version'), str) or not metadata['mc_version']
            or not isinstance(addons, list) or not 1 <= len(addons) <= 64):
        raise ValueError('Backup metadata is incomplete.')
    expected = {'metadata.json': _signature(meta_info)}
    seen = set()
    for addon in addons:
        if not isinstance(addon, dict) or not _filename(addon.get('filename')):
            raise ValueError('Backup filename is invalid.')
        name = addon['filename']
        if name.casefold() in seen:
            raise ValueError('Backup metadata contains duplicate filenames.')
        seen.add(name.casefold())
        if (any(not isinstance(addon.get(key), str) or not _ID.fullmatch(addon[key])
                for key in ('project_id', 'old_version', 'new_version'))
                or any(not isinstance(addon.get(key), str) or not _HASH.fullmatch(addon[key])
                       for key in ('old_sha512', 'new_sha512'))):
            raise ValueError('Backup identity metadata is invalid.')
        try:
            info = directory.info_for(name)
        except FileNotFoundError:
            if metadata['outcome'] == 'cancelled':
                continue
            raise
        _regular(info)
        expected[name] = _signature(info)
    if set(directory.names(65)) != set(expected):
        raise ValueError('Backup contains missing or unexpected files.')
    return created, expected, (directory.info.st_dev, directory.info.st_ino)


def prune_update_backups(backup_dir, server_dir, *, now=None, keep=BACKUP_KEEP,
                         max_age_days=BACKUP_MAX_AGE_DAYS):
    """Remove old safe batches; preserve uncertain recovery and report bounded warnings."""
    result = dict(removed=0, retained=0, protected=0, warnings=[])
    def warn(message):
        if len(result['warnings']) < 10:
            result['warnings'].append(str(message))
    try:
        now = datetime.now(timezone.utc) if now is None else now
        if (not isinstance(now, datetime) or now.tzinfo is None or type(keep) is not int
                or keep < 1 or type(max_age_days) is not int or max_age_days < 1):
            raise ValueError('Invalid backup retention settings.')
        cutoff = now - timedelta(days=max_age_days)
        with _root(backup_dir, server_dir) as root:
            candidates = []
            for name in root.names(4096):
                try:
                    if not _BATCH.fullmatch(name):
                        raise ValueError('Unrecognized entry was preserved.')
                    with root.child(name) as directory:
                        created, files, identity = _inspect(directory, now)
                    candidates.append((created, name, files, identity))
                except (OSError, ValueError, TypeError, RecursionError) as error:
                    result['protected'] += 1
                    warn(f'{name}: {error}')
            for index, (created, name, files, identity) in enumerate(sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)):
                if index < keep and created > cutoff:
                    result['retained'] += 1
                    continue
                try:
                    with root.child(name) as directory:
                        checked = _inspect(directory, now)
                        if checked != (created, files, identity):
                            raise ValueError('Backup changed after inspection.')
                        # Metadata is removed last, so an interrupted cleanup remains protected.
                        for filename in sorted(files, key=lambda entry: entry == 'metadata.json'):
                            if _signature(directory.info_for(filename)) != files[filename]:
                                raise ValueError('Backup file changed before deletion.')
                            directory.unlink(filename)
                    info = root.info_for(name)
                    if (info.st_dev, info.st_ino) != identity:
                        raise ValueError('Backup directory changed before removal.')
                    _directory(info)
                    root.rmdir(name)
                    result['removed'] += 1
                except (OSError, ValueError, TypeError, RecursionError) as error:
                    result['protected'] += 1
                    warn(f'{name}: Cleanup incomplete; review remaining files. {error}')
    except FileNotFoundError:
        # An absent recovery root is normal before the first update.
        pass
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        warn(f'Add-on backup cleanup was skipped: {error}')
    return result
