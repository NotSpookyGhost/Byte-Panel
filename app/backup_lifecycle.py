"""Gracefully pause Minecraft while an exclusively locked backup is created."""

from contextlib import contextmanager
import subprocess

import psutil


class BackupLifecycle:
    """The caller must hold its server-operation lock for the entire context."""

    def __init__(self, get_process, get_log_thread, load_config, save_config,
                 start_server, logger, stop_timeout=180, log_timeout=10, before_stop=None):
        self.get_process = get_process
        self.get_log_thread = get_log_thread
        self.load_config = load_config
        self.save_config = save_config
        self.start_server = start_server
        self.logger = logger
        self.stop_timeout = stop_timeout
        self.log_timeout = log_timeout
        self.before_stop = before_stop or (lambda: None)

    def _report(self, progress, message):
        try:
            progress(message=message)
        except Exception:
            # A progress-display failure must not leave a safely stopped server down.
            self.logger.exception('Could not update shutdown-backup progress')

    def _drain_reader(self, reader):
        if reader is not None:
            reader.join(timeout=self.log_timeout)
            if reader.is_alive():
                raise RuntimeError('Minecraft stopped, but its log reader is still finishing. '
                                   'No backup was created and the server was not restarted. '
                                   'Wait, then start Minecraft manually before retrying.')

    def _mark_offline(self):
        # The log reader or settings page may have saved newer unrelated settings.
        config = self.load_config()
        config.update(is_running=False, server_pid=None, target_state='Offline')
        self.save_config(config)

    def _check_detached_process(self, config, process, running):
        pid = config.get('server_pid')
        if pid is None:
            return
        try:
            if isinstance(pid, bool) or int(pid) <= 0:
                raise ValueError('Invalid PID')
            pid = int(pid)
            if (not running or pid != process.pid) and psutil.pid_exists(pid):
                raise RuntimeError('Minecraft has a running process that the panel cannot '
                                   'gracefully control. Stop it manually before making an '
                                   'offline backup. No backup was created.')
        except (ValueError, TypeError, OSError, psutil.Error) as error:
            raise RuntimeError('Unable to verify the configured Minecraft process. '
                               'Stop Minecraft manually before backing up.') from error

    @contextmanager
    def offline(self, progress=None):
        progress = progress or (lambda **values: None)
        process = self.get_process()
        reader = self.get_log_thread()
        config = self.load_config()
        running = process is not None and process.poll() is None
        self._check_detached_process(config, process, running)
        if running and config.get('target_state') == 'Shutting Down':
            raise RuntimeError('Minecraft is already shutting down. Wait until it is offline '
                               'before backing up; the panel will not restart this shutdown.')

        stop_attempted = False
        reader_drained = False
        reader_failed = False
        archive_created = False
        failure = None
        try:
            if running:
                # This stop belongs to the backup workflow, not crash recovery.
                self.before_stop()
                self._report(progress, 'Stopping Minecraft gracefully before the backup...')
                config = self.load_config()
                config.update(target_state='Shutting Down', server_pid=process.pid,
                              is_running=True)
                self.save_config(config)
                stop_attempted = True
                try:
                    # Minecraft's stop command saves worlds and shuts down plugins.
                    # Never replace it with kill/terminate or a fixed-length sleep.
                    process.stdin.write('stop\n')
                    process.stdin.flush()
                except (OSError, ValueError, AttributeError) as error:
                    raise RuntimeError('Unable to send Minecraft its graceful stop command. '
                                       'No backup was created. Check the console and stop '
                                       'the server manually before retrying.') from error
                try:
                    process.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError('Minecraft did not finish shutting down within '
                                       f'{self.stop_timeout} seconds. No backup was created '
                                       'and the panel did not force-kill it. Check the console; '
                                       'if it stops later, start it manually.') from error
                if process.poll() is None:
                    raise RuntimeError('Minecraft is still running. No backup was created '
                                       'and the panel did not force-kill it.')

            self._report(progress, 'Waiting for Minecraft file and log activity to finish...')
            try:
                self._drain_reader(reader)
                reader_drained = True
            except Exception:
                reader_failed = True
                raise
            self._mark_offline()
            if running and process.poll() != 0:
                raise RuntimeError('Minecraft exited unexpectedly while stopping '
                                   f'(exit code {process.poll()}). No backup was created '
                                   'and the server was not restarted. Check the console '
                                   'before starting it manually.')
            self._report(progress, 'Minecraft is offline. Preparing the backup...')
            yield
            archive_created = True
        except BaseException as error:
            failure = error
            raise
        finally:
            # Even an archive failure should restore the prior running state, but only
            # after a clean exit and a drained reader. Never start beside a live process.
            recover = running and stop_attempted and process.poll() == 0 and not reader_failed
            if recover and not reader_drained:
                try:
                    self._drain_reader(reader)
                    reader_drained = True
                    self._mark_offline()
                except Exception:
                    recover = False
                    self.logger.exception('Cannot safely restart Minecraft after a failed backup shutdown')
            if recover:
                self._report(progress, 'Starting Minecraft again...')
                try:
                    if not self.start_server():
                        raise RuntimeError('The Minecraft start request failed.')
                except Exception as error:
                    self.logger.exception('Minecraft could not be restarted after backup')
                    if archive_created:
                        message = ('Backup created, but Minecraft could not '
                                   'be restarted. Start it manually and check the console.')
                    else:
                        message = (f'Backup failed: {failure}. Minecraft also could not be '
                                   'restarted. Start it manually and check the console.')
                    raise RuntimeError(message) from error
