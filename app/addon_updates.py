"""Opt-in, stopped-server add-on updates from hash-verified Modrinth releases.

No import-time workers, arbitrary download URLs, plugin execution or dependency installs.
The caller supplies the same mutation guard used by server start/restore/upload actions.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

import requests
from addon_retention import BACKUP_KEEP, BACKUP_MAX_AGE_DAYS, prune_update_backups
from guard_auth import is_managed_jar


MAX_JARS = 64
MAX_JAR_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
API_ROOT = 'https://api.modrinth.com/v2'
USER_AGENT = 'NotSpookyGhost/Byte-Panel (https://github.com/NotSpookyGhost/Byte-Panel)'
ENGINES = {'Paper': ('plugins', ('paper', 'spigot', 'bukkit')),
           'Purpur': ('plugins', ('purpur', 'paper', 'spigot', 'bukkit')),
           'Forge': ('mods', ('forge',)), 'Fabric': ('mods', ('fabric',))}
ID_PATTERN = re.compile(r'^[A-Za-z0-9]{1,64}$')
HASH_PATTERN = re.compile(r'^[a-fA-F0-9]{128}$')


class AddonUpdateError(ValueError):
    pass


class AddonUpdateBusy(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _date(value):
    if not isinstance(value, str):
        raise AddonUpdateError('Release publication time is missing.')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except ValueError:
        raise AddonUpdateError('Release publication time is invalid.') from None


def _identity(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def _safe_file(path):
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or path.is_symlink():
        raise AddonUpdateError('Symbolic links, hard links and non-regular add-ons cannot be updated.')
    if details.st_size > MAX_JAR_BYTES:
        raise AddonUpdateError('Add-on exceeds the automatic update size limit (128 MiB).')
    return details


def _signature(details):
    return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns


def _hash_file(path):
    before = _safe_file(path)
    digest = hashlib.sha512()
    count = 0
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise AddonUpdateError('Add-on changed while it was being checked. Check again.')
        while True:
            chunk = stream.read(128 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_JAR_BYTES:
                raise AddonUpdateError('Add-on exceeds the automatic update size limit (128 MiB).')
            digest.update(chunk)
    if _signature(_safe_file(path)) != _signature(before):
        raise AddonUpdateError('Add-on changed while it was being checked. Check again.')
    return digest.hexdigest()


def _artifact(version):
    files = version.get('files')
    if not isinstance(files, list):
        raise AddonUpdateError('Release file metadata is missing.')
    jars = [item for item in files if isinstance(item, dict)
            and isinstance(item.get('filename'), str) and item['filename'].lower().endswith('.jar')
            and item.get('file_type') in (None, 'unknown')]
    # A version may contain separate loader builds: do not guess from filenames/primary.
    if len(jars) != 1:
        raise AddonUpdateError('Release has ambiguous JAR files; update it manually.')
    item = jars[0]
    digest = item.get('hashes', {}).get('sha512') if isinstance(item.get('hashes'), dict) else None
    if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
        raise AddonUpdateError('Release has no valid SHA-512 checksum.')
    if type(item.get('size')) is not int or not 0 < item['size'] <= MAX_JAR_BYTES:
        raise AddonUpdateError('Release file size is missing or exceeds 128 MiB.')
    url = item.get('url')
    if not isinstance(url, str) or any(ord(char) < 33 for char in url):
        raise AddonUpdateError('Release download URL is invalid.')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'cdn.modrinth.com'
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    decoded = unquote(unquote(parsed.path)) if valid else ''
    if (not valid or '\\' in decoded or any(part in ('.', '..') for part in decoded.split('/'))
            or not decoded.startswith('/data/' + version['project_id'] + '/versions/')):
        raise AddonUpdateError('Only direct, project-matched Modrinth CDN downloads are allowed.')
    return dict(url=url, sha512=digest.lower(), size=item['size'])


class AddonUpdateService:
    def __init__(self, server_dir, state_path, load_config, is_running, mutation_guard,
                 session_factory=None):
        self.server = Path(server_dir)
        self.state_path = Path(state_path)
        self.backup_dir = self.state_path.parent / 'addon-update-backups'
        self.load_config = load_config
        self.is_running = is_running
        self.mutation_guard = mutation_guard
        self.session_factory = session_factory or requests.Session
        self._lock = threading.RLock()
        self._work = threading.Lock()
        self._generation = 0
        self._enabled, self._projects = False, set()
        self._rows, self._records = [], []
        self._status, self._message = 'idle', 'Check installed add-ons to identify trusted update sources.'
        self._last_checked = self._last_applied = None
        self._cleanup_report = dict(last_run=None, removed=0, retained=0, protected=0, warnings=[])
        try:
            if not self.state_path.is_symlink() and self.state_path.stat().st_size <= 65536:
                saved = json.loads(self.state_path.read_text(encoding='utf-8'))
                projects = saved.get('projects', [])
                if isinstance(projects, list) and len(projects) <= MAX_JARS and all(_identity(x) for x in projects):
                    self._enabled = saved.get('enabled') is True
                    self._projects = set(projects)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def snapshot(self):
        config = self.load_config()
        with self._lock:
            return dict(enabled=self._enabled, projects=sorted(self._projects), busy=self._work.locked(),
                        addons=[{**row, 'enabled': row.get('project_id') in self._projects} for row in self._rows],
                        status=self._status, message=self._message, last_checked=self._last_checked,
                        last_applied=self._last_applied, engine=config.get('server_type'),
                        mc_version=config.get('mc_version'),
                        backup_retention=dict(keep=BACKUP_KEEP, max_age_days=BACKUP_MAX_AGE_DAYS),
                        backup_cleanup={**self._cleanup_report, 'warnings': list(self._cleanup_report['warnings'])})

    def _save(self):
        if self.state_path.is_symlink() or self.state_path.parent.is_symlink():
            raise AddonUpdateError('Update settings must not use symbolic links.')
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.server.resolve() == self.state_path.parent.resolve() or self.server.resolve() in self.state_path.parent.resolve().parents:
            raise AddonUpdateError('Update settings must be outside Minecraft server files.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.state_path.parent,
                                             prefix='.addon-settings-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(dict(enabled=self._enabled, projects=sorted(self._projects)), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def configure(self, enabled, projects):
        if type(enabled) is not bool or not isinstance(projects, list) or len(projects) > MAX_JARS or not all(_identity(x) for x in projects):
            raise AddonUpdateError('Choose valid verified projects and an enabled/disabled setting.')
        with self._lock:
            known = {row.get('project_id') for row in self._rows if row.get('project_id')}
            if not set(projects).issubset(known | self._projects):
                raise AddonUpdateError('Check installed add-ons before approving their verified projects.')
            previous = self._enabled, self._projects
            self._enabled, self._projects = enabled, set(projects)
            try:
                self._save()
            except Exception:
                self._enabled, self._projects = previous
                raise
            self._generation += 1
        return self.snapshot()

    def reset(self):
        with self._lock:
            self._enabled, self._projects = False, set()
            self._rows, self._records = [], []
            self._generation += 1
            self._save()
            self._message = 'Automatic add-on updates were reset for the replacement server.'
        return self.snapshot()

    def _set(self, status, message):
        with self._lock:
            self._status, self._message = status, message

    def start(self, action):
        if action not in ('check', 'apply'):
            raise AddonUpdateError('Unknown add-on update action.')
        if not self._work.acquire(blocking=False):
            raise AddonUpdateBusy('An add-on update task is already running.')
        self._set('checking' if action == 'check' else 'applying', 'Preparing add-on update task...')
        try:
            threading.Thread(target=self._execute, args=(action,), daemon=True,
                             name='addon-updates').start()
        except Exception:
            self._work.release()
            self._set('error', 'Could not start the add-on update task.')
            raise
        return self.snapshot()

    def _sync(self, action):
        if not self._work.acquire(blocking=False):
            raise AddonUpdateBusy('An add-on update task is already running.')
        self._execute(action)
        return self.snapshot()

    def check(self):
        return self._sync('check')

    def run_once(self):
        return self._sync('apply')

    def cleanup_backups(self):
        """Maintenance also runs when updates are disabled; never race an update."""
        if not self._work.acquire(blocking=False):
            return None
        try:
            return self._cleanup_backups()
        finally:
            self._work.release()

    def _cleanup_backups(self):
        # Called with _work reserved. Cleanup cannot change the update result.
        try:
            report = prune_update_backups(self.backup_dir, self.server)
        except Exception:
            report = dict(removed=0, retained=0, protected=0,
                          warnings=['Update backup cleanup could not finish. Check storage permissions; existing recovery copies were not intentionally discarded.'])
        with self._lock:
            self._cleanup_report = dict(last_run=_now(), **report)
            return {**self._cleanup_report, 'warnings': list(self._cleanup_report['warnings'])}

    def _execute(self, action):
        try:
            if action == 'apply':
                self._apply()
            else:
                self._check()
        except AddonUpdateError as error:
            self._set('error', str(error))
        except Exception:
            self._set('error', 'Add-on update task failed safely. Check permissions, network access and server state.')
        finally:
            try:
                self._cleanup_backups()
            finally:
                self._work.release()

    @contextmanager
    def _session(self):
        session = self.session_factory()
        session.trust_env = False
        session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})
        try:
            yield session
        finally:
            session.close()

    def _api(self, session, path, body=None):
        response = session.request('GET' if body is None else 'POST', API_ROOT + path,
                                   json=body, timeout=(5, 20), allow_redirects=False, stream=True)
        try:
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                raise AddonUpdateError('Modrinth rate limit reached. Wait before checking again.')
            if response.status_code != 200:
                raise AddonUpdateError('Modrinth update information is unavailable. Try later.')
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_JSON_BYTES:
                    raise AddonUpdateError('Modrinth metadata exceeds the safe size limit.')
            try:
                return json.loads(data)
            except ValueError:
                raise AddonUpdateError('Modrinth returned invalid update metadata.') from None
        finally:
            response.close()

    def _scope(self):
        config = self.load_config()
        engine, mc = config.get('server_type'), config.get('mc_version')
        if config.get('is_setup') is not True or engine not in ENGINES or not isinstance(mc, str) or not mc:
            raise AddonUpdateError('Automatic add-on updates support configured Paper, Purpur, Forge and Fabric servers.')
        if self.server.is_symlink() or not self.server.is_dir():
            raise AddonUpdateError('Server directory is missing or uses a symbolic link.')
        info = self.server.stat()
        return engine, mc, config.get('server_name', ''), info.st_dev, info.st_ino

    def _inventory(self, scope):
        root = self.server / ENGINES[scope[0]][0]
        if root.is_symlink():
            raise AddonUpdateError('Add-on directory must not be a symbolic link.')
        if not root.exists():
            return []
        if not root.is_dir() or root.resolve().parent != self.server.resolve():
            raise AddonUpdateError('Invalid add-on directory.')
        paths = sorted((path for path in root.iterdir() if path.name.lower().endswith('.jar')), key=lambda p: p.name)
        if len(paths) > MAX_JARS:
            raise AddonUpdateError('Automatic checks are limited to 64 JARs; manage this larger set manually.')
        for path in paths:
            _safe_file(path)
        # Security components use their pinned compatibility manifest, not the
        # generic latest-compatible update flow (even when Guard is disabled).
        paths = [path for path in paths if not is_managed_jar(path)]
        return [dict(path=path, filename=path.name, sha512=_hash_file(path)) for path in paths]

    def _check(self):
        self._set('checking', 'Identifying installed JARs by SHA-512; checking compatible stable releases...')
        scope = self._scope()
        with self._lock:
            generation = self._generation
        records = self._inventory(scope)
        rows = []
        with self._session() as session:
            for record in records:
                row = dict(filename=record['filename'], source='modrinth', project_id=None,
                           installed_version=None, available_version=None, status='unknown',
                           reason='This exact JAR is not identified by Modrinth; update it manually.')
                record['row'] = row
                rows.append(row)
                installed = self._api(session, '/version_file/' + record['sha512'] + '?algorithm=sha512&multiple=true')
                if isinstance(installed, list):
                    installed = installed[0] if len(installed) == 1 else None
                if not isinstance(installed, dict):
                    continue
                if not _identity(installed.get('project_id')) or not _identity(installed.get('id')):
                    continue
                files = installed.get('files', [])
                if not isinstance(files, list) or not any(isinstance(item, dict) and isinstance(item.get('hashes'), dict)
                        and item['hashes'].get('sha512', '').lower() == record['sha512'] for item in files):
                    continue
                record['installed'] = installed
                row.update(project_id=installed['project_id'], installed_version=str(installed.get('version_number', installed['id'])),
                           status='current', reason='No newer compatible stable release was found.')
                candidate = self._api(session, '/version_file/' + record['sha512'] + '/update?algorithm=sha512',
                                      dict(loaders=list(ENGINES[scope[0]][1]), game_versions=[scope[1]], version_types=['release']))
                if not isinstance(candidate, dict) or candidate.get('id') == installed['id']:
                    continue
                try:
                    self._validate_candidate(candidate, installed, scope)
                    artifact = _artifact(candidate)
                    if artifact['sha512'] == record['sha512']:
                        continue
                    record.update(candidate=candidate, artifact=artifact)
                    row.update(available_version=str(candidate.get('version_number', candidate['id'])),
                               status='update_available', reason='Compatible stable release available; approval and stopped server required.')
                except AddonUpdateError as error:
                    row.update(status='blocked', reason=str(error))
        known = [record['installed'] for record in records if 'installed' in record]
        for record in records:
            if 'candidate' not in record:
                continue
            try:
                if sum(item['project_id'] == record['installed']['project_id'] for item in known) != 1:
                    raise AddonUpdateError('Multiple installed JARs match this project; resolve duplicates manually.')
                self._dependencies(record['candidate'], known)
            except AddonUpdateError as error:
                record.pop('candidate', None)
                record['row'].update(status='blocked', reason=str(error))
        current_scope = self._scope()
        with self._lock:
            if generation != self._generation or scope != current_scope:
                raise AddonUpdateError('Server or update approvals changed. Check again.')
            self._records, self._rows, self._last_checked = records, rows, _now()
            self._status, self._message = 'idle', f'Checked {len(rows)} add-ons; {sum(row["status"] == "update_available" for row in rows)} compatible updates available.'
        return scope, generation, records

    def _validate_candidate(self, version, installed, scope):
        if version.get('project_id') != installed['project_id'] or not _identity(version.get('id')):
            raise AddonUpdateError('Update project identity does not match the installed JAR.')
        if version.get('version_type') != 'release' or version.get('status') != 'listed':
            raise AddonUpdateError('Only listed stable releases are eligible for automatic updates.')
        if not isinstance(version.get('game_versions'), list) or scope[1] not in version['game_versions']:
            raise AddonUpdateError('Update does not explicitly support this Minecraft version.')
        if not isinstance(version.get('loaders'), list) or not set(ENGINES[scope[0]][1]).intersection(version['loaders']):
            raise AddonUpdateError('Update does not support this server engine.')
        environment = version.get('environment')
        server_environments = ('client_and_server', 'server_only', 'server_only_client_optional', 'client_only_server_optional',
                               'dedicated_server_only', 'client_or_server', 'client_or_server_prefers_both')
        if environment not in server_environments and not (environment is None and scope[0] in ('Paper', 'Purpur')):
            raise AddonUpdateError('Update does not explicitly support dedicated servers.')
        if _date(version.get('date_published')) <= _date(installed.get('date_published')):
            raise AddonUpdateError('Release is not newer than the installed version; no automatic downgrade.')

    def _dependencies(self, version, known):
        dependencies = version.get('dependencies')
        if not isinstance(dependencies, list):
            raise AddonUpdateError('Dependency metadata is missing; update manually.')
        for dependency in dependencies:
            if not isinstance(dependency, dict) or dependency.get('dependency_type') not in ('required', 'optional', 'embedded', 'incompatible'):
                raise AddonUpdateError('Dependency metadata is ambiguous; update manually.')
            kind = dependency['dependency_type']
            if kind in ('optional', 'embedded'):
                continue
            project, release = dependency.get('project_id'), dependency.get('version_id')
            if not project and not release:
                raise AddonUpdateError('A dependency cannot be verified by project/version identity.')
            matches = [item for item in known if (not project or item['project_id'] == project)
                       and (not release or item['id'] == release)]
            if kind == 'required' and len(matches) != 1:
                raise AddonUpdateError('A required dependency is missing, ambiguous or not the required version. No dependencies are installed automatically.')
            if kind == 'incompatible' and matches:
                raise AddonUpdateError('Release conflicts with an installed dependency.')

    def _validate_jar(self, path, engine):
        descriptors = {'Paper': ('plugin.yml', 'paper-plugin.yml'),
                       'Purpur': ('plugin.yml', 'paper-plugin.yml'),
                       'Forge': ('META-INF/mods.toml', 'mcmod.info'),
                       'Fabric': ('fabric.mod.json',)}[engine]
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if (len(entries) > 20000 or sum(item.file_size for item in entries) > 512 * 1024 * 1024
                        or any(item.flag_bits & 1 for item in entries)):
                    raise AddonUpdateError('Downloaded JAR exceeds safe archive limits.')
                names = [item.filename for item in entries]
                if len(set(names)) != len(names) or not set(descriptors).intersection(names):
                    raise AddonUpdateError('Downloaded JAR has no valid descriptor for this server engine.')
                for name in set(descriptors).intersection(names):
                    if archive.getinfo(name).file_size > 1024 * 1024:
                        raise AddonUpdateError('Downloaded JAR descriptor exceeds the safe size limit.')
                    archive.read(name)  # Verify descriptor CRC without extracting paths or executing code.
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            raise AddonUpdateError('Downloaded file is not a valid supported add-on JAR.') from None

    def _download(self, session, record, destination, engine):
        artifact = record['artifact']
        response = session.request('GET', artifact['url'], timeout=(5, 30),
                                   allow_redirects=False, stream=True,
                                   headers={'Accept': 'application/java-archive', 'Accept-Encoding': 'identity'})
        digest, size = hashlib.sha512(), 0
        try:
            if response.status_code != 200:
                raise AddonUpdateError('Verified add-on download is unavailable; redirects are not followed.')
            with destination.open('xb') as stream:
                for chunk in response.iter_content(128 * 1024):
                    size += len(chunk)
                    if size > artifact['size'] or size > MAX_JAR_BYTES:
                        raise AddonUpdateError('Downloaded JAR exceeds its declared size.')
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size != artifact['size'] or digest.hexdigest() != artifact['sha512']:
                raise AddonUpdateError('Downloaded JAR failed SHA-512 or size verification. Nothing was installed.')
            self._validate_jar(destination, engine)
        finally:
            response.close()

    def _assert_current(self, scope, generation, records=None):
        with self._lock:
            if generation != self._generation or not self._enabled:
                raise AddonUpdateError('Update approvals changed; installation cancelled.')
        if self.is_running() or scope != self._scope():
            raise AddonUpdateError('Server state changed; installation requires the same stopped server.')
        if records is not None:
            current = {item['filename']: item['sha512'] for item in self._inventory(scope)}
            previous = {item['filename']: item['sha512'] for item in records}
            if current != previous:
                raise AddonUpdateError('Installed add-ons changed after checking; installation cancelled.')

    def _write_backup_metadata(self, directory, metadata):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                             prefix='.metadata-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(metadata, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / 'metadata.json')
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _finish_backup(self, directory, outcome):
        """An unrecorded outcome remains pending and is never automatically pruned."""
        try:
            path = directory / 'metadata.json'
            if directory.is_symlink() or directory.resolve().parent != self.backup_dir.resolve():
                return False
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024:
                return False
            with path.open('rb') as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                return False
            metadata = json.loads(raw)
            metadata['outcome'] = outcome
            self._write_backup_metadata(directory, metadata)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _backup(self, selected, scope):
        if self.backup_dir.is_symlink():
            raise AddonUpdateError('Update backup directory must not be a symbolic link.')
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.backup_dir / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        metadata = dict(retention_version=1, outcome='pending', created_at=_now(),
                        engine=scope[0], mc_version=scope[1], addons=[])
        for record in selected:
            metadata['addons'].append(dict(filename=record['filename'], project_id=record['installed']['project_id'],
                                           old_sha512=record['sha512'], new_sha512=record['artifact']['sha512'],
                                           old_version=record['installed']['id'], new_version=record['candidate']['id']))
        # Write intent first so a process interruption is visibly protected.
        self._write_backup_metadata(directory, metadata)
        try:
            for record in selected:
                source = record['path']
                if _hash_file(source) != record['sha512']:
                    raise AddonUpdateError('Installed add-on changed before backup; installation cancelled.')
                destination = directory / record['filename']
                with source.open('rb') as reader, destination.open('xb') as writer:
                    shutil.copyfileobj(reader, writer, 128 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                if _hash_file(destination) != record['sha512']:
                    raise AddonUpdateError('Previous-JAR backup failed verification. Nothing was installed.')
                record['backup'] = destination
        except Exception:
            # No active file has changed yet; even partial copies may safely expire.
            self._finish_backup(directory, 'cancelled')
            raise
        return directory

    def _install(self, selected, scope, generation, records):
        self._assert_current(scope, generation, records)
        try:
            directory = self._backup(selected, scope)
        except AddonUpdateError:
            raise
        except OSError:
            raise AddonUpdateError('Previous-JAR backup failed. No updates were installed; check free space and filesystem permissions.') from None
        try:
            self._assert_current(scope, generation, records)
        except Exception:
            self._finish_backup(directory, 'cancelled')
            raise
        replaced = []
        try:
            for record in selected:
                self._assert_current(scope, generation)
                if _hash_file(record['path']) != record['sha512']:
                    raise AddonUpdateError('Installed add-on changed before replacement; update cancelled.')
                if _hash_file(record['staged']) != record['artifact']['sha512']:
                    raise AddonUpdateError('Staged add-on changed before replacement; update cancelled.')
                os.chmod(record['staged'], stat.S_IMODE(record['path'].stat().st_mode))
                record['original_mode'] = stat.S_IMODE(record['path'].stat().st_mode)
                with self._lock:
                    if generation != self._generation or not self._enabled:
                        raise AddonUpdateError('Update approvals changed; installation cancelled.')
                    os.replace(record['staged'], record['path'])
                    replaced.append(record)
            for record in selected:
                if _hash_file(record['path']) != record['artifact']['sha512']:
                    raise AddonUpdateError('Installed JAR failed final verification.')
        except Exception:
            restored = True
            for record in reversed(replaced):
                rollback = directory / (uuid.uuid4().hex + '.rollback')
                try:
                    if _hash_file(record['backup']) != record['sha512']:
                        raise AddonUpdateError('Rollback backup changed.')
                    shutil.copyfile(record['backup'], rollback)
                    if _hash_file(rollback) != record['sha512']:
                        raise AddonUpdateError('Rollback copy failed verification.')
                    os.chmod(rollback, record['original_mode'])
                    os.replace(rollback, record['path'])
                except Exception:
                    restored = False
                finally:
                    if rollback.exists():
                        rollback.unlink()
            self._finish_backup(directory, 'rolled_back' if restored else 'recovery_required')
            with self._lock:
                self._enabled = False
                self._generation += 1
                self._save()
            message = ('Update failed; previous JARs restored. Automatic updates disabled for review.' if restored
                       else 'Update failed and automatic rollback needs attention. Keep the server stopped; verified previous JARs remain in addon-update-backups. Automatic updates disabled.')
            raise AddonUpdateError(message) from None
        recorded = self._finish_backup(directory, 'complete')
        with self._lock:
            for record in selected:
                record['row'].update(status='updated', reason='Updated while stopped; previous JAR backed up.',
                                     installed_version=record['row']['available_version'], available_version=None)
            self._last_applied = _now()
            self._status, self._message = 'idle', f'Updated {len(selected)} add-ons while stopped. Previous JARs saved in addon-update-backups/{directory.name}. Server remains stopped.'
            if not recorded:
                self._message += ' Recovery metadata could not be finalized; this batch is protected from automatic cleanup.'

    def _apply(self):
        with self._lock:
            enabled = self._enabled
        if not enabled:
            self._set('idle', 'Automatic add-on updates are disabled. Enable them and approve individual verified projects first.')
            return
        if self.is_running():
            self._set('idle', 'Waiting for the server to be fully stopped; no updates were installed.')
            return
        scope, generation, records = self._check()
        with self._lock:
            approved = set(self._projects)
        selected = [record for record in records if 'candidate' in record
                    and record['installed']['project_id'] in approved]
        if not selected:
            return
        # Validate the resulting graph, including reverse dependencies that might pin
        # an installed version being replaced. Never install dependencies implicitly.
        selected_ids = {record['installed']['project_id'] for record in selected}
        planned = [record['candidate'] if record['installed']['project_id'] in selected_ids else record['installed']
                   for record in records if 'installed' in record]
        for version in planned:
            self._dependencies(version, planned)
        self._assert_current(scope, generation)
        self._set('applying', 'Downloading and verifying approved updates before reserving server files...')
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.addon-update-', dir=self.state_path.parent) as staging:
            with self._session() as session:
                for index, record in enumerate(selected):
                    destination = Path(staging) / f'{index}.jar'
                    self._download(session, record, destination, scope[0])
                    record['staged'] = destination
            try:
                with self.mutation_guard():
                    self._install(selected, scope, generation, records)
            except AddonUpdateError:
                raise
            except Exception:
                raise AddonUpdateError('Server files are busy or server state changed. No further updates were installed; check the task status before retrying.') from None
