"""Independent acceptance checks for Listening Room outcome O6 (faster selection, ambience in the Mix,
new stations, Twitch and YouTube, pending state and Mix truthfulness).

Owned by the acceptance author, not the implementer. Behavioural: the real window on private Xvfb/D-Bus, the real
session controller on the session test double, fixture providers, and a *fake yt-dlp executable* on PATH so the real
subprocess, cancel and "missing" paths run. Oracles are the controller command log, the backend effect log, the
controller's stored state and what the window shows. No source-text greps.

  env -u WAYLAND_DISPLAY GDK_BACKEND=x11 GDK_DEBUG=no-portals xvfb-run -a -s '-screen 0 1440x1000x24' \
    dbus-run-session python3 tests/probes/listening-o6-acceptance.py --repo WORKTREE --output OUTDIR [--only s1_timing,...]
"""
import argparse
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument('--repo', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--only', default='', help='comma-separated scenario function names')
args = parser.parse_args()
sys.stdout.reconfigure(line_buffering=True)
repo = Path(args.repo).resolve()
source = repo / 'src'
sys.path[:0] = [str(source), str(repo / 'tests')]
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)
RESULTS = []
NUMBERS = {}


def check(name, ok, detail=''):
    RESULTS.append({'criterion': name, 'pass': bool(ok), 'detail': str(detail)[:600]})
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ': ' + str(detail)[:600]), flush=True)


folder = tempfile.mkdtemp(prefix='o6-acceptance-')
os.environ['HOME'] = folder
for key, suffix in [('XDG_STATE_HOME', 'state'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache')]:
    path = Path(folder) / suffix
    path.mkdir(mode=0o700)
    os.environ[key] = str(path)
os.environ['GTK_A11Y'] = 'none'
os.environ.pop('ACOUSTID_CLIENT_KEY', None)

# ---- a fake yt-dlp on PATH ------------------------------------------------------------------------------
BIN = Path(folder) / 'bin'
BIN.mkdir()
EMPTY = Path(folder) / 'empty-bin'
EMPTY.mkdir()
YT_LOG = Path(folder) / 'yt-calls.jsonl'
(BIN / 'yt-dlp').write_text('''#!%s
import hashlib, json, os, sys, time
argv = sys.argv[1:]
open(os.environ['FAKE_YT_LOG'], 'a').write(json.dumps({'pid': os.getpid(), 'argv': argv}) + '\\n')
spec = argv[-1]
query = spec.split(':', 1)[1] if ':' in spec else spec
if 'broken' in query:
    sys.exit(1)
if 'slow' in query:
    for _ in range(300):
        time.sleep(.02)
for index in range(3):
    identity = hashlib.md5((query + str(index)).encode()).hexdigest()[:11]
    print(json.dumps({'id': identity, 'title': '%%s result %%d' %% (query, index), 'channel': 'Fixture Channel',
                      'duration': 185 + index, 'live_status': 'not_live'}), flush=True)
''' % sys.executable)
(BIN / 'yt-dlp').chmod((BIN / 'yt-dlp').stat().st_mode | stat.S_IXUSR)
os.environ['FAKE_YT_LOG'] = str(YT_LOG)


def yt_present(present=True):
    os.environ['PATH'] = str(BIN) if present else str(EMPTY)


def yt_calls():
    return [json.loads(line) for line in YT_LOG.read_text().splitlines()] if YT_LOG.exists() else []


yt_present(True)

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Graphene, Gtk

import ambience
import atmosphere_catalogue
import audio_devices
import listening_hub as hub
import metadata_pipeline
import next_window
import online_services
import room_session
import session_client
import session_service
import twitch_channels
import twitch_credentials
from test_listening_session import FakeBackend

MAIN = threading.get_ident()


class Backend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.start_delay = 0.
        self.start_error = None
        self.atmosphere_error = None

    def start_music(self, source, cancel):
        deadline = time.monotonic() + self.start_delay
        while time.monotonic() < deadline and not cancel.is_set():
            time.sleep(.02)
        if self.start_error:
            raise ValueError(self.start_error)
        return super().start_music(source, cancel)

    def start_atmosphere(self, selection, cancel):
        if self.atmosphere_error and selection.get('kind') != 'none':
            raise ValueError(self.atmosphere_error)
        super().start_atmosphere(selection, cancel)
        sound = next((x for x in atmosphere_catalogue.SOUNDS if x['id'] == selection.get('source_id')), {})
        if sound:
            self.state['atmosphere'].update(name=sound['name'], outdoor=sound['outdoor'])
        elif selection.get('name'):
            self.state['atmosphere'].update(name=selection['name'])

    def start_picture(self, picture, cancel=None):
        super().start_picture(picture, cancel)
        preset = next((p for p in ambience.presets() if p['id'] == picture.get('preset_id')), {})
        self.state['picture'].update(name=preset.get('name', 'Scene'), placement=picture.get('placement'))


backend = Backend()
controller = room_session.Controller(backend, Path(folder) / 'controller')
controller._home_action = lambda *_: None
controller._route_preferred = lambda: None
service = session_service.Service(controller)
COMMANDS = []          # (action, args, on_gtk_thread)
SYSTEM_VOLUME = {'percent': 40}
OUTPUTS = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True}]
READ_ONLY = {'inspect', 'get', 'variants', 'station.list', 'station.recent', 'outputs'}


FAIL_REQUESTS = {'actions': (), 'error': None}


def request(action, arguments=None, *_a, **_k):
    COMMANDS.append((action, copy.deepcopy(arguments or {}), threading.get_ident() == MAIN))
    if action in FAIL_REQUESTS['actions']:
        raise FAIL_REQUESTS['error']
    return service.execute(action, arguments or {})


TRACKS = [{'provider': 'jellyfin', 'id': 'trk-1', 'type': 'Audio', 'name': 'Nocturne in C', 'artist': 'Someone', 'album': 'Night'}]
ALBUMS = [{'provider': 'jellyfin', 'id': 'alb-1', 'type': 'MusicAlbum', 'name': 'Night collection', 'artist': 'Various',
           'genres': ['Ambient']}]
PLAYLISTS = [{'provider': 'jellyfin', 'id': 'pl-1', 'type': 'Playlist', 'name': 'Evening wind-down', 'artist': '', 'genres': []}]
WORLD = [{'id': 'w-1', 'name': 'Ambient Drift', 'group': 'Search results', 'kind': 'radio', 'url': 'https://radio.example.test/drift',
          'country': 'Finland', 'language': 'Finnish', 'tags': ['ambient'], 'website': ''}]
LIVE = [{'login': 'lofigirl', 'name': 'lofigirl', 'url': 'https://www.twitch.tv/lofigirl', 'live': True, 'title': 'beats'}]
CONFIG = {name: {'endpoint': 'https://jellyfin.example.test', 'username': 'probe', 'user_id': 'probe-user', 'token_file': ''}
          for name in ('jellyfin', 'music_assistant', 'audiomuse', 'home_assistant')}
KNOBS = {'twitch_connected': False, 'twitch_curated': [], 'twitch_error': None}


def credentials():
    if KNOBS['twitch_connected']:
        return twitch_credentials.Credentials(client_id='fixture-client', access_token='fixture-token')
    return twitch_credentials.Credentials()


def discover(style, curated=None, client_id=None, access_token=None):
    if KNOBS['twitch_error']:
        raise OSError(KNOBS['twitch_error'])
    if not KNOBS['twitch_connected']:
        return {'status': 'unknown', 'message': 'Connect Twitch through the CLI to discover live channels.',
                'reason': 'configuration', 'channels': []}
    return {'status': 'live', 'channels': copy.deepcopy(LIVE) if style == 'music' else [], 'curated_status': {}, 'message': ''}


def browse(query='', kind='albums', parent_id=None, cancel_event=None):
    text = str(query or '').casefold()
    pool = {'albums': ALBUMS, 'playlists': PLAYLISTS, 'tracks': TRACKS, 'recent': ALBUMS}.get(kind, ALBUMS)
    return [copy.deepcopy(x) for x in pool if not text or text in (x['name'] + ' ' + x.get('artist', '')).casefold()]


