"""Independent, movable mpv windows for silent vistas and regular video."""
import json
import fcntl
import uuid
import math
import os
import re
from pathlib import Path
import shutil
import socket  # noqa: F401  (tests patch socket.socket through this name)
import subprocess
import tempfile
import time
import urllib.parse

import mpv_ipc
from visual_scenes import SCENES

MODES = ('ambience', 'video')
APP_IDS = {'ambience': 'org.kotona.ListeningAmbience', 'video': 'org.kotona.ListeningVideo'}
STATE_HOME = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'listening-hub'
ASSETS = Path(os.environ.get('LISTENING_HUB_ASSETS', Path(__file__).parent / 'assets'))


class AmbienceError(ValueError):
    pass


def _mode(mode):
    if mode not in MODES:
        raise AmbienceError('Choose silent ambience or video with sound')
    return mode


def _runtime():
    value = os.environ.get('XDG_RUNTIME_DIR')
    if not value:
        raise AmbienceError('Open the video window from your graphical session')
    path = Path(value) / 'listening-hub'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise AmbienceError('The listening window runtime directory must be private')
    return path


def _run(args, check=True):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AmbienceError(str(error)) from error
    if check and result.returncode:
        raise AmbienceError((result.stderr.strip() or result.stdout.strip())[:300] or 'Window command failed')
    return result


