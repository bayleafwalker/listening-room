"""Route only Listening Room's owned streams; never change the default sink.

PipeWire's PulseAudio compatibility API supplies stable sink names and stream
metadata. Call detection deliberately requires an explicit communication role.
"""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import wave
from array import array

CLIENT_NAMES = frozenset(('ListeningRoomMusic', 'ListeningRoomPicture', 'ListeningRoomAtmosphere'))
_lock = threading.RLock()
_previous_output = None


class DeviceError(ValueError):
    pass


def _run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeviceError('Audio output service unavailable; check PipeWire') from error
    if result.returncode:
        raise DeviceError('Audio output command failed; refresh outputs and try again')
    return result.stdout


def _list(kind):
    response = _run(['pactl', '--format=json', 'list', kind])
    try:
        result = json.loads(response)
    except (ValueError, TypeError) as error:
        raise DeviceError('Audio output service returned unreadable device information') from error
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise DeviceError('Audio output service returned invalid device information')
    return result


def _path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'listening-hub' / 'audio.json'


def settings():
    try:
        value = json.loads(_path().read_text())
        if not isinstance(value, dict):
            raise ValueError('Invalid settings')
    except (OSError, ValueError):
        value = {}
    output = value.get('preferred_output')
    return {'preferred_output': output if isinstance(output, str) and output and not any(ord(c) < 32 for c in output) else None,
            'pause_on_disconnect': value.get('pause_on_disconnect') is True,
            'duck_on_call': value.get('duck_on_call') is True}


def configure(preferred_output=Ellipsis, pause_on_disconnect=None, duck_on_call=None):
    with _lock:
        result = settings()
        if preferred_output is not Ellipsis:
            if preferred_output is not None and (not isinstance(preferred_output, str) or not preferred_output
                    or any(ord(c) < 32 for c in preferred_output)):
                raise DeviceError('Choose an audio output from the device list')
            result['preferred_output'] = preferred_output
        for key, value in (('pause_on_disconnect', pause_on_disconnect), ('duck_on_call', duck_on_call)):
            if value is not None:
                if not isinstance(value, bool):
                    raise DeviceError('Audio policies must be on or off')
                result[key] = value
        path = _path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.audio-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(result, output)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return result


def outputs(with_default=True):
    sinks = _list('sinks')
    default = _run(['pactl', 'get-default-sink']).strip() if with_default else None
    result = []
    for sink in sinks:
        name = sink.get('name')
        if not isinstance(name, str) or not name or any(ord(c) < 32 for c in name):
            continue
        props = sink.get('properties') or {}
        if not isinstance(props, dict):
            props = {}
        result.append({'id': name, 'name': name, 'index': sink.get('index'),
                       'description': str(sink.get('description') or props.get('node.nick') or name),
                       'default': name == default, 'available': True})
    return result


def player_options():
    """Select the preferred sink before decoding can emit an audible sample."""
    policy = settings()
    preferred = policy['preferred_output']
    if not preferred:
        return []
    try:
        present = any(item['name'] == preferred for item in outputs())
    except DeviceError:
        if policy['pause_on_disconnect']:
            raise DeviceError('Could not confirm your preferred output; refresh outputs before playing') from None
        return []
    if not present:
        if policy['pause_on_disconnect']:
            raise DeviceError('Your preferred audio output is disconnected; reconnect it or choose another output')
        return []
    return ['--audio-device=pulse/' + preferred]


def _pid_owned(pid, client_name):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        path = Path('/proc') / str(pid)
        if path.stat().st_uid != os.getuid():
            return False
        args = path.joinpath('cmdline').read_bytes().split(b'\0')
        marker = ('--audio-client-name=' + client_name).encode()
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        if not runtime or marker not in args:
            return False
        directory = str(Path(runtime) / 'listening-hub') + '/'
        return any(arg.startswith(('--input-ipc-server=' + directory).encode()) for arg in args)
    except (OSError, ValueError, TypeError):
        return False


def _owned(stream):
    props = stream.get('properties') or {}
    if not isinstance(props, dict):
        return False
    name = props.get('application.name')
    return (name in CLIENT_NAMES and props.get('application.process.binary') == 'mpv'
            and isinstance(stream.get('index'), int) and not isinstance(stream.get('index'), bool)
            and _pid_owned(props.get('application.process.id'), name))


def _call_active(streams):
    for stream in streams:
        props = stream.get('properties') or {}
        if not isinstance(props, dict):
            continue
        role = str(props.get('media.role', '')).lower()
        corked = stream.get('corked', False)
        if role in ('communication', 'phone') and corked in (False, 'false', 'no', 0):
            return True
    return False


