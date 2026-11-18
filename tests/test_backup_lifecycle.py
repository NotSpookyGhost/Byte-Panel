"""Shutdown-backup lifecycle tests: fake processes only, never real Minecraft."""

from contextlib import nullcontext
import logging
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from backup_lifecycle import BackupLifecycle
from server_automation import ServerAutomation


class BackupLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.config = dict(server_pid=None, is_running=False, target_state='Offline',
                           server_name='Original server')
        self.process = None
        self.reader = None
        self.start = Mock(side_effect=lambda: self.events.append('start') or True)
        self.logger = Mock(spec=logging.Logger)
        self.lifecycle = BackupLifecycle(lambda: self.process, lambda: self.reader,
                                         lambda: self.config.copy(), self.save,
                                         self.start, self.logger, stop_timeout=3, log_timeout=2)
        self.pid_exists = patch('backup_lifecycle.psutil.pid_exists', return_value=False).start()
        self.addCleanup(patch.stopall)

    def save(self, config):
        self.config.clear()
        self.config.update(config)

    def online(self):
        process = self.process = Mock()
        process.pid = 1234
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode
        process.stdin.write.side_effect = lambda command: self.events.append(command)
        process.stdin.flush.side_effect = lambda: self.events.append('flush')
        def wait(timeout):
            self.assertEqual(timeout, 3)
            self.assertEqual(self.config['server_pid'], 1234)
            self.assertTrue(self.config['is_running'])
            self.assertEqual(self.config['target_state'], 'Shutting Down')
            self.events.append('wait')
            process.returncode = 0
            return 0
        process.wait.side_effect = wait
        reader = self.reader = Mock()
        reader.join.side_effect = lambda timeout: self.events.append('join')
        reader.is_alive.return_value = False
        self.config.update(server_pid=1234, is_running=True, target_state='Online')
        return process, reader

    def assert_no_force(self):
        self.process.kill.assert_not_called()
        self.process.terminate.assert_not_called()

    def recovery_policy(self):
        self.config.update(is_setup=True, mc_version='1.21.1', auto_restart=True)
        policy = ServerAutomation(lambda: self.config.copy(), self.start,
                                  lambda: self.process, lambda: self.reader,
                                  nullcontext, self.logger, clock=lambda: 1000,
                                  pid_exists=lambda pid: False)
        policy.track(self.process)
        self.lifecycle.before_stop = policy.stop
        return policy

    def test_before_stop_cancels_recovery_before_writing_shutdown_command(self):
        process, _ = self.online()
        self.lifecycle.before_stop = lambda: self.events.append('cancel recovery')
        with self.lifecycle.offline():
            self.events.append('archive')
        self.assertEqual(self.events[:2], ['cancel recovery', 'stop\n'])
        self.start.assert_called_once_with()

    def test_timed_out_backup_stop_cannot_later_trigger_crash_recovery(self):
        process, _ = self.online()
        policy = self.recovery_policy()
        process.wait.side_effect = subprocess.TimeoutExpired('java', 3)
        with self.assertRaisesRegex(RuntimeError, 'did not finish shutting down'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        # Minecraft eventually exits abnormally after the backup has already failed.
        process.returncode = 1
        self.assertFalse(policy.tick())
        policy.clock = lambda: 1011
        self.assertFalse(policy.tick())
        self.start.assert_not_called()

    def test_abnormal_backup_stop_does_not_trigger_crash_recovery(self):
        process, _ = self.online()
        policy = self.recovery_policy()
        def crashed(timeout):
            process.returncode = 137
            return 137
        process.wait.side_effect = crashed
        with self.assertRaisesRegex(RuntimeError, 'exit code 137'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        self.assertFalse(policy.tick())
        policy.clock = lambda: 1011
        self.assertFalse(policy.tick())
        self.start.assert_not_called()

    def test_running_server_stops_drains_archives_then_requests_start(self):
        process, reader = self.online()
        progress = Mock()
        with self.lifecycle.offline(progress):
            self.assertEqual(self.config['target_state'], 'Offline')
            self.assertIsNone(self.config['server_pid'])
            self.assertFalse(self.config['is_running'])
            self.events.append('archive')
            self.start.assert_not_called()
        self.assertEqual(self.events, ['stop\n', 'flush', 'wait', 'join', 'archive', 'start'])
        reader.join.assert_called_once_with(timeout=2)
        self.start.assert_called_once_with()
        self.assertIn('Starting Minecraft again...', [call.kwargs['message'] for call in progress.call_args_list])
        self.assert_no_force()

    def test_already_offline_server_stays_offline_and_drains_old_reader(self):
        self.reader = Mock()
        self.reader.is_alive.return_value = False
        with self.lifecycle.offline():
            self.events.append('archive')
        self.reader.join.assert_called_once_with(timeout=2)
        self.start.assert_not_called()
        self.assertEqual(self.events, ['archive'])

    def test_offline_without_reader_never_starts(self):
        with self.lifecycle.offline():
            pass
        self.start.assert_not_called()

    def test_configured_live_detached_process_is_rejected_without_signals(self):
        self.config.update(server_pid=9876, is_running=True, target_state='Online')
        self.pid_exists.return_value = True
        with patch('os.kill') as kill:
            with self.assertRaisesRegex(RuntimeError, 'cannot gracefully control'):
                with self.lifecycle.offline():
                    self.fail('Archive must not start')
            kill.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(self.config['server_pid'], 9876)

    def test_detached_second_process_is_rejected_even_with_managed_process(self):
        process, _ = self.online()
        self.config['server_pid'] = 9876
        self.pid_exists.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'cannot gracefully control'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        process.stdin.write.assert_not_called()
        self.start.assert_not_called()

    def test_stale_dead_pid_can_be_backed_up_without_starting(self):
        self.config.update(server_pid=9876, is_running=True, target_state='Online')
        with self.lifecycle.offline():
            self.assertIsNone(self.config['server_pid'])
        self.start.assert_not_called()

    def test_stale_shutdown_state_with_dead_process_archives_without_restart(self):
        process, reader = self.online()
        process.returncode = 0
        self.config.update(server_pid=None, is_running=False, target_state='Shutting Down')
        with self.lifecycle.offline():
            self.assertEqual(self.config['target_state'], 'Offline')
            self.events.append('archive')
        reader.join.assert_called_once_with(timeout=2)
        process.stdin.write.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(self.events, ['join', 'archive'])

    def test_already_requested_shutdown_is_not_restarted(self):
        process, _ = self.online()
        self.config['target_state'] = 'Shutting Down'
        with self.assertRaisesRegex(RuntimeError, 'already shutting down'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        process.stdin.write.assert_not_called()
        self.start.assert_not_called()

    def test_shutdown_timeout_aborts_without_force_kill_or_restart(self):
        process, reader = self.online()
        process.wait.side_effect = subprocess.TimeoutExpired('java', 3)
        with self.assertRaisesRegex(RuntimeError, 'did not finish shutting down'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        reader.join.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(self.config['server_pid'], process.pid)
        self.assert_no_force()

    def test_broken_stdin_while_alive_aborts_without_restart(self):
        process, _ = self.online()
        process.stdin.write.side_effect = BrokenPipeError('closed')
        with self.assertRaisesRegex(RuntimeError, 'graceful stop command'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        process.wait.assert_not_called()
        self.start.assert_not_called()
        self.assert_no_force()

    def test_stdin_flush_error_after_clean_exit_recovers_without_archive(self):
        process, reader = self.online()
        def closed():
            process.returncode = 0
            raise BrokenPipeError('closed while flushing')
        process.stdin.flush.side_effect = closed
        with self.assertRaisesRegex(RuntimeError, 'graceful stop command'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        reader.join.assert_called_once_with(timeout=2)
        self.start.assert_called_once_with()
        self.assert_no_force()

    def test_nonzero_shutdown_exit_aborts_without_restart(self):
        process, reader = self.online()
        def crashed(timeout):
            process.returncode = 137
            return 137
        process.wait.side_effect = crashed
        with self.assertRaisesRegex(RuntimeError, 'exit code 137'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        self.assertEqual(self.config['target_state'], 'Offline')
        self.start.assert_not_called()
        reader.join.assert_called_once_with(timeout=2)
        self.assert_no_force()

    def test_alive_log_reader_prevents_archive_and_restart(self):
        process, reader = self.online()
        reader.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'log reader is still finishing'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        self.start.assert_not_called()
        reader.join.assert_called_once_with(timeout=2)
        self.assert_no_force()

    def test_archive_failure_still_restarts_then_preserves_original_error(self):
        self.online()
        failure = OSError('Disk is full')
        with self.assertRaises(OSError) as caught:
            with self.lifecycle.offline():
                raise failure
        self.assertIs(caught.exception, failure)
        self.start.assert_called_once_with()

    def test_successful_archive_and_failed_restart_are_distinguished(self):
        self.online()
        self.start.side_effect = None
        self.start.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'Backup created, but Minecraft'):
            with self.lifecycle.offline():
                self.events.append('archive')
        self.assertIn('archive', self.events)

    def test_failed_archive_and_failed_restart_report_both_errors(self):
        self.online()
        self.start.side_effect = RuntimeError('bad java path')
        with self.assertRaisesRegex(RuntimeError, 'Disk is full.*also could not be restarted'):
            with self.lifecycle.offline():
                raise OSError('Disk is full')
        self.start.assert_called_once_with()

    def test_state_write_failure_after_verified_stop_still_attempts_restart(self):
        self.online()
        def failing_save(config):
            if config.get('target_state') == 'Offline':
                raise OSError('settings volume unavailable')
            self.save(config)
        self.lifecycle.save_config = failing_save
        with self.assertRaisesRegex(OSError, 'settings volume unavailable'):
            with self.lifecycle.offline():
                self.fail('Archive must not start')
        self.start.assert_called_once_with()

    def test_progress_failure_cannot_prevent_recovery(self):
        self.online()
        with self.lifecycle.offline(Mock(side_effect=RuntimeError('no progress consumer'))):
            self.events.append('archive')
        self.start.assert_called_once_with()
        self.logger.exception.assert_called()

    def test_shutdown_reloads_config_before_marking_offline(self):
        process, reader = self.online()
        def reader_finished(timeout):
            self.config['server_name'] = 'Changed while shutting down'
        reader.join.side_effect = reader_finished
        with self.lifecycle.offline():
            self.assertEqual(self.config['server_name'], 'Changed while shutting down')
        self.assertEqual(self.config['server_name'], 'Changed while shutting down')


if __name__ == '__main__':
    unittest.main()
