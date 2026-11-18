"""Explicit, bounded online adapters for the local Listening Room session.

No request is made at import time. Public configuration and saved media refs
never contain access tokens. Tokens are sent only to the configured origin.
API contracts: https://www.music-assistant.io/api/ and the official Python
client, https://contributors.home-assistant.io/core/internals/rest-api/,
the Jellyfin SDK, and https://listenbrainz.readthedocs.io/en/latest/users/api/.
"""
import hashlib
import contextlib
import metadata_http
import http.server
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ConnectionError(ValueError):
    """Value-free error suitable for display beside a connection."""


SERVICES = {
    'jellyfin': {'name': 'Jellyfin', 'endpoint': '', 'env': 'JELLYFIN_TOKEN',
                 'capabilities': ['library', 'playback', 'artwork', 'queue']},
    'music_assistant': {'name': 'Music Assistant', 'endpoint': '', 'env': 'MUSIC_ASSISTANT_TOKEN',
                        'capabilities': ['players', 'library', 'search', 'playback', 'radio_mode', 'queue_transfer']},
    'home_assistant': {'name': 'Home Assistant', 'endpoint': '', 'env': 'HOME_ASSISTANT_TOKEN',
                       'capabilities': ['scenes', 'lighting_restore']},
    'audiomuse': {'name': 'AudioMuse AI', 'endpoint': '', 'env': 'AUDIOMUSE_TOKEN',
                  'capabilities': ['server_health', 'sonic_mix', 'generated_jellyfin_playlists']},
    'listenbrainz': {'name': 'ListenBrainz', 'endpoint': 'https://api.listenbrainz.org',
                    'env': 'LISTENBRAINZ_TOKEN', 'capabilities': ['history', 'recommendations', 'submit_listen']},
}
_lock = threading.RLock()
_lighting_lock = threading.RLock()
_last_lb_request = 0.0
_lb_lock = threading.Lock()
_relay = None
_relay_lock = threading.Lock()
MAX_RESPONSE = 2 * 1024 * 1024


def _config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'listening-hub'


def _state_dir():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'listening-hub'


def _load(path, default):
    try:
        if path.stat().st_size > MAX_RESPONSE:
            return default
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path, data):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _service(service):
    if service not in SERVICES:
        raise ConnectionError('Choose a supported connection')
    return SERVICES[service]


