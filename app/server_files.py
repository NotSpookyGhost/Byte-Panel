"""Offline-only, recoverable server file operations; never exposes panel data."""

from contextlib import contextmanager
import codecs
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import stat
import threading

from flask import Blueprint, abort, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

from server_backups import BackupBusy


MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 5000
MAX_TREE_ENTRIES = 100000
_CHUNK = 65536
_DIR_FDS = (os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and
            os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)


def _parts(relative, allow_root=True):
    if not isinstance(relative, str) or len(relative) > 4096:
        abort(400, description='Provide a valid relative server path.')
    if '\\' in relative or ':' in relative or any(ord(char) < 32 or ord(char) == 127 for char in relative):
        abort(400, description='Invalid characters in server path.')
    parts = relative.split('/') if relative else []
    if len(parts) > 128:
        abort(413, description='This path is too deeply nested to safely manage.')
    if not parts and not allow_root:
        abort(400, description='The server root cannot be changed. Select a file or folder.')
    for part in parts:
        try:
            encoded = part.encode('utf-8')
        except UnicodeEncodeError:
            abort(400, description='Filenames must contain valid UTF-8 characters.')
        if (not part or part in ('.', '..') or part.lower().startswith('.panel-') or
                part.endswith((' ', '.')) or len(encoded) > 255):
            abort(400, description='Use a relative path without traversal or reserved .panel- names.')
        # Windows aliases and devices must not bypass the same checks used on Linux.
        base = part.split('.')[0].upper()
        if base in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                   *(f'LPT{i}' for i in range(1, 10))}:
            abort(400, description='Reserved device names are not supported.')
    return parts


def _name(value):
    parts = _parts(value, allow_root=False)
    if len(parts) != 1:
        abort(400, description='Enter a filename without folders.')
    return parts[0]


def _safe_node(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        abort(403, description='Symbolic links and filesystem junctions cannot be accessed.')
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        abort(403, description='Only regular files and folders can be accessed.')
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        abort(403, description='Hard-linked files cannot be accessed from the panel.')
    return info


def _same_node(first, second):
    return (first.st_dev, first.st_ino, stat.S_IFMT(first.st_mode)) == (
        second.st_dev, second.st_ino, stat.S_IFMT(second.st_mode))


class _Directory:
    """Pinned directory handles on Linux; rechecked native paths on Windows."""

    def __init__(self, root, parts=(), fd=None):
        self.root, self.parts, self.fd = root, tuple(parts), fd
        self.path = root.joinpath(*parts)

    def check(self):
        if self.fd is not None:
            _safe_node(os.fstat(self.fd))
            return
        current = self.root
        for part in (None, *self.parts):
            if part is not None:
                current /= part
            info = _safe_node(current.lstat())
            if not stat.S_ISDIR(info.st_mode):
                abort(400, description='Select an existing folder.')

    def info(self, name, missing=False):
        self.check()
        try:
            info = (os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                    if self.fd is not None else (self.path / name).lstat())
        except FileNotFoundError:
            if missing:
                return None
            raise
        return _safe_node(info)

    @contextmanager
    def child(self, name):
        expected = self.info(name)
        if not stat.S_ISDIR(expected.st_mode):
            abort(400, description='Select an existing folder.')
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=self.fd) if self.fd is not None else None
        child = _Directory(self.root, (*self.parts, name), fd)
        try:
            child.check()
            actual = os.fstat(fd) if fd is not None else child.path.lstat()
            if not _same_node(expected, actual):
                abort(409, description='The folder changed. Refresh and retry.')
            yield child
        finally:
            if fd is not None:
                os.close(fd)

    @contextmanager
    def read(self, name):
        expected = self.info(name)
        if not stat.S_ISREG(expected.st_mode):
            abort(400, description='Select a regular file.')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        fd = (os.open(name, flags, dir_fd=self.fd) if self.fd is not None
              else os.open(self.path / name, flags))
        with os.fdopen(fd, 'rb') as handle:
            actual = _safe_node(os.fstat(handle.fileno()))
            if not _same_node(expected, actual):
                abort(409, description='The file changed. Refresh and retry.')
            yield handle

    def create(self, name, mode=0o600):
        self.check()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        fd = (os.open(name, flags, mode, dir_fd=self.fd) if self.fd is not None
              else os.open(self.path / name, flags, mode))
        return os.fdopen(fd, 'wb')

    def mkdir(self, name):
        self.check()
        if self.fd is not None:
            os.mkdir(name, 0o700, dir_fd=self.fd)
        else:
            os.mkdir(self.path / name, 0o700)

    def unlink(self, name):
        self.check()
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            os.unlink(self.path / name)

    def names(self):
        self.check()
        # scandir is lazy, so callers can enforce a bound without materializing the tree.
        return os.scandir(self.fd if self.fd is not None else self.path)


