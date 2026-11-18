"""Conservative add-on updates: no live network, Minecraft process or real user files."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from addon_updates import AddonUpdateBusy, AddonUpdateError, AddonUpdateService


def jar_bytes(label, descriptor='plugin.yml'):
    target = BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr(zipfile.ZipInfo(descriptor, date_time=(2020, 1, 1, 0, 0, 0)),
                         'name: Example\nversion: ' + label)
        archive.writestr(zipfile.ZipInfo('example.txt', date_time=(2020, 1, 1, 0, 0, 0)), label)
    return target.getvalue()


def digest(data):
    return hashlib.sha512(data).hexdigest()


class Response:
    def __init__(self, content, status=200, after=None):
        self.content = content if isinstance(content, bytes) else json.dumps(content).encode()
        self.status_code = status
        self.after = after
        self.closed = False

    def iter_content(self, size):
        for offset in range(0, len(self.content), size):
            yield self.content[offset:offset + size]
        if self.after:
            self.after()

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, owner):
        self.owner = owner
        self.headers = {}
        self.trust_env = True
        self.closed = False

    def request(self, method, url, **options):
        self.owner.assertFalse(self.owner.guard_active, 'Network must finish before the server-file lock is acquired')
        self.owner.calls.append((method, url, options))
        if self.owner.before_request:
            self.owner.before_request()
        if url.startswith('https://cdn.modrinth.com/'):
            return Response(self.owner.downloads[url], self.owner.download_status,
                            after=self.owner.after_download)
        if self.owner.api_status != 200:
            return Response({}, self.owner.api_status)
        hash_value = url.split('/version_file/', 1)[1].split('?', 1)[0].split('/')[0]
        version = next((item for item in self.owner.installed.values()
                        if item['files'][0]['hashes']['sha512'] == hash_value), None)
        if version is None:
            return Response({}, 404)
        if method == 'POST':
            return Response(self.owner.candidates.get(version['project_id'], version))
        return Response(version)

    def close(self):
        self.closed = True


class AddonUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='byte-panel-updates-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.server = self.root / 'server'
        self.plugins = self.server / 'plugins'
        self.plugins.mkdir(parents=True)
        self.config = dict(is_setup=True, server_type='Paper', mc_version='1.21.4', server_name='Test')
        self.running = False
        self.guard_active = False
        self.guard_failure = False
        self.before_guard = None
        self.before_request = None
        self.after_download = None
        self.api_status = self.download_status = 200
        self.calls, self.sessions = [], []
        self.installed, self.candidates, self.downloads = {}, {}, {}
        self.old, self.new = jar_bytes('old'), jar_bytes('new')
        self.add_project('TestProj', 'example.jar', self.old, self.new)
        self.service = self.make_service()

    @contextmanager
    def guard(self):
        if self.guard_failure:
            raise RuntimeError('busy')
        if self.before_guard:
            self.before_guard()
        self.guard_active = True
        try:
            yield
        finally:
            self.guard_active = False

    def session(self):
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def make_service(self):
        return AddonUpdateService(self.server, self.root / 'addon_updates.json',
                                  lambda: dict(self.config), lambda: self.running,
                                  self.guard, session_factory=self.session)

    def version(self, project, release, content, date):
        url = f'https://cdn.modrinth.com/data/{project}/versions/{release}/example.jar'
        self.downloads[url] = content
        return dict(id=release, project_id=project, version_number=release,
                    date_published=date, version_type='release', status='listed',
                    game_versions=['1.21.4'], loaders=['paper'], environment='server_only',
                    dependencies=[], files=[dict(filename='example.jar', primary=True,
                        hashes={'sha512': digest(content)}, size=len(content), url=url)])

    def add_project(self, project, filename, old, new):
        (self.plugins / filename).write_bytes(old)
        self.installed[project] = self.version(project, project + 'Old', old, '2025-01-01T00:00:00Z')
        self.candidates[project] = self.version(project, project + 'New', new, '2025-02-01T00:00:00Z')

    def approve(self, projects=None):
        result = self.service.check()
        self.assertEqual(result['status'], 'idle', result)
        self.service.configure(True, projects or ['TestProj'])

    def assert_unchanged(self):
        self.assertEqual((self.plugins / 'example.jar').read_bytes(), self.old)

    def test_check_uses_hash_identity_exact_compatibility_and_does_not_install(self):
        result = self.service.check()
        self.assertEqual(result['addons'][0]['status'], 'update_available')
        self.assertEqual(result['addons'][0]['project_id'], 'TestProj')
        self.assertFalse(result['enabled'])
        self.assert_unchanged()
        self.assertFalse(self.service.backup_dir.exists())
        self.assertIn(digest(self.old), self.calls[0][1])
        self.assertEqual(self.calls[1][2]['json'], {
            'loaders': ['paper', 'spigot', 'bukkit'], 'game_versions': ['1.21.4'], 'version_types': ['release']})
        self.assertTrue(all(not session.trust_env and session.closed for session in self.sessions))
        self.assertTrue(all(options['allow_redirects'] is False for _, _, options in self.calls))

    def test_approvals_are_per_verified_project_and_survive_panel_restart(self):
        with self.assertRaises(AddonUpdateError):
            self.service.configure(True, ['Unknown'])
        with self.assertRaises(AddonUpdateError):
            self.service.configure('yes', [])
        self.approve()
        restored = self.make_service().snapshot()
        self.assertTrue(restored['enabled'])
        self.assertEqual(restored['projects'], ['TestProj'])
        saved = json.loads((self.root / 'addon_updates.json').read_text())
        self.assertEqual(set(saved), {'enabled', 'projects'})

    def test_disabled_or_running_service_does_not_download_or_change_files(self):
        self.service.run_once()
        self.assertEqual(self.calls, [])
        self.approve()
        self.calls.clear()
        self.running = True
        self.assertIn('fully stopped', self.service.run_once()['message'])
        self.assertEqual(self.calls, [])
        self.assert_unchanged()

    def test_valid_update_backs_up_old_jar_and_never_starts_server(self):
        self.approve()
        result = self.service.run_once()
        self.assertEqual(result['status'], 'idle', result)
        self.assertEqual(result['addons'][0]['status'], 'updated')
        self.assertEqual((self.plugins / 'example.jar').read_bytes(), self.new)
        backups = list(self.service.backup_dir.glob('*/example.jar'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.old)
        metadata = json.loads(backups[0].with_name('metadata.json').read_text())
        self.assertEqual(metadata['addons'][0]['old_sha512'], digest(self.old))
        self.assertEqual(metadata['addons'][0]['new_sha512'], digest(self.new))
        self.assertEqual(metadata['retention_version'], 1)
        self.assertEqual(metadata['outcome'], 'complete')
        self.assertEqual(result['backup_retention'], {'keep': 2, 'max_age_days': 7})
        self.assertEqual(result['backup_cleanup']['retained'], 1)
        self.assertTrue(result['backup_cleanup']['last_run'])
        self.assertFalse(self.running)
        self.assertTrue(result['last_applied'])

    def test_unknown_jar_is_never_guessed_from_its_filename(self):
        (self.plugins / 'luckperms.jar').write_bytes(jar_bytes('unknown'))
        result = self.service.check()
        unknown = next(row for row in result['addons'] if row['filename'] == 'luckperms.jar')
        self.assertIsNone(unknown['project_id'])
        self.assertEqual(unknown['status'], 'unknown')
        self.assertTrue(all('luckperms' not in url for _, url, _ in self.calls))

    def test_unapproved_known_project_is_not_updated(self):
        self.add_project('OtherPrj', 'other.jar', jar_bytes('otherold'), jar_bytes('othernew'))
        self.approve()
        self.service.run_once()
        self.assertEqual((self.plugins / 'other.jar').read_bytes(), jar_bytes('otherold'))
        self.assertEqual((self.plugins / 'example.jar').read_bytes(), self.new)

    def test_unsafe_candidate_metadata_is_blocked(self):
        baseline = deepcopy(self.candidates['TestProj'])
        for changes in ({'project_id': 'OtherPrj'}, {'id': '../bad'}, {'version_type': 'beta'},
                        {'status': 'draft'}, {'game_versions': ['1.21.5']}, {'loaders': ['fabric']},
                        {'environment': 'client_only'}, {'date_published': '2024-01-01T00:00:00Z'},
                        {'dependencies': None}, {'dependencies': [{'dependency_type': 'mystery'}]}):
            with self.subTest(changes=changes):
                self.candidates['TestProj'] = {**deepcopy(baseline), **changes}
                result = self.service.check()
                self.assertEqual(result['addons'][0]['status'], 'blocked', result)
                self.assert_unchanged()

    def test_download_url_and_ambiguous_files_fail_closed(self):
        baseline = deepcopy(self.candidates['TestProj'])
        for url in ('http://cdn.modrinth.com/data/TestProj/versions/a/a.jar',
                    'https://evil.example/a.jar', 'https://cdn.modrinth.com.evil.example/a.jar',
                    'https://user:pass@cdn.modrinth.com/data/TestProj/versions/a/a.jar',
                    'https://cdn.modrinth.com/data/TestProj/versions/../a.jar',
                    'https://cdn.modrinth.com/data/OtherPrj/versions/a/a.jar',
                    'https://cdn.modrinth.com/data/TestProj/versions/a/a.jar?url=http://127.0.0.1'):
            with self.subTest(url=url):
                self.candidates['TestProj'] = deepcopy(baseline)
                self.candidates['TestProj']['files'][0]['url'] = url
                self.assertEqual(self.service.check()['addons'][0]['status'], 'blocked')
        self.candidates['TestProj'] = deepcopy(baseline)
        self.candidates['TestProj']['files'].append(deepcopy(baseline['files'][0]))
        self.assertEqual(self.service.check()['addons'][0]['status'], 'blocked')

    def test_required_missing_or_incompatible_dependencies_block_updates(self):
        for dependency in ({'dependency_type': 'required', 'project_id': 'Missing'},
                           {'dependency_type': 'required', 'file_name': 'unknown.jar'},
                           {'dependency_type': 'incompatible', 'project_id': 'TestProj'}):
            with self.subTest(dependency=dependency):
                self.candidates['TestProj']['dependencies'] = [dependency]
                self.assertEqual(self.service.check()['addons'][0]['status'], 'blocked')
                self.assert_unchanged()

    def test_proven_installed_dependency_is_allowed_but_never_auto_installed(self):
        self.add_project('DepProj', 'dependency.jar', jar_bytes('depold'), jar_bytes('depnew'))
        self.candidates['TestProj']['dependencies'] = [dict(
            dependency_type='required', project_id='DepProj', version_id='DepProjOld')]
        self.approve()
        result = self.service.run_once()
        self.assertEqual(result['status'], 'idle', result)
        self.assertEqual((self.plugins / 'example.jar').read_bytes(), self.new)
        self.assertEqual((self.plugins / 'dependency.jar').read_bytes(), jar_bytes('depold'))

    def test_reverse_dependency_version_pin_prevents_breaking_existing_addon(self):
        self.add_project('DepProj', 'dependency.jar', jar_bytes('depold'), jar_bytes('depnew'))
        self.installed['DepProj']['dependencies'] = [dict(
            dependency_type='required', project_id='TestProj', version_id='TestProjOld')]
        self.approve()
        self.assertEqual(self.service.run_once()['status'], 'error')
        self.assert_unchanged()
        self.assertFalse(any(url.startswith('https://cdn.') for _, url, _ in self.calls))

    def test_bad_download_checksum_size_redirect_or_archive_preserves_original(self):
        self.approve()
        url = self.candidates['TestProj']['files'][0]['url']
        for content in (b'corrupt', self.new + b'extra'):
            with self.subTest(content=content[:20]):
                self.downloads[url] = content
                self.assertEqual(self.service.run_once()['status'], 'error')
                self.assert_unchanged()
        self.downloads[url] = self.new
        self.download_status = 302
        self.assertEqual(self.service.run_once()['status'], 'error')
        self.assert_unchanged()
        self.download_status = 200
        bad = jar_bytes('notpaper', 'fabric.mod.json')
        self.candidates['TestProj']['files'][0].update(hashes={'sha512': digest(bad)}, size=len(bad))
        self.downloads[url] = bad
        self.assertEqual(self.service.run_once()['status'], 'error')
        self.assert_unchanged()

    def test_provider_failure_and_rate_limit_stop_without_installing(self):
        for status in (429, 302, 500):
            self.api_status = status
            self.assertEqual(self.service.check()['status'], 'error')
            self.assert_unchanged()
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_symlink_hardlink_and_oversized_jar_are_rejected(self):
        target = self.plugins / 'example.jar'
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path == target):
            self.assertEqual(self.service.check()['status'], 'error')
        hardlink = self.plugins / 'linked.jar'
        os.link(target, hardlink)
        self.assertEqual(self.service.check()['status'], 'error')
        hardlink.unlink()
        with patch('addon_updates.MAX_JAR_BYTES', 10):
            self.assertEqual(self.service.check()['status'], 'error')
        self.assertEqual(self.calls, [])

    def test_staging_races_cancel_without_overwriting_user_changes(self):
        self.approve()
        self.after_download = lambda: (self.plugins / 'example.jar').write_bytes(b'user replacement')
        result = self.service.run_once()
        self.assertEqual(result['status'], 'error')
        self.assertEqual((self.plugins / 'example.jar').read_bytes(), b'user replacement')
        self.assertFalse(self.service.backup_dir.exists())

    def test_server_start_or_engine_change_after_download_cancels_install(self):
        self.approve()
        self.after_download = lambda: setattr(self, 'running', True)
        self.assertEqual(self.service.run_once()['status'], 'error')
        self.assert_unchanged()
        self.running = False
        self.after_download = lambda: self.config.update(mc_version='1.21.5')
        self.assertEqual(self.service.run_once()['status'], 'error')
        self.assert_unchanged()

    def test_reset_during_staging_cancels_and_clears_opt_ins(self):
        self.approve()
        self.after_download = self.service.reset
        self.assertEqual(self.service.run_once()['status'], 'error')
        self.assert_unchanged()
        self.assertFalse(self.service.snapshot()['enabled'])
        self.assertEqual(self.service.snapshot()['projects'], [])

    def test_busy_mutation_guard_reports_reason_without_unhandled_exception(self):
        self.approve()
        self.guard_failure = True
        result = self.service.run_once()
        self.assertEqual(result['status'], 'error')
        self.assertIn('busy', result['message'])
        self.assert_unchanged()

    def test_backup_failure_aborts_before_replacement(self):
        self.approve()
        with patch.object(self.service, '_backup', side_effect=OSError('no space')):
            result = self.service.run_once()
        self.assertEqual(result['status'], 'error')
        self.assertIn('backup failed', result['message'])
        self.assert_unchanged()

    def test_tampered_stage_is_never_installed(self):
        self.approve()
        def change_stage():
            for path in self.root.glob('.addon-update-*/*.jar'):
                path.write_bytes(b'tampered stage')
        self.before_guard = change_stage
        result = self.service.run_once()
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['enabled'])
        self.assert_unchanged()

    def test_changed_dependency_inventory_cancels_staged_update(self):
        self.add_project('DepProj', 'dependency.jar', jar_bytes('depold'), jar_bytes('depnew'))
        self.approve()
        self.after_download = lambda: (self.plugins / 'dependency.jar').unlink()
        self.assertEqual(self.service.run_once()['status'], 'error')
        self.assert_unchanged()

    def test_atomic_replace_failure_rolls_back_and_disables_retries(self):
        self.add_project('OtherPrj', 'other.jar', jar_bytes('otherold'), jar_bytes('othernew'))
        self.approve(['TestProj', 'OtherPrj'])
        original_replace = os.replace
        failed = []
        def replace(source, destination):
            if Path(destination).name == 'other.jar' and not failed:
                failed.append(True)
                raise OSError('fixture replacement failure')
            return original_replace(source, destination)
        with patch('addon_updates.os.replace', side_effect=replace):
            result = self.service.run_once()
        self.assertEqual(result['status'], 'error')
        self.assertIn('previous JARs restored', result['message'])
        self.assertFalse(result['enabled'])
        self.assert_unchanged()
        self.assertEqual((self.plugins / 'other.jar').read_bytes(), jar_bytes('otherold'))
        self.assertEqual(len(list(self.service.backup_dir.glob('*/metadata.json'))), 1)
        manifest = next(self.service.backup_dir.glob('*/metadata.json'))
        self.assertEqual(json.loads(manifest.read_text())['outcome'], 'rolled_back')

    def expire_fixture_backup(self):
        manifest = next(self.service.backup_dir.glob('*/metadata.json'))
        metadata = json.loads(manifest.read_text())
        metadata['created_at'] = '2000-01-01T00:00:00Z'
        manifest.write_text(json.dumps(metadata), encoding='utf-8')
        return manifest.parent

    def test_expiry_runs_even_when_updates_are_disabled_and_server_is_running(self):
        self.approve()
        self.assertEqual(self.service.run_once()['status'], 'idle')
        backup = self.expire_fixture_backup()
        self.service.configure(False, ['TestProj'])
        self.running = True
        report = self.service.cleanup_backups()
        self.assertEqual(report['removed'], 1)
        self.assertFalse(backup.exists())
        self.assertEqual((self.plugins / 'example.jar').read_bytes(), self.new)
        self.assertFalse(self.service.snapshot()['enabled'])

    def test_cleanup_never_runs_during_an_inflight_task(self):
        with self.service._work, patch('addon_updates.prune_update_backups') as prune:
            self.assertIsNone(self.service.cleanup_backups())
            prune.assert_not_called()
        self.assertFalse(self.service.snapshot()['busy'])

    def test_cleanup_failure_does_not_change_a_successful_install(self):
        self.approve()
        with patch('addon_updates.prune_update_backups', side_effect=OSError('fixture cleanup failure')):
            result = self.service.run_once()
        self.assertEqual(result['status'], 'idle', result)
        self.assertTrue(result['enabled'])
        self.assertEqual((self.plugins / 'example.jar').read_bytes(), self.new)
        self.assertTrue(result['backup_cleanup']['warnings'])
        self.assertEqual(len(list(self.service.backup_dir.glob('*/example.jar'))), 1)
        self.assertFalse(result['busy'])

    def test_unrecorded_completion_is_protected_even_after_expiry(self):
        self.approve()
        with patch.object(self.service, '_finish_backup', return_value=False):
            result = self.service.run_once()
        self.assertEqual(result['status'], 'idle')
        self.assertIn('protected', result['message'])
        backup = self.expire_fixture_backup()
        self.assertEqual(json.loads((backup / 'metadata.json').read_text())['outcome'], 'pending')
        report = self.service.cleanup_backups()
        self.assertEqual(report['removed'], 0)
        self.assertEqual(report['protected'], 1)
        self.assertTrue(report['warnings'])
        self.assertEqual((backup / 'example.jar').read_bytes(), self.old)

    def test_large_manifest_can_record_its_outcome_within_retention_limit(self):
        directory = self.service.backup_dir / ('1' * 32)
        directory.mkdir(parents=True)
        metadata = {'outcome': 'pending', 'addons': [
            {'filename': '\u2603' * 200 + str(index) + '.jar'} for index in range(64)]}
        self.service._write_backup_metadata(directory, metadata)
        self.assertGreater((directory / 'metadata.json').stat().st_size, 65536)
        self.assertTrue(self.service._finish_backup(directory, 'complete'))
        self.assertEqual(json.loads((directory / 'metadata.json').read_text())['outcome'], 'complete')

    def test_partial_backup_failure_can_expire_without_changing_active_plugin(self):
        self.approve()
        def fail_copy(source, destination, length):
            destination.write(source.read(8))
            raise OSError('fixture full disk')
        with patch('addon_updates.shutil.copyfileobj', side_effect=fail_copy):
            result = self.service.run_once()
        self.assertEqual(result['status'], 'error')
        self.assert_unchanged()
        backup = self.expire_fixture_backup()
        self.assertEqual(json.loads((backup / 'metadata.json').read_text())['outcome'], 'cancelled')
        report = self.service.cleanup_backups()
        self.assertEqual(report['removed'], 1, report)
        self.assertFalse(backup.exists())
        self.assert_unchanged()

    def test_failed_rollback_preserves_recovery_copy_even_when_expired(self):
        self.add_project('OtherPrj', 'other.jar', jar_bytes('otherold'), jar_bytes('othernew'))
        self.approve(['TestProj', 'OtherPrj'])
        original_replace = os.replace
        def fail_replacement_and_rollback(source, destination):
            if Path(destination).parent == self.plugins and (
                    Path(destination).name == 'other.jar' or Path(source).suffix == '.rollback'):
                raise OSError('fixture installation/rollback failure')
            return original_replace(source, destination)
        with patch('addon_updates.os.replace', side_effect=fail_replacement_and_rollback):
            result = self.service.run_once()
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['enabled'])
        self.assertIn('rollback needs attention', result['message'])
        backup = self.expire_fixture_backup()
        self.assertEqual(json.loads((backup / 'metadata.json').read_text())['outcome'], 'recovery_required')
        report = self.service.cleanup_backups()
        self.assertEqual(report['removed'], 0)
        self.assertEqual(report['protected'], 1)
        self.assertTrue(report['warnings'])
        self.assertEqual((backup / 'example.jar').read_bytes(), self.old)

    def test_async_task_is_bounded_and_rejects_concurrent_jobs(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            entered.set()
            release.wait(3)
        self.before_request = hold
        self.service.start('check')
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.service.snapshot()['busy'])
        try:
            with self.assertRaises(AddonUpdateBusy):
                self.service.start('check')
        finally:
            release.set()
        self.assertTrue(self.service._work.acquire(timeout=3))
        self.service._work.release()
        self.assertEqual(self.service.snapshot()['status'], 'idle')
        self.assertFalse(self.service.snapshot()['busy'])

    def test_fabric_forge_and_purpur_use_matching_roots_and_descriptors(self):
        for engine, loader, descriptor in [('Fabric', 'fabric', 'fabric.mod.json'),
                                            ('Forge', 'forge', 'META-INF/mods.toml'),
                                            ('Purpur', 'paper', 'plugin.yml')]:
            with self.subTest(engine=engine):
                self.config['server_type'] = engine
                self.plugins = self.server / ('plugins' if engine == 'Purpur' else 'mods')
                self.plugins.mkdir(exist_ok=True)
                old, new = jar_bytes('old' + engine, descriptor), jar_bytes('new' + engine, descriptor)
                self.add_project('TestProj', 'example.jar', old, new)
                self.candidates['TestProj']['loaders'] = [loader]
                self.service = self.make_service()
                self.approve()
                result = self.service.run_once()
                self.assertEqual(result['status'], 'idle', result)
                self.assertEqual((self.plugins / 'example.jar').read_bytes(), new)

    def test_mod_updates_require_explicit_dedicated_server_environment_metadata(self):
        self.config['server_type'] = 'Fabric'
        self.plugins = self.server / 'mods'
        self.plugins.mkdir()
        self.add_project('TestProj', 'example.jar', jar_bytes('fabricold', 'fabric.mod.json'),
                         jar_bytes('fabricnew', 'fabric.mod.json'))
        self.candidates['TestProj']['loaders'] = ['fabric']
        for environment in (None, 'unknown', 'unrecognized', 'client_only', 'singleplayer_only'):
            self.candidates['TestProj']['environment'] = environment
            result = self.service.check()
            self.assertEqual(result['addons'][0]['status'], 'blocked')
            self.assertIn('dedicated servers', result['addons'][0]['reason'])
        self.candidates['TestProj']['environment'] = 'client_only_server_optional'
        self.assertEqual(self.service.check()['addons'][0]['status'], 'update_available')


if __name__ == '__main__':
    unittest.main()