def _saved():
    try:
        value = json.loads((STATE_HOME / 'windows.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(mode, value):
    STATE_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _saved()
    data[mode] = value
    descriptor, name = tempfile.mkstemp(dir=STATE_HOME, prefix='.windows-')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(data, output)
        os.replace(name, STATE_HOME / 'windows.json')
    finally:
        Path(name).unlink(missing_ok=True)


def _video_audio(observe=True):
    saved = _saved().get('video', {})
    saved = saved if isinstance(saved, dict) else {}
    audio = saved.get('audio', {})
    audio = dict(audio) if isinstance(audio, dict) else {}
    volume = audio.get('volume')
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or not 0 <= volume <= 100:
        audio.pop('volume', None)
    if not isinstance(audio.get('muted'), bool):
        audio.pop('muted', None)
    if observe:
        for key, prop in (('volume', 'volume'), ('muted', 'mute')):
            try:
                value = _ipc(['get_property', prop], 'video')
                if key == 'volume' and not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100:
                    audio[key] = value
                elif key == 'muted' and isinstance(value, bool):
                    audio[key] = value
            except (OSError, ValueError, AttributeError):
                pass
        if audio:
            _save('video', dict(saved, audio=audio))
    return audio


def _save_video_audio(audio):
    saved = _saved().get('video', {})
    _save('video', dict(saved if isinstance(saved, dict) else {}, audio=audio))


def presets():
    result = [
        {'id': 'northern-lake', 'name': 'Northern lake · offline',
         'url': str(ASSETS / 'northern-lake.mp4'), 'kind': 'local',
         'description': 'A quiet illustrated lake with moving mist and reflections. No network needed.'},
        {'id': 'moonlit-lake', 'name': 'Moonlit lake · offline',
         'url': str(ASSETS / 'moonlit-lake.mp4'), 'kind': 'local',
         'description': 'A slow nocturne for reading, dark academia and late listening.'},
        {'id': 'norway', 'name': 'Norway · scenic film',
         'url': 'https://www.youtube.com/watch?v=VaLgmb5aGjo', 'kind': 'youtube',
         'description': 'Long-form landscapes from Scenic Relaxation. Requires YouTube access.'},
        {'id': 'nordics', 'name': 'Nordic landscapes · scenic film',
         'url': 'https://www.youtube.com/watch?v=czKdZtV5s88', 'kind': 'youtube',
         'description': 'An hour of Nordic landscapes, including Finland. Requires YouTube access.'},
    ]
    for identity, scene in SCENES.items():
        result.append({
            'id': identity, 'name': scene['title'] + ' · offline room',
            'url': str(ASSETS / (identity + '.mp4')), 'kind': 'local',
            'poster_path': str(ASSETS / (identity + '.png')),
            'still_url': str(ASSETS / (identity + '-still.jpg')),
            'description': scene['description'], 'motion': scene['motion'],
            'audio_tone': scene['audio_tone'], 'accent': scene['accent'],
        })
    candidates = []
    wizard = os.environ.get('LISTENING_HUB_WIZARD_VIDEO')
    if wizard:
        candidates.append(Path(wizard))
    for path in candidates:
        if path.is_file():
            result.append({'id': 'wizard-valley', 'name': 'Wizard Valley · preview clip',
                           'url': str(path), 'kind': 'local',
                           'description': 'A local eight-second preview clip, looped silently.'})
            break
    artwork = os.environ.get('LISTENING_HUB_WIZARD_ARTWORK')
    plate = Path(artwork) if artwork else None
    if plate is not None and plate.is_file():
        result.append({'id': 'wizard-artwork', 'name': 'Wizard Valley · original artwork',
                       'url': str(plate), 'kind': 'local',
                       'description': 'The original Wizard Valley artwork as a still view.'})
    for item in result:
        item['available'] = item['kind'] != 'local' or Path(item['url']).is_file()
    return result


def source(value):
    if isinstance(value, str):
        value = next((item for item in presets() if item['id'] == value),
                     {'id': 'custom', 'name': 'Custom video', 'url': value})
    if not isinstance(value, dict) or not isinstance(value.get('url'), str):
        raise AmbienceError('Paste a video URL or choose a vista')
    url = value['url'].strip()
    if not url or any(ord(character) < 32 for character in url):
        raise AmbienceError('The video URL is empty or contains control characters')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() in ('http', 'https'):
        if not parsed.hostname or parsed.username or parsed.password:
            raise AmbienceError('Use a public HTTP or HTTPS video URL')
        if any(key.lower() in ('token', 'api_key', 'apikey', 'access_token')
               for key, _ in urllib.parse.parse_qsl(parsed.query)):
            raise AmbienceError('Use a share URL without an access token')
    elif url.startswith('/'):
        path = Path(url)
        if not path.is_file():
            raise AmbienceError('The local video is unavailable; choose another vista')
        url = str(path.resolve())
    else:
        raise AmbienceError('Use an HTTP(S) video URL or an absolute local video path')
    name = ' '.join(str(value.get('name', 'Custom video')).split())[:160]
    return {'id': str(value.get('id', 'custom')), 'name': name, 'url': url}


def _ipc(command, mode='ambience', timeout=1):
    path = _runtime() / (_mode(mode) + '.sock')
    try:
        return mpv_ipc.call(path, command, timeout)
    except mpv_ipc.Rejected as error:
        raise AmbienceError('Video player: ' + str(error)) from None
    except mpv_ipc.Closed:
        raise AmbienceError('The video window closed') from None


def state(mode='ambience'):
    _mode(mode)
    try:
        if _ipc(['get_property', 'idle-active'], mode):
            return None
        paused = _ipc(['get_property', 'pause'], mode)
        saved = _saved().get(mode, {})
        result = dict(saved)
        result.update(id=mode, status='Paused' if paused else 'Playing',
                      title=_ipc(['get_property', 'media-title'], mode),
                      has_audio=mode == 'video',
                      volume=_ipc(['get_property', 'volume'], mode) if mode == 'video' else 0,
                      muted=_ipc(['get_property', 'mute'], mode) if mode == 'video' else True)
        return result
    except (OSError, ValueError):
        return None


def _window(mode):
    """The owned picture window, or None (also when hyprctl is missing or fails)."""
    try:
        clients = json.loads(_run(['hyprctl', 'clients', '-j']).stdout)
        return next((client for client in clients if client.get('class') == APP_IDS[mode]), None)
    except ValueError:
        return None


def _hyprland():
    """Whether hyprctl answers. Without it the desktop decides where windows go."""
    try:
        json.loads(_run(['hyprctl', 'clients', '-j']).stdout)
        return True
    except ValueError:
        return False


def place(where, mode='ambience'):
    _mode(mode)
    if where not in ('9', 'here', 'pin', 'pip'):
        raise AmbienceError('Choose workspace 9, Bring here, PiP, or Pin')
    client = _window(mode)
    if not client:
        raise AmbienceError('Open a video window first' if _hyprland() else 'Placement needs Hyprland')
    if not re.fullmatch(r'0x[0-9a-fA-F]+', client.get('address', '')):
        raise AmbienceError('Hyprland returned an invalid window address')
    address = json.dumps('address:' + client['address'])
    if where == 'pip':
        # Small, movable, pinned picture on the current desktop.
        if client.get('pinned'):
            _run(['hyprctl', 'dispatch', 'hl.dsp.window.pin({window=' + address + '})'])
        current = json.loads(_run(['hyprctl', 'activeworkspace', '-j']).stdout)
        workspace = int(current['id'])
        _run(['hyprctl', 'dispatch', 'hl.dsp.window.move({workspace=' + str(workspace) + ',follow=false,window=' + address + '})'])
        if not client.get('floating'):
            _run(['hyprctl', 'dispatch', 'hl.dsp.window.float({action="set",window=' + address + '})'])
        _run(['hyprctl', 'dispatch', 'hl.dsp.window.resize({x=640,y=360,relative=false,window=' + address + '})'])
        _run(['hyprctl', 'dispatch', 'hl.dsp.window.pin({window=' + address + '})'])
    elif where == 'pin':
        # A pin belongs to a floating window; preserve normal drag/resize.
        if not client.get('floating'):
            _run(['hyprctl', 'dispatch', 'hl.dsp.window.float({action="set",window=' + address + '})'])
        _run(['hyprctl', 'dispatch', 'hl.dsp.window.pin({window=' + address + '})'])
    else:
        if client.get('pinned'):
            _run(['hyprctl', 'dispatch', 'hl.dsp.window.pin({window=' + address + '})'])
        if where == 'here':
            current = json.loads(_run(['hyprctl', 'activeworkspace', '-j']).stdout)
            workspace = str(current['id'])
        else:
            workspace = '9'
        _run(['hyprctl', 'dispatch', 'hl.dsp.window.move({workspace=' + str(int(workspace)) + ',follow=false,window=' + address + '})'])
    saved = _saved().get(mode, {})
    saved['placement'] = where
    _save(mode, saved)


def command_line(item, mode, mpv, socket_path):
    _mode(mode)
    args = [mpv, '--no-config', '--idle=yes', '--force-window=immediate',
            '--audio-client-name=ListeningRoomPicture', '--input-terminal=no', '--input-ipc-server=' + str(socket_path),
            '--wayland-app-id=' + APP_IDS[mode], '--x11-name=' + APP_IDS[mode],
            '--title=' + ('Vista · ' if mode == 'ambience' else 'Video · ') + item['name'],
            # gpu-next/libplacebo currently duplicates a colour-management
            # surface on this compositor. The EGL path is verified here.
            '--vo=gpu', '--gpu-context=wayland', '--target-colorspace-hint=no',
            '--geometry=960x540', '--hwdec=auto-safe', '--keep-open=yes',
            '--image-display-duration=inf']
    resolver = shutil.which('yt-dlp')
    if resolver:
        # Transient services do not inherit this wrapper's PATH. Keep the
        # resolver explicit so public YouTube links work from the user unit.
        args.append('--script-opts=ytdl_hook-ytdl_path=' + resolver)
    if mode == 'ambience':
        args += ['--no-audio', '--aid=no', '--loop-file=inf', '--stop-screensaver=no',
                 '--osc=no', '--osd-level=0', '--osd-bar=no', '--border=no',
                 '--cursor-autohide=always',
                 '--ytdl-format=bestvideo[height<=1080]/best[height<=1080]']
    else:
        args += ['--stop-screensaver=yes',
                 '--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]/best']
    return args


def _generation(mode, write=False):
    path = _runtime() / (mode + '.generation')
    if write:
        identity = uuid.uuid4().hex
        temporary = path.with_name(path.name + '.' + identity)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, identity.encode())
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        return identity
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def start(value, mode='ambience', placement='9', cancel_event=None, display=None):
    _mode(mode)
    if cancel_event is not None and cancel_event.is_set():
        raise AmbienceError('Picture opening cancelled')
    identity = _generation(mode, write=True)
    descriptor = os.open(_runtime() / (mode + '-start.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            if (cancel_event is not None and cancel_event.is_set()) or _generation(mode) != identity:
                raise AmbienceError('Picture opening cancelled')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.05)
        return _start(value, mode, placement, cancel_event, display, identity)
    finally:
        os.close(descriptor)


def _start(value, mode='ambience', placement='9', cancel_event=None, display=None, generation=None):
    _mode(mode)
    def cancelled():
        if (cancel_event is not None and cancel_event.is_set()) or (generation is not None and _generation(mode) != generation):
            raise AmbienceError('Picture opening cancelled')
    cancelled()
    if placement not in ('9', 'here', 'pin', 'pip'):
        raise AmbienceError('Choose workspace 9 or the current workspace')
    item = source(value)
    mpv = shutil.which('mpv')
    if not mpv:
        raise AmbienceError('The video player is unavailable')
    path = _runtime() / (mode + '.sock')
    unit = 'listening-hub-' + mode + '.service'
    audio = _video_audio() if mode == 'video' else {}
    if mode == 'video':
        import audio_devices
        output_options = audio_devices.player_options()
    else:
        output_options = []
    if not mpv_ipc.ensure_stopped(_run, unit):
        raise AmbienceError('The existing window could not be stopped; its controls have been preserved')
    path.unlink(missing_ok=True)
    try:
        cancelled()
        mpv_ipc.spawn(_run, unit, command_line(item, mode, mpv, path) + output_options
                      + (['--volume=' + str(audio.get('volume', 100)),
                          '--mute=' + ('yes' if audio.get('muted', False) else 'no')] if mode == 'video' else []))
        # An absolute deadline: a YouTube error or an endlessly buffering URL
        # must not hang a control window forever.
        try:
            mpv_ipc.load(path, item['url'], time.monotonic() + 35, cancelled)
        except mpv_ipc.LoadError as failure:
            raise AmbienceError(_LOAD_FAILURES[failure.reason]) from None
        cancelled()
        _save(mode, dict(item, placement=placement, **({'audio': audio} if mode == 'video' else {})))
        # Hyprland rules handle the initial placement. This also works when
        # called outside that configured session or when opening here.
        # Without Hyprland the picture stays where the desktop opened it.
        if _window(mode) is not None or _hyprland():
            mapping_deadline = time.monotonic() + 3
            while _window(mode) is None and time.monotonic() < mapping_deadline:
                cancelled()
                time.sleep(.1)
            if placement == 'pin':
                place('here', mode)
            cancelled()
            place(placement, mode)
            if display:
                place_on_display(display, mode)
        return state(mode)
    except (OSError, ValueError) as error:
        if not mpv_ipc.stop(_run, unit):
            raise AmbienceError(str(error) + ' The window could not be closed; close it manually.') from error
        raise AmbienceError(str(error)) from error


_LOAD_FAILURES = {
    'start': 'The video window did not open',
    'rejected': 'The video URL could not be loaded',
    'closed': 'The video window closed while loading',
    'ended': 'This video could not be played. Try the offline lake, a local clip, or another public URL.',
    'timeout': 'Video loading timed out. Try an offline vista or another URL.'}


def control(action, mode='ambience', value=None):
    _mode(mode)
    if action in ('toggle', 'play-pause'):
        _ipc(['cycle', 'pause'], mode)
    elif action in ('pause', 'play'):
        _ipc(['set_property', 'pause', action == 'pause'], mode)
    elif action in ('stop', 'close'):
        _generation(mode, write=True)
        if mode == 'video':
            _video_audio()
        mpv_ipc.stop(_run, 'listening-hub-' + mode + '.service', check=True)
    elif action == 'volume' and mode == 'video':
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise AmbienceError('Choose a video volume between 0 and 100') from None
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise AmbienceError('Choose a video volume between 0 and 100')
        _ipc(['set_property', 'volume', value], mode)
        audio = _video_audio()
        audio['volume'] = value
        _save_video_audio(audio)
    elif action == 'mute' and mode == 'video':
        audio = _video_audio()
        _ipc(['cycle', 'mute'], mode)
        audio['muted'] = not audio.get('muted', False)
        _save_video_audio(audio)
    else:
        raise AmbienceError('That control is unavailable for this window')


def monitors():
    """Connected displays; failure leaves placement usable on the current desktop."""
    try:
        result = json.loads(_run(['hyprctl', 'monitors', '-j']).stdout)
        return [dict(name=item['name'], description=item.get('description', item['name']))
                for item in result if isinstance(item, dict) and re.fullmatch(r'[A-Za-z0-9_.:-]+', item.get('name', ''))]
    except (OSError, ValueError, TypeError):
        return []


def place_on_display(name, mode='ambience'):
    available = monitors()
    selected = next((item for item in available if item['name'] == name), None)
    if selected is None:
        # A removed monitor must not strand a picture off-screen.
        place('here', mode)
        return {'display': None, 'fallback': True}
    client = _window(mode)
    if not client or not re.fullmatch(r'0x[0-9a-fA-F]+', client.get('address', '')):
        raise AmbienceError('Open a picture before choosing its display')
    address = json.dumps('address:' + client['address'])
    _run(['hyprctl', 'dispatch', 'hl.dsp.window.move({monitor=' + json.dumps(selected['name']) + ',window=' + address + '})'])
    return {'display': selected['name'], 'fallback': False}


def set_lighting(level):
    """Authored dusk progression: dim the owned picture only, never the display."""
    try:
        level = float(level)
    except (TypeError, ValueError):
        raise AmbienceError('Choose a lighting level from 0 to 1') from None
    if not __import__('math').isfinite(level) or not 0 <= level <= 1:
        raise AmbienceError('Choose a lighting level from 0 to 1')
    # mpv updates this named filter in place; source artwork and loops stay fixed.
    _ipc(['vf', 'set', '@room-light:eq=brightness=' + str(-.14 * level) + ':saturation=' + str(1-.12*level)], 'ambience')
    return level
