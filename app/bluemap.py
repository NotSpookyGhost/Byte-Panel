"""Read-only same-origin proxy for BlueMap running inside this server container."""
import re
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
from urllib3.exceptions import HTTPError
from flask import Blueprint, Response, current_app, redirect, render_template, request


MAP_ENDPOINTS = frozenset(('bluemap.entry', 'bluemap.proxy'))
REQUEST_HEADERS = ('Accept', 'Accept-Encoding', 'If-None-Match', 'If-Modified-Since',
                   'Range', 'If-Range', 'Last-Event-ID')
RESPONSE_HEADERS = ('Content-Type', 'Content-Length', 'Content-Encoding', 'ETag',
                    'Last-Modified', 'Accept-Ranges', 'Content-Range', 'Vary', 'Expires')


def checked_port(value):
    if not re.fullmatch(r'[0-9]{4,5}', str(value)) or not 1024 <= int(value) <= 65535 or int(value) == 5000:
        raise ValueError('BlueMap port must be 1024–65535, excluding the panel port 5000.')
    return int(value)


def safe_path(path):
    """Reject traversal and ambiguous encodings without changing valid asset names."""
    checked = path
    for _ in range(5):
        if (checked.startswith('/') or '\\' in checked or ':' in checked or
                any(ord(char) < 32 or ord(char) == 127 for char in checked) or
                any(part in ('.', '..') for part in checked.split('/'))):
            raise ValueError('Invalid map path.')
        decoded = unquote(checked)
        if decoded == checked:
            return path
        checked = decoded
    raise ValueError('Invalid map path encoding.')


def register_bluemap(app, load_config):
    blueprint = Blueprint('bluemap', __name__)

    def unavailable(message, status):
        return render_template('map_unavailable.html', message=message), status

    def enabled_config():
        config = load_config()
        return config if config.get('is_setup') and config.get('bluemap_enabled') is True else None

    @blueprint.route('/map', methods=['GET', 'HEAD'], provide_automatic_options=False)
    def entry():
        if not enabled_config():
            return unavailable('The public map is not enabled for this server.', 404)
        query = request.query_string.decode('latin-1')
        return redirect('/map/' + ('?' + query if query else ''), code=308)

    @blueprint.route('/map/', defaults={'asset_path': ''}, methods=['GET', 'HEAD'], provide_automatic_options=False)
    @blueprint.route('/map/<path:asset_path>', methods=['GET', 'HEAD'], provide_automatic_options=False)
    def proxy(asset_path):
        config = enabled_config()
        if config is None:
            return unavailable('The public map is not enabled for this server.', 404)
        try:
            port = checked_port(config.get('bluemap_port', 8100))
        except ValueError:
            return unavailable('The server owner needs to check the BlueMap port in Panel Settings.', 503)
        try:
            safe_path(asset_path)
        except ValueError:
            return unavailable('Invalid map path.', 400)
        origin = f'http://127.0.0.1:{port}'
        url = origin + '/' + quote(asset_path, safe='/')
        if request.query_string:
            url += '?' + request.query_string.decode('latin-1')
        # Never send panel cookies, credentials, CSRF tokens, forwarding headers,
        # environment HTTP proxies or .netrc credentials to the plugin webserver.
        client = requests.Session()
        client.trust_env = False
        upstream = None
        logger = current_app.logger

        def close():
            if upstream is not None:
                upstream.close()
            client.close()

        try:
            upstream = client.request(request.method, url,
                headers={name: request.headers[name] for name in REQUEST_HEADERS if name in request.headers},
                stream=True, allow_redirects=False, timeout=(3, 90))
        except ValueError:
            close()
            return unavailable('BlueMap returned an unsupported redirect.', 502)
        except requests.Timeout:
            close()
            return unavailable('BlueMap took too long to respond. Please try again shortly.', 504)
        except requests.RequestException:
            close()
            return unavailable('BlueMap is not available. The server owner should check that Minecraft and BlueMap are running, with BlueMap\'s webserver enabled on the configured port.', 503)

        headers = {name: upstream.headers[name] for name in RESPONSE_HEADERS if name in upstream.headers}
        connection_headers = {name.strip().lower() for name in upstream.headers.get('Connection', '').split(',')}
        headers = {name: value for name, value in headers.items() if name.lower() not in connection_headers}
        # Maps are public when enabled but must not remain in shared caches after
        # disabling access. These headers also prevent SSE buffering at proxies.
        headers.update({'Cache-Control': 'no-store', 'CDN-Cache-Control': 'no-store',
                        'Cloudflare-CDN-Cache-Control': 'no-store', 'Surrogate-Control': 'no-store'})
        if 'Location' in upstream.headers and 300 <= upstream.status_code < 400:
            try:
                target = urlsplit(urljoin(url, upstream.headers['Location']))
                if (target.scheme != 'http' or target.hostname != '127.0.0.1' or target.port != port or
                        target.username is not None or target.password is not None):
                    raise ValueError('External redirect')
                safe_path(target.path.lstrip('/'))
            except ValueError:
                close()
                return unavailable('BlueMap returned an unsupported redirect.', 502)
            headers['Location'] = '/map/' + target.path.lstrip('/')
            if target.query:
                headers['Location'] += '?' + target.query
            if target.fragment:
                headers['Location'] += '#' + target.fragment
        is_sse = upstream.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() == 'text/event-stream'
        if is_sse:
            headers['X-Accel-Buffering'] = 'no'

        def body():
            try:
                if is_sse and hasattr(upstream.raw, 'read1'):
                    while True:
                        chunk = upstream.raw.read1(65536, decode_content=False)
                        if not chunk:
                            break
                        yield chunk
                else:
                    # A one-byte fallback avoids delaying live events on old urllib3.
                    yield from upstream.raw.stream(1 if is_sse else 65536, decode_content=False)
            except (requests.RequestException, HTTPError, OSError):
                logger.warning('BlueMap upstream stream disconnected')
            finally:
                close()

        status = upstream.status_code
        if request.method == 'HEAD' or status in (204, 304) or 100 <= status < 200:
            close()
            return Response(status=status, headers=headers)
        response = Response(body(), status=status, headers=headers)
        response.call_on_close(close)
        return response

    app.register_blueprint(blueprint)
