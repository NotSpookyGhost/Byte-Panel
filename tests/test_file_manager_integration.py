"""File-manager authorization/lifecycle wiring, composed with temporary panel fixtures."""

import io
import unittest
from unittest.mock import Mock, patch

import test_accounts as account_tests


class FileManagerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Composition avoids inheriting and rerunning the entire account suite.
        account_tests.AccountIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        account_tests.AccountIntegrationTests.tearDownClass()

    def setUp(self):
        self.fixture = account_tests.AccountIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.panel = self.fixture.module
        self.client = self.fixture.client
        self.root = self.fixture.root
        self.config = self.fixture.config
        self.target = self.root / 'config.txt'
        self.target.write_bytes(b'initial=true\n')

    def headers(self, client=None):
        return self.fixture.headers(client)

    def client_for_permissions(self, username, permissions):
        return self.fixture.client_for(self.fixture.create(username, permissions))

    def revision(self):
        response = self.client.get('/api/files/entry?path=config.txt')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['revision']

    def mutations(self, client, revision, csrf=True):
        headers = self.headers(client) if csrf else {}
        return (
            ('edit', lambda: client.put('/api/files/file', json={
                'path': 'config.txt', 'revision': revision, 'content': 'changed=true\n'}, headers=headers)),
            ('upload', lambda: client.post('/api/files/upload', data={
                'path': '', 'file': (io.BytesIO(b'upload data'), 'uploaded.dat')}, headers=headers)),
            ('create', lambda: client.post('/api/files/create', json={
                'path': '', 'name': 'created.txt', 'content': 'new'}, headers=headers)),
            ('mkdir', lambda: client.post('/api/files/mkdir', json={
                'path': '', 'name': 'created-folder'}, headers=headers)),
            ('move', lambda: client.post('/api/files/move', json={
                'path': 'config.txt', 'destination': 'moved.txt', 'revision': revision}, headers=headers)),
            ('delete', lambda: client.delete('/api/files/entry', json={
                'path': 'config.txt', 'confirmation': 'config.txt', 'revision': revision}, headers=headers)),
        )

    def test_file_read_permissions_are_distinct_from_plugin_permissions(self):
        cases = (('no-files', [], False), ('plugins-only', ['plugins.manage'], False),
                 ('file-reader', ['files.view'], True), ('file-manager', ['files.manage'], True))
        for username, permissions, allowed in cases:
            client = self.client_for_permissions(username, permissions)
            for endpoint in ('/files', '/api/files/list', '/api/files/file?path=config.txt',
                             '/api/files/entry?path=config.txt', '/api/files/download?path=config.txt'):
                with self.subTest(user=username, endpoint=endpoint):
                    response = client.get(endpoint)
                    try:
                        self.assertEqual(response.status_code, 200 if allowed else 403)
                        if endpoint.startswith('/api/files/'):
                            self.assertIn('no-store', response.headers['Cache-Control'])
                    finally:
                        response.close()
            navigation = client.get('/overview').get_data(as_text=True)
            self.assertEqual('href="/files"' in navigation, allowed)
        anonymous = self.fixture.app.test_client()
        self.assertEqual(anonymous.get('/api/files/list').status_code, 401)

    def test_read_only_users_cannot_use_any_file_mutation(self):
        reader = self.client_for_permissions('readonly-files', ['files.view'])
        revision = self.revision()
        with patch.object(self.panel.server_automation, 'stop') as cancel:
            for name, request in self.mutations(reader, revision):
                with self.subTest(operation=name):
                    response = request()
                    self.assertEqual(response.status_code, 403, response.json)
            cancel.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'initial=true\n')
        self.assertEqual([path.name for path in self.root.iterdir()], ['config.txt'])

    def test_file_management_requires_panel_csrf_for_every_mutation(self):
        manager = self.client_for_permissions('csrf-file-manager', ['files.manage'])
        revision = self.revision()
        with patch.object(self.panel.server_automation, 'stop') as cancel:
            for name, request in self.mutations(manager, revision, csrf=False):
                with self.subTest(operation=name):
                    response = request()
                    self.assertEqual(response.status_code, 403, response.json)
            cancel.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'initial=true\n')

    def test_all_file_writes_reject_live_process_pid_or_finishing_reader(self):
        revision = self.revision()
        for state in ('managed-process', 'configured-pid', 'log-reader'):
            self.panel.MINECRAFT_PROCESS = None
            self.panel.MINECRAFT_LOG_THREAD = None
            self.config['server_pid'] = None
            if state == 'managed-process':
                process = Mock()
                process.poll.return_value = None
                self.panel.MINECRAFT_PROCESS = process
            elif state == 'configured-pid':
                self.config['server_pid'] = 43210
            else:
                reader = Mock()
                reader.is_alive.return_value = True
                self.panel.MINECRAFT_LOG_THREAD = reader
            self.panel.save_config(self.config)
            with patch.object(self.panel.psutil, 'pid_exists', return_value=state == 'configured-pid'), \
                 patch.object(self.panel.server_automation, 'stop') as cancel:
                for name, request in self.mutations(self.client, revision):
                    with self.subTest(state=state, operation=name):
                        response = request()
                        self.assertEqual(response.status_code, 409, response.json)
                cancel.assert_not_called()
                self.assertEqual(self.client.get('/api/files/list').status_code, 200)
                self.assertEqual(self.client.get('/api/files/file?path=config.txt').status_code, 200)
        self.assertEqual(self.target.read_bytes(), b'initial=true\n')

    def test_backup_lock_blocks_all_file_writes_without_blocking_reads(self):
        revision = self.revision()
        with self.panel.server_backups.exclusive(), patch.object(self.panel.server_automation, 'stop') as cancel:
            for name, request in self.mutations(self.client, revision):
                with self.subTest(operation=name):
                    response = request()
                    self.assertEqual(response.status_code, 409, response.json)
            cancel.assert_not_called()
            self.assertEqual(self.client.get('/api/files/list').status_code, 200)
        self.assertEqual(self.target.read_bytes(), b'initial=true\n')

    def test_offline_file_manager_can_create_edit_move_and_recoverably_delete(self):
        manager = self.client_for_permissions('offline-file-manager', ['files.manage'])
        headers = self.headers(manager)
        with patch.object(self.panel.server_automation, 'stop', wraps=self.panel.server_automation.stop) as cancel:
            created = manager.post('/api/files/mkdir', json={'name': 'configs'}, headers=headers)
            self.assertEqual(created.status_code, 201, created.json)
            uploaded = manager.post('/api/files/upload', data={
                'path': 'configs', 'file': (io.BytesIO(b'enabled=true\n'), '.env')}, headers=headers)
            self.assertEqual(uploaded.status_code, 201, uploaded.json)
            opened = manager.get('/api/files/file?path=configs/.env').json
            edited = manager.put('/api/files/file', json={**opened, 'content': 'enabled=false\n'}, headers=headers)
            self.assertEqual(edited.status_code, 200, edited.json)
            recovery = self.fixture.data / 'file-manager-backups' / edited.json['recovery_id'] / 'content'
            self.assertEqual(recovery.read_bytes(), b'enabled=true\n')
            moved = manager.post('/api/files/move', json={
                'path': 'configs/.env', 'destination': 'configs/renamed.env', 'revision': edited.json['revision']}, headers=headers)
            self.assertEqual(moved.status_code, 200, moved.json)
            preview = manager.get('/api/files/entry?path=configs').json
            deleted = manager.delete('/api/files/entry', json={**preview, 'confirmation': 'configs'}, headers=headers)
            self.assertEqual(deleted.status_code, 200, deleted.json)
            self.assertFalse((self.root / 'configs').exists())
            recovered = self.fixture.data / 'file-manager-trash' / deleted.json['recovery_id'] / 'content/renamed.env'
            self.assertEqual(recovered.read_bytes(), b'enabled=false\n')
            self.assertEqual(cancel.call_count, 5)

    def test_files_navigation_for_all_engines_and_mods_only_on_loaders(self):
        for engine, addon_path in (('Forge', '/mods'), ('Fabric', '/mods'), ('Paper', '/plugins'),
                                   ('Purpur', '/plugins'), ('Vanilla', None)):
            self.config['server_type'] = engine
            self.panel.save_config(self.config)
            for endpoint in ('/overview', '/files'):
                with self.subTest(engine=engine, endpoint=endpoint):
                    response = self.client.get(endpoint)
                    self.assertEqual(response.status_code, 200)
                    page = response.get_data(as_text=True)
                    nav = page.split('<nav class="nav-group"')[1].split('</nav>')[0]
                    self.assertIn('href="/files"', nav)
                    self.assertEqual('href="/mods"' in nav, addon_path == '/mods')
                    self.assertEqual('href="/plugins"' in nav, addon_path == '/plugins')
                    if endpoint == '/files':
                        self.assertIn('class="nav-link active" aria-current="page"', nav.split('href="/files"')[1])
            self.assertEqual(self.client.get('/api/files/list').status_code, 200)

    def test_file_manager_cannot_expose_private_panel_account_or_recovery_data(self):
        listing = self.client.get('/api/files/list').json
        self.assertEqual([entry['path'] for entry in listing['entries']], ['config.txt'])
        for relative in ('../panel_accounts.db', '../panel_config.json', '../file-manager-backups',
                         '../file-manager-trash', '../addon-update-settings.json'):
            for endpoint in ('file', 'download', 'entry'):
                with self.subTest(path=relative, endpoint=endpoint):
                    response = self.client.get('/api/files/' + endpoint, query_string={'path': relative})
                    self.assertEqual(response.status_code, 400, response.json)
                    self.assertNotIn('SQLite format', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
