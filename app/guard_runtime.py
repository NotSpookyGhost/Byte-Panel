"""Byte Guard private bridge, authenticated panel API and startup boundary."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flask import Blueprint, abort, g, jsonify, request
from werkzeug.exceptions import HTTPException

from connection_guard import ConnectionGuard, GuardConflict
from guard_auth import GuardAuth, GuardAuthError, GUARD_JAR, SUPPORTED_VERSIONS, KNOWN_PROVIDERS
from guard_auth import atomic_private, checked_path, descriptor, digest

MAX_COMPANION_BYTES = 16 * 1024 * 1024


def _java_unescape(value):
    result, index = [], 0
    while index < len(value):
        character = value[index]
        index += 1
        if character == '\\':
            if index == len(value):
                break
            character = value[index]
            index += 1
            if character == 'u':
                raw = value[index:index + 4]
                if len(raw) != 4 or not re.fullmatch(r'[0-9a-fA-F]{4}', raw):
                    raise GuardAuthError('Malformed Unicode escape in server.properties.')
                character = chr(int(raw, 16))
                index += 4
            else:
                character = {'t': '\t', 'n': '\n', 'r': '\r', 'f': '\f'}.get(character, character)
        result.append(character)
    return ''.join(result)


def effective_online_mode(server):
    """Match Java Properties separators, continuations and escapes, not a UI approximation."""
    path = checked_path(server, 'server.properties', missing=True)
    values = {}
    if path.exists():
        if path.stat().st_size > 1024 * 1024:
            raise GuardAuthError('server.properties exceeds the safe validation limit.')
        # Java Properties.load(InputStream) uses ISO-8859-1; relevant keys are ASCII.
        pending = []
        for physical in re.split(r'\r\n|\n|\r', path.read_text('latin-1')):
            if not pending and physical.lstrip(' \t\f').startswith(('#', '!')):
                continue
            line = physical.lstrip(' \t\f') if pending else physical
            tail = len(line) - len(line.rstrip('\\'))
            if tail % 2:
                pending.append(line[:-1])
                continue
            if pending:
                pending.append(line)
                line = ''.join(pending)
                pending.clear()
            line = line.lstrip(' \t\f')
            if not line or line.startswith(('#', '!')):
                continue
            escaped, separator = False, len(line)
            for index, character in enumerate(line):
                if not escaped and character in '=:\t \f':
                    separator = index
                    break
                escaped = character == '\\' and not escaped
            key, rest = _java_unescape(line[:separator]), line[separator:].lstrip(' \t\f')
            if rest.startswith(('=', ':')):
                rest = rest[1:].lstrip(' \t\f')
            values[key] = _java_unescape(rest)
        if pending:
            raise GuardAuthError('Finish the continuation at the end of server.properties.')
    override = checked_path(server, 'properties_override.json', missing=True)
    if override.exists():
        if override.stat().st_size > 1024 * 1024:
            raise GuardAuthError('Queued server properties exceed the safe validation limit.')
        try:
            queued = json.loads(override.read_text('utf-8'))
        except (ValueError, UnicodeError):
            raise GuardAuthError('Queued server properties are invalid. Correct them before starting.') from None
        if not isinstance(queued, dict) or any(not isinstance(key, str) or
                any(c in key for c in '\\\r\n:= \t') or
                not isinstance(value, (str, bool, int, float)) or
                any(c in str(value) for c in '\\\r\n') for key, value in queued.items()):
            raise GuardAuthError('Queued properties contain unsafe keys or values.')
        values.update({key: str(value) for key, value in queued.items()})
    value = str(values.get('online-mode', 'true')).lower()
    if value not in ('true', 'false'):
        raise GuardAuthError('online-mode must be explicitly true or false.')
    return value == 'true'


def redact_auth_commands(line):
    """Defense in depth for known commands; third-party loggers remain trusted."""
    command = r'(?:[a-z0-9_]+:)?(?:login|log|l|register|reg|changepassword|changepass|cp|unregister|unreg|email|totp|2fa|captcha|verification)'
    prefix = r'(?:issued server command:\s*|executing(?: server)? command\s*[:>]\s*)/?'
    line = re.sub(r'(?i)(' + prefix + command + r')\s+\S.*', r'\1 [REDACTED]', line)
    return re.sub(r'(?i)(\bauthme\s+(?:changepassword|password|register)\s+[A-Za-z0-9_]{1,16})\s+\S.*',
                  r'\1 [REDACTED]', line)


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            # A closed socket looks like an outage to the companion. Return an
            # explicit overload response so it cannot treat overload as approval.
            try:
                request.settimeout(0.05)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class GuardRuntime:
    def __init__(self, app, security, data_dir, server_dir, load_config, read_properties,
                 mutation_guard, is_running, on_outcome=None, port=5051):
        self.app, self.security = app, security
        self.data, self.server = Path(data_dir), Path(server_dir)
        self.load_config, self.read_properties = load_config, read_properties
        self.mutation_guard, self.is_running = mutation_guard, is_running
        self.on_outcome = on_outcome or (lambda result, payload: None)
        self.port, self.httpd = port, None
        self.start_lock = threading.Lock()
        self.token_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.guard = ConnectionGuard(data_dir, security.store.scope, self.effective_config)
        self.auth = GuardAuth(server_dir, data_dir, load_config)
        self.token_scope, self.token = None, None
        self._token()
        self.register_routes()
        self.app.add_template_global(self.sidebar_enabled, 'byte_guard_enabled')

    def effective_config(self):
        config = dict(self.load_config())
        online = effective_online_mode(self.server)
        config.update(identity_mode='online' if online else 'offline', online_mode=online)
        return config

    def _token(self):
        with self.token_lock:
            return self._ensure_token()

    def _ensure_token(self):
        scope = self.security.store.scope()
        if self.token_scope != scope:
            path = checked_path(self.data, 'connection-security/bridge-token.json', missing=True)
            value = {}
            if path.exists():
                try:
                    if path.stat().st_size < 4096:
                        saved = json.loads(path.read_text('utf-8'))
                        if isinstance(saved, dict):
                            value = saved
                except (ValueError, OSError):
                    value = {}
            token = value.get('token')
            if value.get('scope') != scope or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
                token = secrets.token_hex(32)
                atomic_private(path, json.dumps(dict(scope=scope, token=token)).encode())
            self.token_scope, self.token = scope, token
        return self.token

    def enabled(self):
        return self.guard.config().get('mode', 'off') != 'off'

    def sidebar_enabled(self):
        try:
            return self.guard.display_mode() in ('monitor', 'enforce')
        except (OSError, sqlite3.Error, ValueError):
            # Navigation stays usable if Guard's private storage needs attention.
            return False

    def auth_status(self, identity_mode=None, enabled=None):
        result = self.auth.status()
        identity_mode = identity_mode or self.effective_config()['identity_mode']
        result['online_mode'] = identity_mode == 'online'
        result['required'] = not result['online_mode'] and (self.enabled() if enabled is None else enabled)
        result['server_running'] = bool(self.is_running())
        return result

    def settings(self):
        result = dict(self.guard.snapshot())
        identity_mode = self.effective_config()['identity_mode']
        health = result.get('bridge', {})
        if self.httpd is None:
            health = dict(health, healthy=False, status='Bridge unavailable',
                          message='The private Byte Guard bridge is offline. Check the panel logs.')
        result.update(health=health, auth=self.auth_status(identity_mode, result['mode'] != 'off'))
        result['effective_identity_mode'] = identity_mode
        result['bridge_running'] = self.httpd is not None
        return result

    def validate_activation(self, values):
        mode = values.get('mode', self.guard.config().get('mode', 'off'))
        if mode == 'off':
            return
        config = self.effective_config()
        if config.get('server_type') not in ('Paper', 'Purpur') or config.get('mc_version') not in SUPPORTED_VERSIONS:
            raise GuardAuthError('Byte Guard requires a supported Paper/Purpur version (1.20.6 or newer).')
        self.validate_direct()
        self.validate_staged_updates()
        if config['identity_mode'] == 'offline':
            self.auth.require()

    def validate_direct(self):
        # Never accept forwarded source information in the direct-only release.
        from guard_auth import read_yaml, _get
        for relative, keys in (
            ('spigot.yml', ('settings.bungeecord',)),
            ('config/paper-global.yml', ('proxies.velocity.enabled', 'proxies.proxy-protocol')),
            ('paper.yml', ('settings.velocity-support.enabled',))):
            path = checked_path(self.server, relative, missing=True)
            if path.exists():
                value = read_yaml(path)
                if any(_get(value, key) is True for key in keys):
                    raise GuardAuthError('Proxy forwarding is enabled. This Guard release supports direct Minecraft connections only.')

    def validate_staged_updates(self):
        # Bukkit installs plugins/update contents during startup. Checking only
        # the currently installed JARs would miss a queued authentication upgrade.
        from guard_auth import read_yaml, _get
        config_path = checked_path(self.server, 'bukkit.yml', missing=True)
        config = read_yaml(config_path) if config_path.exists() else {}
        folder = _get(config, 'settings.update-folder')
        if folder is None:
            folder = 'update'
        if folder == '':
            return
        if (not isinstance(folder, str) or folder.startswith('/') or '\\' in folder or ':' in folder
                or any(part in ('', '.', '..') for part in folder.split('/'))):
            raise GuardAuthError('Guard requires a relative plugin update folder inside plugins.')
        relative = 'plugins/' + folder
        directory = checked_path(self.server, relative, missing=True)
        if not directory.exists():
            return
        if not directory.is_dir():
            raise GuardAuthError('The configured plugin update folder is not a directory.')
        protected_names = {GUARD_JAR.casefold()}
        protected_names.update(item['filename'].casefold() for item in self.auth.inventory()
                               if item['name'].lower() in KNOWN_PROVIDERS | {'packetevents', 'bytepanelguard'})
        for index, entry in enumerate(directory.iterdir()):
            if index >= 2048:
                raise GuardAuthError('The plugin update folder is too large to validate safely.')
            if entry.suffix.lower() != '.jar':
                continue
            path = checked_path(self.server, relative + '/' + entry.name)
            meta = descriptor(path)
            if (entry.name.casefold() in protected_names or not meta or
                    meta['name'].lower() in KNOWN_PROVIDERS | {'packetevents', 'bytepanelguard'}):
                raise GuardAuthError('A security plugin or unrecognized JAR is staged in the plugin update folder. Remove the queued update and use Byte Guard setup.')

    def prepare_start(self):
        """Called under the global startup/files reservation, before Java is spawned."""
        config = self.guard.config()
        enabled = config.get('mode', 'off') != 'off'
        path = checked_path(self.server, 'plugins/' + GUARD_JAR, missing=True)
        if not enabled and not path.exists():
            return []
        if enabled:
            self.validate_activation(config)
            actual = self.effective_config()['identity_mode']
            expected = config.get('identity_mode', actual)
            if expected != actual:
                raise GuardAuthError('Authentication mode changed. Reconfigure Byte Guard before starting Minecraft.')
            if self.httpd is None:
                raise GuardAuthError('The private Guard bridge is not ready. Restart the panel and retry.')
            bundled = Path(os.getenv('BYTEPANEL_GUARD_JAR', '/opt/bytepanel/guard/BytePanelGuard.jar'))
            checksum_file = bundled.with_suffix('.sha512')
            if not bundled.is_file() or not checksum_file.is_file():
                raise GuardAuthError('Bundled Byte Guard is missing. Rebuild this panel image before enabling it.')
            try:
                with checksum_file.open('rb') as handle:
                    checksum = handle.read(4097)
                with bundled.open('rb') as handle:
                    content = handle.read(MAX_COMPANION_BYTES + 1)
            except OSError:
                raise GuardAuthError('Bundled Byte Guard could not be read. Rebuild the panel image.') from None
            # Accept both the bundled plain digest and a standard sha512sum line.
            checksum_line = re.fullmatch(rb'([a-f0-9]{128})(?:[ \t][^\r\n]*)?(?:\r?\n)?', checksum)
            expected_hash = checksum_line.group(1) if checksum_line else None
            if (len(checksum) > 4096 or not expected_hash or not 0 < len(content) <= MAX_COMPANION_BYTES
                    or hashlib.sha512(content).hexdigest().encode('ascii') != expected_hash):
                raise GuardAuthError('Bundled Byte Guard failed its integrity check.')
            for item in self.auth.inventory():
                if item['name'].lower() == 'bytepanelguard' and item['filename'] != GUARD_JAR:
                    raise GuardAuthError('A duplicate or renamed Byte Guard companion (BytePanelGuard) was found. Remove it before starting.')
            path.parent.mkdir(exist_ok=True)
            installed_matches = (path.is_file() and path.stat().st_size == len(content)
                                 and digest(path).encode('ascii') == expected_hash)
            if path.exists() and not installed_matches:
                meta = descriptor(path)
                if not meta or meta['name'].lower() != 'bytepanelguard':
                    raise GuardAuthError('The reserved Guard filename contains an unexpected plugin. Review it manually.')
            if not installed_matches:
                atomic_private(path, content)
        # A replacement server never loads a former server's durable outage roster.
        scope_key = hashlib.sha256(self.security.store.scope().encode()).hexdigest()
        identity_mode = self.effective_config()['identity_mode']
        private_config = checked_path(self.data, 'connection-security/companions/' +
                                      scope_key + '/' + identity_mode + '/companion.json', missing=True)
        value = dict(endpoint='http://127.0.0.1:' + str(self.port), token=self._token(),
                     scope=self.security.store.scope(), identity_mode=identity_mode,
                     protocol_version=1, mode=config.get('mode', 'off'))
        atomic_private(private_config, json.dumps(value).encode())
        return ['-Dbytepanel.guard.config=' + str(private_config)]

    def reset(self):
        with self.token_lock:
            old_scope = self.token_scope
            self.guard.reset()
            self.token_scope = None
            self._ensure_token()
            new_scope = self.token_scope
        if old_scope and old_scope != new_scope:
            # Only our exact files in the revoked scope; never recurse through
            # arbitrary data or touch credentials/AuthMe account data.
            old_key = hashlib.sha256(old_scope.encode()).hexdigest()
            for identity_mode in ('online', 'offline'):
                relative = 'connection-security/companions/' + old_key + '/' + identity_mode
                try:
                    directory = checked_path(self.data, relative, missing=True)
                    if not directory.exists():
                        continue
                    for name in ('companion.json', 'companion-state.json', 'companion-initialized.json'):
                        checked_path(self.data, relative + '/' + name, missing=True).unlink(missing_ok=True)
                    if not any(directory.iterdir()):
                        directory.rmdir()
                except (OSError, GuardAuthError):
                    self.app.logger.warning('Revoked companion cache could not be removed. Review private security storage.')

    def protect_path(self, relative):
        normalized = str(relative).replace('\\', '/').strip('/').casefold()
        protected = ['plugins/' + GUARD_JAR]
        if self.enabled():
            protected += self.auth.protected_paths()
            # Losing the account database allows names to be registered again.
            protected.append('plugins/AuthMe')
        for target in protected:
            target = target.casefold()
            if normalized == target or target.startswith(normalized + '/') or normalized.startswith(target + '/'):
                abort(409, description='This path contains a managed security component. Use Byte Guard settings to change it.')

    def protect_content(self, relative, path):
        normalized = str(relative).replace('\\', '/').casefold()
        if not normalized.startswith('plugins/') or not normalized.endswith('.jar'):
            return
        item = descriptor(Path(path))
        if item and (item['name'].lower() == 'bytepanelguard' or (self.enabled() and item['name'].lower() in
                {'authme', 'authmereloaded', 'packetevents', 'nlogin', 'loginsecurity', 'librelogin',
                 'nexauth', 'authplus', 'auth+', 'openlogin', 'limboauth', 'fastlogin'})):
            abort(409, description='Use Byte Guard setup to install or repair authentication components. Conflicting authentication plugins cannot be installed while Guard is enabled.')

    def start_bridge(self):
        with self.start_lock:
            if self.httpd is not None:
                return
            runtime = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.1'

                def setup(self):
                    super().setup()
                    self.connection.settimeout(3)

                def log_message(self, *args):
                    pass  # Never log authentication headers or request bodies.

                def reply(self, status, value):
                    body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True

                def do_POST(self):
                    try:
                        supplied = self.headers.get('Authorization', '')
                        if not secrets.compare_digest(supplied, 'Bearer ' + runtime._token()):
                            self.reply(401, dict(error='Bridge authentication failed.'))
                            return
                        if self.headers.get('Transfer-Encoding'):
                            raise ValueError('Transfer encoding is not supported.')
                        length = int(self.headers.get('Content-Length', '0'))
                        if not 0 < length <= 65536:
                            raise ValueError('Invalid request size.')
                        value = json.loads(self.rfile.read(length))
                        if not isinstance(value, dict) or value.get('scope') != runtime.security.store.scope():
                            self.reply(409, dict(error='Server scope changed.'))
                            return
                        if self.path == '/v1/prelogin':
                            result = runtime.guard.prelogin(value)
                        elif self.path == '/v1/outcome':
                            result = runtime.guard.outcome(value)
                            runtime.on_outcome(result, value)
                        elif self.path == '/v1/safe-ip':
                            result = runtime.guard.save_session_safe_ip(value)
                        elif self.path == '/v1/snapshot':
                            result = runtime.guard.roster()
                        elif self.path == '/v1/heartbeat':
                            result = runtime.guard.heartbeat(value)
                        else:
                            self.reply(404, dict(error='Unknown bridge operation.'))
                            return
                        self.reply(200, result)
                    except GuardConflict:
                        self.reply(409, dict(error='Guard scope or policy changed; refresh the complete roster.'))
                    except (ValueError, TypeError, OverflowError):
                        self.reply(400, dict(error='Invalid or incompatible Guard request.'))
                    except socket.timeout:
                        self.close_connection = True
                    except OSError:
                        self.reply(503, dict(error='Guard storage is unavailable; admission is not authorized.'))
                    except Exception:
                        # No exception text: requests can contain sensitive metadata.
                        self.reply(503, dict(error='Byte Guard is temporarily unavailable.'))

                def do_GET(self):
                    try:
                        supplied = self.headers.get('Authorization', '')
                        if not secrets.compare_digest(supplied, 'Bearer ' + runtime._token()):
                            self.reply(401, dict(error='Bridge authentication failed.'))
                        elif self.path != '/v1/snapshot':
                            self.reply(404, dict(error='Unknown bridge operation.'))
                        else:
                            self.reply(200, runtime.guard.roster())
                    except Exception:
                        self.reply(503, dict(error='The complete security roster is unavailable.'))

            try:
                server = _BoundedServer(('127.0.0.1', self.port), Handler)
            except OSError:
                self.app.logger.exception('Could not bind Byte Guard private bridge. Guard-enabled starts are blocked.')
                return
            try:
                threading.Thread(target=server.serve_forever, name='connection-guard-bridge', daemon=True).start()
            except RuntimeError:
                server.server_close()
                self.app.logger.exception('Could not start Byte Guard private bridge. Guard-enabled starts are blocked.')
                return
            self.port, self.httpd = server.server_port, server

    def close(self):
        self.stop_event.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def maintenance(self):
        while not self.stop_event.wait(60):
            try:
                self.guard.maintenance()
            except Exception:
                self.app.logger.warning('Byte Guard maintenance failed; check its health screen.')

    def register_routes(self):
        bp = Blueprint('connection_security', __name__)
        runtime = self

        def payload():
            if request.content_length and request.content_length > 16384:
                abort(413, description='Guard request is too large.')
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                abort(400, description='Send a JSON object.')
            return data

        @bp.errorhandler(ValueError)
        def invalid(error):
            return jsonify(error=str(error), message=str(error)), 400

        @bp.errorhandler(HTTPException)
        def http_error(error):
            return jsonify(error=error.description), error.code

        @bp.errorhandler(OSError)
        @bp.errorhandler(sqlite3.Error)
        def storage(error):
            return jsonify(error='Security storage is unavailable. Check permissions and free space.'), 503

        @bp.route('/api/connection-security/settings', methods=['GET', 'PUT'])
        def settings():
            if request.method == 'GET':
                return jsonify(runtime.settings())
            values = payload()
            with runtime.mutation_guard():
                runtime.validate_activation(values)
                actual = runtime.effective_config()['identity_mode']
                if actual != runtime.guard.config()['identity_mode'] and values.get('confirm_identity_change') is not True:
                    raise GuardAuthError('The server identity mode changed. Explicitly confirm reconfiguration; player identities are never converted.')
                values['identity_mode'] = actual
                runtime.guard.save_settings(values, validate_credentials=True)
            return jsonify(runtime.settings())

        @bp.route('/api/connection-security/geoip/update', methods=['POST'])
        def geoip_update():
            runtime.guard.update_geoip()
            return jsonify(runtime.settings())

        @bp.route('/api/connection-security/history/preview')
        def history_preview():
            return jsonify(runtime.guard.history_preview(runtime.data / 'players.db'))

        @bp.route('/api/connection-security/history/import', methods=['POST'])
        def history_import():
            if payload().get('confirm') is not True:
                abort(400, description='Confirm that this history belongs to the current server.')
            with runtime.mutation_guard():
                return jsonify(runtime.guard.history_import(runtime.data / 'players.db'))

        @bp.route('/api/connection-security/auth')
        def auth_status():
            return jsonify(runtime.auth_status())

        @bp.route('/api/connection-security/auth/install', methods=['POST'])
        def auth_install():
            if payload().get('confirm') is not True:
                abort(400, description='Confirm installation of the verified authentication components.')
            with runtime.mutation_guard():
                return jsonify(runtime.auth.install())

        @bp.route('/api/connection-security/auth/repair', methods=['POST'])
        def auth_repair():
            if payload().get('confirm') is not True:
                abort(400, description='Confirm the authentication configuration repair.')
            with runtime.mutation_guard():
                return jsonify(runtime.auth.repair())

        @bp.route('/api/connection-security/events')
        def events():
            return jsonify(runtime.guard.get_events(limit=request.args.get('limit', 50),
                cursor=request.args.get('cursor'), status=request.args.get('status'),
                identity=request.args.get('identity')))

        @bp.route('/api/connection-security/players/<path:identity>')
        def profile(identity):
            return jsonify(runtime.guard.profile(identity))

        @bp.route('/api/connection-security/players/<path:identity>/safe-ips', methods=['POST', 'DELETE'])
        def safe_ips(identity):
            return jsonify(runtime.guard.manage_safe_ip(identity, payload().get('ip'),
                add=request.method == 'POST', actor=g.panel_user['username']))

        @bp.route('/api/connection-security/reviews/<attempt_id>', methods=['POST'])
        def review(attempt_id):
            values = payload()
            return jsonify(runtime.guard.review(attempt_id, values.get('action'), g.panel_user['username']))

        self.app.register_blueprint(bp)
