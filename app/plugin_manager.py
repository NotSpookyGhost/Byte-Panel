"""Authenticated plugin/data uploads and config editing for Paper/Purpur."""

import hashlib
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import zipfile

from flask import Blueprint, abort, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException


MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
TEXT_EXTENSIONS = frozenset({
    '.yml', '.yaml', '.json', '.toml', '.properties', '.conf', '.cfg',
    '.ini', '.txt', '.xml', '.csv', '.md', '.log',
})
SUPPORTED_ENGINES = ('paper', 'purpur')
_WRITE_LOCK = threading.RLock()


def register_plugin_manager(app, server_dir, data_dir, load_config, get_custom_assets):
    plugins = Blueprint('plugins', __name__)
    server_root = Path(server_dir).resolve()
    root = server_root / 'plugins'
    backup_root = Path(data_dir) / 'plugin-config-backups'

    # Flask 3.0 has no per-request setter. Bound multipart parsing without
    # imposing a small global limit on the panel's existing backup ZIP imports.
    class PluginRequest(app.request_class):
        @property
        def max_content_length(self):
            if self.path == '/api/plugins/upload':
                return MAX_UPLOAD_BYTES + 1024 * 1024
            if self.path == '/api/plugins/file':
                return 8 * MAX_CONFIG_BYTES  # JSON escaping can expand text.
            if self.path == '/api/plugins/delete':
                return 32 * 1024
            return super().max_content_length

    app.request_class = PluginRequest

    def checked_path(relative='', must_exist=True):
        """Reject traversal, absolute paths, links, and special filesystem nodes."""
        if not isinstance(relative, str) or len(relative) > 4096:
            abort(400, description='Invalid plugin path.')
        if '\\' in relative or ':' in relative or any(ord(c) < 32 for c in relative):
            abort(400, description='Invalid plugin path.')
        parts = relative.split('/') if relative else []
        if any(not p or p in ('.', '..') or p.startswith('.') for p in parts):
            abort(400, description='Use a relative path inside the plugins folder.')
        if root.is_symlink():
            abort(403, description='The plugins folder cannot be a symbolic link.')
        root.mkdir(exist_ok=True)
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                abort(403, description='Symbolic links cannot be accessed from the panel.')
        if not current.resolve().is_relative_to(root.resolve()):
            abort(403, description='Path is outside the plugins folder.')
        if current.exists():
            mode = current.stat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                abort(403, description='Only regular files and folders can be accessed.')
        elif must_exist:
            abort(404, description='Plugin file or folder not found. Refresh the file list.')
        return current

    def read_text(path):
        if not path.is_file():
            abort(400, description='Select a file to edit.')
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            abort(415, description='This is a binary or unsupported file. Download it to view locally.')
        with path.open('rb') as handle:
            content = handle.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            abort(413, description='The editor supports config files up to 1 MiB.')
        validate_text(content)
        return content

    def validate_text(content):
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            abort(415, description='Only UTF-8 text files can be edited or uploaded as configs.')
        if any(ord(c) < 32 and c not in '\t\r\n' for c in text):
            abort(415, description='Binary files cannot be edited as configs.')
        return text

    def digest(path):
        result = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(65536), b''):
                result.update(chunk)
        return result.hexdigest()

    def backup_file(path):
        backup_root.mkdir(parents=True, exist_ok=True)
        relative = path.relative_to(root).as_posix()
        prefix = hashlib.sha256(relative.encode('utf-8')).hexdigest()[:12] + '-'
        short_name = path.name.encode('utf-8')[:80].decode('utf-8', errors='ignore')
        fd, name = tempfile.mkstemp(prefix=prefix, suffix='-' + short_name + '.bak', dir=backup_root)
        with os.fdopen(fd, 'wb') as target, path.open('rb') as source:
            for chunk in iter(lambda: source.read(65536), b''):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        return Path(name).name

    @plugins.before_request
    def require_plugin_access():
        config = load_config()
        if not session.get('logged_in'):
            abort(401, description='Log in to manage plugins.')
        if not config.get('is_setup'):
            abort(403, description='Finish server setup before managing plugins.')
        if str(config.get('server_type') or '').lower() not in SUPPORTED_ENGINES:
            abort(403, description='Plugin management is available for Paper and Purpur servers only.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-Plugin-CSRF-Token', '')
            expected = session.get('plugin_csrf_token', '')
            if not expected or not secrets.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
                abort(403, description='Your session changed. Refresh the Plugins page and try again.')

    @plugins.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @plugins.errorhandler(OSError)
    def filesystem_error(error):
        app.logger.exception('Plugin file operation failed')
        return jsonify(error='Unable to access the plugin file. Check directory permissions and disk space.'), 500

    @plugins.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @plugins.route('/plugins')
    def page():
        session.setdefault('plugin_csrf_token', secrets.token_hex(32))
        return render_template(
            'plugins.html', config=load_config(), has_custom=get_custom_assets(),
            plugin_csrf_token=session['plugin_csrf_token'],
        )

    @plugins.route('/api/plugins/files')
    def list_files():
        relative = request.args.get('path', '')
        directory = checked_path(relative)
        if not directory.is_dir():
            abort(400, description='Select a folder to browse.')
        entries = []
        for entry in directory.iterdir():
            if entry.name.startswith('.') or entry.is_symlink():
                continue
            mode = entry.stat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                continue
            info = entry.stat()
            folder = entry.is_dir()
            entries.append(dict(
                name=entry.name, path=entry.relative_to(root).as_posix(),
                kind='folder' if folder else 'file', size=0 if folder else info.st_size,
                modified=info.st_mtime,
                editable=not folder and entry.suffix.lower() in TEXT_EXTENSIONS
                and info.st_size <= MAX_CONFIG_BYTES,
            ))
        entries.sort(key=lambda entry: (entry['kind'] != 'folder', entry['name'].lower()))
        return jsonify(path=relative, entries=entries)

    @plugins.route('/api/plugins/file', methods=['GET', 'PUT'])
    def config_file():
        if request.method == 'GET':
            relative = request.args.get('path', '')
            content = read_text(checked_path(relative))
            return jsonify(path=relative, content=content.decode('utf-8'),
                           revision=hashlib.sha256(content).hexdigest())
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get('content'), str):
            abort(400, description='Provide the config text and its original revision.')
        try:
            content = payload['content'].encode('utf-8')
        except UnicodeEncodeError:
            abort(400, description='Config contents must be valid UTF-8 text.')
        if len(content) > MAX_CONFIG_BYTES:
            abort(413, description='The editor supports config files up to 1 MiB.')
        validate_text(content)
        with _WRITE_LOCK:
            path = checked_path(payload.get('path'))
            original = read_text(path)
            if payload.get('revision') != hashlib.sha256(original).hexdigest():
                abort(409, description='This config changed since you opened it. Reload it before saving.')
            backup = backup_file(path)
            fd, temp_name = tempfile.mkstemp(prefix='.panel-edit-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp_name, stat.S_IMODE(path.stat().st_mode))
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        return jsonify(message='Config saved. Restart the server to apply changes.',
                       revision=hashlib.sha256(content).hexdigest(), backup=backup)

    @plugins.route('/api/plugins/upload', methods=['POST'])
    def upload():
        file = request.files.get('file')
        if not file or not file.filename:
            abort(400, description='Choose a plugin JAR, config or data file to upload.')
        filename = file.filename
        if '/' in filename or '\\' in filename or ':' in filename:
            abort(400, description='Upload a filename without a folder path.')
        folder = checked_path(request.form.get('path', ''))
        if not folder.is_dir():
            abort(400, description='Upload destination must be a folder.')
        relative = (folder / filename).relative_to(root).as_posix()
        target = checked_path(relative, must_exist=False)
        is_jar = target.suffix.lower() == '.jar'
        if is_jar and folder != root:
            abort(400, description='Upload plugin JARs into the root plugins folder.')
        # Plugins consume many opaque formats (.json.gz, .schem, .dat, etc.).
        # Store these unchanged in an existing plugin subfolder, never extract
        # archives or execute/import their contents. Root uploads stay restricted.
        is_data_file = not is_jar and folder != root
        if not is_jar and not is_data_file and target.suffix.lower() not in TEXT_EXTENSIONS:
            abort(415, description='Open a plugin subfolder to upload data files. The root accepts plugin JARs and supported text configs.')
        limit = MAX_UPLOAD_BYTES if is_jar or is_data_file else MAX_CONFIG_BYTES
        fd, temp_name = tempfile.mkstemp(prefix='.panel-upload-', dir=folder)
        try:
            size = 0
            with os.fdopen(fd, 'wb') as handle:
                while True:
                    chunk = file.stream.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        abort(413, description='JARs and files in plugin subfolders are limited to 64 MiB; root text configs to 1 MiB.')
                    handle.write(chunk)
            if is_jar:
                try:
                    with zipfile.ZipFile(temp_name) as jar:
                        if not {'plugin.yml', 'paper-plugin.yml'}.intersection(jar.namelist()):
                            abort(400, description='This JAR has no plugin.yml or paper-plugin.yml descriptor.')
                except zipfile.BadZipFile:
                    abort(400, description='The uploaded JAR is not a valid Java archive.')
            elif not is_data_file:
                validate_text(Path(temp_name).read_bytes())
            backup = None
            with _WRITE_LOCK:
                target = checked_path(relative, must_exist=False)
                if target.exists():
                    if not target.is_file():
                        abort(409, description='A folder already uses that filename.')
                    revision = digest(target)
                    if request.form.get('replace_revision') != revision:
                        return jsonify(error='A file with this name already exists.',
                                       conflict=True, revision=revision), 409
                    backup = backup_file(target)
                elif request.form.get('replace_revision'):
                    abort(409, description='The existing file was removed. Refresh and upload again.')
                os.chmod(temp_name, stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644)
                os.replace(temp_name, target)
            message = ('File uploaded. Follow the plugin import/reload instructions; data is not imported automatically.'
                       if is_data_file else 'File uploaded. Restart the server to apply changes.')
            return jsonify(message=message,
                           path=relative, backup=backup), 201
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @plugins.route('/api/plugins/delete', methods=['GET', 'DELETE'])
    def delete_plugin():
        # GET previews the exact JAR being confirmed; DELETE requires that revision.
        payload = request.args if request.method == 'GET' else request.get_json(silent=True)
        if payload is None or not hasattr(payload, 'get'):
            abort(400, description='Provide the plugin path and its confirmed revision.')
        relative = payload.get('path')
        with _WRITE_LOCK:
            path = checked_path(relative)
            if not path.is_file() or path.suffix.lower() != '.jar':
                abort(400, description='Only plugin JAR files can be deleted. Configs and folders are kept.')
            revision = digest(path)
            if request.method == 'GET':
                return jsonify(path=relative, name=path.name, revision=revision)
            if payload.get('revision') != revision:
                abort(409, description='This JAR changed or was not confirmed. Refresh and confirm deletion again.')
            backup = backup_file(path)
            # Keep the original if backup creation fails or an external update happens.
            path = checked_path(relative)
            if digest(backup_root / backup) != revision or digest(path) != revision:
                abort(409, description='This JAR changed during deletion. It was not deleted. Refresh and try again.')
            path.unlink()
        return jsonify(path=relative, backup=backup,
                       message='Plugin JAR deleted. Its config folders were kept. Restart the server to unload it.')

    @plugins.route('/api/plugins/download')
    def download():
        path = checked_path(request.args.get('path', ''))
        if not path.is_file():
            abort(400, description='Select a file to download.')
        return send_file(path, as_attachment=True, download_name=path.name,
                         mimetype='application/octet-stream')

    app.register_blueprint(plugins)
