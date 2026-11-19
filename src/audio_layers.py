"""A private, independent mpv audio layer; never controls other players."""
import json
import fcntl
from contextlib import contextmanager
import math
import os
import re
from pathlib import Path
import shutil
import socket  # noqa: F401  (tests patch socket.socket through this name)
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid

import atmosphere_catalogue
import mpv_ipc

UNIT = 'listening-hub-overlay.service'
_mix_generation = 0
_play_generation = 0
_start_lock = threading.Lock()
_commit_lock = threading.RLock()


class LayerError(ValueError):
    pass


def _runtime_dir():
    base = os.environ.get('XDG_RUNTIME_DIR')
    if not base:
        raise LayerError('Open the audio layer from your graphical session')
    path = Path(base) / 'listening-hub'
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise LayerError('The audio layer runtime directory must be private')
    return path


def _player():
    """Read only a narrowly named owned player; support the previous fixed unit."""
    if not os.environ.get('XDG_RUNTIME_DIR'):
        return {'unit': UNIT, 'socket': 'overlay.sock'}
    try:
        data = json.loads((_runtime_dir() / 'overlay-player.json').read_text())
        def valid(item):
            if not isinstance(item, dict):
                return False
            identity = re.fullmatch(r'listening-hub-overlay-([a-f0-9]{12})\.service', item.get('unit', ''))
            return ((identity and item.get('socket') == 'overlay-' + identity[1] + '.sock') or
                    (item.get('unit') == UNIT and item.get('socket') == 'overlay.sock'))
        if valid(data):
            result = {'unit': data['unit'], 'socket': data['socket']}
            retired = data.get('retiring', [])
            if isinstance(retired, list):
                result['retiring'] = [{'unit': x['unit'], 'socket': x['socket']} for x in retired[:8]
                                      if valid(x) and x['unit'] != data['unit']]
            return result
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {'unit': UNIT, 'socket': 'overlay.sock'}


def _runtime():
    return _runtime_dir() / _player()['socket']


