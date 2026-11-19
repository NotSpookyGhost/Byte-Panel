"""Boundary tests; no Java process, credentials or external connections."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
from contextlib import nullcontext

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from guard_runtime import GuardRuntime, effective_online_mode, redact_auth_commands
from guard_auth import GuardAuthError, digest
from test_connection_guard import FakeGeo


class PropertiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def properties(self, value):
        (self.root / 'server.properties').write_text(value, encoding='latin-1')

    def test_java_property_syntax(self):
        for value in ('online-mode=false', 'online-mode:false', 'online-mode false',
                      r'online\u002dmode=false', 'online-\\\n  mode=false',
                      '! comment\\\nonline-mode=false', '# comment\\\nonline-mode=false',
                      'online-mode=true\nonline-mode=false', '\tonline-mode : false'):
            with self.subTest(value=value):
                self.properties(value)
                self.assertFalse(effective_online_mode(self.root))
        self.properties('# online-mode=false\nonline-mode=true')
        self.assertTrue(effective_online_mode(self.root))

    def test_queued_override_is_effective_and_invalid_data_rejected(self):
        self.properties('online-mode=true')
        override = self.root / 'properties_override.json'
        override.write_text(json.dumps({'online-mode': 'false'}))
        self.assertFalse(effective_online_mode(self.root))
        for value in ('[]', '{"online-mode":"true\\nonline-mode=false"}', '{bad'):
            override.write_text(value)
            with self.assertRaises(GuardAuthError):
                effective_online_mode(self.root)
        override.unlink()
        for value in ('online-mode=TRUE ', 'online-mode=maybe', r'online\uXXXXmode=false', 'online-mode=\\'):
            self.properties(value)
            with self.assertRaises(GuardAuthError):
                effective_online_mode(self.root)

    def test_missing_properties_defaults_online(self):
        self.assertTrue(effective_online_mode(self.root))

    def test_authentication_logs_redacted_without_destroying_normal_commands(self):
        for command in ('/login', '/l', '/cp', '/unreg', '/register', '/authme:changepassword',
                        '/email', '/totp', '/captcha'):
            for prefix in ('Player issued server command: ', 'Executing command: '):
                value = redact_auth_commands(prefix + command + ' hidden-password old-password\n')
                self.assertNotIn('hidden-password', value)
                self.assertNotIn('old-password', value)
                self.assertIn('[REDACTED]', value)
        self.assertNotIn('secret', redact_auth_commands('Executing command: authme register Alice secret'))
        self.assertEqual(redact_auth_commands('Player issued server command: /help plugins'),
                         'Player issued server command: /help plugins')


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / 'server'
        self.server.mkdir()
        self.scope = 'scope-one'
        self.app = Flask(__name__)
        self.security = Mock()
        self.security.store.scope.side_effect = lambda: self.scope
        self.config = dict(server_type='Paper', mc_version='1.21.1', is_setup=True)
        self.runtime = GuardRuntime(self.app, self.security, self.root, self.server,
            lambda: self.config, Mock(), nullcontext, lambda: False, port=0)
        self.runtime.guard.geoip = FakeGeo()
        self.addCleanup(self.runtime.close)
        self.client = self.app.test_client()

    def jar(self, path, name):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('plugin.yml', 'name: ' + name + '\nversion: "1.0"\nmain: example.Main\n')
        return path

    def test_bridge_explicit_storage_failure_is_not_transport_fallback(self):
        self.runtime.start_bridge()
        connection = http.client.HTTPConnection('127.0.0.1', self.runtime.port, timeout=3)
        self.addCleanup(connection.close)
        with patch.object(self.runtime.guard, 'prelogin', side_effect=OSError('secret storage path')):
            connection.request('POST', '/v1/prelogin', json.dumps(dict(scope=self.scope)),
                {'Authorization': 'Bearer ' + self.runtime._token()})
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertNotIn(b'secret storage path', response.read())

    def test_snapshot_requires_token_and_is_complete(self):
        self.runtime.start_bridge()
        for authorized, expected in ((False, 401), (True, 200)):
            connection = http.client.HTTPConnection('127.0.0.1', self.runtime.port, timeout=3)
            try:
                connection.request('GET', '/v1/snapshot', headers=
                    {'Authorization': 'Bearer ' + self.runtime._token()} if authorized else {})
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                body = json.loads(response.read())
                if authorized:
                    self.assertTrue(body['complete'])
                    self.assertEqual(body['scope'], self.scope)
                self.assertNotIn(self.runtime._token(), json.dumps(body))
            finally:
                connection.close()

    def test_scope_replacement_rotates_private_config_and_token(self):
        self.jar(self.server / 'plugins/BytePanelGuard.jar', 'BytePanelGuard')
        first_path = self.runtime.prepare_start()[0].split('=', 1)[1]
        first = json.loads(Path(first_path).read_text())
        self.scope = 'scope-two'
        self.runtime.reset()
        second_path = self.runtime.prepare_start()[0].split('=', 1)[1]
        second = json.loads(Path(second_path).read_text())
        self.assertNotEqual(first_path, second_path)
        self.assertFalse(Path(first_path).exists())
        self.assertNotEqual(first['token'], second['token'])
        self.assertFalse(Path(second_path).is_relative_to(self.server))
        self.assertEqual(second['mode'], 'off')

    def test_bundled_companion_requires_digest_and_private_bridge(self):
        self.runtime.guard.save_settings({'mode': 'enforce'})
        bundle = self.jar(self.root / 'bundle/BytePanelGuard.jar', 'BytePanelGuard')
        checksum = bundle.with_suffix('.sha512')
        checksum.write_text('0' * 128)
        with patch.dict(os.environ, {'BYTEPANEL_GUARD_JAR': str(bundle)}):
            with self.assertRaisesRegex(GuardAuthError, 'bridge'):
                self.runtime.prepare_start()
            self.runtime.start_bridge()
            with self.assertRaisesRegex(GuardAuthError, 'integrity'):
                self.runtime.prepare_start()
            checksum.write_text(digest(bundle))
            args = self.runtime.prepare_start()
            self.assertEqual(len(args), 1)
            self.assertTrue((self.server / 'plugins/BytePanelGuard.jar').is_file())
            self.assertNotIn(self.runtime._token(), ' '.join(args))

    def test_mode_switch_requires_explicit_confirmation(self):
        (self.server / 'server.properties').write_text('online-mode=false')
        values = dict(mode='off')
        self.assertEqual(self.client.put('/api/connection-security/settings', json=values).status_code, 400)
        values['confirm_identity_change'] = True
        response = self.client.put('/api/connection-security/settings', json=values)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json['identity_mode'], 'offline')
        self.assertEqual(response.json['effective_identity_mode'], 'offline')

    def test_settings_flatten_health_without_secret(self):
        response = self.client.get('/api/connection-security/settings')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('geoip', response.json)
        self.assertIn('healthy', response.json['health'])
        self.assertNotIn('license_key', response.json)

    def test_staged_authentication_updates_cannot_replace_validated_components(self):
        self.jar(self.server / 'plugins/update/renamed.jar', 'AuthMe')
        with self.assertRaisesRegex(GuardAuthError, 'staged'):
            self.runtime.validate_staged_updates()
        (self.server / 'bukkit.yml').write_text('settings:\n  update-folder: pending\n')
        self.runtime.validate_staged_updates()
        self.jar(self.server / 'plugins/pending/new-security.jar', 'BytePanelGuard')
        with self.assertRaisesRegex(GuardAuthError, 'staged'):
            self.runtime.validate_staged_updates()

    def test_protect_authentication_accounts_as_well_as_config(self):
        self.runtime.guard.save_settings({'mode': 'enforce'})
        from werkzeug.exceptions import Conflict
        for path in ('plugins/AuthMe/authme.db', 'plugins/AuthMe', 'plugins'):
            with self.assertRaises(Conflict):
                self.runtime.protect_path(path)


if __name__ == '__main__':
    unittest.main()