def _endpoint(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ConnectionError('Enter an HTTP or HTTPS server address')
    try:
        url = urllib.parse.urlsplit(value)
        valid = url.scheme in ('http', 'https') and url.hostname and not (
            url.username or url.password or url.query or url.fragment)
        url.port
    except ValueError:
        valid = False
    if not valid:
        raise ConnectionError('Enter a server address without sign-in details or query parameters')
    return value.rstrip('/')


def config():
    """Public settings only. This operation does not check account access."""
    raw = _load(_config_dir() / 'connections.json', {})
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for key, spec in SERVICES.items():
        item = raw.get(key, {})
        item = item if isinstance(item, dict) else {}
        result[key] = {field: item.get(field, '') for field in ('endpoint', 'username', 'user_id', 'token_file')}
        result[key]['endpoint'] = result[key]['endpoint'] or spec['endpoint']
        # Imported configuration is data; unknown fields cannot leak tokens.
    return result


def save_connection(service, values):
    # Serialize settings and token-file updates with request-context capture.
    with _lock:
        return _save_connection_locked(service, values)


def _save_connection_locked(service, values):
    _service(service)
    if not isinstance(values, dict):
        raise ConnectionError('Invalid connection settings')
    allowed = {'endpoint', 'username', 'user_id', 'token_file'}
    if set(values) - allowed:
        raise ConnectionError('Save access tokens separately from connection settings')
    old = config()
    item = dict(old[service])
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ConnectionError('Invalid connection setting')
        item[key] = value.strip()
    if item['endpoint']:
        item['endpoint'] = _endpoint(item['endpoint'])
    if item['token_file'] and not Path(item['token_file']).expanduser().is_absolute():
        raise ConnectionError('Choose an absolute path for the private token file')
    with _lock:
        changed = any(item.get(key) != old[service].get(key) for key in ('endpoint', 'token_file'))
        old[service] = item
        _write(_config_dir() / 'connections.json', old)
        if changed:
            forget_check(service)
    return item


def forget_check(service):
    """Drop a stored check: it described a configuration that is no longer current."""
    with _lock:
        stored = _load(_state_dir() / 'connection-checks.json', {})
        if isinstance(stored, dict) and service in stored:
            del stored[service]
            _write(_state_dir() / 'connection-checks.json', stored)


def save_token(service, token):
    with _lock:
        return _save_token_locked(service, token)


def _save_token_locked(service, token):
    """A user-entered token is written to a separate mode-0600 file."""
    _service(service)
    if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() or ord(c) < 32 for c in token):
        raise ConnectionError('Enter a token without spaces or line breaks')
    directory = _config_dir() / 'credentials'
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ConnectionError('The connection credentials directory must be private and owned by you')
    fd, temporary = tempfile.mkstemp(prefix='.' + service, dir=directory)
    path = directory / (service + '.token')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(token + '\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    save_connection(service, {'token_file': str(path)})
    forget_check(service)
    return {'saved': True}


def _token(service, required=True, settings=None):
    spec = _service(service)
    token = os.environ.get(spec['env'], '')
    if not token:
        filename = (settings if settings is not None else config()[service])['token_file']
        if filename:
            try:
                fd = os.open(Path(filename).expanduser(), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                with os.fdopen(fd) as handle:
                    metadata = os.fstat(handle.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or metadata.st_size > 16384:
                        raise ConnectionError('The token file must be a private regular file owned by you (chmod 600)')
                    token = handle.read(16385).strip()
            except OSError:
                raise ConnectionError('Could not read the private token file') from None
    if not token and required:
        raise ConnectionError('Add a token in connection settings, then test the connection')
    if any(c.isspace() or ord(c) < 32 for c in token):
        raise ConnectionError('The saved token is invalid; replace it in connection settings')
    if service == 'jellyfin' and any(c in token for c in ('"', '\\')):
        raise ConnectionError('The Jellyfin token contains invalid header characters')
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization must never follow an unexpected redirect, even within
        # one service. Configure its final base URL instead.
        fp.close()
        raise ConnectionError('The server redirected this request; use its final server address')


def _cancel(event):
    if event is not None and event.is_set():
        raise ConnectionError('Request cancelled')


def _request_context(service, required=True, settings=None):
    """Bind credentials to one settings snapshot; never persist or display it."""
    with _lock:
        settings = dict(settings if settings is not None else config()[service])
        if not settings['endpoint']:
            raise ConnectionError('Set the ' + _service(service)['name'] + ' server address first')
        settings['endpoint'] = _endpoint(settings['endpoint'])
        return {'service': service, 'settings': settings,
                'token': _token(service, required=required, settings=settings), 'required': required}


def _check_context(context):
    with _lock:
        current = config()[context['service']]
        if current != context['settings'] or _token(context['service'], required=context['required'], settings=current) != context['token']:
            raise ConnectionError('Connection changed during the request; try again')


def _read_bounded(response, limit, cancel_event=None, timeout=30):
    """Read in small cancellable increments with a total wall-clock deadline."""
    deadline = time.monotonic() + timeout
    chunks = []
    size = 0
    read = getattr(response, 'read1', response.read)
    while True:
        _cancel(cancel_event)
        if time.monotonic() >= deadline:
            raise ConnectionError('Request timed out')
        chunk = read(min(65536, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise ConnectionError('The server response was too large; narrow the search')
    _cancel(cancel_event)
    return b''.join(chunks)


def _request(service, path, params=None, data=None, method=None, cancel_event=None, raw=False, required=True, context=None, bounded=False):
    spec = _service(service)
    context = context or _request_context(service, required=required)
    if context['service'] != service:
        raise ConnectionError('Invalid service context')
    _check_context(context)
    endpoint = context['settings']['endpoint']
    if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '..' in path.split('/'):
        raise ConnectionError('Invalid service request')
    _cancel(cancel_event)
    headers = {'User-Agent': 'listening-room/2.0 (local desktop client)', 'Accept': 'application/json'}
    token = context['token']
    if service == 'jellyfin' and not token:
        headers['Authorization'] = 'MediaBrowser Client="Listening Room", Device="Desktop", DeviceId="listening-room", Version="2.0"'
    if token:
        headers['Authorization'] = ('MediaBrowser Client="Listening Room", Device="Desktop", DeviceId="listening-room", Version="2.0", Token="' + token + '"'
                                    if service == 'jellyfin' else ('Token ' if service == 'listenbrainz' else 'Bearer ') + token)
    query = '?' + urllib.parse.urlencode(params) if params else ''
    payload = json.dumps(data).encode() if data is not None else None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(endpoint + path + query, data=payload, headers=headers, method=method)
    try:
        if bounded:
            chunks = []
            size = 0
            header_seen = False
            with contextlib.closing(metadata_http.exchange(request.full_url, headers, payload, cancel_event, 30)) as events:
                for kind, block in events:
                    if kind == b'H':
                        if header_seen:
                            raise ConnectionError('Invalid service response')
                        header_seen = True
                        status = json.loads(block).get('status')
                        if status != 200:
                            raise ConnectionError(spec['name'] + ' request failed (HTTP ' + str(status) + ')')
                    elif kind == b'D':
                        if not header_seen:
                            raise ConnectionError('Invalid service response')
                        size += len(block)
                        if size > MAX_RESPONSE:
                            raise ConnectionError('The server response was too large; narrow the search')
                        chunks.append(block)
                if not header_seen:
                    raise ConnectionError('Invalid service response')
            body = b''.join(chunks)
        else:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=6) as response:
                body = _read_bounded(response, MAX_RESPONSE, cancel_event)
        _check_context(context)
        _cancel(cancel_event)
        if len(body) > MAX_RESPONSE:
            raise ConnectionError('The server response was too large; narrow the search')
        if raw:
            return body
        return json.loads(body) if body else None
    except metadata_http.TransportError as error:
        raise ConnectionError(str(error)) from None
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise ConnectionError(spec['name'] + ' rejected access; update the connection and test again') from None
        if error.code == 404:
            raise ConnectionError(spec['name'] + ' could not find this item or API; check the server address') from None
        if error.code == 429:
            raise ConnectionError(spec['name'] + ' is limiting requests; wait before retrying') from None
        raise ConnectionError(spec['name'] + ' request failed (HTTP ' + str(error.code) + ')') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ConnectionError(spec['name'] + ' could not be reached; check the server and retry') from None
    except ConnectionError:
        raise
    except (ValueError, UnicodeError):
        raise ConnectionError(spec['name'] + ' returned an invalid response') from None


def capabilities():
    """Describe supported operations without claiming a live integration test."""
    settings = config()
    return [{'id': key, 'name': spec['name'], 'configured': bool(settings[key]['endpoint']),
             'capabilities': list(spec['capabilities']), 'state': 'not tested'} for key, spec in SERVICES.items()]


def test_connection(service, cancel_event=None):
    """Check a service. A failed check also withdraws any earlier stored success."""
    try:
        return _test_connection(service, cancel_event)
    except Exception:
        forget_check(service)
        raise


def _test_connection(service, cancel_event=None):
    _service(service)
    if service == 'jellyfin':
        user = _request(service, '/Users/Me', cancel_event=cancel_event)
        if not isinstance(user, dict) or not isinstance(user.get('Id'), str):
            raise ConnectionError('Jellyfin did not return a user identity')
        save_connection(service, {'user_id': user['Id'], 'username': str(user.get('Name', ''))})
        browse_jellyfin(kind='albums', cancel_event=cancel_event)
        detail = 'Library access verified. Local playback and artwork are checked when selected.'
        states = {'library': 'working', 'playback': 'not tested', 'artwork': 'not tested', 'queue': 'supported'}
    elif service == 'music_assistant':
        players = ma_players(cancel_event=cancel_event)
        detail = str(len(players)) + ' players returned. Playback and queue transfer need a selected player.'
        states = {'players': 'working', 'library': 'not tested', 'search': 'not tested', 'playback': 'not tested', 'radio_mode': 'not tested', 'queue_transfer': 'not tested'}
    elif service == 'audiomuse':
        # v0.8.8 exposes /api/config but has no /api/health. The response is
        # checked in memory and never persisted or displayed.
        health = _request(service, '/api/config', cancel_event=cancel_event, required=False)
        if not isinstance(health, dict) or 'num_recent_albums' not in health:
            raise ConnectionError('AudioMuse returned an unrecognized health response; check its version and server address')
        detail = ('AudioMuse web service responded. Worker and generator readiness are not verified. '
                  'Playlists already saved in Jellyfin remain available there.')
        states = {'server_health': 'working', 'sonic_mix': 'not tested', 'generated_jellyfin_playlists': 'not tested'}
    elif service == 'home_assistant':
        scenes = ha_scenes(cancel_event=cancel_event)
        detail = str(len(scenes)) + ' scenes returned. No lights were changed.'
        states = {'scenes': 'working', 'lighting_restore': 'not tested'}
    else:
        result = _request(service, '/1/validate-token', cancel_event=cancel_event)
        if not isinstance(result, dict) or not result.get('valid'):
            raise ConnectionError('ListenBrainz rejected this token; replace it and test again')
        save_connection(service, {'username': str(result.get('user_name', ''))})
        detail = 'Account access verified. History and listen submission run only when requested.'
        states = {'history': 'not tested', 'recommendations': 'not tested', 'submit_listen': 'not tested'}
    result = {'id': service, 'state': 'working', 'detail': detail, 'capabilities': states, 'checked_at': int(time.time())}
    with _lock:
        checks = _load(_state_dir() / 'connection-checks.json', {})
        checks = checks if isinstance(checks, dict) else {}
        checks[service] = result
        _write(_state_dir() / 'connection-checks.json', checks)
    return result


def checks():
    value = _load(_state_dir() / 'connection-checks.json', {})
    return value if isinstance(value, dict) else {}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
        raise ConnectionError('Select a valid service item')
    return value


def _jf_item(item):
    if not isinstance(item, dict) or not isinstance(item.get('Id'), str) or not item.get('Name'):
        return None
    kind = item.get('Type', 'Audio')
    artists = item.get('Artists') or [entry.get('Name', '') for entry in item.get('AlbumArtists', []) if isinstance(entry, dict)]
    ref = {'provider': 'jellyfin', 'id': _id(item['Id']), 'name': str(item['Name'])[:300], 'type': kind,
           'artist': ', '.join(str(value) for value in artists)[:300], 'album': str(item.get('Album', ''))[:300],
           'image_id': str(item.get('AlbumId') or item['Id']), 'duration': int(item.get('RunTimeTicks') or 0) / 10000000,
           'uri': 'jellyfin://' + kind.lower() + '/' + item['Id']}
    genres = item.get('Genres')
    if isinstance(genres, list):
        ref['genres'] = [genre[:100] for genre in genres[:30] if isinstance(genre, str) and genre]
    tags = item.get('Tags')
    if isinstance(tags, list):
        ref['tags'] = [tag[:100] for tag in tags[:30] if isinstance(tag, str) and tag]
    user_data = item.get('UserData')
    if isinstance(user_data, dict):
        if isinstance(user_data.get('IsFavorite'), bool):
            ref['favorite'] = user_data['IsFavorite']
        count = user_data.get('PlayCount')
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 100000000:
            ref['play_count'] = count
        date = user_data.get('LastPlayedDate')
        if isinstance(date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.+Z-]{1,35}', date):
            ref['last_played'] = date
    return ref


def browse_jellyfin(query='', kind='albums', parent_id=None, cancel_event=None):
    if kind not in ('albums', 'playlists', 'recent', 'tracks'):
        raise ConnectionError('Choose albums, playlists, recent albums or tracks')
    query = str(query).strip()[:200]
    user = config()['jellyfin']['user_id']
    if not user:
        raise ConnectionError('Test the Jellyfin connection to select its account before browsing')
    params = {'UserId': _id(user), 'Limit': 60, 'Recursive': 'true',
              'IncludeItemTypes': {'albums': 'MusicAlbum', 'playlists': 'Playlist', 'recent': 'MusicAlbum', 'tracks': 'Audio'}[kind],
              'Fields': 'PrimaryImageAspectRatio,Genres,Tags,RunTimeTicks', 'EnableUserData': 'true', 'EnableTotalRecordCount': 'false'}
    if query:
        params['SearchTerm'] = query
    if parent_id:
        params['ParentId'] = _id(parent_id)
        params['SortBy'] = 'ParentIndexNumber,IndexNumber,SortName'
    elif kind == 'recent':
        params.update(SortBy='DateCreated', SortOrder='Descending')
    else:
        params['SortBy'] = 'SortName'
    data = _request('jellyfin', '/Items', params=params, cancel_event=cancel_event)
    if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
        raise ConnectionError('Jellyfin returned an invalid library listing')
    return [ref for row in data['Items'][:60] if (ref := _jf_item(row))]


def jellyfin_queue(item, cancel_event=None):
    if not isinstance(item, dict) or item.get('provider') != 'jellyfin':
        raise ConnectionError('Select a Jellyfin album, playlist or track')
    identity = _id(item.get('id'))
    if item.get('type') == 'Audio':
        return [dict(item)]
    if item.get('type') not in ('MusicAlbum', 'Playlist'):
        raise ConnectionError('This Jellyfin item is not a music album or playlist')
    if item['type'] == 'Playlist':
        data = _request('jellyfin', '/Playlists/' + identity + '/Items',
                        params={'UserId': _id(config()['jellyfin']['user_id']), 'Limit': 200, 'Fields': 'Genres,Tags,RunTimeTicks', 'EnableUserData': 'true'}, cancel_event=cancel_event)
        if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
            raise ConnectionError('Jellyfin returned an invalid playlist')
        result = [ref for row in data['Items'][:200] if row.get('Type') == 'Audio' and (ref := _jf_item(row))]
    else:
        result = browse_jellyfin(kind='tracks', parent_id=identity, cancel_event=cancel_event)
    if not result:
        raise ConnectionError('This album or playlist has no playable music tracks')
    return result


def audiomuse_similar(item, cancel_event=None):
    """Resolve an explicit AudioMuse sonic mix seed back to verified Jellyfin tracks.

    The AudioMuse service is optional and version-dependent. Its response may
    use internal IDs; only IDs independently returned by the connected Jellyfin
    library are playable. This operation never creates a remote playlist.
    """
    if not isinstance(item, dict) or item.get('provider') != 'jellyfin' or item.get('type') != 'Audio':
        raise ConnectionError('Choose a Jellyfin track as the AudioMuse seed')
    seed = _id(item.get('id'))
    data = _request('audiomuse', '/api/similar_tracks', params={'item_id': seed, 'n': 30},
                    cancel_event=cancel_event, required=False)
    rows = data.get('results') if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ConnectionError('AudioMuse returned an unsupported mix response; check its version and analysis')
    ids = []
    for row in rows[:60]:
        value = row.get('item_id') if isinstance(row, dict) else None
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value) and value not in ids:
            ids.append(value)
    if not ids:
        raise ConnectionError('AudioMuse returned no similar tracks; check library analysis')
    user = _id(config()['jellyfin']['user_id'])
    result = _request('jellyfin', '/Items', params={'UserId': user, 'Ids': ','.join(ids),
                       'IncludeItemTypes': 'Audio', 'Recursive': 'true', 'Limit': 60,
                       'Fields': 'RunTimeTicks'}, cancel_event=cancel_event)
    if not isinstance(result, dict) or not isinstance(result.get('Items'), list):
        raise ConnectionError('Jellyfin could not verify the generated tracks')
    refs = {}
    for row in result['Items'][:60]:
        if isinstance(row, dict) and row.get('Type') == 'Audio':
            ref = _jf_item(row)
            if ref:
                refs[ref['id']] = ref
    tracks = [refs[identity] for identity in ids if identity in refs]
    if not tracks:
        raise ConnectionError('AudioMuse returned no tracks that match this Jellyfin library')
    return tracks


def artwork(item):
    """Fetch authenticated artwork to a private local cache; no token URLs."""
    identity = _id(item.get('image_id') or item.get('id'))
    cache = _state_dir() / 'artwork'
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    key = hashlib.sha256((config()['jellyfin']['endpoint'] + ':' + identity).encode()).hexdigest()
    path = cache / (key + '.jpg')
    if not path.is_file():
        body = _request('jellyfin', '/Items/' + identity + '/Images/Primary', params={'MaxWidth': 640, 'Format': 'jpg'}, raw=True)
        if not body.startswith(b'\xff\xd8'):
            raise ConnectionError('Jellyfin did not return JPEG artwork for this item')
        fd, name = tempfile.mkstemp(dir=cache, prefix='.artwork-')
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(body)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return str(path)


class _StreamRelay(http.server.ThreadingHTTPServer):
    """Loopback media only; authentication never reaches mpv or redirects."""
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self):
        self.entries = {}
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), _StreamHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True, name='jellyfin-stream-relay')
        self.thread.start()

    def add(self, endpoint, token, identity):
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            # Keep a small bounded set so ongoing reads/seeks survive a later
            # preparation, without retaining old account credentials forever.
            while len(self.entries) >= 64:
                self.entries.pop(next(iter(self.entries)))
            self.entries[ticket] = (endpoint, token, identity)
        return 'http://127.0.0.1:' + str(self.server_port) + '/' + ticket

    def handle_error(self, request, client_address):
        # No remote header or credential-bearing exception is logged.
        pass


