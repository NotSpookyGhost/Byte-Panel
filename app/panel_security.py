"""Central HTTP/WebSocket authorization and account/SMTP API routes."""

import secrets
from pathlib import Path
import sqlite3
import threading
import time

from flask import Blueprint, g, jsonify, redirect, render_template, request, session, url_for

from panel_accounts import (AccountStore, PERMISSIONS, grants, session_grants, persistent_secret,
                            PASSWORD_RULES, PASSWORD_REQUIREMENTS, PASSWORD_PATTERN)
from panel_mail import PanelMailer, PasswordEmailDeliveryError, smtp_failure
from bluemap import MAP_ENDPOINTS


ROUTE_PERMISSIONS = {
    'overview': None, 'api_stats': None, 'logout': None, 'api_minecraft_versions': None,
    'console': 'console.view', 'get_console': 'console.view',
    'handle_api_command': 'console.command',
    'logs': 'logs.view', 'clear_logs': 'logs.clear',
    'players': 'players.view', 'api_players': 'players.view',
    'get_player_history': 'players.view', 'get_player_ban_history': 'players.view',
    'moderate_player': 'players.manage', 'properties': 'properties.manage',
    'start_server': 'server.control', 'stop_server': 'server.control', 'server_action': 'server.control',
    'settings': 'settings.manage', 'purge_server': 'server.purge', 'import_server': 'server.import',
    'addon_update_settings': 'settings.manage', 'check_addon_updates': 'settings.manage',
    'apply_addon_updates': 'settings.manage',
    'backups_page': ('backups.create', 'backups.restore'),
    'api_list_backups': ('backups.create', 'backups.restore'),
    'api_download_backup': 'backups.create', 'api_delete_backup': 'backups.create',
    'api_restore_backup': 'backups.restore', 'rename_backup': 'backups.create',
    'api_create_backup': 'backups.create',
    'api_backup_status': 'backups.create',
    'plugins.page': 'plugins.view', 'plugins.list_files': 'plugins.view',
    'plugins.config_file': 'plugins.view', 'plugins.upload': 'plugins.manage',
    'plugins.create_config': 'plugins.manage',
    'plugins.download': 'plugins.view', 'plugins.delete_plugin': 'plugins.manage',
    'files.page': 'files.view', 'files.list_files': 'files.view',
    'files.file': 'files.view', 'files.entry_details': 'files.view', 'files.download': 'files.view',
    'files.upload': 'files.manage', 'files.mkdir': 'files.manage',
    'files.create': 'files.manage', 'files.move': 'files.manage',
    'accounts.users_page': 'users.manage', 'accounts.users_api': 'users.manage',
    'accounts.user_api': 'users.manage', 'accounts.smtp_api': 'settings.manage',
    'accounts.test_smtp': 'settings.manage', 'accounts.my_account': None,
    'accounts.password_api': None,
    'accounts.invite_api': 'users.manage',
}
SETUP_ROUTES = {'welcome', 'setup', 'api_setup_progress', 'accounts.bootstrap'}


