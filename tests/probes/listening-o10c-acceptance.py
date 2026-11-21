"""O10-CONTROL acceptance: the room's transport does what it says, from every surface, and failures show.

Written by the acceptance author from the O10-CONTROL brief and the 2026-11-21 UX assessment (N2, N3,
N9, N10, Proposal E, desktop integration), not from the implementation. Real GTK on a private Xvfb with
its own D-Bus (tests/probes/o6_env.py re-executes this file there), the real session controller on the
session test double and fixture providers. Four surfaces are driven:

  window   the real NextWindow: Rooms cards, Now's room Pause/Play, the failure card, the Mix rows
  MPRIS    session_service.Mpris owning its name on the probe's private session bus, called over D-Bus
           from a second connection exactly as playerctl/Waybar would
  keys     `listening-hub toggle` (what the XF86 media keys run), in process, with the player list
           pinned to the room's radio entry so nothing reaches playerctl or rofi
  roomctl  the real `roomctl.py` as a subprocess against the real controller socket
           (session_service.make_server on a private runtime dir; DISPLAY unset so it can never try to
           start a service)

Nothing reaches the desktop session, the network, systemd or real credentials; no audio is played.

Readings of the brief this probe takes (see the commit message):
  * N2 "a room that fails" = nothing in the room can start (music, ambience and scene all fail). That
    shows the target-named failure card and keeps the previous state. A failed music layer *alone*
    (ambience fine) enters the room from idle: the ambience plays and the failure shows on the music layer.
    While another room plays, it shows the target card and keeps that room intact instead (coordinator
    decision in phase 2: a failed start never disturbs what plays).
  * Criterion 3 lets the implementer pick the rule for individually paused layers; the oracle only
    requires Pause -> Resume to restore exactly what played, and that Play on a room that is silent
    because every layer was paused separately is never a no-op, and gives the same result from the
    window, MPRIS and roomctl.
  * The outcome says stopping behaves the same everywhere: MPRIS Stop leaves nothing audible.

  scripts/listening-dev.sh probe OUT listening-o10c-acceptance
  (O10C_SECTIONS=N2,MPRIS limits the run to those sections)
"""
import copy
import concurrent.futures
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from unittest import mock

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o10c-acceptance', extra_args={'--repo': {'default': ''}})
env.__enter__()
REPO = Path(env.args.repo or Path(__file__).resolve().parents[2])
ONLY = set(filter(None, os.environ.get('O10C_SECTIONS', '').split(',')))
checks = []
RAIN = {'kind': 'local', 'source_id': 'rain-glass', 'volume': 20, 'muted': False, 'window_open': 35}
TARGET = 'Northern quiet'           # a catalogue radio room (music + scene, no ambience)
STORED_VOLUME = 70


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:500]), flush=True)


def write_results():
    (env.output / 'o10c-acceptance.json').write_text(json.dumps(
        [{'criterion': name, 'pass': ok} for name, ok in checks], indent=1))


def reset():
    for window in list(env.app.get_windows()):
        window.close()
    env.pump(.3)
    env.start_error = env.atmosphere_error = None
    env.start_delay = 0.
    env.backend.fail_picture = False
    if env.controller.active:
        env.service.execute('leave', {'fade_seconds': 0})
    env.settle(.6)


def section(name):
    def wrap(function):
        if ONLY and name not in ONLY:
            return function
        print('== section %s' % name, flush=True)
        try:
            function()
        except Exception as error:   # one broken section never hides the others
            check('%s: section ran without raising' % name, False, '%s: %s\n%s' % (
                type(error).__name__, error, traceback.format_exc()[-1500:]))
        finally:
            try:
                reset()
            except Exception:
                traceback.print_exc()
            write_results()
        return function
    return wrap


# ---- the test double's transport --------------------------------------------------------------------
# mpv cycles pause on toggle/play-pause; the session double ignores both, which would make a toggle look
# broken for the wrong reason. Give it mpv's meaning.
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
    """A room playing a library album (a queue, so it has track controls), as Play on a Library row sends it."""
    tracks = copy.deepcopy(o6_env.TRACKS)
    env.service.execute('music', {'item': {'name': 'Neoclassical collection', 'kind': 'radio', 'reference': tracks[0],
                                           'queue': tracks, 'queue_index': 0}})


def layer(name, action):
    env.service.execute('layer', {'layer': name, 'action': action})
    env.settle(.5)


def mapped_texts(root):
    found = set()
    for widget in env.widgets(root):
        if not widget.get_mapped():
            continue
        for getter in ('get_label', 'get_text', 'get_title', 'get_subtitle'):
            value = getattr(widget, getter, lambda: None)()
            if isinstance(value, str) and value.strip():
                found.add(value)
    return found


def in_popover(widget):
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, env.Gtk.Popover):
            return True
        parent = parent.get_parent()
    return False


def button_name(widget):
    label = widget.get_label() if hasattr(widget, 'get_label') else None
    if not label:
        label = ' '.join(item.get_text() for item in env.widgets(widget)
                         if isinstance(item, env.Gtk.Label) and item.get_text())
    return (label or '').strip(), (widget.get_tooltip_text() or '').strip()


def mapped_buttons(root, pattern):
    """Mapped buttons outside popovers whose label or tooltip matches the regex (case-insensitive)."""
    regex = re.compile(pattern, re.I)
    return [w for w in env.widgets(root) if isinstance(w, env.Gtk.Button) and w.get_mapped() and not in_popover(w)
            and any(regex.search(part) for part in button_name(w) if part)]


def now_page(window):
    return window.pages.get_visible_child() if window.current_page() == 'now' else None


def failure_card(window, room):
    """The pending failure card for `room`: mapped Try again and Back, and the room named in mapped text."""
    page = now_page(window)
    if page is None:
        return False
    texts = mapped_texts(page)
    return bool(mapped_buttons(page, r'^try again$') and mapped_buttons(page, r'^back$')
                and any(room in text for text in texts))