@contextmanager
def _directory(root, parts=()):
    info = _safe_node(root.lstat())
    if not stat.S_ISDIR(info.st_mode):
        abort(403, description='The server data root must be a regular directory.')
    fd = os.open(root, _DIRECTORY_FLAGS) if _DIR_FDS else None
    directory = _Directory(root, fd=fd)
    try:
        directory.check()
        if fd is not None and not _same_node(info, os.fstat(fd)):
            abort(409, description='The server directory changed. Retry after checking its storage.')
        if parts:
            with directory.child(parts[0]) as child:
                with _descend(child, parts[1:]) as target:
                    yield target
        else:
            yield directory
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def _descend(directory, parts):
    if not parts:
        yield directory
    else:
        with directory.child(parts[0]) as child:
            with _descend(child, parts[1:]) as target:
                yield target


def _digest(directory, name):
    result = hashlib.sha256()
    with directory.read(name) as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(_CHUNK), b''):
            result.update(chunk)
        after = _safe_node(os.fstat(source.fileno()))
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        abort(409, description='The file changed while it was being read. Refresh and retry.')
    if not _same_node(after, directory.info(name)):
        abort(409, description='The file was replaced. Refresh and retry.')
    return result.hexdigest()


def _revision(directory, name):
    info = directory.info(name)
    if stat.S_ISREG(info.st_mode):
        return _digest(directory, name)
    digest = hashlib.sha256()
    count = [0]

    def visit(parent, child_name, relative, depth=0):
        count[0] += 1
        if count[0] > MAX_TREE_ENTRIES or depth > 128:
            abort(413, description='This folder is too large or deeply nested to safely change from the panel.')
        node = parent.info(child_name)
        digest.update(json.dumps([relative, node.st_mode, node.st_size, node.st_mtime_ns,
                                  node.st_ctime_ns, node.st_dev, node.st_ino],
                                 ensure_ascii=True, separators=(',', ':')).encode('utf-8'))
        if stat.S_ISDIR(node.st_mode):
            with parent.child(child_name) as child:
                names = []
                with child.names() as entries:
                    for entry in entries:
                        _name(entry.name)
                        names.append(entry.name)
                        if len(names) + count[0] > MAX_TREE_ENTRIES:
                            abort(413, description='This folder is too large to safely change from the panel.')
                for descendant in sorted(names):
                    visit(child, descendant, relative + '/' + descendant, depth + 1)
        current = parent.info(child_name)
        if (node.st_mtime_ns, node.st_ctime_ns, node.st_size, node.st_ino) != (
                current.st_mtime_ns, current.st_ctime_ns, current.st_size, current.st_ino):
            abort(409, description='The folder contents changed. Refresh and retry.')

    visit(directory, name, '')
    return digest.hexdigest()


def _validate_text(content):
    if len(content) > MAX_TEXT_BYTES:
        abort(413, description='The editor supports UTF-8 text files up to 1 MiB.')
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        abort(415, description='Only UTF-8 text can be edited. Download binary files instead.')
    if any(ord(char) < 32 and char not in '\t\r\n' for char in text):
        abort(415, description='Binary files cannot be edited as text.')
    return text


def _text_bytes(value):
    if not isinstance(value, str):
        abort(400, description='Provide text content as a string.')
    try:
        content = value.encode('utf-8')
    except UnicodeEncodeError:
        abort(400, description='Content must be valid UTF-8 text.')
    _validate_text(content)
    return content


def _editable(directory, name, info):
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TEXT_BYTES:
        return False
    with directory.read(name) as handle:
        sample = handle.read(8192)
    try:
        text = codecs.getincrementaldecoder('utf-8')().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return not any(ord(char) < 32 and char not in '\t\r\n' for char in text)


