"""O11-CONTROL acceptance: a watched video and partially paused layers are first-class room state.

Written by the acceptance author from the O11-CONTROL brief, UX assessment 2026-11-21 Proposal E (MPRIS,
Waybar and keys bullets), N9, N10 and the O10 backlog ("roomctl verb for video", "Music paused"), not from
the implementation. Real GTK on a private Xvfb with its own D-Bus (tests/probes/o6_env.py re-executes this
file there), the real session controller on the session test double and fixture providers.

Surfaces driven, as in the O10-CONTROL probe:
  window   the real NextWindow (Now's room Pause/Play, the Mix rows and keys, the connecting state)
  popover  next_mini.PopoverHost, the Waybar left-click window
  MPRIS    session_service.Mpris on the private session bus, called over D-Bus from a second connection
  keys     `listening-hub toggle` (what the XF86 media keys run), in process, players pinned to the room
  roomctl  the real `roomctl.py` as a subprocess against the real controller socket (DISPLAY unset)

The Watch video runs the real `room_session.Backend` video path into the real `ambience` module; process
starts go to the O10-YOUTUBE oracle's fake `systemd-run` (a fake mpv JSON-IPC server on a private socket),
so "the video is paused" means the player's own `pause` property. Nothing reaches the desktop session, the
network, systemd or real credentials; no audio is played.

Readings of the brief this probe takes:
  * Criterion 1 "same resume semantics": room Pause pauses everything audible, room Play restores exactly
    what that Pause paused. While watching, the room's layers are already paused by Watch, so room Pause
    then Play pauses and resumes the video and leaves Music and Ambience paused (Watch's own resume offer is
    still the explicit way back). With the room resumed during Watch, Pause/Play covers all three.
  * "show" the video: a `video` verb of `listening-roomctl` prints the video's name and state; `inspect`
    does not count (it already existed). A URL that is not a public YouTube video is refused with a non-zero
    exit and opens nothing. The outcome ("seen ... from Waybar") also asks `status` to name the video.
  * Criterion 2: the partially-paused line names the paused layer ("Music paused", "Ambience paused") and
    the layer still playing, with class "paused" (a string, or a list holding "paused" but not "playing").
    A stopped layer is not "paused". Every other state keeps the 2.x line exactly (the revised WP2 oracle
    status_line_problem, which every status line read here must satisfy).
  * Criterion 3: the per-layer items are buttons in the popover, below the room name; tapping one acts on
    that layer alone and its look follows the layer's state, including changes made elsewhere.
  * Criterion 4: M/A act while the Mix is open; they must not act while a text entry has focus. Shift+Up/Down
    change the focused row's level (any step) and neither the other row nor the room volume. Keys are
    delivered through the focus chain's key and shortcut controllers and the application accelerators
    (no xdotool in the dev shell). "Registered in the shortcut table" = the window's Keyboard shortcuts list
    names them.
  * Criterion 5: while connecting the Mix shows a progress indicator and Cancel, and no other control except
    the sheet's own close. If the implementer takes the brief's fallback (no Cancel, with a recorded reason)
    the K checks fail and the coordinator rules.

Limits set by the author, not by the brief (flagged for a ruling): the fixture connect takes 6 s; Cancel must
take effect within 3 s; "nothing starts afterwards" is observed until 2 s after the fixture connect would have
finished; `status --follow` and the popover must show a change within 2 s. The 5 s Leave allowance is O10's
E11b (a faded Leave).

  scripts/listening-dev.sh probe OUT listening-o11c-acceptance
  (O11C_SECTIONS=VIDEO,PARTIAL limits the run to those sections)
"""
import concurrent.futures
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
import types
from unittest import mock

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o11c-acceptance', extra_args={'--repo': {'default': ''}})
env.__enter__()
REPO = Path(env.args.repo or Path(__file__).resolve().parents[2])
ONLY = set(filter(None, os.environ.get('O11C_SECTIONS', '').split(',')))
checks = []
RAIN = {'kind': 'local', 'source_id': 'rain-glass', 'volume': 20, 'muted': False, 'window_open': 35}
TARGET = 'Northern quiet'           # a catalogue radio room (music + scene, no ambience of its own)
VIDEO_ID = 'o11cWatch01'
VIDEO_URL = 'https://www.youtube.com/watch?v=' + VIDEO_ID
VIDEO_TITLE = 'Lighthouse at dusk'
CONNECT_SECONDS = 6.      # author-set: the fixture's slow connect
CANCEL_SECONDS = 3.       # author-set: Cancel takes effect within this
CHANGE_SECONDS = 2.       # author-set: follow line / popover show a change within this
LEAVE_SECONDS = 5.        # O10 E11b: a faded Leave

from test_listening_wp2_protocol_oracles import status_line_problem  # noqa: E402  (the revised WP2 oracle)


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:600]), flush=True)


def write_results():
    (env.output / 'o11c-acceptance.json').write_text(json.dumps(
        [{'criterion': name, 'pass': ok} for name, ok in checks], indent=1))


def reset():
    for window in list(env.app.get_windows()):
        window.close()
    env.pump(.3)
    env.start_error = env.atmosphere_error = None
    env.start_delay = 0.
    if env.controller.snapshot().get('video'):
        try:
            env.service.execute('video', {'action': 'stop'})
        except Exception:
            pass
    if env.controller.active:
        env.service.execute('leave', {'fade_seconds': 0})
    env.settle(.6)


def section(name):
    def wrap(function):
        if ONLY and name not in ONLY:
            return function
        print('== section %s (%s)' % (name, function.__name__), flush=True)
        try:
            function()
        except Exception as error:   # one broken section never hides the others
            check('%s %s: section ran without raising' % (name, function.__name__), False, '%s: %s\n%s' % (
                type(error).__name__, error, traceback.format_exc()[-1500:]))
        finally:
            try:
                reset()
            except Exception:
                traceback.print_exc()
            write_results()
        return function
    return wrap


# ---- the test double's transport: give toggle mpv's meaning (as the O10 probe does) ------------------
_original_control = env.backend._control


def _control(layer, action, value):
    if action in ('toggle', 'play-pause'):
        action = 'play' if env.backend.state[layer].get('status') == 'Paused' else 'pause'
    return _original_control(layer, action, value)


env.backend._control = _control


# ---- helpers ----------------------------------------------------------------------------------------