def room_button(window):
    """Now's room Pause/Play."""
    button = getattr(window, 'play', None)
    if button is None:
        found = mapped_buttons(window, r'(pause|play) the room')
        button = found[0] if found else None
    assert button is not None, 'no room Pause/Play button on Now'
    return button


def press_room(window):
    env.pump(.4)
    room_button(window).emit('clicked')
    env.settle(.8)


def open_window(page=None):
    window = env.launch(page=page)
    env.pump(.8)
    return window


ROOMS_PAGE = []


def enter_from_rooms(window, name):
    window.show_rooms()
    env.pump(.6)
    ROOMS_PAGE[:] = [window.current_page()]
    entry = next((e for e in window.rooms.card_for.values() if e.get('name') == name), None)
    assert entry is not None, 'no Rooms card named %r (have %r)' % (name, [e.get('name') for e in window.rooms.card_for.values()])
    window.rooms.open_preview(entry)
    env.pump(.4)
    window.rooms.press_enter()
    env.pump(.2)
    return entry


# ---- MPRIS over the private session bus ---------------------------------------------------------------
_mpris = {}


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


def _run(function):
    future = (_mpris.get('pool') or concurrent.futures.ThreadPoolExecutor(1)).submit(function)
    while not future.done():
        env.pump(.03)
    return future.result()


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


def mpris_values():
    mpris_bus()
    env.settle(.3)
    values = _try_values()
    assert values is not None, 'MPRIS GetAll failed'
    return values


def mpris(method):
    bus = mpris_bus()
    _call(bus['module'].PLAYER_INTERFACE, method)
    env.settle(.9)


def metadata_strings(values):
    found = []
    for value in (values.get('Metadata') or {}).values():
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(v for v in value if isinstance(v, str))
    return [v for v in found if not v.startswith('/') and not v.startswith('file:')]


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
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or env.folder) / 'o10c-session'
        runtime.mkdir(mode=0o700, exist_ok=True)
        path = runtime / 'session.sock'
        server, descriptor = session_service.make_server(path, env.service)
        assert server is not None, 'could not bind the private controller socket'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _socket.update(path=path, server=server, descriptor=descriptor)
    return _socket['path']


def roomctl(*argv, timeout=15):
    path = controller_socket()
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE')}
    environment.update(LISTENING_SESSION_SOCKET=str(path), PYTHONPATH=os.pathsep.join([str(env.source), str(REPO / 'tests')]),
                       PYTHONDONTWRITEBYTECODE='1')
    pool = concurrent.futures.ThreadPoolExecutor(1)
    future = pool.submit(subprocess.run, [sys.executable, str(env.source / 'roomctl.py')] + list(argv),
                         capture_output=True, text=True, timeout=timeout, env=environment)
    while not future.done():
        env.pump(.05)
    result = future.result()
    pool.shutdown()
    env.settle(.8)
    return result


def raw_socket(action, args):
    import session_client
    return session_client._exchange(controller_socket(), action, args, 5)


# =====================================================================================================
# Criterion 1 (N2): a failed Enter is never silent.

@section('N2')
def n2_room_fails_from_idle():
    reset()
    env.start_error = '[Errno 111] Connection refused'
    env.atmosphere_error = 'Ambience could not load'
    env.backend.fail_picture = True
    window = open_window('rooms')
    mark = len(env.log)
    enter_from_rooms(window, TARGET)
    went_back = []
    deadline = time.monotonic() + 11      # past the window's start grace (8 s)
    while time.monotonic() < deadline:
        env.pump(.25)
        if window.current_page() != 'now' and not failure_card(window, TARGET):
            went_back.append(round(11 - (deadline - time.monotonic()), 1))
    env.shot(window, 'o10c-n2-idle-failed')
    check('N2a from idle, a room that fails shows the failure card on Now named after the target, with Try again and Back',
          failure_card(window, TARGET), (window.current_page(), sorted(mapped_texts(window))[:30]))
    check('N2b ... and the window never went back to Rooms while it failed',
          not went_back and any(c[0] == 'enter' for c in env.log[mark:]), went_back[:5])
    check('N2c ... and nothing was left playing (the idle state is intact)', not audible(), status())
    # Try again resends the same target exactly once; with the fault gone the target enters.
    env.start_error = env.atmosphere_error = None
    env.backend.fail_picture = False
    mark = len(env.log)
    retry = mapped_buttons(now_page(window) or window, r'^try again$')
    if retry:
        retry[0].emit('clicked')
    wait(lambda: (snap().get('active') or {}).get('name') == TARGET and 'music' in audible(), 8)
    enters = [c for c in env.commands(mark) if c[0] in ('enter', 'return')]
    check('N2d Try again sends one enter for the same room and, with the fault gone, that room plays',
          retry and len(enters) == 1 and (snap().get('active') or {}).get('name') == TARGET and 'music' in audible(),
          (bool(retry), enters, (snap().get('active') or {}).get('name')))
    window.close()
    reset()
    # Back returns to where the enter came from.
    env.start_error = '[Errno 111] Connection refused'
    env.atmosphere_error = 'Ambience could not load'
    env.backend.fail_picture = True
    window = open_window('rooms')
    enter_from_rooms(window, TARGET)
    wait(lambda: failure_card(window, TARGET), 11)
    back = mapped_buttons(now_page(window) or window, r'^back$')
    if back:
        back[0].emit('clicked')
    env.pump(.8)
    check('N2e Back from that card returns to Rooms and the card is gone, still with nothing playing',
          back and window.current_page() in ROOMS_PAGE and not failure_card(window, TARGET) and not audible(),
          (bool(back), window.current_page()))


