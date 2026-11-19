"""Managed AuthMe safety tests using synthetic JARs and disposable directories."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import guard_auth as module
PUBLISHED_ARTIFACTS = (*module.AUTH_ARTIFACTS, module.PACKET_ARTIFACT)


def jar_bytes(name='AuthMe', version='6.0.1', descriptor=None, extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('plugin.yml', descriptor if descriptor is not None else f'name: {name}\nversion: "{version}"\n')
        if extra is not None:
            archive.writestr('paper-plugin.yml', extra)
    return stream.getvalue()


def artifact(name, payload):
    return dict(name=name, version='6.0.1' if name == 'AuthMe' else '2.14.0',
                filename=name + '.jar', versions=['1.21.4'],
                url='https://cdn.modrinth.com/data/TestProj/versions/TestVers/' + name + '.jar',
                size=len(payload), sha512=hashlib.sha512(payload).hexdigest())


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, _):
        yield self.body[:17]
        yield self.body[17:]


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.trust_env = responses, [], True
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, url, **options):
        self.calls.append((url, options))
        return self.responses.pop(0)


class AuthManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='byte-panel-auth-test-')
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.server = self.data / 'server'
        self.plugins = self.server / 'plugins'
        self.plugins.mkdir(parents=True)
        self.config = dict(server_type='Paper', mc_version='1.21.4')
        self.auth_bytes = jar_bytes()
        self.packet_bytes = jar_bytes('packetevents', '2.14.0')
        self.auth_artifact = artifact('AuthMe', self.auth_bytes)
        self.packet_artifact = artifact('packetevents', self.packet_bytes)
        self.addCleanup(patch.stopall)
        patch.object(module, 'AUTH_ARTIFACTS', (self.auth_artifact,)).start()
        patch.object(module, 'PACKET_ARTIFACT', self.packet_artifact).start()
        self.session = FakeSession([FakeResponse(self.auth_bytes), FakeResponse(self.packet_bytes)])
        self.manager = module.GuardAuth(self.server, self.data, lambda: self.config,
                                       session_factory=lambda: self.session)

    def install(self):
        return self.manager.install()

    def test_manifest_pins_are_well_formed(self):
        # Inspect the actual pinned table, not a downloaded or executed artifact.
        source = Path(module.__file__).read_text('utf-8')
        self.assertNotIn('Hooks.proxy\'', source)
        for entry in PUBLISHED_ARTIFACTS:
            self.assertRegex(entry['sha512'], r'^[0-9a-f]{128}$')
            self.assertGreater(entry['size'], 0)
            self.assertLess(entry['size'], 64 * 1024 * 1024)
            self.assertTrue(entry['url'].startswith('https://cdn.modrinth.com/data/'))

    def test_fresh_install_hashes_config_and_no_credentials(self):
        self.assertTrue(self.manager.status()['can_install'])
        result = self.install()
        self.assertTrue(result['compatible'])
        self.assertEqual((self.plugins / 'AuthMe.jar').read_bytes(), self.auth_bytes)
        self.assertEqual((self.plugins / 'packetevents.jar').read_bytes(), self.packet_bytes)
        self.assertEqual((self.plugins / 'AuthMe.jar').stat().st_nlink, 1)
        settings = module.read_yaml(self.plugins / 'AuthMe/config.yml')
        self.assertEqual(self.manager.issues(settings), [])
        self.assertEqual(module._get(settings, 'settings.security.passwordHash'), 'BCRYPT')
        self.assertEqual(module._get(settings, 'ExternalBoardOptions.bCryptLog2Round'), 12)
        self.assertFalse(module._get(settings, 'settings.enablePremium'))
        self.assertFalse(module._get(settings, 'settings.sessions.enabled'))
        self.assertTrue(module._get(settings, 'Security.SQLProblem.stopServer'))
        self.assertFalse(module._get(settings, 'Protection.geoIpDatabase.enabled'))
        self.assertNotIn('licenseKey', json.dumps(settings))
        self.assertNotIn('mailPassword', json.dumps(settings))
        self.assertFalse(self.session.trust_env)
        self.assertTrue(all(options['allow_redirects'] is False for _, options in self.session.calls))
        self.assertFalse(list(self.plugins.glob('.panel-auth-*')))

    def test_engine_version_restrictions(self):
        for engine, version in [('Forge', '1.21.4'), ('Fabric', '1.21.4'), ('Vanilla', '1.21.4'), ('Paper', '1.2.5')]:
            self.config.update(server_type=engine, mc_version=version)
            self.assertFalse(self.manager.status()['can_install'])
            with self.assertRaises(module.GuardAuthError):
                self.manager.install()
        self.assertEqual(self.session.calls, [])

    def test_conflicting_provider_detected_by_descriptor_not_filename(self):
        (self.plugins / 'totally-renamed.jar').write_bytes(jar_bytes('LoginSecurity', '3.0'))
        result = self.manager.status()
        self.assertFalse(result['can_install'])
        self.assertIn('Conflicting', ' '.join(result['issues']))
        self.assertEqual(result['providers'][0]['name'], 'LoginSecurity')

    def test_duplicate_authme_even_under_different_names(self):
        (self.plugins / 'A.jar').write_bytes(self.auth_bytes)
        (self.plugins / 'B.jar').write_bytes(self.auth_bytes)
        self.assertIn('Conflicting', ' '.join(self.manager.status()['issues']))

    def test_existing_accounts_without_jar_never_overwritten(self):
        directory = self.plugins / 'AuthMe'
        directory.mkdir()
        account = directory / 'authme.db'
        account.write_bytes(b'existing-account-hashes')
        with self.assertRaises(module.GuardAuthError):
            self.install()
        self.assertEqual(account.read_bytes(), b'existing-account-hashes')
        self.assertEqual(self.session.calls, [])

    def test_existing_verified_dependency_is_reused(self):
        (self.plugins / 'renamed-packet.jar').write_bytes(self.packet_bytes)
        self.assertTrue(self.install()['compatible'])
        self.assertEqual(len(self.session.calls), 1)
        self.assertFalse((self.plugins / 'packetevents.jar').exists())

    def test_unverified_dependency_is_not_replaced(self):
        original = jar_bytes('packetevents', '2.1.0')
        (self.plugins / 'packetevents.jar').write_bytes(original)
        with self.assertRaises(module.GuardAuthError):
            self.install()
        self.assertEqual((self.plugins / 'packetevents.jar').read_bytes(), original)

    def test_bad_jars_block_install(self):
        invalids = [b'not a jar', jar_bytes(descriptor='name: AuthMe\nname: Different\nversion: 6.0.1'),
                    jar_bytes(descriptor='name: AuthMe\nversion: 6.0.1\nextra: &cycle [*cycle]'),
                    jar_bytes(extra='name: LoginSecurity\nversion: 1.0'),
                    jar_bytes(descriptor='name: [AuthMe]\nversion: 6.0.1'),
                    jar_bytes(descriptor='name: AuthMe\nversion: 6.0.1\nextra: ' + 'a' * 65536)]
        path = self.plugins / 'ambiguous.jar'
        for value in invalids:
            with self.subTest(size=len(value)):
                path.write_bytes(value)
                self.assertIsNone(module.descriptor(path))
                self.assertFalse(self.manager.status()['can_install'])
        self.assertEqual(self.session.calls, [])

    def test_duplicate_zip_members_rejected(self):
        target = self.plugins / 'duplicate.jar'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(target, 'w') as jar:
                jar.writestr('plugin.yml', 'name: AuthMe\nversion: 6.0.1')
                jar.writestr('plugin.yml', 'name: Innocent\nversion: 1.0')
        self.assertIsNone(module.descriptor(target))
        self.assertFalse(self.manager.status()['can_install'])

    def test_download_failure_leaves_plugins_unchanged(self):
        self.session.responses[1] = FakeResponse(b'corrupt download')
        sentinel = self.plugins / 'unrelated.txt'
        sentinel.write_text('keep')
        with self.assertRaises(module.GuardAuthError):
            self.install()
        self.assertEqual(list(self.plugins.iterdir()), [sentinel])
        self.assertTrue(self.manager.status()['can_install'])

    def test_redirect_rejected(self):
        self.session.responses[0] = FakeResponse(b'', 302)
        with self.assertRaises(module.GuardAuthError):
            self.install()
        self.assertEqual(list(self.plugins.iterdir()), [])

    def test_wrong_descriptor_version_rejected_even_with_matching_hash(self):
        payload = jar_bytes('AuthMe', '5.0.0')
        entry = dict(self.auth_artifact, size=len(payload), sha512=hashlib.sha512(payload).hexdigest())
        self.session.responses[0] = FakeResponse(payload)
        with self.assertRaises(module.GuardAuthError):
            self.manager._download(entry, self.data / 'wrong.jar')

    def test_manifest_rejects_untrusted_origin_or_invalid_hash(self):
        for changes in [{'url': 'https://example.test/plugin.jar'}, {'sha512': '0' * 127},
                        {'size': -1}, {'url': 'https://cdn.modrinth.com/x?secret=value'}]:
            with self.subTest(changes=changes), self.assertRaises(module.GuardAuthError):
                self.manager._download(dict(self.auth_artifact, **changes), self.data / 'never.jar')
        self.assertEqual(self.session.calls, [])

    def test_failed_publication_rolls_back_only_created_files(self):
        sentinel = self.plugins / 'unrelated.txt'
        sentinel.write_bytes(b'do not delete')
        original = module._publish_new
        calls = []
        def fail_second(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError('simulated full disk')
            return original(source, destination)
        with patch.object(module, '_publish_new', side_effect=fail_second), self.assertRaises(OSError):
            self.install()
        self.assertEqual(list(self.plugins.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'do not delete')
        self.assertTrue(self.manager.status()['can_install'])

    def test_failed_copy_leaves_no_partial_loadable_jar(self):
        with patch.object(module.shutil, 'copyfileobj', side_effect=OSError('full disk')), self.assertRaises(OSError):
            self.install()
        self.assertEqual(list(self.plugins.iterdir()), [])
        self.assertTrue(self.manager.status()['can_install'])

    def test_publication_never_overwrites_existing_destination(self):
        source, destination = self.data / 'new', self.plugins / 'existing.jar'
        source.write_bytes(b'new')
        destination.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            module._publish_new(source, destination)
        self.assertEqual(destination.read_bytes(), b'existing')
        self.assertFalse(list(self.plugins.glob('.panel-auth-*')))

    def test_rollback_preserves_unrelated_new_account_data(self):
        original = module._publish_new
        def fail_config(source, destination):
            if destination.name == 'config.yml':
                (destination.parent / 'authme.db').write_bytes(b'external accounts')
                raise OSError('simulated failure')
            return original(source, destination)
        with patch.object(module, '_publish_new', side_effect=fail_config), self.assertRaisesRegex(module.GuardAuthError, 'preserved'):
            self.install()
        self.assertEqual((self.plugins / 'AuthMe/authme.db').read_bytes(), b'external accounts')
        self.assertFalse((self.plugins / 'AuthMe.jar').exists())
        self.assertFalse((self.plugins / 'packetevents.jar').exists())

    def test_repair_changes_only_security_settings_keeps_accounts_and_secrets(self):
        self.install()
        path = self.plugins / 'AuthMe/config.yml'
        settings = module.read_yaml(path)
        module._set(settings, 'settings.sessions.enabled', True)
        module._set(settings, 'DataSource.backend', 'MYSQL')
        module._set(settings, 'DataSource.mySQLPassword', 'fixture-secret')
        module._set(settings, 'settings.messagesLanguage', 'de')
        module._set(settings, 'settings.security.passwordHash', 'SHA256')
        path.write_text(module.yaml.safe_dump(settings), encoding='utf-8')
        before = path.read_bytes()
        accounts = self.plugins / 'AuthMe/authme.db'
        accounts.write_bytes(b'preserve these hashes')
        self.assertTrue(self.manager.status()['can_repair'])
        self.assertNotIn('fixture-secret', json.dumps(self.manager.status()))
        result = self.manager.repair()
        self.assertTrue(result['compatible'])
        repaired = module.read_yaml(path)
        self.assertFalse(module._get(repaired, 'settings.sessions.enabled'))
        self.assertEqual(module._get(repaired, 'DataSource.mySQLPassword'), 'fixture-secret')
        self.assertEqual(module._get(repaired, 'settings.security.passwordHash'), 'SHA256')
        self.assertEqual(module._get(repaired, 'settings.messagesLanguage'), 'de')
        self.assertEqual(accounts.read_bytes(), b'preserve these hashes')
        backups = list((self.data / 'connection-security/config-backups').glob('*.yml'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_yaml_wrong_types_bypasses_and_database_failure_fail_closed(self):
        self.install()
        path = self.plugins / 'AuthMe/config.yml'
        for key, value in [('settings.sessions.enabled', 'false'),
                           ('settings.enablePremium', True), ('Hooks.bungeecord', True),
                           ('settings.restrictions.allowCommands', ['/op']),
                           ('settings.unrestrictions.UnrestrictedName', ['admin']),
                           ('Security.SQLProblem.stopServer', False)]:
            config = module.read_yaml(path)
            module._set(config, key, value)
            self.assertTrue(self.manager.issues(config), key)
        path.write_text('settings:\n  sessions: &x [*x]\n')
        report = self.manager.status()
        self.assertFalse(report['compatible'])
        self.assertFalse(report['can_repair'])

    def test_protected_identity_detected_for_renamed_files(self):
        path = self.plugins / 'renamed.jar'
        path.write_bytes(self.auth_bytes)
        self.assertTrue(module.is_managed_jar(path))
        self.assertIn('plugins/renamed.jar', self.manager.protected_paths())

    def test_linked_files_are_rejected(self):
        source = self.data / 'outside.jar'
        source.write_bytes(self.auth_bytes)
        try:
            os.link(source, self.plugins / 'hardlinked.jar')
        except OSError as error:
            self.skipTest(str(error))
        self.assertFalse(self.manager.status()['can_install'])

    def test_broken_ancestor_link_is_rejected_before_private_write(self):
        link = self.data / 'linked'
        outside = self.data / 'missing-target'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaises(module.GuardAuthError):
            module.atomic_private(link / 'nested' / 'credentials.json', b'never write')
        self.assertFalse(outside.exists())

    def test_yaml_duplicate_keys_and_excessive_depth_are_rejected(self):
        path = self.data / 'test.yml'
        for contents in ['settings: {}\nsettings: {}\n',
                         'root: ' + '[' * 40 + '0' + ']' * 40]:
            path.write_text(contents)
            with self.assertRaises(module.GuardAuthError):
                module.read_yaml(path)

    def test_download_deadline_aborts_before_publishing(self):
        with patch.object(module.time, 'monotonic', side_effect=[0, 121]), self.assertRaisesRegex(module.GuardAuthError, 'timed out'):
            self.install()
        self.assertEqual(list(self.plugins.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
