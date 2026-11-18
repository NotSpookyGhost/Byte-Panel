"""Bounded file I/O tests; no live server or production logs are used."""

import builtins
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from log_reader import StartupLogStatus, read_tail


SCAN_BYTES = 256 * 1024


class CountedFile:
    def __init__(self, stream, reads):
        self.stream = stream
        self.reads = reads

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, amount=-1):
        if amount < 0:
            raise AssertionError('Unbounded reads are forbidden.')
        if amount > 8192:
            raise AssertionError('A read allocated more than one bounded chunk.')
        self.reads.append((self.stream.tell(), amount))
        return self.stream.read(amount)


class LogFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='byte-log-reader-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'console.log'
        self.reads = []

    def write(self, data):
        self.path.write_bytes(data)

    def append(self, data):
        with self.path.open('ab') as stream:
            stream.write(data)

    def counted_open(self, *args, **kwargs):
        return CountedFile(builtins.open(*args, **kwargs), self.reads)

    def count_reads(self):
        self.reads.clear()
        return patch('log_reader.open', side_effect=self.counted_open, create=True)

    def assert_bounded(self, limit=SCAN_BYTES):
        self.assertLessEqual(sum(amount for _, amount in self.reads), limit)


class ReadTailTests(LogFixture):
    def test_empty_and_short_logs(self):
        for data in (b'', b'one', b'one\ntwo\n', b'one\r\ntwo\r\n'):
            with self.subTest(data=data):
                self.write(data)
                self.assertEqual(read_tail(self.path), data.decode('utf-8'))

    def test_last_lines_with_and_without_trailing_newline(self):
        for ending in (b'', b'\n'):
            self.write(b'first\nsecond\nthird' + ending)
            self.assertEqual(read_tail(self.path, max_lines=2),
                             (b'second\nthird' + ending).decode())

    def test_preserves_crlf_and_utf8(self):
        self.write('old\r\n玩家 joins 🌍\r\nlast\r\n'.encode())
        self.assertEqual(read_tail(self.path, max_lines=2), '玩家 joins 🌍\r\nlast\r\n')

    def test_reverse_reads_do_not_scan_large_prefix(self):
        self.write(b'old\n' * 500000 + b'new\n' * 100)
        with self.count_reads():
            self.assertEqual(read_tail(self.path), 'new\n' * 100)
        self.assert_bounded()
        self.assertLessEqual(sum(n for _, n in self.reads), 8192)
        self.assertGreater(min(offset for offset, _ in self.reads), 1000000)

    def test_byte_limit_discards_partial_first_line(self):
        self.write(b'older\n' + b'x' * 2000 + b'\nlatest\n')
        with self.count_reads():
            self.assertEqual(read_tail(self.path, max_bytes=100), 'latest\n')
        self.assert_bounded(100)

    def test_single_huge_line_returns_bounded_utf8_suffix(self):
        for ending in ('', '\n'):
            self.write(('🌍' * 1000 + ending).encode())
            with self.count_reads():
                result = read_tail(self.path, max_bytes=103)
            self.assertTrue(result)
            self.assertNotIn('\ufffd', result)
            self.assertLessEqual(len(result.encode()), 103)
            self.assertTrue(('🌍' * 1000 + ending).endswith(result))
            self.assert_bounded(103)

    def test_reverse_chunk_boundary_and_line_limit(self):
        lines = [f'entry {number} ' + 'x' * 100 for number in range(250)]
        self.write(('\n'.join(lines) + '\n').encode())
        with self.count_reads():
            result = read_tail(self.path, max_lines=100)
        self.assertEqual(result, '\n'.join(lines[-100:]) + '\n')
        self.assert_bounded()

    def test_invalid_utf8_is_displayed_without_crashing(self):
        self.write(b'ok\ninvalid \xff\n')
        self.assertEqual(read_tail(self.path), 'ok\ninvalid \ufffd\n')

    def test_nonpositive_limits_do_not_open_file(self):
        with patch('log_reader.open', side_effect=AssertionError, create=True):
            self.assertEqual(read_tail(self.path, max_lines=0), '')
            self.assertEqual(read_tail(self.path, max_lines=-1), '')
            self.assertEqual(read_tail(self.path, max_bytes=0), '')

    def test_missing_file_preserves_error_for_console_fallback(self):
        with self.assertRaises(FileNotFoundError):
            read_tail(self.path)