class _StreamHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_HEAD(self):
        self._stream(head=True)

    def do_GET(self):
        self._stream()

    def _stream(self, head=False):
        ticket = self.path.removeprefix('/')
        with self.server.lock:
            source = self.server.entries.get(ticket)
        if source is None:
            self.send_error(404, 'Media selection expired')
            return
        endpoint, token, identity = source
        headers = {'X-Emby-Token': token, 'User-Agent': 'listening-room/2.0 (local desktop client)'}
        # Only a conventional single byte range is forwarded, never arbitrary
        # client headers, destination URLs or service operations.
        value = self.headers.get('Range', '')
        if value:
            if not re.fullmatch(r'bytes=(?:\d+-\d*|-\d+)', value):
                self.send_error(400, 'Unsupported byte range')
                return
            headers['Range'] = value
        upstream = urllib.request.Request(endpoint + '/Audio/' + identity + '/stream?static=true',
                                          headers=headers, method='HEAD' if head else 'GET')
        started = False
        try:
            with urllib.request.build_opener(_NoRedirect()).open(upstream, timeout=8) as response:
                self.send_response(response.status)
                for key in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                    if response.headers.get(key):
                        self.send_header(key, response.headers[key])
                self.send_header('Connection', 'close')
                self.end_headers()
                started = True
                if not head:
                    while chunk := response.read(64 * 1024):
                        self.wfile.write(chunk)
        except (ConnectionError, OSError, urllib.error.URLError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            if not started:
                self.send_error(502, 'Jellyfin stream unavailable; check the connection and retry')


def _relay_stream(identity):
    global _relay
    context = _request_context('jellyfin')
    _check_context(context)
    endpoint, token = context['settings']['endpoint'], context['token']
    with _relay_lock:
        if _relay is None:
            try:
                _relay = _StreamRelay()
            except OSError:
                raise ConnectionError('Could not start the private local library stream relay') from None
        return _relay.add(endpoint, token, _id(identity))


def close_playback_relay():
    """Release service-owned media relay threads on controller shutdown."""
    global _relay
    with _relay_lock:
        relay, _relay = _relay, None
    if relay is not None:
        relay.shutdown()
        relay.server_close()
        with relay.lock:
            relay.entries.clear()
        relay.thread.join(timeout=2)


def prepare_playback(item, cancel_event=None):
    """Prepare a trusted source descriptor for the shared session controller.

    mpv receives a loopback media URL, never an authorization header. The
    relay refuses upstream redirects and only serves explicitly prepared
    music tracks. Persist ``reference``/queue refs so replay can prepare a
    fresh relay URL after restart.
    """
    queue = jellyfin_queue(item, cancel_event)
    first = queue[0]
    ref = {key: item[key] for key in ('provider', 'id', 'type', 'name') if key in item}
    source = {'id': 'jellyfin-' + _id(first['id']), 'name': first.get('artist', '') + ' — ' + first['name'],
              'kind': 'radio', 'url': _relay_stream(first['id']),
              'reference': ref, 'queue': queue, 'queue_index': 0,
              'can_seek': True, 'can_next': len(queue) > 1, 'can_previous': False}
    with _lock:
        _write(_state_dir() / 'library-queue.json', {'reference': ref, 'items': queue, 'index': 0})
    return source


def library_queue():
    result = _load(_state_dir() / 'library-queue.json', {})
    return result if isinstance(result, dict) else {}


def jellyfin_link(item):
    endpoint = config()['jellyfin']['endpoint']
    if not endpoint:
        raise ConnectionError('Set the Jellyfin server address first')
    return _endpoint(endpoint) + '/web/#/details?id=' + urllib.parse.quote(_id(item.get('id')))


MA_COMMANDS = {'players/all', 'players/cmd/play', 'players/cmd/pause', 'players/cmd/stop',
               'players/cmd/volume_set', 'player_queues/play_media', 'player_queues/transfer',
               'player_queues/items', 'music/search', 'player_queues/get_active_queue',
               'music/playlists/library_items', 'music/tracks/library_items', 'music/albums/library_items'}


def ma_command(command, args=None, cancel_event=None):
    if command not in MA_COMMANDS or (args is not None and not isinstance(args, dict)):
        raise ConnectionError('Unsupported Music Assistant operation')
    data = _request('music_assistant', '/api', data={'message_id': uuid.uuid4().hex, 'command': command, 'args': args or {}}, cancel_event=cancel_event)
    if isinstance(data, dict):
        if data.get('error_code') is not None or data.get('error'):
            raise ConnectionError('Music Assistant could not complete this operation; check the selected player and API support')
        if 'result' in data:
            return data['result']
    return data


def _ma_item(item):
    if not isinstance(item, dict):
        return None
    uri, name = item.get('uri'), item.get('name')
    if (not isinstance(uri, str) or not uri or len(uri) > 2048 or
            any(ord(c) < 32 for c in uri) or not isinstance(name, str) or not name):
        return None
    # A URI is a reference consumed by the configured MA server; reject URLs
    # with embedded credentials or query strings before saving it in room state.
    try:
        parsed = urllib.parse.urlsplit(uri)
        if not parsed.scheme or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
    except ValueError:
        return None
    artists = item.get('artists') if isinstance(item.get('artists'), list) else []
    return {'provider': 'music_assistant', 'id': str(item.get('item_id', ''))[:160],
            'name': name[:300], 'type': str(item.get('media_type', ''))[:40],
            'artist': ', '.join(str(a.get('name', '')) for a in artists if isinstance(a, dict))[:300],
            'uri': uri}


def browse_music_assistant(query='', kind='playlists', cancel_event=None, provider=None, search_all=False):
    """Browse MA's library or search its configured providers without starting playback.

    A provider filter is an MA provider instance/domain, e.g. a configured YouTube
    Music provider. Its availability and actual playback support remain server-owned.
    """
    if kind not in ('playlists', 'tracks', 'albums'):
        raise ConnectionError('Choose playlists, tracks or albums')
    if not isinstance(query, str) or len(query) > 200:
        raise ConnectionError('Enter a shorter music search')
    if provider is not None and (not isinstance(provider, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', provider)):
        raise ConnectionError('Choose a valid Music Assistant provider')
    if search_all:
        if not query.strip():
            raise ConnectionError('Enter a search before searching music providers')
        args = {'search_query': query.strip(), 'media_types': [kind[:-1]], 'limit': 60}
        if provider:
            args['providers'] = [provider]
        data = ma_command('music/search', args, cancel_event)
        if not isinstance(data, dict):
            raise ConnectionError('Music Assistant returned an invalid search')
        rows = data.get(kind)
    else:
        args = {'limit': 60}
        if query.strip():
            args['search'] = query.strip()
        if provider:
            args['provider'] = provider
        rows = ma_command('music/' + kind + '/library_items', args, cancel_event)
    if not isinstance(rows, list):
        raise ConnectionError('Music Assistant returned an invalid library listing')
    return [ref for row in rows[:60] if (ref := _ma_item(row))]


def jellyfin_login(username, password, cancel_event=None):
    """Explicit username login. Password is sent in-process to the configured origin only."""
    if not isinstance(username, str) or not username.strip() or len(username) > 256:
        raise ConnectionError('Enter your Jellyfin username')
    if not isinstance(password, str) or len(password) > 4096 or not password:
        raise ConnectionError('Enter your Jellyfin password')
    endpoint = _endpoint(config()['jellyfin']['endpoint'])
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ConnectionError('Use HTTPS for Jellyfin sign-in (HTTP is allowed only on loopback)')
    # Ignore a stale existing token during sign-in; do not send it with the password.
    _cancel(cancel_event)
    request = urllib.request.Request(endpoint + '/Users/AuthenticateByName',
        data=json.dumps({'Username': username.strip(), 'Pw': password}).encode(),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'Authorization': 'MediaBrowser Client="Listening Room", Device="Desktop", DeviceId="listening-room", Version="2.0"'}, method='POST')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=6) as response:
            body = response.read(MAX_RESPONSE + 1)
        _cancel(cancel_event)
        if len(body) > MAX_RESPONSE:
            raise ConnectionError('Jellyfin returned an invalid sign-in response')
        data = json.loads(body)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code in (401, 403):
            raise ConnectionError('Jellyfin rejected sign-in; check your account and password') from None
        raise ConnectionError('Jellyfin sign-in failed (HTTP ' + str(code) + ')') from None
    except (OSError, ValueError, UnicodeError):
        raise ConnectionError('Jellyfin sign-in could not complete; check the server and retry') from None
    if not isinstance(data, dict) or not isinstance(data.get('User'), dict) or not isinstance(data['User'].get('Id'), str):
        raise ConnectionError('Jellyfin returned an invalid sign-in response')
    token = data.get('AccessToken')
    if not isinstance(token, str) or not token or any(c in token for c in ('"', '\\')):
        raise ConnectionError('Jellyfin returned an invalid sign-in response')
    save_token('jellyfin', token)
    save_connection('jellyfin', {'user_id': _id(data['User']['Id']), 'username': str(data['User'].get('Name', username))})
    return {'signed_in': True}


def ma_players(cancel_event=None):
    result = ma_command('players/all', cancel_event=cancel_event)
    if not isinstance(result, list):
        raise ConnectionError('Music Assistant returned an invalid player listing')
    return [{'id': str(player['player_id']), 'name': str(player.get('display_name') or player.get('name') or player['player_id']),
             'available': bool(player.get('available', False)), 'state': str(player.get('state', 'unknown')),
             'volume': player.get('volume_level')} for player in result[:100] if isinstance(player, dict) and player.get('player_id')]


def ma_play(uri, player_id, radio_mode=False):
    if not isinstance(uri, str) or not uri or len(uri) > 2048 or any(ord(c) < 32 for c in uri):
        raise ConnectionError('Enter a Music Assistant media URI or supported URL')
    if not isinstance(player_id, str) or not player_id:
        raise ConnectionError('Choose a Music Assistant player')
    if not isinstance(radio_mode, bool):
        raise ConnectionError('Choose a valid Music Assistant play mode')
    args = {'queue_id': _ma_queue_id(player_id), 'media': uri, 'option': 'replace'}
    if radio_mode:
        args['radio_mode'] = True
    return ma_command('player_queues/play_media', args)


def ma_transfer(source, target):
    if not source or not target or source == target:
        raise ConnectionError('Choose two different Music Assistant players')
    source_queue, target_queue = _ma_queue_id(source), _ma_queue_id(target)
    if source_queue == target_queue:
        raise ConnectionError('These speakers already use the same queue')
    return ma_command('player_queues/transfer', {'source_queue_id': source_queue, 'target_queue_id': target_queue, 'auto_play': True})


def _ma_queue_id(player_id):
    result = ma_command('player_queues/get_active_queue', {'player_id': player_id})
    if isinstance(result, dict) and result.get('queue_id'):
        return str(result['queue_id'])
    if result is not None:
        raise ConnectionError('Music Assistant returned an invalid active queue')
    return player_id


def ha_scenes(cancel_event=None):
    rows = _request('home_assistant', '/api/states', cancel_event=cancel_event)
    if not isinstance(rows, list):
        raise ConnectionError('Home Assistant returned an invalid scene listing')
    return [{'id': item['entity_id'], 'name': str((item.get('attributes') or {}).get('friendly_name') or item['entity_id']),
             'entities': [entity for entity in (item.get('attributes') or {}).get('entity_id', []) if isinstance(entity, str)]}
            for item in rows[:2000] if isinstance(item, dict) and str(item.get('entity_id', '')).startswith('scene.')]


def room_scenes():
    data = _load(_config_dir() / 'room-lighting.json', {})
    return data if isinstance(data, dict) else {}


def set_room_scene(room_id, scene_id=None, entities=None):
    if not isinstance(room_id, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', room_id):
        raise ConnectionError('Choose a room to configure lighting')
    if scene_id and not re.fullmatch(r'scene\.[a-z0-9_]+', scene_id):
        raise ConnectionError('Choose a Home Assistant scene')
    entities = entities or []
    if not isinstance(entities, list) or len(entities) > 100 or any(not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', str(entity)) for entity in entities):
        raise ConnectionError('Choose valid entities for the lighting snapshot')
    with _lock:
        settings = room_scenes()
        if scene_id:
            settings[room_id] = {'scene_id': scene_id, 'entities': entities, 'enabled': True}
        else:
            settings.pop(room_id, None)
        _write(_config_dir() / 'room-lighting.json', settings)
    return {'saved': True}


def activate_room_scene(room_id, cancel_event=None):
    with _lighting_lock:
        _cancel(cancel_event)
        try:
            return _activate_room_scene(room_id, cancel_event)
        except ConnectionError:
            if cancel_event is not None and cancel_event.is_set():
                restore_room_scene()
            raise


def _activate_room_scene(room_id, cancel_event):
    settings = room_scenes().get(room_id, {})
    if not settings.get('enabled'):
        return {'changed': False}
    scene = settings.get('scene_id')
    entities = settings.get('entities') or []
    if not re.fullmatch(r'scene\.[a-z0-9_]+', str(scene)):
        raise ConnectionError('This room has an invalid lighting scene; choose it again')
    snapshot_path = _state_dir() / 'lighting-snapshot.json'
    previous = _load(snapshot_path, {})
    snapshots = previous.get('snapshots', []) if isinstance(previous, dict) else []
    if isinstance(previous, dict) and previous.get('snapshot_id'):
        snapshots = [previous]
    captured = {entity for entry in snapshots for entity in entry.get('entities', [])}
    additional = [entity for entity in entities if entity not in captured]
    # Capture each newly affected entity before its first change. Separate
    # snapshots preserve the true initial state when switching between rooms
    # whose scenes affect different sets of lights.
    if additional:
        _cancel(cancel_event)
        snapshot = 'listening_room_' + uuid.uuid4().hex
        _request('home_assistant', '/api/services/scene/create', data={'scene_id': snapshot, 'snapshot_entities': additional})
        snapshots.append({'snapshot_id': 'scene.' + snapshot, 'entities': additional})
        _write(snapshot_path, {'snapshots': snapshots})
    _cancel(cancel_event)
    _request('home_assistant', '/api/services/scene/turn_on', data={'entity_id': scene})
    _cancel(cancel_event)
    return {'changed': True, 'restorable': bool(snapshots), 'scene_id': scene}


def restore_room_scene():
    with _lighting_lock:
        return _restore_room_scene()


def _restore_room_scene():
    path = _state_dir() / 'lighting-snapshot.json'
    saved = _load(path, {})
    snapshots = saved.get('snapshots', []) if isinstance(saved, dict) else []
    if isinstance(saved, dict) and saved.get('snapshot_id'):
        snapshots = [saved]
    if not snapshots:
        return {'changed': False}
    for item in snapshots:
        if not isinstance(item, dict) or not re.fullmatch(r'scene\.listening_room_[a-f0-9]{32}', str(item.get('snapshot_id', ''))):
            raise ConnectionError('The saved lighting snapshot is invalid')
    while snapshots:
        item = snapshots[-1]
        _request('home_assistant', '/api/services/scene/turn_on', data={'entity_id': item['snapshot_id']})
        snapshots.pop()
        # Retain only still-pending restores so a partial failure can be
        # retried without replaying a snapshot already restored successfully.
        _write(path, {'snapshots': snapshots})
    path.unlink(missing_ok=True)
    return {'changed': True}


def _public_url(url):
    if not isinstance(url, str):
        return ''
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query)
                 if not any(term in key.lower() for term in ('token', 'auth', 'password', 'secret', 'key', 'signature'))]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ''))
    except ValueError:
        return ''


