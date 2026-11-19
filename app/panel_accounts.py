"""Server-scoped identities. Never place this database in a server backup."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import string
import time

from werkzeug.security import check_password_hash, generate_password_hash


PERMISSIONS = {
    'server.control': 'Start, stop and restart the server',
    'console.view': 'Read the live console',
    'console.command': 'Run arbitrary console commands (powerful)',
    'logs.view': 'Read log history',
    'logs.clear': 'Clear log history',
    'players.view': 'View players, IP addresses and history',
    'players.manage': 'Kick, ban and unban players',
    'properties.manage': 'Edit server properties',
    'plugins.view': 'Browse and download plugins/mods and their configs',
    'plugins.manage': 'Upload/delete executable plugins/mods and create or edit configs (powerful)',
    'files.view': 'Browse and download all server files (may contain secrets)',
    'files.manage': 'Create, upload, edit, move and delete server files (powerful)',
    'backups.create': 'Create and manage backups (download, rename and delete)',
    'backups.restore': 'Restore backups and replace server data (powerful)',
    'settings.manage': 'Edit panel, runtime and SMTP settings (powerful)',
    'users.manage': 'Create and manage accounts within your own permissions',
    'server.import': 'Import a server ZIP and replace server data (powerful)',
}
# Owner capabilities are never assignable, including through the Administrator role.
OWNER_PERMISSIONS = {'server.purge'}
DEPENDENCIES = {
    'console.command': 'console.view', 'logs.clear': 'logs.view',
    'players.manage': 'players.view', 'plugins.manage': 'plugins.view',
    'files.manage': 'files.view',
}


TEMPORARY_PASSWORD_TTL = 24 * 60 * 60
PASSWORD_RULES = (
    (r'[A-Z]', 'an uppercase letter (A-Z)'),
    (r'[0-9]', 'a number (0-9)'),
    (r'[\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e]', 'a punctuation symbol (for example !, @ or #)'),
)
PASSWORD_REQUIREMENTS = ('Use 12-256 characters with at least one uppercase letter (A-Z), '
                         'one number (0-9), and one punctuation symbol (for example !, @ or #).')
# Share the exact category expressions with both password forms.
PASSWORD_PATTERN = ''.join(r'(?=[\s\S]*' + pattern + ')' for pattern, _ in PASSWORD_RULES) + r'[\s\S]{12,256}'


def grants(user):
    if not user:
        return set()
    if user['role'] == 'owner':
        return set(PERMISSIONS) | OWNER_PERMISSIONS
    if user['role'] == 'admin':
        return set(PERMISSIONS)
    return set(user['permissions']) & set(PERMISSIONS)


def session_grants(user):
    # Assigned permissions remain intact for management checks, but a temporary
    # password never grants application or WebSocket access.
    return set() if user and user.get('must_change_password') else grants(user)


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Use a password between 12 and 256 characters.')
    missing = [label for pattern, label in PASSWORD_RULES if not re.search(pattern, password)]
    if missing:
        raise ValueError('Password must include ' + ', '.join(missing) + '.')
    return password


def generate_temporary_password():
    """Use cryptographic randomness and guarantee each required character class."""
    symbols = '!@#$%&*+-_=?'
    alphabet = string.ascii_letters + string.digits + symbols
    characters = [secrets.choice(string.ascii_uppercase), secrets.choice(string.digits),
                  secrets.choice(symbols)]
    characters.extend(secrets.choice(alphabet) for _ in range(21))
    secrets.SystemRandom().shuffle(characters)
    return validate_password(''.join(characters))


def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256:600000')


def validate_email(email, required=False):
    if not isinstance(email, str):
        raise ValueError('Enter a valid email address.')
    email = email.strip()
    if not email and not required:
        return ''
    if len(email) > 254 or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', email):
        raise ValueError('Enter a valid email address.')
    return email


def user_fields(data):
    username = data.get('username', '')
    if not isinstance(username, str):
        raise ValueError('Enter a username.')
    username = username.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,32}', username):
        raise ValueError('Username must be 3–32 letters, numbers, dots, underscores or hyphens.')
    role = data.get('role', 'custom')
    if role not in ('admin', 'custom'):
        raise ValueError('Select Administrator or Custom permissions.')
    permissions = data.get('permissions', [])
    if not isinstance(permissions, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions):
        raise ValueError('Unknown permission.')
    permissions = set(permissions)
    permissions.update(DEPENDENCIES[p] for p in list(permissions) if p in DEPENDENCIES)
    enabled = data.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError('Enabled must be true or false.')
    return dict(username=username, email=validate_email(data.get('email', '')),
                role=role, permissions=sorted(permissions), enabled=enabled)


def persistent_secret(data_dir):
    configured = os.environ.get('PANEL_SECRET_KEY')
    if configured:
        return configured
    path = Path(data_dir) / 'panel_session.key'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding='utf-8').strip()
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        value = secrets.token_hex(32)
        handle.write(value)
    return value


class AccountStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'panel_accounts.db'
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
                permissions TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL)''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(accounts)')}
            if 'must_change_password' not in columns:
                db.execute('ALTER TABLE accounts ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0')
            if 'temporary_password_expires' not in columns:
                db.execute('ALTER TABLE accounts ADD COLUMN temporary_password_expires REAL')
            # Preserve effective legacy backup/import access, never legacy purge
            # rights. The old restore API required BOTH manage and reset.
            for row in db.execute('SELECT id, permissions FROM accounts').fetchall():
                previous = set(json.loads(row['permissions']))
                migrated = previous & set(PERMISSIONS)
                if 'backups.manage' in previous:
                    migrated.add('backups.create')
                if 'server.reset' in previous:
                    migrated.add('server.import')
                    if 'backups.manage' in previous:
                        migrated.add('backups.restore')
                if migrated != previous:
                    db.execute('UPDATE accounts SET permissions=?, revision=revision+1 WHERE id=?',
                               (json.dumps(sorted(migrated)), row['id']))
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('scope', secrets.token_hex(32)))
        os.chmod(self.path, 0o600)
        self.dummy_hash = hash_password(secrets.token_urlsafe(24))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row):
        if row is None:
            return None
        user = dict(row)
        user.pop('password_hash', None)
        user['permissions'] = json.loads(user['permissions'])
        user['enabled'] = bool(user['enabled'])
        user['must_change_password'] = bool(user['must_change_password'])
        return user

    @staticmethod
    def password_is_current(row):
        return not row['must_change_password'] or (row['temporary_password_expires'] or 0) > time.time()

    def list_users(self):
        with self.connection() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM accounts ORDER BY created, username')]

    def invitation_is_current(self, user):
        """Do not send a delayed password for an edited, revoked or expired invite."""
        with self.connection() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (user['id'],)).fetchone()
        return bool(row and row['revision'] == user['revision'] and row['enabled']
                    and row['must_change_password'] and self.password_is_current(row)
                    and row['email'] == user['email'] and row['username'] == user['username'])

    def scope(self):
        with self.connection() as db:
            return db.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()[0]

    def current_user(self, cookie):
        with self.connection() as db:
            if cookie.get('account_scope') != db.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()[0]:
                return None
            row = db.execute('SELECT * FROM accounts WHERE id=? AND enabled=1', (cookie.get('account_id'),)).fetchone()
            if not row or row['revision'] != cookie.get('account_revision') or not self.password_is_current(row):
                return None
            return self.public(row)

    def sign_in(self, cookie, user):
        csrf = cookie.get('panel_csrf')
        cookie.clear()
        cookie.update(logged_in=True, account_id=user['id'], account_revision=user['revision'],
                      account_scope=self.scope(), panel_csrf=csrf or secrets.token_hex(32))

    def authenticate(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            return None
        with self.connection() as db:
            row = db.execute('SELECT * FROM accounts WHERE username=?', (username.strip(),)).fetchone()
        valid = check_password_hash(row['password_hash'] if row else self.dummy_hash, password)
        return self.public(row) if valid and row and row['enabled'] and self.password_is_current(row) else None

    def _insert(self, db, fields, password_hash, owner=False, temporary=False):
        uid = secrets.token_hex(16)
        db.execute('''INSERT INTO accounts
            (id, username, email, password_hash, role, permissions, enabled, created,
             must_change_password, temporary_password_expires)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                uid, fields['username'], fields.get('email', ''), password_hash,
                'owner' if owner else fields['role'], json.dumps(fields.get('permissions', [])),
                int(fields.get('enabled', True)), datetime.now(timezone.utc).isoformat(),
                int(temporary), time.time() + TEMPORARY_PASSWORD_TTL if temporary else None))
        return self.public(db.execute('SELECT * FROM accounts WHERE id=?', (uid,)).fetchone())

    def bootstrap(self, username, password, legacy=False):
        # Legacy usernames/passwords continue working; newly chosen ones are validated.
        fields = dict(username=username, role='custom', permissions=[], email='', enabled=True)
        if not legacy:
            fields = user_fields(fields)
            validate_password(password)
        encoded = hash_password(password)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
                raise ValueError('An owner already exists. Sign in with that account.')
            return self._insert(db, fields, encoded, owner=True)

    def save_user(self, data, actor, uid=None, *, temporary=False):
        fields = user_fields(data)
        password = data.get('password', '')
        encoded = hash_password(validate_password(password)) if password or not uid else None
        desired = set(PERMISSIONS) if fields['role'] == 'admin' else set(fields['permissions'])
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # Re-check the actor inside the write transaction to avoid stale privileges.
            live_actor = self.public(db.execute('SELECT * FROM accounts WHERE id=?', (actor['id'],)).fetchone())
            if not live_actor or not live_actor['enabled'] or live_actor['revision'] != actor['revision'] or 'users.manage' not in session_grants(live_actor):
                raise ValueError('Your permissions changed. Sign in again.')
            if not desired <= grants(live_actor):
                raise ValueError('You cannot grant permissions you do not have.')
            if uid:
                target = self.public(db.execute('SELECT * FROM accounts WHERE id=?', (uid,)).fetchone())
                self.check_target(live_actor, target)
                if target['must_change_password']:
                    validate_email(fields['email'], required=True)
                    if fields['email'] != target['email']:
                        # A corrected invitation address needs a fresh password.
                        db.execute('UPDATE accounts SET temporary_password_expires=0 WHERE id=?', (uid,))
                db.execute('''UPDATE accounts SET username=?, email=?, role=?, permissions=?, enabled=?,
                    password_hash=COALESCE(?, password_hash), revision=revision+1 WHERE id=?''',
                    (fields['username'], fields['email'], fields['role'], json.dumps(fields['permissions']),
                     int(fields['enabled']), encoded, uid))
                return self.public(db.execute('SELECT * FROM accounts WHERE id=?', (uid,)).fetchone())
            return self._insert(db, fields, encoded, temporary=temporary)

    def invite_user(self, data, actor, uid=None):
        """Return the secret only to the email sender, never to API serializers."""
        temporary_password = generate_temporary_password()
        if uid is None:
            validate_email(data.get('email', ''), required=True)
            user = self.save_user({**data, 'password': temporary_password}, actor, temporary=True)
            return user, temporary_password
        encoded = hash_password(temporary_password)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            live_actor = self.public(db.execute('SELECT * FROM accounts WHERE id=?', (actor['id'],)).fetchone())
            if not live_actor or not live_actor['enabled'] or live_actor['revision'] != actor['revision'] or 'users.manage' not in session_grants(live_actor):
                raise ValueError('Your permissions changed. Sign in again.')
            target = self.public(db.execute('SELECT * FROM accounts WHERE id=?', (uid,)).fetchone())
            self.check_target(live_actor, target)
            validate_email(target['email'], required=True)
            db.execute('''UPDATE accounts SET password_hash=?, must_change_password=1,
                temporary_password_expires=?, revision=revision+1 WHERE id=?''',
                (encoded, time.time() + TEMPORARY_PASSWORD_TTL, uid))
            user = self.public(db.execute('SELECT * FROM accounts WHERE id=?', (uid,)).fetchone())
            return user, temporary_password

    @staticmethod
    def check_target(actor, target):
        if not target:
            raise ValueError('Account not found.')
        if target['role'] == 'owner' or target['id'] == actor['id']:
            raise ValueError('The owner and your own account cannot be changed here. Use My account to change your password.')
        if not grants(target) <= grants(actor):
            raise ValueError('You cannot manage an account with more permissions than your own.')

    def delete_user(self, uid, actor):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            live_actor = self.public(db.execute('SELECT * FROM accounts WHERE id=?', (actor['id'],)).fetchone())
            if not live_actor or not live_actor['enabled'] or live_actor['revision'] != actor['revision'] or 'users.manage' not in session_grants(live_actor):
                raise ValueError('Your permissions changed. Sign in again.')
            target = self.public(db.execute('SELECT * FROM accounts WHERE id=?', (uid,)).fetchone())
            self.check_target(live_actor, target)
            db.execute('DELETE FROM accounts WHERE id=?', (uid,))

    def change_password(self, user, old_password, password):
        if not self.authenticate(user['username'], old_password):
            raise ValueError('Current password is incorrect.')
        if password == old_password:
            raise ValueError('Choose a new password that is different from your current or temporary password.')
        encoded = hash_password(validate_password(password))
        with self.connection() as db:
            changed = db.execute('''UPDATE accounts SET password_hash=?, revision=revision+1,
                must_change_password=0, temporary_password_expires=NULL WHERE id=? AND revision=?
                AND (must_change_password=0 OR temporary_password_expires>?)''',
                                 (encoded, user['id'], user['revision'], time.time())).rowcount
            if not changed:
                raise ValueError('Your account changed. Sign in again.')
            return self.public(db.execute('SELECT * FROM accounts WHERE id=?', (user['id'],)).fetchone())

    def reset(self, keep_actor=None):
        """Clear a purged server, or retain its owner and acting user on replacement.

        A restore/import must never transfer ownership to the acting user.
        Retained identities keep their password, role and assigned permissions.
        All previous sessions and other users are removed from the new scope.
        """
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if keep_actor:
                actor = db.execute('SELECT * FROM accounts WHERE id=?', (keep_actor['id'],)).fetchone()
                owner = db.execute("SELECT * FROM accounts WHERE role='owner'").fetchone()
                if not actor or not actor['enabled'] or actor['revision'] != keep_actor['revision']:
                    raise ValueError('Your account changed. Sign in again.')
                if not owner or not owner['enabled']:
                    raise ValueError('The original server owner is missing. Replacement was not authorized.')
                db.execute('DELETE FROM accounts WHERE id NOT IN (?, ?)', (owner['id'], actor['id']))
                db.execute('UPDATE accounts SET revision=revision+1')
            else:
                db.execute('DELETE FROM accounts')
            db.execute("UPDATE metadata SET value=? WHERE key='scope'", (secrets.token_hex(32),))
            if keep_actor:
                return self.public(db.execute('SELECT * FROM accounts WHERE id=?', (keep_actor['id'],)).fetchone())

    def get_setting(self, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', (key, json.dumps(value)))