def wait(predicate, timeout=5., step=.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        env.pump(step)
    return bool(predicate())


def snap():
    env.controller.refresh()
    return env.controller.snapshot()


def status():
    layers = snap().get('layers') or {}
    return {name: (layers.get(name) or {}).get('status', 'Stopped') for name in ('music', 'atmosphere', 'picture')}


def audible():
    return {name for name, value in status().items() if name != 'picture' and value == 'Playing'}


def layer_name(name):
    item = (snap().get('layers') or {}).get(name) or {}
    return item.get('name') or item.get('title') or ''


def enter_room(room_id='northern-quiet', ambience=True):
    if env.controller.active:
        env.service.execute('leave', {'fade_seconds': 0})
        env.settle(.4)
    args = {'room_id': room_id}
    if ambience:
        args['atmosphere'] = dict(RAIN)
    env.service.execute('enter', args)
    wait(lambda: env.controller.active and not env.controller.transition and status()['music'] == 'Playing'
         and (not ambience or status()['atmosphere'] == 'Playing'), 8)
    env.settle(.4)
    assert env.controller.active, 'the room did not start'


def start_library_room():
    tracks = copy.deepcopy(o6_env.TRACKS)
    env.service.execute('music', {'item': {'name': 'Neoclassical collection', 'kind': 'radio', 'reference': tracks[0],
                                           'queue': tracks, 'queue_index': 0}})
    wait(lambda: env.controller.active and status()['music'] == 'Playing' and not env.controller.transition, 8)
    env.settle(.6)


def layer(name, action):
    env.service.execute('layer', {'layer': name, 'action': action})
    env.settle(.5)


def in_popover(widget):
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, env.Gtk.Popover):
            return True
        parent = parent.get_parent()
    return False


def icons(widget):
    found = []
    for item in env.widgets(widget):
        name = item.get_icon_name() if hasattr(item, 'get_icon_name') else None
        if name:
            found.append(name)
    return tuple(found)


def button_name(widget):
    label = widget.get_label() if hasattr(widget, 'get_label') else None
    if not label:
        label = ' '.join(item.get_text() for item in env.widgets(widget)
                         if isinstance(item, env.Gtk.Label) and item.get_text())
    return (label or '').strip(), (widget.get_tooltip_text() or '').strip()


def look(widget):
    """Everything a viewer can see or hear (screen reader) of a button."""
    accessible = ''
    try:
        accessible = widget.get_accessible_role().value_nick
    except Exception:
        pass
    return button_name(widget) + (icons(widget), tuple(sorted(widget.get_css_classes())), accessible)


def mapped_buttons(root, pattern=None):
    regex = re.compile(pattern, re.I) if pattern else None
    return [w for w in env.widgets(root) if isinstance(w, env.Gtk.Button) and w.get_mapped() and not in_popover(w)
            and (regex is None or any(regex.search(part) for part in button_name(w) if part))]


def open_window(page=None):
    window = env.launch(page=page)
    env.pump(.8)
    return window


def press_room(window):
    env.pump(.4)
    button = getattr(window, 'play', None) or mapped_buttons(window, r'(pause|play) the room')[0]
    button.emit('clicked')
    env.settle(.8)


def enter_from_rooms(window, name):
    window.show_rooms()
    env.pump(.6)
    entry = next((e for e in window.rooms.card_for.values() if e.get('name') == name), None)
    assert entry is not None, 'no Rooms card named %r' % name
    window.rooms.open_preview(entry)
    env.pump(.4)
    window.rooms.press_enter()
    env.pump(.2)
    return entry


def mix_widget(window):
    return getattr(window, 'mix', None) or window


def open_mix(window):
    window.show_now()
    env.pump(.5)
    window.open_mix()
    env.pump(.9)


def bounds(widget, root):
    ok, rect = widget.compute_bounds(root)
    return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok else None


# ---- keys through the focus chain --------------------------------------------------------------------

def deliver_key(window, keyval, state=0):
    """One key press as GTK would route it: capture (root to focus), then target/bubble (focus to root) over
    every key and shortcut controller on the focus chain, then the application accelerators."""
    Gtk, Gdk = env.Gtk, env.Gdk
    modifiers = Gdk.ModifierType(state)
    chain, node = [], window.get_focus()
    while node is not None:
        chain.append(node)
        node = node.get_parent()
    if window not in chain:
        chain.append(window)
    mask = Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK

    def trigger_matches(trigger):
        if isinstance(trigger, Gtk.AlternativeTrigger):
            return trigger_matches(trigger.get_first()) or trigger_matches(trigger.get_second())
        if isinstance(trigger, Gtk.KeyvalTrigger):
            return (Gdk.keyval_to_lower(trigger.get_keyval()) == Gdk.keyval_to_lower(keyval)
                    and (trigger.get_modifiers() & mask) == (modifiers & mask))
        return False

    def run(widget, controller):
        if isinstance(controller, Gtk.EventControllerKey):
            return bool(controller.emit('key-pressed', keyval, 0, modifiers))
        if isinstance(controller, Gtk.ShortcutController):
            for index in range(controller.get_n_items()):
                shortcut = controller.get_item(index)
                if trigger_matches(shortcut.get_trigger()):
                    if shortcut.get_action().activate(Gtk.ShortcutActionFlags(0), widget, shortcut.get_arguments()):
                        return True
        return False

    def controllers(widget, phases):
        return [c for c in widget.observe_controllers() if c.get_propagation_phase() in phases]
    handled = False
    for widget in reversed(chain):
        if any(run(widget, c) for c in controllers(widget, (Gtk.PropagationPhase.CAPTURE,))):
            handled = True
            break
    if not handled:
        for widget in chain:
            if any(run(widget, c) for c in controllers(widget, (Gtk.PropagationPhase.TARGET, Gtk.PropagationPhase.BUBBLE))):
                handled = True
                break
    if not handled:
        for action in env.app.get_actions_for_accel(Gtk.accelerator_name(keyval, modifiers)):
            scope, _, name = action.partition('.')
            target = window if scope == 'win' else env.app
            if target.lookup_action(name) is not None:
                target.activate_action(name, None)
                handled = True
    env.settle(1.0)
    return handled


# ---- MPRIS over the private session bus ---------------------------------------------------------------
_mpris = {}


def _run(function):
    future = (_mpris.get('pool') or concurrent.futures.ThreadPoolExecutor(1)).submit(function)
    while not future.done():
        env.pump(.03)
    return future.result()


