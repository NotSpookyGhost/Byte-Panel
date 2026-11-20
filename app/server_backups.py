"""Atomic server archives and short-request jobs with optional offline lifecycle hooks."""

from contextlib import contextmanager, nullcontext
import errno
import os
from pathlib import Path
import threading
import uuid
from restic_store import ResticStore

from werkzeug.utils import secure_filename


PORTABLE_KEYS = ('server_name', 'mc_version', 'server_type', 'ram_gb', 'java_path',
                 'forge_version', 'forge_full_version', 'launch_type', 'launch_target')


def backup_server_key(server_name):
    """Storage identifier shared by archive creation and the UI; never a display label."""
    return secure_filename(str(server_name)) or 'server'


def normalize_backup_exclusions(paths):
    """Exact paths relative to the server root, used by both the directory picker and snapshot writer."""
    if not isinstance(paths, list):
        raise ValueError('Backup exclusions must be a list of server-relative paths.')
    result = {}
    for value in paths:
        if not isinstance(value, str):
            raise ValueError('Each backup exclusion must be a path.')
        path = value.strip().replace('\\', '/')
        if not path:
            continue
        if (path.startswith('/') or any(c in path for c in ':*?[]') or
                any(ord(c) < 32 or ord(c) == 127 for c in path) or len(path) > 1024):
            raise ValueError('Use exact server-relative paths in backup exclusions, without wildcards or drive letters.')
        path = path.rstrip('/')
        if any(part in ('', '.', '..') for part in path.split('/')):
            raise ValueError('Each backup exclusion must name a file or folder under the server root. Use a path such as bluemap/web/maps.')
        if path in ('ServerEngineInfo', 'panel_config.json', 'player_history.json'):
            raise ValueError(f'{path} is panel backup metadata and cannot be excluded.')
        result[path] = None
        if len(result) > 128:
            raise ValueError('Use at most 128 backup exclusions.')
    return list(result)


class BackupBusy(RuntimeError):
    pass


class BackupCompletedError(RuntimeError):
    """The snapshot exists, but post-backup recovery failed; never imply it was lost."""
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
    def __init__(self, data_dir, server_dir, load_config, logger, backup_context=None, scope=None):
        self.data = Path(data_dir)
        self.server = Path(server_dir)
        self.load_config = load_config
        self.restic = ResticStore(self.data, self.server, logger, scope)
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
        # Pruning may repack shared data. Minecraft has already restarted here.
        try:
            keep = None if is_manual or is_initial or is_restore_backup else max(1, int(self.load_config().get('auto_backup_keep', 5)))
            self.restic.maintain(Path(path).parent, keep)
        except (OSError, ValueError, RuntimeError):
            self.logger.exception('Snapshot saved, but restic maintenance needs attention')
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
        # A successful status guarantees a published snapshot and released filesystem lock.
        self._update(job_id, **result)

    def _archive(self, server_name, is_manual, is_initial=False, is_restore_backup=False, progress=None):
        progress = progress or (lambda **values: None)
        group = self.restic.group(backup_server_key(server_name), create=True)
        config = self.load_config()
        excluded = normalize_backup_exclusions(config.get('backup_exclusions', []))
        metadata = {key: config[key] for key in PORTABLE_KEYS if key in config}
        if excluded:
            metadata['backup_exclusions'] = sorted(excluded)
        kind = 'InitialBackup' if is_initial else 'RestoreBackup' if is_restore_backup else 'manual' if is_manual else 'auto'
        return self.restic.create(group, kind, metadata, excluded, progress)
