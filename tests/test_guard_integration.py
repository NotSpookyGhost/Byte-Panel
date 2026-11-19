"""Guard authorization/lifecycle integration; temporary data and no real services."""
import http.client
import io
import json
from pathlib import Path
import sqlite3
import unittest
import uuid
from unittest.mock import Mock, patch
import zipfile

import test_accounts as account_tests
from test_connection_guard import FakeGeo, UUID


class GuardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_type = type('GuardAccountFixture', (account_tests.AccountIntegrationTests,), {})
        cls.fixture_type.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_type.module.guard_runtime.close()
        cls.fixture_type.tearDownClass()

    def setUp(self):
        self.fixture = self.fixture_type()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.panel, self.client, self.root = self.fixture.module, self.fixture.client, self.fixture.root
        self.runtime, self.config = self.panel.guard_runtime, self.fixture.config
        self.guard = self.runtime.guard
        self.geo = FakeGeo()
        replacement = patch.object(self.guard, 'geoip', self.geo)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.guard.reset()

    def headers(self, client=None):
        return self.fixture.headers(client)

    def user(self, name, permissions):
        return self.fixture.client_for(self.fixture.create(name, permissions))

    def enable(self):
        self.guard.save_settings(dict(mode='enforce'))

    def request(self, ip='8.8.8.8'):
        policy = self.guard.config()
        return dict(scope=policy['scope'], policy_revision=policy['policy_revision'], protocol_version=1,
                    identity_mode=policy['identity_mode'], username='Steve', uuid=UUID, ip=ip,
                    attempt_id=uuid.uuid4().hex)

    def enroll(self):
        self.enable()
        request = self.request()
        self.guard.prelogin(request)
        result = self.guard.outcome(dict(request, stage='joined'))
        self.panel.record_guard_outcome(result, request)
        return request

    def jar(self, filename, name, version='1.0'):
        path = self.root / 'plugins' / filename
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, 'w') as target:
            target.writestr('plugin.yml', 'name: ' + name + '\nversion: ' + version + '\nmain: example.Test\n')
        return path

    def test_settings_and_history_require_settings_permission(self):
        endpoints = ('settings', 'auth', 'history/preview')
        viewer = self.user('player-viewer', ['players.view'])
        settings = self.user('settings-user', ['settings.manage'])
        anonymous = self.panel.app.test_client()
        for suffix in endpoints:
            path = '/api/connection-security/' + suffix
            self.assertEqual(anonymous.get(path).status_code, 401)
            self.assertEqual(viewer.get(path).status_code, 403)
            response = settings.get(path)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertNotIn('secret-license', response.get_data(as_text=True))
            self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(viewer.get('/api/connection-security/events').status_code, 200)
        self.assertEqual(settings.get('/api/connection-security/events').status_code, 403)

    def test_all_security_mutations_require_csrf_and_correct_permissions(self):
        readonly = self.user('guard-reader', ['players.view'])
        mutations = [
            ('put', '/api/connection-security/settings', {'mode': 'off'}),
            ('post', '/api/connection-security/geoip/update', {}),
            ('post', '/api/connection-security/history/import', {'confirm': True}),
            ('post', '/api/connection-security/auth/install', {'confirm': True}),
            ('post', '/api/connection-security/auth/repair', {'confirm': True}),
            ('post', '/api/connection-security/reviews/12345678', {'action': 'allow_once'}),
            ('post', '/api/connection-security/players/online:' + UUID + '/safe-ips', {'ip': '1.1.1.1'}),
            ('delete', '/api/connection-security/players/online:' + UUID + '/safe-ips', {'ip': '1.1.1.1'}),
        ]
        with patch.object(self.guard, 'update_geoip') as download, \
             patch.object(self.runtime.auth, 'install') as install, \
             patch.object(self.runtime.auth, 'repair') as repair:
            for method, path, payload in mutations:
                self.assertEqual(getattr(self.client, method)(path, json=payload).status_code, 403)
                response = getattr(readonly, method)(path, json=payload, headers=self.headers(readonly))
                self.assertEqual(response.status_code, 403)
            download.assert_not_called()
            install.assert_not_called()
            repair.assert_not_called()

    def test_review_permission_does_not_require_settings_or_moderation(self):
        self.enroll()
        rejected = self.request('1.1.1.1')
        self.guard.prelogin(rejected)
        reviewer = self.user('reviewer', ['players.security'])
        self.assertEqual(reviewer.get('/api/connection-security/events?status=pending').status_code, 200)
        result = reviewer.post('/api/connection-security/reviews/' + rejected['attempt_id'],
                               json={'action': 'allow_once'}, headers=self.headers(reviewer))
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(reviewer.put('/api/connection-security/settings', json={'mode': 'off'},
                                     headers=self.headers(reviewer)).status_code, 403)
        self.assertEqual(reviewer.post('/api/player/ban', json={'username': 'Steve', 'ip': '1.1.1.1'},
                                      headers=self.headers(reviewer)).status_code, 403)

    def test_safe_ip_management_and_profile_links(self):
        self.enroll()
        reviewer = self.user('address-reviewer', ['players.security'])
        base = '/api/connection-security/players/online:' + UUID
        saved = reviewer.post(base + '/safe-ips', json={'ip': '2001:0db8::7'}, headers=self.headers(reviewer))
        self.assertEqual(200, saved.status_code, saved.json)
        self.assertEqual('2001:db8::7', saved.json['safe_ips'][0]['ip'])
        viewer = self.user('address-viewer', ['players.view'])
        self.assertEqual(saved.json, viewer.get(base).json)
        self.assertIn(f'data-guard-identity="online:{UUID}"', viewer.get('/players').get_data(as_text=True))
        self.assertEqual('online:' + UUID, viewer.get('/api/players').json[0]['guard_identity'])
        removed = reviewer.delete(base + '/safe-ips', json={'ip': '2001:db8::7'}, headers=self.headers(reviewer))
        self.assertEqual(200, removed.status_code, removed.json)
        self.assertEqual([], removed.json['safe_ips'])

    def test_bridge_safe_ip_requires_a_current_session_and_token(self):
        joined = self.enroll()
        self.runtime.port = 0
        self.runtime.start_bridge()
        self.addCleanup(self.runtime.close)
        body = {key: joined[key] for key in ('scope', 'policy_revision', 'attempt_id', 'identity_mode', 'protocol_version')}
        for token, changes, expected in [('', {}, 401), (self.runtime._token(), {'attempt_id': 'does-not-exist'}, 409),
                                         (self.runtime._token(), {}, 200)]:
            connection = http.client.HTTPConnection('127.0.0.1', self.runtime.port, timeout=3)
            try:
                connection.request('POST', '/v1/safe-ip', json.dumps(dict(body, **changes)),
                                   {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(expected, response.status, response.read())
            finally:
                connection.close()
        self.assertEqual('8.8.8.8', self.guard.profile('online:' + UUID)['safe_ips'][0]['ip'])

    def test_authentication_installation_is_explicit_offline_and_reserved(self):
        endpoint = '/api/connection-security/auth/install'
        with patch.object(self.runtime.auth, 'install', return_value={'installed': True}) as install:
            self.assertEqual(self.client.post(endpoint, json={}, headers=self.headers()).status_code, 400)
            self.panel.MINECRAFT_PROCESS = Mock(poll=Mock(return_value=None))
            self.assertEqual(self.client.post(endpoint, json={'confirm': True}, headers=self.headers()).status_code, 409)
            self.panel.MINECRAFT_PROCESS = None
            with self.panel.server_backups.exclusive():
                self.assertEqual(self.client.post(endpoint, json={'confirm': True}, headers=self.headers()).status_code, 409)
            install.assert_not_called()
            result = self.client.post(endpoint, json={'confirm': True}, headers=self.headers())
            self.assertEqual(result.status_code, 200)
            install.assert_called_once()

    def test_offline_activation_requires_auth_provider_and_valid_database(self):
        (self.root / 'server.properties').write_text('online-mode=false\n')
        response = self.client.put('/api/connection-security/settings', json={'mode': 'monitor'},
                                   headers=self.headers())
        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual('off', self.guard.config()['mode'])
        (self.root / 'server.properties').write_text('online-mode=true\n')
        self.geo.ready = False
        with patch.object(self.geo, 'update', side_effect=ValueError('No validated City database')):
            response = self.client.put('/api/connection-security/settings', json={'mode': 'enforce'},
                                       headers=self.headers())
        self.assertEqual(response.status_code, 400)

    def test_startup_validates_file_and_queued_identity_mode_before_java(self):
        self.enable()
        for queued in (False, True):
            with self.subTest(queued=queued):
                (self.root / 'server.properties').write_text('online-mode=true\n' if queued else 'online-mode=false\n')
                override = self.root / 'properties_override.json'
                if queued:
                    override.write_text(json.dumps({'online-mode': 'false'}))
                with patch.object(self.panel, 'build_server_command', return_value=['java', '-jar', 'server.jar']), \
                     patch.object(self.panel.subprocess, 'Popen') as launch:
                    self.assertFalse(self.panel.start_minecraft_server(automatic=queued))
                    launch.assert_not_called()

    def test_managed_guard_cannot_be_deleted_or_moved_through_parent(self):
        target = self.jar('BytePanelGuard.jar', 'BytePanelGuard')
        preview = self.client.get('/api/plugins/delete?path=BytePanelGuard.jar')
        self.assertEqual(preview.status_code, 200)
        response = self.client.delete('/api/plugins/delete', json=preview.json, headers=self.headers())
        self.assertEqual(response.status_code, 409, response.json)
        for relative in ('plugins/BytePanelGuard.jar', 'plugins'):
            entry = self.client.get('/api/files/entry', query_string={'path': relative}).json
            deleted = self.client.delete('/api/files/entry', json={**entry, 'confirmation': relative},
                                         headers=self.headers())
            self.assertEqual(deleted.status_code, 409, deleted.json)
            moved = self.client.post('/api/files/move', json={**entry, 'destination': 'renamed'},
                                     headers=self.headers())
            self.assertEqual(moved.status_code, 409, moved.json)
        self.assertTrue(target.exists())

    def test_generic_updater_excludes_security_plugins_by_descriptor(self):
        self.jar('BytePanelGuard.jar', 'BytePanelGuard')
        self.jar('renamed-auth.jar', 'AuthMe', '6.0.1')
        self.jar('renamed-packets.jar', 'packetevents', '2.14.0')
        self.jar('normal.jar', 'Example')
        rows = self.panel.addon_updates._inventory(self.panel.addon_updates._scope())
        self.assertEqual(['normal.jar'], [row['filename'] for row in rows])

    def test_backups_keep_auth_accounts_but_exclude_guard_and_private_state(self):
        self.jar('BytePanelGuard.jar', 'BytePanelGuard')
        auth_db = self.root / 'plugins' / 'AuthMe' / 'authme.db'
        auth_db.parent.mkdir()
        auth_db.write_bytes(b'fixture-account-password-hashes')
        self.enroll()
        archive_path = self.panel.server_backups.create('Guard_Backup', True)
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn('plugins/AuthMe/authme.db', archive.namelist())
            self.assertEqual(b'fixture-account-password-hashes', archive.read('plugins/AuthMe/authme.db'))
            self.assertNotIn('plugins/BytePanelGuard.jar', archive.namelist())
            self.assertFalse(any('connection-security' in name for name in archive.namelist()))
            self.assertNotIn(b'secret-license', archive.read('panel_config.json'))

    def test_import_restore_purge_clear_scoped_state_and_preserve_geo_credentials(self):
        archive = self.fixture.zip_bytes({'ServerEngineInfo': json.dumps(self.config),
            'server.properties': 'online-mode=true\n', 'plugins/AuthMe/authme.db': 'restored-password-hashes'})
        directory = self.fixture.data / 'backups' / 'Guard_Reset'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'server.zip').write_bytes(archive)
        for action in ('import', 'restore', 'purge'):
            self.enroll()
            previous_scope = self.guard.config()['scope']
            if action == 'import':
                response = self.client.post('/api/import', headers=self.headers(),
                    data={'import_zip': (io.BytesIO(archive), 'server.zip')})
            elif action == 'restore':
                response = self.client.post('/api/backup/restore', headers=self.headers(),
                    json={'server_name': 'Guard_Reset', 'filename': 'server.zip'})
            else:
                response = self.client.post('/api/purge', headers=self.headers())
            self.assertEqual(response.status_code, 200, response.json)
            self.assertNotEqual(previous_scope, self.guard.config()['scope'])
            self.assertEqual('off', self.guard.config()['mode'])
            self.assertEqual([], self.guard.roster()['identities'])
            self.assertEqual([], self.guard.get_events()['events'])
            self.assertEqual([0, 0, 0], self.fixture.player_record_counts())
            self.assertTrue(self.guard.config()['has_license_key'])
            if action != 'purge':
                self.assertEqual('restored-password-hashes',
                    (self.root / 'plugins' / 'AuthMe' / 'authme.db').read_text())

    def test_only_completed_authenticated_sessions_enter_legacy_player_list(self):
        (self.root / 'server.properties').write_text('online-mode=false\n')
        self.guard.save_settings(dict(mode='enforce', identity_mode='offline'))
        self.guard.heartbeat(dict(self.guard.config(), ready=True, auth_ready=True))
        request = self.request()
        self.guard.prelogin(request)
        result = self.guard.outcome(dict(request, stage='joined'))
        self.panel.record_guard_outcome(result, request)
        self.assertEqual([0, 0, 0], self.fixture.player_record_counts())
        result = self.guard.outcome(dict(request, stage='authenticated'))
        self.panel.record_guard_outcome(result, request)
        self.assertEqual([1, 1, 0], self.fixture.player_record_counts())
        duplicate = self.guard.outcome(dict(request, stage='authenticated'))
        self.panel.record_guard_outcome(duplicate, request)
        self.assertEqual([1, 1, 0], self.fixture.player_record_counts())
        with sqlite3.connect(self.panel.PLAYER_DB_FILE) as conn:
            self.assertEqual(1, conn.execute('SELECT online_status FROM users').fetchone()[0])
        result = self.guard.outcome(dict(request, stage='disconnected'))
        self.panel.record_guard_outcome(result, request)
        with sqlite3.connect(self.panel.PLAYER_DB_FILE) as conn:
            self.assertEqual(0, conn.execute('SELECT online_status FROM users').fetchone()[0])

    def test_private_bridge_requires_token_and_scope_and_is_loopback_only(self):
        self.runtime.port = 0
        self.runtime.start_bridge()
        self.addCleanup(self.runtime.close)
        self.assertEqual('127.0.0.1', self.runtime.httpd.server_address[0])
        body = json.dumps(dict(scope=self.guard.config()['scope']))
        for token, scope, expected in [('', self.guard.config()['scope'], 401),
                                       (self.runtime._token(), 'stale-scope', 409),
                                       (self.runtime._token(), self.guard.config()['scope'], 200)]:
            connection = http.client.HTTPConnection('127.0.0.1', self.runtime.port, timeout=3)
            try:
                connection.request('POST', '/v1/snapshot', json.dumps({'scope': scope}),
                                   {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(expected, response.status, result)
                if expected == 200:
                    self.assertTrue(result['complete'])
                    self.assertEqual(1, result['protocol_version'])
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
