"""Authenticated plugin/mod uploads and bounded add-on config editing."""

from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import zipfile
import zlib

from flask import Blueprint, abort, g, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

from server_files import _directory, _name, _parts


MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_DESCRIPTOR_BYTES = 1024 * 1024
TEXT_EXTENSIONS = frozenset({
    '.yml', '.yaml', '.json', '.toml', '.properties', '.conf', '.cfg',
    '.ini', '.txt', '.xml', '.csv', '.md', '.log',
})
SUPPORTED_ENGINES = ('paper', 'purpur', 'forge', 'fabric')
_WRITE_LOCK = threading.RLock()


def register_plugin_manager(app, server_dir, data_dir, load_config, get_custom_assets, mutation_guard=None):
    plugins = Blueprint('plugins', __name__)
    server_root = Path(server_dir).resolve()
    mutation_guard = mutation_guard or nullcontext

    def active_root():
        return server_root / g.addon_root_name

    def active_backup_root():
        return Path(data_dir) / (g.addon_kind + '-config-backups')

    # Flask 3.0 has no per-request setter. Bound multipart parsing without
    # imposing a small global limit on the panel's existing backup ZIP imports.
    class PluginRequest(app.request_class):
        @property
        def max_content_length(self):
            if self.path == '/api/plugins/upload':
                return MAX_UPLOAD_BYTES + 1024 * 1024
            if self.path == '/api/plugins/file':
                return 8 * MAX_CONFIG_BYTES  # JSON escaping can expand text.
            if self.path in ('/api/plugins/delete', '/api/plugins/create'):
                return 32 * 1024
            return super().max_content_length

    app.request_class = PluginRequest

    def checked_path(relative='', must_exist=True):
        """Reject traversal, absolute paths, links, and special filesystem nodes."""
        root = active_root()
        if not isinstance(relative, str) or len(relative) > 4096:
            abort(400, description='Invalid add-on path.')
        if '\\' in relative or ':' in relative or any(ord(c) < 32 for c in relative):
            abort(400, description='Invalid add-on path.')
        parts = relative.split('/') if relative else []
        if any(not p or p in ('.', '..') or p.startswith('.') for p in parts):
            abort(400, description=f'Use a relative path inside the {g.addon_root_name} folder.')
        if root.is_symlink():
            abort(403, description=f'The {g.addon_root_name} folder cannot be a symbolic link.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            root.mkdir(exist_ok=True)
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                abort(403, description='Symbolic links cannot be accessed from the panel.')
        if not current.resolve().is_relative_to(root.resolve()):
            abort(403, description=f'Path is outside the {g.addon_root_name} folder.')
        if current.exists():
            mode = current.stat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                abort(403, description='Only regular files and folders can be accessed.')
        elif must_exist:
            abort(404, description='Add-on file or folder not found. Refresh the file list.')
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
        root = active_root()
        backup_root = active_backup_root()
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
        if not session.get('logged_in'):
            abort(401, description='Log in to manage plugins or mods.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            guard = mutation_guard()
            guard.__enter__()
            g.plugin_mutation_guard = guard
        config = load_config()
        if not config.get('is_setup'):
            abort(403, description='Finish server setup before managing add-ons.')
        g.addon_engine = str(config.get('server_type') or '').lower()
        if g.addon_engine not in SUPPORTED_ENGINES:
            abort(403, description='Add-on management requires Paper, Purpur, Forge or Fabric.')
        g.addon_kind = 'mod' if g.addon_engine in ('forge', 'fabric') else 'plugin'
        g.addon_root_name = g.addon_kind + 's'
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-Plugin-CSRF-Token', '')
            expected = session.get('plugin_csrf_token', '')
            if not expected or not secrets.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
                abort(403, description='Your session changed. Refresh the add-ons page and try again.')

    @plugins.teardown_request
    def release_mutation_guard(error):
        guard = g.pop('plugin_mutation_guard', None)
        if guard is not None:
            guard.__exit__(type(error) if error else None, error,
                           error.__traceback__ if error else None)

    @plugins.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @plugins.errorhandler(OSError)
    def filesystem_error(error):
        app.logger.exception('Add-on file operation failed')
        return jsonify(error='Unable to access the add-on file. Check directory permissions and disk space.'), 500

    @plugins.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @plugins.route('/mods')
    @plugins.route('/plugins')
    def page():
        session.setdefault('plugin_csrf_token', secrets.token_hex(32))
        return render_template(
            'plugins.html', config=load_config(), has_custom=get_custom_assets(),
            plugin_csrf_token=session['plugin_csrf_token'],
            addon_kind=g.addon_kind, root_name=g.addon_root_name,
            addon_title=g.addon_root_name.capitalize(),
        )

    @plugins.route('/api/plugins/files')
    def list_files():
        root = active_root()
        relative = request.args.get('path', '')
        if not relative and not root.exists() and not root.is_symlink():
            return jsonify(path='', entries=[])
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

    @plugins.route('/api/plugins/create', methods=['POST'])
    def create_config():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            abort(400, description='Provide the new config filename and its folder.')
        name = _name(payload.get('name'))
        folder = payload.get('path', '')
        parts = _parts(folder)
        if name.startswith('.'):
            abort(400, description='Enter a filename that does not start with a dot.')
        if Path(name).suffix.lower() not in TEXT_EXTENSIONS:
            abort(415, description='Choose a supported config extension such as .yml, .json, .toml, or .txt.')
        with _WRITE_LOCK:
            if not checked_path(folder).is_dir():
                abort(400, description='Select a folder for the new config.')
            with _directory(server_root, (g.addon_root_name, *parts)) as parent:
                try:
                    # Exclusive creation prevents overwriting an existing entry.
                    with parent.create(name, mode=0o644):
                        pass
                except FileExistsError:
                    abort(409, description='A file or folder already uses that name.')
        return jsonify(path='/'.join((*parts, name)), revision=hashlib.sha256(b'').hexdigest(),
                       message='Config file created.'), 201

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
        root = active_root()
        file = request.files.get('file')
        if not file or not file.filename:
            abort(400, description=f'Choose a {g.addon_kind} JAR, config or data file to upload.')
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
            abort(400, description=f'Upload {g.addon_kind} JARs into the root {g.addon_root_name} folder.')
        # Plugins consume many opaque formats (.json.gz, .schem, .dat, etc.).
        # Store these unchanged in an existing plugin subfolder, never extract
        # archives or execute/import their contents. Root uploads stay restricted.
        is_data_file = not is_jar and folder != root
        if not is_jar and not is_data_file and target.suffix.lower() not in TEXT_EXTENSIONS:
            abort(415, description=f'Open a {g.addon_kind} subfolder to upload data files. The root accepts {g.addon_kind} JARs and supported text configs.')
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
                        abort(413, description='JARs and files in add-on subfolders are limited to 64 MiB; root text configs to 1 MiB.')
                    handle.write(chunk)
            if is_jar:
                try:
                    with zipfile.ZipFile(temp_name) as jar:
                        if g.addon_kind == 'plugin':
                            if not {'plugin.yml', 'paper-plugin.yml'}.intersection(jar.namelist()):
                                abort(400, description='This JAR has no plugin.yml or paper-plugin.yml descriptor.')
                        else:
                            descriptors = ('fabric.mod.json',) if g.addon_engine == 'fabric' else ('META-INF/mods.toml', 'mcmod.info')
                            descriptor = next((name for name in descriptors if name in jar.namelist()), None)
                            if descriptor is None:
                                abort(400, description=f'This is not a {g.addon_engine.capitalize()} mod JAR. Expected ' + ' or '.join(descriptors) + '.')
                            info = jar.getinfo(descriptor)
                            if (info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or
                                    info.flag_bits & 1 or not 0 < info.file_size <= MAX_DESCRIPTOR_BYTES):
                                abort(400, description='Mod metadata must be an unencrypted text file up to 1 MiB.')
                            # Inspect only this bounded descriptor, never extract or load
                            # mod code. Minecraft/version/dependency compatibility is
                            # still the server operator's responsibility.
                            with jar.open(info) as metadata:
                                content = metadata.read(MAX_DESCRIPTOR_BYTES + 1)
                            if len(content) > MAX_DESCRIPTOR_BYTES:
                                abort(400, description='Mod metadata exceeds the 1 MiB limit.')
                            try:
                                descriptor_text = content.decode('utf-8-sig')
                            except UnicodeDecodeError:
                                abort(400, description='Mod metadata must be valid UTF-8 text.')
                            if not descriptor_text.strip() or '\x00' in descriptor_text:
                                abort(400, description='Mod metadata must contain readable text.')
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
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
            message = (f'File uploaded. Follow the {g.addon_kind} import/reload instructions; data is not imported automatically.'
                       if is_data_file else 'File uploaded. Restart the server to apply changes.')
            return jsonify(message=message,
                           path=relative, backup=backup), 201
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @plugins.route('/api/plugins/delete', methods=['GET', 'DELETE'])
    def delete_plugin():
        backup_root = active_backup_root()
        # GET previews the exact JAR being confirmed; DELETE requires that revision.
        payload = request.args if request.method == 'GET' else request.get_json(silent=True)
        if payload is None or not hasattr(payload, 'get'):
            abort(400, description='Provide the add-on path and its confirmed revision.')
        relative = payload.get('path')
        with _WRITE_LOCK:
            path = checked_path(relative)
            if not path.is_file() or path.suffix.lower() != '.jar':
                abort(400, description=f'Only {g.addon_kind} JAR files can be deleted. Configs and folders are kept.')
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
                       message=f'{g.addon_kind.capitalize()} JAR deleted. Its config folders were kept. Restart the server to unload it.')

    @plugins.route('/api/plugins/download')
    def download():
        path = checked_path(request.args.get('path', ''))
        if not path.is_file():
            abort(400, description='Select a file to download.')
        return send_file(path, as_attachment=True, download_name=path.name,
                         mimetype='application/octet-stream')

    app.register_blueprint(plugins)