def route(name, remember=True):
    if not isinstance(name, str) or not any(item['name'] == name for item in outputs()):
        raise DeviceError('This audio output is unavailable; choose another output')
    moved, errors = [], []
    for stream in _list('sink-inputs'):
        if _owned(stream):
            try:
                _run(['pactl', 'move-sink-input', str(stream['index']), name])
                moved.append(stream['index'])
            except DeviceError as error:
                errors.append(str(error))
    if errors:
        raise DeviceError('Some room audio could not move; refresh outputs and retry')
    if remember:
        configure(preferred_output=name)
    return {'output': name, 'moved': moved}


def status():
    result = dict(settings(), available=False, outputs=[], owned_streams=[], call_active=False,
                  preferred_available=False, error=None,
                  call_detection='Only applications declaring a Communication or Phone stream role')
    try:
        result['outputs'] = outputs()
        result['owned_streams'] = [dict(index=x['index'], sink=x.get('sink'),
                                        name=x['properties']['application.name'])
                                   for x in _list('sink-inputs') if _owned(x)]
        # Active call playback OR capture can declare the communication role.
        result['call_active'] = _call_active(_list('source-outputs')) or _call_active(_list('sink-inputs'))
        result['available'] = True
        result['preferred_available'] = (not result['preferred_output'] or
                any(x['name'] == result['preferred_output'] for x in result['outputs']))
    except DeviceError as error:
        result['error'] = str(error)
    return result


def poll_policy(detect_calls=False):
    """Read policy state. The controller owns pausing/ducking and restoration.

    Asks PipeWire only for what an enabled policy needs: output presence for
    pause-on-disconnect, call streams for ducking (`detect_calls` adds that for
    a caller whose own settings enable it). Each stream list is read once. With
    both policies off nothing is run.
    """
    global _previous_output
    result = dict(settings(), available=False, outputs=[], owned_streams=[], call_active=False,
                  preferred_available=False, error=None,
                  call_detection='Only applications declaring a Communication or Phone stream role')
    preferred = result['preferred_output']
    watch_output = bool(result['pause_on_disconnect'] and preferred)
    watch_calls = bool(result['duck_on_call'] or detect_calls)
    if not (watch_output or watch_calls):
        _previous_output = None
        result.update(disconnected=False, disconnect=False, reconnected=False, duck=False)
        return result
    try:
        if watch_output:
            result['outputs'] = outputs(with_default=False)
            result['preferred_available'] = any(x['name'] == preferred for x in result['outputs'])
        if watch_calls:
            # Active call playback OR capture can declare the communication role.
            result['call_active'] = _call_active(_list('source-outputs')) or _call_active(_list('sink-inputs'))
        result['available'] = True
        if not watch_output:
            result['preferred_available'] = not preferred
    except DeviceError as error:
        result['error'] = str(error)
    result['disconnected'] = bool(result['available'] and watch_output and not result['preferred_available'])
    result['disconnect'] = result['disconnected'] and result['pause_on_disconnect']
    result['reconnected'] = bool(_previous_output == (preferred, False) and result['preferred_available'])
    _previous_output = (preferred, result['preferred_available']) if watch_output else None
    result['duck'] = result['duck_on_call'] and result['call_active']
    return result


def test_sound(sink_name=None):
    """Play one quiet second to an explicitly chosen output, on user action."""
    devices = outputs()
    chosen = sink_name or settings()['preferred_output'] or next((x['name'] for x in devices if x['default']), None)
    if chosen is None or not any(item['name'] == chosen for item in devices):
        raise DeviceError('Choose an available output before testing sound')
    mpv = shutil.which('mpv')
    if not mpv:
        raise DeviceError('The audio player is unavailable')
    with tempfile.TemporaryDirectory(prefix='listening-output-test-') as folder:
        path = Path(folder) / 'test.wav'
        rate = 24000
        samples = array('h', (round(32767 * .08 * math.sin(math.tau * 440 * i / rate)
                                  * min(1, i / 1200, (rate - 1 - i) / 1200)) for i in range(rate)))
        import sys
        if sys.byteorder != 'little':
            samples.byteswap()
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            output.writeframes(samples.tobytes())
        _run([mpv, '--no-config', '--no-video', '--no-terminal', '--ao=pulse',
              '--audio-device=pulse/' + chosen, '--volume=25', '--ytdl=no', str(path)])
    return {'output': chosen, 'message': 'Played a quiet one-second test sound'}