def bookmark(snapshot, note=''):
    """Remember uncertain metadata as reported, never fabricate artist/title."""
    selected = snapshot.get('selected') or (snapshot.get('layers') or {}).get('music') or {}
    active = snapshot.get('active') or {}
    music = active.get('music') or {}
    reference = music.get('reference') or music.get('collection_reference') or {}
    artist = str(selected.get('artist') or '')[:300]
    station = str(selected.get('station') or selected.get('source_name') or '')[:300]
    if selected.get('id') == 'radio' and reference.get('provider') != 'jellyfin':
        # The legacy radio adapter uses artist for the station label to keep
        # desktop widgets readable. That does not establish a track artist.
        station = str(selected.get('name') or selected.get('source_name') or selected.get('artist') or '')[:300]
        artist = str(selected.get('track_artist') or '')[:300]
    elif reference.get('provider') == 'jellyfin':
        queue = music.get('queue') or []
        index = music.get('queue_index', 0)
        if isinstance(index, int) and 0 <= index < len(queue):
            artist = str(queue[index].get('artist') or '')[:300]
    item = {'id': uuid.uuid4().hex, 'remembered_at': int(time.time()),
            'title': str(selected.get('title') or selected.get('name') or '')[:300],
            'artist': artist, 'station': station,
            'room': str(active.get('name') or active.get('room_id') or '')[:300],
            'url': _public_url(selected.get('url')), 'note': str(note)[:1000],
            'metadata_status': 'as reported by player'}
    if not item['title'] and not item['station']:
        raise ConnectionError('Start a source before remembering what is playing')
    with _lock:
        entries = remembered()
        _write(_state_dir() / 'remembered.json', [item] + entries[:499])
    return item