def previous_room():
    """The room already playing when another is entered: a catalogue room never named after a target.

    [Revised in phase 2] The first version used o6_env.start_station_room(); its station room inherits the name of
    the last room entered, so after an earlier scenario had entered the target it was itself called after the target
    and the Rooms card only showed Now (no enter was sent). The criterion is unchanged."""
    enter_room('midnight-library', ambience=False)
    return (snap().get('active') or {}).get('name')


@section('N2')
def n2_room_fails_while_playing():
    reset()
    before_active = previous_room()
    before = {k: {f: v.get(f) for f in ('status', 'source_id', 'name')} for k, v in (snap().get('layers') or {}).items()}
    window = open_window('rooms')
    env.start_error = '[Errno 111] Connection refused'
    env.atmosphere_error = 'Ambience could not load'
    env.backend.fail_picture = True
    mark = len(env.log)
    enter_from_rooms(window, TARGET)
    wait(lambda: failure_card(window, TARGET), 11)
    env.pump(1.0)
    env.shot(window, 'o10c-n2-playing-failed')
    check('N2f0 (setup) the playing room is not the target and the window sent an enter for the target',
          before_active and before_active != TARGET and any(c[0] == 'enter' for c in env.log[mark:]),
          (before_active, env.commands(mark)))
    page = now_page(window)
    texts = mapped_texts(page) if page is not None else set()
    check('N2f while another room plays, a room that fails shows the failure card named after the target '
          '(not the playing room), with Try again and Back',
          failure_card(window, TARGET), sorted(texts)[:30])
    after = {k: {f: v.get(f) for f in ('status', 'source_id', 'name')} for k, v in (snap().get('layers') or {}).items()}
    check('N2g ... and the previous room is intact: same room, same layers, still playing',
          (snap().get('active') or {}).get('name') == before_active and after == before and 'music' in audible(),
          ((snap().get('active') or {}).get('name'), after))
    back = mapped_buttons(page or window, r'^back$')
    if back:
        back[0].emit('clicked')
    env.pump(.8)
    check('N2h Back keeps the previous room playing and the card goes away',
          back and not failure_card(window, TARGET) and (snap().get('active') or {}).get('name') == before_active
          and 'music' in audible(), (bool(back), window.current_page()))


def rainy_variant():
    enter_room('northern-quiet', ambience=True)
    env.service.execute('save', {'name': 'Rainy quiet'})
    env.settle(1.0)
    env.service.execute('leave', {'fade_seconds': 0})
    env.settle(.6)


def music_failure_shown(window):
    page = now_page(window)
    roots = [r for r in (page, getattr(window, 'mix', None)) if r is not None]
    for root in roots:
        if mapped_buttons(root, r'^try again$'):
            return True
        if any(re.search(r'couldn.t|can.t (start|play|load)|failed', t, re.I) for t in mapped_texts(root)):
            return True
    return False


@section('N2')
def n2_music_alone_fails():
    reset()
    rainy_variant()
    window = open_window('rooms')
    env.start_error = '[Errno 111] Connection refused'     # music only: ambience and scene are fine
    enter_from_rooms(window, 'Rainy quiet')
    went_back = []
    deadline = time.monotonic() + 11
    while time.monotonic() < deadline:
        env.pump(.25)
        if window.current_page() != 'now':
            went_back.append(round(11 - (deadline - time.monotonic()), 1))
    env.shot(window, 'o10c-n2-music-alone-failed')
    check('N2i a room whose music alone fails is entered: its ambience plays',
          (snap().get('active') or {}).get('name') == 'Rainy quiet' and 'atmosphere' in audible(),
          ((snap().get('active') or {}).get('name'), status(), (snap().get('errors') or {})))
    check('N2j ... the window stays on Now and the failure shows on the music layer',
          not went_back and music_failure_shown(window), (went_back[:4], sorted(mapped_texts(window))[:30]))
    window.close()
    reset()
    # The same while another room plays.
    # [Revised in phase 2, coordinator decision] The first version (N2k, kept below as superseded) entered the room
    # with its ambience here too. Decision: a failed start must leave the playing room intact (o6 7n/7o), which wins
    # over entering a half-working room, so a music-only failure while another room plays shows the target card.
    rainy_variant()
    playing = previous_room()
    before = {k: {f: v.get(f) for f in ('status', 'source_id', 'name')} for k, v in (snap().get('layers') or {}).items()}
    window = open_window('rooms')
    env.start_error = '[Errno 111] Connection refused'
    mark = len(env.log)
    enter_from_rooms(window, 'Rainy quiet')
    env.pump(.5)
    check('N2k0 (setup) the playing room is not Rainy quiet and the window sent an enter for it',
          playing and playing != 'Rainy quiet' and any(c[0] == 'enter' for c in env.log[mark:]),
          (playing, env.commands(mark)))
    wait(lambda: failure_card(window, 'Rainy quiet') or ((snap().get('active') or {}).get('name') == 'Rainy quiet'
                                                         and 'atmosphere' in audible()), 11)
    env.pump(2.0)    # a card must stay, not flash
    env.shot(window, 'o10c-n2-playing-music-alone-failed')
    check('N2k [ORIGINAL, superseded by N2m-o] while another room plays, a room whose music alone fails is entered with '
          'its ambience playing, on Now, with the music failure shown',
          (snap().get('active') or {}).get('name') == 'Rainy quiet' and 'atmosphere' in audible()
          and window.current_page() == 'now' and music_failure_shown(window),
          ((snap().get('active') or {}).get('name'), status(), window.current_page(),
           ((snap().get('layers') or {}).get('music') or {}).get('source_id'), snap().get('errors')))
    page = now_page(window)
    check('N2m while another room plays, a room whose music alone fails shows the failure card named after it, '
          'with Try again and Back, and it stays',
          failure_card(window, 'Rainy quiet'), (window.current_page(), sorted(mapped_texts(page or window))[:30]))
    after = {k: {f: v.get(f) for f in ('status', 'source_id', 'name')} for k, v in (snap().get('layers') or {}).items()}
    check('N2n ... and the playing room is intact: same room, same layers, still playing (the new ambience never started)',
          (snap().get('active') or {}).get('name') == playing and after == before and 'music' in audible(),
          ((snap().get('active') or {}).get('name'), after, before))
    back = mapped_buttons(page or window, r'^back$')
    if back:
        back[0].emit('clicked')
    env.pump(.8)
    check('N2o ... and Back keeps the playing room playing and the card goes away',
          back and not failure_card(window, 'Rainy quiet') and (snap().get('active') or {}).get('name') == playing
          and 'music' in audible(), (bool(back), window.current_page(), status()))


