"""Automatic startup/recovery policy tests with fake clocks and processes only."""

from contextlib import contextmanager
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from server_automation import ServerAutomation
from server_backups import BackupBusy


class FakeProcess:
    def __init__(self, pid=1234, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


class ServerAutomationTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.config = dict(is_setup=True, mc_version='1.21.1', auto_start=False,
                           auto_restart=False, server_pid=None)
        self.process = None
        self.reader = None
        self.busy = False
        self.lock_held = False
        self.starts = []
        self.logger = Mock(spec=logging.Logger)
        self.pid_exists = Mock(return_value=False)
        self.start = Mock(side_effect=self.start_process)
        self.policy = ServerAutomation(lambda: self.config.copy(), self.start,
                                       lambda: self.process, lambda: self.reader,
                                       self.exclusive, self.logger, clock=lambda: self.now,
                                       pid_exists=self.pid_exists)

    @contextmanager
    def exclusive(self):
        if self.busy or self.lock_held:
            raise BackupBusy('busy')
        self.lock_held = True
        try:
            yield
        finally:
            self.lock_held = False

    def start_process(self):
        self.assertTrue(self.lock_held, 'Automatic startup must hold the operation lock')
        self.process = FakeProcess(2000 + len(self.starts))
        self.starts.append(self.process)
        self.config['server_pid'] = self.process.pid
        self.policy.track(self.process, automatic=True)
        return True

    def track(self, returncode=None, automatic=False):
        self.process = FakeProcess(returncode=returncode)
        self.config['server_pid'] = self.process.pid
        self.policy.track(self.process, automatic=automatic)
        return self.process

    def crash_and_restart(self):
        self.process.returncode = 1
        self.assertFalse(self.policy.tick())
        self.now += 10
        self.assertTrue(self.policy.tick())

    def test_defaults_never_start_server(self):
        self.assertFalse(self.policy.tick())
        self.now = 1000
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_boot_start_is_once_and_independent_of_crash_recovery(self):
        self.config['auto_start'] = True
        self.assertTrue(self.policy.tick())
        self.assertFalse(self.policy.tick())
        self.process.returncode = 1
        self.now += 100
        self.assertFalse(self.policy.tick())
        self.start.assert_called_once_with()

    def test_boot_flag_requires_exact_true(self):
        for flag in ('true', 1, 'false'):
            with self.subTest(flag=flag):
                self.setUp()
                self.config['auto_start'] = flag
                self.assertFalse(self.policy.tick())
                self.start.assert_not_called()

    def test_boot_requires_configured_server(self):
        for key, value in (('is_setup', False), ('mc_version', None), ('mc_version', '')):
            with self.subTest(key=key, value=value):
                self.setUp()
                self.config.update(auto_start=True)
                self.config[key] = value
                self.assertFalse(self.policy.tick())
                self.config.update(is_setup=True, mc_version='1.21.1')
                self.assertFalse(self.policy.tick())
                self.start.assert_not_called()

    def test_enabling_auto_start_later_does_not_launch(self):
        self.policy.tick()
        self.config['auto_start'] = True
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_busy_boot_still_consumes_disabled_or_unconfigured_eligibility(self):
        self.busy = True
        self.config.update(is_setup=False, auto_start=True)
        self.policy.tick()
        self.config['is_setup'] = True
        self.busy = False
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_eligible_boot_waits_for_operation_lock(self):
        self.config['auto_start'] = True
        self.busy = True
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()
        self.busy = False
        self.assertTrue(self.policy.tick())
        self.start.assert_called_once_with()

    def test_disabling_pending_boot_cancels_it_permanently(self):
        self.config['auto_start'] = True
        self.busy = True
        self.policy.tick()
        self.config['auto_start'] = False
        self.busy = False
        self.policy.tick()
        self.config['auto_start'] = True
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_boot_waits_for_old_reader(self):
        self.config['auto_start'] = True
        self.reader = Mock()
        self.reader.is_alive.return_value = True
        self.assertFalse(self.policy.tick())
        self.reader.is_alive.return_value = False
        self.assertTrue(self.policy.tick())

    def test_existing_managed_process_satisfies_boot_without_later_restart(self):
        self.config.update(auto_start=True, auto_restart=True)
        self.process = FakeProcess()
        self.assertFalse(self.policy.tick())
        self.process.returncode = 1
        self.now = 1000
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_existing_saved_pid_satisfies_boot_without_signals(self):
        self.config.update(auto_start=True, server_pid=9876)
        self.pid_exists.return_value = True
        with patch('os.kill') as kill:
            self.assertFalse(self.policy.tick())
            self.pid_exists.return_value = False
            self.assertFalse(self.policy.tick())
            kill.assert_not_called()
        self.start.assert_not_called()

    def test_only_remembered_nonzero_exit_schedules_recovery(self):
        self.config['auto_restart'] = True
        self.process = FakeProcess(returncode=1)
        self.policy.tick()
        self.now = 100
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()
        self.policy.track(self.process)
        self.policy.tick()
        self.now += 10
        self.assertTrue(self.policy.tick())

    def test_crash_recovery_waits_ten_seconds(self):
        self.config['auto_restart'] = True
        self.track(returncode=-9)
        self.assertFalse(self.policy.tick())
        self.now = 9.99
        self.assertFalse(self.policy.tick())
        self.now = 10
        self.assertTrue(self.policy.tick())
        self.start.assert_called_once_with()

    def test_clean_exit_never_recovers_even_with_both_flags(self):
        self.config.update(auto_start=True, auto_restart=True)
        self.track(returncode=0)
        self.policy.tick()
        self.now = 1000
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_manual_stop_cancels_pending_crash_and_boot(self):
        self.config.update(auto_start=True, auto_restart=True)
        self.track(returncode=1)
        self.policy.tick()
        self.policy.stop()
        self.now = 1000
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_manual_stop_before_first_tick_cancels_boot(self):
        self.config['auto_start'] = True
        self.policy.stop()
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_manual_launch_consumes_boot_before_first_tick(self):
        self.config.update(auto_start=True, auto_restart=False)
        self.track(returncode=1)
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_manual_start_replaces_pending_old_crash(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.tick()
        replacement = self.track()
        self.now = 10
        self.assertFalse(self.policy.tick())
        self.assertIs(self.process, replacement)
        self.start.assert_not_called()

    def test_disabled_recovery_cancels_pending_crash_after_tick(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.tick()
        self.config['auto_restart'] = False
        self.policy.tick()
        self.config['auto_restart'] = True
        self.now = 20
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_immediate_cancel_blocks_disable_reenable_between_ticks(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.tick()
        self.config['auto_restart'] = False
        self.policy.cancel_pending_restart()
        self.config['auto_restart'] = True
        self.now = 20
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_immediate_cancel_also_discards_unobserved_dead_process(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.cancel_pending_restart()
        self.policy.tick()
        self.now = 20
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_immediate_cancel_keeps_live_process_for_future_crash(self):
        self.track()
        self.policy.cancel_pending_restart()
        self.config['auto_restart'] = True
        self.crash_and_restart()
        self.start.assert_called_once_with()

    def test_recovery_flag_requires_exact_true(self):
        for flag in ('true', 1, 'false'):
            with self.subTest(flag=flag):
                self.setUp()
                self.config['auto_restart'] = flag
                self.track(returncode=1)
                self.policy.tick()
                self.now = 100
                self.assertFalse(self.policy.tick())
                self.start.assert_not_called()

    def test_busy_operation_does_not_spend_retry_budget(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.tick()
        self.now = 10
        self.busy = True
        for _ in range(10):
            self.assertFalse(self.policy.tick())
        self.start.assert_not_called()
        self.busy = False
        self.assertTrue(self.policy.tick())
        self.crash_and_restart()
        self.crash_and_restart()
        self.assertEqual(self.start.call_count, 3)

    def test_busy_reader_or_live_pid_does_not_spend_retry_budget(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.tick()
        self.now = 10
        self.reader = Mock()
        self.reader.is_alive.return_value = True
        for _ in range(5):
            self.assertFalse(self.policy.tick())
        self.reader.is_alive.return_value = False
        self.pid_exists.return_value = True
        self.assertFalse(self.policy.tick())
        self.pid_exists.return_value = False
        self.assertTrue(self.policy.tick())
        self.crash_and_restart()
        self.crash_and_restart()
        self.assertEqual(self.start.call_count, 3)

    def test_other_live_managed_process_prevents_recovery(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.tick()
        self.process = FakeProcess(pid=5555)
        self.now = 10
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_three_rapid_retries_exhaust_until_manual_start(self):
        self.config['auto_restart'] = True
        self.track()
        for _ in range(3):
            self.crash_and_restart()
        self.process.returncode = 1
        self.assertFalse(self.policy.tick())
        self.now += 1000
        self.assertFalse(self.policy.tick())
        self.assertEqual(self.start.call_count, 3)
        self.logger.error.assert_called_once()
        self.track()
        self.crash_and_restart()
        self.assertEqual(self.start.call_count, 4)

    def test_stable_run_expires_old_attempts_before_limit_is_exhausted(self):
        self.config['auto_restart'] = True
        self.track()
        for _ in range(3):
            self.crash_and_restart()
        self.now += 301
        self.crash_and_restart()
        self.assertEqual(self.start.call_count, 4)
        self.logger.error.assert_not_called()

    def test_failed_launch_without_popen_is_still_bounded(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.start.side_effect = RuntimeError('Java executable missing')
        for _ in range(3):
            self.assertFalse(self.policy.tick())
            self.now += 10
            self.assertFalse(self.policy.tick())
        self.policy.tick()
        self.now += 1000
        self.assertFalse(self.policy.tick())
        self.assertEqual(self.start.call_count, 3)
        self.logger.error.assert_called_once()

    def test_boot_failure_does_not_repeat_without_tracked_crash(self):
        self.config.update(auto_start=True, auto_restart=True)
        self.start.side_effect = None
        self.start.return_value = False
        self.assertFalse(self.policy.tick())
        self.now += 1000
        self.assertFalse(self.policy.tick())
        self.start.assert_called_once_with()

    def test_fast_boot_crash_can_recover_when_separately_enabled(self):
        self.config.update(auto_start=True, auto_restart=True)
        def fast_exit():
            self.start_process()
            self.process.returncode = 1
            return False
        self.start.side_effect = fast_exit
        self.assertFalse(self.policy.tick())
        self.start.side_effect = self.start_process
        self.policy.tick()
        self.now += 10
        self.assertTrue(self.policy.tick())
        self.assertEqual(self.start.call_count, 2)

    def test_stop_does_not_reset_exhausted_budget_but_manual_track_does(self):
        self.config['auto_restart'] = True
        self.track()
        for _ in range(3):
            self.crash_and_restart()
        self.process.returncode = 1
        self.policy.tick()
        self.policy.stop()
        self.track(returncode=1, automatic=True)
        self.now += 1000
        self.assertFalse(self.policy.tick())
        self.assertEqual(self.start.call_count, 3)
        self.track()
        self.crash_and_restart()
        self.assertEqual(self.start.call_count, 4)

    def test_missing_server_configuration_cancels_pending_recovery(self):
        self.config['auto_restart'] = True
        self.track(returncode=1)
        self.policy.tick()
        self.config['is_setup'] = False
        self.policy.tick()
        self.config['is_setup'] = True
        self.now += 100
        self.assertFalse(self.policy.tick())
        self.start.assert_not_called()

    def test_run_uses_interruptible_one_second_wait_and_logs_errors(self):
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.side_effect = [False, True]
        with patch.object(self.policy, 'tick', side_effect=[RuntimeError('temporary failure'), False]) as tick:
            self.policy.run(stop_event)
        self.assertEqual(tick.call_count, 2)
        self.assertEqual(stop_event.wait.call_args_list[0].args, (1,))
        self.assertEqual(stop_event.wait.call_args_list[1].args, (1,))
        self.logger.exception.assert_called_once()

    def test_stopped_run_does_no_work(self):
        stop_event = Mock()
        stop_event.is_set.return_value = True
        with patch.object(self.policy, 'tick') as tick:
            self.policy.run(stop_event)
        tick.assert_not_called()
        stop_event.wait.assert_not_called()

    def test_configuration_read_error_releases_shared_lock(self):
        self.policy.tick()
        self.policy.load_config = Mock(side_effect=OSError('configuration temporarily unavailable'))
        with self.assertRaisesRegex(OSError, 'temporarily unavailable'):
            self.policy.tick()
        self.assertFalse(self.lock_held)


if __name__ == '__main__':
    unittest.main()
