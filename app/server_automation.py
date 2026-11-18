"""Opt-in boot startup and bounded crash recovery, without import-time threads."""

from collections import deque
import threading
import time

import psutil

from server_backups import BackupBusy


class ServerAutomation:
    """Call track for every launched process and stop before every intentional stop.

    start_server must launch through the trusted internal path and call track with
    automatic=True as soon as Popen exists. tick owns the shared operation lock;
    that callback must not try to acquire the same non-reentrant operation lock.
    """

    def __init__(self, load_config, start_server, get_process, get_log_thread,
                 exclusive, logger, clock=time.monotonic, pid_exists=psutil.pid_exists,
                 restart_delay=10, max_restarts=3, restart_window=300):
        if restart_delay < 0 or max_restarts < 1 or restart_window <= 0:
            raise ValueError('Invalid automatic restart limits.')
        self.load_config = load_config
        self.start_server = start_server
        self.get_process = get_process
        self.get_log_thread = get_log_thread
        self.exclusive = exclusive
        self.logger = logger
        self.clock = clock
        self.pid_exists = pid_exists
        self.restart_delay = restart_delay
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self._state_lock = threading.RLock()
        self._tracked = None
        self._pending_at = None
        self._attempts = deque()
        self._exhausted = False
        self._boot_evaluated = False
        self._boot_requested = False

    def track(self, process, automatic=False):
        """A new process replaces pending recovery for its predecessor."""
        with self._state_lock:
            self._tracked = process
            self._pending_at = None
            self._boot_evaluated = True
            self._boot_requested = False
            if not automatic:
                self._attempts.clear()
                self._exhausted = False

    def stop(self):
        """Cancel startup/recovery before a manual or other intentional shutdown."""
        with self._state_lock:
            self._tracked = None
            self._pending_at = None
            self._boot_evaluated = True
            self._boot_requested = False

    def cancel_pending_restart(self):
        """Apply a disabled recovery setting immediately, even between ticks."""
        with self._state_lock:
            self._pending_at = None
            if self._tracked is not None and self._tracked.poll() is not None:
                self._tracked = None

    @staticmethod
    def _configured(config):
        return config.get('is_setup') is True and bool(config.get('mc_version'))

    def _busy_reason(self, config):
        process = self.get_process()
        if process is not None and process.poll() is None:
            return 'managed process'
        pid = config.get('server_pid')
        if pid is not None:
            try:
                if isinstance(pid, bool) or int(pid) <= 0:
                    raise ValueError('Invalid PID')
                pid = int(pid)
            except (TypeError, ValueError) as error:
                raise RuntimeError('Cannot verify the configured Minecraft process for automatic startup.') from error
            if self.pid_exists(pid):
                return 'configured process'
        reader = self.get_log_thread()
        if reader is not None and reader.is_alive():
            return 'log reader'
        return None

    def _within_budget(self, now):
        while self._attempts and self._attempts[0] <= now - self.restart_window:
            self._attempts.popleft()
        if self._exhausted:
            return False
        if len(self._attempts) >= self.max_restarts:
            self._exhausted = True
            self._pending_at = None
            self._tracked = None
            self.logger.error('Automatic Minecraft restart limit reached (%s attempts in %s seconds). '
                              'Recovery is paused until a manual start.',
                              self.max_restarts, self.restart_window)
            return False
        return True

    def _start(self):
        try:
            started = bool(self.start_server())
        except Exception:
            self.logger.exception('Automatic Minecraft startup failed')
            return False
        if not started:
            self.logger.warning('Automatic Minecraft startup did not complete. Check the server console.')
        return started

    def _tick_locked(self):
        config = self.load_config()
        if not self._configured(config):
            self._boot_requested = False
            self._pending_at = None
            self._tracked = None
            return False
        if config.get('auto_start') is not True:
            self._boot_requested = False
        if self._boot_requested:
            busy = self._busy_reason(config)
            if busy in ('managed process', 'configured process'):
                # An existing server satisfies startup; never start a second copy or
                # turn this boot request into recovery after its later manual shutdown.
                self._boot_requested = False
                return False
            if busy:
                return False
            self._boot_requested = False
            self.logger.info('Starting Minecraft automatically when Byte Panel starts.')
            return self._start()

        if self._tracked is None:
            return False
        exit_code = self._tracked.poll()
        if exit_code is None:
            self._pending_at = None
            return False
        if exit_code == 0 or config.get('auto_restart') is not True:
            # Disabling recovery cancels the old crash, rather than resurrecting it
            # when the setting is enabled again later in this panel session.
            self._tracked = None
            self._pending_at = None
            return False
        now = self.clock()
        if not self._within_budget(now):
            return False
        if self._pending_at is None:
            self._pending_at = now + self.restart_delay
            self.logger.warning('Minecraft exited with code %s. Automatic restart scheduled in %s seconds.',
                                exit_code, self.restart_delay)
        if now < self._pending_at or self._busy_reason(config):
            return False
        # Do not spend an attempt while a backup, detached process, or old reader
        # prevents startup. Keep the crashed process until track replaces it so a
        # launch failure before Popen can retry within the same bounded budget.
        self._attempts.append(now)
        self._pending_at = None
        self.logger.info('Starting Minecraft crash recovery attempt %s of %s.',
                         len(self._attempts), self.max_restarts)
        return self._start()

    def tick(self):
        """Run one policy check; return True only when a start request succeeds."""
        with self._state_lock:
            if not self._boot_evaluated:
                config = self.load_config()
                self._boot_requested = self._configured(config) and config.get('auto_start') is True
                self._boot_evaluated = True
        try:
            with self.exclusive():
                with self._state_lock:
                    return self._tick_locked()
        except BackupBusy:
            return False

    def run(self, stop_event):
        """One-second interruptible polling; the owner explicitly starts the thread."""
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                self.logger.exception('Minecraft automation check failed; retrying later')
            if stop_event.wait(1):
                break