@section('N2')
def n2_layer_failure_in_room():
    """Regression guard: a music change failing inside a playing room shows on its layer; nothing else stops."""
    reset()
    enter_room('northern-quiet', ambience=True)
    window = open_window()
    window.show_now()
    env.pump(.6)
    env.start_error = '[Errno 111] Connection refused'
    try:
        env.service.execute('layer', {'layer': 'music', 'action': 'retry'})
    except Exception as error:
        print('music retry refused: %s' % error)
    env.settle(1.5)
    env.start_error = None
    check('N2l a music change that fails inside a playing room shows on the music layer; the ambience keeps playing '
          'and the window stays on Now without a room failure card',
          'atmosphere' in audible() and window.current_page() == 'now' and music_failure_shown(window)
          and not failure_card(window, TARGET), (status(), window.current_page()))


# =====================================================================================================
# Criterion 2 (N3, E room level): MPRIS and media keys act on the room like the window's Pause.

def window_press():
    """Press Now's room Pause/Play once in a fresh window."""
    window = open_window()
    window.show_now()
    env.pump(.5)
    press_room(window)
    window.close()
    env.pump(.3)


def fresh(paused_by_window=False):
    """Each scenario starts from its own room (music + rain + scene), so one failure never sets up the next."""
    reset()
    enter_room('northern-quiet', ambience=True)
    playing = status()
    if paused_by_window:
        window_press()
    return playing


def window_pause_reference():
    fresh(paused_by_window=True)
    return status()


@section('MPRIS')
def mpris_room_transport():
    reference = window_pause_reference()
    check('E0 (reference) the window\'s room Pause pauses music and ambience and keeps the scene',
          reference['music'] == 'Paused' and reference['atmosphere'] == 'Paused' and reference['picture'] != 'Stopped',
          reference)
    before = fresh()
    check('E1 MPRIS reports Playing for a playing room', mpris_values().get('PlaybackStatus') == 'Playing',
          mpris_values().get('PlaybackStatus'))
    mpris('Pause')
    after = status()
    check('E2 MPRIS Pause pauses the room exactly as the window\'s Pause does (music and ambience paused, scene kept)',
          after == reference, (before, after, reference))
    check('E3 ... and MPRIS then reports Paused', mpris_values().get('PlaybackStatus') == 'Paused',
          mpris_values().get('PlaybackStatus'))
    window_press()
    check('E4 the window\'s Play resumes a room MPRIS paused: everything that played plays again',
          status() == before, (status(), before))
    before = fresh(paused_by_window=True)
    mpris('Play')
    check('E5 MPRIS Play resumes a room the window paused, exactly as it was', status() == before, (status(), before))
    before = fresh()
    mpris('PlayPause')
    check('E6 MPRIS PlayPause on a playing room pauses it like the window\'s Pause', status() == reference,
          (status(), reference))
    mpris('PlayPause')
    check('E7 ... and PlayPause again restores exactly what played', status() == before, (status(), before))
    before = fresh(paused_by_window=True)
    mpris('PlayPause')
    check('E7b MPRIS PlayPause on a room the window paused restores exactly what played', status() == before,
          (status(), before))


@section('MPRIS')
def mpris_partial_rooms():
    fresh()
    layer('music', 'pause')            # the music paused on its own; the rain is still audible
    values = mpris_values()
    check('E8 with only the ambience playing (music paused on its own), MPRIS still reports the room as Playing',
          values.get('PlaybackStatus') == 'Playing', (values.get('PlaybackStatus'), status()))
    mpris('Pause')
    check('E9 ... and MPRIS Pause silences it: the ambience pauses too', not audible(), status())
    fresh()
    layer('music', 'stop')
    values = mpris_values()
    mpris('Pause')
    check('E10 with the music stopped and the ambience playing, MPRIS reports Playing and Pause pauses the ambience',
          values.get('PlaybackStatus') == 'Playing' and not audible(), (values.get('PlaybackStatus'), status()))
    fresh()
    mpris('Stop')
    check('E11 [ORIGINAL, superseded by E11b] MPRIS Stop stops the room: nothing is left audible', not audible(), status())
    # E11 sampled 0.9 s after Stop, which calls a faded Leave (the window's own Leave fades for 1.2 s) a failure.
    wait(lambda: not audible(), 5)
    check('E11b MPRIS Stop stops the room: within 5 s (a Leave fade) nothing is left audible', not audible(), status())


@section('MPRIS')
def mpris_metadata():
    fresh()
    music_name = ((snap().get('layers') or {}).get('music') or {}).get('name') or ''
    strings = metadata_strings(mpris_values())
    check('E12 MPRIS metadata names the room', any(TARGET in s for s in strings), strings)
    check('E13 ... and what plays (the music\'s station or track)', bool(music_name) and any(music_name in s for s in strings),
          (music_name, strings))
    layer('music', 'stop')
    ambience = ((snap().get('layers') or {}).get('atmosphere') or {}).get('name') or 'Rain on glass'
    strings = metadata_strings(mpris_values())
    check('E14 with only the ambience playing, MPRIS metadata names the room and the ambience',
          any(TARGET in s for s in strings) and any(ambience in s for s in strings), (ambience, strings))


