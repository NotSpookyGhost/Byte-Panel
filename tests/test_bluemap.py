"""BlueMap reverse-proxy tests: fake HTTP upstream on loopback, no Minecraft."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from flask import Flask
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from bluemap import register_bluemap


TILE = b'\x00\x01PRBM\xff\x80' * 100
COMPRESSED_TILE = gzip.compress(TILE, mtime=0)
FIRST_EVENT = b'event: player\ndata: {"players":[]}\n\n'


class MapFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.do_GET()

    def reply(self, body=b'', status=200, headers=None):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def do_GET(self):
        with self.server.records_lock:
            self.server.records.append((self.command, self.path, dict(self.headers)))
        path = urlsplit(self.path).path
        if path == '/':
            self.reply(b'<html><script src="assets/map.js"></script></html>',
                       headers={'Content-Type': 'text/html; charset=utf-8'})
        elif path == '/assets/map.js':
            self.reply(b'window.mapLoaded=true;', headers={'Content-Type': 'application/javascript'})
        elif path == '/maps/world/tiles/tile.prbm':
            self.reply(COMPRESSED_TILE, headers={'Content-Type': 'application/octet-stream',
                       'Content-Encoding': 'gzip', 'ETag': '"tile-1"', 'Vary': 'Accept-Encoding'})
        elif path == '/maps/world/live/players.json':
            self.reply(b'{"players":[]}', headers={'Content-Type': 'application/json'})
        elif path == '/range.bin':
            self.reply(TILE[2:6], status=206, headers={'Content-Type': 'application/octet-stream',
                       'Accept-Ranges': 'bytes', 'Content-Range': f'bytes 2-5/{len(TILE)}'})
        elif path == '/empty':
            self.reply(status=204)
        elif path == '/unchanged':
            self.reply(status=304, headers={'ETag': '"unchanged"'})
        elif path == '/headers':
            self.reply(b'ok', headers={'Content-Type': 'text/plain', 'Set-Cookie': 'session=evil',
                       'WWW-Authenticate': 'Basic realm="untrusted"', 'Refresh': '0;url=https://example.invalid/',
                       'X-Secret-Upstream': 'private', 'Cache-Control': 'public, max-age=86400',
                       'Connection': 'X-Secret-Upstream'})
        elif path == '/local-redirect':
            self.reply(status=302, headers={'Location': '/assets/map.js?x=1'})
        elif path == '/absolute-redirect':
            self.reply(status=307, headers={'Location':
                       f'http://127.0.0.1:{self.server.server_port}/assets/map.js?x=2'})
        elif path == '/external-redirect':
            self.reply(status=302, headers={'Location': 'https://example.invalid/private'})
        elif path == '/protocol-redirect':
            self.reply(status=302, headers={'Location': '//example.invalid/private'})
        elif path == '/malformed-redirect':
            self.reply(status=302, headers={'Location': 'http://[invalid'})
        elif path == '/maps/world/live/sse':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(FIRST_EVENT)
                self.wfile.flush()
                self.server.release_sse.wait(4)
                self.wfile.write(b':\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True
        else:
            self.reply(b'not found', status=404, headers={'Content-Type': 'text/plain'})


class BlueMapProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), MapFixtureHandler)
        cls.upstream.daemon_threads = True
        cls.upstream.records = []
        cls.upstream.records_lock = threading.Lock()
        cls.upstream.release_sse = threading.Event()
        cls.worker = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.release_sse.set()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.worker.join(timeout=5)

    def setUp(self):
        self.config = {'is_setup': True, 'bluemap_enabled': True,
                       'bluemap_port': self.upstream.server_port}
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'app' / 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='loopback-tests-only')
        register_bluemap(self.app, lambda: self.config.copy())
        self.client = self.app.test_client()
        self.upstream.release_sse.set()
        with self.upstream.records_lock:
            self.upstream.records.clear()

    def records(self):
        with self.upstream.records_lock:
            return list(self.upstream.records)

    def test_entry_redirect_is_relative_and_preserves_query_for_lan_and_tunnel(self):
        for origin in ('http://192.168.0.10:5050', 'https://mc.bytedev.app'):
            with self.subTest(origin=origin):
                response = self.client.get('/map?world=one&world=two&value=a%2Bb', base_url=origin)
                self.assertIn(response.status_code, (301, 302, 307, 308))
                self.assertEqual(response.headers['Location'], '/map/?world=one&world=two&value=a%2Bb')
        self.assertEqual(self.records(), [])

    def test_public_root_assets_live_data_and_queries_reach_only_loopback(self):
        response = self.client.get('/map/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'src="assets/map.js"', response.data)
        response = self.client.get('/map/assets/map.js?next=https%3A%2F%2Fevil.invalid&x=1&x=2')
        self.assertEqual(response.data, b'window.mapLoaded=true;')
        self.assertIn('javascript', response.content_type)
        self.assertEqual(self.records()[-1][1], '/assets/map.js?next=https%3A%2F%2Fevil.invalid&x=1&x=2')
        response = self.client.get('/map/maps/world/live/players.json')
        self.assertEqual(response.json, {'players': []})
        self.assertEqual(self.records()[-1][1], '/maps/world/live/players.json')

    def test_gzipped_binary_tiles_are_not_double_decoded_or_corrupted(self):
        response = self.client.get('/map/maps/world/tiles/tile.prbm')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, COMPRESSED_TILE)
        self.assertEqual(gzip.decompress(response.data), TILE)
        self.assertEqual(response.headers['Content-Encoding'], 'gzip')
        self.assertEqual(response.headers['Content-Length'], str(len(COMPRESSED_TILE)))
        self.assertEqual(response.headers['ETag'], '"tile-1"')
        self.assertIn('Accept-Encoding', response.headers['Vary'])

    def test_head_empty_not_modified_and_ranges_preserve_http_semantics(self):
        response = self.client.head('/map/maps/world/tiles/tile.prbm')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'')
        self.assertEqual(response.headers['Content-Length'], str(len(COMPRESSED_TILE)))
        self.assertEqual(self.records()[-1][0], 'HEAD')
        for path, status in (('/empty', 204), ('/unchanged', 304)):
            with self.subTest(status=status):
                response = self.client.get('/map' + path)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.data, b'')
        response = self.client.get('/map/range.bin', headers={'Range': 'bytes=2-5', 'If-Range': '"tile-1"'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, TILE[2:6])
        self.assertEqual(response.headers['Content-Range'], f'bytes 2-5/{len(TILE)}')
        self.assertEqual(response.headers['Accept-Ranges'], 'bytes')

    def test_request_header_allowlist_never_forwards_panel_credentials(self):
        allowed = {'Accept': 'application/json', 'Accept-Encoding': 'gzip',
                   'If-None-Match': '"abc"', 'If-Modified-Since': 'Wed, 18 Nov 2026 00:00:00 GMT',
                   'Range': 'bytes=0-1', 'If-Range': '"abc"', 'Last-Event-ID': '42'}
        self.client.set_cookie('session', 'private-panel-session')
        headers = dict(allowed, Authorization='Bearer private-token',
                       **{'X-Panel-CSRF': 'secret', 'X-Forwarded-Host': 'evil.invalid',
                          'X-Forwarded-For': '203.0.113.1', 'Host': 'evil.invalid'})
        response = self.client.get('/map/assets/map.js', headers=headers)
        self.assertEqual(response.status_code, 200)
        response.get_data()
        sent = {key.lower(): value for key, value in self.records()[-1][2].items()}
        for name, value in allowed.items():
            self.assertEqual(sent[name.lower()], value)
        for name in ('cookie', 'authorization', 'x-panel-csrf', 'x-forwarded-host', 'x-forwarded-for'):
            self.assertNotIn(name, sent)
        self.assertEqual(sent['host'], f'127.0.0.1:{self.upstream.server_port}')

    def test_response_headers_do_not_set_panel_cookies_or_cache_private_content(self):
        response = self.client.get('/map/headers')
        self.assertEqual(response.data, b'ok')
        for name in ('Set-Cookie', 'WWW-Authenticate', 'Refresh', 'X-Secret-Upstream', 'Connection'):
            self.assertNotIn(name, response.headers)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_local_redirects_are_rebased_and_external_redirects_are_not_followed(self):
        for path, expected, status in (('local-redirect', '/map/assets/map.js?x=1', 302),
                                       ('absolute-redirect', '/map/assets/map.js?x=2', 307)):
            with self.subTest(path=path):
                response = self.client.get('/map/' + path)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers['Location'], expected)
        for path in ('external-redirect', 'protocol-redirect', 'malformed-redirect'):
            with self.subTest(path=path):
                response = self.client.get('/map/' + path)
                self.assertEqual(response.status_code, 502)
                self.assertNotIn('Location', response.headers)
        self.assertEqual(len(self.records()), 5)

    def test_disabled_or_unconfigured_map_never_contacts_upstream(self):
        for updates in ({'bluemap_enabled': False}, {'is_setup': False}):
            for path in ('/map', '/map/', '/map/maps/world/live/players.json'):
                original = self.config.copy()
                self.config.update(updates)
                with self.subTest(updates=updates, path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)
                self.config = original
        self.assertEqual(self.records(), [])

    def test_mutating_methods_and_options_are_not_proxied(self):
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'):
            for path in ('/map', '/map/', '/map/assets/map.js'):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.client.open(path, method=method).status_code, 405)
        self.assertEqual(self.records(), [])

    def test_invalid_ports_cannot_contact_arbitrary_services_or_recurse(self):
        for port in ('evil.invalid:80', 0, 80, 1023, 5000, 65536, True, None):
            with self.subTest(port=port):
                self.config['bluemap_port'] = port
                with patch('bluemap.requests.Session') as factory:
                    response = self.client.get('/map/')
                    self.assertGreaterEqual(response.status_code, 400)
                    factory.assert_not_called()

    def test_unsafe_paths_are_rejected_without_upstream_requests(self):
        for path in ('../private', '%2e%2e/private', 'maps/../private',
                     'maps/%5cprivate', 'maps/%00private', 'maps/%0d%0aprivate'):
            with self.subTest(path=path):
                response = self.client.get('/map/' + path)
                # Werkzeug may reject a decoded newline before the path route matches.
                if '%0d%0a' in path:
                    self.assertIn(response.status_code, (400, 404))
                else:
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(self.records(), [])

    def test_missing_upstream_asset_remains_a_404(self):
        response = self.client.get('/map/assets/missing.png')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, b'not found')

    def test_upstream_connection_failures_and_timeouts_have_safe_errors(self):
        for error, status in ((requests.ConnectionError('private internal detail'), 503),
                              (requests.Timeout('private internal detail'), 504)):
            with self.subTest(status=status), patch('bluemap.requests.Session') as factory:
                session = factory.return_value
                session.request.side_effect = error
                response = self.client.get('/map/')
                self.assertEqual(response.status_code, status)
                self.assertNotIn(b'private internal detail', response.data)
                self.assertNotIn(b'Traceback', response.data)
                self.assertFalse(session.trust_env)
                session.close.assert_called()
                args = session.request.call_args
                self.assertFalse(args.kwargs['allow_redirects'])
                self.assertTrue(args.kwargs['stream'])
                self.assertEqual(args.kwargs['timeout'], (3, 90))

    def test_default_port_is_8100_without_contacting_a_real_service(self):
        self.config.pop('bluemap_port')
        with patch('bluemap.requests.Session') as factory:
            factory.return_value.request.side_effect = requests.ConnectionError('fixture')
            self.assertEqual(self.client.get('/map/').status_code, 503)
            self.assertEqual(factory.return_value.request.call_args.args[:2],
                             ('GET', 'http://127.0.0.1:8100/'))

    def test_sse_fallback_keeps_events_unbuffered_and_closes_resources(self):
        raw = SimpleNamespace(stream=Mock(return_value=iter([FIRST_EVENT, b':\n'])))
        upstream = SimpleNamespace(status_code=200, headers={'Content-Type': 'text/event-stream'},
                                   raw=raw, close=Mock())
        with patch('bluemap.requests.Session') as factory:
            factory.return_value.request.return_value = upstream
            response = self.client.get('/map/maps/world/live/sse', buffered=False)
            self.assertEqual(next(iter(response.response)), FIRST_EVENT)
            response.close()
            raw.stream.assert_called_once_with(1, decode_content=False)
            upstream.close.assert_called()
            factory.return_value.close.assert_called()

    def test_sse_first_event_is_streamed_before_the_connection_closes(self):
        self.upstream.release_sse.clear()
        response = None
        started = time.monotonic()
        try:
            response = self.client.get('/map/maps/world/live/sse', buffered=False)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content_type.startswith('text/event-stream'))
            self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
            self.assertIn('no-store', response.headers['Cache-Control'])
            received = b''
            iterator = iter(response.response)
            while FIRST_EVENT not in received:
                received += next(iterator)
            self.assertLess(time.monotonic() - started, 2,
                            'SSE waited for the upstream to finish instead of streaming the event')
            self.assertFalse(self.upstream.release_sse.is_set())
        finally:
            self.upstream.release_sse.set()
            if response is not None:
                response.close()


if __name__ == '__main__':
    unittest.main()
