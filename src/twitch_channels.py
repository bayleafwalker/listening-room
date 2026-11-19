"""Twitch channel discovery, with explicit uncertainty when Helix is unavailable."""
import json
import os
from pathlib import Path
import random
import re
import tempfile
import urllib.parse
import urllib.error
import urllib.request
import http_fetch
import twitch_credentials as local_credentials

STYLES = {'music': {'name': 'Music', 'category': 'Music'},
          'asmr': {'name': 'ASMR', 'category': 'ASMR'}}
LOGIN = re.compile(r'^[a-zA-Z0-9_]{1,25}$')


def collections():
    return {key: dict(value) for key, value in STYLES.items()}


def _login(value):
    value = str(value).strip().lower()
    if not LOGIN.fullmatch(value):
        raise ValueError('Twitch channel logins must contain only letters, numbers, and underscores')
    return value


def parse_names(value):
    """Channel logins from names or public https://twitch.tv/<login> URLs, separated by spaces, commas or semicolons."""
    result = []
    for item in re.split(r'[\s,;]+', (value or '').strip()):
        if not item:
            continue
        if '://' in item:
            parsed = urllib.parse.urlsplit(item)
            if (parsed.scheme != 'https' or parsed.hostname not in ('twitch.tv', 'www.twitch.tv') or parsed.query
                    or parsed.fragment or parsed.username or parsed.password or parsed.port):
                raise ValueError('Use Twitch channel names or public channel URLs')
            item = parsed.path.strip('/')
        if not LOGIN.fullmatch(item):
            raise ValueError('Use Twitch channel names or public channel URLs')
        if item.lower() not in result:
            result.append(item.lower())
    return result


def preferences_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'listening-hub' / 'twitch.json'


def save_preferences(style, logins, excluded=()):
    """Atomically save only channel preferences, with private permissions."""
    if style not in STYLES:
        raise ValueError('Unknown Twitch style')
    names = list(dict.fromkeys(_login(item) for item in logins))
    exclusions = list(dict.fromkeys(_login(item) for item in excluded))
    if len(names) > 100:
        raise ValueError('At most 100 curated Twitch channels per style')
    path = preferences_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('Invalid Twitch preferences')
    # Whitelist keys: never persist API credentials or other arbitrary data.
    clean = {}
    for key in STYLES:
        if key != style and key in data:
            if not isinstance(data[key], list):
                raise ValueError('Invalid Twitch preferences')
            clean[key] = list(dict.fromkeys(_login(item) for item in data[key]))
    clean[style] = names
    clean['excluded'] = exclusions
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.twitch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(clean, output, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def excluded_channels():
    path = preferences_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get('excluded', []), list):
        raise ValueError('Invalid Twitch exclusions')
    return list(dict.fromkeys(_login(item) for item in data.get('excluded', [])))


def curated_channels(style):
    """Read optional user-maintained logins; never read or write credentials here."""
    path = preferences_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get(style, []), list):
        raise ValueError('Twitch preferences must contain lists of channel logins')
    return list(dict.fromkeys(_login(item) for item in data.get(style, [])))


def _credentials(client_id=None, access_token=None):
    if client_id is not None or access_token is not None:
        if not client_id or not access_token:
            raise local_credentials.CredentialError('Both Twitch client ID and access token are required')
        return client_id, access_token
    saved = local_credentials.load()
    return saved.client_id, saved.access_token


def _json_get(request):
    # Custom Authorization headers must not follow a redirected API request.
    return http_fetch.fetch_json(request, 5, 2 * 1024 * 1024)


def _get(endpoint, params, client_id, token):
    url = 'https://api.twitch.tv/helix/' + endpoint + '?' + urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(url, headers={'Client-ID': client_id, 'Authorization': 'Bearer ' + token})
    data = _json_get(request)
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise ValueError('Invalid Twitch response')
    return data['data']


