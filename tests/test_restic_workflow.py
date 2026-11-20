"""End-to-end coverage against a real restic CLI, using a private disposable volume."""
import io
import json
from pathlib import Path
import zipfile
import unittest
from unittest.mock import patch

import test_accounts


class ResticWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_accounts.AccountIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        test_accounts.AccountIntegrationTests.tearDownClass()

    def setUp(self):
        self.fixture = test_accounts.AccountIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.app = self.fixture.module
        self.world = self.fixture.root / 'level.dat'
        self.world.write_bytes(b'first world')
        (self.fixture.root / 'ServerEngineInfo').write_text(json.dumps({'server_type': 'Paper', 'mc_version': '1.21.1'}))

    def post(self, url, payload):
        return self.client.post(url, json=payload, headers=self.fixture.headers())

    def test_snapshot_actions_deduplication_and_recovery(self):
        import os
        (self.fixture.root / 'large-region.mca').write_bytes(os.urandom(3 * 1024 * 1024))
        result = self.post('/api/backup/create', {}).json
        group = result['server_key']
        self.assertTrue(result['file'].endswith('.restic'), result)
        marker = self.fixture.data / 'backups' / group / result['file']
        first = json.loads(marker.read_text())
        second = Path(self.app.create_backup('Test Server', False))
        self.assertLess(json.loads(second.read_text())['added_bytes'], first['added_bytes'] // 4)
        listing = self.client.get('/api/backups/list').json
        self.assertEqual({r['format'] for r in listing['backups'][group]}, {'restic'})
        self.assertGreater(listing['storage'][group], 3 * 1024 * 1024)
        self.app.server_backups.restic.run(marker.parent / '.restic-repo', ['check', '--read-data'])
        renamed = self.client.post('/api/backup/rename', data=dict(server_name=group, old_name=marker.name,
                                  new_name='Before exploration'), headers=self.fixture.headers())
        self.assertEqual(renamed.json['status'], 'success')
        marker = marker.with_name('Before exploration.restic')
        with self.client.get('/api/backup/download/' + group + '/' + marker.name) as download:
            self.assertEqual(download.status_code, 200)
            self.assertIn('Before exploration.zip', download.headers['Content-Disposition'])
            with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
                self.assertEqual(archive.read('level.dat'), b'first world')
                self.assertIn('ServerEngineInfo', archive.namelist())
                self.assertIn('panel_config.json', archive.namelist())
                self.assertFalse(any('password' in n or '.restic' in n for n in archive.namelist()))
        self.world.write_bytes(b'current world')
        payload = dict(server_name=group, filename=marker.name)
        with patch.object(self.app.server_backups.restic, 'restore', side_effect=self.app.ResticError('test read error')):
            self.assertEqual(self.post('/api/backup/restore', payload).status_code, 500)
        self.assertEqual(self.world.read_bytes(), b'current world')
        self.assertEqual(self.post('/api/backup/restore', payload).json['status'], 'restored')
        self.assertEqual(self.world.read_bytes(), b'first world')
        saved = self.app.load_config()
        saved['server_name'] = 'Renamed Realm'
        self.app.save_config(saved)
        self.assertEqual(Path(self.app.create_backup('Renamed Realm', True)).parent.name, group)
        self.assertEqual(self.post('/api/backup/delete', payload).json['status'], 'success')
        self.assertFalse(marker.exists())
        self.assertTrue(second.is_file())
        self.assertEqual(self.client.post('/api/import', data={'import_backup_path': group + '/' + second.name},
                                         headers=self.fixture.headers()).status_code, 200)
        self.assertEqual(self.world.read_bytes(), b'first world')
        new = Path(self.app.create_backup('Test Server', True))
        self.assertNotEqual(new.parent.name, group)

    def test_missing_key_preserves_existing_repositories_and_live_files(self):
        path = Path(self.app.create_backup('Test Server', True))
        key = self.fixture.data / 'restic/password'
        key.rename(key.with_suffix('.recovery'))
        self.addCleanup(key.with_suffix('.recovery').rename, key)
        result = self.post('/api/backup/create', {})
        self.assertEqual(result.status_code, 500)
        self.assertFalse(key.exists())
        self.assertTrue(path.is_file())
        self.assertEqual(self.world.read_bytes(), b'first world')