patches = [
    patch.object(session_client, 'request', side_effect=request),
    patch.object(audio_devices, 'outputs', return_value=OUTPUTS),
    patch.object(audio_devices, 'test_sound', side_effect=lambda name=None: None),
    patch.object(hub, 'system_volume', side_effect=lambda: dict(SYSTEM_VOLUME)),
    patch.object(hub, 'set_volume', side_effect=lambda value, **kw: SYSTEM_VOLUME.update(percent=value)),
    patch.object(hub, 'search_radio', side_effect=lambda q, **kw: [copy.deepcopy(x) for x in WORLD
                                                                  if not q or str(q).casefold() in x['name'].casefold() + ' ambient']),
    patch.object(ambience, 'set_lighting', side_effect=lambda level: level),
    patch.object(ambience, 'monitors', return_value=[]),
    patch.object(metadata_pipeline, 'library_origin', return_value='fixture-library'),
    patch.object(online_services, 'config', side_effect=lambda: copy.deepcopy(CONFIG)),
    patch.object(online_services, 'browse_jellyfin', side_effect=browse),
    patch.object(online_services, 'jellyfin_queue', side_effect=lambda item, cancel_event=None: copy.deepcopy(TRACKS)),
    patch.object(online_services, 'prepare_playback',
                 side_effect=lambda reference, cancel_event=None: dict(reference, url='file:///fixture/a.flac')),
    patch.object(online_services, 'jellyfin_link',
                 side_effect=lambda item: 'https://jellyfin.example.test/web/#/details?id=' + str(item.get('id'))),
    patch.object(twitch_credentials, 'load', side_effect=credentials),
    patch.object(twitch_channels, 'curated_channels', side_effect=lambda style: list(KNOBS['twitch_curated']) if style == 'music' else []),
    patch.object(twitch_channels, 'excluded_channels', return_value=[]),
    patch.object(twitch_channels, 'discover', side_effect=discover),
]
for item in patches:
    item.start()

context = GLib.MainContext.default()
APP = None
STATION = {'name': 'Morning lift',
           'sources': [{'source_id': 'somafm-groove-salad', 'name': 'SomaFM Groove Salad', 'url': 'https://somafm.com/groovesalad.pls', 'kind': 'radio'}],
           'fallbacks': [], 'preferences': {}, 'end_policy': 'stop', 'arc': 'none', 'generation': {'kind': 'collection'}}


# ---- harness ----------------------------------------------------------------------------------------------
def pump(seconds=.4):
    deadline = time.monotonic() + seconds
    last = 0
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if time.monotonic() - last > .25:
            last = time.monotonic()
            controller.worker.submit(controller.refresh)   # off the GTK thread, as in the real service
        time.sleep(.005)


def settle(seconds=.8):
    try:
        controller.worker.submit(lambda: None).result(timeout=6)
    except Exception:
        pass
    pump(seconds)


def walk(root):
    yield root
    child = root.get_first_child()
    while child:
        yield from walk(child)
        child = child.get_next_sibling()


def label_texts(root):
    return {w.get_text() for w in walk(root) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text()}


def all_texts(root):
    found = set()
    for w in walk(root):
        for getter in ('get_label', 'get_text', 'get_title', 'get_subtitle', 'get_tooltip_text'):
            value = getattr(w, getter, lambda: None)()
            if isinstance(value, str) and value:
                found.add(value)
    return found


def button_text(button):
    value = button.get_label()
    if value is None:
        value = ' '.join(w.get_text() for w in walk(button) if isinstance(w, Gtk.Label) and w.get_text())
    return value or button.get_tooltip_text() or ''


def buttons(root, text, mapped=True):
    return [w for w in walk(root) if isinstance(w, Gtk.Button) and (w.get_mapped() or not mapped) and button_text(w) == text]


def click(button):
    button.emit('clicked')
    pump(.3)


def sent(mark):
    return [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY and not c[0].startswith('metadata.')]


def mutating_actions(mark):
    return [(c[0], c[1]) for c in sent(mark)]


def build_app():
    global APP
    if APP is None:
        APP = next_window.NextApp()
        APP.register(None)
        APP.hold()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        next_window.POLL_MS = 250
    return APP


def close_windows():
    for win in list(build_app().get_windows()):
        win.close()
    pump(.3)


def launch(page='listen', width=1200, height=800):
    close_windows()
    window = next_window.NextWindow(build_app())
    window.set_default_size(width, height)
    window.present()
    pump(1.2)
    got = (window.get_width(), window.get_height())
    if got != (width, height):
        window.set_default_size(width + (width - got[0]), height + (height - got[1]))
        pump(.8)
    {'listen': window.show_listen, 'rooms': window.show_rooms, 'now': window.show_now}[page]()
    pump(.9)
    return window


def leave():
    if controller.active is not None:
        controller.leave(0)
        settle(1.0)
    backend.start_delay, backend.start_error, backend.atmosphere_error = 0., None, None
    backend.fail_music = backend.fail_picture = False


def start_room(name='Morning lift'):
    leave()
    if not any(s['name'] == name for s in controller.stations()):
        service.execute('station.save', {'station': dict(STATION, name=name)})
    station = next(s for s in controller.stations() if s['name'] == name)
    service.execute('station.start', {'id': station['id']})
    settle(1.2)
    assert controller.active, 'the station did not start'
    return station


def snap():
    return service.execute('inspect', {})


def layer_view(snapshot=None):
    active = (snapshot or snap()).get('active') or {}
    return {k: json.dumps(active.get(k), sort_keys=True, default=str) for k in ('music', 'atmosphere', 'picture')}


def effects_since(mark):
    return [c for c in backend.calls[mark:] if c[0] in ('start_music', 'start_atmosphere', 'start_picture', 'start_verified_music')]


def toast_button(window, text):
    return next((w for w in walk(window.toasts) if isinstance(w, Gtk.Button) and w.get_mapped() and button_text(w) == text), None)


def toast_titles(window):
    return label_texts(window.toasts)


def scope_to(page, key):
    keys = [k for k, _t in page.scope_list]
    page.scopes.set_active(keys.index(key))
    pump(.5)


def type_query(page, text, wait=1.5):
    page.search.set_text(text)
    page.query_submitted()
    pump(wait)


def dialogs(window):
    return window.get_visible_dialog()


def toggles(dialog):
    return [w for w in walk(dialog) if isinstance(w, Adw.ToggleGroup)]


def toggle_names(group):
    return [group.get_toggle(i).get_name() for i in range(group.get_n_toggles())]


def card_gesture(card):
    return next(c for w in walk(card) for c in w.observe_controllers() if isinstance(c, Gtk.GestureClick) and c.get_button() == 1)


def click_card(card, presses=(1,)):
    gesture = card_gesture(card)
    for n in presses:
        gesture.emit('pressed', n, 12., 12.)
        gesture.emit('released', n, 12., 12.)


def second_press(window, card, n=2):
    """The second press of a double-click lands wherever the first click's preview now is: the stack."""
    ok, spot = card.compute_point(window.rooms.stack, Graphene.Point().init(12., 12.))
    again = next(c for c in window.rooms.stack.observe_controllers()
                 if isinstance(c, Gtk.GestureClick) and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE)
    again.emit('pressed', n, spot.x if ok else 12., spot.y if ok else 12.)


def preview_visible(window):
    stack = window.rooms.stack
    name = stack.get_visible_child_name() or ''
    child = stack.get_visible_child()
    return name.startswith('preview') and child is not None and child.get_mapped()


def room_cards(window):
    cards, child = [], window.cards.get_first_child()
    while child is not None:
        cards.append(child)
        child = child.get_next_sibling()
    return cards