class StartupStatusTests(LogFixture):
    def setUp(self):
        super().setUp()
        self.status = StartupLogStatus()

    def test_supported_startup_markers(self):
        for marker in (b'Done (2.1s)!', b'For help, type "help"'):
            with self.subTest(marker=marker):
                self.status.reset()
                self.write(b'[Server thread/INFO]: ' + marker + b'\n')
                self.assertTrue(self.status.ready(self.path, 123))

    def test_unready_log_does_not_claim_online(self):
        self.write(b'Preparing spawn\nDownloading\n')
        self.assertFalse(self.status.ready(self.path, 123))

    def test_marker_split_across_chunks_and_utf8_bytes(self):
        self.write(b'x' * 8187 + '🌍'.encode() + b'Done (1s)!\n')
        self.assertTrue(self.status.ready(self.path, 123))

    def test_marker_split_across_calls(self):
        self.write(b'x' * (SCAN_BYTES - 3) + b'Done (1s)!\n')
        with self.count_reads():
            self.assertFalse(self.status.ready(self.path, 123))
        self.assert_bounded()
        with self.count_reads():
            self.assertTrue(self.status.ready(self.path, 123))
        self.assert_bounded()

    def test_incremental_budget_advances_instead_of_rereading_whole_file(self):
        self.write(b'x' * (SCAN_BYTES * 3 + 100) + b'For help, type "help"\n')
        results = []
        for _ in range(5):
            with self.count_reads():
                results.append(self.status.ready(self.path, 123))
            self.assert_bounded()
        self.assertEqual(results, [False, False, False, True, True])

    def test_appended_partial_marker_is_preserved(self):
        self.write(b'Preparing\nFor help, ')
        self.assertFalse(self.status.ready(self.path, 123))
        self.append(b'type "help"\n')
        self.assertTrue(self.status.ready(self.path, 123))

    def test_no_new_output_only_checks_small_fingerprints(self):
        self.write(b'Preparing\n' * 200)
        self.assertFalse(self.status.ready(self.path, 123))
        with self.count_reads():
            self.assertFalse(self.status.ready(self.path, 123))
        self.assertLessEqual(sum(amount for _, amount in self.reads), 80)

    def test_cached_ready_performs_no_file_io(self):
        self.write(b'Done (1s)!\n' + b'x' * (SCAN_BYTES * 3))
        self.assertTrue(self.status.ready(self.path, 123))
        with patch('log_reader.open', side_effect=AssertionError, create=True):
            self.assertTrue(self.status.ready(self.path, 123))

    def test_ready_survives_rotation_truncation_and_missing_log(self):
        self.write(b'Done (1s)!\n')
        self.assertTrue(self.status.ready(self.path, 123))
        self.path.replace(self.path.with_suffix('.old'))
        self.write(b'New rotated log\n')
        self.assertTrue(self.status.ready(self.path, 123))
        self.write(b'')
        self.assertTrue(self.status.ready(self.path, 123))
        self.path.unlink()
        self.assertTrue(self.status.ready(self.path, 123))

    def test_changed_pid_resets_readiness_for_new_session_log(self):
        self.write(b'Done (1s)!\n')
        self.assertTrue(self.status.ready(self.path, 123))
        self.write(b'Preparing new process\n')
        self.assertFalse(self.status.ready(self.path, 456))
        self.append(b'Done (2s)!\n')
        self.assertTrue(self.status.ready(self.path, 456))

    def test_changed_path_replaces_single_active_cache(self):
        self.write(b'Done (1s)!\n')
        self.assertTrue(self.status.ready(self.path, 123))
        other = self.path.with_name('other.log')
        other.write_bytes(b'Starting\n')
        self.assertFalse(self.status.ready(other, 123))
        self.write(b'Starting again\n')
        self.assertFalse(self.status.ready(self.path, 123))

    def test_reset_and_absent_pid_forget_cached_readiness(self):
        for clear in (self.status.reset, lambda: self.status.ready(self.path, None)):
            self.write(b'Done (1s)!\n')
            self.assertTrue(self.status.ready(self.path, 123))
            clear()
            self.write(b'Starting\n')
            self.assertFalse(self.status.ready(self.path, 123))

    def test_missing_unreadable_and_invalid_paths_return_false(self):
        self.assertFalse(self.status.ready(self.path, 123))
        self.assertFalse(self.status.ready(None, 123))
        with patch('log_reader.open', side_effect=PermissionError, create=True):
            self.assertFalse(self.status.ready(self.path, 123))
        self.write(b'Done (1s)!\n')
        self.assertTrue(self.status.ready(self.path, 123))

    def test_unfinished_scan_restarts_when_file_rotates(self):
        self.write(b'x' * 10000)
        self.assertFalse(self.status.ready(self.path, 123))
        self.path.replace(self.path.with_suffix('.old'))
        self.write(b'Done (1s)!\n' + b'x' * 12000)
        self.assertTrue(self.status.ready(self.path, 123))

    def test_unfinished_scan_restarts_after_truncation(self):
        self.write(b'x' * 10000)
        self.assertFalse(self.status.ready(self.path, 123))
        self.write(b'Done (1s)!\n')
        self.assertTrue(self.status.ready(self.path, 123))

    def test_truncate_regrow_detects_changed_prefix(self):
        self.write(b'x' * 10000)
        self.assertFalse(self.status.ready(self.path, 123))
        self.write(b'Done (1s)!\n' + b'x' * 12000)
        with self.count_reads():
            self.assertTrue(self.status.ready(self.path, 123))
        self.assert_bounded()

    def test_rewrite_detects_changed_cursor_when_prefix_is_unchanged(self):
        self.write(b'x' * 10000)
        self.assertFalse(self.status.ready(self.path, 123))
        self.write(b'x' * 100 + b'Done (1s)!\n' + b'y' * 12000)
        self.assertTrue(self.status.ready(self.path, 123))

    def test_concurrent_polling_serializes_scan_state(self):
        self.write(b'x' * (SCAN_BYTES + 400) + b'Done (1s)!\n')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.status.ready(self.path, 123), range(16)))
        self.assertTrue(any(results))
        self.assertTrue(self.status.ready(self.path, 123))


if __name__ == '__main__':
    unittest.main()
