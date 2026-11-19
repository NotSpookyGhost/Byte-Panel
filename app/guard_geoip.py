"""Private, administrator-owned GeoLite2-City downloads and local-only lookups.

No player address is ever sent to MaxMind. Requests only download the database.
Download endpoint/redirect host: https://dev.maxmind.com/geoip/updating-databases/
"""
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlsplit, urljoin

import requests


DOWNLOAD_URL = 'https://download.maxmind.com/geoip/databases/GeoLite2-City/download?suffix=tar.gz'
DOWNLOAD_HOSTS = {
    'download.maxmind.com',
    'mm-prod-geoip-databases.a2649acb697e2c09b632799562c076f2.r2.cloudflarestorage.com',
}
MAX_AGE_SECONDS = 30 * 86400
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024


class GeoIPError(ValueError):
    """Safe user-facing error; never propagate credential-bearing HTTP errors."""


def private_directory(path):
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if parent.exists():
            info = parent.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or not stat.S_ISDIR(info.st_mode)):
                raise GeoIPError('Private security storage cannot contain linked directories.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def ordinary_file(path):
    info = Path(path).lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise GeoIPError('Private security storage contains an unsafe file.')
    return info


def atomic_json(path, value):
    path = Path(path)
    if path.exists() or path.is_symlink():
        ordinary_file(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.guard-', delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(value, handle, allow_nan=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        if path.is_symlink():
            raise GeoIPError('Private security storage contains an unsafe file.')
        return default
    if ordinary_file(path).st_size > 65536:
        raise GeoIPError('Private security settings are invalid.')
    try:
        with path.open(encoding='utf-8') as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise GeoIPError('Private security settings are invalid.') from None


def normalize_ip(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('Invalid IP address.')
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


class GeoIPService:
    def __init__(self, directory, *, session_factory=requests.Session, reader_factory=None,
                 clock=time.time):
        self.directory = private_directory(directory)
        self.credentials_path = self.directory / 'maxmind.json'
        self.database_path = self.directory / 'GeoLite2-City.mmdb'
        self.state_path = self.directory / 'geoip-status.json'
        self._clock = clock
        self._session_factory = session_factory
        self._reader_factory = reader_factory
        self._lock = threading.RLock()
        self._update_lock = threading.Lock()
        self._reader = None
        self._signature = None
        self._build_epoch = None
        # Invalid secret storage is a startup error, never silently reset credentials.
        self._credentials = read_json(self.credentials_path, {})
        self._state = read_json(self.state_path, {})

    def _new_reader(self, path):
        if self._reader_factory:
            return self._reader_factory(str(path))
        import geoip2.database
        return geoip2.database.Reader(str(path))

    def credentials(self):
        with self._lock:
            return dict(account_id=self._credentials.get('account_id', ''),
                        has_license_key=bool(self._credentials.get('license_key')),
                        credentials_validated=self._validated())

    def _fingerprint(self, credentials=None):
        current = self._credentials if credentials is None else credentials
        return hashlib.sha256((current.get('account_id', '') + ':' +
                               current.get('license_key', '')).encode()).hexdigest()

    def _validated(self):
        return bool(self._credentials.get('license_key') and
                    self._state.get('validated_fingerprint') == self._fingerprint())

    def set_credentials(self, payload):
        with self._update_lock, self._lock:
            updated = dict(self._credentials)
            if 'account_id' in payload:
                account = str(payload['account_id']).strip()
                if account and (not account.isascii() or not account.isdigit() or len(account) > 20):
                    raise GeoIPError('MaxMind account ID must contain digits only.')
                updated['account_id'] = account
            if payload.get('clear_license_key') is True:
                updated.pop('license_key', None)
            elif 'license_key' in payload:
                key = payload['license_key']
                if not isinstance(key, str) or len(key) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                    raise GeoIPError('Invalid MaxMind license key.')
                if key:
                    updated['license_key'] = key
            atomic_json(self.credentials_path, updated)
            self._credentials = updated
            return self.credentials()

    def _inspect(self, path):
        reader = self._new_reader(path)
        try:
            metadata = reader.metadata()
            if metadata.database_type != 'GeoLite2-City':
                raise GeoIPError('Download is not a GeoLite2-City database.')
            epoch = metadata.build_epoch
            if (not isinstance(epoch, (int, float)) or not math.isfinite(epoch) or epoch <= 0
                    or epoch > self._clock() + 86400):
                raise GeoIPError('GeoLite2-City database has an invalid build date.')
            return reader, float(epoch)
        except Exception:
            reader.close()
            raise

    def _load_reader(self):
        try:
            info = ordinary_file(self.database_path)
            signature = (info.st_ino, info.st_size, info.st_mtime_ns)
            if self._reader is not None and signature == self._signature:
                return
            reader, epoch = self._inspect(self.database_path)
            if self._reader:
                self._reader.close()
            self._reader, self._build_epoch, self._signature = reader, epoch, signature
        except Exception:
            if self._reader:
                self._reader.close()
            self._reader = self._signature = self._build_epoch = None

    def health(self):
        with self._lock:
            self._load_reader()
            age = max(0, self._clock() - self._build_epoch) if self._build_epoch else None
            stale = age is None or age > MAX_AGE_SECONDS
            return dict(**self.credentials(), available=self._reader is not None,
                        ready=self._reader is not None and not stale and self._validated(),
                        stale=stale, build_epoch=self._build_epoch,
                        age_days=round(age / 86400, 2) if age is not None else None,
                        last_check=self._state.get('last_check'),
                        last_update=self._state.get('last_update'),
                        error=self._state.get('error', ''), updating=self._update_lock.locked())

    def lookup(self, value):
        ip = normalize_ip(value)
        if not ipaddress.ip_address(ip).is_global:
            return dict(ip=ip, usable=False, reason='non_public')
        with self._lock:
            self._load_reader()
            if self._reader is None:
                return dict(ip=ip, usable=False, reason='database_unavailable')
            if self._clock() - self._build_epoch > MAX_AGE_SECONDS:
                return dict(ip=ip, usable=False, reason='database_stale')
            try:
                result = self._reader.city(ip)
                country = result.country.iso_code
                latitude, longitude = result.location.latitude, result.location.longitude
                accuracy = result.location.accuracy_radius
                valid = (isinstance(country, str) and len(country) == 2
                         and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                 and math.isfinite(v) for v in (latitude, longitude, accuracy)))
                valid = valid and -90 <= latitude <= 90 and -180 <= longitude <= 180 and 0 <= accuracy <= 20040
                if not valid:
                    return dict(ip=ip, usable=False, reason='insufficient_location')
                return dict(ip=ip, usable=True, country=country.upper(),
                            country_name=result.country.name, city=result.city.name,
                            latitude=latitude, longitude=longitude, accuracy_km=accuracy,
                            database_build=self._build_epoch)
            except Exception:
                return dict(ip=ip, usable=False, reason='unmapped')

    @staticmethod
    def _safe_url(url):
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname in DOWNLOAD_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)

    def _download(self, credentials, destination):
        started = time.monotonic()
        session = self._session_factory()
        session.trust_env = False  # No ambient .netrc authentication on signed redirect URLs.
        response = None
        try:
            url = DOWNLOAD_URL
            for redirect in range(4):
                if not self._safe_url(url):
                    raise GeoIPError('MaxMind redirected to an unexpected download location.')
                # Explicit redirects: the secret is never attached to Cloudflare R2.
                auth = ((credentials['account_id'], credentials['license_key'])
                        if urlsplit(url).hostname == 'download.maxmind.com' else None)
                response = session.get(url, auth=auth, stream=True, allow_redirects=False,
                                       timeout=(10, 30), headers={'User-Agent': 'BytePanel-ConnectionGuard/1'})
                if response.status_code in (301, 302, 303, 307, 308):
                    target = response.headers.get('Location', '')
                    response.close()
                    response = None
                    if not target:
                        raise GeoIPError('MaxMind download redirect is invalid.')
                    url = urljoin(url, target)
                    continue
                if response.status_code != 200:
                    raise GeoIPError('MaxMind download failed. Check the account ID, license key and download entitlement.')
                total = 0
                with destination.open('wb') as handle:
                    os.chmod(destination, 0o600)
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES or time.monotonic() - started > 180:
                            raise GeoIPError('MaxMind database download exceeded its safety limits.')
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                return
            raise GeoIPError('MaxMind download redirected too many times.')
        finally:
            if response is not None:
                response.close()
            session.close()

    def _extract(self, archive_path, target):
        found, expanded, count = False, 0, 0
        # Streaming tar iteration avoids loading unbounded headers or extracting paths.
        with tarfile.open(archive_path, mode='r|gz') as archive:
            for member in archive:
                count += 1
                expanded += member.size
                path = PurePosixPath(member.name)
                if (count > 32 or expanded > MAX_DATABASE_BYTES * 2 or member.size < 0
                        or path.is_absolute() or '..' in path.parts or '\\' in member.name
                        or not (member.isdir() or member.isfile())):
                    raise GeoIPError('MaxMind archive contains unsafe entries.')
                if path.name != 'GeoLite2-City.mmdb':
                    continue
                if found or not member.isfile() or not 1 <= member.size <= MAX_DATABASE_BYTES:
                    raise GeoIPError('MaxMind archive contains an invalid City database.')
                found = True
                stream = archive.extractfile(member)
                with target.open('wb') as handle:
                    os.chmod(target, 0o600)
                    remaining = member.size
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            raise GeoIPError('MaxMind database download is incomplete.')
                        handle.write(chunk)
                        remaining -= len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        if not found:
            raise GeoIPError('MaxMind archive did not contain GeoLite2-City.mmdb.')

    def update(self):
        if not self._update_lock.acquire(blocking=False):
            raise GeoIPError('A GeoLite2-City update is already running.')
        try:
            with self._lock:
                credentials = dict(self._credentials)
            if not credentials.get('account_id') or not credentials.get('license_key'):
                raise GeoIPError('Enter your own MaxMind account ID and license key first.')
            with tempfile.TemporaryDirectory(prefix='.geoip-', dir=self.directory) as staging:
                archive, target = Path(staging) / 'database.tar.gz', Path(staging) / 'database.mmdb'
                self._download(credentials, archive)
                self._extract(archive, target)
                reader, epoch = self._inspect(target)
                reader.close()
                if self._clock() - epoch > MAX_AGE_SECONDS:
                    raise GeoIPError('Downloaded City database is older than 30 days.')
                with self._lock:
                    if self.database_path.exists() or self.database_path.is_symlink():
                        ordinary_file(self.database_path)
                    if self._reader:
                        self._reader.close()
                    self._reader = self._signature = None
                    os.replace(target, self.database_path)
                    self._state = dict(last_check=self._clock(), last_update=self._clock(),
                                       validated_fingerprint=self._fingerprint(credentials), error='')
                    atomic_json(self.state_path, self._state)
        except Exception as error:
            message = str(error) if isinstance(error, GeoIPError) else 'GeoLite2-City update failed. Check connectivity and private storage permissions.'
            with self._lock:
                self._state.update(last_check=self._clock(), error=message)
                atomic_json(self.state_path, self._state)
            raise GeoIPError(message) from None
        finally:
            self._update_lock.release()
        return self.health()

    def maintenance(self):
        with self._lock:
            due = self._clock() - self._state.get('last_check', 0) >= 86400
            configured = bool(self._credentials.get('account_id') and self._credentials.get('license_key'))
        if due and configured:
            return self.update()
        return self.health()

    def close(self):
        with self._lock:
            if self._reader:
                self._reader.close()
                self._reader = None
