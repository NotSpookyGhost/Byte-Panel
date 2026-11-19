"""All MaxMind requests and MMDB readers are injected; no credentials or live network."""
from io import BytesIO
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from guard_geoip import GeoIPService, GeoIPError, DOWNLOAD_URL, DOWNLOAD_HOSTS, normalize_ip


NOW = 1800000000
R2 = 'https://' + next(host for host in DOWNLOAD_HOSTS if host.endswith('cloudflarestorage.com')) + '/signed?token=example'


def archive(entries=None):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as target:
        for name, data, kind in entries or [('GeoLite2-City_test/GeoLite2-City.mmdb', b'citydb', tarfile.REGTYPE)]:
            member = tarfile.TarInfo(name)
            member.size, member.type = len(data), kind
            if kind == tarfile.SYMTYPE:
                member.linkname = '/etc/passwd'
            target.addfile(member, BytesIO(data))
    return stream.getvalue()


class Response:
    def __init__(self, status=200, content=None, location=None):
        self.status_code, self.content = status, archive() if content is None else content
        self.headers = {'Location': location} if location else {}
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.closed, self.trust_env = responses, [], False, True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class Reader:
    def __init__(self, path, epoch=NOW - 86400, database_type='GeoLite2-City'):
        self.epoch, self.database_type, self.closed, self.queries = epoch, database_type, False, []
        if Path(path).read_bytes() == b'bad':
            raise ValueError('corrupt')

    def metadata(self):
        return SimpleNamespace(database_type=self.database_type, build_epoch=self.epoch)

    def city(self, ip):
        self.queries.append(ip)
        if ip == '9.9.9.9':
            raise ValueError('not found')
        return SimpleNamespace(country=SimpleNamespace(iso_code='US', name='United States'),
                               city=SimpleNamespace(name='Test'),
                               location=SimpleNamespace(latitude=40., longitude=-74., accuracy_radius=10))

    def close(self):
        self.closed = True


class GeoIPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.responses = [Response(status=302, location=R2), Response()]
        self.session = FakeSession(self.responses)
        self.readers = []
        self.epoch, self.database_type = NOW - 86400, 'GeoLite2-City'
        self.service = GeoIPService(self.temp.name, session_factory=lambda: self.session,
                                   reader_factory=self.reader, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.service.set_credentials(dict(account_id='12345', license_key='SECRET-KEY'))

    def reader(self, path):
        result = Reader(path, self.epoch, self.database_type)
        self.readers.append(result)
        return result

    def test_credentials_masked_and_blank_keeps_secret(self):
        self.service.set_credentials(dict(license_key=''))
        self.assertNotIn('SECRET-KEY', json.dumps(self.service.credentials()))
        self.assertTrue(self.service.credentials()['has_license_key'])
        self.service.update()
        self.assertEqual(('12345', 'SECRET-KEY'), self.session.calls[0][1]['auth'])
        self.assertTrue(self.service.health()['credentials_validated'])

    def test_redirect_strips_auth_and_disables_ambient_credentials(self):
        self.service.update()
        self.assertEqual(DOWNLOAD_URL, self.session.calls[0][0])
        self.assertEqual(R2, self.session.calls[1][0])
        self.assertIsNone(self.session.calls[1][1]['auth'])
        self.assertFalse(self.session.trust_env)
        self.assertFalse(self.session.calls[0][1]['allow_redirects'])
        self.assertTrue(self.session.closed)

    def test_untrusted_or_http_redirect_denied_without_following(self):
        for url in ('http://download.maxmind.com/a', 'https://evil.example/a',
                    'https://download.maxmind.com.evil.example/a', 'https://user:secret@download.maxmind.com/a'):
            with self.subTest(url=url):
                self.session.responses = [Response(status=302, location=url)]
                self.session.calls.clear()
                with self.assertRaises(GeoIPError):
                    self.service.update()
                self.assertEqual(1, len(self.session.calls))
                self.assertFalse(self.service.database_path.exists())

    def test_download_errors_redact_auth_and_signed_urls(self):
        self.session.responses = [RuntimeError('SECRET-KEY https://signed.example/token')]
        with self.assertRaises(GeoIPError) as caught:
            self.service.update()
        output = str(caught.exception) + json.dumps(self.service.health())
        self.assertNotIn('SECRET-KEY', output)
        self.assertNotIn('signed.example', output)

    def test_archive_traversal_links_duplicates_rejected(self):
        variants = [
            [('../GeoLite2-City.mmdb', b'citydb', tarfile.REGTYPE)],
            [('/GeoLite2-City.mmdb', b'citydb', tarfile.REGTYPE)],
            [('GeoLite2-City.mmdb', b'', tarfile.SYMTYPE)],
            [('a/GeoLite2-City.mmdb', b'citydb', tarfile.REGTYPE),
             ('b/GeoLite2-City.mmdb', b'citydb', tarfile.REGTYPE)],
        ]
        for entries in variants:
            with self.subTest(entries=entries):
                self.session.responses = [Response(content=archive(entries))]
                with self.assertRaises(GeoIPError):
                    self.service.update()
                self.assertFalse(self.service.database_path.exists())

    def test_wrong_database_type_and_old_database_rejected(self):
        for kind, epoch in [('GeoLite2-Country', NOW - 1), ('GeoLite2-City', NOW - 31 * 86400),
                            ('GeoLite2-City', NOW + 2 * 86400)]:
            self.database_type, self.epoch = kind, epoch
            self.session.responses = [Response()]
            with self.assertRaises(GeoIPError):
                self.service.update()
            self.assertFalse(self.service.database_path.exists())

    def test_failed_update_preserves_existing_database(self):
        self.service.update()
        before = self.service.database_path.read_bytes()
        self.session.responses = [Response(content=archive([('GeoLite2-City.mmdb', b'bad', tarfile.REGTYPE)]))]
        with self.assertRaises(GeoIPError):
            self.service.update()
        self.assertEqual(before, self.service.database_path.read_bytes())
        self.assertTrue(self.service.health()['ready'])
        self.assertTrue(self.service.health()['error'])

    def test_stale_database_cannot_geolocate_new_ip(self):
        self.service.update()
        self.now += 31 * 86400
        self.assertFalse(self.service.health()['ready'])
        self.assertEqual('database_stale', self.service.lookup('8.8.8.8')['reason'])

    def test_lookups_are_local_and_public_172_not_misclassified(self):
        self.service.update()
        count = len(self.session.calls)
        self.assertTrue(self.service.lookup('172.100.1.1')['usable'])
        self.assertTrue(self.service.lookup('2001:4860:4860::8888')['usable'])
        for ip in ('172.16.0.1', '192.168.1.1', '127.0.0.1', '::1', '10.0.0.1'):
            self.assertEqual('non_public', self.service.lookup(ip)['reason'])
        self.assertEqual(count, len(self.session.calls))
        self.assertEqual('unmapped', self.service.lookup('9.9.9.9')['reason'])

    def test_ip_validation_and_mapped_normalization(self):
        self.assertEqual('8.8.8.8', normalize_ip('::ffff:8.8.8.8'))
        for value in ('google.com', '1.2.3.4:80', 'fe80::1%eth0', None):
            with self.assertRaises(ValueError):
                normalize_ip(value)

    def test_reader_reused_and_invalid_location_not_invented(self):
        self.service.update()
        self.service.lookup('8.8.8.8')
        count = len(self.readers)
        self.service.lookup('8.8.4.4')
        self.assertEqual(count, len(self.readers))
        response = SimpleNamespace(country=SimpleNamespace(iso_code='US', name='US'),
                                   city=SimpleNamespace(name=None),
                                   location=SimpleNamespace(latitude=None, longitude=None, accuracy_radius=None))
        with patch.object(self.service._reader, 'city', return_value=response):
            self.assertEqual('insufficient_location', self.service.lookup('8.8.8.8')['reason'])

    def test_changed_credentials_need_revalidation_and_removal_explicit(self):
        self.service.update()
        self.service.set_credentials(dict(license_key='NEW-KEY'))
        self.assertFalse(self.service.health()['ready'])
        self.service.set_credentials(dict(clear_license_key=True))
        self.assertFalse(self.service.credentials()['has_license_key'])
        with self.assertRaises(GeoIPError):
            self.service.update()

    def test_daily_updates_and_no_automatic_network_without_credentials(self):
        self.service.update()
        count = len(self.session.calls)
        self.service.maintenance()
        self.assertEqual(count, len(self.session.calls))
        self.now += 86401
        self.session.responses = [Response()]
        self.service.maintenance()
        self.assertEqual(count + 1, len(self.session.calls))
        self.service.set_credentials(dict(clear_license_key=True))
        self.now += 86401
        self.service.maintenance()
        self.assertEqual(count + 1, len(self.session.calls))

    def test_download_size_limit_and_no_credentials_in_image_configuration(self):
        self.session.responses = [Response(content=b'x' * 100)]
        with patch('guard_geoip.MAX_ARCHIVE_BYTES', 10):
            with self.assertRaises(GeoIPError):
                self.service.update()
        self.assertFalse(self.service.database_path.exists())
        self.assertEqual([], list(Path(self.temp.name).glob('.geoip-*')))

    def test_update_serialization_and_private_files(self):
        self.service._update_lock.acquire()
        try:
            with self.assertRaises(GeoIPError):
                self.service.update()
        finally:
            self.service._update_lock.release()
        self.assertTrue(self.service.credentials_path.exists())
        self.assertFalse(self.service.database_path.exists())


if __name__ == '__main__':
    unittest.main()
