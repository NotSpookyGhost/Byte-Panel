"""Local restic repositories. Published .restic files are small snapshot references."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import tarfile
import uuid
import zipfile


class ResticError(RuntimeError):
    pass


def plain_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ResticError(f'Expected a regular backup file: {path.name}')
    return info


def directory(path):
    if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400 or not path.is_dir():
        raise ResticError(f'Expected a regular backup directory: {path.name}')
    return path


def private_write(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reference(path):
    plain_file(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(data.get('snapshot', ''))):
        raise ResticError(f'Invalid restic backup reference: {path.name}')
    return data


class ResticStore:
    def __init__(self, data, server, logger, scope=None):
        self.data = data.absolute()
        self.server = server.absolute()
        self.groups = self.data / 'backups'
        self.private = self.data / 'restic'
        self.password = self.private / 'password'
        self.logger = logger
        self.scope = scope

    @staticmethod
    def owner(group):
        marker = group / '.restic-owner'
        plain_file(marker)
        owner = marker.read_text(encoding='ascii')
        if not re.fullmatch(r'[0-9a-f]{64}', owner):
            raise ResticError('Invalid backup owner marker. Restore the marker from your recovery copy.')
        return owner

    def group(self, key, create=False):
        identity = self.scope() if self.scope else hashlib.sha256(key.encode()).hexdigest()
        if not re.fullmatch(r'[0-9a-f]{64}', identity):
            raise ResticError('Invalid server backup identity.')
        if not self.groups.exists() and not create:
            return self.groups / key
        self.groups.mkdir(exist_ok=True)
        directory(self.groups)
        for group in self.groups.iterdir():
            if group.is_symlink() or not group.is_dir():
                continue
            marker = group / '.restic-owner'
            if marker.is_file() and not marker.is_symlink() and marker.read_bytes() == identity.encode('ascii'):
                return directory(group)
        group = self.groups / key
        if (group / '.restic-owner').exists():
            group = self.groups / (key + '_' + identity)
        if (group / '.restic-repo').exists() or any(group.glob('*.restic')):
            raise ResticError('Backup ownership is missing or inconsistent. Restore the .restic-owner file for this group.')
        if create:
            group.mkdir(exist_ok=True)
            directory(group)
            private_write(group / '.restic-owner', identity.encode())
        return group

    def _prepare(self, group):
        self.private.mkdir(mode=0o700, exist_ok=True)
        directory(self.private)
        repo = group / '.restic-repo'
        if not self.password.exists():
            if any(self.groups.glob('*/.restic-repo/config')) or any(self.groups.glob('*/*.restic')):
                raise ResticError('Restore /data/restic/password from your recovery copy before accessing backups.')
            private_write(self.password, secrets.token_urlsafe(48).encode('ascii'))
        plain_file(self.password)
        if not repo.exists():
            if any(group.glob('*.restic')) or any(group.glob('*.deleting')):
                raise ResticError('The restic repository for this server is missing. Restore it from storage.')
            with tempfile.TemporaryDirectory(dir=group, prefix='.restic-init-') as tmp:
                new = Path(tmp) / 'repo'
                self.run(new, ['init', '--repository-version', '2'])
                new.rename(repo)
        directory(repo)
        return repo

    def run(self, repo, arguments, *, consume=None, reader=None):
        directory(repo.parent)
        if repo.exists():
            directory(repo)
        directory(self.private)
        plain_file(self.password)
        cmd = ['restic', '--repo', str(repo), '--password-file', str(self.password),
               '--cache-dir', str(self.private / 'cache'), *arguments]
        with tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen(cmd, cwd=self.data, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=errors)
            except FileNotFoundError as error:
                raise ResticError('The restic executable is missing. Rebuild the Byte Panel image.') from error
            with process:
                try:
                    if reader is not None:
                        result = reader(process.stdout)
                    elif consume is not None:
                        for line in process.stdout:
                            consume(json.loads(line))
                        result = None
                    else:
                        result = process.communicate()[0]
                    code = process.wait()
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
            if code:
                errors.seek(max(0, errors.tell() - 2048))
                detail = errors.read().decode('utf-8', errors='replace').strip()
                self.logger.error('restic %s exited %d: %s', arguments, code, detail)
                reason = {3: 'Some source files could not be read.', 10: 'The repository is missing.',
                          11: 'The repository is locked.', 12: 'The repository password is incorrect.'}.get(code)
                if 'space left' in detail.lower():
                    reason = 'Not enough free space on the backup volume.'
                raise ResticError(reason or 'Restic failed. Check the panel logs and backup volume.')
            return result

    def create(self, group, kind, metadata, exclusions, progress):
        if self.server.parent != self.data:
            raise ResticError('The Minecraft directory must be directly inside the panel data directory.')
        directory(self.server)
        repo = self._prepare(group)
        meta = self.private / 'metadata'
        meta.mkdir(mode=0o700, exist_ok=True)
        directory(meta)
        private_write(meta / 'panel_config.json', json.dumps(metadata, indent=2).encode())
        sources = [self.server.name, 'restic/metadata/panel_config.json']
        history = self.data / 'player_history.json'
        if history.exists():
            plain_file(history)
            sources.append('player_history.json')
        transaction = 'panel-job-' + uuid.uuid4().hex
        journal = group / ('.creating-' + transaction)
        private_write(journal, b'')
        args = ['--json', 'backup', '--host', 'byte-panel', '--group-by', 'host',
                '--tag', kind, '--tag', transaction]
        for path in (*exclusions, 'panel_config.json', 'player_history.json'):
            args.extend(['--exclude', (self.server / path).as_posix()])
        summary = {}
        def update(message):
            if message.get('message_type') == 'summary':
                summary.update(message)
            elif message.get('message_type') == 'status':
                files = message.get('files_done', 0)
                percent = round(100 * message.get('percent_done', 0))
                progress(files_done=files, message=f'Saving restic snapshot: {percent}% ({files} files)')
        progress(message='Saving restic snapshot...')
        self.run(repo, args + ['--', *sources], consume=update)
        snapshot = summary.get('snapshot_id', '')
        if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
            raise ResticError('Restic did not confirm a complete snapshot.')
        progress(message='Checking snapshot contents...')
        def check(node):
            if node.get('struct_type') == 'node' and node.get('type') not in ('file', 'dir'):
                raise ValueError('Cannot back up symbolic links or special files: ' + str(node.get('path')))
        self.run(repo, ['--json', 'ls', snapshot], consume=check)
        info = dict(snapshot=snapshot, kind=kind, timestamp=datetime.now().timestamp(),
                    logical_bytes=summary['total_bytes_processed'], added_bytes=summary['data_added_packed'],
                    history='player_history.json' in sources)
        filename = f'{group.name}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex}_{kind}.restic'
        path = group / filename
        private_write(path, json.dumps(info).encode())
        journal.unlink()
        return str(path)

    @staticmethod
    def repository_bytes(repo):
        total = 0
        if repo.is_dir() and not repo.is_symlink():
            for folder, _, names in os.walk(repo):
                for name in names:
                    try:
                        total += (Path(folder) / name).lstat().st_size
                    except FileNotFoundError:
                        pass  # A concurrent operation finished using a temporary pack or lock.
        return total

    def export(self, path, output):
        info = reference(path)
        repo = path.parent / '.restic-repo'
        snapshot = info['snapshot']
        # Stream the tree once, clamping old region-file timestamps for ZIP readers.
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            def copy_tree(stream):
                with tarfile.open(fileobj=stream, mode='r|') as source:
                    for member in source:
                        name = member.name.removeprefix('./').rstrip('/')
                        if name in ('', '.'):
                            continue
                        if (not (member.isdir() or member.isfile()) or name.startswith('/')
                                or any(part in ('', '.', '..') for part in name.split('/'))
                                or ':' in name or chr(92) in name):
                            raise ResticError('Snapshot contains an entry that cannot be exported safely.')
                        # DOS timestamps can represent only 1980 through 2107.
                        timestamp = max(315532800, min(member.mtime, 4354819198))
                        stamp = datetime.fromtimestamp(timestamp, timezone.utc).timetuple()[:6]
                        item = zipfile.ZipInfo(name + ('/' if member.isdir() else ''), stamp)
                        item.create_system = 3
                        item.external_attr = ((stat.S_IFDIR if member.isdir() else stat.S_IFREG) | member.mode) << 16
                        item.compress_type = zipfile.ZIP_DEFLATED
                        if member.isdir():
                            archive.writestr(item, b'')
                        else:
                            item.file_size = member.size
                            with source.extractfile(member) as src, archive.open(item, 'w') as dest:
                                shutil.copyfileobj(src, dest, 1024 * 1024)
            self.run(repo, ['dump', '--archive', 'tar', snapshot + ':/' + self.server.name, '/'], reader=copy_tree)
            members = ['restic/metadata/panel_config.json']
            if info['history']:
                members.append('player_history.json')
            for member in members:
                with archive.open(Path(member).name, 'w', force_zip64=True) as dest:
                    self.run(repo, ['dump', snapshot, '/' + member],
                             reader=lambda src: shutil.copyfileobj(src, dest, 1024 * 1024))
        output.seek(0)

    def restore(self, path, stage):
        info = reference(path)
        self.run(path.parent / '.restic-repo', ['restore', info['snapshot'], '--verify', '--target', str(stage)])
        world = directory(stage / self.server.name)
        metadata = stage / 'restic/metadata/panel_config.json'
        plain_file(metadata)
        os.replace(metadata, world / 'panel_config.json')
        if info['history']:
            history = stage / 'player_history.json'
            plain_file(history)
            os.replace(history, world / 'player_history.json')
        return world

    def maintain(self, group, keep=None):
        repo = group / '.restic-repo'
        if keep is not None:
            automatic = sorted(group.glob('*_auto.restic'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            for old in automatic[keep:]:
                self.mark_deleted(old)
        journals = list(group.glob('.creating-panel-job-*'))
        if journals:
            published = {reference(path)['snapshot'] for path in group.glob('*.restic')}
            for journal in journals:
                tag = journal.name.removeprefix('.creating-')
                if not re.fullmatch(r'panel-job-[0-9a-f]{32}', tag):
                    raise ResticError('Invalid backup transaction marker.')
                snapshots = json.loads(self.run(repo, ['--json', 'snapshots', '--tag', tag]))
                abandoned = [s['id'] for s in snapshots if s['id'] not in published]
                if abandoned:
                    self.run(repo, ['forget', *abandoned])
            self.run(repo, ['prune'])
            for journal in journals:
                journal.unlink()
        pending = list(group.glob('*.restic.deleting'))
        if pending:
            ids = {reference(p)['snapshot'] for p in pending}
            existing = json.loads(self.run(repo, ['--json', 'snapshots']))
            remaining = sorted(ids.intersection(s['id'] for s in existing))
            if remaining:
                self.run(repo, ['forget', *remaining])
            self.run(repo, ['prune'])
            for p in pending:
                p.unlink(missing_ok=True)

    @staticmethod
    def mark_deleted(path):
        reference(path)
        os.replace(path, path.with_name(path.name + '.deleting'))

    def delete(self, path):
        self.mark_deleted(path)
        self.maintain(path.parent)