def scenario(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()
        check(fn.__name__ + ' (scenario crashed)', False, traceback.format_exc()[-700:])
    try:
        close_windows()
    except Exception:
        pass
    try:
        leave()
    except Exception:
        pass


# ---- main-thread I/O tracer ---------------------------------------------------------------------------------
# A bare os.stat() is a metadata lookup, not blocking work; open/read/decode/network/subprocess/sleep are.
BLOCKING = {'open', 'listdir', 'scandir', 'connect', 'recv', 'recv_into', 'sendall', 'send', 'read', 'readline',
            'sleep', 'waitpid', 'getaddrinfo', 'create_connection', 'fork_exec', 'urlopen', 'new_from_file', 'new_from_filename',
            'load_contents', 'file_get_contents'}
SRC = str(source)


class Tracer:
    """Records blocking calls made on the GTK thread by application code (not by importlib or GTK itself)."""

    def __init__(self):
        self.hits = []

    def app_frame(self, frame):
        depth, found = 0, False
        while frame is not None and depth < 25:
            name = frame.f_code.co_filename
            if 'importlib' in name:
                return False
            if name.startswith(SRC):
                found = True
            frame, depth = frame.f_back, depth + 1
        return found

    def __call__(self, frame, event, arg):
        if event == 'c_call':
            name = getattr(arg, '__name__', '')
            if name == 'connect' and type(getattr(arg, '__self__', None)).__name__ != 'socket':
                return    # a GObject signal connection, not a network connect
            if name in BLOCKING and self.app_frame(frame):
                self.hits.append((name, frame.f_code.co_filename.replace(SRC + '/', ''), frame.f_lineno))
        elif event == 'call' and frame.f_code.co_name in ('urlopen', 'create_connection') and self.app_frame(frame.f_back):
            self.hits.append((frame.f_code.co_name, 'stdlib', 0))

    def __enter__(self):
        self.hits.clear()
        sys.setprofile(self)
        return self

    def __exit__(self, *_):
        sys.setprofile(None)


tracer = Tracer()


class StallMeter:
    """A 4 ms main-loop heartbeat: the largest gap between two beats is the longest the GTK thread was held."""

    def __init__(self):
        self.last = time.monotonic()
        self.worst = 0.
        self.source = None

    def beat(self):
        now = time.monotonic()
        self.worst = max(self.worst, now - self.last)
        self.last = now
        return True

    def __enter__(self):
        self.last, self.worst = time.monotonic(), 0.
        self.source = GLib.timeout_add(4, self.beat)
        return self

    def __exit__(self, *_):
        GLib.source_remove(self.source)


# =======================================================================================================
def s1_timing():
    """Criterion 1: single click -> preview at once; a single click never enters; double-click/Enter enters once."""
    leave()
    window = launch('rooms')
    cards = room_cards(window)
    check('1a the Rooms grid has room cards to click', len(cards) >= 4, len(cards))
    mark = len(COMMANDS)
    times, stalls = [], []
    with tracer:
        for card in cards[:6]:
            window.rooms.close_preview(animate=False)
            pump(.45)
            with StallMeter() as stall:
                start = time.monotonic()
                click_card(card)
                while not preview_visible(window) and time.monotonic() - start < 1.5:
                    context.iteration(False)
                times.append(time.monotonic() - start)
                pump(.3)
            stalls.append(stall.worst)
    NUMBERS['single_click_to_preview_ms'] = [round(t * 1000, 1) for t in times]
    check('1b single click -> preview visible within 150 ms for every card (measured %s ms)' % NUMBERS['single_click_to_preview_ms'],
          times and max(times) <= .150, NUMBERS['single_click_to_preview_ms'])
    NUMBERS['click_preview_worst_main_loop_gap_ms'] = [round(x * 1000, 1) for x in stalls]
    # The very first preview of a session pays one-off toolkit warm-up (icon theme, first paint of a new widget tree)
    # inside GTK's own drawing, not in application code (the Python handler takes ~10 ms); it is recorded, not gated
    # tightly. Every later preview must keep the loop free.
    check('1c the GTK thread was never held for more than 150 ms after the first (cold) preview (gaps %s ms), and never more than 400 ms'
          % NUMBERS['click_preview_worst_main_loop_gap_ms'], max(stalls[1:]) <= .150 and stalls[0] <= .400,
          NUMBERS['click_preview_worst_main_loop_gap_ms'])
    check('1d single clicks never entered a room (and sent nothing)', not sent(mark), mutating_actions(mark))
    pump(1.2)
    check('1e still no command after waiting longer than any double-click window', not sent(mark), mutating_actions(mark))
    check('1f no blocking file/network/decode call from application code on the GTK thread during click -> preview',
          not tracer.hits, tracer.hits[:6])
    # A slow second click (after the double-click time) is a separate click, not an entry.
    window.rooms.close_preview(animate=False)
    pump(.5)
    click_card(cards[0])
    pump(.9)
    second_press(window, cards[0])
    pump(.8)
    check('1g a second click after the double-click time does not enter', not sent(mark), mutating_actions(mark))
    # Double-click: second press while the preview from the first click is up.
    window.rooms.close_preview(animate=False)
    pump(.6)
    mark = len(COMMANDS)
    click_card(cards[1])
    pump(.08)
    second_press(window, cards[1])
    pump(1.5)
    enters = [c for c in sent(mark) if c[0] == 'enter']
    check('1h double-click enters exactly once', len(enters) == 1 and len(sent(mark)) == 1, mutating_actions(mark))
    check('1i the room is really entered (controller active)', controller.active is not None)
    window.show_rooms()
    pump(.6)
    # Entering the room you are already in sends nothing.
    mark = len(COMMANDS)
    cards = room_cards(window)
    entry_name = (snap().get('active') or {}).get('name')
    same = next((c for c in cards if window.rooms.card_for[c]['name'] == entry_name), None)
    if same is not None:
        window.rooms.close_preview(animate=False)
        click_card(same)
        pump(.08)
        second_press(window, same)
        pump(.9)
        check('1j double-click on the room you are already in sends no enter and shows Now',
              not [c for c in sent(mark) if c[0] == 'enter'] and window.current_page() == 'now', mutating_actions(mark))
    else:
        check('1j (the active room has no card in the grid; skipped)', True)
    leave()
    # Enter / Return key on a focused card (child-activated) enters once.
    window.show_rooms()
    pump(.6)
    card = window.cards.get_first_child()
    mark = len(COMMANDS)
    window.cards.emit('child-activated', card)
    pump(1.5)
    check('1k Enter on a card enters exactly once', len([c for c in sent(mark) if c[0] == 'enter']) == 1, mutating_actions(mark))
    leave()


def s1b_other_selection_speed():
    """Criterion 1: other selections do no blocking work on the GTK thread and update within 100 ms."""
    start_room()
    window = launch('listen')
    page = window.listen
    measured = {}

    def timed(name, action, ready):
        start = time.monotonic()
        action()
        while not ready() and time.monotonic() - start < 1.5:
            context.iteration(False)
        measured[name] = round((time.monotonic() - start) * 1000, 1)
        pump(.25)

    keys = [k for k, _t in page.scope_list]
    with tracer, StallMeter() as stall:
        for key in keys:
            timed('scope:' + key, lambda key=key: page.scopes.set_active(keys.index(key)), lambda key=key: page.scope == key)
        page.scopes.set_active(keys.index('all'))
        pump(.4)
        window.show_now()
        pump(.4)
        timed('open Mix', window.open_mix, lambda: window.sheet.get_open())
        pump(.5)
        window.sheet.set_open(False)
        pump(.4)
        window.show_listen()
        station = next(s for s in controller.stations() if s['name'] == 'Morning lift')
        timed('open a station', lambda: page.show_station(station), lambda: page.stack.get_visible_child_name() == 'station')
        pump(.6)
        groups = [g for g in toggles(page.widget) if g.get_mapped() and g.get_n_toggles() >= 2 and 'Low' in toggle_names(g)]
        if groups:
            timed('mood choice', lambda: groups[0].set_active(toggle_names(groups[0]).index('Low')), lambda: groups[0].get_active() == toggle_names(groups[0]).index('Low'))
            pump(.8)
    NUMBERS['selection_ms'] = measured
    slow = {k: v for k, v in measured.items() if v > 100}
    check('1l every selection (scope chips, Mix, station, mood) updates within 100 ms: %s' % measured, not slow and len(measured) >= 8, slow)
    check('1m the GTK thread was never held for more than 120 ms during those selections (worst %.0f ms)' % (stall.worst * 1000),
          stall.worst <= .120, stall.worst)
    check('1n no blocking file/network/decode call from application code on the GTK thread while selecting',
          not tracer.hits, tracer.hits[:8])
    check('1o no controller request was made synchronously on the GTK thread during the whole scenario',
          not [c for c in COMMANDS if c[2]], [c[0] for c in COMMANDS if c[2]][:8])


# =======================================================================================================
def s2_inertness():
    """Criteria 2/3/5: browsing ambience mode, YouTube search and the new-station dialog start nothing."""
    start_room()
    window = launch('now')
    page = window.listen
    effect_mark, mark = len(backend.calls), len(COMMANDS)
    before = layer_view()
    window.open_mix()
    pump(.8)
    window.mix.rows['atmosphere'].change.emit('clicked')
    pump(.8)
    for key in [k for k, _t in page.scope_list]:
        scope_to(page, key)
    scope_to(page, 'youtube')
    type_query(page, 'rain sounds')
    scope_to(page, 'radio')
    type_query(page, 'ambient')
    scope_to(page, 'library')
    type_query(page, 'night')
    check('2a browsing every ambience scope and searching sent no start command', not sent(mark), mutating_actions(mark))
    check('2b ... and nothing reached the audio backend', not effects_since(effect_mark), effects_since(effect_mark))
    check('2c ... and the room layers are exactly as before', layer_view() == before)
    press = buttons(page.widget, 'Cancel')
    if press:
        click(press[0])
    window.show_listen()
    pump(.5)
    scope_to(page, 'all')
    mark, effect_mark = len(COMMANDS), len(backend.calls)
    click(buttons(page.index_body, 'New station')[0])
    pump(.8)
    dialog = dialogs(window)
    check('2d New station opens a dialog', dialog is not None)
    if dialog is not None:
        modes = next(g for g in toggles(dialog) if 'link' in toggle_names(g))
        scopes = next(g for g in toggles(dialog) if 'radio' in toggle_names(g))
        modes.set_active(1)
        pump(.3)
        search = next(w for w in walk(dialog) if isinstance(w, Gtk.SearchEntry))
        for index in range(scopes.get_n_toggles()):
            scopes.set_active(index)
            search.set_text('ambient')
            search.emit('activate')
            pump(1.2)
        modes.set_active(0)
        pump(.3)
        link = next(w for w in walk(dialog) if isinstance(w, Gtk.Entry) and not isinstance(w, Gtk.SearchEntry))
        for text in ('https://www.youtube.com/watch?v=abcdefghijk', 'http://192.168.1.20:8000/x.mp3', 'https://www.twitch.tv/lofigirl'):
            link.set_text(text)
            click(buttons(dialog, 'Use this link')[0])
        check('2e the new-station dialog (all modes, searches, link checks) sent no command', not sent(mark), mutating_actions(mark))
        check('2f ... and started no sound', not effects_since(effect_mark), effects_since(effect_mark))
        dialog.force_close()
    pump(.4)
    scope_to(page, 'channels')
    KNOBS['twitch_connected'], KNOBS['twitch_curated'] = False, []
    page.refresh_twitch(force=True)
    pump(1.0)
    mark = len(COMMANDS)
    entries = buttons(page.index_body, 'Open Preferences')
    if entries:
        click(entries[0])
        pump(1.0)
        if getattr(window, 'preferences', None) is not None:
            window.preferences.force_close()
    check('2g opening Twitch setup sent no command', not sent(mark), mutating_actions(mark))


# =======================================================================================================
def s3_ambience():
    """Criterion 2: the Ambience row changes through Listen's ambience mode and replaces only that layer."""
    start_room()
    service.execute('layer', {'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass', 'name': 'Rain on glass'})
    settle(.8)
    window = launch('now')
    page = window.listen
    window.open_mix()
    pump(.9)
    row = window.mix.rows['atmosphere']
    check('3a Ambience has a plain Change… button (like Music), not a menu',
          isinstance(row.change, Gtk.Button) and not isinstance(row.change, Gtk.MenuButton) and row.change.get_label() == 'Change…',
          row.change.get_label())
    music_row = window.mix.rows['music']
    check('3b Music and Ambience use the same kind of change control',
          type(music_row.change) is type(row.change) and music_row.change.get_label() == 'Change…')
    mark = len(COMMANDS)
    click(row.change)
    pump(.6)
    texts = label_texts(page.widget)
    check('3c Change… opens Listen with an ambience-mode title naming the room',
          window.current_page() == 'browse' and any(t.startswith('Choose an ambience for') for t in texts), sorted(texts)[:12])
    scope_titles = [t for _k, t in page.scope_list]
    check('3d the ambience scopes include sounds, radio, live channels, YouTube and the library',
          {'Sounds', 'Radio', 'Live channels', 'YouTube', 'Library'} <= set(scope_titles), scope_titles)
    check('3e Sounds is the first scope and lists offline sounds with Play as ambience as the primary action',
          scope_titles[0] == 'Sounds' and page.scope == 'sounds' and len(buttons(page.index_body, 'Play as ambience')) >= 6
          and not buttons(page.index_body, 'Tune in'))
    check('3f nothing was sent by opening ambience mode', not sent(mark), mutating_actions(mark))
    sources = [('sounds', None, 'a sound'), ('radio', None, 'a radio'), ('library', None, 'a library item'),
               ('youtube', 'rain', 'a YouTube result'), ('channels', None, 'a live channel')]
    KNOBS['twitch_connected'] = True
    page.refresh_twitch(force=True)
    pump(1.0)
    for scope, query, what in sources:
        window.open_mix()
        pump(.5)
        if window.current_page() != 'browse':
            window.mix.rows['atmosphere'].change.emit('clicked')
            pump(.6)
        scope_to(page, scope)
        if query:
            type_query(page, query)
        options = buttons(page.index_body, 'Play as ambience')
        if not options:
            check('3g %s offers Play as ambience' % what, False, sorted(label_texts(page.index_body))[:12])
            continue
        before = layer_view()
        mark = len(COMMANDS)
        pick = options[-1] if scope == 'sounds' else options[0]
        pick.emit('clicked')
        pump(1.4)
        commands = mutating_actions(mark)
        after = layer_view()
        one = len(commands) == 1 and commands[0][0] in ('layer', 'atmosphere') and (commands[0][1].get('layer', 'atmosphere') == 'atmosphere')
        check('3g %s: one ambience command, replacing only the ambience layer' % what,
              one and after['music'] == before['music'] and after['picture'] == before['picture'] and after['atmosphere'] != before['atmosphere'],
              (commands, before['atmosphere'][:80], after['atmosphere'][:80]))
        check('3h %s: returns to the Mix over Now' % what, window.current_page() == 'now' and window.sheet.get_open())
    window.open_mix()
    window.mix.rows['atmosphere'].change.emit('clicked')
    pump(.6)
    mark = len(COMMANDS)
    click(buttons(page.widget, 'Cancel')[0])
    pump(.6)
    check('3i Cancel returns to the Mix and sends nothing', window.current_page() == 'now' and window.sheet.get_open() and not sent(mark))
    window.mix.rows['music'].change.emit('clicked')
    pump(.7)
    check('3j the Music change path still opens Listen with Tune in (not ambience mode)',
          window.current_page() == 'browse' and buttons(page.index_body, 'Tune in') and not buttons(page.index_body, 'Play as ambience')
          and not any(t.startswith('Choose an ambience') for t in label_texts(page.widget)))
    window.show_now()
    window.open_mix()
    pump(.5)
    backend.atmosphere_error = '[Errno 111] Connection refused'
    window.mix.rows['atmosphere'].change.emit('clicked')
    pump(.6)
    scope_to(page, 'sounds')
    before = layer_view()
    buttons(page.index_body, 'Play as ambience')[0].emit('clicked')
    pump(1.4)
    backend.atmosphere_error = None
    shown_text = ' '.join(sorted(label_texts(window)))
    check('3k a failing ambience choice keeps the old ambience and shows no raw error',
          layer_view()['atmosphere'] == before['atmosphere'] and 'Errno' not in shown_text and 'ValueError' not in shown_text, shown_text[:300])


# =======================================================================================================
LINK_OK = 'http://192.168.1.20:8000/night.mp3'


def open_new_station(window):
    page = window.listen
    window.show_listen()
    page.show_index()
    page.search.set_text('')
    scope_to(page, 'all')
    click(buttons(page.index_body, 'New station')[0])
    pump(.7)
    return dialogs(window)


def entries_of(dialog):
    return [w for w in walk(dialog) if isinstance(w, Gtk.Entry) and not isinstance(w, Gtk.SearchEntry)]


def create_button(dialog):
    return buttons(dialog, 'Create station', mapped=False)[0]


def tune_buttons(page):
    return [b for b in walk(page.widget) if isinstance(b, Gtk.Button) and b.get_mapped() and button_text(b) in ('Tune in', 'Play')]


def s4_new_station():
    """Criterion 3: a station from scratch."""
    leave()
    if not any(s['name'] == 'Morning lift' for s in controller.stations()):
        service.execute('station.save', {'station': STATION})
    window = launch('listen')
    page = window.listen
    dialog = open_new_station(window)
    check('4a New station opens a form with a name, a start and moods',
          dialog is not None and any('Low' in toggle_names(g) for g in toggles(dialog)) and len(entries_of(dialog)) >= 2)
    name_entry, link_entry = entries_of(dialog)[:2]
    create = create_button(dialog)
    mark = len(COMMANDS)
    check('4b Create is off before anything is chosen', not create.get_sensitive())
    refused = ['file:///etc/passwd', 'javascript:alert(1)', 'ftp://example.test/a.mp3', 'https://user:pw@example.test/a.mp3', 'not a link', '']
    leaked = []
    for url in refused:
        link_entry.set_text(url)
        name_entry.set_text('Probe')
        pump(.1)
        click(buttons(dialog, 'Use this link')[0])
        pump(.2)
        if create.get_sensitive():
            leaked.append(url)
    check('4c unsafe or non-stream links are never accepted as a start', not leaked, leaked)
    link_entry.set_text('https://www.example.test/some/page.html')
    click(buttons(dialog, 'Use this link')[0])
    check('4d an ordinary web page is refused with a reason', not create.get_sensitive() and any('cannot be a station' in t for t in label_texts(dialog)),
          sorted(label_texts(dialog))[:10])
    link_entry.set_text(LINK_OK)
    name_entry.set_text('   ')
    click(buttons(dialog, 'Use this link')[0])
    check('4e a blank name cannot be created (or is given a real default, never blank)',
          not create.get_sensitive() or name_entry.get_text().strip() != '')
    name_entry.set_text('  Night   stream  ')
    pump(.2)
    check('4f a stream on the user\'s own network is accepted, and said to be', create.get_sensitive()
          and any('own network' in t for t in label_texts(dialog)), sorted(label_texts(dialog))[:10])
    energy = next(g for g in toggles(dialog) if 'Low' in toggle_names(g))
    energy.set_active(toggle_names(energy).index('Low'))
    pump(.2)
    check('4g filling the form sent nothing', not sent(mark), mutating_actions(mark))
    click(create)
    pump(1.4)
    saved = [c for c in sent(mark) if c[0] == 'station.save']
    stored = [s for s in controller.stations() if s['name'] == 'Night stream']
    check('4h Create sends one station.save with the name, the stream and the mood; it is stored',
          len(saved) == 1 and saved[0][1]['station']['sources'][0]['url'] == LINK_OK
          and saved[0][1]['station']['preferences'].get('energy') == 'Low' and len(stored) == 1, (mutating_actions(mark), len(stored)))
    check('4i saving did not start playback', not [c for c in sent(mark) if c[0] in ('station.start', 'enter', 'music', 'link')])
    page.show_index()
    scope_to(page, 'stations')
    check('4j it appears under Your stations', 'Night stream' in label_texts(page.index_body), sorted(label_texts(page.index_body))[:15])
    mark = len(COMMANDS)
    page.show_station(stored[0])
    pump(.6)
    tune = tune_buttons(page)
    check('4k the station page has a Tune in', bool(tune))
    if tune:
        tune[0].emit('clicked')
        pump(1.8)
        starts = [c for c in sent(mark) if c[0] == 'station.start']
        check('4l Tune in sends one station.start for it and the room is playing', len(starts) == 1 and controller.active is not None,
              mutating_actions(mark))
    leave()
    window.show_listen()
    page.show_station(stored[0])
    pump(.6)
    mark = len(COMMANDS)
    click(buttons(page.widget, 'Rename')[0])
    rename = dialogs(window)
    entry = next(w for w in walk(rename) if isinstance(w, Gtk.Entry))
    entry.set_text('Night stream 2')
    rename.emit('response', 'rename')
    pump(1.2)
    rename.force_close()
    names = [s['name'] for s in controller.stations()]
    check('4m Rename saves through station.save and the old name is gone', 'Night stream 2' in names and 'Night stream' not in names
          and any(c[0] == 'station.save' for c in sent(mark)), names)
    current = next(s for s in controller.stations() if s['name'] == 'Night stream 2')
    page.show_station(current)
    pump(.5)
    mark = len(COMMANDS)
    click(buttons(page.widget, 'Delete…')[0])
    confirm = dialogs(window)
    check('4n Delete asks first and sends nothing yet', confirm is not None and not sent(mark), mutating_actions(mark))
    confirm.emit('response', 'cancel')
    confirm.force_close()
    pump(.5)
    check('4o cancelling keeps it', any(s['name'] == 'Night stream 2' for s in controller.stations()) and not sent(mark))
    click(buttons(page.widget, 'Delete…')[0])
    confirm = dialogs(window)
    confirm.emit('response', 'delete')
    confirm.force_close()
    pump(1.4)
    check('4p confirming deletes exactly that station', not any(s['name'] == 'Night stream 2' for s in controller.stations())
          and any(s['name'] == 'Morning lift' for s in controller.stations())
          and [c[0] for c in sent(mark)].count('station.delete') == 1, mutating_actions(mark))
    pre = next(s for s in controller.stations() if s['name'] == 'Morning lift')
    window.show_listen()
    page.show_station(pre)
    pump(.5)
    delete = buttons(page.widget, 'Delete…')
    check('4q a pre-existing station offers Delete…', bool(delete))
    if delete:
        click(delete[0])
        confirm = dialogs(window)
        check('4r ... and asks before deleting it', confirm is not None)
        if confirm is not None:
            confirm.emit('response', 'delete')
            confirm.force_close()
            pump(1.4)
        check('4s ... and it is gone', not any(s['name'] == 'Morning lift' for s in controller.stations()))
    service.execute('station.save', {'station': dict(STATION, name='Playing now')})
    playing = next(s for s in controller.stations() if s['name'] == 'Playing now')
    service.execute('station.start', {'id': playing['id']})
    settle(1.2)
    page.show_station(playing)
    pump(.6)
    delete = buttons(page.widget, 'Delete…')
    if delete:
        mark = len(COMMANDS)
        click(delete[0])
        pump(.8)
        confirm = dialogs(window)
        said = ' '.join(sorted(label_texts(window) | (label_texts(confirm) if confirm is not None else set())))
        if confirm is not None:
            confirm.emit('response', 'delete')
            confirm.force_close()
        pump(1.4)
        check('4t deleting the station that is playing is explained in words (stop or leave first), nothing is deleted, no raw error',
              any(s['name'] == 'Playing now' for s in controller.stations()) and ('before deleting' in said or 'stop' in said.lower())
              and 'Errno' not in said and 'Traceback' not in said, said[:300])
    else:
        check('4t the playing station offers no Delete… (acceptable: it cannot be removed while playing)', True)
    leave()


def s4b_seeds():
    """Criterion 3: the seed can be any search result or an AudioMuse mix."""
    leave()
    KNOBS['twitch_connected'] = True
    window = launch('listen')
    page = window.listen
    page.refresh_twitch(force=True)
    pump(1.0)
    cases = [('radio', 'ambient', 'Ambient Drift', 'https://radio.example.test/drift'),
             ('library', 'night', 'Night collection', None),
             ('library', 'nocturne', 'Nocturne in C', None),
             ('library', 'evening', 'Evening wind-down', None),
             ('channels', 'lofi', 'lofigirl', 'https://www.twitch.tv/lofigirl'),
             ('youtube', 'lofi', 'lofi result 0', None)]
    for scope, query, title, url in cases:
        label = '%s %r' % (scope, query)
        dialog = open_new_station(window)
        if dialog is None:
            check('4u seed from %s' % label, False, 'no dialog')
            continue
        toggles(dialog)[0].set_active(1)
        pump(.3)
        scopes = next(g for g in toggles(dialog) if 'radio' in toggle_names(g))
        scopes.set_active(toggle_names(scopes).index(scope))
        search = next(w for w in walk(dialog) if isinstance(w, Gtk.SearchEntry))
        search.set_text(query)
        search.emit('activate')
        pump(1.6)
        starts = [b for b in walk(dialog) if isinstance(b, Gtk.Button) and button_text(b) == 'Start from this' and b.get_mapped()]
        wanted = None
        for b in starts:
            row = b.get_parent()
            if any(title in w.get_text() for w in walk(row) if isinstance(w, Gtk.Label)):
                wanted = b
                break
        if wanted is None and scope in ('radio', 'channels', 'youtube') and starts:
            wanted = starts[0]
        if wanted is None:
            check('4u seed from %s lists a result to start from' % label, False, sorted(label_texts(dialog))[:15])
            dialog.force_close()
            continue
        mark = len(COMMANDS)
        wanted.emit('clicked')
        pump(.3)
        name_entry = entries_of(dialog)[0]
        check('4v choosing a %s result fills the name and sends nothing' % scope, name_entry.get_text().strip() != '' and not sent(mark),
              (name_entry.get_text(), mutating_actions(mark)))
        similar = [w for w in walk(dialog) if isinstance(w, Gtk.CheckButton) and 'AudioMuse' in (w.get_label() or '')]
        audiomuse = False
        if title == 'Nocturne in C':
            check('4w a library track offers the AudioMuse mix', bool(similar) and similar[0].get_visible())
            if similar:
                similar[0].set_active(True)
                audiomuse = True
        else:
            check('4w a %s seed does not offer the AudioMuse mix' % scope if scope != 'library' else '4w an album/playlist seed is created',
                  scope == 'library' or not (similar and similar[0].get_visible()))
        before = {s['id'] for s in controller.stations()}
        click(create_button(dialog))
        pump(1.5)
        new = [s for s in controller.stations() if s['id'] not in before]
        ok = len(new) == 1 and bool(new[0]['sources'])
        source_ok = ok and (url is None or new[0]['sources'][0].get('url') == url)
        if ok and scope == 'library':
            source_ok = source_ok and bool(new[0]['sources'][0].get('reference'))
        if ok and audiomuse:
            source_ok = source_ok and (new[0].get('generation') or {}).get('kind') == 'audiomuse'
        check('4x the saved station from a %s result (%s) has a usable source%s' % (scope, title, ' and the AudioMuse mix' if audiomuse else ''),
              source_ok, json.dumps(new, default=str)[:400])
        if ok:
            scope_to(page, 'stations') if window.listen.stack.get_visible_child_name() == 'index' else None
            check('4y ... and the %s-seeded station appears under Your stations' % scope,
                  (window.show_listen() or True) and (page.show_index() or True) and (scope_to(page, 'stations') or True)
                  and new[0]['name'] in label_texts(page.index_body), (new[0]['name'], sorted(label_texts(page.index_body))[:12]))
            mark = len(COMMANDS)
            page.show_station(new[0])
            pump(.5)
            tune = tune_buttons(page)
            if tune:
                tune[0].emit('clicked')
                pump(2.0)
                # An AudioMuse station needs the AudioMuse service, which this fixture does not run: only the command is gated.
                check('4z ... and tunes in (one start; room active unless it needs the AudioMuse service)',
                      len([c for c in sent(mark) if c[0] == 'station.start']) == 1 and (audiomuse or controller.active is not None),
                      mutating_actions(mark))
            else:
                check('4z ... and tunes in (room active)', False, 'no Tune in on the station page')
            leave()
        window.show_listen()
    KNOBS['twitch_connected'] = False


# =======================================================================================================
def s5_twitch():
    """Criterion 4: Twitch states are truthful and reachable."""
    leave()
    KNOBS.update(twitch_connected=False, twitch_curated=[], twitch_error=None)
    window = launch('listen')
    page = window.listen
    page.refresh_twitch(force=True)
    pump(1.0)
    check('5a with no account and no channels, Listen says "Set up Twitch to see live channels"',
          'Set up Twitch to see live channels' in label_texts(page.index_body), sorted(label_texts(page.index_body))[:15])
    scope_to(page, 'channels')
    texts = label_texts(page.index_body)
    check('5b the Live channels scope shows the setup entry and no false "No live channels" claim',
          'Set up Twitch to see live channels' in texts and not any('No live channels' in t for t in texts), sorted(texts)[:12])
    mark = len(COMMANDS)
    click(buttons(page.index_body, 'Open Preferences')[0])
    pump(1.2)
    prefs = getattr(window, 'preferences', None)
    check('5c the entry opens Preferences on the Twitch connection', prefs is not None and 'Twitch' in all_texts(prefs)
          and any('sign-in' in t.lower() or 'sign in' in t.lower() or 'connect' in t.lower() for t in all_texts(prefs)),
          sorted(all_texts(prefs))[:20] if prefs else None)
    check('5d ... and starts nothing', not sent(mark))
    if prefs is not None:
        prefs.force_close()
        pump(.4)
    KNOBS.update(twitch_connected=False, twitch_curated=['jazzhopcafe'])
    page.refresh_twitch(force=True)
    pump(1.2)
    texts = label_texts(page.index_body)
    check('5e saved channels without an account are listed and live status is said to be unknown',
          'jazzhopcafe' in texts and any('not connected' in t.lower() or 'unknown' in t.lower() or "can't tell" in t.lower() or 'live status' in t.lower() for t in texts),
          sorted(texts)[:15])
    KNOBS.update(twitch_connected=True, twitch_curated=[])
    page.refresh_twitch(force=True)
    pump(1.4)
    texts = label_texts(page.index_body)
    check('5f once set up, live channels are listed and the setup entry is gone', 'lofigirl' in texts and 'Set up Twitch to see live channels' not in texts,
          sorted(texts)[:15])
    mark = len(COMMANDS)
    buttons(page.index_body, 'Tune in')[0].emit('clicked')
    pump(1.5)
    music = [c for c in sent(mark) if c[0] in ('link', 'music', 'station.start')]
    check('5g a live channel tunes in as music with one command naming the channel',
          len(music) == 1 and 'twitch.tv/lofigirl' in json.dumps(music[0][1]), mutating_actions(mark))
    leave()
    start_room()
    window.show_listen()
    page.begin_ambience('now')
    pump(.6)
    scope_to(page, 'channels')
    mark = len(COMMANDS)
    options = buttons(page.index_body, 'Play as ambience')
    check('5h Live channels offers Play as ambience when set up', bool(options))
    if options:
        options[0].emit('clicked')
        pump(1.4)
        check('5i a live channel plays as ambience through one ambience command',
              len(sent(mark)) == 1 and 'twitch' in json.dumps(sent(mark)[0][1]).lower(), mutating_actions(mark))
    leave()
    KNOBS.update(twitch_connected=True, twitch_error='network down')
    window = launch('listen')
    page = window.listen
    page.refresh_twitch(force=True)
    pump(1.3)
    scope_to(page, 'channels')
    texts = label_texts(page.index_body)
    check('5j when Twitch cannot be reached the list does not claim there is nothing live',
          not any(t == 'No live channels yet.' for t in texts), sorted(texts)[:12])
    KNOBS.update(twitch_connected=False, twitch_error=None)
    page.refresh_twitch(force=True)
    pump(.8)
    scope_to(page, 'all')
    page.search.set_text('https://www.twitch.tv/lofigirl')
    pump(.9)
    check('5k a pasted Twitch link still offers the link sheet (works without an account)',
          bool(buttons(page.index_body, 'Open link…')), sorted(label_texts(page.index_body))[:12])


# =======================================================================================================
def alive(pid):
    try:
        os.kill(pid, 0)
        with open('/proc/%d/stat' % pid) as handle:
            return handle.read().split()[2] != 'Z'
    except (OSError, IndexError):
        return False


def s6_youtube():
    """Criterion 5: YouTube search, off-thread, cancellable, inert, truthful when yt-dlp is missing."""
    leave()
    yt_present(True)
    YT_LOG.write_text('')
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    check('6a the YouTube scope says how it works before any search',
          any('Nothing plays until' in t for t in label_texts(page.index_body)), sorted(label_texts(page.index_body))[:10])
    mark, effect_mark = len(COMMANDS), len(backend.calls)
    injected = 'rain; touch /tmp/o6-injection --exec'
    with StallMeter() as stall:
        type_query(page, injected, wait=2.0)
    calls = yt_calls()
    check('6b the search ran yt-dlp with ytsearch, the query as one argument, and no API key',
          len(calls) >= 1 and calls[-1]['argv'][-1].startswith('ytsearch') and calls[-1]['argv'][-1].endswith(injected)
          and not any('key' in a.lower() for a in calls[-1]['argv']), calls[-1:] if calls else None)
    check('6c a query cannot inject a command or an option', not Path('/tmp/o6-injection').exists())
    type_query(page, 'rain', wait=1.8)
    texts = label_texts(page.index_body)
    check('6d results show title, channel and duration, with Tune in',
          any('rain result 0' in t for t in texts) and any('Fixture Channel' in t and '3:05' in t for t in texts)
          and len(buttons(page.index_body, 'Tune in')) == 3, sorted(texts)[:12])
    menus = [w for w in walk(page.index_body) if isinstance(w, Gtk.MenuButton) and w.get_mapped()]
    menu_words = set()
    if menus:
        menus[0].popup()
        pump(.4)
        menu_words = all_texts(menus[0].get_popover())
        menus[0].popdown()
    check('6e each result has a menu with Play as ambience and Open elsewhere', {'Play as ambience', 'Open elsewhere'} <= menu_words, menu_words)
    check('6f YouTube search is inert', not sent(mark) and not effects_since(effect_mark), mutating_actions(mark))
    check('6g the GTK thread was not blocked by the search subprocess (worst gap %.0f ms)' % (stall.worst * 1000), stall.worst <= .200, stall.worst)
    YT_LOG.write_text('')
    page.search.set_text('slow one')
    pump(.3)
    page.query_submitted()
    pump(.7)
    started = yt_calls()
    check('6h a slow search shows that it is searching, with a Stop', any('Searching' in t for t in label_texts(page.index_body))
          and bool(buttons(page.index_body, 'Stop')), sorted(label_texts(page.index_body))[:12])
    stop = buttons(page.index_body, 'Stop')
    if stop:
        click(stop[0])
    pump(1.2)
    pid = started[-1]['pid'] if started else 0
    check('6i Stop kills the yt-dlp process', bool(pid) and not alive(pid), pid)
    check('6j and no result appears afterwards', not any('slow one result' in t for t in label_texts(page.index_body)),
          sorted(label_texts(page.index_body))[:10])
    page.search.set_text('slow two')
    pump(.3)
    page.query_submitted()
    pump(.5)
    first = yt_calls()[-1]['pid']
    page.search.set_text('quick three')
    pump(.3)
    page.query_submitted()
    pump(1.8)
    texts = label_texts(page.index_body)
    check('6k a newer query replaces an older slow one: only the newest results show, the old process is gone',
          any('quick three result' in t for t in texts) and not any('slow two result' in t for t in texts) and not alive(first),
          sorted(texts)[:10])
    page.search.set_text('broken query')
    pump(.3)
    page.query_submitted()
    pump(1.8)
    texts = label_texts(page.index_body)
    check('6l a failing yt-dlp says the search did not finish (no raw error, no crash)',
          not any('Errno' in t or 'Traceback' in t or 'CalledProcessError' in t for t in texts) and any('search' in t.lower() for t in texts),
          sorted(texts)[:12])
    page.search.set_text('https://www.youtube.com/watch?v=abcdefghijk')
    pump(.9)
    opener = buttons(page.index_body, 'Open link…')
    check('6m a pasted YouTube link still offers the link sheet', bool(opener), sorted(label_texts(page.index_body))[:10])
    if opener:
        click(opener[0])
        sheet = dialogs(window)
        check('6n the link sheet offers Play and Play as ambience', sheet is not None and {'Play', 'Play as ambience'} <= all_texts(sheet),
              sorted(all_texts(sheet))[:15] if sheet else None)
        if sheet is not None:
            sheet.force_close()
    page.search.set_text('')
    type_query(page, 'tune me')
    mark = len(COMMANDS)
    tune = buttons(page.index_body, 'Tune in')
    if tune:
        tune[0].emit('clicked')
        pump(1.5)
    check('6o Tune in on a result sends one link command to play it as music',
          len(sent(mark)) == 1 and sent(mark)[0][0] == 'link' and sent(mark)[0][1].get('destination') == 'music'
          and 'youtube.com/watch?v=' in sent(mark)[0][1].get('url', ''), mutating_actions(mark))
    leave()
    yt_present(False)
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    pump(.4)
    before_calls = len(yt_calls())
    type_query(page, 'rain', wait=1.2)
    texts = label_texts(page.index_body)
    check('6p without yt-dlp the YouTube scope says to install it, truthfully', any('yt-dlp' in t for t in texts), sorted(texts)[:12])
    check('6q ... and does not pretend to search or hang on Searching', not any('Searching' in t for t in texts) and len(yt_calls()) == before_calls)
    window.open_preferences()
    pump(.9)
    check('6r Preferences says the same about yt-dlp', any('yt-dlp' in t for t in all_texts(window.preferences)),
          sorted(t for t in all_texts(window.preferences) if 'yt' in t.lower())[:5])
    window.preferences.force_close()
    pump(.3)
    dialog = open_new_station(window)
    toggles(dialog)[0].set_active(1)
    pump(.3)
    scopes = next(g for g in toggles(dialog) if 'radio' in toggle_names(g))
    scopes.set_active(toggle_names(scopes).index('youtube'))
    search = next(w for w in walk(dialog) if isinstance(w, Gtk.SearchEntry))
    search.set_text('rain')
    search.emit('activate')
    pump(1.0)
    check('6s the new-station YouTube search also says yt-dlp is missing', any('yt-dlp' in t for t in label_texts(dialog)),
          sorted(label_texts(dialog))[:12])
    dialog.force_close()
    yt_present(True)


# =======================================================================================================
def connecting_text(window):
    return [t for t in label_texts(window) if t.startswith('Connecting to ')]


def s7_pending():
    """Pending state: Connecting at once, one command for repeated presses, plain-words failure with the room intact."""
    leave()
    window = launch('rooms')
    entry = window.rooms.card_for[room_cards(window)[2]]
    backend.start_delay = 2.5
    mark = len(COMMANDS)
    window.rooms.open_preview(entry)
    pump(.5)
    started = time.monotonic()
    window.rooms.press_enter()
    while not connecting_text(window) and time.monotonic() - started < 1.0:
        context.iteration(False)
    seen_after = time.monotonic() - started
    NUMBERS['connecting_visible_ms'] = round(seen_after * 1000, 1)
    check('7a pressing Enter room shows "Connecting to <room>…" on Now within 150 ms (%.0f ms)' % (seen_after * 1000),
          seen_after <= .150 and window.current_page() == 'now' and ('Connecting to ' + entry['name'] + '…') in connecting_text(window),
          (seen_after, window.current_page(), connecting_text(window)))
    for _ in range(4):
        window.rooms.press_enter()
        window.rooms.enter(entry)
        window.return_room()
    pump(.3)
    enters = [c for c in sent(mark) if c[0] in ('enter', 'return')]
    check('7c repeated presses (Enter room, enter, return) send exactly one command', len(enters) == 1, mutating_actions(mark))
    settle(3.0)
    check('7d when the room is up the connecting card clears and Now shows it',
          not connecting_text(window) and controller.active is not None and window.current_page() == 'now')
    backend.start_delay = 0.
    leave()
    window.show_listen()
    pump(.5)
    scope_to(window.listen, 'radio')
    backend.start_delay = 2.0
    mark = len(COMMANDS)
    tune = buttons(window.listen.index_body, 'Tune in')[0]
    for _ in range(4):
        tune.emit('clicked')
    started = time.monotonic()
    while not connecting_text(window) and time.monotonic() - started < 1.0:
        context.iteration(False)
    pump(.3)
    check('7e four presses of Tune in send one start and Now says Connecting',
          len([c for c in sent(mark) if c[0] in ('music', 'link', 'station.start')]) == 1 and bool(connecting_text(window)), mutating_actions(mark))
    settle(3.0)
    backend.start_delay = 0.
    # (a) The controller cannot be reached / refuses: the pending card says so in words, the previous room stays.
    leave()
    start_room('Morning lift')
    window = launch('rooms')
    previous = (snap().get('active') or {}).get('name')
    before = layer_view()
    FAIL_REQUESTS.update(actions=('enter', 'return', 'link', 'music', 'station.start'), error=ConnectionRefusedError(111, 'Connection refused'))
    entry = window.rooms.card_for[room_cards(window)[3]]
    mark = len(COMMANDS)
    window.rooms.open_preview(entry)
    pump(.4)
    window.rooms.press_enter()
    pump(1.5)
    texts = label_texts(window)
    blob = ' '.join(sorted(texts))
    check('7f a failed start names what failed in plain words, with no error codes or exception names',
          any('start' in t.lower() and entry['name'] in t for t in texts)
          and not any(bad in blob for bad in ('Errno', 'ValueError', 'Traceback', 'Exception', '111', 'Connection refused', '[')), sorted(texts)[:20])
    check('7g ... and offers Try again and a way back', bool(buttons(window, 'Try again')) and bool(buttons(window, 'Back')), sorted(texts)[:20])
    check('7h the previous room is intact in the controller and its name is still the active one',
          layer_view() == before and (snap().get('active') or {}).get('name') == previous, (previous, (snap().get('active') or {}).get('name')))
    check('7i ... and the window says it is still there', previous in blob, sorted(texts)[:20])
    FAIL_REQUESTS.update(actions=())
    mark = len(COMMANDS)
    retry = buttons(window, 'Try again')
    if retry:
        click(retry[0])
    settle(2.0)
    check('7j Try again (after the cause is gone) sends one start and the new room plays',
          len([c for c in sent(mark) if c[0] == 'enter']) == 1 and (snap().get('active') or {}).get('name') == entry['name'],
          (mutating_actions(mark), (snap().get('active') or {}).get('name')))
    check('7k the failure card is gone once it worked', not buttons(window, 'Try again'))
    FAIL_REQUESTS.update(actions=('enter',), error=OSError('The room service is not running'))
    window.show_rooms()
    pump(.5)
    window.rooms.open_preview(window.rooms.card_for[room_cards(window)[1]])
    pump(.4)
    window.rooms.press_enter()
    pump(1.5)
    back = buttons(window, 'Back')
    if back:
        click(back[0])
    FAIL_REQUESTS.update(actions=())
    check('7l a second failure shows its message, and Back dismisses it', bool(back) and not buttons(window, 'Try again'),
          sorted(label_texts(window))[:15])
    leave()
    window = launch('listen')
    scope_to(window.listen, 'radio')
    FAIL_REQUESTS.update(actions=('music', 'link', 'station.start'), error=ConnectionRefusedError(111, 'Connection refused'))
    buttons(window.listen.index_body, 'Tune in')[0].emit('clicked')
    pump(1.6)
    blob = ' '.join(sorted(label_texts(window)))
    check('7m a failed Tune in explains itself in plain words and offers Try again, with no raw error',
          bool(buttons(window, 'Try again')) and 'Errno' not in blob and 'Connection refused' not in blob, sorted(label_texts(window))[:15])
    FAIL_REQUESTS.update(actions=())
    leave()
    # (b) The backend fails to start the music of an existing room: the room keeps its music, in words, and no
    # success is claimed.
    start_room('Morning lift')
    window = launch('rooms')
    before = layer_view()
    backend.start_error = '[Errno 111] Connection refused'
    entry = window.rooms.card_for[room_cards(window)[5]]
    mark = len(COMMANDS)
    window.rooms.open_preview(entry)
    pump(.4)
    window.rooms.press_enter()
    pump(2.0)
    texts = label_texts(window) | label_texts(window.toasts)
    blob = ' '.join(sorted(texts))
    check('7n a backend that cannot start the new music says so in plain words and keeps the old music playing',
          any('couldn’t start' in t or "couldn't start" in t for t in texts) and layer_view()['music'] == before['music']
          and any(c[0] == 'enter' for c in sent(mark))
          and not any(bad in blob for bad in ('Errno', 'ValueError', 'Traceback')), sorted(texts)[:20])
    check('7o ... and does not tell the listener they entered a room whose music did not start',
          not any(t == 'Entered ' + entry['name'] for t in texts), sorted(texts)[:20])
    backend.start_error = None


# =======================================================================================================
def row_menu(row):
    if row.menu.get_mapped():
        row.menu.popup()
    pump(.4)
    items = [button_text(b) for b in walk(row.popover) if isinstance(b, Gtk.Button) and b.get_mapped()]
    row.menu.popdown()
    pump(.2)
    return items


def s8_mix():
    """Mix truthfulness: No ambience, Try again only on failure, a toast with an Undo that restores."""
    start_room()
    window = launch('now')
    page = window.listen
    service.execute('layer', {'layer': 'atmosphere', 'action': 'stop'})
    settle(.9)
    window.open_mix()
    pump(.9)
    row = window.mix.rows['atmosphere']
    texts = label_texts(row)
    scales = [w for w in walk(row) if isinstance(w, Gtk.Scale) and w.get_mapped()]
    mutes = [w for w in walk(row) if isinstance(w, (Gtk.ToggleButton, Gtk.Button)) and w.get_mapped()
             and 'mute' in (button_text(w) + (w.get_tooltip_text() or '')).lower()]
    check('8a with no ambience the row says "No ambience"', 'No ambience' in texts, sorted(texts))
    check('8b ... with no level slider and no Mute', not scales and not mutes, (len(scales), [button_text(m) for m in mutes]))
    check('8c ... and a primary "Choose ambience" button', bool(buttons(row, 'Choose ambience'))
          and buttons(row, 'Choose ambience')[0].has_css_class('suggested-action'), sorted(texts))
    check('8d ... and no Stop or options menu for something that is not playing',
          not any(isinstance(w, Gtk.MenuButton) and w.get_mapped() for w in walk(row)) and not buttons(row, 'Stop'))
    text_blob = ' '.join(label_texts(row)).lower()
    check('8e ... and does not describe a chosen "stream" or "none"', 'stream' not in text_blob and 'none' not in text_blob, text_blob)
    mark = len(COMMANDS)
    click(buttons(row, 'Choose ambience')[0])
    pump(.6)
    scope_to(page, 'sounds')
    pick = buttons(page.index_body, 'Play as ambience')[0]
    pick.emit('clicked')
    off = not pick.get_sensitive()
    pump(1.2)
    titles = toast_titles(window)
    check('8f choosing an ambience gives feedback: a toast naming it, with Undo',
          any(t.startswith('Ambience:') for t in titles) and toast_button(window, 'Undo') is not None, sorted(titles))
    check('8g the pressed control is off while its command runs, and exactly one command was sent', off and len(sent(mark)) == 1,
          (off, mutating_actions(mark)))
    check('8h the ambience layer now has a source', (controller.active['atmosphere'] or {}).get('kind') not in (None, 'none'))
    undo = toast_button(window, 'Undo')
    if undo is not None:
        click(undo)
        settle(1.2)
    window.open_mix()
    pump(.6)
    check('8i Undo returns to no ambience and the Mix shows "No ambience" again',
          (controller.active['atmosphere'] or {}).get('kind') in (None, 'none')
          and 'No ambience' in label_texts(window.mix.rows['atmosphere']), json.dumps(controller.active.get('atmosphere'), default=str))
    for first_kind in ('local', 'radio'):
        if first_kind == 'local':
            service.execute('layer', {'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass', 'name': 'Rain on glass'})
        else:
            service.execute('layer', {'layer': 'atmosphere', 'action': 'select', 'kind': 'radio', 'url': 'https://radio.example.test/drift', 'name': 'Ambient Drift'})
        settle(1.0)
        before = dict(controller.active['atmosphere'])
        window.show_now()
        window.open_mix()
        pump(.5)
        window.mix.rows['atmosphere'].change.emit('clicked')
        pump(.6)
        scope_to(page, 'sounds')
        options = buttons(page.index_body, 'Play as ambience')
        options[-1 if first_kind == 'local' else 0].emit('clicked')
        pump(1.3)
        changed = dict(controller.active['atmosphere'])
        undo = toast_button(window, 'Undo')
        if undo is not None:
            click(undo)
            settle(1.3)
        restored = dict(controller.active['atmosphere'])
        same = all(restored.get(k) == before.get(k) for k in ('kind', 'source_id', 'url', 'name') if k in before)
        check('8j Undo after replacing a %s ambience restores the same source' % first_kind, undo is not None and same and changed != before,
              (before, changed, restored))
    window.show_now()
    window.open_mix()
    pump(.8)
    healthy = {k: row_menu(window.mix.rows[k]) for k in ('music', 'atmosphere', 'picture')}
    check('8k no healthy layer offers Try again; each offers Stop', all('Try again' not in v and 'Stop' in v for v in healthy.values()), healthy)
    for layer, flag in (('music', 'fail_music'), ('picture', 'fail_picture'), ('atmosphere', 'atmosphere_error')):
        setattr(backend, flag, 'Sound could not load' if flag == 'atmosphere_error' else True)
        try:
            controller.layer({'layer': layer, 'action': 'retry'})
        except Exception:
            pass
        settle(1.0)
        window.show_now()
        window.open_mix()
        pump(.8)
        menus = {k: row_menu(window.mix.rows[k]) for k in ('music', 'atmosphere', 'picture')}
        failing_has = 'Try again' in menus[layer]
        others_clean = all('Try again' not in v for k, v in menus.items() if k != layer)
        setattr(backend, flag, None if flag == 'atmosphere_error' else False)
        try:
            controller.layer({'layer': layer, 'action': 'retry'})
        except Exception:
            pass
        settle(1.0)
        check('8l after the %s layer fails only its menu offers Try again' % layer, failing_has and others_clean, menus)
    window.show_now()
    window.open_mix()
    pump(.8)
    recovered = {k: row_menu(window.mix.rows[k]) for k in ('music', 'atmosphere', 'picture')}
    check('8m once the layers recover, Try again is gone again', all('Try again' not in v for v in recovered.values()), recovered)


# =======================================================================================================
def main():
    build_app()
    selected = [x for x in args.only.split(',') if x]
    for fn in [s1_timing, s1b_other_selection_speed, s2_inertness, s3_ambience, s4_new_station, s4b_seeds, s5_twitch,
               s6_youtube, s7_pending, s8_mix]:
        if selected and fn.__name__ not in selected:
            continue
        print('==', fn.__name__, flush=True)
        scenario(fn)


try:
    main()
except Exception:
    traceback.print_exc()
    check('acceptance run completed', False, traceback.format_exc()[-600:])
finally:
    (output / 'acceptance-o6.json').write_text(json.dumps({'results': RESULTS, 'numbers': NUMBERS}, indent=2))
    print('NUMBERS', json.dumps(NUMBERS))
    failed = [r for r in RESULTS if not r['pass']]
    print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed')
    os._exit(1 if failed else 0)