@section('KEYS')
def media_keys():
    reference = window_pause_reference()
    before = fresh()
    code, notes = media_key()
    check('E15 the media key (listening-hub toggle) pauses the whole room exactly as the window\'s Pause does',
          code == 0 and status() == reference, (code, notes, status(), reference))
    window = open_window()
    window.show_now()
    env.pump(.5)
    tooltip = (room_button(window).get_tooltip_text() or '').lower()
    window.close()
    env.pump(.3)
    check('E16 ... and the window then offers Play/Resume for the room', 'resume' in tooltip or 'play' in tooltip,
          tooltip)
    before = fresh(paused_by_window=True)
    code, notes = media_key()
    check('E17 the media key on a room the window paused restores exactly what played',
          code == 0 and status() == before, (code, notes, status(), before))
    fresh()
    layer('music', 'pause')
    code, notes = media_key()
    check('E18 with only the ambience still playing, the media key silences the room', code == 0 and not audible(),
          (code, notes, status()))


# =====================================================================================================
# Criterion 3 (E, layer level): own pause/resume and Stop on each Mix row; exact restore.

NAVIGATION = re.compile(r'change|choose|open scene|save|close mix|close scene|scene', re.I)


def mix_candidates(window):
    mix = getattr(window, 'mix', None) or window
    found = []
    for widget in env.widgets(mix):
        if not isinstance(widget, env.Gtk.Button) or isinstance(widget, env.Gtk.ToggleButton):
            continue
        if not widget.get_mapped() or in_popover(widget) or not widget.get_sensitive():
            continue
        label, tooltip = button_name(widget)
        if NAVIGATION.search(label + ' ' + tooltip):
            continue
        found.append((widget, (label, tooltip, widget.get_icon_name() if hasattr(widget, 'get_icon_name') else None)))
    return found


def with_mix(prepare):
    """A fresh room (music + rain), prepared, the window on Now with the Mix open."""
    enter_room('northern-quiet', ambience=True)
    prepare()
    window = open_window()
    window.show_now()
    env.pump(.4)
    window.open_mix()
    env.pump(.8)
    return window


def discover(prepare, wanted):
    """Click every candidate on the Mix rows (one per fresh room) and return the descriptor of the first whose
    effect is `wanted(before, after)`, with every observed effect for the log."""
    window = with_mix(prepare)
    count = len(mix_candidates(window))
    window.close()
    seen = []
    for index in range(count):
        reset()
        window = with_mix(prepare)
        candidates = mix_candidates(window)
        if index >= len(candidates):
            window.close()
            continue
        widget, descriptor = candidates[index]
        before = status()
        widget.emit('clicked')
        env.settle(1.0)
        after = status()
        seen.append((descriptor, after))
        window.close()
        env.pump(.2)
        if wanted(before, after):
            return descriptor, seen
    return None, seen


def click_descriptor(descriptor):
    window = open_window()
    window.show_now()
    env.pump(.4)
    window.open_mix()
    env.pump(.8)
    widget = next((w for w, d in mix_candidates(window) if d == descriptor), None)
    if widget is not None:
        widget.emit('clicked')
        env.settle(1.0)
    window.close()
    env.pump(.2)
    return widget is not None


@section('MIX')
def mix_rows():
    reset()
    nothing = lambda: None
    found = {}
    rules = {
        'music pause': (nothing, lambda b, a: a['music'] == 'Paused' and a['atmosphere'] == 'Playing'),
        'music stop': (nothing, lambda b, a: a['music'] == 'Stopped' and a['atmosphere'] == 'Playing'),
        'ambience pause': (nothing, lambda b, a: a['atmosphere'] == 'Paused' and a['music'] == 'Playing'),
        'ambience stop': (nothing, lambda b, a: a['atmosphere'] == 'Stopped' and a['music'] == 'Playing'),
        'music resume': (lambda: layer('music', 'pause'), lambda b, a: a['music'] == 'Playing' and a['atmosphere'] == 'Playing'),
        'ambience resume': (lambda: layer('atmosphere', 'pause'), lambda b, a: a['atmosphere'] == 'Playing' and a['music'] == 'Playing'),
    }
    letters = {'music pause': 'L1', 'music resume': 'L2', 'music stop': 'L3',
               'ambience pause': 'L4', 'ambience resume': 'L5', 'ambience stop': 'L6'}
    for name in ('music pause', 'music resume', 'music stop', 'ambience pause', 'ambience resume', 'ambience stop'):
        prepare, wanted = rules[name]
        descriptor, seen = discover(prepare, wanted)
        found[name] = descriptor
        check('%s the Mix has a visible control (not in a menu) for %s that touches only that layer'
              % (letters[name], name), descriptor is not None, seen)
        reset()
    # Room Pause -> Resume restores exactly what played, with a layer paused on its own (by its row when there is
    # one, else by the controller's layer action that row would send).
    enter_room('northern-quiet', ambience=True)
    if found.get('music pause'):
        click_descriptor(found['music pause'])
    else:
        layer('music', 'pause')
    window = open_window()
    window.show_now()
    env.pump(.4)
    press_room(window)
    paused = status()
    press_room(window)
    restored = status()
    window.close()
    check('L7 with the music paused on its own, room Pause silences everything and room Resume restores exactly '
          'what played (ambience back, music still paused)',
          paused['music'] == 'Paused' and paused['atmosphere'] == 'Paused'
          and restored['music'] == 'Paused' and restored['atmosphere'] == 'Playing', (paused, restored))
    reset()
    enter_room('northern-quiet', ambience=True)
    before = status()
    window = open_window()
    window.show_now()
    env.pump(.4)
    press_room(window)
    press_room(window)
    window.close()
    check('L8 room Pause then Resume with everything playing restores everything', status() == before, (status(), before))