def remembered():
    data = _load(_state_dir() / 'remembered.json', [])
    return [item for item in data[:500] if isinstance(item, dict)] if isinstance(data, list) else []


def discovery_links(item):
    query = ' '.join(str(item.get(key, '')) for key in ('artist', 'title')).strip()
    if not query:
        query = str(item.get('name') or item.get('station') or '')
    return {'query': query[:400], 'youtube_music': 'https://music.youtube.com/search?' + urllib.parse.urlencode({'q': query[:400]}),
            'musicbrainz': 'https://musicbrainz.org/search?' + urllib.parse.urlencode({'query': query[:400], 'type': 'recording', 'method': 'indexed'})}


def _lb_wait():
    global _last_lb_request
    with _lb_lock:
        delay = 1.05 - (time.monotonic() - _last_lb_request)
        if delay > 0:
            time.sleep(delay)
        _last_lb_request = time.monotonic()


def listenbrainz_history():
    user = config()['listenbrainz']['username']
    if not user:
        raise ConnectionError('Test the ListenBrainz connection to select its account first')
    _lb_wait()
    result = _request('listenbrainz', '/1/user/' + urllib.parse.quote(user, safe='') + '/listens', params={'count': 20})
    if not isinstance(result, dict) or not isinstance(result.get('payload'), dict):
        raise ConnectionError('ListenBrainz returned an invalid history')
    return result['payload'].get('listens', [])[:20]


