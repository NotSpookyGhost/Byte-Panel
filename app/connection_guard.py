"""Private, scoped Byte Guard policy. Offline enrollment requires authentication.

The authenticated companion owns admission; console text never proves identity.
Passwords are neither accepted nor stored by this service.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from guard_geoip import GeoIPService, normalize_ip, private_directory, ordinary_file, read_json, atomic_json

PROTOCOL_VERSION = 1
EVENT_DAYS = 90
EVENT_LIMIT = 100000
ATTEMPT_SECONDS = 120
MAX_INFLIGHT = 4096
MAX_SAFE_IPS = 32
_USERNAME = re.compile(r'^[A-Za-z0-9_]{1,16}$')
_ATTEMPT = re.compile(r'^[A-Za-z0-9_-]{8,80}$')


class GuardConflict(ValueError):
    """Stale scope, policy or identity mode must never cause fail-open behavior."""


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':'))


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def identity_key(mode, username, player_uuid=None):
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise ValueError('Invalid Minecraft username.')
    if mode == 'offline':
        return 'offline:' + username.lower()
    if mode != 'online':
        raise ValueError('Identity mode must be online or offline.')
    try:
        parsed = uuid.UUID(str(player_uuid))
        if parsed.int == 0 or parsed.variant != uuid.RFC_4122:
            raise ValueError()
        return 'online:' + str(parsed)
    except (ValueError, AttributeError, TypeError):
        raise ValueError('A valid authenticated UUID is required.') from None


def _usable(location):
    if not isinstance(location, dict) or location.get('usable') is not True:
        return False
    country = location.get('country')
    values = [location.get(key) for key in ('latitude', 'longitude', 'accuracy_km')]
    return (isinstance(country, str) and re.fullmatch('[A-Z]{2}', country) is not None
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                    for v in values)
            and -90 <= values[0] <= 90 and -180 <= values[1] <= 180 and 0 <= values[2] <= 20040)


def nearby(reference, location, radius):
    if not _usable(reference) or not _usable(location) or reference['country'] != location['country']:
        return False
    lat1, lat2 = math.radians(reference['latitude']), math.radians(location['latitude'])
    dlat = lat2 - lat1
    dlon = math.radians(location['longitude'] - reference['longitude'])
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    distance = 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, haversine))))
    return distance + reference['accuracy_km'] + location['accuracy_km'] <= radius


class ConnectionGuard:
    def __init__(self, data_dir, scope_provider, config_provider, geoip=None, *, clock=time.time):
        self.marker_path = Path(data_dir).absolute() / '.connection-guard-initialized.json'
        marker_exists = self.marker_path.exists() or self.marker_path.is_symlink()
        marker = read_json(self.marker_path, {})
        self.directory = private_directory(Path(data_dir) / 'connection-security')
        self.database_path = self.directory / 'guard.sqlite3'
        database_exists = self.database_path.exists() or self.database_path.is_symlink()
        if marker_exists and (marker.get('schema_version') != 1 or
                              not isinstance(marker.get('initialization_id'), str) or not database_exists):
            raise GuardConflict('Byte Guard storage is missing or invalid. Restore its private data before starting.')
        if database_exists:
            ordinary_file(self.database_path)
        self.scope_provider, self.config_provider, self._clock = scope_provider, config_provider, clock
        self._lock, self._heartbeat = threading.RLock(), {}
        self.geoip = geoip if geoip is not None else GeoIPService(self.directory / 'geoip')
        with self._lock, closing(sqlite3.connect(self.database_path)) as conn, conn:
            if database_exists:
                expected = {
                    'metadata': {'key', 'value'}, 'identities': {'identity', 'data'},
                    'attempts': {'seq', 'attempt_id', 'identity', 'created', 'review', 'data'},
                    'reservations': {'identity', 'attempt_id', 'expires'},
                    'approvals': {'identity', 'ip', 'expires', 'source', 'claim'},
                    'audit': {'seq', 'created', 'data'},
                }
                if marker_exists:
                    expected['inflight'] = {'attempt_id', 'expires', 'data'}
                for table, columns in expected.items():
                    actual = {row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')}
                    if not columns.issubset(actual):
                        raise GuardConflict('Byte Guard database is incomplete. Restore private security data.')
                policy = self._policy(conn)
                if (not isinstance(policy['scope'], str) or not policy['scope']
                        or type(policy['policy_revision']) is not int or policy['policy_revision'] < 1
                        or policy['mode'] not in ('off', 'monitor', 'enforce')
                        or policy['identity_mode'] not in ('online', 'offline')
                        or type(policy['radius_km']) not in (int, float)
                        or not 1 <= policy['radius_km'] <= 20000):
                    raise GuardConflict('Byte Guard policy is incomplete. Restore private security data.')
                if marker_exists and self._meta(conn, 'initialization_id') != marker['initialization_id']:
                    raise GuardConflict('Byte Guard database does not match its private initialization record.')
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identities (identity TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT UNIQUE NOT NULL,
                    identity TEXT NOT NULL, created REAL NOT NULL, review TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_identity ON attempts(identity, seq);
                CREATE INDEX IF NOT EXISTS attempts_review ON attempts(review, seq);
                CREATE INDEX IF NOT EXISTS attempts_created ON attempts(created);
                CREATE TABLE IF NOT EXISTS reservations (
                    identity TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals (
                    identity TEXT NOT NULL, ip TEXT NOT NULL, expires REAL NOT NULL,
                    source TEXT NOT NULL, claim TEXT, PRIMARY KEY(identity, ip));
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inflight (
                    attempt_id TEXT PRIMARY KEY, expires REAL NOT NULL, data TEXT NOT NULL);
            ''')
            if self._meta(conn, 'policy_revision') is None:
                for key, value in dict(policy_revision=1, mode='off', radius_km=250,
                                       identity_mode=self._inactive_identity_mode(), scope=str(self.scope_provider())).items():
                    self._setmeta(conn, key, value)
            self._ensure_scope(conn)
            initialization_id = self._meta(conn, 'initialization_id')
            if initialization_id is None:
                initialization_id = uuid.uuid4().hex
                self._setmeta(conn, 'initialization_id', initialization_id)
        os.chmod(self.database_path, 0o600)
        if not marker_exists:
            atomic_json(self.marker_path, dict(schema_version=1, initialization_id=initialization_id))

    def _effective_mode(self):
        config = self.config_provider() or {}
        if config.get('identity_mode') in ('online', 'offline'):
            return config['identity_mode']
        value = config.get('online-mode', config.get('online_mode', True))
        return 'offline' if value is False or str(value).lower() == 'false' else 'online'

    def _inactive_identity_mode(self):
        try:
            return self._effective_mode()
        except (ValueError, OSError):
            # Keep the panel available to fix unreadable properties. Activation
            # and Guard-enabled starts still require strict identity validation.
            return 'online'

    @staticmethod
    def _meta(conn, key, default=None):
        row = conn.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _setmeta(conn, key, value):
        conn.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)', (key, _json(value)))

    def _policy(self, conn):
        return {key: self._meta(conn, key) for key in
                ('scope', 'policy_revision', 'mode', 'radius_km', 'identity_mode')}

    def _clear_scope(self, conn, scope):
        for table in ('identities', 'attempts', 'reservations', 'approvals', 'audit', 'inflight'):
            conn.execute('DELETE FROM ' + table)
        for key, value in dict(scope=scope, policy_revision=self._meta(conn, 'policy_revision', 0) + 1,
                               mode='off', identity_mode=self._inactive_identity_mode(), history_imported=False).items():
            self._setmeta(conn, key, value)
        self._heartbeat = {}

    def _ensure_scope(self, conn):
        current = str(self.scope_provider())
        if not current or len(current) > 200:
            raise GuardConflict('Server scope is unavailable.')
        if self._meta(conn, 'scope') != current:
            self._clear_scope(conn, current)

    @contextmanager
    def _transaction(self):
        with self._lock:
            ordinary_file(self.database_path)
            conn = sqlite3.connect(self.database_path, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute('BEGIN IMMEDIATE')
                self._ensure_scope(conn)
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _profile(conn, identity):
        row = conn.execute('SELECT data FROM identities WHERE identity=?', (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _event(conn, attempt_id):
        row = conn.execute('SELECT data FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None:
            row = conn.execute('SELECT data FROM inflight WHERE attempt_id=?', (attempt_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _store_profile(conn, profile):
        conn.execute('INSERT OR REPLACE INTO identities(identity,data) VALUES (?,?)',
                     (profile['identity'], _json(profile)))

    def _store_event(self, conn, event):
        conn.execute('UPDATE attempts SET review=?,data=? WHERE attempt_id=?',
                     (event['review_status'], _json(event), event['attempt_id']))
        if event['completed'] or event['disconnected']:
            conn.execute('DELETE FROM inflight WHERE attempt_id=?', (event['attempt_id'],))
        elif event['joined'] or event['authenticated']:
            # Offline players can wait at the authentication screen longer than
            # admission reservations. Retain only bounded in-flight state.
            conn.execute('UPDATE inflight SET data=?,expires=? WHERE attempt_id=?',
                         (_json(event), self._clock() + EVENT_DAYS * 86400, event['attempt_id']))
        else:
            conn.execute('UPDATE inflight SET data=? WHERE attempt_id=?', (_json(event), event['attempt_id']))

    def config(self):
        with self._transaction() as conn:
            return dict(**self._policy(conn), **self.geoip.credentials())

    def save_settings(self, payload, *, validate_credentials=False):
        if not isinstance(payload, dict):
            raise ValueError('Settings must be an object.')
        mode, radius = payload.get('mode'), payload.get('radius_km')
        if mode is not None and mode not in ('off', 'monitor', 'enforce'):
            raise ValueError('Choose Off, Monitor or Enforce.')
        if radius is not None and (isinstance(radius, bool) or not isinstance(radius, (float, int))
                                  or not 1 <= radius <= 20000):
            raise ValueError('Radius must be between 1 and 20,000 km.')
        requested_identity = payload.get('identity_mode')
        if requested_identity is not None and requested_identity not in ('online', 'offline'):
            raise ValueError('Identity mode must be online or offline.')
        credentials = dict(payload)
        if payload.get('remove_license_key') is True:
            credentials['clear_license_key'] = True
        with self._transaction() as conn:
            current = self._policy(conn)
            if (mode or current['mode']) != 'off' and credentials.get('clear_license_key'):
                raise ValueError('Turn Byte Guard off before removing the license key.')
        if any(key in credentials for key in ('account_id', 'license_key', 'clear_license_key')):
            self.geoip.set_credentials(credentials)
        saved = self.geoip.credentials()
        if (validate_credentials and saved.get('account_id') and saved.get('has_license_key')
                and not saved.get('credentials_validated')):
            # Explicit Save and validate may download before activating the policy.
            # Failure preserves the previous policy and GeoIP keeps its last good DB.
            self.geoip.update()
        with self._transaction() as conn:
            current = self._policy(conn)
            next_mode, next_identity = mode or current['mode'], requested_identity or current['identity_mode']
            if current['mode'] == 'off' and next_mode != 'off' and requested_identity is None:
                next_identity = self._effective_mode()
            if next_mode != 'off':
                if next_identity != self._effective_mode():
                    raise ValueError('Identity mode does not match the effective server configuration.')
                if not self.geoip.health().get('ready'):
                    raise ValueError('Validate your MaxMind credentials and download a current City database before enabling Guard.')
            if next_identity != current['identity_mode']:
                conn.execute('DELETE FROM reservations')
                conn.execute('DELETE FROM approvals')
                self._heartbeat = {}
            updated = dict(mode=next_mode, radius_km=radius if radius is not None else current['radius_km'],
                           identity_mode=next_identity)
            if any(current[key] != value for key, value in updated.items()):
                for key, value in updated.items():
                    self._setmeta(conn, key, value)
                self._setmeta(conn, 'policy_revision', current['policy_revision'] + 1)
                conn.execute('DELETE FROM approvals')
            self._audit(conn, 'settings_changed', 'panel administrator', updated)
        return self.snapshot()

    def update_geoip(self):
        return self.geoip.update()

    def snapshot(self):
        with self._transaction() as conn:
            policy = self._policy(conn)
            count = conn.execute("SELECT COUNT(*) FROM attempts WHERE review='pending'").fetchone()[0]
            heartbeat = dict(self._heartbeat)
            current = heartbeat.get('policy_revision') == policy['policy_revision']
            fresh = self._clock() - heartbeat.get('received_at', 0) < 45
            heartbeat['healthy'] = bool(heartbeat.get('ready') and fresh and current
                                        and (policy['identity_mode'] != 'offline' or heartbeat.get('auth_ready')))
            if heartbeat.get('received_at') is not None and not fresh:
                heartbeat['status'] = 'Heartbeat expired'
            elif heartbeat.get('received_at') is not None and not current:
                heartbeat.update(status='Waiting for current policy', pending_revision=True)
            return dict(**policy, protocol_version=1, **self.geoip.credentials(),
                        geoip=self.geoip.health(), bridge=heartbeat, pending_reviews=count,
                        history_imported=self._meta(conn, 'history_imported', False))

    health = snapshot

    def _validate_scope(self, policy, payload, *, revision=False):
        if payload.get('scope') != policy['scope']:
            raise GuardConflict('Server scope changed; refresh companion configuration.')
        if payload.get('protocol_version', 1) != PROTOCOL_VERSION:
            raise GuardConflict('Unsupported Byte Guard protocol.')
        if revision and payload.get('policy_revision') != policy['policy_revision']:
            raise GuardConflict('Byte Guard policy changed; refresh the complete roster.')
        if payload.get('identity_mode', policy['identity_mode']) != policy['identity_mode']:
            raise GuardConflict('Server identity mode changed; reconfigure Byte Guard.')

    def heartbeat(self, payload):
        with self._transaction() as conn:
            policy = self._policy(conn)
            self._validate_scope(policy, payload, revision=True)
            self._heartbeat = dict(received_at=self._clock(), policy_revision=policy['policy_revision'],
                                   auth_ready=payload.get('auth_ready') is True,
                                   ready=payload.get('ready') is True,
                                   status=str(payload.get('status', 'Unknown'))[:160],
                                   pending_events=min(100000, max(0, payload.get('pending_events', 0)))
                                       if type(payload.get('pending_events', 0)) is int else 0,
                                   plugin_version=str(payload.get('plugin_version', ''))[:40], mode=policy['mode'])
            return dict(protocol_version=1, accepted=True, **policy)

    def roster(self):
        with self._transaction() as conn:
            policy = self._policy(conn)
            identities = []
            for row in conn.execute('SELECT data FROM identities ORDER BY identity'):
                profile = json.loads(row[0])
                if profile['identity'].startswith(policy['identity_mode'] + ':'):
                    ips = profile['accepted_ips'] if profile.get('authenticated', False) else []
                    identities.append(dict(identity=profile['identity'], accepted_ips=ips))
            return dict(protocol_version=1, complete=True, identities=identities, **policy)

    def _location(self, ip):
        try:
            result = self.geoip.lookup(ip)
            if _usable(result):
                return {key: result.get(key) for key in ('ip', 'usable', 'country', 'country_name',
                        'city', 'latitude', 'longitude', 'accuracy_km', 'database_build')}
            return dict(ip=ip, usable=False, reason=str(result.get('reason', 'unavailable'))[:60])
        except Exception:
            return dict(ip=ip, usable=False, reason='database_unavailable')

    def prelogin(self, payload):
        if not isinstance(payload, dict) or 'password' in payload:
            raise ValueError('Invalid connection request; passwords are never accepted.')
        attempt_id = payload.get('attempt_id')
        if not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
            raise ValueError('Invalid attempt ID.')
        username, ip, now = payload.get('username'), normalize_ip(payload.get('ip')), self._clock()
        with self._transaction() as conn:
            policy = self._policy(conn)
            self._validate_scope(policy, payload)
            request_revision = payload.get('policy_revision', policy['policy_revision'])
            stale = request_revision != policy['policy_revision']
            fallback = payload.get('fallback') is True
            if stale and (not fallback or type(request_revision) is not int
                          or not 0 < request_revision < policy['policy_revision']):
                raise GuardConflict('Byte Guard policy changed; refresh the complete roster.')
            if policy['mode'] != 'off' and self._effective_mode() != policy['identity_mode']:
                raise GuardConflict('Effective server identity mode does not match Guard.')
            identity = identity_key(policy['identity_mode'], username, payload.get('uuid'))
            existing = self._event(conn, attempt_id)
            if existing:
                if (existing['identity'], existing['ip'], existing['policy_revision']) != (identity, ip, request_revision):
                    raise GuardConflict('Attempt ID was reused for another connection.')
                if stale:
                    return self._revoked_decision(existing, policy)
                return self._decision(existing)
            occurred = now
            if fallback:
                if not stale:
                    self._validate_scope(policy, payload, revision=True)
                if (not stale and payload.get('mode') != policy['mode']) or payload.get('mode') not in ('monitor', 'enforce'):
                    raise GuardConflict('Offline journal mode no longer matches current policy.')
                if payload.get('fallback_decision') not in ('new_identity', 'accepted_ip'):
                    raise GuardConflict('Offline journal decision is invalid.')
                timestamp = payload.get('occurred_at')
                if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                        or not math.isfinite(timestamp)):
                    raise GuardConflict('Offline journal timestamp is invalid.')
                occurred = timestamp / 1000
                if not now - EVENT_DAYS * 86400 <= occurred <= now + 60:
                    raise GuardConflict('Offline journal timestamp is outside the supported window.')
            profile, location = self._profile(conn, identity), self._location(ip)
            conn.execute('DELETE FROM reservations WHERE expires<?', (now,))
            conn.execute('DELETE FROM inflight WHERE expires<?', (now,))
            reservation = conn.execute('SELECT * FROM reservations WHERE identity=?', (identity,)).fetchone()
            approval = conn.execute('SELECT * FROM approvals WHERE identity=? AND ip=? AND expires>?',
                                    (identity, ip, now)).fetchone()
            eligible, reason, pending, approval_source = False, 'disabled', False, None
            if stale:
                reason, pending = 'policy_revoked', True
            elif policy['mode'] != 'off':
                if fallback:
                    choice = payload.get('fallback_decision')
                    if choice == 'new_identity' and profile is None and reservation is None:
                        eligible, reason = True, 'new_identity'
                    elif choice == 'accepted_ip' and profile and profile.get('authenticated') and ip in profile['accepted_ips']:
                        eligible, reason = True, 'accepted_ip'
                    else:
                        raise GuardConflict('Offline journal decision conflicts with the current identity roster.')
                elif profile is None:
                    eligible, reason = reservation is None, 'new_identity' if reservation is None else 'identity_busy'
                elif ip in profile['accepted_ips']:
                    eligible, reason = True, 'accepted_ip'
                elif any(entry['ip'] == ip for entry in profile.get('safe_ips', [])):
                    eligible, reason = True, 'safe_ip'
                elif approval and (not approval['claim'] or approval['claim'] == attempt_id):
                    eligible, reason, approval_source = True, 'approved_once', approval['source']
                elif any(nearby(reference, location, policy['radius_km']) for reference in profile['references']):
                    eligible, reason = True, 'nearby'
                else:
                    pending = True
                    if not _usable(location):
                        reason = 'location_unavailable'
                    elif profile['references'] and all(ref.get('country') != location['country'] for ref in profile['references']):
                        reason = 'country_changed'
                    else:
                        reason = 'outside_trusted_area'
                if policy['identity_mode'] == 'offline' and not fallback:
                    auth_ready = (self._heartbeat.get('ready') is True and self._heartbeat.get('auth_ready') is True
                                  and now - self._heartbeat.get('received_at', 0) < 45
                                  and self._heartbeat.get('policy_revision') == policy['policy_revision'])
                    if not auth_ready:
                        eligible, reason, pending = False, 'authentication_unavailable', False
            action = 'allow' if not stale and (policy['mode'] == 'off' or eligible or
                                (policy['mode'] == 'monitor' and pending)) else 'deny'
            if (action == 'allow' or stale) and conn.execute('SELECT COUNT(*) FROM inflight').fetchone()[0] >= MAX_INFLIGHT:
                if fallback:
                    raise GuardConflict('Connection journal replay capacity is temporarily exhausted.')
                action, eligible, reason, pending = 'deny', False, 'capacity_exhausted', False
                approval_source = None
            if eligible and profile is None:
                # Historical fallback stages may be separated by a large durable
                # journal backlog. Keep their reservation until replay completes.
                lifetime = EVENT_DAYS * 86400 if fallback else ATTEMPT_SECONDS
                conn.execute('INSERT INTO reservations VALUES (?,?,?)', (identity, attempt_id, now + lifetime))
            if approval_source:
                conn.execute('UPDATE approvals SET claim=? WHERE identity=? AND ip=?', (attempt_id, identity, ip))
            event = dict(protocol_version=1, attempt_id=attempt_id, identity=identity, username=username,
                         uuid=identity[7:] if identity.startswith('online:') else payload.get('uuid'), ip=ip,
                         location=location, timestamp=_utc(occurred), received_at=_utc(now), created=now,
                         occurred_at=occurred, action=action, reason=reason, trust_on_success=eligible,
                         review_status='pending' if pending else 'none', stage='prelogin', joined=False,
                         authenticated=False, completed=False, disconnected=False, fallback=fallback,
                         approval_source=approval_source, fallback_revoked=stale, **policy)
            if stale:
                event.update(policy_revision=request_revision, mode=payload['mode'])
            conn.execute('INSERT INTO attempts(attempt_id,identity,created,review,data) VALUES (?,?,?,?,?)',
                         (attempt_id, identity, now, event['review_status'], _json(event)))
            if action == 'allow' or stale:
                lifetime = EVENT_DAYS * 86400 if fallback else ATTEMPT_SECONDS
                conn.execute('INSERT INTO inflight VALUES (?,?,?)', (attempt_id, now + lifetime, _json(event)))
            return self._revoked_decision(event, policy) if stale else self._decision(event)

    @staticmethod
    def _revoked_decision(event, policy):
        return dict(protocol_version=1, attempt_id=event['attempt_id'], action='deny',
                    reason='policy_revoked', message='Connection policy changed; reconnect.',
                    trust_on_success=False, replay_accepted=True, revoked=True, **policy)

    @staticmethod
    def _decision(event):
        message = ('Connection requires administrator review. Reference: ' + event['attempt_id'][:12]
                   if event['action'] == 'deny' else '')
        # Explicit per-IP permissions remain revocable through the live panel.
        cache_trust = event['trust_on_success'] and not event['approval_source'] and event['reason'] != 'safe_ip'
        return {**{key: event[key] for key in ('protocol_version', 'attempt_id', 'scope', 'policy_revision',
                                              'mode', 'identity_mode', 'action', 'reason')},
                'trust_on_success': bool(cache_trust), 'message': message}

    def outcome(self, payload):
        if not isinstance(payload, dict) or 'password' in payload:
            raise ValueError('Invalid connection outcome.')
        stage = payload.get('stage')
        if stage not in ('joined', 'authenticated', 'disconnected'):
            raise ValueError('Invalid connection outcome stage.')
        with self._transaction() as conn:
            policy = self._policy(conn)
            # Same-scope revoked outcomes are audit acknowledgements, not permissions.
            self._validate_scope(policy, dict(payload, identity_mode=policy['identity_mode']))
            event = self._event(conn, payload.get('attempt_id'))
            if not event:
                attempt = payload.get('attempt_id')
                if not isinstance(attempt, str) or not _ATTEMPT.fullmatch(attempt):
                    raise GuardConflict('Connection attempt is unknown.')
                identity = identity_key(payload.get('identity_mode', policy['identity_mode']),
                                        payload.get('username'), payload.get('uuid'))
                if stage != 'disconnected' and self._profile(conn, identity) is None:
                    raise GuardConflict('Connection attempt is unknown.')
                # Acknowledging an expired disconnect or a completed identity's
                # duplicate journal stage grants no trust and changes no profile.
                return dict(protocol_version=1, scope=policy['scope'], policy_revision=policy['policy_revision'],
                            mode=policy['mode'], accepted=True, revoked=True, completed=False, committed=False,
                            stage=stage, attempt_id=attempt, identity=identity, username=payload['username'],
                            uuid=payload.get('uuid'), ip=normalize_ip(payload.get('ip')), country='Unknown')
            if payload.get('policy_revision', event['policy_revision']) != event['policy_revision']:
                raise GuardConflict('Outcome policy does not match its attempt.')
            if payload.get('identity_mode', event['identity_mode']) != event['identity_mode']:
                raise GuardConflict('Outcome identity mode does not match its attempt.')
            revoked = event['policy_revision'] != policy['policy_revision'] or event.get('revoked', False)
            if 'ip' in payload and normalize_ip(payload['ip']) != event['ip']:
                raise GuardConflict('Outcome IP does not match the attempt.')
            if 'username' in payload and identity_key(event['identity_mode'], payload['username'], payload.get('uuid')) != event['identity']:
                raise GuardConflict('Outcome identity does not match the attempt.')
            was_completed = event['completed']
            if stage == 'disconnected':
                event['disconnected'], event['stage'] = True, 'disconnected'
                conn.execute('DELETE FROM reservations WHERE attempt_id=?', (event['attempt_id'],))
                conn.execute('UPDATE approvals SET claim=NULL WHERE claim=?', (event['attempt_id'],))
            elif event['action'] != 'allow' and not event.get('fallback_revoked'):
                raise GuardConflict('A rejected attempt cannot report successful admission.')
            elif event['disconnected']:
                if not event.get(stage, False):
                    raise GuardConflict('A disconnected attempt cannot authenticate or join.')
            else:
                event[stage], event['stage'] = True, stage
                success = event['joined'] and (event['identity_mode'] == 'online' or event['authenticated'])
                if success and not event['completed']:
                    if revoked:
                        self._complete_revoked(conn, event)
                    else:
                        revoked = not self._complete(conn, event)
                    if revoked:
                        event['revoked'] = True
            self._store_event(conn, event)
            return dict(protocol_version=1, scope=policy['scope'], policy_revision=policy['policy_revision'],
                        mode=policy['mode'], accepted=True, completed=event['completed'], revoked=revoked,
                        committed=event['completed'] and not was_completed, stage=stage,
                        identity=event['identity'], username=event['username'], uuid=event['uuid'], ip=event['ip'],
                        country=event['location'].get('country_name') or event['location'].get('country') or 'Unknown',
                        attempt_id=event['attempt_id'], timestamp=event['timestamp'])

    def _complete_revoked(self, conn, event):
        # Retain existence, but never resurrect permission revoked by a policy change.
        if event['mode'] != 'off' and self._profile(conn, event['identity']) is None:
            self._store_profile(conn, dict(identity=event['identity'], username=event['username'],
                first_seen=event['timestamp'], source='revoked_policy_session',
                references=[], accepted_ips=[], authenticated=True, original_ip=event['ip']))
        conn.execute('DELETE FROM reservations WHERE attempt_id=?', (event['attempt_id'],))
        event['completed'] = True

    def _complete(self, conn, event):
        if not event['trust_on_success']:
            event['completed'] = True
            return True
        now, profile = self._clock(), self._profile(conn, event['identity'])
        if profile is None:
            reservation = conn.execute('SELECT * FROM reservations WHERE identity=?', (event['identity'],)).fetchone()
            if (not reservation or reservation['attempt_id'] != event['attempt_id']
                    or (not event['fallback'] and reservation['expires'] < now)):
                event.update(reason='enrollment_expired', review_status='pending', trust_on_success=False)
                self._complete_revoked(conn, event)
                return False
            profile = dict(identity=event['identity'], username=event['username'],
                           first_seen=event['timestamp'], source='offline_journal' if event['fallback'] else 'first_connection',
                           references=[event['location']] if _usable(event['location']) else [],
                           accepted_ips=[], authenticated=True, original_ip=event['ip'])
        if event['approval_source']:
            approval = conn.execute('SELECT * FROM approvals WHERE identity=? AND ip=?',
                                    (event['identity'], event['ip'])).fetchone()
            if not approval or approval['claim'] != event['attempt_id'] or approval['expires'] < now:
                event.update(reason='approval_expired', review_status='pending', trust_on_success=False)
                self._complete_revoked(conn, event)
                return False
            conn.execute('DELETE FROM approvals WHERE identity=? AND ip=?', (event['identity'], event['ip']))
        elif event['reason'] != 'safe_ip' and event['ip'] not in profile['accepted_ips']:
            profile['accepted_ips'].append(event['ip'])
        profile.update(username=event['username'], last_seen=event['timestamp'], authenticated=True)
        self._store_profile(conn, profile)
        conn.execute('DELETE FROM reservations WHERE attempt_id=?', (event['attempt_id'],))
        event['completed'] = True
        if event['review_status'] == 'pending':
            event['review_status'] = 'resolved'
        return True

    def _audit(self, conn, action, actor, details):
        conn.execute('INSERT INTO audit(created,data) VALUES (?,?)',
                     (self._clock(), _json(dict(timestamp=_utc(self._clock()), action=action,
                                               actor=str(actor)[:100], details=details))))

    def review(self, attempt_id, action, actor='administrator'):
        if action not in ('allow_once', 'trust_area', 'keep_blocked'):
            raise ValueError('Unknown review action.')
        with self._transaction() as conn:
            policy, event = self._policy(conn), self._event(conn, attempt_id)
            if not event or event['review_status'] != 'pending':
                raise GuardConflict('This attempt is not awaiting review.')
            if event['scope'] != policy['scope'] or event['identity_mode'] != policy['identity_mode']:
                raise GuardConflict('This review belongs to another server identity mode.')
            profile = self._profile(conn, event['identity'])
            if not profile:
                raise GuardConflict('Player reference is unavailable.')
            if action == 'allow_once':
                conn.execute('INSERT OR REPLACE INTO approvals(identity,ip,expires,source,claim) VALUES (?,?,?,?,NULL)',
                             (event['identity'], event['ip'], self._clock() + 900, attempt_id))
            elif action == 'trust_area':
                if not _usable(event['location']):
                    raise ValueError('This attempt has no usable geographic area to trust.')
                profile['references'].append(dict(event['location'], source='administrator', approved_at=_utc(self._clock())))
                self._store_profile(conn, profile)
            event.update(review_status='resolved', review_action=action, reviewed_at=_utc(self._clock()))
            self._store_event(conn, event)
            self._audit(conn, action, actor, dict(attempt_id=attempt_id, identity=event['identity'], ip=event['ip']))
            return event

    def get_events(self, limit=50, cursor=None, status=None, identity=None):
        try:
            limit, cursor = min(200, max(1, int(limit))), int(cursor) if cursor is not None else None
        except (TypeError, ValueError):
            raise ValueError('Invalid event pagination.') from None
        if cursor is not None and not 0 < cursor <= 2**63 - 1:
            raise ValueError('Invalid event pagination.')
        if status not in (None, '', 'pending', 'resolved', 'none'):
            raise ValueError('Invalid review status.')
        conditions, args = [], []
        for column, value, operator in (('seq', cursor, '<'), ('review', status, '='), ('identity', identity, '=')):
            if value is not None and value != '':
                conditions.append(column + operator + '?')
                args.append(value)
        clause = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self._transaction() as conn:
            rows = conn.execute('SELECT seq,data FROM attempts' + clause + ' ORDER BY seq DESC LIMIT ?',
                                (*args, limit + 1)).fetchall()
            return dict(events=[dict(json.loads(row['data']), cursor=row['seq']) for row in rows[:limit]],
                        next_cursor=rows[limit - 1]['seq'] if len(rows) > limit else None)

    def profile(self, identity):
        with self._transaction() as conn:
            profile = self._profile(conn, identity)
            if profile is None:
                raise ValueError('No Byte Guard reference exists for this identity.')
            return profile

    def player_profile_links(self, players):
        with self._transaction() as conn:
            mode = self._policy(conn)['identity_mode']
            known = {row[0] for row in conn.execute('SELECT identity FROM identities')}
        links = {}
        for player in players:
            try:
                identity = identity_key(mode, player['username'], player['uuid'])
            except ValueError:
                continue
            if identity in known:
                links[player['uuid']] = identity
        return links

    def _change_safe_ip(self, conn, profile, ip, *, add, source, actor):
        entries = profile.setdefault('safe_ips', [])
        existing = next((entry for entry in entries if entry['ip'] == ip), None)
        if add and existing is None:
            if len(entries) >= MAX_SAFE_IPS:
                raise ValueError(f'A player can have up to {MAX_SAFE_IPS} safe IP addresses.')
            entries.append(dict(ip=ip, source=source, added_at=_utc(self._clock())))
        elif not add and existing is not None:
            entries.remove(existing)
        else:
            return False
        self._store_profile(conn, profile)
        self._audit(conn, 'safe_ip_added' if add else 'safe_ip_removed', actor,
                    dict(identity=profile['identity'], ip=ip, source=source))
        return True

    def manage_safe_ip(self, identity, ip, *, add, actor):
        try:
            ip = normalize_ip(ip.strip() if isinstance(ip, str) else ip)
        except ValueError:
            raise ValueError('Enter a single IPv4 or IPv6 address.') from None
        with self._transaction() as conn:
            policy, profile = self._policy(conn), self._profile(conn, identity)
            if profile is None or not identity.startswith(policy['identity_mode'] + ':'):
                raise ValueError('No Byte Guard profile exists for this player in the current identity mode.')
            self._change_safe_ip(conn, profile, ip, add=add, source='panel', actor=actor)
            return profile

    def save_session_safe_ip(self, payload):
        attempt_id = payload.get('attempt_id')
        if not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
            raise ValueError('Invalid attempt ID.')
        with self._transaction() as conn:
            policy, event = self._policy(conn), self._event(conn, attempt_id)
            self._validate_scope(policy, payload, revision=True)
            if (policy['mode'] == 'off' or self._effective_mode() != policy['identity_mode']
                    or not event or event['policy_revision'] != policy['policy_revision']
                    or event['identity_mode'] != policy['identity_mode'] or event['action'] != 'allow'
                    or event.get('revoked') or not event['completed'] or not event['joined']
                    or event['disconnected'] or (policy['identity_mode'] == 'offline' and not event['authenticated'])):
                raise GuardConflict('A current authenticated join recorded by Byte Guard is required.')
            profile = self._profile(conn, event['identity'])
            if profile is None:
                raise GuardConflict('The player profile is unavailable.')
            created = self._change_safe_ip(conn, profile, event['ip'], add=True,
                                           source='player', actor=event['username'])
            return dict(protocol_version=PROTOCOL_VERSION, attempt_id=attempt_id, accepted=True,
                        created=created, **policy)

    def reset(self):
        with self._transaction() as conn:
            self._clear_scope(conn, str(self.scope_provider()))
        return self.snapshot()

    def maintenance(self):
        with self._transaction() as conn:
            now = self._clock()
            for table in ('attempts', 'audit'):
                conn.execute('DELETE FROM ' + table + ' WHERE created<?', (now - EVENT_DAYS * 86400,))
                conn.execute('DELETE FROM ' + table + ' WHERE seq IN (SELECT seq FROM ' + table +
                             ' ORDER BY seq DESC LIMIT -1 OFFSET ?)', (EVENT_LIMIT,))
            conn.execute('DELETE FROM reservations WHERE expires<?', (now,))
            conn.execute('DELETE FROM approvals WHERE expires<?', (now,))
            conn.execute('DELETE FROM inflight WHERE expires<?', (now,))
        return self.geoip.maintenance()

    def _history_candidates(self, players_db_path, mode):
        path = Path(players_db_path)
        if not path.exists():
            return [], 0
        ordinary_file(path)
        candidates, skipped, seen = [], 0, set()
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            source.row_factory = sqlite3.Row
            rows = source.execute('''SELECT u.uuid,u.username,l.ip,l.timestamp
                 FROM users u JOIN login_events l ON l.uuid=u.uuid
                 ORDER BY l.timestamp ASC,l.event_id ASC LIMIT 100001''').fetchall()
        if len(rows) > EVENT_LIMIT:
            raise ValueError('Player history is too large for a safe one-time import.')
        for row in rows:
            try:
                # Even offline history requires UUIDs: shared "unknown" conflates identities.
                identity_key('online', row['username'], row['uuid'])
                identity = identity_key(mode, row['username'], row['uuid'])
                ip = normalize_ip(row['ip'])
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(dict(identity=identity, username=row['username'], ip=ip,
                                       timestamp=row['timestamp'], location=self._location(ip),
                                       source='estimated_from_historical_ip', authenticated=mode == 'online'))
            except (ValueError, TypeError):
                skipped += 1
        return candidates, skipped

    def history_preview(self, players_db_path):
        with self._transaction() as conn:
            policy = self._policy(conn)
            candidates, skipped = self._history_candidates(players_db_path, policy['identity_mode'])
            return dict(candidates=candidates, skipped=skipped,
                        imported=self._meta(conn, 'history_imported', False), scope=policy['scope'])

    def history_import(self, players_db_path):
        with self._transaction() as conn:
            if self._meta(conn, 'history_imported', False):
                raise GuardConflict('Historical references have already been confirmed for this server.')
            policy = self._policy(conn)
            candidates, skipped = self._history_candidates(players_db_path, policy['identity_mode'])
            count = 0
            for candidate in candidates:
                if self._profile(conn, candidate['identity']):
                    continue
                profile = dict(identity=candidate['identity'], username=candidate['username'],
                               first_seen=candidate['timestamp'], source=candidate['source'],
                               references=[candidate['location']] if _usable(candidate['location']) else [],
                               accepted_ips=[candidate['ip']], original_ip=candidate['ip'],
                               authenticated=candidate['authenticated'])
                self._store_profile(conn, profile)
                count += 1
            self._setmeta(conn, 'history_imported', True)
            self._setmeta(conn, 'policy_revision', policy['policy_revision'] + 1)
            self._audit(conn, 'history_confirmed', 'panel administrator', dict(imported=count, skipped=skipped))
            return dict(imported=count, skipped=skipped, scope=policy['scope'])
