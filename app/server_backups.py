"""Atomic server archives and short-request jobs with optional offline lifecycle hooks."""

from contextlib import contextmanager, nullcontext
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import stat
import threading
import uuid
import zipfile

from werkzeug.utils import secure_filename


PORTABLE_KEYS = ('server_name', 'mc_version', 'server_type', 'ram_gb', 'java_path',
                 'forge_version', 'forge_full_version', 'launch_type', 'launch_target')


def backup_server_key(server_name):
    """Storage identifier shared by archive creation and the UI; never a display label."""
    return secure_filename(str(server_name)) or 'server'


class BackupBusy(RuntimeError):
    pass


class BackupCompletedError(RuntimeError):
    """The ZIP exists, but post-backup recovery failed; never imply it was lost."""
    def __init__(self, path, error):
        self.path = str(path)
        message = str(error)
        super().__init__(message if message.startswith('Backup created') else
                         'Backup created, but server recovery failed: ' + message)


def backup_error(error):
    if isinstance(error, OSError):
        if error.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', errno.ENOSPC)):
            return 'Not enough free space or disk quota to create the backup. Free space on the /data volume and try again.'
        if error.errno in (errno.EACCES, errno.EPERM):
            return 'Permission denied while reading server files or writing the backup. Check the /data volume permissions.'
        if error.errno == errno.ENOENT:
            return 'A server file disappeared during the backup. Stop Minecraft and any plugins writing files, then retry.'
    return str(error) or 'Backup creation failed. Check the panel container logs for details.'