def listenbrainz_recommendations():
    """Bounded experimental recording recommendations; no generated titles."""
    user = config()['listenbrainz']['username']
    if not user:
        raise ConnectionError('Test the ListenBrainz connection to select its account first')
    _lb_wait()
    result = _request('listenbrainz', '/1/cf/recommendation/user/' + urllib.parse.quote(user, safe='') + '/recording',
                      params={'count': 10})
    if result is None:
        return []
    if not isinstance(result, dict) or not isinstance(result.get('payload'), dict):
        raise ConnectionError('ListenBrainz returned invalid recommendations')
    rows = result['payload'].get('mbids') or []
    return [{'recording_mbid': row['recording_mbid'], 'score': row.get('score'),
             'url': 'https://musicbrainz.org/recording/' + row['recording_mbid']}
            for row in rows[:10] if isinstance(row, dict) and re.fullmatch(r'[a-f0-9-]{36}', str(row.get('recording_mbid', '')))]


def submit_listen(item, listened_seconds, duration=None):
    """Submit only a confirmed track that has met ListenBrainz's listen rule."""
    if not item.get('artist') or not item.get('title'):
        raise ConnectionError('Confirm a track artist and title before submitting a listen')
    try:
        listened = float(listened_seconds)
        length = float(duration) if duration is not None else 0
    except (TypeError, ValueError):
        raise ConnectionError('Provide a valid listening duration') from None
    if not math.isfinite(listened) or not math.isfinite(length) or listened < 0 or length < 0:
        raise ConnectionError('Provide a valid listening duration')
    threshold = min(240, length / 2) if length > 0 else 240
    if listened < threshold:
        raise ConnectionError('This track has not yet met the ListenBrainz listening threshold')
    _lb_wait()
    return _request('listenbrainz', '/1/submit-listens', data={'listen_type': 'single', 'payload': [{
        'listened_at': int(item.get('remembered_at') or time.time()),
        'track_metadata': {'artist_name': str(item['artist'])[:300], 'track_name': str(item['title'])[:300],
                           'additional_info': {'submission_client': 'Listening Room', 'submission_client_version': '2.0'}}}]})


