"""Server-file browser tests: isolated fixtures, no Minecraft or external services."""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from flask import Flask, abort
from jinja2 import DictLoader

APP_DIR = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP_DIR))
import server_files
from server_backups import BackupBusy


class ServerFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.server = self.data / 'server'
        self.server.mkdir()
        self.config = dict(is_setup=True, server_type='Forge', server_name='Test Server')
        self.active = False
        self.blocked = False
        self.guard_calls = 0
        self.app = Flask(__name__)
        self.app.config.update(SECRET_KEY='isolated-test', TESTING=True)
        self.app.jinja_loader = DictLoader({'files.html': '<h1>{{ config.server_name }}</h1>'})
        base = self.app.request_class
        class PriorRequest(base):
            @property
            def max_content_length(self):
                if self.path == '/existing-upload':
                    return 12345
                return super().max_content_length
        self.app.request_class = PriorRequest
        server_files.register_file_manager(self.app, self.server, self.data, lambda: self.config,
                                           lambda: {}, self.guard)
        self.client = self.app.test_client()
        with self.client.session_transaction() as cookie:
            cookie['logged_in'] = True

    @contextmanager
    def guard(self):
        self.guard_calls += 1
        if self.blocked == 'busy':
            raise BackupBusy('A backup is already running.')
        if self.blocked:
            abort(409, description='Stop Minecraft and wait for its log reader before editing.')
        self.assertFalse(self.active)
        self.active = True
        try:
            yield
        finally:
            self.active = False

    def put_file(self, relative='config.txt', content=b'original\r\n'):
        path = self.server / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def preview(self, relative):
        response = self.client.get('/api/files/entry', query_string={'path': relative})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def upload(self, contents, name='upload.bin', path='', **form):
        return self.client.post('/api/files/upload', data={
            'file': (io.BytesIO(contents), name), 'path': path, **form})

    def test_authenticated_setup_required_for_all_file_routes(self):
        self.assertEqual(self.app.test_client().get('/api/files/list').status_code, 401)
        self.assertEqual(self.client.get('/files').status_code, 200)
        for engine in ('Vanilla', 'Paper', 'Purpur', 'Forge', 'Fabric'):
            self.config['server_type'] = engine
            self.assertEqual(self.client.get('/api/files/list').status_code, 200)
        self.config['is_setup'] = False
        self.assertEqual(self.client.get('/api/files/list').status_code, 403)
        self.assertEqual(self.upload(b'data').status_code, 403)

    def test_listing_is_scoped_sorted_private_and_browses_dotfiles(self):
        self.put_file('world/level.dat', b'\x00binary')
        self.put_file('.env', b'SERVER=test')
        self.put_file('notes.txt', b'hello')
        self.put_file('.panel-secret-temp', b'hidden')
        (self.data / 'panel_accounts.db').write_bytes(b'private account data')
        response = self.client.get('/api/files/list')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['path'], '')
        self.assertIsNone(response.json['parent'])
        self.assertEqual([entry['name'] for entry in response.json['entries']], ['world', '.env', 'notes.txt'])
        self.assertEqual(response.json['skipped'], 1)
        self.assertEqual(response.json['entries'][0]['kind'], 'folder')
        self.assertTrue(response.json['entries'][1]['editable'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        nested = self.client.get('/api/files/list?path=world').json
        self.assertEqual(nested['parent'], '')
        self.assertFalse(nested['entries'][0]['editable'])
        self.assertNotIn('private account data', response.get_data(as_text=True))

    def test_text_edit_requires_current_revision_and_keeps_recovery_copy(self):
        path = self.put_file()
        opened = self.client.get('/api/files/file?path=config.txt')
        self.assertEqual(opened.json['content'], 'original\r\n')
        payload = dict(path='config.txt', revision=opened.json['revision'], content='changed\r\n')
        saved = self.client.put('/api/files/file', json=payload)
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(path.read_bytes(), b'changed\r\n')
        self.assertEqual(saved.json['revision'], hashlib.sha256(b'changed\r\n').hexdigest())
        recovery = self.data / 'file-manager-backups' / saved.json['recovery_id']
        self.assertEqual((recovery / 'content').read_bytes(), b'original\r\n')
        metadata = json.loads((recovery / 'metadata.json').read_text())
        self.assertEqual(metadata['path'], 'config.txt')
        self.assertEqual(metadata['operation'], 'edit')
        self.assertEqual(self.client.put('/api/files/file', json=payload).status_code, 409)
        self.assertEqual(path.read_bytes(), b'changed\r\n')
        self.assertFalse(list(self.server.glob('.panel-*')))

    def test_text_limits_binary_and_invalid_payloads_are_json_errors(self):
        self.put_file('binary.dat', b'hello\x00data')
        self.put_file('invalid.txt', b'\xff')
        self.put_file('large.txt', b'123456789')
        for path in ('binary.dat', 'invalid.txt'):
            response = self.client.get('/api/files/file', query_string={'path': path})
            self.assertEqual(response.status_code, 415)
            self.assertEqual(response.json['status'], 'error')
        with patch.object(server_files, 'MAX_TEXT_BYTES', 8):
            self.assertEqual(self.client.get('/api/files/file?path=large.txt').status_code, 413)
            self.assertEqual(self.client.post('/api/files/create', json={'name': 'too-big.txt', 'content': '123456789'}).status_code, 413)
        for payload in (None, [], 'text', {'path': None}, {'path': 'large.txt', 'content': 4},
                        {'path': 'large.txt', 'content': 'a', 'revision': '\u2603'}):
            response = self.client.put('/api/files/file', json=payload)
            self.assertIn(response.status_code, (400, 409))
            self.assertIsNotNone(response.json)

    def test_upload_streams_binary_without_extracting_and_downloads_attachment(self):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, 'w') as archive:
            archive.writestr('../escape.txt', 'must never extract')
        data = contents.getvalue()
        (self.server / 'mods').mkdir()
        response = self.upload(data, 'archive.zip', 'mods')
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json['path'], 'mods/archive.zip')
        self.assertEqual((self.server / 'mods/archive.zip').read_bytes(), data)
        self.assertFalse((self.data / 'escape.txt').exists())
        downloaded = self.client.get('/api/files/download?path=mods/archive.zip')
        try:
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.data, data)
            self.assertEqual(downloaded.content_length, len(data))
            self.assertIn('attachment', downloaded.headers['Content-Disposition'])
            self.assertEqual(downloaded.headers['X-Content-Type-Options'], 'nosniff')
        finally:
            downloaded.close()

    def test_confirmed_upload_replacement_preserves_original_and_rejects_stale_revision(self):
        path = self.put_file('data.bin', b'old\x00bytes')
        conflict = self.upload(b'new\x00bytes', 'data.bin')
        self.assertEqual(conflict.status_code, 409)
        self.assertTrue(conflict.json['conflict'])
        saved = self.upload(b'new\x00bytes', 'data.bin', replace_revision=conflict.json['revision'])
        self.assertEqual(saved.status_code, 201, saved.json)
        self.assertEqual(path.read_bytes(), b'new\x00bytes')
        self.assertEqual((self.data / 'file-manager-backups' / saved.json['recovery_id'] / 'content').read_bytes(), b'old\x00bytes')
        self.assertEqual(self.upload(b'wrong', 'data.bin', replace_revision=conflict.json['revision']).status_code, 409)
        self.assertEqual(self.upload(b'wrong', 'missing.bin', replace_revision=conflict.json['revision']).status_code, 409)

    def test_upload_limit_and_failure_clean_up_temporary_file_inside_guard(self):
        original = server_files._Directory.unlink
        def checked_unlink(directory, name):
            self.assertTrue(self.active, 'Temporary cleanup must hold the mutation guard')
            return original(directory, name)
        with patch.object(server_files, 'MAX_UPLOAD_BYTES', 8), \
             patch.object(server_files._Directory, 'unlink', checked_unlink):
            response = self.upload(b'123456789')
        self.assertEqual(response.status_code, 413, response.json)
        self.assertEqual(list(self.server.iterdir()), [])
        with patch.object(server_files, '_publish', side_effect=OSError('publish failed')):
            with self.assertLogs(self.app.logger, level='ERROR'):
                response = self.upload(b'content')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(list(self.server.iterdir()), [])

    def test_create_folder_and_text_file_never_overwrite(self):
        created = self.client.post('/api/files/mkdir', json={'path': '', 'name': '.settings'})
        self.assertEqual(created.status_code, 201, created.json)
        created = self.client.post('/api/files/create', json={'path': '.settings', 'name': 'new.conf', 'content': 'ready=true'})
        self.assertEqual(created.status_code, 201, created.json)
        self.assertEqual((self.server / '.settings/new.conf').read_text(), 'ready=true')
        self.assertEqual(self.client.post('/api/files/mkdir', json={'name': '.settings'}).status_code, 409)
        self.assertEqual(self.client.post('/api/files/create', json={'path': '.settings', 'name': 'new.conf', 'content': 'wrong'}).status_code, 409)
        self.assertEqual((self.server / '.settings/new.conf').read_text(), 'ready=true')

    def test_move_file_and_nested_folder_preserves_contents_without_overwrite(self):
        self.put_file('world/region/data.mca', b'chunks')
        (self.server / 'destination').mkdir()
        preview = self.preview('world')
        response = self.client.post('/api/files/move', json={**preview, 'destination': 'destination/renamed'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual((self.server / 'destination/renamed/region/data.mca').read_bytes(), b'chunks')
        self.assertFalse((self.server / 'world').exists())
        path = 'destination/renamed/region/data.mca'
        response = self.client.post('/api/files/move', json={**self.preview(path), 'destination': 'data.mca'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual((self.server / 'data.mca').read_bytes(), b'chunks')
        self.put_file('collision.mca', b'keep')
        response = self.client.post('/api/files/move', json={**self.preview('data.mca'), 'destination': 'collision.mca'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((self.server / 'collision.mca').read_bytes(), b'keep')
        self.assertTrue((self.server / 'data.mca').exists())

    def test_move_rejects_self_descendant_stale_and_root_operations(self):
        target = self.put_file('folder/child.txt')
        preview = self.preview('folder')
        for destination in ('folder', 'folder/nested', ''):
            self.assertEqual(self.client.post('/api/files/move', json={**preview, 'destination': destination}).status_code, 400)
        target.write_bytes(b'changed after preview')
        self.assertEqual(self.client.post('/api/files/move', json={**preview, 'destination': 'elsewhere'}).status_code, 409)
        self.assertEqual(self.client.post('/api/files/move', json={'path': '', 'destination': 'other'}).status_code, 400)
        self.assertEqual(self.client.delete('/api/files/entry', json={'path': '', 'confirmation': ''}).status_code, 400)

    def test_delete_file_and_folder_are_recoverable_with_exact_confirmation(self):
        for path in ('one.txt', 'world'):
            with self.subTest(path=path):
                target = self.put_file('world/region/data.mca' if path == 'world' else path, b'valuable bytes')
                preview = self.preview(path)
                self.assertEqual(self.client.delete('/api/files/entry', json=preview).status_code, 400)
                response = self.client.delete('/api/files/entry', json={**preview, 'confirmation': path})
                self.assertEqual(response.status_code, 200, response.json)
                self.assertFalse((self.server / path).exists())
                recovery = self.data / 'file-manager-trash' / response.json['recovery_id']
                recovered = recovery / 'content' / 'region/data.mca' if path == 'world' else recovery / 'content'
                self.assertEqual(recovered.read_bytes(), b'valuable bytes')
                self.assertEqual(json.loads((recovery / 'metadata.json').read_text())['path'], path)

    def test_all_mutations_obey_guard_but_reads_work_while_running(self):
        target = self.put_file()
        preview = self.preview('config.txt')
        self.blocked = True
        operations = (
            lambda: self.client.put('/api/files/file', json={**preview, 'content': 'new'}),
            lambda: self.upload(b'upload'),
            lambda: self.client.post('/api/files/create', json={'name': 'new.txt'}),
            lambda: self.client.post('/api/files/mkdir', json={'name': 'new'}),
            lambda: self.client.post('/api/files/move', json={**preview, 'destination': 'new.txt'}),
            lambda: self.client.delete('/api/files/entry', json={**preview, 'confirmation': 'config.txt'}),
        )
        for operation in operations:
            response = operation()
            self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(self.guard_calls, len(operations))
        self.assertEqual(target.read_bytes(), b'original\r\n')
        self.assertEqual(self.client.get('/api/files/list').status_code, 200)
        self.assertEqual(self.client.get('/api/files/file?path=config.txt').status_code, 200)

    def test_paths_never_escape_server_or_expose_panel_credentials(self):
        (self.data / 'secret.txt').write_text('private')
        invalid = ('../secret.txt', '/secret.txt', '..\\secret.txt', 'C:/secret.txt', 'folder//file',
                   'folder/./file', 'folder/../file', '.panel-temp', 'file\x00name', 'file\nname',
                   'file:stream', 'file.', 'file ', 'CON', 'NUL.txt', 'bad\ud800')
        for path in invalid:
            with self.subTest(path=path):
                response = self.client.get('/api/files/file', query_string={'path': path}) if '\ud800' not in path else (
                    self.client.post('/api/files/create', json={'name': path}))
                self.assertEqual(response.status_code, 400, response.json)
                self.assertEqual(self.client.post('/api/files/mkdir', json={'name': path}).status_code, 400)
        self.assertEqual((self.data / 'secret.txt').read_text(), 'private')
        self.assertEqual(list(self.server.iterdir()), [])

    def test_list_and_recursive_preview_have_bounded_entry_counts(self):
        self.put_file('tree/one.txt')
        self.put_file('tree/two.txt')
        with patch.object(server_files, 'MAX_LIST_ENTRIES', 1):
            self.assertEqual(self.client.get('/api/files/list?path=tree').status_code, 413)
        with patch.object(server_files, 'MAX_TREE_ENTRIES', 2):
            self.assertEqual(self.client.get('/api/files/entry?path=tree').status_code, 413)
        self.assertTrue((self.server / 'tree/one.txt').exists())

    def test_hard_links_cannot_be_read_downloaded_or_modified(self):
        outside = self.data / 'private.txt'
        outside.write_bytes(b'private')
        try:
            os.link(outside, self.server / 'hard.txt')
        except OSError:
            self.skipTest('Hard links are not supported on this test volume')
        for endpoint in ('file', 'download', 'entry'):
            self.assertEqual(self.client.get('/api/files/' + endpoint + '?path=hard.txt').status_code, 403)
        self.assertEqual(self.upload(b'changed', 'hard.txt').status_code, 403)
        self.assertEqual(self.client.get('/api/files/list').json['entries'], [])
        self.assertEqual(outside.read_bytes(), b'private')

    def test_symlink_ancestors_and_recursive_links_are_rejected(self):
        outside = self.data / 'private'
        outside.mkdir()
        (outside / 'secret.txt').write_bytes(b'private')
        (self.server / 'tree').mkdir()
        try:
            (self.server / 'linked').symlink_to(outside, target_is_directory=True)
            (self.server / 'tree/link.txt').symlink_to(outside / 'secret.txt')
        except OSError:
            self.skipTest('This host does not permit symbolic links')
        self.assertEqual(self.client.get('/api/files/list?path=linked').status_code, 403)
        self.assertEqual(self.client.get('/api/files/file?path=linked/secret.txt').status_code, 403)
        self.assertEqual(self.upload(b'changed', 'secret.txt', 'linked').status_code, 403)
        self.assertEqual(self.client.get('/api/files/entry?path=tree').status_code, 403)
        self.assertEqual(self.client.delete('/api/files/entry', json={'path': 'tree', 'confirmation': 'tree', 'revision': 'bad'}).status_code, 403)
        self.assertEqual((outside / 'secret.txt').read_bytes(), b'private')

    def test_recovery_failure_does_not_replace_or_delete_original(self):
        target = self.put_file()
        preview = self.preview('config.txt')
        with patch.object(server_files.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertLogs(self.app.logger, level='ERROR'):
                edited = self.client.put('/api/files/file', json={**preview, 'content': 'wrong'})
                deleted = self.client.delete('/api/files/entry', json={**preview, 'confirmation': 'config.txt'})
        self.assertEqual(edited.status_code, 500)
        self.assertEqual(deleted.status_code, 500)
        self.assertEqual(target.read_bytes(), b'original\r\n')

    def test_external_change_during_snapshot_is_not_overwritten(self):
        target = self.put_file()
        preview = self.preview('config.txt')
        original = server_files.os.fsync
        def changed_during_snapshot(fd):
            original(fd)
            target.write_bytes(b'new external edit')
        with patch.object(server_files.os, 'fsync', side_effect=changed_during_snapshot):
            result = self.client.put('/api/files/file', json={**preview, 'content': 'stale browser edit'})
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(target.read_bytes(), b'new external edit')
        self.assertFalse(list(self.server.glob('.panel-*')))

    def test_destination_race_cannot_overwrite_file_created_before_publication(self):
        original = server_files._rename_noreplace
        def appeared(source, name, destination, new_name):
            self.assertTrue(self.active)
            (destination.path / new_name).write_bytes(b'external file must survive')
            return original(source, name, destination, new_name)
        with patch.object(server_files, '_rename_noreplace', side_effect=appeared):
            result = self.upload(b'panel upload', 'raced.txt')
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual((self.server / 'raced.txt').read_bytes(), b'external file must survive')
        self.assertFalse(list(self.server.glob('.panel-*')))

    def test_recovery_directories_cannot_redirect_writes_through_links(self):
        outside = self.data / 'outside-recovery'
        outside.mkdir()
        try:
            (self.data / 'file-manager-backups').symlink_to(outside, target_is_directory=True)
            (self.data / 'file-manager-trash').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('This host does not permit symbolic links')
        target = self.put_file()
        preview = self.preview('config.txt')
        result = self.client.put('/api/files/file', json={**preview, 'content': 'wrong'})
        self.assertEqual(result.status_code, 403)
        result = self.client.delete('/api/files/entry', json={**preview, 'confirmation': 'config.txt'})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(target.read_bytes(), b'original\r\n')
        self.assertEqual(list(outside.iterdir()), [])

    def test_recursive_hard_links_and_special_nodes_block_folder_mutation(self):
        target = self.put_file('tree/config.txt')
        try:
            os.link(target, self.data / 'outside-hardlink.txt')
        except OSError:
            self.skipTest('Hard links are unavailable on this volume')
        self.assertEqual(self.client.get('/api/files/entry?path=tree').status_code, 403)
        self.assertEqual(self.client.post('/api/files/move', json={
            'path': 'tree', 'destination': 'moved', 'revision': 'bad'}).status_code, 403)
        self.assertEqual(self.client.delete('/api/files/entry', json={
            'path': 'tree', 'confirmation': 'tree', 'revision': 'bad'}).status_code, 403)
        self.assertTrue(target.exists())

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO checks run on Unix')
    def test_special_fifo_is_never_opened_or_recursively_moved(self):
        (self.server / 'tree').mkdir()
        os.mkfifo(self.server / 'tree/pipe')
        self.assertEqual(self.client.get('/api/files/file?path=tree/pipe').status_code, 403)
        self.assertEqual(self.client.get('/api/files/entry?path=tree').status_code, 403)
        self.assertEqual(self.client.get('/api/files/list?path=tree').json['entries'], [])

    def test_busy_guard_returns_json_and_leaves_original_untouched(self):
        target = self.put_file()
        preview = self.preview('config.txt')
        self.blocked = 'busy'
        response = self.client.put('/api/files/file', json={**preview, 'content': 'new'})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertIn('backup', response.json['error'])
        self.assertEqual(target.read_bytes(), b'original\r\n')

    def test_request_limits_preserve_prior_request_class(self):
        for path, limit in (('/api/files/upload', server_files.MAX_UPLOAD_BYTES + 1024 * 1024),
                            ('/api/files/file', 8 * server_files.MAX_TEXT_BYTES),
                            ('/api/files/move', 64 * 1024), ('/existing-upload', 12345)):
            with self.app.test_request_context(path):
                self.assertEqual(self.app.request_class.max_content_length.fget(__import__('flask').request), limit)


if __name__ == '__main__':
    unittest.main()