def discover(style, curated=None, client_id=None, access_token=None):
    """Return channels and live certainty. Configured Helix also discovers category streams.

    Without Helix, curated channels remain selectable but their status is unknown.
    API failures never claim an offline result.
    """
    if style not in STYLES:
        raise ValueError('Unknown Twitch style')
    explicit_credentials = client_id is not None or access_token is not None
    names = list(dict.fromkeys(_login(item) for item in (curated_channels(style) if curated is None else curated)))
    # Helix accepts at most 100 user_login values per request.
    if len(names) > 100:
        raise ValueError('At most 100 curated Twitch channels per style')
    def unknown(message, reason='unavailable', http_status=None):
        return {'status': 'unknown', 'message': message, 'reason': reason, 'http_status': http_status, 'curated_status': {name: 'unknown' for name in names}, 'channels': [
            {'login': name, 'name': name, 'url': 'https://www.twitch.tv/' + name, 'live': None}
            for name in names]}
    try:
        client_id, access_token = _credentials(client_id, access_token)
    except local_credentials.CredentialError as error:
        return unknown(str(error), 'configuration')
    if not client_id or not access_token:
        return unknown('Connect Twitch through the CLI to discover live channels; saved channels remain available.', 'configuration')
    try:
        games = _get('games', {'name': STYLES[style]['category']}, client_id, access_token)
        if not games or not games[0].get('id'):
            return unknown('Twitch category could not be resolved; live status is unknown.')
        game_id = str(games[0]['id'])
        streams = _get('streams', {'game_id': game_id, 'first': 100}, client_id, access_token)
        curated_streams = _get('streams', {'user_login': names, 'first': 100}, client_id, access_token) if names else []
        curated_status = {name: 'offline' for name in names}
        for item in curated_streams:
            login = _login(item.get('user_login', ''))
            if login in curated_status:
                curated_status[login] = 'live' if str(item.get('game_id', '')) == game_id else 'style_mismatch'
        channels = {}
        for item in streams + curated_streams:
            if str(item.get('game_id', '')) != game_id:
                continue
            login = _login(item.get('user_login', ''))
            channels[login] = {'login': login, 'name': str(item.get('user_name') or login),
                               'url': 'https://www.twitch.tv/' + login, 'live': True,
                               'title': str(item.get('title') or ''),
                               'curated': login in names, 'category': STYLES[style]['category']}
        return {'status': 'live' if channels else 'offline', 'channels': list(channels.values()),
                'curated_status': curated_status,
                'message': '' if channels else 'No live channels found for this style.'}
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 401:
            if not explicit_credentials:
                try:
                    renewed = local_credentials.refresh(access_token)
                except local_credentials.CredentialError as renewal_error:
                    return unknown(str(renewal_error), 'authentication', 401)
                # Explicit arguments on this one retry prevent renewal loops.
                result = discover(style, names, renewed.client_id, renewed.access_token)
                if result['status'] in ('live', 'offline'):
                    result['authentication'] = 'renewed'
                    result['message'] = 'Saved Twitch login renewed. ' + (str(len(result['channels'])) + ' live choices in this style.' if result['channels'] else 'No live channels found for this style.')
                return result
            return unknown('Twitch login expired or was revoked. Reconnect the Twitch login, then Refresh channels.', 'authentication', 401)
        if error.code == 403:
            return unknown('Twitch denied discovery access. Check the connected application and its permissions.', 'permission', 403)
        if error.code == 429:
            return unknown('Twitch is limiting requests. Wait a moment, then Refresh channels.', 'rate_limit', 429)
        return unknown('Twitch discovery is temporarily unavailable (HTTP ' + str(error.code) + '). Try Refresh channels again.', 'service', error.code)
    except (OSError, ValueError, KeyError):
        return unknown('Could not reach Twitch discovery. Check the connection, then Refresh channels; saved channels remain available.', 'connection')


def choose(result, previous=None, excluded=(), rng=None):
    excluded = {_login(item) for item in excluded}
    options = [item for item in result['channels'] if item['login'] not in excluded]
    if previous:
        options = [item for item in options if item['login'] != previous.lower()]
    return (rng or random).choice(options) if options else None


def _validate_token(token):
    request = urllib.request.Request('https://id.twitch.tv/oauth2/validate',
                                     headers={'Authorization': 'Bearer ' + token})
    data = _json_get(request)
    if not isinstance(data, dict):
        raise ValueError('Invalid Twitch token response')
    return data


def _follow_page(params, client_id, token):
    url = 'https://api.twitch.tv/helix/channels/followed?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={'Client-ID': client_id, 'Authorization': 'Bearer ' + token})
    data = _json_get(request)
    if not isinstance(data, dict) or not isinstance(data.get('data'), list) or not isinstance(data.get('pagination', {}), dict):
        raise ValueError('Invalid Twitch follows response')
    return data


def import_following(profile, client_id=None, access_token=None, max_pages=5):
    """Read followed channels for review; never save or assign them to a style."""
    explicit_credentials = client_id is not None or access_token is not None
    profile = _login(profile)
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 5:
        raise ValueError('Choose between one and five follows pages')
    def needs(message):
        return {'status': 'needs_connection', 'message': message, 'channels': [], 'complete': False}
    try:
        client_id, access_token = _credentials(client_id, access_token)
    except local_credentials.CredentialError as error:
        return needs(str(error))
    if not client_id or not access_token:
        return needs('Connect a Twitch user token with user:read:follows to import follows.')
    try:
        token = _validate_token(access_token)
        if token.get('client_id') != client_id or str(token.get('login') or '').lower() != profile or not token.get('user_id'):
            return needs('The Twitch token must belong to the requested profile and client.')
        if 'user:read:follows' not in (token.get('scopes') or []):
            return needs('The Twitch user token needs the user:read:follows scope.')
        channels = []
        cursor = None
        for _ in range(max_pages):
            params = {'user_id': token['user_id'], 'first': 100}
            if cursor:
                params['after'] = cursor
            page = _follow_page(params, client_id, access_token)
            for item in page['data']:
                login = _login(item.get('broadcaster_login', ''))
                channels.append({'login': login, 'name': str(item.get('broadcaster_name') or login),
                                 'url': 'https://www.twitch.tv/' + login})
            cursor = page.get('pagination', {}).get('cursor')
            if not cursor:
                break
        return {'status': 'follows', 'message': 'Review follows before adding them to a style.',
                'channels': channels, 'complete': not bool(cursor)}
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 401:
            if not explicit_credentials:
                try:
                    renewed = local_credentials.refresh(access_token)
                except local_credentials.CredentialError as renewal_error:
                    return needs(str(renewal_error))
                return import_following(profile, renewed.client_id, renewed.access_token, max_pages)
            return needs('Twitch login expired or was revoked. Reconnect the Twitch user login, then retry the import.')
        return {'status': 'error', 'message': 'Twitch follows could not be retrieved (HTTP ' + str(error.code) + '). Try again later.',
                'channels': [], 'complete': False}
    except (OSError, ValueError, KeyError):
        return {'status': 'error', 'message': 'Twitch follows could not be retrieved.',
                'channels': [], 'complete': False}