def preview_link(value):
    """Classify explicit paste/drop without fetching or starting playback."""
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ConnectionError('Paste one public media URL')
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        parsed.port
    except ValueError:
        raise ConnectionError('Use a public HTTP or HTTPS media URL without sign-in details') from None
    host = parsed.hostname.lower()
    public = _public_url(value)
    if public != value and any(term in parsed.query.lower() for term in ('token', 'auth', 'key', 'secret', 'signature')):
        raise ConnectionError('Use a public link without private access parameters')
    if host in ('youtube.com', 'www.youtube.com', 'music.youtube.com', 'm.youtube.com', 'youtu.be'):
        import listening_hub as hub
        try:
            url = hub.youtube_video_url(value)
        except hub.HubError:
            return {'kind': 'browser', 'name': 'YouTube discovery', 'url': value, 'actions': ['original']}
        return {'kind': 'youtube', 'name': 'YouTube video', 'url': url, 'original': value, 'actions': ['music', 'atmosphere', 'picture', 'original']}
    if host in ('twitch.tv', 'www.twitch.tv') and re.fullmatch(r'/[A-Za-z0-9_]{1,25}/?', parsed.path) and not parsed.query:
        return {'kind': 'twitch', 'name': 'Twitch · ' + parsed.path.strip('/'), 'url': 'https://www.twitch.tv/' + parsed.path.strip('/'), 'original': value, 'actions': ['music', 'atmosphere', 'original']}
    suffix = Path(parsed.path).suffix.lower()
    if suffix in ('.mp3', '.flac', '.ogg', '.opus', '.m3u', '.m3u8', '.pls', '.aac'):
        return {'kind': 'stream', 'name': Path(parsed.path).name or host, 'url': value, 'original': value, 'actions': ['music', 'atmosphere', 'original']}
    if suffix in ('.mp4', '.webm', '.mkv', '.mov'):
        return {'kind': 'video', 'name': Path(parsed.path).name or host, 'url': value, 'original': value, 'actions': ['picture', 'original']}
    return {'kind': 'browser', 'name': host, 'url': value, 'actions': ['original']}