@section('MIX')
def silent_room_play():
    """Every layer paused on its own: Play is never a no-op, and gives the same result from each surface."""
    results = {}
    for surface in ('window', 'mpris', 'roomctl'):
        reset()
        enter_room('northern-quiet', ambience=True)
        layer('music', 'pause')
        layer('atmosphere', 'pause')
        error = ''
        if surface == 'window':
            window = open_window()
            window.show_now()
            env.pump(.4)
            press_room(window)
            env.settle(1.5)
            window.close()
        elif surface == 'mpris':
            mpris('Play')
            env.settle(1.0)
        else:
            result = roomctl('resume')
            error = result.stderr.strip()
        results[surface] = (frozenset(audible()), error)
    check('L9 Play on a room silent because every layer was paused on its own starts sound again',
          bool(results['window'][0]), results)
    check('L10 ... and MPRIS Play and listening-roomctl resume do the same as the window\'s Play',
          results['window'][0] and results['mpris'][0] == results['window'][0] == results['roomctl'][0]
          and not results['roomctl'][1], results)


# =====================================================================================================
# Criterion 4 (N9): the connecting state.

TRACK_CONTROLS = r'next track|previous track|like this|skip'


def connecting_samples(window, target, log=None):
    """What Now shows while the controller is still connecting to `target` (sampled for 2.6 s)."""
    samples = []
    deadline = time.monotonic() + 2.6
    while time.monotonic() < deadline:
        env.pump(.2)
        page = now_page(window)
        if log is not None:
            log.append((window.current_page(), bool(env.controller.transition)))
        if page is None or not env.controller.transition:
            continue
        controls = [button_name(w) for w in mapped_buttons(page, TRACK_CONTROLS)]
        scales = [round(w.get_value()) for w in env.widgets(page)
                  if isinstance(w, env.Gtk.Scale) and w.get_mapped()]
        samples.append((controls, scales))
    return samples


@section('N9')
def connecting_cold():
    reset()
    env.service.execute('room_volume', {'value': STORED_VOLUME})
    env.settle(.4)
    window = open_window('rooms')
    env.start_delay = 3.5
    enter_from_rooms(window, TARGET)
    samples = connecting_samples(window, TARGET)
    env.shot(window, 'o10c-n9-connecting-cold')
    env.start_delay = 0.
    check('N9a (setup) the cold-start Connecting state was observed on Now', bool(samples), samples)
    check('N9b connecting to a radio room from a cold start shows no track controls (Next track, Less/More like this)',
          samples and not any(controls for controls, _ in samples), samples[:3])
    check('N9c ... and never shows the volume at 0 while the stored room volume is %d (a shown level is %d)'
          % (STORED_VOLUME, STORED_VOLUME),
          samples and all(all(abs(v - STORED_VOLUME) <= 1 for v in scales) for _, scales in samples), samples[:3])


@section('N9')
def connecting_from_library():
    reset()
    start_library_room()
    wait(lambda: env.controller.active and status()['music'] == 'Playing' and not env.controller.transition, 8)
    env.settle(.8)
    window = open_window()
    window.show_now()
    env.pump(1.0)
    page = now_page(window)
    had = bool(page is not None and mapped_buttons(page, r'next track'))
    check('N9d (setup) the library room shows Next track before switching', had,
          sorted(mapped_texts(window))[:20])
    env.start_delay = 3.5
    mark = len(env.log)
    enter_from_rooms(window, 'Night drive')   # a radio room other than the one the library room was built on
    seen = []
    samples = connecting_samples(window, 'Night drive', seen)
    env.shot(window, 'o10c-n9-connecting-from-library')
    env.start_delay = 0.
    check('N9f (setup) the Connecting state from the library room was observed on Now', bool(samples),
          (seen[:6], env.commands(mark), sorted(mapped_texts(window))[:30]))
    check('N9e connecting from a library room to a radio room hides the previous room\'s track controls',
          samples and not any(controls for controls, _ in samples), samples[:3])


# =====================================================================================================
# Criterion 5 (N10): no menu holds a single item.

def menu_items(button):
    model = button.get_menu_model() if hasattr(button, 'get_menu_model') else None
    if model is not None:
        def count(menu):
            total = 0
            for index in range(menu.get_n_items()):
                nested = [menu.get_item_link(index, link) for link in ('section', 'submenu')]
                nested = [n for n in nested if n is not None]
                total += sum(count(n) for n in nested) if nested else 1
            return total
        return count(model), []
    popover = button.get_popover()
    if popover is None or popover.get_child() is None:
        return 0, []
    child = popover.get_child()

    def shown(widget):
        while widget is not None and widget is not popover:
            if not widget.get_visible():
                return False
            widget = widget.get_parent()
        return True
    Gtk = env.Gtk
    kinds = (Gtk.Button, Gtk.CheckButton, Gtk.Switch, Gtk.Scale, Gtk.DropDown, Gtk.Entry, Gtk.ListBoxRow)
    items = [w for w in env.widgets(child) if isinstance(w, kinds) and shown(w)
             and not (isinstance(w, Gtk.ListBoxRow) and not w.get_activatable())
             and not any(isinstance(p, kinds) for p in ancestors(w, child))]
    return len(items), [button_name(w)[0] or button_name(w)[1] for w in items]


def ancestors(widget, stop):
    parent = widget.get_parent()
    while parent is not None and parent is not stop:
        yield parent
        parent = parent.get_parent()


def single_menus(window):
    found = []
    for widget in env.widgets(window):
        if isinstance(widget, env.Gtk.MenuButton) and widget.get_mapped():
            count, names = menu_items(widget)
            if count == 1:
                found.append((button_name(widget), names))
    return found