class PanelSecurity:
    def __init__(self, app, data_dir, load_config, save_config, get_assets):
        self.app = app
        self.store = AccountStore(data_dir)
        self.mailer = PanelMailer(self.store, load_config, server_dir=Path(data_dir) / 'server')
        self.load_config = load_config
        self.get_assets = get_assets
        self.failures = {}
        self.lock = threading.RLock()
        self.socket_clients = {}
        app.secret_key = persistent_secret(data_dir)
        app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
        # Upgrade the legacy owner without changing their credentials.
        config = load_config()
        if config.get('is_setup') and config.get('admin_pass') and not self.store.list_users():
            self.store.bootstrap(config.get('admin_user', 'admin'), config['admin_pass'], legacy=True)
        if 'admin_pass' in config:
            config.pop('admin_pass', None)
            save_config(config)
        app.before_request(self.authorize)
        app.context_processor(self.context)
        app.after_request(self.private_response)
        self.register_routes()

    def current_user(self):
        return self.store.current_user(session)

    def can(self, permission):
        return permission in session_grants(getattr(g, 'panel_user', None))

    def context(self):
        session.setdefault('panel_csrf', secrets.token_hex(32))
        return dict(current_user=getattr(g, 'panel_user', None), can=self.can,
                    panel_permissions=sorted(session_grants(getattr(g, 'panel_user', None))),
                    panel_csrf=session['panel_csrf'], permission_labels=PERMISSIONS,
                    can_assign_admin=set(PERMISSIONS) <= session_grants(getattr(g, 'panel_user', None)),
                    password_rules=PASSWORD_RULES, password_requirements=PASSWORD_REQUIREMENTS,
                    password_pattern=PASSWORD_PATTERN)

    def error(self, message, status):
        if request.path.startswith('/api/') or request.is_json:
            return jsonify(error=message, message=message, status='error'), status
        return render_template('access_error.html', message=message, config=self.load_config()), status

    def authorize(self):
        endpoint = request.endpoint
        # Only BlueMap's GET/HEAD routes are public; each checks setup/enabled
        # state. Do not read or forward panel sessions to the map webserver.
        if endpoint in MAP_ENDPOINTS:
            return None
        if endpoint in ('static', 'serve_custom_branding') or endpoint is None:
            return None
        g.panel_user = self.current_user()
        if session.get('logged_in') and not g.panel_user:
            session.clear()
        config = self.load_config()
        unclaimed = not self.store.list_users() and not config.get('is_setup')
        if endpoint == 'api_minecraft_versions' and unclaimed and request.method == 'GET':
            return None
        # All changes, including the initial owner claim, require a browser CSRF token.
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-Panel-CSRF', '') or request.form.get('_csrf', '')
            expected = session.get('panel_csrf', '')
            if not expected or not secrets.compare_digest(str(supplied).encode(), expected.encode()):
                return self.error('Session expired or request could not be verified. Refresh the page and try again.', 403)
        if g.panel_user and g.panel_user['must_change_password'] and endpoint not in ('accounts.my_account', 'accounts.password_api', 'logout', 'login'):
            if request.path.startswith('/api/') or request.is_json:
                return self.error('Change your temporary password under My account before using the panel.', 403)
            return redirect(url_for('accounts.my_account'))
        if endpoint == 'login':
            if unclaimed:
                return redirect(url_for('setup'))
            return None
        if endpoint in SETUP_ROUTES:
            # The owner must be able to read the final result after the worker
            # saves is_setup=True. All setup mutations still remain locked.
            if config.get('is_setup') and endpoint != 'api_setup_progress':
                return self.error('Reset the server before running setup again.', 409)
            if unclaimed and endpoint in ('welcome', 'setup', 'accounts.bootstrap'):
                if request.method == 'GET' or endpoint in ('welcome', 'accounts.bootstrap'):
                    return None
            if not g.panel_user:
                return self.login_required()
            if g.panel_user['role'] != 'owner':
                return self.error('Only the server owner can complete setup.', 403)
            return None
        if not g.panel_user:
            if unclaimed and not request.path.startswith('/api/'):
                return redirect(url_for('welcome' if not config.get('welcome_seen') else 'setup'))
            return self.login_required()
        if not config.get('is_setup') and endpoint not in ('api_list_backups', 'import_server', 'logout', 'accounts.my_account', 'accounts.password_api', 'api_minecraft_versions'):
            return self.error('Complete server setup first.', 409) if request.path.startswith('/api/') else redirect(url_for('setup'))
        # Explicit endpoint allow-list: newly added routes fail closed.
        if endpoint not in ROUTE_PERMISSIONS:
            return self.error('Access to this operation has not been granted.', 403)
        needed = ROUTE_PERMISSIONS[endpoint]
        if endpoint == 'plugins.config_file' and request.method == 'PUT':
            needed = 'plugins.manage'
        if (endpoint == 'files.file' and request.method == 'PUT') or (endpoint == 'files.entry_details' and request.method == 'DELETE'):
            needed = 'files.manage'
        if endpoint == 'import_server' and 'import_backup_path' in request.form:
            needed = 'backups.restore'
        if endpoint == 'purge_server' and g.panel_user['role'] != 'owner':
            return self.error('Only the original setup owner can purge this server.', 403)
        allowed = any(self.can(permission) for permission in needed) if isinstance(needed, tuple) else not needed or self.can(needed)
        if not allowed:
            return self.error('Your account does not have permission for this operation.', 403)

    def login_required(self):
        if request.path.startswith('/api/'):
            return self.error('Sign in to continue.', 401)
        return redirect(url_for('login'))

    @staticmethod
    def private_response(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    def login(self, username, password):
        # Use the direct peer, not an untrusted client-supplied forwarding header.
        peer = request.remote_addr or 'unknown'
        now = time.monotonic()
        with self.lock:
            self.failures = {key: value for key, value in self.failures.items() if now - value[1] < 600}
            count, _ = self.failures.get(peer, (0, now))
            if count >= 10:
                return None, 'Too many failed attempts. Try again in 10 minutes.'
        user = self.store.authenticate(username, password)
        with self.lock:
            if user:
                self.failures.pop(peer, None)
                self.store.sign_in(session, user)
                return user, None
            previous = self.failures.get(peer, (0, now))
            self.failures[peer] = (previous[0] + 1, previous[1])
        return None, 'Invalid username or password.'

    def emit_console(self, socketio, line):
        with self.lock:
            clients = list(self.socket_clients.items())
        for sid, identity in clients:
            if 'console.view' in session_grants(self.store.current_user(identity)):
                socketio.emit('console_output', {'data': line}, to=sid)
            else:
                with self.lock:
                    self.socket_clients.pop(sid, None)

    def register_routes(self):
        bp = Blueprint('accounts', __name__)

        @bp.before_request
        def limit_body():
            if request.content_length and request.content_length > 65536:
                return self.error('Request is too large.', 413)

        @bp.errorhandler(ValueError)
        def invalid(error):
            return self.error(str(error), 400)

        @bp.errorhandler(sqlite3.IntegrityError)
        def duplicate(error):
            return self.error('That username is already in use (names are case-insensitive).', 409)

        def payload():
            value = request.get_json(silent=True)
            if not isinstance(value, dict):
                raise ValueError('Send a JSON object.')
            return value

        @bp.route('/api/bootstrap', methods=['POST'])
        def bootstrap():
            data = payload()
            user = self.store.bootstrap(data.get('username', ''), data.get('password', ''))
            self.store.sign_in(session, user)
            return jsonify(user=user), 201

        @bp.route('/users')
        def users_page():
            return render_template('users.html', config=self.load_config(), has_custom=self.get_assets())

        @bp.route('/account')
        def my_account():
            return render_template('account.html', config=self.load_config(), has_custom=self.get_assets())

        @bp.route('/api/account/password', methods=['POST'])
        def password_api():
            data = payload()
            if 'confirm_password' in data and data['confirm_password'] != data.get('password'):
                raise ValueError('The new passwords do not match.')
            first_login = g.panel_user['must_change_password']
            user = self.store.change_password(g.panel_user, data.get('current_password', ''), data.get('password', ''))
            self.store.sign_in(session, user)
            return jsonify(message='Password changed. Your other sessions have been signed out.',
                           redirect=url_for('overview') if first_login else None)

        def require_invitation_delivery():
            config = self.mailer.config()
            if not config['enabled'] or not config['notify_new_users']:
                raise ValueError('Enable SMTP delivery and automatic account invitations in Settings before creating or inviting users.')
            if not config['host'] or not config['from_email'] or not config['panel_url']:
                raise ValueError('Complete and save SMTP settings before sending account invitations.')

        def deliver_invitation(user, temporary_password):
            try:
                result = self.mailer.welcome(user, temporary_password)
                return result if result in ('sent', 'cancelled') else 'failed'
            except Exception as error:
                # Never log the message, password, recipient or provider reply.
                part = 'password' if isinstance(error, PasswordEmailDeliveryError) else 'welcome'
                if part == 'password':
                    error = error.error
                diagnostic = smtp_failure(error)
                self.app.logger.warning('Account invitation failed: account=%s part=%s category=%s smtp_code=%s stage=%s',
                                        user['id'], part, diagnostic['code'], diagnostic['smtp_code'], diagnostic['stage'])
                return 'password_failed' if part == 'password' else 'failed'

        @bp.route('/api/users', methods=['GET', 'POST'])
        def users_api():
            if request.method == 'GET':
                return jsonify(users=self.store.list_users())
            data = payload()
            if data.get('password'):
                raise ValueError('Temporary passwords are generated automatically. Refresh the Users page before creating an account.')
            require_invitation_delivery()
            user, temporary_password = self.store.invite_user(data, g.panel_user)
            delivery = deliver_invitation(user, temporary_password)
            return jsonify(user=user, email_status=delivery), 201

        @bp.route('/api/users/<uid>/invite', methods=['POST'])
        def invite_api(uid):
            require_invitation_delivery()
            user, temporary_password = self.store.invite_user({}, g.panel_user, uid)
            return jsonify(user=user, email_status=deliver_invitation(user, temporary_password))

        @bp.route('/api/users/<uid>', methods=['PUT', 'DELETE'])
        def user_api(uid):
            if request.method == 'DELETE':
                self.store.delete_user(uid, g.panel_user)
                return jsonify(message='Account deleted and sessions revoked.')
            data = payload()
            if data.get('password'):
                raise ValueError('Use Send temporary password instead of choosing a password for another user.')
            return jsonify(user=self.store.save_user(data, g.panel_user, uid=uid))

        @bp.route('/api/settings/smtp', methods=['GET', 'PUT'])
        def smtp_api():
            if request.method == 'PUT':
                return jsonify(smtp=self.mailer.save(payload()))
            return jsonify(smtp=self.mailer.config(public=True))

        @bp.route('/api/settings/smtp/test', methods=['POST'])
        def test_smtp():
            data = payload()
            # Validate before attempting delivery, and report failures without secret-bearing exceptions.
            from panel_accounts import validate_email
            recipient = validate_email(data.get('recipient', ''), required=True)
            if not self.mailer.config()['enabled']:
                raise ValueError('Enable and save SMTP settings first.')
            try:
                self.mailer.send(recipient, 'Byte Panel SMTP test', 'SMTP delivery from your Minecraft panel is working.\n')
            except Exception as error:
                diagnostic = smtp_failure(error)
                self.app.logger.warning('SMTP test failed: category=%s smtp_code=%s stage=%s elapsed_seconds=%s exception=%s',
                                        diagnostic['code'], diagnostic['smtp_code'], diagnostic['stage'],
                                        diagnostic['elapsed_seconds'], type(error).__name__)
                # The diagnostic request completed, even though SMTP delivery failed.
                # Avoid 502 here: reverse proxies may replace its JSON with an HTML page.
                return jsonify(success=False, status='error', error=diagnostic['message'], message=diagnostic['message'],
                               error_code=diagnostic['code'], smtp_code=diagnostic['smtp_code'],
                               stage=diagnostic['stage'], elapsed_seconds=diagnostic['elapsed_seconds'])
            return jsonify(success=True, status='success',
                           message='Test email accepted by the SMTP server. Check the inbox and spam folder.')

        self.app.register_blueprint(bp)