def mpris_bus():
    if not _mpris:
        import session_service
        from gi.repository import Gio
        _mpris['service'] = session_service.Mpris(env.service)
        _mpris['module'] = session_service
        _mpris['pool'] = concurrent.futures.ThreadPoolExecutor(1)

        def connect():
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            return Gio.DBusConnection.new_for_address_sync(
                address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
        _mpris['connection'] = _run(connect)
        assert wait(lambda: _try_values() is not None, 6), 'MPRIS name never appeared on the private bus'
    return _mpris


def _call(interface, method, arguments=None, result=None):
    from gi.repository import Gio, GLib
    module, connection = _mpris['module'], _mpris['connection']
    return _run(lambda: connection.call_sync(module.MPRIS_NAME, module.MPRIS_PATH, interface, method, arguments,
                                            GLib.VariantType.new(result) if result else None,
                                            Gio.DBusCallFlags.NONE, 3000, None))


def _try_values():
    from gi.repository import GLib
    try:
        return _call('org.freedesktop.DBus.Properties', 'GetAll',
                     GLib.Variant('(s)', (_mpris['module'].PLAYER_INTERFACE,)), '(a{sv})').unpack()[0]
    except GLib.Error:
        return None


def mpris_status():
    mpris_bus()
    env.settle(.3)
    values = _try_values()
    return (values or {}).get('PlaybackStatus')


def mpris(method):
    bus = mpris_bus()
    _call(bus['module'].PLAYER_INTERFACE, method)
    env.settle(.9)


# ---- media keys: listening-hub toggle ----------------------------------------------------------------

def media_key():
    import listening_hub
    import player_core

    def players_now():
        music = (snap().get('layers') or {}).get('music') or {}
        return [{'id': 'radio', 'status': music.get('status') if music.get('status') in ('Playing', 'Paused') else 'Playing',
                 'title': music.get('name') or 'Radio', 'artist': ''}]
    notes = []
    with mock.patch.object(listening_hub, 'all_info', side_effect=players_now), \
            mock.patch.object(player_core, 'all_info', side_effect=players_now), \
            mock.patch.object(listening_hub, 'players', return_value=[]), \
            mock.patch.object(player_core, 'players', return_value=[]), \
            mock.patch.object(listening_hub, 'notify', side_effect=notes.append), \
            mock.patch.object(listening_hub, 'menu', side_effect=AssertionError('the media key opened a menu')), \
            mock.patch.object(sys, 'argv', ['listening-hub', 'toggle']):
        code = listening_hub.main()
    env.settle(.9)
    return code, notes


# ---- roomctl over the real socket ----------------------------------------------------------------------
_socket = {}


def controller_socket():
    if not _socket:
        import session_service
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or env.folder) / 'o11c-session'
        runtime.mkdir(mode=0o700, exist_ok=True)
        path = runtime / 'session.sock'
        server, descriptor = session_service.make_server(path, env.service)
        assert server is not None, 'could not bind the private controller socket'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _socket.update(path=path, server=server, descriptor=descriptor)
    return _socket['path']


def roomctl_environment():
    path = controller_socket()
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE')}
    environment.update(LISTENING_SESSION_SOCKET=str(path), PYTHONPATH=os.pathsep.join([str(env.source), str(REPO / 'tests')]),
                       PYTHONDONTWRITEBYTECODE='1')
    return environment


def roomctl(*argv, timeout=15):
    environment = roomctl_environment()
    pool = concurrent.futures.ThreadPoolExecutor(1)
    future = pool.submit(REAL_RUN, [sys.executable, str(env.source / 'roomctl.py')] + list(argv),
                         capture_output=True, text=True, timeout=timeout, env=environment)
    while not future.done():
        env.pump(.05)
    result = future.result()
    pool.shutdown()
    env.settle(.8)
    return result


def waybar_status():
    result = roomctl('status')
    try:
        line = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        line = None
    STATUS_LINES.append((line, snap()))
    return line


STATUS_LINES = []   # every status line read, with the snapshot it described (judged by P9)


def classes(line):
    value = (line or {}).get('class')
    return [value] if isinstance(value, str) else list(value) if isinstance(value, list) else []


def paused_class(line):
    return 'paused' in classes(line) and 'playing' not in classes(line)


