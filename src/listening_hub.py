#!/usr/bin/env python3
"""A small, session-local controller for radio and MPRIS players."""
import argparse
import html
import fcntl
import json
import math
import os
import re
import shutil
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import mpv_ipc

STATE = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'listening-hub'
RUNTIME = Path(os.environ['XDG_RUNTIME_DIR']) / 'listening-hub' if os.environ.get('XDG_RUNTIME_DIR') else None
SOCKET = RUNTIME / 'radio.sock' if RUNTIME else None
CATALOG = os.environ.get('LISTENING_HUB_CATALOG', '')

class HubError(Exception):
    pass

def clean(value):
    return re.sub(r'[\x00-\x1f\x7f\s]+', ' ', str(value or '')).strip()[:240]

def valid_url(url):
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme.lower() in ('http', 'https') and bool(p.hostname) and not p.username and not p.password
    except ValueError:
        return False

def run(args, check=True, timeout=10, **kwargs):
    try:
        p = subprocess.run(args, text=True, capture_output=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HubError('%s: %s' % (args[0], e)) from e
    if check and p.returncode:
        raise HubError(clean(p.stderr) or '%s failed' % args[0])
    return p

def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def save_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
            f.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def state():
    x = load_json(STATE / 'state.json', {})
    return x if isinstance(x, dict) else {}

def update_state(**values):
    s = state()
    s.update(values)
    save_json(STATE / 'state.json', s)

def favorites():
    x = load_json(STATE / 'favorites.json', [])
    return [v for v in x if isinstance(v, dict) and v.get('kind') == 'radio' and valid_url(v.get('url', ''))] if isinstance(x, list) else []

def save_favorite(item):
    if not valid_url(item.get('url', '')):
        raise HubError('Radio URL must be HTTP or HTTPS')
    entries = [x for x in favorites() if x['url'] != item['url']]
    entries.append({'id': 'favorite-' + __import__('hashlib').sha256(item['url'].encode()).hexdigest()[:16], 'name': clean(item.get('name')) or item['url'], 'group': 'Favorites', 'kind': 'radio', 'url': item['url']})
    save_json(STATE / 'favorites.json', entries)
    if item.get('id') and state().get('radio_source_id') == item.get('id'):
        update_state(radio_source_id=entries[-1]['id'])

def sources():
    x = load_json(CATALOG, {}) if CATALOG else {}
    entries = x.get('sources', []) if isinstance(x, dict) else []
    return [v for v in entries if isinstance(v, dict) and all(isinstance(v.get(k), str) and v[k] for k in ('id', 'name', 'group', 'kind'))] + favorites()

def players():
    p = run(['playerctl', '-l'], check=False)
    return sorted(set(x.strip() for x in p.stdout.splitlines() if x.strip() and not x.strip().lower().startswith('listeningroom'))) if p.returncode == 0 else []

def player_info(name):
    p = run(['playerctl', '-p', name, 'metadata', '--format', '{{status}}\t{{artist}}\t{{title}}'], check=False)
    if p.returncode:
        return None
    parts = (p.stdout.rstrip('\n').split('\t') + ['', '', ''])[:3]
    return {'id': name, 'status': clean(parts[0]), 'artist': clean(parts[1]), 'title': clean(parts[2])}

def radio_unit():
    unit = state().get('radio_unit', 'listening-hub-radio.service')
    return unit if re.fullmatch(r'listening-hub-radio(?:-[0-9a-f]{12})?\.service', str(unit)) else 'listening-hub-radio.service'


def radio_socket(current=None):
    name = (state() if current is None else current).get('radio_socket')
    if RUNTIME and isinstance(name, str) and re.fullmatch(r'radio-[0-9a-f]{12}\.sock', name):
        return RUNTIME / name
    return SOCKET


def ipc(command, timeout=2, socket_path=None):
    if SOCKET is None:
        raise HubError('XDG_RUNTIME_DIR is required for radio playback')
    try:
        return mpv_ipc.call(socket_path or radio_socket(), command, timeout)
    except mpv_ipc.Rejected as error:
        raise HubError('mpv: ' + clean(str(error))) from None
    except mpv_ipc.Closed:
        raise HubError('mpv closed its connection') from None

def radio_audio_state(observe=True, current=None):
    """Persist only the owned radio player's real volume and mute state.

    `current` is an already-read state.json; state.json is rewritten only when
    the observed audio differs from what it holds.
    """
    # Without a state snapshot each read resolves the player's socket itself.
    where = {} if current is None else {'socket_path': radio_socket(current)}
    if current is None:
        current = state()
    stored = current.get('radio_audio')
    saved = current.get('radio_audio', {})
    saved = dict(saved) if isinstance(saved, dict) else {}
    volume = saved.get('volume')
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or not 0 <= volume <= 100:
        saved.pop('volume', None)
    if not isinstance(saved.get('muted'), bool):
        saved.pop('muted', None)
    if observe:
        for key, prop in (('volume', 'volume'), ('muted', 'mute')):
            try:
                value = ipc(['get_property', prop], **where)
                if key == 'volume' and not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100:
                    saved[key] = value
                elif key == 'muted' and isinstance(value, bool):
                    saved[key] = value
            except (OSError, ValueError, HubError, AttributeError):
                pass
        if saved and saved != stored:
            update_state(radio_audio=saved)
    return saved


def radio_info(current=None):
    where = {} if current is None else {'socket_path': radio_socket(current)}
    if current is None:
        current = state()
    try:
        pause = ipc(['get_property', 'pause'], **where)
        idle = ipc(['get_property', 'idle-active'], **where)
        if idle:
            return None
        station = clean(current.get('radio_name')) or 'Radio'
        title = clean(ipc(['get_property', 'media-title'], **where))
        return {'id': 'radio', 'status': 'Paused' if pause else 'Playing', 'title': title if title and title != station else '', 'artist': station}
    except (OSError, ValueError, HubError):
        return None

def all_info():
    result = []
    r = radio_info()
    if r:
        result.append(r)
    try:
        v = ambience_module().state('video')
        if v and v.get('status') in ('Playing', 'Paused'):
            result.append({'id': 'video', 'status': v['status'], 'title': clean(v.get('title') or v.get('name')), 'artist': 'Video'})
    except (ImportError, OSError, HubError, AttributeError):
        pass
    for name in players():
        info = player_info(name)
        if info:
            result.append(info)
    return result

def target(infos):
    selected = state().get('selected')
    return next((x for x in infos if x['id'] == selected), None) or next((x for x in infos if x['status'].lower() == 'playing'), None) or (infos[0] if infos else None)

def player_command(name, command):
    if name == 'radio':
        if command == 'play-pause':
            ipc(['cycle', 'pause'])
        elif command in ('pause', 'play'):
            ipc(['set_property', 'pause', command == 'pause'])
        elif command == 'stop':
            radio_audio_state()
            mpv_ipc.stop(run, radio_unit(), check=True)
        else:
            raise HubError('Live radio has no track navigation')
    elif name == 'video':
        ambience_module().control(command, 'video')
    else:
        run(['playerctl', '-p', name, command])

def youtube_video_url(url):
    """Return a single public video URL; reject searches, playlists and credentials."""
    if not valid_url(url):
        raise HubError('Enter a public YouTube video URL')
    p = urllib.parse.urlsplit(url)
    host = p.hostname.lower()
    try:
        port = p.port
    except ValueError:
        raise HubError('Enter a public HTTPS YouTube video URL') from None
    if p.scheme.lower() != 'https' or port not in (None, 443):
        raise HubError('Enter a public HTTPS YouTube video URL')
    if host in ('youtube.com', 'www.youtube.com', 'music.youtube.com', 'm.youtube.com') and p.path == '/watch':
        ids = urllib.parse.parse_qs(p.query).get('v', [])
        video = ids[0] if len(ids) == 1 else ''
    elif host == 'youtu.be':
        video = p.path.removeprefix('/')
    else:
        video = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
        raise HubError('Enter a public YouTube video URL, not a search or playlist')
    return 'https://www.youtube.com/watch?v=' + video

def start_youtube_audio(url):
    """Play one explicitly selected public video as audio, without account access."""
    url = youtube_video_url(url)
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        raise HubError('yt-dlp executable not found')
    start_radio({'id': 'youtube-video', 'name': 'YouTube audio', 'url': url}, ytdlp=ytdlp)

def start_twitch_audio(url):
    """Route an explicitly selected Twitch channel through the Music lane."""
    from audio_layers import _url, LayerError
    try:
        url = _url(url)
    except LayerError as error:
        raise HubError(str(error)) from error
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        raise HubError('yt-dlp executable not found')
    start_radio({'id': 'twitch-channel', 'name': 'Twitch · ' + url.rsplit('/', 1)[-1], 'url': url}, ytdlp=ytdlp)

def _private_runtime():
    if RUNTIME is None or SOCKET is None:
        raise HubError('XDG_RUNTIME_DIR is required for radio playback')
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != os.getuid() or RUNTIME.stat().st_mode & 0o077:
        raise HubError('Radio runtime directory must be private and owned by your user')


def start_radio(item, ytdlp=None, cancel_event=None, fade_seconds=0.6, mpv_options=None, trusted_fd=None, trusted_unit=None):
    _private_runtime()
    descriptor = os.open(RUNTIME / 'radio-start.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise HubError('Radio start cancelled')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.05)
        return _start_radio(item, ytdlp, cancel_event, fade_seconds, mpv_options, trusted_fd, trusted_unit)
    finally:
        os.close(descriptor)


def playback_url(item):
    """Allow public streams and exact catalogue-owned offline sound assets."""
    url = item.get('url', '')
    if valid_url(url):
        return url
    import atmosphere_catalogue
    try:
        local = atmosphere_catalogue.source(item.get('source_id') or item.get('id'))
        expected = Path(local['url'])
        if isinstance(url, str) and Path(url).is_absolute() and Path(url) == expected:
            return str(expected)
    except (OSError, TypeError, ValueError):
        pass
    raise HubError('Choose an HTTP stream or an installed offline sound')


_LOAD_FAILURES = {
    'start': 'Radio player did not start; check the user session',
    'rejected': 'Unable to load radio: ',
    'closed': 'Radio player closed before stream loaded',
    'ended': 'Radio stream could not be opened: ',
    'timeout': 'Radio loading timed out; previous playback has been preserved'}


def _start_radio(item, ytdlp=None, cancel_event=None, fade_seconds=0.6, mpv_options=None, trusted_fd=None, trusted_unit=None):
    """Load a muted candidate before replacing the current owned player.

    A failed/cancelled load stops only its candidate. Committed players retain
    unique identities so a stale cleanup can never stop a newer selection.
    """
    if trusted_fd is None:
        url = playback_url(item)
    else:
        import stat
        if not isinstance(trusted_fd, int) or not stat.S_ISREG(os.fstat(trusted_fd).st_mode):
            raise HubError('Invalid verified audio snapshot')
        url = '/proc/' + str(os.getpid()) + '/fd/' + str(trusted_fd)
    if RUNTIME is None or SOCKET is None:
        raise HubError('XDG_RUNTIME_DIR is required for radio playback')
    mpv = shutil.which('mpv')
    if not mpv:
        raise HubError('mpv executable not found')
    _private_runtime()
    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise HubError('Radio start cancelled')
    cancelled()
    prior = [x['id'] for x in all_info() if x['id'] != 'radio' and x['status'].lower() == 'playing'] if state().get('pause_others_on_radio_start', False) else []
    import audio_devices
    output_options = audio_devices.player_options()
    audio = radio_audio_state()
    old_unit, old_socket = radio_unit(), radio_socket()
    if trusted_unit is not None and (trusted_fd is None or not isinstance(trusted_unit, str) or not re.fullmatch(r'listening-hub-radio-[0-9a-f]{12}\.service', trusted_unit)):
        raise HubError('Invalid verified player identity')
    identity = trusted_unit[len('listening-hub-radio-'):-len('.service')] if trusted_unit else uuid.uuid4().hex[:12]
    unit = 'listening-hub-radio-' + identity + '.service'
    path = RUNTIME / ('radio-' + identity + '.sock')
    committed = False
    try:
        player_options = (['--ytdl=yes', '--script-opts=ytdl_hook-ytdl_path=' + ytdlp,
                           '--ytdl-raw-options=ignore-config=,no-playlist=', '--ytdl-format=bestaudio/best']
                          if ytdlp else ['--ytdl=no'])
        cancelled()
        mpv_ipc.spawn(run, unit, mpv_ipc.audio_player(mpv, path, 'ListeningRoomMusic')
                      + player_options + output_options + list(mpv_options or []))
        try:
            mpv_ipc.load(path, url, time.monotonic() + (35 if ytdlp else 12), cancelled)
        except mpv_ipc.LoadError as failure:
            raise HubError(_LOAD_FAILURES[failure.reason] + clean(failure.detail)) from None
        cancelled()
        # Preflight the private command path while the previous player survives.
        ipc(['set_property', 'volume', 0], socket_path=path)
        ipc(['set_property', 'mute', True], socket_path=path)
        cancelled()
        if not mpv_ipc.ensure_stopped(run, old_unit):
            raise HubError('The existing radio could not be stopped; its controls have been preserved')
        update_state(selected='radio', radio_name=clean(item.get('name')), radio_source_id=clean(item.get('id')),
                     radio_unit=unit, radio_socket=path.name)
        committed = True
        if old_socket and old_socket != path:
            old_socket.unlink(missing_ok=True)
        cancelled()
        ipc(['set_property', 'mute', bool(audio.get('muted', False))], socket_path=path)
        level = audio.get('volume', 100)
        steps = max(1, min(30, int(float(fade_seconds) * 20)))
        for step in range(1, steps + 1):
            cancelled()
            ipc(['set_property', 'volume', level * step / steps], socket_path=path)
            if steps > 1:
                if cancel_event is not None:
                    cancel_event.wait(float(fade_seconds) / steps)
                else:
                    time.sleep(float(fade_seconds) / steps)
    except (OSError, ValueError, HubError) as error:
        mpv_ipc.stop(run, unit)
        path.unlink(missing_ok=True)
        suffix = '' if committed else ' Previous playback has been preserved.'
        raise HubError('Could not start radio: ' + str(error) + suffix) from error
    failed = []
    for name in prior:
        try:
            player_command(name, 'pause')
        except HubError:
            failed.append(name)
    if failed:
        notify('Radio started, but could not pause: ' + ', '.join(failed))


def play(item):
    kind = item.get('kind')
    if kind == 'radio':
        import session_client
        session_client.request('music', {'item': item})
    elif kind == 'search':
        query = item.get('query')
        if not isinstance(query, str) or not clean(query):
            raise HubError('Invalid search query in catalogue')
        station_menu(search_radio(clean(query)))
    elif kind in ('web', 'discovery'):
        if not valid_url(item.get('url', '')):
            raise HubError('Invalid web URL')
        run(['xdg-open', item['url']])
    elif kind == 'app':
        command = item.get('command')
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise HubError('Invalid app command in catalogue')
        try:
            subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise HubError('Could not launch app: %s' % e) from e
    else:
        raise HubError('Unknown source type: ' + clean(kind))

def ambience_module():
    # Kept separate so the compact controller does not depend on GTK.
    import ambience
    return ambience

def system_volume():
    try:
        output = run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@']).stdout
        match = re.search(r'Volume:\s*([0-9.]+)', output)
        if not match:
            return {'percent': None, 'muted': None}
        return {'percent': max(0, min(100, round(float(match.group(1)) * 100))), 'muted': '[MUTED]' in output}
    except (HubError, ValueError):
        return {'percent': None, 'muted': None}

def player_volume(name):
    try:
        if name == 'radio':
            value = float(ipc(['get_property', 'volume']))
        elif name == 'video':
            value = ambience_module().state('video').get('volume')
            if value is None:
                return None
            value = float(value)
        else:
            value = float(run(['playerctl', '-p', name, 'volume']).stdout.strip()) * 100
        if not 0 <= value <= 100:
            return None
        return round(value)
    except (HubError, OSError, ValueError, TypeError, ImportError, AttributeError):
        return None

def volume_value(value, current):
    text = str(value).strip()
    if not re.fullmatch(r'[+-]?\d+', text):
        raise HubError('Volume must be 0–100 or a signed adjustment such as +5')
    number = int(text)
    if text.startswith(('+', '-')):
        if current is None:
            raise HubError('Current volume is unavailable')
        return max(0, min(100, current + number))
    if not 0 <= number <= 100:
        raise HubError('Volume must be between 0 and 100')
    return number

def selected_target(target_id=None):
    infos = all_info()
    if target_id is not None:
        selected = next((x for x in infos if x['id'] == target_id), None)
        if not selected:
            raise HubError('Selected player is no longer available: ' + clean(target_id))
        return selected
    selected = target(infos)
    if not selected:
        raise HubError('No active player')
    return selected

def set_volume(value, master=False, target_id=None):
    if master:
        level = volume_value(value, system_volume()['percent'])
        run(['wpctl', 'set-volume', '-l', '1.0', '@DEFAULT_AUDIO_SINK@', str(level / 100)])
        return level
    current = selected_target(target_id)
    name = current['id']
    level = volume_value(value, player_volume(name))
    if name == 'radio':
        ipc(['set_property', 'volume', level])
        audio = radio_audio_state()
        audio['volume'] = level
        update_state(radio_audio=audio)
    elif name == 'video':
        ambience_module().control('volume', 'video', level)
    else:
        run(['playerctl', '-p', name, 'volume', str(level / 100)])
    return level

def mute(master=False, target_id=None):
    if master:
        run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', 'toggle'])
        return
    current = selected_target(target_id)
    name = current['id']
    if name == 'radio':
        audio = radio_audio_state()
        ipc(['cycle', 'mute'])
        audio['muted'] = not audio.get('muted', False)
        update_state(radio_audio=audio)
    elif name == 'video':
        ambience_module().control('mute', 'video')
    else:
        volume = player_volume(name)
        if volume is None:
            raise HubError('Player volume is unavailable')
        volumes = state().get('unmute_volumes', {})
        if not isinstance(volumes, dict):
            volumes = {}
        if volume:
            volumes[name] = volume
            update_state(unmute_volumes=volumes)
            run(['playerctl', '-p', name, 'volume', '0'])
        else:
            previous = volumes.get(name, 50)
            if not isinstance(previous, (int, float)) or not 1 <= previous <= 100:
                previous = 50
            run(['playerctl', '-p', name, 'volume', str(previous / 100)])

PREFERENCE_KEYS = ('pause_others_on_radio_start',)

def preferences():
    current = state()
    return {'pause_others_on_radio_start': bool(current.get('pause_others_on_radio_start', False))}

def profiles():
    data = load_json(STATE / 'profiles.json', {})
    return data if isinstance(data, dict) else {}

def private_favorite(item):
    try:
        parsed = urllib.parse.urlsplit(item['url'])
        return bool(parsed.username or parsed.password) or any(
            marker in key.casefold()
            for key, _ in urllib.parse.parse_qsl(parsed.query) + urllib.parse.parse_qsl(parsed.fragment)
            for marker in ('token', 'auth', 'key', 'secret', 'signature', 'password', 'credential'))
    except (KeyError, TypeError, ValueError):
        return True

def save_profile(name, force=False):
    name = clean(name)
    if not name:
        raise HubError('Profile name is required')
    data = profiles()
    if name in data and not force:
        raise HubError('Profile already exists; use --force to replace it')
    # Preserve ordinary radio query parameters; do not duplicate access tokens.
    data[name] = {'preferences': preferences(), 'favorites': [x for x in favorites() if not private_favorite(x)]}
    save_json(STATE / 'profiles.json', data)

def ensure_baseline():
    if 'default' not in profiles():
        save_profile('default')

def restore_profile(name):
    profile = profiles().get(name)
    if not isinstance(profile, dict):
        raise HubError('Profile not found: ' + clean(name))
    prefs = profile.get('preferences', {})
    favs = profile.get('favorites', [])
    if not isinstance(prefs, dict) or not isinstance(favs, list):
        raise HubError('Invalid profile')
    valid_favs = [x for x in favs if isinstance(x, dict) and x.get('kind') == 'radio' and isinstance(x.get('id'), str) and isinstance(x.get('name'), str) and valid_url(x.get('url', ''))]
    update_state(**{k: bool(prefs[k]) for k in PREFERENCE_KEYS if k in prefs})
    protected = [x for x in favorites() if private_favorite(x)]
    save_json(STATE / 'favorites.json', valid_favs + protected)

BUILTIN_SCENES = [
    {'id': 'dark-academia', 'name': 'Dark academia', 'source_id': 'dark-academia-piano', 'ambience_id': 'moonlit-lake'},
    {'id': 'night-drive', 'name': 'Night drive', 'source_id': 'truckersfm', 'ambience_id': 'northern-lake'},
    {'id': 'finnish-discovery', 'name': 'Finnish discovery', 'source_id': 'ylex', 'ambience_id': 'northern-lake'},
    {'id': 'focus', 'name': 'Focus', 'source_id': 'somafm-drone-zone', 'ambience_id': 'northern-lake'},
]

def scenes():
    data = load_json(STATE / 'scenes.json', [])
    return BUILTIN_SCENES + ([x for x in data if isinstance(x, dict) and isinstance(x.get('id'), str) and isinstance(x.get('name'), str)] if isinstance(data, list) else [])

def save_scene(name):
    name = clean(name)
    if not name:
        raise HubError('Scene name is required')
    selected = target(all_info())
    if not selected or selected['id'] != 'radio':
        raise HubError('Select an active radio station before saving a scene')
    source_id = state().get('radio_source_id')
    if source_id == 'twitch-channel':
        raise HubError('Keep this Twitch channel in a Channels pool to replay it; radio scenes need a station.')
    if source_id == 'youtube-video':
        raise HubError('YouTube audio is opened from its video URL. Save that URL to replay it; radio scenes need a station.')
    if not source_id:
        # Version 1's already-running radio stored its label but no catalogue
        # ID. Adopt an unambiguous match without restarting the stream.
        matches = [x for x in sources() if x['kind'] == 'radio' and x['name'] == state().get('radio_name')]
        if len(matches) == 1:
            source_id = matches[0]['id']
            update_state(radio_source_id=source_id)
    if not any(x['id'] == source_id and x['kind'] == 'radio' for x in sources()):
        raise HubError('Save this station as a favorite before saving a scene')
    scene_id = 'user-' + __import__('hashlib').sha256(name.encode()).hexdigest()[:16]
    entry = {'id': scene_id, 'name': name, 'source_id': source_id, 'volume': player_volume('radio')}
    try:
        ambient = ambience_module().state('ambience')
        preset = next((x for x in ambience_module().presets() if isinstance(x, dict) and isinstance(ambient, dict) and x.get('url') == ambient.get('url')), None)
        if preset:
            entry['ambience_id'] = clean(preset['id'])
            if ambient.get('placement') in ('9', 'here', 'pin'):
                entry['placement'] = ambient['placement']
    except (ImportError, OSError, HubError, AttributeError):
        pass
    saved = [x for x in scenes() if x.get('id', '').startswith('user-') and x['id'] != scene_id]
    save_json(STATE / 'scenes.json', saved + [entry])
    return entry

def play_scene(scene_id):
    scene = next((x for x in scenes() if x['id'] == scene_id), None)
    if not scene:
        raise HubError('Scene not found: ' + clean(scene_id))
    item = next((x for x in sources() if x['id'] == scene.get('source_id')), None)
    if not item:
        raise HubError('Scene source is unavailable')
    if scene.get('ambience_id') and not any(x.get('id') == scene['ambience_id'] for x in ambience_module().presets()):
        raise HubError('Scene ambience preset is unavailable')
    recipe = {'id': 'scene-' + scene['id'], 'room_id': None, 'name': scene['name'],
              'music': {'source_id': item['id'], 'volume': scene.get('volume', 50), 'muted': False},
              'atmosphere': {'kind': 'none', 'volume': 20, 'muted': False},
              'picture': {'preset_id': scene.get('ambience_id'), 'enabled': bool(scene.get('ambience_id')),
                          'placement': scene.get('placement', '9'), 'still': False}}
    import session_client
    return session_client.request('enter', {'recipe': recipe})


def transport(player_id, command):
    """User transport entry point; backend primitives remain explicitly owned."""
    if player_id == 'radio':
        import session_client
        return session_client.request('layer', {'layer': 'music', 'action': command})
    return player_command(player_id, command)

def inspect_state():
    ensure_baseline()
    infos = all_info()
    for entry in infos:
        entry['volume'] = player_volume(entry['id'])
        entry['can_next'] = entry['id'] not in ('radio', 'video')
        entry['can_previous'] = entry['id'] not in ('radio', 'video')
    selected = target(infos)
    try:
        ambience = ambience_module().state('ambience')
        video = ambience_module().state('video')
    except (ImportError, OSError, HubError, AttributeError):
        ambience = video = None
    return {'players': infos, 'selected': selected, 'selected_id': selected['id'] if selected else None,
            'system_volume': system_volume(), 'preferences': preferences(), 'sources': sources(),
            'favorites': favorites(), 'profiles': [{'id': k, 'name': k} for k in sorted(profiles())],
            'scenes': scenes(), 'ambience': ambience, 'video': video}

def notify(message):
    try:
        run(['notify-send', 'Listening Hub', clean(message)], check=False)
    except HubError:
        pass

def choose(prompt, options, custom=False):
    if not options and not custom:
        raise HubError('No entries available')
    labels = [clean(label) for label, _ in options]
    args = ['rofi', '-dmenu', '-i', '-p', prompt]
    if not custom:
        # Return the original row index: directory stations can legitimately
        # share a name and country, but must remain individually selectable.
        args += ['-no-custom', '-format', 'i']
    p = run(args, check=False, timeout=None, input='\n'.join(labels) + '\n')
    if p.returncode:
        return None
    answer = p.stdout.rstrip('\n')
    if custom:
        return answer
    try:
        index = int(answer)
        return options[index][1] if 0 <= index < len(options) else None
    except ValueError:
        return None

# ISO 3166-1 alpha-2 assigned codes, for input validation without a runtime dependency.
COUNTRY_CODES = frozenset('AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW'.split())


def search_radio(query, *, country=None, language=None, tag=None, cache=True):
    def term(value, label):
        if value is None:
            return ''
        if not isinstance(value, str) or len(value) > 240 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise HubError('Invalid ' + label + ' search value')
        return value.strip()

    query = term(query, 'station name')
    filters = {'countrycode': term(country, 'country').upper(),
               'language': term(language, 'language'), 'tag': term(tag, 'tag')}
    key, sep, value = query.partition(':')
    fields = {'country': 'countrycode', 'tag': 'tag', 'language': 'language'}
    if sep and key.lower() in fields:
        field = fields[key.lower()]
        value = term(value, key.lower())
        if not value:
            raise HubError('Enter a station name or a search value')
        if filters[field] and filters[field].casefold() != value.casefold():
            raise HubError('Conflicting ' + key.lower() + ' search values')
        filters[field] = value.upper() if field == 'countrycode' else value
    elif query:
        filters['name'] = query
    if filters['countrycode'] and filters['countrycode'] not in COUNTRY_CODES:
        raise HubError('Country must be an ISO 3166-1 alpha-2 code')
    filters = {key: value for key, value in filters.items() if value}
    try:
        addresses = socket.getaddrinfo('all.api.radio-browser.info', 443, type=socket.SOCK_STREAM)
        hosts = sorted({row[4][0] for row in addresses})
        # Reverse DNS gives the public hostnames needed for TLS certificate validation.
        names = []
        for address in hosts:
            try:
                name = socket.gethostbyaddr(address)[0].rstrip('.')
                if name.endswith('.api.radio-browser.info'):
                    names.append(name)
            except OSError:
                continue
        hosts = sorted(set(names))
    except OSError:
        hosts = []
    if not hosts:
        hosts = ['de1.api.radio-browser.info', 'nl1.api.radio-browser.info']
    random.shuffle(hosts)
    params = urllib.parse.urlencode(dict(filters, limit=50, hidebroken='true', order='clickcount', reverse='true'))
    errors = []
    data = None
    for host in hosts:
        url = 'https://' + host + '/json/stations/search?' + params
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'listening-hub/1.0'})
            with urllib.request.urlopen(request, timeout=5) as r:
                data = json.load(r)
            if not isinstance(data, list):
                raise ValueError('invalid response')
            break
        except (OSError, ValueError) as e:
            errors.append('%s: %s' % (host, clean(e)))
    if data is None or not isinstance(data, list):
        raise HubError('Radio Browser search failed: ' + '; '.join(errors))
    result = []
    for row in data:
        if not isinstance(row, dict) or not valid_url(row.get('url_resolved', '')):
            continue
        website = row.get('homepage', '')
        tags = row.get('tags', '')
        result.append({'id': 'browser-' + clean(row.get('stationuuid')), 'name': clean(row.get('name')) or 'Unnamed station', 'group': 'Search results', 'kind': 'radio', 'url': row['url_resolved'], 'country': clean(row.get('country')), 'countrycode': clean(row.get('countrycode')), 'language': clean(row.get('language')), 'tags': [clean(tag) for tag in tags.split(',') if clean(tag)] if isinstance(tags, str) else [], 'website': website if valid_url(website) else ''})
        if len(result) >= 50:
            break
    if cache:
        save_json(STATE / 'search-cache.json', result)
    return result

