"""Backup regression tests: temporary data only, no Docker or Minecraft."""
import errno
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from server_backups import ServerBackups, BackupBusy, backup_error


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.server = self.data / 'server'
        self.server.mkdir()
        self.config = dict(server_name='Test Server', server_type='Paper', mc_version='1.21.1',
                           auto_backup_keep=2, admin_pass='private', smtp={'password': 'private'})
        self.log = logging.getLogger('backup-tests')
        self.manager = ServerBackups(self.data, self.server, lambda: self.config.copy(), self.log)
        (self.server / 'world').mkdir()
        (self.server / 'world' / 'level.dat').write_bytes(b'world data')

    def create(self, **kwargs):
        return Path(self.manager.create('Test Server', kwargs.pop('is_manual', True), **kwargs))

    def archives(self):
        return sorted((self.data / 'backups').rglob('*.zip'))

    def test_old_and_future_dates_are_clamped_without_changing_source(self):
        old = self.server / 'old.dat'
        future = self.server / 'future.dat'
        old.write_bytes(b'old')
        future.write_bytes(b'future')
        os.utime(old, (0, 0))
        os.utime(future, (4354905600, 4354905600))
        (self.server / 'empty-folder').mkdir()
        with zipfile.ZipFile(self.create()) as archive:
            self.assertEqual(archive.getinfo('old.dat').date_time[0], 1980)
            self.assertEqual(archive.getinfo('future.dat').date_time[0], 2107)
            self.assertEqual(archive.read('world/level.dat'), b'world data')
            self.assertIn('empty-folder/', archive.namelist())
            self.assertIsNone(archive.testzip())
        self.assertEqual(old.stat().st_mtime, 0)
        self.assertEqual(future.stat().st_mtime, 4354905600)

    def test_same_second_backups_never_overwrite_existing_archive(self):
        first = self.create()
        original = first.read_bytes()
        (self.server / 'world' / 'level.dat').write_bytes(b'new data')
        second = self.create()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(len(self.archives()), 2)

    def test_metadata_is_portable_and_never_written_to_live_server(self):
        stale = self.server / 'panel_config.json'
        stale.write_text('old live file must remain untouched', encoding='utf-8')
        (self.server / 'player_history.json').write_text('stale', encoding='utf-8')
        (self.data / 'player_history.json').write_text('{"Player": {}}', encoding='utf-8')
        (self.data / 'panel_accounts.db').write_bytes(b'private')
        with zipfile.ZipFile(self.create()) as archive:
            config = json.loads(archive.read('panel_config.json'))
            self.assertEqual(config['server_name'], 'Test Server')
            self.assertNotIn('admin_pass', config)
            self.assertNotIn('smtp', config)
            self.assertNotIn('panel_accounts.db', archive.namelist())
            self.assertEqual(json.loads(archive.read('player_history.json')), {'Player': {}})
            self.assertEqual(archive.namelist().count('panel_config.json'), 1)
        self.assertEqual(stale.read_text(), 'old live file must remain untouched')
        self.assertEqual((self.server / 'player_history.json').read_text(), 'stale')

    def test_disk_failure_removes_partial_and_preserves_previous_backups(self):
        previous = self.create(is_manual=False)
        original = previous.read_bytes()
        self.config['auto_backup_keep'] = 1
        with patch('server_backups.zipfile.ZipFile.write', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                self.create(is_manual=False)
        self.assertEqual(self.archives(), [previous])
        self.assertEqual(previous.read_bytes(), original)
        self.assertEqual(list((self.data / 'backups').rglob('*.partial')), [])
        self.assertFalse((self.server / 'panel_config.json').exists())
        self.assertFalse(self.manager.lock.locked())

    def test_partial_archive_is_hidden_until_verified(self):
        original = zipfile.ZipFile.testzip
        def verify(archive):
            self.assertEqual(self.archives(), [])
            self.assertEqual(len(list((self.data / 'backups').rglob('*.partial'))), 1)
            return original(archive)
        with patch('server_backups.zipfile.ZipFile.testzip', verify):
            complete = self.create()
        self.assertEqual(self.archives(), [complete])

    def test_verification_and_publication_failures_publish_nothing(self):
        for target, kwargs in (
            ('server_backups.zipfile.ZipFile.testzip', {'return_value': 'world/level.dat'}),
            ('server_backups.os.replace', {'side_effect': PermissionError(errno.EACCES, 'denied')})
        ):
            with self.subTest(target=target), patch(target, **kwargs):
                with self.assertRaises((OSError, ValueError)):
                    self.create()
            self.assertEqual(self.archives(), [])
            self.assertEqual(list((self.data / 'backups').rglob('*.partial')), [])

    def test_filesystem_traversal_errors_are_not_silently_ignored(self):
        def cannot_walk(*args, **kwargs):
            kwargs['onerror'](PermissionError(errno.EACCES, 'directory unreadable'))
            return iter(())
        with patch('server_backups.os.walk', side_effect=cannot_walk):
            with self.assertRaises(PermissionError):
                self.create()
        self.assertEqual(self.archives(), [])

    def test_symlinks_never_leak_files_from_outside_server(self):
        secret = self.data / 'private.txt'
        secret.write_text('not part of backup', encoding='utf-8')
        link = self.server / 'link'
        try:
            link.symlink_to(secret)
        except OSError as error:
            self.skipTest('Symlink creation unavailable: ' + str(error))
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.create()
        self.assertEqual(self.archives(), [])

    def test_retention_only_prunes_old_automatic_backups_after_success(self):
        manual = self.create()
        initial = self.create(is_initial=True)
        safety = self.create(is_restore_backup=True)
        first = self.create(is_manual=False)
        second = self.create(is_manual=False)
        third = self.create(is_manual=False)
        self.assertFalse(first.exists())
        for path in (manual, initial, safety, second, third):
            self.assertTrue(path.exists())
        self.config['auto_backup_keep'] = 0
        last = self.create(is_manual=False)
        self.assertTrue(last.exists())
        self.assertEqual(len(list(last.parent.glob('*_auto.zip'))), 1)

    def test_background_job_is_nonblocking_exclusive_and_scope_bound(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original_update = self.manager._update
        def update(job_id, **values):
            original_update(job_id, **values)
            if values.get('status') in ('success', 'error'):
                finished.set()
        def archive(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release backup')
            kwargs['progress'](files_done=3, message='Verifying...')
            return str(self.server / 'example.zip')
        with patch.object(self.manager, '_archive', side_effect=archive), \
             patch.object(self.manager, '_update', side_effect=update):
            try:
                job = self.manager.start('scope', is_restore=True)
                self.assertTrue(entered.wait(2))
                self.assertEqual(job['status'], 'running')
                self.assertIsNone(self.manager.status(job['job_id'], 'different-server-scope'))
                with self.assertRaises(BackupBusy):
                    self.manager.start('scope')
                with self.assertRaises(BackupBusy):
                    self.create()
                with self.assertRaises(BackupBusy):
                    with self.manager.exclusive():
                        self.fail('Overlapping destructive operation was allowed')
            finally:
                release.set()
                self.assertTrue(finished.wait(5))
        result = self.manager.status(job['job_id'], 'scope')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['file'], 'example.zip')
        self.assertEqual(result['files_done'], 3)
        self.assertFalse(self.manager.lock.locked())

    def test_background_failure_and_thread_start_failure_release_lock(self):
        finished = threading.Event()
        update = self.manager._update
        def record(job_id, **values):
            update(job_id, **values)
            if values.get('status') == 'error':
                finished.set()
        with patch.object(self.manager, '_archive', side_effect=OSError(errno.ENOSPC, 'full')), \
             patch.object(self.manager, '_update', side_effect=record), \
             self.assertLogs(self.log, level='ERROR'):
            job = self.manager.start('scope')
            self.assertTrue(finished.wait(5))
        status = self.manager.status(job['job_id'], 'scope')
        self.assertEqual(status['status'], 'error')
        self.assertIn('free space', status['message'])
        self.assertFalse(self.manager.lock.locked())
        with patch('server_backups.threading.Thread.start', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError):
                self.manager.start('scope')
        self.assertFalse(self.manager.lock.locked())

    def test_readable_errors(self):
        self.assertIn('free space', backup_error(OSError(errno.ENOSPC, 'full')))
        self.assertIn('Permission denied', backup_error(PermissionError(errno.EACCES, 'denied')))
        self.assertIn('disappeared', backup_error(FileNotFoundError(errno.ENOENT, 'missing')))
        self.assertIn('logs', backup_error(RuntimeError()))