def _save_player(player):
    directory = _runtime_dir()
    fd, temporary = tempfile.mkstemp(prefix='.overlay-player-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(player, output)
        os.replace(temporary, directory / 'overlay-player.json')
    finally:
        Path(temporary).unlink(missing_ok=True)


def _epoch():
    try:
        return (_runtime_dir() / 'overlay-epoch').read_text()[:64]
    except OSError:
        return ''


def _invalidate_shared():
    if not os.environ.get('XDG_RUNTIME_DIR'):
        return None
    value = uuid.uuid4().hex
    directory = _runtime_dir()
    fd, temporary = tempfile.mkstemp(prefix='.overlay-epoch-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(value)
        os.replace(temporary, directory / 'overlay-epoch')
    finally:
        Path(temporary).unlink(missing_ok=True)
    return value


@contextmanager
def _commit():
    with _commit_lock:
        if not os.environ.get('XDG_RUNTIME_DIR'):
            yield
            return
        descriptor = os.open(_runtime_dir() / 'overlay-control.lock',
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


def _run(args, check=True):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LayerError('Audio layer service unavailable') from error
    if check and result.returncode:
        raise LayerError('Audio layer service command failed')
    return result


def _ipc(command, socket_path=None):
    try:
        return mpv_ipc.call(socket_path or _runtime(), command, 1)
    except mpv_ipc.Rejected:
        raise LayerError('Audio layer player rejected the command') from None
    except mpv_ipc.Closed:
        raise LayerError('Audio layer player closed') from None
    except (OSError, ValueError) as error:
        raise LayerError('Audio layer player is unavailable') from error


def _volume(value):
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise LayerError('Choose a volume from 0 to 100') from None
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise LayerError('Choose a volume from 0 to 100')
    return value


def _settings_path():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'listening-hub' / 'overlay-audio.json'


def _source_path():
    return _settings_path().with_name('overlay-source.json')


def _source():
    try:
        value = json.loads(_source_path().read_text())
        if not isinstance(value, dict):
            return {}
        if value.get('kind') == 'local':
            item = next((x for x in atmosphere_catalogue.presets() if x['id'] == value.get('source_id')), None)
            if item:
                return {key: item[key] for key in ('kind', 'source_id', 'name', 'url', 'outdoor')} | {
                    'window_open': _volume(value.get('window_open', 100))}
        elif value.get('kind') == 'twitch':
            url = _url(value.get('url'))
            identity = url.rstrip('/').rsplit('/', 1)[-1]
            return {'kind': 'twitch', 'source_id': identity, 'name': 'Twitch · ' + identity, 'url': url}
        elif value.get('kind') == 'radio':
            return {key: value[key] for key in ('kind', 'source_id', 'url', 'name', 'reference', 'resolver') if key in value}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _save_source(value):
    path = _source_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.overlay-source-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def presets():
    return atmosphere_catalogue.presets()


def _window_gain(source):
    return .18 + .82 * source.get('window_open', 100) / 100 if source.get('outdoor') else 1.0


def _window_filter(source):
    # The packaged mono beds are 24 kHz: stay below their 12 kHz Nyquist limit.
    cutoff = 900 + 9100 * (source.get('window_open', 100) / 100) ** 2
    return '@room-window:lavfi=[lowpass=f=' + str(round(cutoff)) + ']'


def _settings():
    try:
        data = json.loads(_settings_path().read_text())
        if not isinstance(data, dict) or not isinstance(data.get('muted'), bool):
            raise ValueError('Invalid overlay settings')
        return {'volume': _volume(data.get('volume')), 'muted': data['muted']}
    except (OSError, ValueError, TypeError):
        return {'volume': 20.0, 'muted': False}


def _save_settings(settings):
    path = _settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.overlay-audio-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump({'volume': _volume(settings['volume']), 'muted': bool(settings['muted'])}, output)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _current_settings():
    current = state()
    if current is None:
        return _settings()
    return {'volume': _volume(current['volume']), 'muted': bool(current['muted'])}


def _url(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise LayerError('Choose a public Twitch channel URL')
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise LayerError('Choose a public Twitch channel URL') from None
    if (parsed.scheme != 'https' or parsed.hostname not in ('twitch.tv', 'www.twitch.tv')
            or parsed.username or parsed.password or port or parsed.query or parsed.fragment
            or not re.fullmatch(r'/[A-Za-z0-9_]{1,25}/?', parsed.path)):
        raise LayerError('Choose a public Twitch channel URL')
    return value


def state():
    try:
        if _ipc(['get_property', 'idle-active']):
            return None
        result = {'status': 'Paused' if _ipc(['get_property', 'pause']) else 'Playing',
                  'title': _ipc(['get_property', 'media-title']),
                  'volume': _ipc(['get_property', 'volume']),
                  'muted': _ipc(['get_property', 'mute'])}
        source = _source()
        if source:
            result.update(source)
            if source.get('kind') == 'local':
                result['effective_volume'] = result['volume']
                result['volume'] = _settings()['volume']
                result['title'] = source['name']
        if _player().get('retiring'):
            result['warning'] = 'Previous background player is paused; Stop will retry cleanup'
        return result
    except LayerError:
        return None


def remember_audio():
    """Snapshot this owned player's actual mix before an external stop."""
    _save_settings(_current_settings())


def audio_settings():
    """Read the remembered layer mix without probing or starting a player."""
    return _settings() | _source()


def start_layer(url, volume=None, timeout=35, cancel_event=None):
    return _start_audio(url, volume, timeout, cancel_event)


def start_source(source, volume=None, timeout=35, cancel_event=None):
    """Play a validated direct source in the independently owned audio lane."""
    import listening_hub as hub
    source = dict(source)
    if source.get('reference'):
        import online_services
        source = online_services.prepare_playback(source['reference'], cancel_event=cancel_event)
    url = source.get('url')
    if not hub.valid_url(url):
        raise LayerError('Choose a direct audio source')
    query_keys = [key.casefold() for key, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)]
    if not source.get('reference') and (hub.private_favorite(source) or any(
            any(marker in key for marker in ('auth', 'token', 'key', 'secret', 'signature', 'password', 'credential'))
            for key in query_keys)):
        raise LayerError('Private audio URLs cannot be saved in the background lane')
    _start_audio(url, volume, timeout, cancel_event, direct=source)
    return source


def media_details():
    """Probe only the owned overlay player; absence is not evidence of EOF."""
    details = {}
    for key, prop in (('eof_reached', 'eof-reached'), ('position', 'time-pos'), ('duration', 'duration')):
        try:
            details[key] = _ipc(['get_property', prop])
        except LayerError:
            pass
    return details


def start_local(preset_id, volume=None, timeout=12, cancel_event=None, window_open=None):
    if cancel_event is not None and cancel_event.is_set():
        raise LayerError('Audio layer start cancelled')
    try:
        item = atmosphere_catalogue.source(preset_id)
    except ValueError as error:
        raise LayerError(str(error)) from error
    previous = _source()
    openness = previous.get('window_open', 100) if window_open is None else _volume(window_open)
    item = dict(item, window_open=openness)
    return _start_audio(item['url'], volume, timeout, cancel_event, local=item)


def _start_audio(url, volume, timeout, cancel_event, local=None, direct=None):
    global _play_generation
    if cancel_event is not None and cancel_event.is_set():
        raise LayerError('Audio layer start cancelled')
    url = local['url'] if local else (url if direct else _url(url))
    if volume is not None:
        volume = _volume(volume)
    needs_resolver = not local and (not direct or direct.get('resolver') or direct.get('source_id') in ('twitch-channel', 'youtube-video'))
    mpv, resolver = shutil.which('mpv'), shutil.which('yt-dlp') if needs_resolver else None
    if not mpv or (needs_resolver and not resolver):
        raise LayerError('mpv and yt-dlp are required for this audio layer')
    import audio_devices
    try:
        device_options = audio_devices.player_options()
    except audio_devices.DeviceError as error:
        raise LayerError(str(error)) from error
    # Serialize candidate starts. Each owns a unique service/socket so failed
    # cleanup cannot stop the previous player or a newer replacement.
    _play_generation += 1
    generation = _play_generation
    epoch = _invalidate_shared()
    with _start_lock:
        descriptor = os.open(_runtime_dir() / 'overlay-start.lock',
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                if generation != _play_generation or _epoch() != epoch or (cancel_event is not None and cancel_event.is_set()):
                    raise LayerError('Audio layer start cancelled')
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.025)
            return _start_audio_locked(url, volume, timeout, cancel_event, local, generation,
                                       mpv, resolver, device_options, direct, epoch)
        finally:
            os.close(descriptor)


def _start_audio_locked(url, volume, timeout, cancel_event, local, generation, mpv, resolver, device_options, direct=None, epoch=None):
    global _mix_generation
    def check_cancel():
        if generation != _play_generation or _epoch() != epoch or (cancel_event is not None and cancel_event.is_set()):
            raise LayerError('Audio layer start cancelled')
    check_cancel()
    previous = _player()
    if len(previous.get('retiring', [])) >= 8:
        raise LayerError('Stop background audio to finish cleaning up previous players before tuning in again')
    previous_source = _source()
    identity = uuid.uuid4().hex[:12]
    candidate = {'unit': 'listening-hub-overlay-' + identity + '.service',
                 'socket': 'overlay-' + identity + '.sock'}
    path = _runtime_dir() / candidate['socket']
    # A prior openness ramp must never write into a replacement player's mix.
    _mix_generation += 1
    settings = _current_settings()
    initial_mix = dict(settings)
    if volume is not None:
        settings['volume'] = volume
    committed = False
    try:
        check_cancel()
        mpv_ipc.spawn(_run, candidate['unit'], mpv_ipc.audio_player(mpv, path, 'ListeningRoomAtmosphere') + device_options +
              (['--ytdl=no', '--loop-file=inf'] +
               (['--af=' + _window_filter(local)] if local.get('outdoor') else []) if local else [
              '--ytdl-format=bestaudio/best', '--ytdl-raw-options=ignore-config=,no-playlist=',
              '--script-opts=ytdl_hook-ytdl_path=' + resolver] if resolver else ['--ytdl=no']) +
              (['--keep-open=yes'] if direct and direct.get('reference', {}).get('provider') == 'jellyfin' else []))
        try:
            mpv_ipc.load(path, url, time.monotonic() + timeout, check_cancel)
        except mpv_ipc.LoadError as failure:
            raise LayerError(_LOAD_FAILURES[failure.reason]) from None
        check_cancel()
        saved = ({key: local[key] for key in ('kind', 'source_id', 'name', 'url', 'outdoor', 'window_open')}
                 if local else ({key: direct[key] for key in ('source_id', 'name', 'reference', 'resolver') if key in direct} | {'kind': 'radio'}
                                if direct else {'kind': 'twitch', 'url': url}))
        if direct and direct.get('id'):
            saved['source_id'] = direct['id']
        if direct and not direct.get('reference'):
            saved['url'] = url
        # Confirm its command path while the previous player
        # remains usable. The candidate is still inaudible.
        _ipc(['set_property', 'volume', 0], socket_path=path)
        _ipc(['set_property', 'mute', True], socket_path=path)
        check_cancel()
        with _commit():
            check_cancel()
            prior_settings = _settings()
            live_mix = _current_settings()
            settings['muted'] = live_mix['muted']
            if volume is None or live_mix['volume'] != initial_mix['volume']:
                settings['volume'] = live_mix['volume']
            prior_state = state()
            was_playing = bool(prior_state and prior_state.get('status') != 'Paused')
            previous_path = _runtime_dir() / previous['socket']
            try:
                # Pause the old source reversibly. It survives
                # every activation/persistence failure; a failed
                # later service cleanup cannot produce overlap.
                _ipc(['set_property', 'volume', settings['volume'] * _window_gain(local or {})], socket_path=path)
                if was_playing:
                    _ipc(['set_property', 'pause', True], socket_path=previous_path)
                check_cancel()
                _save_source(saved)
                _save_settings(settings)
                candidate['retiring'] = previous.get('retiring', []) + [
                    {key: previous[key] for key in ('unit', 'socket')}]
                _save_player(candidate)
                check_cancel()
                _ipc(['set_property', 'mute', settings['muted']], socket_path=path)
                check_cancel()
                committed = True
            except (OSError, ValueError):
                _save_source(previous_source)
                _save_settings(prior_settings)
                _save_player(previous)
                if was_playing and _epoch() == epoch:
                    _ipc(['set_property', 'pause', False], socket_path=previous_path)
                raise
        # Retired players are paused. Failed cleanup is retained
        # in the shared pointer, so another process's Stop can
        # retry it without discarding the working new source.
        for retired in list(candidate['retiring']):
            try:
                if not mpv_ipc.stop(_run, retired['unit']):
                    continue
            except LayerError:
                continue
            try:
                (_runtime_dir() / retired['socket']).unlink(missing_ok=True)
            except OSError:
                pass
            with _commit():
                current = _player()
                if current['unit'] == candidate['unit']:
                    current['retiring'] = [x for x in current.get('retiring', [])
                                           if x['unit'] != retired['unit']]
                    try:
                        _save_player(current)
                    except OSError:
                        pass  # Leave a retryable cleanup record.
        return state()
    except (OSError, ValueError) as error:
        if committed:
            # A completed handover survives housekeeping failures. Its shared
            # retiring records remain available to Stop for a later retry.
            return state()
        stopped = mpv_ipc.stop(_run, candidate['unit'])
        path.unlink(missing_ok=True)
        if not stopped:
            raise LayerError(str(error) + ' The audio layer could not be stopped.') from error
        suffix = '' if committed else ' Previous background playback has been preserved.'
        raise LayerError(str(error) + suffix) from error


_LOAD_FAILURES = {
    'start': 'Audio layer player did not start',
    'rejected': 'Audio layer could not load the stream',
    'closed': 'Audio layer closed while loading',
    'ended': 'Audio layer could not play this stream; try Twitch in a browser',
    'timeout': 'Audio layer loading timed out; try Twitch in a browser'}


def control(action, value=None):
    global _mix_generation, _play_generation
    if action in ('toggle', 'play-pause'):
        current = state()
        if current is not None:
            return control('pause' if current.get('status') == 'Playing' else 'play')
        _ipc(['cycle', 'pause'])
    elif action in ('pause', 'play'):
        if action == 'pause':
            _play_generation += 1
            _invalidate_shared()
        with _commit():
            try:
                _ipc(['set_property', 'pause', action == 'pause'])
            finally:
                if action == 'pause':
                    for retired in _player().get('retiring', []):
                        try:
                            _ipc(['set_property', 'pause', True], socket_path=_runtime_dir() / retired['socket'])
                        except LayerError:
                            pass
    elif action == 'stop':
        _play_generation += 1
        _invalidate_shared()
        with _commit():
            _mix_generation += 1
            settings = _current_settings()
            _save_settings(settings)
            player = _player()
            units = [player['unit']] + [x['unit'] for x in player.get('retiring', [])]
        failed = False
        for unit in units:
            try:
                failed = not mpv_ipc.ensure_stopped(_run, unit) or failed
            except LayerError:
                failed = True
        if failed:
            raise LayerError('The audio layer could not be stopped')
    elif action in ('volume', 'mute'):
        with _commit():
            current = state()
            settings = ({'volume': _volume(current['volume']), 'muted': bool(current['muted'])}
                        if current is not None else _settings())
            if action == 'volume':
                settings['volume'] = _volume(value)
                command = ['set_property', 'volume', settings['volume'] * _window_gain(_source())]
            else:
                settings['muted'] = not settings['muted']
                command = ['set_property', 'mute', settings['muted']]
            if current is not None:
                _ipc(command)
            _save_settings(settings)
    elif action in ('window', 'window-open'):
        openness = _volume(value)
        source = _source()
        if source.get('kind') != 'local' or not source.get('outdoor'):
            raise LayerError('Window controls are available for outdoor atmosphere sounds')
        _mix_generation += 1
        generation = _mix_generation
        player = _player()
        previous = source.get('window_open', 100)
        active = state() is not None
        for step in range(1, 9):
            if generation != _mix_generation or _player()['unit'] != player['unit']:
                return
            source['window_open'] = previous + (openness - previous) * step / 8
            if active:
                _ipc(['af', 'set', _window_filter(source)], socket_path=_runtime_dir() / player['socket'])
                _ipc(['set_property', 'volume', _settings()['volume'] * _window_gain(source)], socket_path=_runtime_dir() / player['socket'])
            if step < 8 and active:
                time.sleep(.025)
        with _commit():
            if generation == _mix_generation and _player()['unit'] == player['unit']:
                _save_source(source)
    else:
        raise LayerError('Unknown audio layer control')