def station_menu(entries):
    if not entries:
        notify('No stations found')
        return
    item = choose('Station', [(clean(x['name']) + (' — ' + clean(x.get('country')) if x.get('country') else ''), x) for x in entries])
    if item:
        station_actions(item)

def station_actions(item):
    action = choose(clean(item['name']), [('Play', 'play'), ('Save favorite', 'save'), ('Remove favorite', 'remove')] if item['id'].startswith('favorite-') else [('Play', 'play'), ('Save favorite', 'save')])
    if action == 'play':
        play(item)
    elif action == 'save':
        save_favorite(item)
        notify('Saved ' + item['name'])
    elif action == 'remove':
        save_json(STATE / 'favorites.json', [x for x in favorites() if x['url'] != item['url']])

def menu():
    entries = sources()
    groups = sorted(set(x['group'] for x in entries))
    action = choose('Listening Hub', [('Open listening room', 'room'), ('Current controls', 'controls'), ('Volume', 'volume'), ('Scenes', 'scenes'), ('Profiles', 'profiles'), ('Select player', 'players'), ('Search world radio', 'search'), ('Custom radio URL', 'custom'), ('Recent search results', 'recent'), ('When starting radio: ' + ('pause other players' if state().get('pause_others_on_radio_start', False) else 'keep other players playing'), 'policy')] + [('Browse: ' + g, ('group', g)) for g in groups] + [(clean(x['name']) + '  [' + clean(x['group']) + ']  ' + ' '.join(clean(t) for t in x.get('tags', []) if isinstance(t, str)), ('source', x)) for x in entries])
    if not action:
        return
    if isinstance(action, tuple) and action[0] == 'source':
        item = action[1]
        station_actions(item) if item['kind'] == 'radio' else play(item)
    elif isinstance(action, tuple):
        items = [x for x in entries if x['group'] == action[1]]
        item = choose(action[1], [(x['name'], x) for x in items])
        if item:
            station_actions(item) if item['kind'] == 'radio' else play(item)
    elif action == 'room':
        try:
            subprocess.Popen(['listening-room'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise HubError('Could not open listening room: %s' % e) from e
    elif action == 'volume':
        option = choose('Volume', [('Selected player volume', 'player'), ('Master volume', 'master'), ('Toggle player mute', 'mute'), ('Toggle master mute', 'master-mute')])
        if option in ('player', 'master'):
            level = choose('Volume 0–100 or +5 / -5', [], custom=True)
            if level:
                set_volume(level, option == 'master')
        elif option in ('mute', 'master-mute'):
            mute(option == 'master-mute')
    elif action == 'profiles':
        ensure_baseline()
        option = choose('Profiles', [('Save current settings', 'save')] + [('Restore ' + x, ('restore', x)) for x in sorted(profiles())])
        if option == 'save':
            name = choose('Profile name', [], custom=True)
            if name:
                save_profile(name)
        elif isinstance(option, tuple):
            restore_profile(option[1])
    elif action == 'scenes':
        option = choose('Scenes', [('Save current radio scene', 'save')] + [(x['name'], ('play', x['id'])) for x in scenes()])
        if option == 'save':
            name = choose('Scene name', [], custom=True)
            if name:
                save_scene(name)
        elif isinstance(option, tuple):
            play_scene(option[1])
    elif action == 'players':
        select_player()
    elif action == 'controls':
        current = target(all_info())
        if not current:
            raise HubError('No active player')
        options = [('Play / pause', 'play-pause'), ('Stop', 'stop')]
        if current['id'] != 'radio':
            options += [('Next track', 'next'), ('Previous track', 'previous')]
        command = choose('Control ' + current['id'], options)
        if command:
            transport(current['id'], command)
    elif action == 'policy':
        update_state(pause_others_on_radio_start=not state().get('pause_others_on_radio_start', False))
        notify('Radio start: ' + ('pause other players' if state().get('pause_others_on_radio_start') else 'keep other players playing'))
    elif action == 'search':
        query = choose('Name, country:FI, tag:genre or language:name', [], custom=True)
        if query and clean(query):
            station_menu(search_radio(clean(query)))
    elif action == 'recent':
        cached = load_json(STATE / 'search-cache.json', [])
        station_menu([x for x in cached if isinstance(x, dict) and isinstance(x.get('id'), str) and isinstance(x.get('name'), str) and valid_url(x.get('url', ''))] if isinstance(cached, list) else [])
    elif action == 'custom':
        url = choose('Radio stream URL', [], custom=True)
        if not url:
            return
        if not valid_url(url):
            raise HubError('Radio URL must be HTTP or HTTPS')
        name = choose('Station name', [], custom=True)
        if name:
            station_actions({'id': 'custom', 'name': clean(name), 'kind': 'radio', 'url': url})

def select_player():
    infos = all_info()
    if not infos:
        notify('No active players found')
        return
    selected = choose('Select player', [('%s — %s — %s' % (x['id'], x['status'], x['title']), x['id']) for x in infos])
    if selected:
        update_state(selected=selected)

def status():
    current = target(all_info())
    if not current:
        return {'text': '󰎆', 'tooltip': 'Listening Hub: no active player', 'alt': 'stopped', 'class': 'stopped'}
    artist, title = current['artist'], current['title']
    label = ' — '.join(x for x in (artist, title) if x) or current['id']
    playing = current['status'].lower() == 'playing'
    return {'text': ('▶ ' if playing else 'Ⅱ ') + html.escape(label), 'tooltip': html.escape('%s: %s\n%s' % (clean(current['id']), clean(current['status']), label)), 'alt': 'playing' if playing else 'paused', 'class': 'playing' if playing else 'paused'}

def main():
    parser = argparse.ArgumentParser(description='Control radio and desktop media players')
    parser.add_argument('action', choices=['status', 'menu', 'toggle', 'next', 'previous', 'stop', 'players', 'play', 'doctor', 'config', 'select-player', 'inspect', 'volume', 'master-volume', 'mute', 'master-mute', 'profile', 'scene', 'ambience', 'video'])
    parser.add_argument('id', nargs='?')
    parser.add_argument('value', nargs='?')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'status':
            print(json.dumps(status(), ensure_ascii=False))
        elif args.action == 'menu':
            menu()
        elif args.action == 'inspect':
            print(json.dumps(inspect_state(), ensure_ascii=False))
        elif args.action in ('volume', 'master-volume'):
            if args.id is None:
                raise HubError('Volume value is required')
            set_volume(args.id, args.action == 'master-volume')
        elif args.action in ('mute', 'master-mute'):
            mute(args.action == 'master-mute')
        elif args.action == 'profile':
            if args.id == 'save' and args.value:
                if args.value != 'default':
                    ensure_baseline()
                save_profile(args.value, args.force)
            elif args.id == 'restore' and args.value:
                ensure_baseline()
                restore_profile(args.value)
            else:
                raise HubError('Usage: profile save|restore NAME [--force]')
        elif args.action == 'scene':
            if args.id == 'save' and args.value:
                save_scene(args.value)
            elif args.id == 'play' and args.value:
                play_scene(args.value)
            else:
                raise HubError('Usage: scene save NAME | scene play ID')
        elif args.action in ('ambience', 'video'):
            module = ambience_module()
            mode = args.action
            if args.id == 'start' and args.value:
                preset = next((x for x in module.presets() if x.get('id') == args.value), None)
                module.start(preset or args.value, mode)
                if mode == 'video':
                    update_state(selected='video')
            elif args.id in ('stop', 'toggle'):
                module.control('play-pause' if args.id == 'toggle' else args.id, mode)
            elif args.id in ('here', 'workspace', 'pin'):
                module.place('9' if args.id == 'workspace' else args.id, mode)
            else:
                raise HubError('Usage: %s start PRESET_OR_URL | stop | toggle | here | workspace | pin' % mode)
        elif args.action == 'select-player':
            select_player()
        elif args.action == 'players':
            print(json.dumps(all_info(), ensure_ascii=False))
        elif args.action == 'doctor':
            for name in ('playerctl', 'mpv', 'systemd-run', 'systemctl', 'rofi', 'notify-send', 'xdg-open'):
                print('%s: %s' % (name, shutil.which(name) or 'missing'))
            print('pause others on radio start: %s' % ('on' if state().get('pause_others_on_radio_start', False) else 'off'))
            print('catalogue: %s' % (CATALOG or 'not configured'))
            print('runtime directory: %s' % os.environ.get('XDG_RUNTIME_DIR', 'not configured'))
        elif args.action == 'config':
            if args.id != 'pause-others' or args.value not in ('on', 'off'):
                raise HubError('Usage: listening-hub config pause-others on|off')
            update_state(pause_others_on_radio_start=args.value == 'on')
        elif args.action == 'play':
            item = next((x for x in sources() if x['id'] == args.id), None)
            if not item:
                raise HubError('Source not found: ' + clean(args.id))
            play(item)
        else:
            current = target(all_info())
            if not current:
                if args.action == 'toggle':
                    menu()
                    return 0
                raise HubError('No active player')
            transport(current['id'], 'play-pause' if args.action == 'toggle' else args.action)
    except (HubError, OSError, ValueError, ImportError) as e:
        if args.action == 'status':
            print(json.dumps({'text': '󰎆', 'tooltip': html.escape('Listening Hub: ' + clean(e)), 'alt': 'error', 'class': 'error'}))
        else:
            print('listening-hub: %s' % e, file=sys.stderr)
            notify(str(e))
            return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
