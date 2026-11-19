"""Pinned offline-auth provisioning. Never owns or resets Minecraft accounts."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time
from urllib.parse import urlsplit
import zipfile
import zlib

import requests
import yaml


GUARD_JAR = 'BytePanelGuard.jar'
KNOWN_PROVIDERS = {'authme', 'authmereloaded', 'nlogin', 'loginsecurity', 'librelogin',
                   'nexauth', 'authplus', 'auth+', 'openlogin', 'limboauth', 'fastlogin'}
# Publisher artifacts, pinned by SHA-512. No runtime "latest" or user-supplied URL.
AUTH_ARTIFACTS = (
    dict(name='AuthMe', version='6.0.1', filename='AuthMe-6.0.1-Spigot-Legacy.jar',
         versions=['1.20.6', '1.21', '1.21.1', '1.21.2', '1.21.3', '1.21.4', '1.21.5'],
         url='https://cdn.modrinth.com/data/9js4IEHC/versions/atrJDDxJ/AuthMe-6.0.1-Spigot-Legacy.jar',
         size=1117257, sha512='56592f76428ba56000a576f6bc434f1a4e635c07165cec13da10ecbafeec43fb03506701150d16b6bbd0022338318f6ffbef2163778864f6b6b03650536e398f'),
    dict(name='AuthMe', version='6.0.1', filename='AuthMe-6.0.1-Spigot-1.21.jar',
         versions=['1.21.6', '1.21.7', '1.21.8', '1.21.9', '1.21.10'],
         url='https://cdn.modrinth.com/data/9js4IEHC/versions/tTOsg5Wq/AuthMe-6.0.1-Spigot-1.21.jar',
         size=1121558, sha512='a72bb9fba35b7b046980d2f7f3c40635accfc04457fcf6dd678f5e6b6f3d58a0831495841b497050241aabb05a647813bcc286bd70e5f2a8c24730a58ad46157'),
    dict(name='AuthMe', version='6.0.1', filename='AuthMe-6.0.1-Paper.jar',
         versions=['1.21.11', '26.1', '26.1.1', '26.1.2', '26.2', '26.3'],
         url='https://cdn.modrinth.com/data/9js4IEHC/versions/fPuH4uYs/AuthMe-6.0.1-Paper.jar',
         size=1153596, sha512='832eb1570f1887493dc09bd183360c6c890aafe24fbc635d36f7c3162c339f064e3a0e082519defe7e67569c1d78072f582cb49c14a24d125d4ece463b765271'),
)
PACKET_ARTIFACT = dict(name='packetevents', version='2.14.0', filename='packetevents-spigot-2.14.0.jar',
    url='https://cdn.modrinth.com/data/HYKaKraK/versions/m78nFxYg/packetevents-spigot-2.14.0.jar',
    size=5723413, sha512='18a942cb55f64783a63b28deea2cebbbe782e5d6a1089f6700430e183bfbce0a45398a640ed0dff4361b55190dcf47017f2ca7788f4a5c8c9efad1f549a63fe3')
SUPPORTED_VERSIONS = frozenset(v for artifact in AUTH_ARTIFACTS for v in artifact['versions'])
REQUIRED_SETTINGS = {
    'settings.sessions.enabled': False,
    'settings.restrictions.allowChat': False,
    'settings.restrictions.allowMovement': False,
    'settings.restrictions.ForceSingleSession': True,
    'settings.restrictions.ProtectInventoryBeforeLogIn': True,
    'settings.restrictions.kickNonRegistered': False,
    'settings.registration.enabled': True,
    'settings.registration.force': True,
    'settings.unrestrictions.UnrestrictedName': [],
    'settings.unrestrictions.UnrestrictedInventories': [],
    'Hooks.bungeecord': False,
    'settings.enablePremium': False,
    'Security.SQLProblem.stopServer': True,
}
ALLOWED_COMMANDS = ['/login', '/log', '/l', '/register', '/reg', '/email', '/captcha', '/2fa', '/totp']


class GuardAuthError(ValueError):
    pass


class _UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise GuardAuthError('Authentication YAML aliases are not supported.')
        self.guard_nodes = getattr(self, 'guard_nodes', 0) + 1
        self.guard_depth = getattr(self, 'guard_depth', 0) + 1
        if self.guard_nodes > 20000 or self.guard_depth > 32:
            raise GuardAuthError('Authentication YAML is too complex.')
        try:
            return super().compose_node(parent, index)
        finally:
            self.guard_depth -= 1


def _mapping(loader, node, deep=False):
    values = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int)) or key in values:
            raise GuardAuthError('Configuration contains duplicate or invalid keys.')
        values[key] = loader.construct_object(value_node, deep=deep)
    return values


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def checked_path(root, relative, missing=False):
    """No symbolic/reparse/hard-link writes, including existing ancestors."""
    root = Path(root).absolute()
    current = root
    for parent in reversed((root, *root.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise GuardAuthError('Security files cannot use linked directories.')
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(p in ('.', '..') or ':' in p for p in parts):
        raise GuardAuthError('Invalid security path.')
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise GuardAuthError('Required security file is missing.') from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise GuardAuthError('Security files must be ordinary, unlinked files.')
    return current


def atomic_private(path, content):
    path = Path(path)
    checked_path(path.parent, path.name, missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_path(path.parent, path.name, missing=True)
    temporary = path.with_name('.panel-' + secrets.token_hex(12))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_yaml(path, maximum=1024 * 1024):
    if path.stat().st_size > maximum:
        raise GuardAuthError('Authentication configuration exceeds the size limit.')
    try:
        with path.open('rb') as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise GuardAuthError('Authentication configuration exceeds the size limit.')
        value = yaml.load(data.decode('utf-8'), Loader=_UniqueLoader)
        if not isinstance(value, dict):
            raise GuardAuthError('Authentication configuration must be a mapping.')
        return value
    except (UnicodeError, yaml.YAMLError, RecursionError):
        raise GuardAuthError('Authentication configuration is not valid safe YAML.') from None


def descriptor(path):
    try:
        with zipfile.ZipFile(path) as jar:
            names = jar.namelist()
            if len(names) > 20000 or len(names) != len(set(names)):
                raise GuardAuthError('Plugin archive has duplicate or excessive entries.')
            found = []
            for name in ('plugin.yml', 'paper-plugin.yml'):
                if name not in names:
                    continue
                info = jar.getinfo(name)
                if (not 0 < info.file_size <= 65536 or info.flag_bits & 1 or info.is_dir()
                        or stat.S_ISLNK(info.external_attr >> 16)):
                    raise GuardAuthError('Plugin descriptor exceeds the safe size limit.')
                with jar.open(info) as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise GuardAuthError('Plugin descriptor exceeds the safe size limit.')
                value = yaml.load(raw.decode('utf-8'), Loader=_UniqueLoader)
                if (not isinstance(value, dict) or not isinstance(value.get('name'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_.+ -]{1,100}', value['name'])
                        or not isinstance(value.get('version'), (str, int, float))):
                    raise GuardAuthError('Plugin descriptor identity is invalid.')
                found.append(dict(name=value['name'], version=str(value['version'])))
            if found and any(item != found[0] for item in found):
                raise GuardAuthError('Plugin descriptors disagree about identity.')
            return found[0] if found else None
    except (zipfile.BadZipFile, UnicodeError, yaml.YAMLError, RecursionError,
            GuardAuthError, RuntimeError, NotImplementedError, EOFError, zlib.error):
        return None


def _file_identity(path):
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _publish_new(source, destination):
    """Stage beside the destination, then publish without replacing any file."""
    fd, name = tempfile.mkstemp(prefix='.panel-auth-', suffix='.tmp', dir=destination.parent)
    temporary, published = Path(name), False
    identity = None
    try:
        with os.fdopen(fd, 'wb') as target, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, target, 65536)
            target.flush()
            os.fsync(target.fileno())
        identity = _file_identity(temporary)
        os.link(temporary, destination)  # Atomic no-replace publication, same filesystem.
        published = True
        temporary.unlink()
        return identity
    except BaseException:
        if published and destination.exists() and _file_identity(destination) == identity:
            destination.unlink()
        raise
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    value = hashlib.sha512()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            value.update(chunk)
    return value.hexdigest()


def _get(data, key):
    for part in key.split('.'):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _set(data, key, value):
    parts = key.split('.')
    for part in parts[:-1]:
        if not isinstance(data.get(part), dict):
            data[part] = {}
        data = data[part]
    data[parts[-1]] = deepcopy(value)


class GuardAuth:
    def __init__(self, server_dir, data_dir, config_provider, session_factory=requests.Session):
        self.server, self.data = Path(server_dir), Path(data_dir)
        self.config_provider, self.session_factory = config_provider, session_factory

    def artifact(self):
        config = self.config_provider()
        if config.get('server_type') not in ('Paper', 'Purpur'):
            raise GuardAuthError('Byte Guard currently requires Paper or Purpur.')
        version = config.get('mc_version')
        artifact = next((a for a in AUTH_ARTIFACTS if version in a['versions']), None)
        if artifact is None:
            raise GuardAuthError('This Minecraft version has no verified Guard authentication combination.')
        return artifact

    def inventory(self):
        root = checked_path(self.server, 'plugins', missing=True)
        if not root.exists():
            return []
        result = []
        for index, path in enumerate(root.iterdir()):
            if index >= 2048:
                raise GuardAuthError('Too many plugin files to verify authentication safely.')
            if path.suffix.lower() != '.jar':
                continue
            checked_path(self.server, path.relative_to(self.server))
            if path.stat().st_size > 64 * 1024 * 1024:
                raise GuardAuthError('A plugin JAR exceeds the safe authentication inspection limit.')
            item = descriptor(path)
            if item is None:
                raise GuardAuthError('A plugin JAR has invalid or ambiguous metadata. Review installed plugins before configuring authentication.')
            result.append(dict(item, filename=path.name, path=path))
        return result

    def issues(self, config):
        issues = []
        for key, value in REQUIRED_SETTINGS.items():
            actual = _get(config, key)
            # Explicitly disabled optional hooks are allowed to be absent.
            if actual is None and (key.startswith('Hooks.') or key.endswith('enablePremium')):
                continue
            if actual != value or (isinstance(value, bool) and not isinstance(actual, bool)):
                issues.append('Required setting: ' + key + ' = ' + json.dumps(value))
        commands = _get(config, 'settings.restrictions.allowCommands')
        if not isinstance(commands, list) or any(not isinstance(c, str) or c.lower() not in ALLOWED_COMMANDS for c in commands):
            issues.append('Unauthenticated commands must be restricted to authentication and recovery commands.')
        return issues

    def status(self):
        report = dict(status='missing', compatible=False, installed=False, can_install=False,
                      can_repair=False, issues=[], providers=[])
        try:
            artifact = self.artifact()
            inventory = self.inventory()
            providers = [p for p in inventory if p['name'].lower() in KNOWN_PROVIDERS]
            report['providers'] = [{k: p[k] for k in ('name', 'version', 'filename')} for p in providers]
            if any(p['name'].lower() != 'authme' for p in providers) or len(providers) > 1:
                raise GuardAuthError('Conflicting authentication providers detected. No automatic migration or second provider installation is allowed.')
            packets = [p for p in inventory if p['name'].lower() == 'packetevents']
            if len(packets) > 1:
                raise GuardAuthError('Multiple PacketEvents copies are installed.')
            if not providers:
                # Existing data without a JAR is not a fresh installation.
                auth_dir = checked_path(self.server, 'plugins/AuthMe', missing=True)
                if auth_dir.exists():
                    raise GuardAuthError('Existing AuthMe data was found without its plugin. Restore a compatible AuthMe JAR; accounts will not be overwritten.')
                report['can_install'] = not packets or digest(packets[0]['path']) == PACKET_ARTIFACT['sha512']
                report['issues'] = ['Install the required authentication components before enabling Guard offline.'
                                    if report['can_install'] else 'Existing PacketEvents is not the verified dependency. Back up and review it before installing AuthMe.']
                return report
            report['installed'] = True
            auth = providers[0]
            report.update(name=auth['name'], version=auth['version'])
            if auth['version'] != artifact['version'] or digest(auth['path']) != artifact['sha512']:
                raise GuardAuthError('Installed AuthMe is not the pinned compatible artifact for this Minecraft version. Update manually after backing up its accounts.')
            if not packets or digest(packets[0]['path']) != PACKET_ARTIFACT['sha512']:
                raise GuardAuthError('Install the verified PacketEvents 2.14.0 dependency before enabling Guard.')
            path = checked_path(self.server, 'plugins/AuthMe/config.yml')
            settings = read_yaml(path)
            report['issues'] = self.issues(settings)
            report['compatible'] = not report['issues']
            report['can_repair'] = bool(report['issues'])
            report['status'] = 'configured' if report['compatible'] else 'configuration_required'
        except (GuardAuthError, OSError) as error:
            report['status'] = 'incompatible'
            report['issues'].append(str(error) if isinstance(error, GuardAuthError) else 'Authentication files could not be read safely.')
        return report

    def require(self):
        report = self.status()
        if not report['compatible']:
            raise GuardAuthError('Offline Byte Guard requires authentication: ' + '; '.join(report['issues']))
        return report

    def _download(self, artifact, path):
        url = urlsplit(artifact['url'])
        if (url.scheme != 'https' or url.hostname != 'cdn.modrinth.com' or url.port not in (None, 443)
                or url.username or url.password or url.query or url.fragment
                or not re.fullmatch(r'[a-f0-9]{128}', artifact['sha512'])
                or not isinstance(artifact['size'], int) or not 0 < artifact['size'] <= 64 * 1024 * 1024):
            raise GuardAuthError('Authentication artifact manifest is invalid.')
        try:
            with self.session_factory() as session:
                session.trust_env = False
                with session.get(artifact['url'], stream=True, timeout=(5, 30), allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise GuardAuthError('The trusted authentication download is unavailable.')
                    total, checksum = 0, hashlib.sha512()
                    deadline = time.monotonic() + 120
                    with path.open('xb') as target:
                        for chunk in response.iter_content(65536):
                            if time.monotonic() > deadline:
                                raise GuardAuthError('Authentication artifact download timed out.')
                            total += len(chunk)
                            if total > artifact['size']:
                                raise GuardAuthError('Authentication artifact exceeds its verified size.')
                            checksum.update(chunk)
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
            if total != artifact['size'] or checksum.hexdigest() != artifact['sha512']:
                raise GuardAuthError('Authentication artifact checksum did not match. Nothing was installed.')
            meta = descriptor(path)
            if (not meta or meta['name'].lower() != artifact['name'].lower()
                    or meta['version'] != artifact['version']):
                raise GuardAuthError('Authentication artifact has an unexpected plugin identity.')
        except requests.RequestException:
            raise GuardAuthError('Could not download the trusted authentication artifact. Retry later.') from None

    def install(self):
        report = self.status()
        if not report['can_install']:
            raise GuardAuthError('Fresh installation is unavailable. ' + '; '.join(report['issues']))
        artifact = self.artifact()
        staging_root = checked_path(self.data, 'connection-security/downloads', missing=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='install-', dir=staging_root) as directory:
            staged = Path(directory)
            components = [artifact]
            if not any(p['name'].lower() == 'packetevents' for p in self.inventory()):
                components.append(PACKET_ARTIFACT)
            for component in components:
                self._download(component, staged / component['filename'])
            # Recheck after network work; never overwrite existing JARs or account data.
            if not self.status()['can_install']:
                raise GuardAuthError('Authentication files changed during download. Retry after reviewing them.')
            plugin_dir = checked_path(self.server, 'plugins', missing=True)
            plugin_dir.mkdir(exist_ok=True)
            for component in components:
                if (plugin_dir / component['filename']).exists():
                    raise GuardAuthError('An installation destination already exists.')
            config = {}
            for key, value in REQUIRED_SETTINGS.items():
                _set(config, key, value)
            _set(config, 'settings.restrictions.allowCommands', ALLOWED_COMMANDS)
            _set(config, 'settings.security.passwordHash', 'BCRYPT')
            _set(config, 'ExternalBoardOptions.bCryptLog2Round', 12)
            _set(config, 'settings.logLevel', 'INFO')
            _set(config, 'settings.useAsyncTasks', True)
            _set(config, 'settings.security.minPasswordLength', 12)
            _set(config, 'settings.security.passwordMaxLength', 64)
            _set(config, 'settings.restrictions.loginTimeout', 30)
            _set(config, 'settings.restrictions.registerTimeout', 60)
            _set(config, 'Protection.geoIpDatabase.enabled', False)
            _set(config, 'DataSource.backend', 'SQLITE')
            staged_config = staged / 'config.yml'
            atomic_private(staged_config, yaml.safe_dump(config, sort_keys=False).encode())
            auth_dir = plugin_dir / 'AuthMe'
            auth_dir.mkdir()  # exclusive creation, accounts can never be overwritten
            directory_identity = _file_identity(auth_dir)[:2]
            created = []
            try:
                for component in components:
                    destination = checked_path(self.server, 'plugins/' + component['filename'], missing=True)
                    identity = _publish_new(staged / component['filename'], destination)
                    created.append((destination, identity))
                config_path = checked_path(self.server, 'plugins/AuthMe/config.yml', missing=True)
                identity = _publish_new(staged_config, config_path)
                created.append((config_path, identity))
                result = self.status()
                if not result['compatible']:
                    raise GuardAuthError('Installed authentication components did not pass verification.')
                return result
            except BaseException as error:
                incomplete = False
                for path, identity in reversed(created):
                    try:
                        checked_path(self.server, path.relative_to(self.server))
                        if _file_identity(path) != identity:
                            incomplete = True
                        else:
                            path.unlink()
                    except (OSError, GuardAuthError):
                        incomplete = True
                try:
                    checked_path(self.server, 'plugins/AuthMe')
                    if _file_identity(auth_dir)[:2] == directory_identity:
                        auth_dir.rmdir()  # Never remove unrelated new files recursively.
                    else:
                        incomplete = True
                except (OSError, GuardAuthError):
                    incomplete = True
                if incomplete:
                    raise GuardAuthError('Authentication installation failed. Some files changed during recovery and were preserved for manual review.') from error
                raise

    def repair(self):
        report = self.status()
        if not report['can_repair']:
            raise GuardAuthError('This installation cannot be repaired automatically.')
        path = checked_path(self.server, 'plugins/AuthMe/config.yml')
        config = read_yaml(path)
        backup = checked_path(self.data, 'connection-security/config-backups', missing=True)
        backup.mkdir(parents=True, exist_ok=True)
        atomic_private(backup / (secrets.token_hex(16) + '.yml'), path.read_bytes())
        for key, value in REQUIRED_SETTINGS.items():
            _set(config, key, value)
        _set(config, 'settings.restrictions.allowCommands', ALLOWED_COMMANDS)
        atomic_private(path, yaml.safe_dump(config, sort_keys=False).encode())
        return self.status()

    def protected_paths(self):
        result = ['plugins/' + GUARD_JAR]
        for item in self.inventory():
            if item['name'].lower() in {'authme', 'packetevents', 'bytepanelguard'}:
                result.append('plugins/' + item['filename'])
        result.append('plugins/AuthMe/config.yml')
        return result


def is_managed_jar(path):
    """Exclude identities, not merely filenames, from generic automatic updates."""
    if Path(path).name.casefold() == GUARD_JAR.casefold():
        return True
    item = descriptor(Path(path))
    return bool(item and item['name'].lower() in {'bytepanelguard', 'authme', 'packetevents'})
