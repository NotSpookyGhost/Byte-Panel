"""Bounded console-tail reads and incremental Minecraft startup detection."""

import os
import stat
import threading


_READ_CHUNK_BYTES = 8192
_MAX_SCAN_BYTES = 256 * 1024
_STARTUP_MARKERS = (b'Done (', b'For help, type')
_MARKER_OVERLAP = max(map(len, _STARTUP_MARKERS)) - 1
_HEAD_BYTES = 64


def read_tail(path, max_lines=100, max_bytes=_MAX_SCAN_BYTES):
    """Return the newest lines, reading at most max_bytes from a regular file.

    Newlines, including CRLF, are preserved. If the byte limit cuts a line,
    that partial first line is omitted when newer complete lines exist. A
    single oversized final line is shown as a bounded suffix instead. Missing
    or unreadable files raise OSError so the caller can choose its UI message.
    """
    if max_lines <= 0 or max_bytes <= 0:
        return ''
    chunks = []
    newline_count = 0
    remaining = max_bytes
    with open(path, 'rb', buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return ''
        position = info.st_size
        while position and remaining and newline_count <= max_lines:
            amount = min(position, remaining, _READ_CHUNK_BYTES)
            position -= amount
            stream.seek(position)
            chunk = stream.read(amount)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= amount
            newline_count += chunk.count(b'\n')

    data = b''.join(reversed(chunks))
    if position and data:
        first_newline = data.find(b'\n')
        if first_newline >= 0 and first_newline + 1 < len(data):
            data = data[first_newline + 1:]
        else:
            # Do not manufacture replacement characters solely because a
            # byte-bounded suffix begins halfway through a UTF-8 character.
            cut = 0
            while cut < min(3, len(data)) and data[cut] & 0xC0 == 0x80:
                cut += 1
            data = data[cut:]
    return b''.join(data.splitlines(keepends=True)[-max_lines:]).decode(
        'utf-8', errors='replace')


class StartupLogStatus:
    """Cache startup readiness for one active log path/process identity.

    Before a marker is found, each call reads at most 256 KiB, advancing from
    its previous offset rather than rereading the entire log. Rotation,
    truncation and detected in-place rewrites restart an unfinished scan.
    Once ready, the same PID/path stays ready without any further file I/O,
    including after log rotation. A changed PID/path or reset() clears it.

    The launcher must truncate its session log before starting a new process
    (or use a fresh path); old startup markers in an append-only multi-session
    log cannot be attributed to a PID from their text alone.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self._key = None
        self._ready = False
        self._reset_scan()

    def _reset_scan(self, identity=None):
        self._identity = identity
        self._offset = 0
        self._head = b''
        self._carry = b''

    def reset(self):
        """Forget the session, for example when explicitly launching a server."""
        with self._lock:
            self._reset()

    def ready(self, path, pid):
        """Return observed readiness; missing/unreadable logs return False."""
        with self._lock:
            try:
                if not pid:
                    self._reset()
                    return False
                key = (os.path.abspath(os.fspath(path)), pid)
                if key != self._key:
                    self._reset()
                    self._key = key
                if self._ready:
                    return True
                with open(path, 'rb', buffering=0) as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        return False
                    identity = (info.st_dev, info.st_ino)
                    budget = _MAX_SCAN_BYTES
                    if identity != self._identity or info.st_size < self._offset:
                        self._reset_scan(identity)
                    elif self._offset:
                        # Detect a truncate-and-regrow or in-place rewrite
                        # even when the new size has already caught up. These
                        # tiny verification reads count toward the same budget.
                        stream.seek(0)
                        head = stream.read(len(self._head))
                        budget -= len(self._head)
                        if head != self._head:
                            self._reset_scan(identity)
                        elif self._carry:
                            stream.seek(self._offset - len(self._carry))
                            carry = stream.read(len(self._carry))
                            budget -= len(self._carry)
                            if carry != self._carry:
                                self._reset_scan(identity)

                    stream.seek(self._offset)
                    while budget > 0 and self._offset < info.st_size:
                        amount = min(_READ_CHUNK_BYTES, budget,
                                     info.st_size - self._offset)
                        chunk = stream.read(amount)
                        budget -= amount
                        if not chunk:
                            break
                        if len(self._head) < _HEAD_BYTES:
                            self._head += chunk[:_HEAD_BYTES - len(self._head)]
                        self._offset += len(chunk)
                        candidate = self._carry + chunk
                        if any(marker in candidate for marker in _STARTUP_MARKERS):
                            self._ready = True
                            return True
                        self._carry = candidate[-_MARKER_OVERLAP:]
                return False
            except (OSError, ValueError, TypeError):
                return False