def paired_devices():
    """Only paired, reachable KDE Connect devices; no pairing changes."""
    executable = shutil.which('kdeconnect-cli')
    if not executable:
        raise ConnectionError('KDE Connect is unavailable in this desktop session')
    try:
        result = subprocess.run([executable, '--list-available', '--id-name-only'], capture_output=True,
                                text=True, timeout=8, env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.TimeoutExpired):
        raise ConnectionError('Could not contact KDE Connect; open its desktop settings and retry') from None
    if result.returncode:
        raise ConnectionError('KDE Connect could not list paired devices; check the desktop session')
    devices = []
    for line in result.stdout.splitlines()[:100]:
        identity, separator, name = line.partition(' ')
        if separator and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity):
            devices.append({'id': identity, 'name': name.strip()[:200] or identity})
    return devices


def remote_commands():
    """Named, fixed CLI commands; never interpolate device-controlled text."""
    stable = Path.home() / '.local/bin/listening-roomctl'
    executable = str(stable) if stable.is_file() else shutil.which('listening-roomctl')
    if not executable:
        raise ConnectionError('Install Listening Room before adding phone commands')
    command = shlex.quote(executable) + ' '
    commands = {
        'listening-room-quiet': {'name': 'Quiet listening room', 'command': command + 'quiet'},
        'listening-room-return': {'name': 'Return to last listening room', 'command': command + 'return'},
        'listening-room-stop': {'name': 'Stop listening room', 'command': command + 'stop'},
    }
    import room_catalogue
    for room in room_catalogue.RECIPES:
        commands['listening-room-enter-' + room['id']] = {
            'name': 'Enter ' + room['name'], 'command': command + 'enter ' + shlex.quote(room['id'])}
    return commands


def _qsettings_commands(value):
    """Decode Qt's quoted QString or QByteArray, refusing unknown encodings.

    QSettings uses C-style backslash escapes. JSON's own backslashes are
    escaped again at this layer. Existing malformed data is never replaced.
    """
    value = value.strip()
    if value.startswith('@ByteArray(') and value.endswith(')'):
        value = value[11:-1]
    elif value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    decoded = []
    index = 0
    escapes = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"', '0': '\0'}
    while index < len(value):
        char = value[index]
        if char == '\\':
            index += 1
            if index >= len(value):
                raise ConnectionError('Existing phone commands have an unsupported encoding; edit them in KDE Connect settings')
            char = value[index]
            if char == 'x':
                match = re.match(r'[a-fA-F0-9]{1,4}', value[index + 1:])
                if not match:
                    raise ConnectionError('Existing phone commands have an invalid escape')
                decoded.append(chr(int(match.group(), 16)))
                index += len(match.group())
            elif char in escapes:
                decoded.append(escapes[char])
            else:
                raise ConnectionError('Existing phone commands have an unsupported encoding; edit them in KDE Connect settings')
        else:
            decoded.append(char)
        index += 1
    try:
        data = json.loads(''.join(decoded))
    except ValueError:
        raise ConnectionError('Existing phone commands are invalid; repair them in KDE Connect settings first') from None
    if not isinstance(data, dict):
        raise ConnectionError('Existing phone commands are invalid; repair them in KDE Connect settings first')
    return data


def install_remote_commands(device_id):
    """Explicit per-device install preserving every unrelated command/key.

    Format verified against KDE's KdeConnectPluginConfig and runcommand
    plugin. Emit its normal configChanged signal after an atomic write.
    """
    if not isinstance(device_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', device_id):
        raise ConnectionError('Choose a paired phone')
    if device_id not in {device['id'] for device in paired_devices()}:
        raise ConnectionError('The selected phone is not paired and reachable; reconnect it and retry')
    root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'kdeconnect'
    directory = root / device_id / 'kdeconnect_runcommand'
    for path in (root, root / device_id, directory):
        path.mkdir(parents=False, mode=0o700, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise ConnectionError('KDE Connect configuration must use owned directories without writable shared access')
    if root.stat().st_mode & 0o077:
        raise ConnectionError('The KDE Connect configuration directory must be private (chmod 700)')
    filename = directory / 'config'
    original = ''
    if filename.exists() or filename.is_symlink():
        try:
            fd = os.open(filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd) as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_size > MAX_RESPONSE:
                    raise ConnectionError('The phone command configuration must be an owned regular file')
                original = handle.read(MAX_RESPONSE + 1)
        except OSError:
            raise ConnectionError('Could not read the phone commands without following a symbolic link') from None
    lines = original.splitlines()
    section = None
    command_line = None
    insert_line = len(lines)
    commands = {}
    for index, line in enumerate(lines):
        if line.startswith('[') and line.endswith(']'):
            if section == 'General' and command_line is None:
                insert_line = index
            section = line[1:-1]
        elif section == 'General' and line.startswith('commands='):
            if command_line is not None:
                raise ConnectionError('The phone command configuration contains duplicate command settings')
            command_line = index
            commands = _qsettings_commands(line.partition('=')[2])
    commands.update(remote_commands())
    # Qt accepts quoted strings with escaped slash/quote characters. JSON's
    # second encoding supplies those escapes, with UTF-8 retained directly.
    encoded = 'commands=' + json.dumps(json.dumps(commands, ensure_ascii=False, separators=(',', ':')), ensure_ascii=False)
    if command_line is not None:
        lines[command_line] = encoded
    elif '[General]' in lines:
        lines.insert(insert_line, encoded)
    else:
        lines = ['[General]', encoded, ''] + lines
    fd, temporary = tempfile.mkstemp(prefix='.room-commands-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write('\n'.join(lines) + '\n')
        os.replace(temporary, filename)
    finally:
        Path(temporary).unlink(missing_ok=True)
    notified = _notify_phone_config(device_id)
    return {'installed': len(remote_commands()), 'device_id': device_id, 'notified': notified}


def _notify_phone_config(device_id):
    try:
        from gi.repository import Gio
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.emit_signal(None, '/kdeconnect/' + device_id + '/kdeconnect_runcommand', 'org.kde.kdeconnect.config', 'configChanged', None)
        bus.flush_sync(None)
        return True
    except Exception:
        # The file is valid without a session bus; KDE Connect rereads it on
        # its next config load. Never restart the user's daemon implicitly.
        return False
