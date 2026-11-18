"""Updater routes and scheduling composed with isolated account fixtures."""
import io
import unittest
from unittest.mock import Mock, patch

from werkzeug.exceptions import Conflict

import test_accounts as account_tests
from addon_updates import AddonUpdateBusy


class TickStop:
    def __init__(self, ticks):
        self.ticks = ticks
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.ticks

    def wait(self, timeout):
        self.waits.append(timeout)
        return self.is_set()


class AddonUpdateIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Compose the reusable fixture rather than inherit and rerun its full suite.
        cls.fixture_type = type('AddonAccountFixture', (account_tests.AccountIntegrationTests,), {})
        cls.fixture_type.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_type.tearDownClass()

    def setUp(self):
        self.fixture = self.fixture_type()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.module = self.fixture.module
        self.service = self.module.addon_updates
        self.service.reset()
        self.client = self.fixture.client

    def headers(self, client=None):
        return self.fixture.headers(client)

    def test_all_update_routes_require_login_and_settings_permission(self):
        anonymous = self.module.app.test_client()
        viewer = self.fixture.client_for(self.fixture.create('viewer'))
        manager = self.fixture.client_for(self.fixture.create('manager', permissions=['plugins.manage', 'server.control']))
        settings = self.fixture.client_for(self.fixture.create('settings', permissions=['settings.manage']))
        for client, code in ((anonymous, 401), (viewer, 403), (manager, 403)):
            with self.subTest(code=code, client=client):
                self.assertEqual(client.get('/api/addon-updates').status_code, code)
                headers = self.headers(client) if client is not anonymous else {}
                with patch.object(self.service, 'start') as start:
                    for action in ('check', 'apply'):
                        response = client.post('/api/addon-updates/' + action, headers=headers)
                        self.assertEqual(response.status_code, 403)  # Missing CSRF is denied before anonymous auth.
                    start.assert_not_called()
        self.assertEqual(settings.get('/api/addon-updates').status_code, 200)
        response = settings.put('/api/addon-updates', headers=self.headers(settings),
                                json={'enabled': True, 'projects': []})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['enabled'])

    def test_csrf_required_for_settings_and_task_start(self):
        response = self.client.put('/api/addon-updates', json={'enabled': True, 'projects': []})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.service.snapshot()['enabled'])
        with patch.object(self.service, 'start') as start:
            for action in ('check', 'apply'):
                response = self.client.post('/api/addon-updates/' + action)
                self.assertEqual(response.status_code, 403)
            start.assert_not_called()

    def test_update_settings_validate_types_and_verified_project_identity(self):
        for payload in ([], None, {}, {'enabled': 'yes', 'projects': []},
                        {'enabled': True, 'projects': ['Unknown']},
                        {'enabled': True, 'projects': 'not-a-list'}):
            with self.subTest(payload=payload):
                response = self.client.put('/api/addon-updates', headers=self.headers(), json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.service.snapshot()['enabled'])
        response = self.client.put('/api/addon-updates', headers=self.headers(),
                                   json={'enabled': True, 'projects': []})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['enabled'])
        response = self.client.put('/api/addon-updates', headers=self.headers(),
                                   json={'enabled': False, 'projects': []})
        self.assertFalse(response.json['enabled'])

    def test_check_and_apply_return_accepted_without_blocking_http_on_network(self):
        for action in ('check', 'apply'):
            with self.subTest(action=action):
                state = {'status': 'checking', 'busy': True, 'enabled': True, 'addons': []}
                with patch.object(self.service, 'start', return_value=state) as start:
                    response = self.client.post('/api/addon-updates/' + action, headers=self.headers())
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json, state)
                start.assert_called_once_with(action)

    def test_busy_tasks_return_json_conflict(self):
        with patch.object(self.service, 'start', side_effect=AddonUpdateBusy('Update already running')):
            for action in ('check', 'apply'):
                response = self.client.post('/api/addon-updates/' + action, headers=self.headers())
                self.assertEqual(response.status_code, 409)
                self.assertIn('already running', response.json['error'])

    def test_apply_rejects_live_process_saved_pid_and_old_log_reader(self):
        scenarios = ('managed', 'saved', 'reader')
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.module.MINECRAFT_PROCESS = Mock(poll=Mock(return_value=None)) if scenario == 'managed' else None
                self.module.MINECRAFT_LOG_THREAD = Mock(is_alive=Mock(return_value=True)) if scenario == 'reader' else None
                config = self.module.load_config()
                config['server_pid'] = 123 if scenario == 'saved' else None
                self.module.save_config(config)
                with patch.object(self.module.psutil, 'pid_exists', return_value=True), patch.object(self.service, 'start') as start:
                    response = self.client.post('/api/addon-updates/apply', headers=self.headers())
                self.assertEqual(response.status_code, 409)
                self.assertIn('Stop Minecraft', response.json['error'])
                start.assert_not_called()

    def test_mutation_guard_requires_offline_state_and_cancels_crash_recovery(self):
        with patch.object(self.module.server_automation, 'stop') as stop:
            with self.module.server_file_mutation_guard():
                self.assertTrue(self.module.server_backups.lock.locked())
            stop.assert_called_once_with()
        self.module.MINECRAFT_LOG_THREAD = Mock(is_alive=Mock(return_value=True))
        with self.assertRaises(Conflict), self.module.server_file_mutation_guard():
            self.fail('A running log reader must prevent mutation')
        self.module.MINECRAFT_LOG_THREAD = None
        with self.module.server_backups.exclusive():
            with self.assertRaises(Conflict), self.module.server_file_mutation_guard():
                self.fail('A concurrent backup must prevent mutation')

    def run_scheduler(self, *, enabled=True, running=False, ticks=2, times=None, start_error=None):
        stop = TickStop(ticks)
        with patch.object(self.service, 'snapshot', return_value={'enabled': enabled}), \
                patch.object(self.module, 'server_process_active', return_value=running), \
                patch.object(self.module.time, 'monotonic', side_effect=times or [0, 60]), \
                patch.object(self.service, 'start', side_effect=start_error) as start, \
                patch.object(self.module.app.logger, 'exception'):
            self.module.addon_update_scheduler(stop)
        self.assertEqual(stop.waits, [60] * ticks)
        return start

    def test_scheduler_is_opt_in_offline_and_hourly(self):
        self.run_scheduler(enabled=False, times=[0, 60]).assert_not_called()
        self.run_scheduler(running=True, times=[0, 60]).assert_not_called()
        self.run_scheduler().assert_called_once_with('apply')
        start = self.run_scheduler(ticks=3, times=[0, 3601, 3602])
        self.assertEqual(start.call_count, 2)

    def approve_fixture_project(self):
        self.service._rows = [{'project_id': 'TestProj'}]
        self.service.configure(True, ['TestProj'])
        self.assertTrue(self.service.snapshot()['enabled'])

    def test_replacing_server_clears_update_approvals(self):
        archive = self.fixture.zip_bytes()
        backup_dir = self.fixture.data / 'backups' / 'Updater_Reset'
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / 'server.zip').write_bytes(archive)
        for action in ('import', 'restore', 'purge'):
            with self.subTest(action=action):
                self.approve_fixture_project()
                if action == 'import':
                    response = self.client.post('/api/import', headers=self.headers(),
                        data={'import_zip': (io.BytesIO(archive), 'server.zip')})
                elif action == 'restore':
                    response = self.client.post('/api/backup/restore', headers=self.headers(),
                        json={'server_name': 'Updater_Reset', 'filename': 'server.zip'})
                else:
                    response = self.client.post('/api/purge', headers=self.headers())
                self.assertEqual(response.status_code, 200, response.json)
                self.assertFalse(self.service.snapshot()['enabled'])
                self.assertEqual(self.service.snapshot()['projects'], [])

    def test_invalid_import_and_refused_restore_preserve_approvals(self):
        self.approve_fixture_project()
        response = self.client.post('/api/import', headers=self.headers(),
            data={'import_zip': (io.BytesIO(b'not a zip'), 'broken.zip')})
        self.assertEqual(response.status_code, 400)
        backup_dir = self.fixture.data / 'backups' / 'Updater_Refused'
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / 'server.zip').write_bytes(self.fixture.zip_bytes())
        self.module.MINECRAFT_PROCESS = Mock(poll=Mock(return_value=None))
        response = self.client.post('/api/backup/restore', headers=self.headers(),
            json={'server_name': 'Updater_Refused', 'filename': 'server.zip'})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.service.snapshot()['enabled'])
        self.assertEqual(self.service.snapshot()['projects'], ['TestProj'])

    def test_scheduler_busy_waits_before_retry_and_other_failures_cool_down(self):
        start = self.run_scheduler(times=[0, 60, 60], start_error=[AddonUpdateBusy('busy'), None])
        self.assertEqual(start.call_count, 2)
        start = self.run_scheduler(start_error=RuntimeError('fixture failure'))
        start.assert_called_once_with('apply')

    def test_scheduler_maintains_backups_hourly_even_when_updates_disabled(self):
        with patch.object(self.service, 'cleanup_backups', return_value={}) as cleanup:
            start = self.run_scheduler(enabled=False, running=True, ticks=4, times=[0, 60, 3600, 3660])
        start.assert_not_called()
        self.assertEqual(cleanup.call_count, 2)

    def test_scheduler_retries_cleanup_after_busy_worker_finishes(self):
        with patch.object(self.service, 'cleanup_backups', side_effect=[None, {}]) as cleanup:
            self.run_scheduler(enabled=False, ticks=3, times=[0, 60, 120])
        self.assertEqual(cleanup.call_count, 2)


if __name__ == '__main__':
    unittest.main()