@section('N10')
def no_single_item_menus():
    states = []

    def scan(label):
        window = open_window()
        window.show_now()
        env.pump(.5)
        window.open_mix()
        env.pump(.9)
        states.append((label, single_menus(window)))
        env.shot(window, 'o10c-n10-' + label)
        window.close()
        env.pump(.2)
    reset()
    enter_room('northern-quiet', ambience=True)
    scan('radio-and-rain')
    reset()
    enter_room('northern-quiet', ambience=False)
    scan('radio-only')
    reset()
    enter_room('northern-quiet', ambience=True)
    env.start_error = '[Errno 111] Connection refused'
    try:
        env.service.execute('layer', {'layer': 'music', 'action': 'retry'})
    except Exception:
        pass
    env.settle(1.2)
    env.start_error = None
    scan('music-failed')
    reset()
    start_library_room()
    wait(lambda: env.controller.active and status()['music'] == 'Playing' and not env.controller.transition, 8)
    env.settle(.6)
    scan('library')
    for label, found in states:
        check('N10 (%s) no menu on Now or in the Mix holds a single item' % label, not found, found)


# =====================================================================================================
# Criterion 6: additive registry, backward-compatible state, roomctl layer actions, docs.

ACTIONS_240 = {   # the 2.4.0 registry (main e45d947): name -> argument kinds
    'atmosphere': {'source_id': 'text', 'id': 'text'}, 'delete': {}, 'enter': {
        'room_id': 'text', 'variant_id': 'text', 'recipe': 'object', 'music': 'object', 'atmosphere': 'object',
        'picture': 'object'}, 'get': {}, 'inspect': {}, 'layer': {'layer': 'text', 'action': 'text', 'minutes': 'number'},
    'leave': {'fade_seconds': 'number'}, 'link': {'destination': 'text'}, 'metadata.analyze': {}, 'metadata.cancel': {},
    'metadata.configure_acoustid': {}, 'metadata.confirm_identity': {}, 'metadata.enrich': {}, 'metadata.export': {},
    'metadata.import': {}, 'metadata.inspect': {}, 'metadata.jobs': {}, 'metadata.list': {},
    'metadata.lookup_reference': {}, 'metadata.match': {}, 'metadata.review': {}, 'metadata.revoke': {},
    'metadata.status': {}, 'music': {'item': 'object', 'source_id': 'text'}, 'online': {},
    'picture': {'action': 'text', 'minutes': 'number'}, 'quiet': {'minutes': 'number'}, 'rename': {}, 'resume': {},
    'return': {}, 'room_volume': {}, 'save': {'music': 'object', 'atmosphere': 'object', 'picture': 'object'},
    'settings': {}, 'station.delete': {}, 'station.feedback': {}, 'station.hold': {}, 'station.list': {},
    'station.play_next': {}, 'station.recent': {}, 'station.replay': {}, 'station.reset_memory': {},
    'station.save': {}, 'station.start': {}, 'station.update': {'preferences': 'object'}, 'stop': {},
    'subscribe': {'fields': 'list'}, 'variants': {}, 'wind_down': {'minutes': 'number'}}
QUEUED_240 = {'atmosphere', 'link', 'save'}
SNAPSHOT_KEYS_240 = {'active', 'audio_policy', 'config_generation', 'errors', 'last_entered_at', 'last_session',
                     'layers', 'levels', 'media', 'now', 'recent', 'resume_offer', 'room_volume', 'settings', 'station',
                     'station_learned', 'station_recent', 'stations', 'timers', 'transition', 'variants'}
STORED_240 = {   # session.json as 2.4.0 writes it
    'version': 1,
    'last_session': {'id': 'northern-quiet', 'room_id': 'northern-quiet', 'name': 'Northern quiet', 'poster_path': None,
                     'accent': '#9cb8b8',
                     'music': {'source_id': 'somafm-drone-zone', 'volume': 50.0, 'muted': False, 'name': 'SomaFM Drone Zone',
                               'kind': 'radio'},
                     'atmosphere': {'kind': 'local', 'source_id': 'rain-glass', 'volume': 20.0, 'muted': False,
                                    'window_open': 35.0},
                     'picture': {'preset_id': 'room-northern-quiet', 'enabled': True, 'still': False, 'placement': 'here',
                                 'display': None},
                     'room_volume': 70.0},
    'settings': {'placement': 'here', 'still': False, 'reduced_motion': False, 'pause_on_disconnect': True,
                 'duck_on_call': False, 'keep_controls': False},
    'recent': ['northern-quiet'], 'station_history': [], 'station_memory': {}, 'room_volume': 70.0,
    'last_entered_at': 1795255249.0, 'placement': {'chosen': False, 'migrated': True}}
ROOMCTL_FORMS = (   # the ways `listening-roomctl` could reasonably spell a layer action
    lambda layer, action: ['layer', layer, action],
    lambda layer, action: ['layer', json.dumps({'layer': layer, 'action': action})],
    lambda layer, action: [layer, action],
    lambda layer, action: ['%s-%s' % (layer, action)],
)


