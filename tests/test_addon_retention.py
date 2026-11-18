"""Retention tests use disposable directories only; never production backups."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import addon_retention as retention


class AddonRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='byte-panel-retention-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backups = self.root / 'addon-update-backups'
        self.backups.mkdir()
        self.server = self.root / 'server'
        self.server.mkdir()
        self.now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
        self.sequence = 0

    def batch(self, age=1, outcome='complete', **fields):
        self.sequence += 1
        folder = self.backups / f'{self.sequence:032x}'
        folder.mkdir()
        metadata = dict(retention_version=1, outcome=outcome,
                        created_at=(self.now - timedelta(days=age)).isoformat(),
                        engine='Paper', mc_version='1.21.4',
                        addons=[dict(filename='example.jar', project_id='TestProj',
                                     old_sha512='a' * 128, new_sha512='b' * 128,
                                     old_version='Old1', new_version='New2')])
        metadata.update(fields)
        (folder / 'example.jar').write_bytes(b'old jar fixture')
        (folder / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
        return folder

    def run_cleanup(self, **options):
        return retention.prune_update_backups(self.backups, self.server, now=self.now, **options)

    def test_keeps_newest_two_by_created_time_not_name_or_mtime(self):
        newest = self.batch(age=1)
        oldest = self.batch(age=3)
        middle = self.batch(age=2)
        os.utime(newest, (0, 0))
        result = self.run_cleanup()
        self.assertEqual((result['removed'], result['retained'], result['protected']), (1, 2, 0))
        self.assertTrue(newest.exists() and middle.exists())
        self.assertFalse(oldest.exists())
        self.assertEqual(result['warnings'], [])

    def test_seven_day_expiry_includes_boundary_even_when_newest(self):
        boundary = self.batch(age=7)
        older = self.batch(age=8)
        result = self.run_cleanup()
        self.assertEqual(result['removed'], 2)
        self.assertFalse(boundary.exists() or older.exists())

    def test_newest_two_recent_and_old_batches_expire_together(self):
        batches = [self.batch(age=age) for age in (0, 1, 2, 10)]
        result = self.run_cleanup()
        self.assertEqual((result['removed'], result['retained']), (2, 2))
        self.assertEqual([folder.exists() for folder in batches], [True, True, False, False])

    def test_only_explicit_safe_outcomes_are_eligible(self):
        safe = [self.batch(age=8, outcome=value) for value in ('complete', 'rolled_back', 'cancelled')]
        protected = [self.batch(age=9, outcome=value) for value in ('pending', 'recovery_required', 'unexpected', None)]
        result = self.run_cleanup()
        self.assertEqual((result['removed'], result['protected']), (3, 4))
        self.assertTrue(all(not folder.exists() for folder in safe))
        self.assertTrue(all(folder.exists() for folder in protected))

    def test_legacy_missing_and_invalid_schema_are_protected(self):
        folders = []
        for version in (None, True, 0, 2, '1'):
            folders.append(self.batch(age=8, retention_version=version))
        folder = self.batch(age=8)
        metadata = json.loads((folder / 'metadata.json').read_text())
        del metadata['retention_version']
        (folder / 'metadata.json').write_text(json.dumps(metadata))
        folders.append(folder)
        self.assertEqual(self.run_cleanup()['protected'], len(folders))
        self.assertTrue(all(folder.exists() for folder in folders))

    def test_cancelled_partial_or_empty_copies_can_expire(self):
        empty = self.batch(age=8, outcome='cancelled')
        (empty / 'example.jar').unlink()
        result = self.run_cleanup()
        self.assertEqual(result['removed'], 1)
        self.assertFalse(empty.exists())

    def test_complete_missing_jar_and_cancelled_unknown_file_stay(self):
        complete = self.batch(age=8)
        (complete / 'example.jar').unlink()
        cancelled = self.batch(age=8, outcome='cancelled')
        (cancelled / 'unknown.txt').write_text('must remain')
        self.assertEqual(self.run_cleanup()['protected'], 2)
        self.assertTrue(complete.exists() and cancelled.exists())

    def test_malformed_oversized_duplicate_metadata_are_preserved(self):
        malformed, oversized, duplicate = [self.batch(age=8) for _ in range(3)]
        (malformed / 'metadata.json').write_text('{broken')
        (oversized / 'metadata.json').write_bytes(b' ' * (retention._MAX_METADATA + 1))
        (duplicate / 'metadata.json').write_text('{"retention_version":1,"retention_version":1}')
        result = self.run_cleanup()
        self.assertEqual((result['removed'], result['protected']), (0, 3))

    def test_future_naive_and_invalid_dates_are_preserved(self):
        for value in ((self.now + timedelta(days=1)).isoformat(), '2026-01-01T00:00:00', 'not a date'):
            self.batch(age=8, created_at=value)
        self.assertEqual(self.run_cleanup()['protected'], 3)

    def test_invalid_filename_metadata_cannot_delete_outside(self):
        outside = self.root / 'outside.jar'
        outside.write_bytes(b'outside sentinel')
        for name in ('../../outside.jar', str(outside), '..\\outside.jar', 'C:outside.jar', 'evil\x00.jar'):
            folder = self.batch(age=8)
            metadata = json.loads((folder / 'metadata.json').read_text())
            metadata['addons'][0]['filename'] = name
            (folder / 'metadata.json').write_text(json.dumps(metadata))
        self.assertEqual(self.run_cleanup()['removed'], 0)
        self.assertEqual(outside.read_bytes(), b'outside sentinel')

    def test_duplicate_and_bad_identity_metadata_is_protected(self):
        duplicate, invalid = [self.batch(age=8) for _ in range(2)]
        metadata = json.loads((duplicate / 'metadata.json').read_text())
        metadata['addons'].append(dict(metadata['addons'][0]))
        (duplicate / 'metadata.json').write_text(json.dumps(metadata))
        metadata = json.loads((invalid / 'metadata.json').read_text())
        metadata['addons'][0]['old_sha512'] = 'wrong'
        (invalid / 'metadata.json').write_text(json.dumps(metadata))
        self.assertEqual(self.run_cleanup()['protected'], 2)

    def test_unknown_entries_and_nested_directories_are_preserved(self):
        folder = self.batch(age=8)
        (folder / 'nested').mkdir()
        (self.backups / 'notes.txt').write_text('not ours')
        unrelated = self.backups / 'not-a-batch'
        unrelated.mkdir()
        self.assertEqual(self.run_cleanup()['protected'], 3)
        self.assertTrue((folder / 'example.jar').exists())
        self.assertTrue(unrelated.exists())

    def test_hardlinked_jar_and_metadata_are_protected(self):
        for filename in ('example.jar', 'metadata.json'):
            folder = self.batch(age=8)
            original = folder / filename
            linked = self.root / (str(self.sequence) + filename)
            try:
                os.link(original, linked)
            except OSError as error:
                self.skipTest(f'Hardlinks unavailable: {error}')
        self.assertEqual(self.run_cleanup()['protected'], 2)

    def test_symlinked_jar_and_batch_do_not_follow_targets(self):
        folder = self.batch(age=8)
        outside = self.root / 'outside.jar'
        outside.write_bytes(b'outside')
        (folder / 'example.jar').unlink()
        try:
            os.symlink(outside, folder / 'example.jar')
            os.symlink(self.server, self.backups / ('f' * 32), target_is_directory=True)
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {error}')
        self.assertEqual(self.run_cleanup()['protected'], 2)
        self.assertEqual(outside.read_bytes(), b'outside')
        self.assertTrue(self.server.is_dir())

    def test_symlinked_backup_root_is_rejected(self):
        self.backups.rmdir()
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_text('keep')
        try:
            os.symlink(outside, self.backups, target_is_directory=True)
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {error}')
        result = self.run_cleanup()
        self.assertEqual(result['removed'], 0)
        self.assertTrue(result['warnings'])
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_windows_junction_reparse_attributes_are_rejected(self):
        class Junction:
            st_mode = stat.S_IFDIR | 0o700
            st_file_attributes = 0x400
        with self.assertRaises(ValueError):
            retention._directory(Junction())

    def test_symlinked_metadata_is_preserved_without_reading_target(self):
        folder = self.batch(age=8)
        outside = self.root / 'outside.json'
        (folder / 'metadata.json').rename(outside)
        try:
            os.symlink(outside, folder / 'metadata.json')
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {error}')
        self.assertEqual(self.run_cleanup()['protected'], 1)
        self.assertTrue(outside.exists())
        self.assertTrue((folder / 'example.jar').exists())

    def test_symlinked_ancestor_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.backups = outside / 'addon-update-backups'
        self.backups.mkdir()
        folder = self.batch(age=8)
        alias = self.root / 'alias'
        try:
            os.symlink(outside, alias, target_is_directory=True)
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {error}')
        self.backups = alias / 'addon-update-backups'
        result = self.run_cleanup()
        self.assertEqual(result['removed'], 0)
        self.assertTrue(result['warnings'])
        self.assertTrue((folder / 'example.jar').exists())

    def test_directory_symlink_swap_before_unlink_cannot_delete_outside(self):
        folder = self.batch(age=8)
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'example.jar'
        sentinel.write_bytes(b'outside sentinel')
        probe = self.root / 'probe'
        try:
            os.symlink(outside, probe, target_is_directory=True)
            probe.unlink()
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {error}')
        moved = self.root / 'original-batch'
        original = retention._Folder.unlink
        swapped = []
        def replace_directory(directory, filename):
            if directory.path == folder and not swapped:
                swapped.append(True)
                folder.rename(moved)
                os.symlink(outside, folder, target_is_directory=True)
            return original(directory, filename)
        with patch.object(retention._Folder, 'unlink', replace_directory):
            result = self.run_cleanup()
        self.assertEqual((result['removed'], result['protected']), (0, 1))
        self.assertEqual(sentinel.read_bytes(), b'outside sentinel')
        self.assertTrue((moved / 'example.jar').exists())

    def test_server_overlap_and_broad_directory_are_rejected(self):
        folder = self.batch(age=8)
        for server in (self.root, self.backups, self.backups / 'server'):
            result = retention.prune_update_backups(self.backups, server, now=self.now)
            self.assertEqual(result['removed'], 0)
            self.assertTrue(result['warnings'])
        result = retention.prune_update_backups(self.root, self.server, now=self.now)
        self.assertTrue(result['warnings'])
        self.assertTrue(folder.exists())

    def test_other_recovery_directories_are_untouched(self):
        sentinels = []
        for name in ('file-manager-backups', 'file-manager-trash', 'backups'):
            target = self.root / name
            target.mkdir()
            sentinel = target / 'keep.jar'
            sentinel.write_bytes(b'must stay')
            sentinels.append(sentinel)
        self.batch(age=8)
        self.assertEqual(self.run_cleanup()['removed'], 1)
        self.assertTrue(all(path.read_bytes() == b'must stay' for path in sentinels))

    def test_change_between_inspection_and_deletion_is_protected(self):
        folder = self.batch(age=8)
        original = retention._inspect
        calls = []
        def changed(directory, now):
            calls.append(True)
            if len(calls) == 2:
                (folder / 'extra.jar').write_bytes(b'do not delete')
            return original(directory, now)
        with patch.object(retention, '_inspect', side_effect=changed):
            result = self.run_cleanup()
        self.assertEqual((result['removed'], result['protected']), (0, 1))
        self.assertTrue((folder / 'example.jar').exists())

    def test_deletion_errors_are_bounded_warnings_not_exceptions(self):
        folder = self.batch(age=8)
        with patch.object(retention._Folder, 'unlink', side_effect=PermissionError('fixture denied')):
            result = self.run_cleanup()
        self.assertEqual((result['removed'], result['protected']), (0, 1))
        self.assertIn('fixture denied', result['warnings'][0])
        self.assertTrue((folder / 'metadata.json').exists())

    def test_warnings_are_capped_and_missing_root_is_normal(self):
        for _ in range(15):
            self.batch(age=8, outcome='pending')
        result = self.run_cleanup()
        self.assertEqual(result['protected'], 15)
        self.assertEqual(len(result['warnings']), 10)
        missing = self.root / 'not-created' / 'addon-update-backups'
        self.assertEqual(retention.prune_update_backups(missing, self.server, now=self.now),
                         dict(removed=0, retained=0, protected=0, warnings=[]))

    def test_invalid_options_do_not_delete(self):
        folder = self.batch(age=8)
        for options in ({'keep': 0}, {'keep': True}, {'max_age_days': 0}, {'max_age_days': '7'}):
            self.assertTrue(self.run_cleanup(**options)['warnings'])
        self.assertTrue(retention.prune_update_backups(self.backups, self.server,
                                                      now=datetime(2026, 11, 18))['warnings'])
        self.assertTrue(folder.exists())


if __name__ == '__main__':
    unittest.main()