def _rename_noreplace(source, name, destination, new_name):
    """Never use POSIX rename's overwrite behavior for moves or trash publication."""
    source.check()
    destination.check()
    if destination.info(new_name, missing=True) is not None:
        abort(409, description='The destination already exists. Choose a different name.')
    if source.fd is not None and destination.fd is not None:
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            abort(501, description='This container lacks safe non-overwriting file operations. Rebuild the image with --pull and recreate the container, or manage files on the host.')
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(source.fd, os.fsencode(name), destination.fd, os.fsencode(new_name), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    elif os.name == 'nt':
        # Windows rename refuses an existing destination, including empty folders.
        os.rename(source.path / name, destination.path / new_name)
    else:
        abort(501, description='Safe non-overwriting moves are unavailable on this system.')


def _publish(directory, temporary, name, replace):
    directory.check()
    if replace:
        directory.info(name)
        if directory.fd is not None:
            os.replace(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        else:
            os.replace(directory.path / temporary, directory.path / name)
    else:
        _rename_noreplace(directory, temporary, directory, name)


def register_file_manager(app, server_dir, data_dir, load_config, get_assets, mutation_guard,
                          path_guard=None, content_guard=None):
    files = Blueprint('files', __name__)
    root, data_root = Path(os.path.abspath(server_dir)), Path(os.path.abspath(data_dir))
    if data_root == root or root in data_root.parents:
        raise ValueError('The server file root must not contain private panel data.')
    lock = threading.RLock()
    path_guard = path_guard or (lambda relative: None)
    content_guard = content_guard or (lambda relative, path: None)

    class FileRequest(app.request_class):
        @property
        def max_content_length(self):
            if self.path == '/api/files/upload':
                return MAX_UPLOAD_BYTES + 1024 * 1024
            if self.path in ('/api/files/file', '/api/files/create'):
                return 8 * MAX_TEXT_BYTES
            if self.path.startswith('/api/files/'):
                return 64 * 1024
            return super().max_content_length

    app.request_class = FileRequest

    @files.before_request
    def require_access():
        if not session.get('logged_in'):
            abort(401, description='Sign in to browse server files.')
        if not load_config().get('is_setup'):
            abort(403, description='Finish server setup before browsing files.')

    @files.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(status='error', error=error.description, message=error.description), error.code

    @files.errorhandler(BackupBusy)
    def busy_error(error):
        return jsonify(status='error', error=str(error), message=str(error)), 409

    @files.errorhandler(OSError)
    def filesystem_error(error):
        code = {errno.ENOENT: 404, errno.EEXIST: 409, errno.ENOTDIR: 400,
                errno.ELOOP: 403, errno.EACCES: 403, errno.EPERM: 403,
                errno.ENOSPC: 507, errno.EDQUOT: 507, errno.EXDEV: 409}.get(error.errno, 500)
        message = {404: 'File or folder not found. Refresh the list.',
                   409: 'The destination exists or cannot safely be moved across filesystems.',
                   400: 'Select an existing folder.', 403: 'Access denied or unsafe filesystem link.',
                   507: 'Not enough storage space to complete the operation.'}.get(
                       code, 'Unable to access server files. Check storage space and permissions.')
        if code == 500:
            app.logger.exception('Server file operation failed')
        return jsonify(status='error', error=message, message=message), code

    @files.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400, description='Provide a JSON object containing the file operation.')
        return value

    def confirmed(value, revision):
        if (not isinstance(value, str) or not value.isascii() or
                not secrets.compare_digest(value, revision)):
            abort(409, description='This entry changed or was not confirmed. Refresh and try again.')

    def entry(parent, name, relative):
        info = parent.info(name)
        folder = stat.S_ISDIR(info.st_mode)
        return dict(name=name, path=relative, kind='folder' if folder else 'file',
                    size=0 if folder else info.st_size, modified=info.st_mtime,
                    editable=_editable(parent, name, info))

    @contextmanager
    def recovery(kind, relative, revision, operation):
        category = 'file-manager-' + kind
        with _directory(data_root) as data:
            if data.info(category, missing=True) is None:
                try:
                    data.mkdir(category)
                except FileExistsError:
                    pass
            with data.child(category) as collection:
                identifier = secrets.token_hex(16)
                collection.mkdir(identifier)
                with collection.child(identifier) as saved:
                    metadata = dict(path=relative, revision=revision, operation=operation,
                                    created_at=datetime.now(timezone.utc).isoformat())
                    with saved.create('metadata.json') as handle:
                        handle.write(json.dumps(metadata, ensure_ascii=True, indent=2).encode('utf-8'))
                        handle.flush()
                        os.fsync(handle.fileno())
                    yield identifier, saved

    def snapshot(parent, name, relative, revision, operation):
        with recovery('backups', relative, revision, operation) as (identifier, saved):
            digest = hashlib.sha256()
            with parent.read(name) as source, saved.create('content') as target:
                for chunk in iter(lambda: source.read(_CHUNK), b''):
                    target.write(chunk)
                    digest.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            confirmed(digest.hexdigest(), revision)
            confirmed(_digest(parent, name), revision)
            return identifier

    def write_file(parent, name, relative, stream, limit, expected=None, operation='upload'):
        path_guard(relative)
        existing = parent.info(name, missing=True)
        recovery_id = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                abort(409, description='A folder already uses that name.')
            revision = _digest(parent, name)
            if expected != revision:
                return None, (jsonify(status='error', error='A file already exists or changed.',
                                     message='Confirm replacement of the current file.', conflict=True,
                                     revision=revision), 409)
            recovery_id = snapshot(parent, name, relative, revision, operation)
        elif expected is not None:
            abort(409, description='The original file was removed. Refresh before uploading again.')
        temporary = '.panel-file-' + secrets.token_hex(16)
        created = False
        digest, size = hashlib.sha256(), 0
        try:
            with parent.create(temporary) as target:
                created = True
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        abort(413, description='The file exceeds the allowed upload or editor limit.')
                    target.write(chunk)
                    digest.update(chunk)
                target.flush()
                os.fsync(target.fileno())
                if hasattr(os, 'fchmod'):
                    os.fchmod(target.fileno(), stat.S_IMODE(existing.st_mode) if existing else 0o644)
            if existing is not None:
                confirmed(_digest(parent, name), expected)
            content_guard(relative, root.joinpath(*_parts(relative)[:-1], temporary))
            _publish(parent, temporary, name, replace=existing is not None)
            created = False
        finally:
            if created:
                parent.unlink(temporary)
        result = dict(path=relative, revision=digest.hexdigest(), message='File saved. Restart or reload Minecraft when needed.')
        if recovery_id:
            result.update(recovery_id=recovery_id, recovery_kind='backups')
        return result, None

    @files.route('/files')
    def page():
        return render_template('files.html', config=load_config(), has_custom=get_assets())

    @files.route('/api/files/list')
    def list_files():
        relative = request.args.get('path', '')
        parts = _parts(relative)
        entries, skipped = [], 0
        with lock, _directory(root, parts) as directory:
            with directory.names() as children:
                for index, child in enumerate(children):
                    if index >= MAX_LIST_ENTRIES:
                        abort(413, description='This folder has too many entries to display safely.')
                    try:
                        _name(child.name)
                        entries.append(entry(directory, child.name, '/'.join((*parts, child.name))))
                    except HTTPException as error:
                        if error.code not in (400, 403):
                            raise
                        skipped += 1
        entries.sort(key=lambda item: (item['kind'] != 'folder', item['name'].casefold()))
        return jsonify(path=relative, parent='/'.join(parts[:-1]) if parts else None,
                       entries=entries, skipped=skipped)

    @files.route('/api/files/file', methods=['GET', 'PUT'])
    def file():
        if request.method == 'GET':
            relative = request.args.get('path', '')
            parts = _parts(relative, allow_root=False)
            with lock, _directory(root, parts[:-1]) as parent:
                with parent.read(parts[-1]) as source:
                    content = source.read(MAX_TEXT_BYTES + 1)
                text = _validate_text(content)
                revision = hashlib.sha256(content).hexdigest()
                confirmed(_digest(parent, parts[-1]), revision)
            return jsonify(path=relative, content=text, revision=revision)
        values = payload()
        relative = values.get('path')
        parts = _parts(relative, allow_root=False)
        content = _text_bytes(values.get('content'))
        with mutation_guard(), lock, _directory(root, parts[:-1]) as parent:
            # A binary file may be replaced by an explicitly confirmed upload,
            # but cannot be silently converted by the text editor.
            with parent.read(parts[-1]) as source:
                original = source.read(MAX_TEXT_BYTES + 1)
            _validate_text(original)
            confirmed(values.get('revision'), hashlib.sha256(original).hexdigest())
            result, error = write_file(parent, parts[-1], relative, io.BytesIO(content),
                                       MAX_TEXT_BYTES, values['revision'], 'edit')
        return error or jsonify(result)

    @files.route('/api/files/upload', methods=['POST'])
    def upload():
        with mutation_guard(), lock:
            uploaded = request.files.getlist('file')
            if len(uploaded) != 1 or len(request.files) != 1 or not uploaded[0].filename:
                abort(400, description='Select exactly one file to upload.')
            item = uploaded[0]
            name = _name(item.filename)
            parts = _parts(request.form.get('path', ''))
            relative = '/'.join((*parts, name))
            with _directory(root, parts) as parent:
                result, error = write_file(parent, name, relative, item.stream, MAX_UPLOAD_BYTES,
                                           request.form.get('replace_revision'), 'upload')
        return error or (jsonify(result), 201)

    @files.route('/api/files/mkdir', methods=['POST'])
    def mkdir():
        values = payload()
        parts, name = _parts(values.get('path', '')), _name(values.get('name'))
        relative = '/'.join((*parts, name))
        with mutation_guard(), lock, _directory(root, parts) as parent:
            path_guard(relative)
            if parent.info(name, missing=True) is not None:
                abort(409, description='A file or folder already uses that name.')
            parent.mkdir(name)
            revision = _revision(parent, name)
        return jsonify(path=relative, revision=revision, message='Folder created.'), 201

    @files.route('/api/files/create', methods=['POST'])
    def create():
        values = payload()
        parts, name = _parts(values.get('path', '')), _name(values.get('name'))
        content = _text_bytes(values.get('content', ''))
        relative = '/'.join((*parts, name))
        with mutation_guard(), lock, _directory(root, parts) as parent:
            result, error = write_file(parent, name, relative, io.BytesIO(content), MAX_TEXT_BYTES,
                                       operation='create')
        return error or (jsonify(result), 201)

    @files.route('/api/files/move', methods=['POST'])
    def move():
        values = payload()
        source_path, destination_path = values.get('path'), values.get('destination')
        source_parts = _parts(source_path, allow_root=False)
        destination_parts = _parts(destination_path, allow_root=False)
        if (source_parts == destination_parts or
                destination_parts[:len(source_parts)] == source_parts):
            abort(400, description='Choose a different destination outside the selected folder.')
        with mutation_guard(), lock, _directory(root, source_parts[:-1]) as source:
            path_guard(source_path)
            path_guard(destination_path)
            content_guard(destination_path, root.joinpath(*source_parts))
            with _directory(root, destination_parts[:-1]) as destination:
                revision = _revision(source, source_parts[-1])
                confirmed(values.get('revision'), revision)
                if destination.info(destination_parts[-1], missing=True) is not None:
                    abort(409, description='The destination already exists. Choose a different name.')
                confirmed(_revision(source, source_parts[-1]), revision)
                _rename_noreplace(source, source_parts[-1], destination, destination_parts[-1])
                revision = _revision(destination, destination_parts[-1])
        return jsonify(path=destination_path, revision=revision, message='Entry moved.')

    @files.route('/api/files/entry', methods=['GET', 'DELETE'])
    def entry_details():
        values = request.args if request.method == 'GET' else payload()
        relative = values.get('path', '')
        parts = _parts(relative, allow_root=False)
        if request.method == 'GET':
            with lock, _directory(root, parts[:-1]) as parent:
                result = entry(parent, parts[-1], relative)
                result['revision'] = _revision(parent, parts[-1])
            return jsonify(result)
        if values.get('confirmation') != relative:
            abort(400, description='Confirm deletion by entering the exact relative path.')
        with mutation_guard(), lock, _directory(root, parts[:-1]) as parent:
            path_guard(relative)
            revision = _revision(parent, parts[-1])
            confirmed(values.get('revision'), revision)
            with recovery('trash', relative, revision, 'delete') as (identifier, saved):
                confirmed(_revision(parent, parts[-1]), revision)
                _rename_noreplace(parent, parts[-1], saved, 'content')
        return jsonify(path=relative, revision=revision, recovery_id=identifier, recovery_kind='trash',
                       message='Moved to private file-manager trash. Restore it from the data volume if needed.')

    @files.route('/api/files/download')
    def download():
        parts = _parts(request.args.get('path', ''), allow_root=False)
        with lock, _directory(root, parts[:-1]) as parent:
            with parent.read(parts[-1]) as original:
                stream = os.fdopen(os.dup(original.fileno()), 'rb')
                size = os.fstat(original.fileno()).st_size
        try:
            response = send_file(stream, as_attachment=True, download_name=parts[-1],
                                 mimetype='application/octet-stream', conditional=False)
            response.content_length = size
            response.call_on_close(stream.close)
            return response
        except BaseException:
            stream.close()
            raise

    app.register_blueprint(files)