class ServerBackups:
    def __init__(self, data_dir, server_dir, load_config, logger, backup_context=None):
        self.data = Path(data_dir)
        self.server = Path(server_dir)
        self.load_config = load_config
        self.logger = logger
        self.backup_context = backup_context
        self.lock = threading.Lock()
        self.jobs_lock = threading.Lock()
        self.jobs = {}

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise BackupBusy('A backup or server file operation is already running. Wait for it to finish before trying again.')
        try:
            yield
        finally:
            self.lock.release()

    def create(self, server_name, is_manual, is_initial=False, is_restore_backup=False):
        with self.exclusive():
            return self._create(server_name, is_manual, is_initial, is_restore_backup)

    def _create(self, server_name, is_manual, is_initial=False, is_restore_backup=False, progress=None):
        # All callers already hold the reservation, including during stop and recovery.
        progress = progress or (lambda **values: None)
        shutdown = self.load_config().get('backup_shutdown') is True and not is_initial
        if shutdown and self.backup_context is None:
            raise RuntimeError('Offline backup mode is unavailable. Stop Minecraft manually before backing up.')
        context = self.backup_context(progress) if shutdown else nullcontext()
        path = None
        try:
            with context:
                path = self._archive(server_name, is_manual, is_initial, is_restore_backup, progress=progress)
        except Exception as error:
            if path is not None:
                raise BackupCompletedError(path, error) from error
            raise
        return path

    def status(self, job_id, scope):
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job or job['scope'] != scope:
                return None
            return {key: value for key, value in job.items() if key != 'scope'}

    def _update(self, job_id, **values):
        with self.jobs_lock:
            self.jobs[job_id].update(values)

    def start(self, scope, is_restore=False):
        # Reserve the filesystem before starting the thread; two requests cannot queue
        # competing archives, or sneak a purge in before the worker starts.
        if not self.lock.acquire(blocking=False):
            raise BackupBusy('A backup or server file operation is already running. Wait for it to finish before trying again.')
        job_id = uuid.uuid4().hex
        try:
            with self.jobs_lock:
                while len(self.jobs) >= 32:
                    del self.jobs[next(iter(self.jobs))]
                self.jobs[job_id] = dict(job_id=job_id, scope=scope, status='running',
                                        message='Preparing backup...', files_done=0)
            thread = threading.Thread(target=self._run, args=(job_id, is_restore), daemon=True)
            thread.start()
        except Exception:
            with self.jobs_lock:
                self.jobs.pop(job_id, None)
            self.lock.release()
            raise
        return self.status(job_id, scope)

    def _run(self, job_id, is_restore):
        try:
            path = self._create(self.load_config().get('server_name', 'Server'), True,
                                 is_restore_backup=is_restore,
                                 progress=lambda **values: self._update(job_id, **values))
            result = dict(status='success', file=os.path.basename(path), server_key=Path(path).parent.name,
                          message='Backup created successfully!')
        except Exception as error:
            self.logger.exception('Server backup failed (job %s)', job_id)
            result = dict(status='error', message=backup_error(error))
            if isinstance(error, BackupCompletedError):
                result.update(backup_created=True, file=Path(error.path).name,
                              server_key=Path(error.path).parent.name)
        finally:
            self.lock.release()
        # A successful status guarantees a completed ZIP and released filesystem lock.
        self._update(job_id, **result)

    def _archive(self, server_name, is_manual, is_initial=False, is_restore_backup=False, progress=None):
        progress = progress or (lambda **values: None)
        server_name = backup_server_key(server_name)
        root = self.server.resolve(strict=True)
        backup_root = self.data / 'backups'
        destination = backup_root / server_name
        if backup_root.is_symlink() or destination.is_symlink():
            raise ValueError('The backup destination must not be a symbolic link.')
        destination.mkdir(parents=True, exist_ok=True)
        if root == destination.resolve() or root in destination.resolve().parents:
            raise ValueError('The backup destination must be outside the server folder.')
        kind = 'InitialBackup' if is_initial else 'RestoreBackup' if is_restore_backup else 'manual' if is_manual else 'auto'
        filename = f'{server_name}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex}_{kind}.zip'
        completed = destination / filename
        partial = destination / (filename + '.partial')
        config = self.load_config()
        done = 0

        def walk_error(error):
            raise error

        def include(archive, path, name):
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f'Cannot back up symbolic links or special files: {name}. Replace them with regular files and retry.')
            archive.write(path, name)

        try:
            # Clamps out-of-range timestamps *in the archive*, leaving source dates intact.
            with zipfile.ZipFile(partial, 'x', zipfile.ZIP_DEFLATED, allowZip64=True,
                                 strict_timestamps=False) as archive:
                for folder, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
                    for name in sorted(directories):
                        path = Path(folder) / name
                        include(archive, path, path.relative_to(root).as_posix())
                    for name in sorted(files):
                        path = Path(folder) / name
                        relative = path.relative_to(root).as_posix()
                        if relative in ('panel_config.json', 'player_history.json'):
                            continue
                        include(archive, path, relative)
                        done += 1
                        progress(files_done=done, message=f'Compressing server files: {done} files saved...')
                # Write portable metadata directly to the ZIP, never into the live server.
                archive.writestr('panel_config.json', json.dumps(
                    {key: config[key] for key in PORTABLE_KEYS if key in config}, indent=4))
                history = self.data / 'player_history.json'
                if history.exists():
                    include(archive, history, 'player_history.json')
            progress(message='Verifying the backup archive...')
            with zipfile.ZipFile(partial) as archive:
                damaged = archive.testzip()
                if damaged:
                    raise ValueError(f'Backup verification failed for {damaged}. Check the server storage and retry.')
            with open(partial, 'r+b') as handle:
                os.fsync(handle.fileno())
            os.replace(partial, completed)
        finally:
            if partial.exists():
                try:
                    partial.unlink()
                except OSError:
                    self.logger.exception('Could not remove an incomplete .partial backup; it remains hidden from the backup list')

        # Retention only runs after publication; a failure must not remove good backups.
        if not is_manual and not is_initial:
            try:
                keep = max(1, int(config.get('auto_backup_keep', 5)))
                old = sorted((p for p in destination.glob('*_auto.zip') if p != completed),
                             key=lambda p: p.stat().st_mtime)
                for path in old[:max(0, len(old) + 1 - keep)]:
                    path.unlink()
            except (OSError, ValueError, TypeError):
                self.logger.exception('Backup succeeded, but automatic backup retention failed')
        return str(completed)