class Follow:
    """`listening-roomctl status --follow` as Waybar runs it, with each line's arrival time."""

    def __init__(self):
        self.lines = []
        self.process = subprocess.Popen([sys.executable, str(env.source / 'roomctl.py'), 'status', '--follow'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        env=roomctl_environment())
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for raw in self.process.stdout:
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            self.lines.append((time.monotonic(), value))

    def wait_for(self, predicate, since, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = next((t for t, line in list(self.lines) if t >= since and predicate(line)), None)
            if hit is not None:
                return hit - since
            env.pump(.05)
        return None

    def close(self):
        self.process.kill()
        self.process.wait(5)


# ---- the Watch video: real Backend video path, fake mpv processes --------------------------------------
REAL_RUN = subprocess.run
from test_listening_o10y_oracles import FakeProcesses, VIDEO_UNIT  # noqa: E402

PROCS = FakeProcesses()


@contextlib.contextmanager
def video_harness():
    import ambience
    import audio_devices
    import room_session
    real_which = ambience.shutil.which

    def run(args, *pos, **kwargs):
        if isinstance(args, (list, tuple)) and args and str(args[0]) != sys.executable:
            return PROCS(args, *pos, **kwargs)
        return REAL_RUN(args, *pos, **kwargs)
    added = []
    for name in ('start_video', 'stop_video', 'video_state', 'extra_video_control'):
        if name not in env.backend.__dict__:
            setattr(env.backend, name, types.MethodType(getattr(room_session.Backend, name), env.backend))
            added.append(name)
    patches = [mock.patch('subprocess.run', run),
               mock.patch.object(ambience, 'STATE_HOME', Path(env.folder) / 'state' / 'listening-hub'),
               mock.patch.object(audio_devices, 'player_options', return_value=[]),
               mock.patch.object(ambience.shutil, 'which',
                                 side_effect=lambda name, *a, **k: '/fake/' + name if name in ('mpv', 'yt-dlp') else real_which(name, *a, **k))]
    for patch in patches:
        patch.start()
    try:
        yield
    finally:
        try:
            reset()
        finally:
            for patch in reversed(patches):
                patch.stop()
            for name in added:
                delattr(env.backend, name)
            PROCS.close()


def player():
    return PROCS.player(VIDEO_UNIT)


def video_playing():
    item = player()
    return bool(item and item['url'] and not item['pause'])


def video_paused():
    item = player()
    return bool(item and item['url'] and item['pause'])


def video_state():
    item = player()
    return None if not item else ('paused' if item['pause'] else 'playing') if item['url'] else 'idle'


def start_video():
    env.service.execute('video', {'action': 'start', 'url': VIDEO_URL, 'name': VIDEO_TITLE})
    assert wait(lambda: video_playing() and str((snap().get('video') or {}).get('status')) == 'Playing', 8), \
        'fixture: the video did not start (%r, %r)' % (PROCS.calls[-3:], snap().get('errors'))
    env.settle(.5)


def room_layers():
    value = status()
    return value['music'], value['atmosphere']


def effects_since(mark):
    return [c for c in env.backend.calls[mark:] if c[0] in ('start_music', 'start_verified_music', 'start_atmosphere',
                                                         'start_picture')]


# =====================================================================================================
# Criterion 1: video in the room.

START_FORMS = (lambda url: ['video', url], lambda url: ['video', 'start', url],
               lambda url: ['video', json.dumps({'action': 'start', 'url': url})], lambda url: ['watch', url])
SHOW_FORMS = (['video'], ['video', 'show'], ['video', 'status'])
STOP_FORMS = (['video', 'stop'], ['video', json.dumps({'action': 'stop'})], ['stop-video'], ['unwatch'])


@section('VIDEO')
def roomctl_video():
    with video_harness():
        enter_room('northern-quiet', ambience=True)
        helptext = roomctl('--help').stdout
        check('V1 listening-roomctl --help names the video command', re.search(r'\bvideo\b', helptext, re.I) is not None,
              helptext[-400:])
        recipe = json.dumps({k: (env.controller.active or {}).get(k) for k in ('music', 'atmosphere', 'picture')}, sort_keys=True)
        mark, attempts, started = len(env.backend.calls), [], None
        for form in START_FORMS:
            result = roomctl(*form(VIDEO_URL))
            attempts.append((form(VIDEO_URL)[:2], result.returncode, result.stderr.strip()[-160:]))
            if wait(lambda: video_playing(), 4):
                started = form
                break
        loaded = [c for u, c in PROCS.ipc if u == VIDEO_UNIT and c[0] == 'loadfile']
        check('V2 listening-roomctl starts the room\'s video from a URL the user gives (that video plays)',
              started is not None and loaded and VIDEO_ID in loaded[-1][1], (attempts, loaded[-1:]))
        same = json.dumps({k: (env.controller.active or {}).get(k) for k in ('music', 'atmosphere', 'picture')}, sort_keys=True)
        check('V2b ... as Watch: the room stays the same room and nothing else starts',
              started is not None and same == recipe and not effects_since(mark), (effects_since(mark), same == recipe))
        if started is None:
            start_video()
        env.settle(1.0)
        name = (snap().get('video') or {}).get('name') or ''
        shown = []
        for form in SHOW_FORMS:
            result = roomctl(*form)
            shown.append((form, result.returncode, result.stdout.strip()[-300:]))
            if result.returncode == 0 and name and name in result.stdout and re.search(r'playing|watching', result.stdout, re.I):
                break
        check('V3 listening-roomctl shows the room\'s video: its name and that it plays',
              any(code == 0 and name and name in out and re.search(r'playing|watching', out, re.I) for _, code, out in shown),
              (name, shown))
        if started is None:
            start_video()       # the fixture's own start, so the show/stop checks judge their own verbs
        line = waybar_status()
        check('V4 while a video is watched, listening-roomctl status (Waybar) names it',
              bool(name) and name in ((line or {}).get('text', '') + (line or {}).get('tooltip', '')), (name, line))
        stopped = []
        for form in STOP_FORMS:
            result = roomctl(*form)
            stopped.append((form, result.returncode, result.stderr.strip()[-160:]))
            if wait(lambda: player() is None and not snap().get('video'), 3):
                break
        check('V5 listening-roomctl stops the room\'s video: the player closes and the room reports no video',
              player() is None and not snap().get('video'), (stopped, video_state(), snap().get('video')))
        check('V5b ... and stopping it resumes nothing (Resume stays explicit)', player() is None and not audible(),
              (video_state(), status()))
        spawned = len(PROCS.spawned(VIDEO_UNIT))
        bad = [roomctl(*form('https://example.test/not-a-video')) for form in START_FORMS[:2]]
        check('V6 with the start verb working, a link that is not a public YouTube video is refused (non-zero exit, a message) and opens nothing',
              started is not None and all(r.returncode != 0 and r.stderr.strip() for r in bad) and len(PROCS.spawned(VIDEO_UNIT)) == spawned,
              [(r.returncode, r.stderr.strip()[-120:]) for r in bad])


def fresh_watch():
    """Each surface starts from its own room paused by Watch, with the video playing."""
    reset()
    enter_room('northern-quiet', ambience=True)
    start_video()


def window_press():
    window = open_window()
    window.show_now()
    env.pump(.5)
    press_room(window)
    window.close()
    env.pump(.3)


SURFACES = (   # (label, pause, play): each the room transport from one surface
    ('V9/V11', 'listening-roomctl pause / play', lambda: roomctl('pause'), lambda: roomctl('play')),
    ('V12/V13', 'MPRIS Pause / Play', lambda: mpris('Pause'), lambda: mpris('Play')),
    ('V14', 'MPRIS PlayPause twice', lambda: mpris('PlayPause'), lambda: mpris('PlayPause')),
    ('V15', 'the media key (listening-hub toggle) twice', media_key, media_key),
    ('V16', 'the window\'s room Pause then Play', window_press, window_press),
)


@section('VIDEO')
def video_transport_with_room():
    with video_harness():
        fresh_watch()
        layers = room_layers()
        check('V7 (setup) Watch paused the room and the video plays', layers == ('Paused', 'Paused') and video_playing(),
              (layers, video_state()))
        check('V8 MPRIS reports Playing while the video plays (something is audible)', mpris_status() == 'Playing',
              (mpris_status(), layers, video_state()))
        for label, words, pause, play in SURFACES:
            fresh_watch()
            pause()
            paused = wait(video_paused, 2)
            after_pause = (video_state(), room_layers())
            if label == 'V9/V11':
                check('V10 ... the room reports the video as paused',
                      paused and wait(lambda: str((snap().get('video') or {}).get('status')) == 'Paused', 3), snap().get('video'))
                check('V10b ... and MPRIS then reports Paused', paused and mpris_status() == 'Paused', (mpris_status(), video_state()))
            play()
            resumed = wait(video_playing, 2)
            check('%s %s pauses the video and then resumes it; Music and Ambience stay paused throughout (Play restores '
                  'exactly what Pause paused)' % (label, words),
                  paused and resumed and after_pause[1] == ('Paused', 'Paused') and room_layers() == ('Paused', 'Paused'),
                  (after_pause, (video_state(), room_layers())))


@section('VIDEO')
def video_transport_everything():
    with video_harness():
        enter_room('northern-quiet', ambience=True)
        start_video()
        env.service.execute('resume', {})       # the listener presses Resume while watching
        env.settle(.8)
        before = (room_layers(), video_state())
        check('V17 (setup) the room resumed while the video plays', before == (('Playing', 'Playing'), 'playing'), before)
        roomctl('pause')
        paused = (room_layers(), video_state())
        roomctl('play')
        wait(lambda: video_playing() and room_layers() == ('Playing', 'Playing'), 2)
        after = (room_layers(), video_state())
        check('V18 with the room and the video playing, room Pause pauses all three and Play restores all three',
              paused == (('Paused', 'Paused'), 'paused') and after == before, (before, paused, after))


@section('VIDEO')
def video_without_room():
    with video_harness():
        start_video()
        mark = len(env.backend.calls)
        roomctl('pause')
        paused = wait(video_paused, 2)
        mpris_now = mpris_status()
        roomctl('play')
        resumed = wait(video_playing, 2)
        check('V19 with no room, room transport pauses and resumes the video', paused and resumed, (paused, resumed))
        check('V19b ... MPRIS reports the paused video as Paused, and no room is invented or started',
              mpris_now == 'Paused' and env.controller.active is None and not effects_since(mark),
              (mpris_now, env.controller.active, effects_since(mark)))


@section('VIDEO')
def leave_stops_video():
    with video_harness():
        enter_room('northern-quiet', ambience=True)
        start_video()
        roomctl('leave')
        gone = wait(lambda: player() is None and not snap().get('video'), LEAVE_SECONDS)
        check('V20 Leave stops the video: within %d s (a Leave fade) the player is closed and no video is reported'
              % LEAVE_SECONDS, gone, (video_state(), snap().get('video')))


@section('VIDEO')
def video_protocol_compatibility():
    import session_client
    with video_harness():
        enter_room('northern-quiet', ambience=True)
        session_client._exchange(controller_socket(), 'video', {'action': 'start', 'url': VIDEO_URL, 'name': VIDEO_TITLE}, 5)
        opened = wait(video_playing, 6)
        session_client._exchange(controller_socket(), 'video', {'action': 'stop'}, 5)
        closed = wait(lambda: player() is None, 4)
        check('X4 a 2.5.0 client\'s raw `video` start/stop request still opens and closes the video', opened and closed,
              (opened, closed))


# =====================================================================================================
# Criterion 2: partial pause is reported.

def says_paused(line, word):
    return re.search(r'\b%s\b[^·|]*\bpaused\b|\bpaused\b[^·|]*\b%s\b' % (word, word), (line or {}).get('text', ''), re.I)


@section('PARTIAL')
def partial_pause_status():
    reset()
    enter_room('northern-quiet', ambience=True)
    music, rain = layer_name('music'), layer_name('atmosphere')
    line = waybar_status()
    check('P0 with everything playing the line is the playing line (class playing, nothing said paused)',
          classes(line) == ['playing'] and 'paused' not in (line or {}).get('text', '').lower(), line)
    follow = Follow()
    try:
        follow.wait_for(lambda l: True, 0, 5)
        mark = time.monotonic()
        layer('music', 'pause')
        line = waybar_status()
        check('P1 with the music paused on its own, status says "Music paused" and names the ambience still playing (%s)'
              % rain, bool(rain) and says_paused(line, 'music') and rain in (line or {}).get('text', ''), line)
        check('P2 ... with a paused class', paused_class(line), classes(line))
        check('P3 ... and MPRIS PlaybackStatus is Playing (the ambience is audible)', mpris_status() == 'Playing',
              mpris_status())
        delay = paused_delay = follow.wait_for(lambda l: says_paused(l, 'music') and paused_class(l), mark, CHANGE_SECONDS)
        check('P4 status --follow prints the "Music paused" line with the paused class within %d s' % CHANGE_SECONDS,
              delay is not None, [l for _, l in follow.lines[-3:]])
        mark = time.monotonic()
        layer('music', 'play')
        delay = follow.wait_for(lambda l: classes(l) == ['playing'] and 'paused' not in l.get('text', '').lower(),
                                mark, CHANGE_SECONDS)
        check('P5 ... and the playing line again within %d s once the music plays' % CHANGE_SECONDS,
              paused_delay is not None and delay is not None,
              [l for _, l in follow.lines[-3:]])
    finally:
        follow.close()
    layer('atmosphere', 'pause')
    line = waybar_status()
    check('P6 with the ambience paused on its own, status says the ambience is paused, names the music (%s) and has '
          'the paused class' % music,
          (says_paused(line, 'ambience') or (rain and says_paused(line, re.escape(rain))))
          and bool(music) and music in (line or {}).get('text', '') and paused_class(line), line)
    layer('atmosphere', 'play')
    layer('music', 'stop')
    line = waybar_status()
    check('P7 a stopped layer is not reported as paused (music stopped, ambience playing: no "paused", class playing)',
          not says_paused(line, 'music') and classes(line) == ['playing'], line)
    reset()
    enter_room('northern-quiet', ambience=True)
    env.service.execute('transport', {'action': 'pause'})
    env.settle(.6)
    line = waybar_status()
    check('P8 the whole room paused keeps the 2.x paused line', paused_class(line) and status_line_problem(line, snap()) is None,
          (line, status_line_problem(line, snap())))
    problems = [(status_line_problem(l, s), l) for l, s in STATUS_LINES if status_line_problem(l, s)]
    check('P9 every status line read in this probe stays parseable and keeps the 2.x line outside partial pause and '
          'video (revised WP2 status oracle)', not problems, problems[:3])


# =====================================================================================================
# Criterion 3: the popover's per-layer line; the mini player keeps one room Pause/Play.

LAYER_WORDS = {'music': r'\bmusic\b', 'atmosphere': r'\bambience\b'}


def layer_items(root, name):
    regex = re.compile(LAYER_WORDS[name], re.I)
    other = re.compile(r'volume|level|change|choose|timer|leave|wind|break', re.I)
    return [w for w in mapped_buttons(root) if regex.search(' '.join(button_name(w))) and not other.search(' '.join(button_name(w)))]


def room_buttons(root):
    """Room-level Pause/Play: pause/play buttons (by name or icon) that are not about one layer."""
    found = []
    for widget in mapped_buttons(root):
        text = ' '.join(button_name(widget))
        if re.search(LAYER_WORDS['music'] + '|' + LAYER_WORDS['atmosphere'], text, re.I):
            continue
        if re.search(r'^(pause|play|resume)\b|\b(pause|play) the room\b', text, re.I) or \
                any(re.match(r'media-playback-(start|pause)', icon) for icon in icons(widget)):
            found.append(widget)
    return found


@section('POPOVER')
def popover_layer_line():
    import next_mini
    import session_client
    reset()
    enter_room('northern-quiet', ambience=True)
    host = next_mini.PopoverHost(env.app, session_client.request, launch=lambda arguments: None)
    env.pump(1.5)
    window = host.window
    music, rain = layer_items(window, 'music'), layer_items(window, 'atmosphere')
    env.shot(window, 'o11c-popover-playing')
    check('U1 the popover shows a tappable Music item and a tappable Ambience item',
          len(music) == 1 and len(rain) == 1, ([button_name(w) for w in music], [button_name(w) for w in rain]))
    room = [w for w in env.widgets(window) if isinstance(w, env.Gtk.Label) and w.get_mapped() and w.get_text() == TARGET]
    placed = bool(music and rain and room)
    if placed:
        name_box = bounds(room[0], window)
        placed = all(bounds(w, window) and bounds(w, window)[1] >= name_box[1] + name_box[3] - 1 for w in music + rain)
    check('U2 ... under the room name', placed, (room and bounds(room[0], window),
                                                [bounds(w, window) for w in music + rain]))
    check('U3 the popover keeps one room Pause/Play besides the per-layer items', len(room_buttons(window)) == 1,
          [button_name(w) for w in room_buttons(window)])
    if music and rain:
        playing_look, rain_look = look(music[0]), look(rain[0])
        music[0].emit('clicked')
        env.settle(1.0)
        after = status()
        music = layer_items(window, 'music') or music
        paused_look = look(music[0])
        check('U4 tapping Music pauses the music alone (the ambience keeps playing)',
              after['music'] == 'Paused' and after['atmosphere'] == 'Playing', after)
        check('U5 ... and the Music item then shows it is paused (its look changed), the Ambience item unchanged',
              paused_look != playing_look and look((layer_items(window, 'atmosphere') or rain)[0]) == rain_look,
              (playing_look, paused_look))
        music[0].emit('clicked')
        env.settle(1.0)
        check('U6 tapping Music again plays it', after['music'] == 'Paused' and status()['music'] == 'Playing'
              and status()['atmosphere'] == 'Playing', (after, status()))
        rain = layer_items(window, 'atmosphere') or rain
        rain[0].emit('clicked')
        env.settle(1.0)
        first = status()
        rain = layer_items(window, 'atmosphere') or rain
        rain[0].emit('clicked')
        env.settle(1.0)
        check('U7 tapping Ambience pauses the ambience alone, and again plays it',
              first['atmosphere'] == 'Paused' and first['music'] == 'Playing' and status()['atmosphere'] == 'Playing', (first, status()))
        roomctl('music', 'pause')
        shown = wait(lambda: layer_items(window, 'music') and look(layer_items(window, 'music')[0]) == paused_look, CHANGE_SECONDS)
        check('U8 a pause made elsewhere (listening-roomctl music pause) shows on the popover line within %d s'
              % CHANGE_SECONDS, shown, (status(), [look(w) for w in layer_items(window, 'music')]))
        env.shot(window, 'o11c-popover-music-paused')
    else:
        for name in ('U4', 'U5', 'U6', 'U7', 'U8'):
            check(name + ' (no per-layer items to tap)', False)
    window.close()
    env.pump(.3)
    main = open_window()
    main.set_default_size(420, 700)
    env.pump(1.5)
    mini = getattr(main, 'mini_player', None)
    root = mini if mini is not None and mini.get_mapped() else main
    check('U9 the mini player keeps one room Pause/Play and no per-layer items',
          getattr(main, 'mini_layout', None) is True and len(room_buttons(root)) == 1
          and not layer_items(root, 'music') and not layer_items(root, 'atmosphere'),
          (getattr(main, 'mini_layout', None), [button_name(w) for w in room_buttons(root)],
           [button_name(w) for w in layer_items(root, 'music') + layer_items(root, 'atmosphere')]))
    main.close()


# =====================================================================================================
# Criterion 4: Mix keys, rows and the shortcut list.

def mix_levels():
    import next_model
    rows = {row['layer']: row for row in next_model.mix_rows(snap())}
    return {name: rows.get(name, {}).get('level') for name in ('music', 'atmosphere')}, snap().get('room_volume')


def row_of(window, title):
    """The Mix row whose title label reads `title` (the nearest ancestor holding that label and a slider)."""
    mix = mix_widget(window)
    label = next((w for w in env.widgets(mix) if isinstance(w, env.Gtk.Label) and w.get_mapped() and w.get_text() == title), None)
    node = label
    while node is not None and node is not mix:
        if any(isinstance(w, env.Gtk.Scale) and w.get_mapped() for w in env.widgets(node)):
            return node
        node = node.get_parent()
    return None


def focus_row(window, title):
    row = row_of(window, title)
    target = next((w for w in env.widgets(row) if isinstance(w, env.Gtk.Button) and not isinstance(w, env.Gtk.MenuButton)
                   and w.get_mapped() and w.get_focusable()), None) if row is not None else None
    if target is not None:
        target.grab_focus()
        env.pump(.2)
    return target is not None and window.get_focus() is target


@section('KEYS')
def mix_keys():
    Gdk = env.Gdk
    reset()
    enter_room('northern-quiet', ambience=True)
    window = open_window()
    open_mix(window)
    deliver_key(window, Gdk.KEY_m)
    first = status()
    deliver_key(window, Gdk.KEY_m)
    second = status()
    check('M1 in the Mix, M pauses the music alone', first['music'] == 'Paused' and first['atmosphere'] == 'Playing', first)
    keys_work = first['music'] == 'Paused'
    check('M2 ... and M again plays it', keys_work and second['music'] == 'Playing' and second['atmosphere'] == 'Playing',
          (first, second))
    deliver_key(window, Gdk.KEY_a)
    first = status()
    deliver_key(window, Gdk.KEY_a)
    second = status()
    check('M3 in the Mix, A pauses the ambience alone', first['atmosphere'] == 'Paused' and first['music'] == 'Playing', first)
    check('M4 ... and A again plays it', first['atmosphere'] == 'Paused' and second['atmosphere'] == 'Playing'
          and second['music'] == 'Playing', (first, second))
    focused = focus_row(window, 'Music')
    before = mix_levels()
    deliver_key(window, Gdk.KEY_Up, Gdk.ModifierType.SHIFT_MASK)
    up = mix_levels()
    deliver_key(window, Gdk.KEY_Down, Gdk.ModifierType.SHIFT_MASK)
    deliver_key(window, Gdk.KEY_Down, Gdk.ModifierType.SHIFT_MASK)
    down = mix_levels()
    check('M5 Shift+Up with the Music row focused raises the music level, and neither the ambience level nor the room volume',
          focused and up[0]['music'] > before[0]['music'] and up[0]['atmosphere'] == before[0]['atmosphere'] and up[1] == before[1],
          (focused, before, up))
    check('M6 Shift+Down lowers it again, the same way', focused and down[0]['music'] < up[0]['music']
          and down[0]['atmosphere'] == before[0]['atmosphere'] and down[1] == before[1], (up, down))
    focused = focus_row(window, 'Ambience')
    before = mix_levels()
    deliver_key(window, Gdk.KEY_Up, Gdk.ModifierType.SHIFT_MASK)
    up = mix_levels()
    check('M7 Shift+Up with the Ambience row focused raises the ambience level alone',
          focused and up[0]['atmosphere'] > before[0]['atmosphere'] and up[0]['music'] == before[0]['music'] and up[1] == before[1],
          (focused, before, up))
    window.sheet.set_open(False)
    env.pump(.4)
    window.show_listen()
    env.pump(.8)
    entry = getattr(getattr(window, 'listen', None), 'search', None)
    if entry is None or not entry.get_mapped():
        entry = next((w for w in env.widgets(window) if isinstance(w, env.Gtk.Editable) and w.get_mapped()), None)
    typed = entry is not None and entry.grab_focus()
    env.pump(.2)
    before = status()
    for keyval in (Gdk.KEY_m, Gdk.KEY_a):
        deliver_key(window, keyval)
    check('M8 with M working in the Mix, typing M or A in a text entry (Listen search) changes no layer',
          keys_work and typed and status() == before, (keys_work, typed, before, status()))
    window.close()
    env.pump(.3)


def shortcut_items(window):
    window.activate_action('win.shortcuts', None)
    env.pump(1.0)
    dialog = window.get_visible_dialog() if hasattr(window, 'get_visible_dialog') else None
    items = []
    if dialog is not None:
        body = dialog.get_body() if hasattr(dialog, 'get_body') else ''
        items += [line for line in (body or '').split('\n') if line.strip()]
        for widget in env.widgets(dialog):
            accel = widget.get_accelerator() if hasattr(widget, 'get_accelerator') else None
            if accel:
                holder = widget.get_parent().get_parent() if widget.get_parent() else widget
                items.append(accel + ' ' + ' '.join(env.texts(holder)))
        for closer in ('force_close', 'close'):
            if hasattr(dialog, closer):
                getattr(dialog, closer)()
                break
        env.pump(.4)
    return items


@section('KEYS')
def shortcut_list():
    reset()
    enter_room('northern-quiet', ambience=True)
    window = open_window()
    items = shortcut_items(window)
    window.close()
    key = lambda letter: r'(^|[\s>+])%s($|[\s,])' % letter
    music = any(re.search(r'music', i, re.I) and re.search(key('m'), i, re.I) for i in items)
    rain = any(re.search(r'ambience', i, re.I) and re.search(key('a'), i, re.I) for i in items)
    level = any(re.search(r'shift', i, re.I) and re.search(r'up|↑', i, re.I) for i in items)
    check('M9 the Keyboard shortcuts list names M (music), A (ambience) and Shift+Up/Down (the focused row\'s level)',
          music and rain and level, items)


def single_menus(window):
    found = []
    for widget in env.widgets(window):
        if isinstance(widget, env.Gtk.MenuButton) and widget.get_mapped():
            popover = widget.get_popover()
            child = popover.get_child() if popover is not None else None
            if child is None:
                continue
            visible = [w for w in env.widgets(child) if isinstance(w, env.Gtk.Button) and w.get_visible()
                       and all(p.get_visible() for p in (w.get_parent(),) if p is not None)]
            if len(visible) == 1:
                found.append(button_name(widget))
    return found


@section('ROWS')
def row_buttons():
    reset()
    enter_room('northern-quiet', ambience=True)
    window = open_window()
    open_mix(window)
    row = row_of(window, 'Music')
    stop = [w for w in mapped_buttons(row, r'^stop\b')] if row is not None else []
    check('R1 radio room: Stop is a button on the Music row, and no Mix menu holds a single item',
          bool(stop) and not single_menus(mix_widget(window)), ([button_name(w) for w in stop], single_menus(mix_widget(window))))
    env.shot(window, 'o11c-mix-radio')
    window.close()
    reset()
    start_library_room()
    window = open_window()
    open_mix(window)
    row = row_of(window, 'Music')
    nexts = mapped_buttons(row, r'next track') if row is not None else []
    check('R2 library room: Next track is a button on the Music row (not only in the ⋯ menu)', bool(nexts),
          [button_name(w) for w in mapped_buttons(row)] if row is not None else 'no Music row')
    env.shot(window, 'o11c-mix-library')
    window.close()


# =====================================================================================================
# Criterion 5 (N9): connecting shows only progress and an honest Cancel.

PROGRESS = ('Spinner', 'ProgressBar', 'LevelBar')


def connecting_view(window):
    mix = mix_widget(window)
    progress = [w for w in env.widgets(mix) if w.get_mapped() and type(w).__name__ in PROGRESS]
    cancel = [w for w in mapped_buttons(mix, r'^cancel$')]
    closing = re.compile(r'close mix|close', re.I)
    others = []
    for w in env.widgets(mix):
        if not w.get_mapped() or in_popover(w) or w in cancel:
            continue
        if isinstance(w, (env.Gtk.Button, env.Gtk.Scale, env.Gtk.Switch, env.Gtk.DropDown)) and w.get_sensitive():
            name = ' '.join(button_name(w)) if isinstance(w, env.Gtk.Button) else type(w).__name__
            if isinstance(w, env.Gtk.Button) and closing.fullmatch(button_name(w)[1] or button_name(w)[0] or ''):
                continue
            others.append(name)
    return progress, cancel, others


def cancel_while_connecting(label, room_name):
    window = open_window('rooms')
    env.start_delay = CONNECT_SECONDS
    mark = len(env.backend.calls)
    started = time.monotonic()
    enter_from_rooms(window, room_name)
    connecting = wait(lambda: bool(env.controller.transition), 3)
    open_mix(window)
    progress, cancel, others = connecting_view(window)
    env.shot(window, 'o11c-connecting-' + label)
    if not cancel:
        cancel = mapped_buttons(window, r'^cancel$')
    if cancel:
        cancel[0].emit('clicked')
        clicked = time.monotonic()
        env.pump(.2)
    else:
        clicked = None
    return window, mark, started, clicked, connecting, progress, cancel, others


def no_connect_state():
    shot = snap()
    return not shot.get('transition') and (shot.get('entry') or {}).get('state') != 'connecting'


@section('CONNECT')
def cancel_from_idle():
    reset()
    window, mark, started, clicked, connecting, progress, cancel, others = cancel_while_connecting('idle', TARGET)
    check('K1 (setup) the room was connecting when the Mix was opened', connecting, snap().get('transition'))
    check('K2 while connecting, the Mix shows a progress indicator and Cancel', bool(progress) and bool(cancel),
          ([type(w).__name__ for w in progress], [button_name(w) for w in cancel]))
    check('K3 ... and no other control (the sheet\'s close aside)', bool(cancel) and not others, others)
    restored = clicked is not None and wait(lambda: no_connect_state() and env.controller.active is None, CANCEL_SECONDS)
    check('K4 Cancel abandons the attempt in the controller within %d s: not connecting, back to idle' % CANCEL_SECONDS,
          restored, (snap().get('transition'), snap().get('entry'), (env.controller.active or {}).get('name')))
    wait(lambda: False, max(0., started + CONNECT_SECONDS + 2 - time.monotonic()))
    check('K5 nothing starts afterwards: past the moment the connect would have finished, no layer started and the room '
          'is still idle', clicked is not None and not effects_since(mark) and not audible() and env.controller.active is None,
          (effects_since(mark), status(), (env.controller.active or {}).get('name')))
    check('K6 ... and the window no longer shows the connecting Cancel', clicked is not None and not mapped_buttons(window, r'^cancel$'),
          [button_name(w) for w in mapped_buttons(window, r'^cancel$')])
    window.close()


@section('CONNECT')
def cancel_from_playing_room():
    reset()
    enter_room('northern-quiet', ambience=True)
    before = ((env.controller.active or {}).get('id'), (snap()['layers'].get('music') or {}).get('source_id'), status())
    window, mark, started, clicked, connecting, progress, cancel, others = cancel_while_connecting('switching', 'Night drive')
    check('K7 (setup) switching to another room was connecting, with Cancel shown', connecting and bool(cancel),
          (snap().get('transition'), [button_name(w) for w in cancel]))

    def now():
        return ((env.controller.active or {}).get('id'), (snap()['layers'].get('music') or {}).get('source_id'), status())
    restored = clicked is not None and wait(lambda: no_connect_state() and now() == before, CANCEL_SECONDS)
    check('K8 Cancel while switching restores the previous room within %d s: same room, same music, playing as before'
          % CANCEL_SECONDS, restored, (before, now(), snap().get('entry')))
    wait(lambda: False, max(0., started + CONNECT_SECONDS + 2 - time.monotonic()))
    check('K9 ... and nothing of the target starts afterwards: the previous room is still the one playing',
          clicked is not None and not effects_since(mark) and now() == before, (effects_since(mark), before, now()))
    window.close()


# =====================================================================================================
# Criterion 6: additive protocol, CHANGELOG. (O10's C6 checks in o10c stay the 2.4.0 guard.)

ACTIONS_250 = json.loads('''{"atmosphere": {"id": "text", "source_id": "text"}, "delete": {}, "enter": {"atmosphere": "object",
"music": "object", "picture": "object", "recipe": "object", "room_id": "text", "variant_id": "text"}, "get": {}, "inspect": {},
"layer": {"action": "text", "layer": "text", "minutes": "number"}, "leave": {"fade_seconds": "number"}, "link": {"destination": "text"},
"metadata.analyze": {}, "metadata.cancel": {}, "metadata.configure_acoustid": {}, "metadata.confirm_identity": {}, "metadata.enrich": {},
"metadata.export": {}, "metadata.import": {}, "metadata.inspect": {}, "metadata.jobs": {}, "metadata.list": {},
"metadata.lookup_reference": {}, "metadata.match": {}, "metadata.review": {}, "metadata.revoke": {}, "metadata.status": {},
"music": {"item": "object", "source_id": "text"}, "online": {}, "picture": {"action": "text", "minutes": "number"},
"quiet": {"minutes": "number"}, "rename": {}, "resume": {}, "return": {}, "room_volume": {}, "save": {"atmosphere": "object",
"music": "object", "picture": "object"}, "settings": {}, "station.delete": {}, "station.feedback": {}, "station.hold": {},
"station.list": {}, "station.play_next": {}, "station.recent": {}, "station.replay": {}, "station.reset_memory": {},
"station.save": {}, "station.start": {}, "station.update": {"preferences": "object"}, "stop": {}, "subscribe": {"fields": "list"},
"transport": {"action": "text"}, "variants": {}, "video": {"action": "text", "name": "text", "url": "text"},
"wind_down": {"minutes": "number"}}''')   # the 2.5.0 registry (main 70db15e): name -> argument kinds
QUEUED_250 = {'atmosphere', 'layer', 'link', 'picture', 'save'}   # queued when sent with no arguments
SNAPSHOT_KEYS_250 = {'active', 'audio_policy', 'config_generation', 'entry', 'errors', 'last_entered_at', 'last_session',
                     'layers', 'levels', 'media', 'now', 'recent', 'resume_offer', 'room_volume', 'settings', 'station',
                     'station_learned', 'station_recent', 'stations', 'timers', 'transition', 'variants', 'video'}


@section('C6')
def additive():
    import room_session
    missing = sorted(set(ACTIONS_250) - set(room_session.ACTIONS))
    changed = sorted(name for name, kinds in ACTIONS_250.items() if name in room_session.ACTIONS
                     and any(room_session.ACTIONS[name].args.get(k) != v for k, v in kinds.items()))
    check('X1 every 2.5.0 controller action still exists with the same argument types (changes are additive)',
          not missing and not changed, (missing, changed))
    flipped = sorted(n for n in ACTIONS_250 if n in room_session.ACTIONS
                     and room_session.ACTIONS[n].runs_immediately({}) == (n in QUEUED_250))
    flipped += ['video %s' % a for a in ('stop',) if 'video' in room_session.ACTIONS
                and not room_session.ACTIONS['video'].runs_immediately({'action': a})]
    flipped += ['transport %s' % a for a in ('pause', 'toggle') if 'transport' in room_session.ACTIONS
                and not room_session.ACTIONS['transport'].runs_immediately({'action': a})]
    check('X2 ... and what ran at once in 2.5.0 still runs at once (stop, quiet, resume, transport, video stop)', not flipped, flipped)
    reset()
    enter_room('northern-quiet', ambience=True)
    keys = set(snap())
    check('X3 the snapshot keeps every 2.5.0 field', SNAPSHOT_KEYS_250 <= keys, sorted(SNAPSHOT_KEYS_250 - keys))
    changelog = (REPO / 'CHANGELOG.md').read_text(errors='replace')
    head = changelog.split('## [2.5.0]', 1)[0]
    check('X5 the CHANGELOG records the change above 2.5.0: the video from roomctl and room transport, and partial '
          'pause in the status', re.search(r'video', head, re.I) is not None and re.search(r'roomctl', head) is not None
          and re.search(r'paused', head, re.I) is not None, head[-600:])


def finish():
    if _socket:
        _socket['server'].shutdown()
        _socket['server'].server_close()
    if _mpris.get('pool'):
        _mpris['pool'].shutdown(wait=False)
    write_results()
    failed = [name for name, ok in checks if not ok]
    print('%d passed, %d failed' % (len(checks) - len(failed), len(failed)), flush=True)
    env.__exit__(None, None, None)


finish()