@section('C6')
def registry_and_state():
    import room_session
    missing = sorted(set(ACTIONS_240) - set(room_session.ACTIONS))
    changed = sorted(name for name, kinds in ACTIONS_240.items() if name in room_session.ACTIONS
                     and any(room_session.ACTIONS[name].args.get(k) != v for k, v in kinds.items()))
    check('C6a every 2.4.0 controller action still exists with the same argument types (changes are additive)',
          not missing and not changed, (missing, changed))
    flipped = sorted(n for n in ACTIONS_240 if n in room_session.ACTIONS and n not in ('layer', 'picture')
                     and room_session.ACTIONS[n].runs_immediately({}) == (n in QUEUED_240))
    flipped += ['%s %s' % (n, a) for n in ('layer', 'picture') for a in ('pause', 'stop')
                if n in room_session.ACTIONS and not room_session.ACTIONS[n].runs_immediately({'action': a})]
    check('C6b ... and what ran at once still runs at once (quiet, resume, stop, layer pause/stop)', not flipped, flipped)
    reset()
    enter_room('northern-quiet', ambience=True)
    keys = set(snap())
    check('C6c the snapshot keeps every 2.4.0 field', SNAPSHOT_KEYS_240 <= keys, sorted(SNAPSHOT_KEYS_240 - keys))
    # A 2.4.0 client's raw layer request over the socket still works (and old quiet/resume keep their meaning).
    raw_socket('layer', {'layer': 'atmosphere', 'action': 'pause'})
    env.settle(.6)
    first = status()
    raw_socket('layer', {'layer': 'atmosphere', 'action': 'play'})
    wait(lambda: status()['atmosphere'] == 'Playing', 3)
    raw_socket('quiet', {})
    env.settle(.6)
    quiet = status()
    offer = snap().get('resume_offer') or {}
    raw_socket('resume', {})
    env.settle(.8)
    check('C6d a 2.4.0 client over the socket: layer pause/play act on that layer; quiet pauses the room with a '
          'resume offer and resume restores it',
          first['atmosphere'] == 'Paused' and first['music'] == 'Playing' and quiet['music'] == quiet['atmosphere'] == 'Paused'
          and sorted(offer.get('layers') or []) and status()['music'] == status()['atmosphere'] == 'Playing',
          (first, quiet, offer, status()))
    reset()
    # 2.4.0 stored state loads and its room can be returned to.
    import tempfile
    from test_listening_session import FakeBackend
    with tempfile.TemporaryDirectory(prefix='o10c-state-') as directory:
        (Path(directory) / 'session.json').write_text(json.dumps(STORED_240))
        backend = FakeBackend()
        controller = room_session.Controller(backend, directory)
        controller._home_action = lambda *_: None
        controller._route_preferred = lambda: None
        try:
            shot = controller.snapshot()
            loaded = ((shot.get('last_session') or {}).get('name') == 'Northern quiet' and shot.get('room_volume') == 70.0
                      and shot.get('recent') == ['northern-quiet'])
            controller.dispatch('return', {})
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not (controller.active and not controller.transition):
                time.sleep(.1)
            controller.refresh()
            layers = controller.snapshot().get('layers') or {}
            entered = (controller.active or {}).get('name') == 'Northern quiet' and \
                (layers.get('atmosphere') or {}).get('status') == 'Playing'
        finally:
            controller.close()
    check('C6e session state stored by 2.4.0 loads unchanged and its room can be returned to', loaded and entered,
          (loaded, entered))


@section('C6')
def roomctl_layers():
    reset()
    helptext = roomctl('--help').stdout
    working = {}
    attempts = []
    for word in ('music', 'ambience', 'atmosphere'):
        layer_key = 'music' if word == 'music' else 'atmosphere'
        other = 'atmosphere' if layer_key == 'music' else 'music'
        for index, form in enumerate(ROOMCTL_FORMS):
            enter_room('northern-quiet', ambience=True)
            result = roomctl(*form(word, 'pause'))
            now = status()
            attempts.append((form(word, 'pause'), result.returncode, now, result.stderr.strip()[-160:]))
            if result.returncode == 0 and now[layer_key] == 'Paused' and now[other] == 'Playing':
                working[layer_key] = (word, index)
                break
        if layer_key in working:
            continue
    music, ambience = working.get('music'), working.get('atmosphere')
    check('C6f listening-roomctl pauses the music layer alone', music is not None, attempts)
    check('C6g listening-roomctl pauses the ambience layer alone', ambience is not None, attempts)
    results = {}
    for layer_key, found in (('music', music), ('atmosphere', ambience)):
        if not found:
            continue
        word, index = found
        other = 'atmosphere' if layer_key == 'music' else 'music'
        enter_room('northern-quiet', ambience=True)
        roomctl(*ROOMCTL_FORMS[index](word, 'pause'))
        resumed = next((verb for verb in ('play', 'resume', 'toggle')
                        if roomctl(*ROOMCTL_FORMS[index](word, verb)).returncode == 0
                        and status()[layer_key] == 'Playing'), None)
        result = roomctl(*ROOMCTL_FORMS[index](word, 'stop'))
        stopped = result.returncode == 0 and status()[layer_key] == 'Stopped' and status()[other] == 'Playing'
        results[layer_key] = (word, index, resumed, stopped)
    check('C6h ... and resumes and stops each layer alone (the other layer keeps playing)',
          len(results) == 2 and all(r[2] and r[3] for r in results.values()), results)
    words = {ROOMCTL_FORMS[f[1]](f[0], 'pause')[0] for f in (music, ambience) if f}
    check('C6i listening-roomctl --help lists the layer action', words and all(w.split('-')[0] in helptext for w in words),
          (words, helptext[-400:]))
    runbook = (REPO / 'docs' / 'runbooks' / 'listening-room-session.md').read_text(errors='replace')
    documented = words and all(('listening-roomctl ' + w) in runbook for w in words)
    check('C6j [ORIGINAL, superseded by C6j2] the session runbook documents the roomctl layer command', documented, words)
    # C6j required the literal `listening-roomctl WORD`; a usage line such as `listening-roomctl music|ambience pause|...`
    # documents the same command.
    documented = words and all(re.search(r'listening-roomctl\s+(?:[\w-]+\|)*' + re.escape(w) + r'(?:\||\s)', runbook)
                               for w in words)
    check('C6j2 the session runbook documents the roomctl layer command (literally or as a usage line)', documented, words)
    changelog = (REPO / 'CHANGELOG.md').read_text(errors='replace')
    head = changelog.split('## [2.4.0]', 1)[0]
    check('C6k the CHANGELOG records the change above 2.4.0: MPRIS/media keys act on the room, and roomctl layer actions',
          re.search(r'MPRIS|media key', head) is not None and 'roomctl' in head and re.search(r'layer', head, re.I) is not None,
          head[-600:])


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
