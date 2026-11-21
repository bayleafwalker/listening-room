"""Independent acceptance checks for Listening Room outcome O10-YOUTUBE (Watch with sound, Show as scene,
YouTube shelves before typing).

Owned by the acceptance author, not the implementer. Written from the brief's criteria and UX assessment
Proposals B and C. Behavioural: the real window on private Xvfb/D-Bus, the real session controller with the
room layers on the session test double and the video window on the real `ambience` module, whose unit
commands go to a fake `systemd-run` that starts a fake mpv JSON-IPC server. A fake `yt-dlp` on PATH serves
search, live and shelf queries. The network guard is installed, so a thumbnail or other HTTP read cannot
leave the machine. Nothing is downloaded or played.

Oracles: the controller command log, the backend effect log, the fake unit/IPC log, the yt-dlp call log,
the controller snapshot and what the window shows.

  env -u WAYLAND_DISPLAY GDK_BACKEND=x11 GDK_DEBUG=no-portals xvfb-run -a -s '-screen 0 1440x1000x24' \
    dbus-run-session python3 tests/probes/listening-o10y-acceptance.py --repo WORKTREE --output OUTDIR [--only w2_watch,...]
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


def check(name, ok, detail=''):
    RESULTS.append({'criterion': name, 'pass': bool(ok), 'detail': str(detail)[:600]})
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ': ' + str(detail)[:600]), flush=True)


folder = tempfile.mkdtemp(prefix='o10y-acceptance-')
os.environ['HOME'] = folder
for key, suffix in [('XDG_STATE_HOME', 'state'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'),
                    ('XDG_RUNTIME_DIR', 'runtime')]:
    path = Path(folder) / suffix
    path.mkdir(mode=0o700)
    os.environ[key] = str(path)
os.environ['GTK_A11Y'] = 'none'
for key in ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'ACOUSTID_CLIENT_KEY'):
    os.environ.pop(key, None)

import netguard  # noqa: E402
netguard.install()

# ---- a fake yt-dlp on PATH ------------------------------------------------------------------------------
BIN = Path(folder) / 'bin'
BIN.mkdir()
EMPTY = Path(folder) / 'empty-bin'
EMPTY.mkdir()
YT_LOG = Path(folder) / 'yt-calls.jsonl'
YT_KNOBS = Path(folder) / 'yt-knobs.json'
(BIN / 'yt-dlp').write_text('''#!%s
import hashlib, json, os, re, sys, time
argv = sys.argv[1:]
knobs = {}
try:
    knobs = json.load(open(os.environ['FAKE_YT_KNOBS']))
except (OSError, ValueError):
    pass
open(os.environ['FAKE_YT_LOG'], 'a').write(json.dumps({'pid': os.getpid(), 'argv': argv, 'at': time.time()}) + '\\n')
if knobs.get('fail'):
    sys.exit(1)
time.sleep(float(knobs.get('delay', 0)))
spec = argv[-1] if argv else ''
match = re.match(r'^ytsearch(\\d*):(.*)$', spec, re.S)
if match:
    query, live = match.group(2), 'live' in match.group(2).lower()
else:
    query, live = spec, spec.startswith('http')
for index in range(3):
    identity = hashlib.md5((query + str(index)).encode()).hexdigest()[:11]
    title = ('Live %%s %%d' if live else '%%s result %%d') %% (query[-40:], index)
    print(json.dumps({'id': identity, 'title': title, 'channel': 'Fixture Channel',
                      'duration': None if live else 185 + index,
                      'live_status': 'is_live' if live else 'not_live'}), flush=True)
''' % sys.executable)
(BIN / 'yt-dlp').chmod((BIN / 'yt-dlp').stat().st_mode | stat.S_IXUSR)
# Revision H1 (acceptance author, phase 2, 2026-11-21): `ambience.start` looks mpv up on PATH before it reaches the
# fake systemd-run, and PATH is only this directory, so Watch failed with "The video player is unavailable". The
# fake mpv is never executed (systemd-run is faked); it only has to exist. A harness fix, no criterion changed.
(BIN / 'mpv').write_text('#!/bin/sh\nexit 1\n')
(BIN / 'mpv').chmod((BIN / 'mpv').stat().st_mode | stat.S_IXUSR)
os.environ['FAKE_YT_LOG'] = str(YT_LOG)
os.environ['FAKE_YT_KNOBS'] = str(YT_KNOBS)


def yt_present(present=True):
    os.environ['PATH'] = str(BIN) if present else str(EMPTY)


def yt_knobs(**knobs):
    YT_KNOBS.write_text(json.dumps(knobs))


def yt_calls():
    return [json.loads(line) for line in YT_LOG.read_text().splitlines()] if YT_LOG.exists() else []


yt_present(True)
yt_knobs()

import gi  # noqa: E402
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import ambience  # noqa: E402
import audio_devices  # noqa: E402
import listening_hub as hub  # noqa: E402
import metadata_pipeline  # noqa: E402
import next_station  # noqa: E402
import next_window  # noqa: E402
import online_services  # noqa: E402
import room_catalogue  # noqa: E402
import room_session  # noqa: E402
import session_client  # noqa: E402
import session_service  # noqa: E402
import twitch_credentials  # noqa: E402
from test_listening_o10y_oracles import FakeProcesses, VideoBackend  # noqa: E402

MAIN = threading.get_ident()
VIDEO_UNIT = 'listening-hub-video.service'
PROCESSES = FakeProcesses()


def fake_unit_run(argv, check=True):
    result = PROCESSES(argv)
    if check and result.returncode:
        raise ambience.AmbienceError(result.stderr or 'Window command failed')
    return result


class Backend(VideoBackend):
    def start_picture(self, picture, cancel=None):
        super().start_picture(picture, cancel)
        self.state['picture'].update(name='Scene', placement=picture.get('placement'))


backend = Backend()
controller = room_session.Controller(backend, Path(folder) / 'controller')
controller._home_action = lambda *_: None
controller._route_preferred = lambda: None
service = session_service.Service(controller)
COMMANDS = []          # (action, args, on_gtk_thread)
READ_ONLY = {'inspect', 'get', 'variants', 'station.list', 'station.recent', 'outputs'}
OUTPUTS = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True}]
SYSTEM_VOLUME = {'percent': 40}


def request(action, arguments=None, *_a, **_k):
    COMMANDS.append((action, copy.deepcopy(arguments or {}), threading.get_ident() == MAIN))
    return service.execute(action, arguments or {})


REAL_TIME = time.time
CLOCK = {'offset': 0.}

patches = [
    patch.object(session_client, 'request', side_effect=request),
    patch.object(ambience, '_run', side_effect=fake_unit_run),
    patch.object(audio_devices, 'player_options', return_value=[]),
    patch.object(audio_devices, 'outputs', return_value=OUTPUTS),
    patch.object(audio_devices, 'test_sound', side_effect=lambda name=None: None),
    patch.object(hub, 'system_volume', side_effect=lambda: dict(SYSTEM_VOLUME)),
    patch.object(hub, 'set_volume', side_effect=lambda value, **kw: SYSTEM_VOLUME.update(percent=value)),
    patch.object(ambience, 'set_lighting', side_effect=lambda level: level),
    patch.object(ambience, 'monitors', return_value=[]),
    patch.object(metadata_pipeline, 'library_origin', return_value='fixture-library'),
    patch.object(online_services, 'config', side_effect=lambda: {}),
    patch.object(twitch_credentials, 'load', side_effect=lambda: twitch_credentials.Credentials()),
    patch('time.time', new=lambda: REAL_TIME() + CLOCK['offset']),
]
for item in patches:
    item.start()

context = GLib.MainContext.default()
APP = None
ROOM_ID = 'midnight-library'


# ---- harness ----------------------------------------------------------------------------------------------
def pump(seconds=.4):
    deadline = time.monotonic() + seconds
    last = 0
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if time.monotonic() - last > .25:
            last = time.monotonic()
            controller.worker.submit(controller.refresh)
        time.sleep(.005)


def settle(seconds=.8):
    try:
        controller.worker.submit(lambda: None).result(timeout=8)
    except Exception:
        pass
    pump(seconds)


def walk(root):
    if root is None:
        return
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


def is_watch(button):
    return button_text(button) in ('Watch', 'Watch with sound') or button.get_tooltip_text() == 'Watch with sound'


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
    {'listen': window.show_listen, 'rooms': window.show_rooms, 'now': window.show_now}[page]()
    pump(.9)
    return window


def leave():
    try:
        service.execute('video', {'action': 'stop'})
    except Exception:
        pass
    PROCESSES.close()
    if controller.active is not None:
        controller.leave(0)
        settle(1.0)
    with controller.lock:
        controller.resume_offer = None
    backend.fail_music = backend.fail_picture = False


def enter_room():
    leave()
    controller.enter({'room_id': ROOM_ID, 'with_view': True})
    settle(1.2)
    assert controller.active, 'the room did not start'


def snap():
    return service.execute('inspect', {})


def effects_since(mark):
    return [c for c in backend.calls[mark:] if c[0] in ('start_music', 'start_atmosphere', 'start_picture', 'start_verified_music')]


def in_toast(widget, top):
    """Inside a shown toast (AdwToastWidget), not just anywhere under the window's toast overlay."""
    parent = widget.get_parent()
    while parent is not None and parent is not top:
        if type(parent).__gtype__.name.startswith('AdwToast') and parent is not top:
            return True
        parent = parent.get_parent()
    return False


def toast_button(window, text):
    return next((w for w in walk(window.toasts) if isinstance(w, Gtk.Button) and w.get_mapped() and button_text(w) == text
                 and in_toast(w, window.toasts)), None)


def wait_toast(window, text, seconds=7.):
    """Toasts queue behind one another (5 s each), so wait for the one asked for."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = toast_button(window, text)
        if found is not None:
            return found
        pump(.2)
    return None


def toast_titles(window):
    return {w.get_text() for w in walk(window.toasts) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text()
            and in_toast(w, window.toasts)}


def scope_to(page, key):
    keys = [k for k, _t in page.scope_list]
    page.scopes.set_active(keys.index(key))
    pump(.5)


def type_query(page, text, wait=1.5):
    page.search.set_text(text)
    page.query_submitted()
    pump(wait)


def rows(root):
    """Listen rows as (title, row widget); a row is the box that holds the row title."""
    found = []
    for w in walk(root):
        if isinstance(w, Gtk.Box) and w.has_css_class('listen-row') and w.get_mapped():
            title = next((x.get_text() for x in walk(w) if isinstance(x, Gtk.Label) and x.has_css_class('row-name')), '')
            found.append((title, w))
    return found


def row_controls(row):
    """The row's own controls in visual order: ('button', text, widget) or ('menu', None, widget)."""
    controls = []
    for w in walk(row):
        if isinstance(w, Gtk.MenuButton) and w.get_mapped():
            controls.append(('menu', None, w))
        elif isinstance(w, Gtk.Button) and w.get_mapped() and not any(
                isinstance(a, Gtk.MenuButton) for a in ancestors(w, row)):
            controls.append(('button', button_text(w), w))
    return controls


def ancestors(widget, stop):
    parent = widget.get_parent()
    while parent is not None and parent is not stop:
        yield parent
        parent = parent.get_parent()


def primary(row):
    return next((text for kind, text, w in row_controls(row) if kind == 'button' and not is_watch(w)), None)


def menu_items(row):
    menu = next((w for kind, _t, w in row_controls(row) if kind == 'menu'), None)
    if menu is None:
        return [], None
    menu.popup()
    pump(.4)
    items = [(button_text(b), b) for b in walk(menu.get_popover()) if isinstance(b, Gtk.Button) and b.get_mapped()]
    return items, menu


def menu_labels(row):
    items, menu = menu_items(row)
    if menu is not None:
        menu.popdown()
        pump(.2)
    return [text for text, _b in items]


def choose_menu(row, prefix):
    items, menu = menu_items(row)
    chosen = next((b for text, b in items if text.startswith(prefix)), None)
    if chosen is not None:
        chosen.emit('clicked')
        pump(.4)
    elif menu is not None:
        menu.popdown()
    return chosen is not None


def watch_button(row):
    return next((w for kind, _t, w in row_controls(row) if kind == 'button' and is_watch(w)), None)


def youtube_rows(page):
    return [(t, r) for t, r in rows(page.index_body) if 'YouTube' in ' '.join(label_texts(r))]


def video_commands(mark, action=None):
    return [c for c in sent(mark) if c[0] == 'video' and (action is None or c[1].get('action') == action)]


def command_url(command):
    arguments = command[1]
    return str(arguments.get('url') or (arguments.get('item') or {}).get('url') or '')


def watching_text(window):
    """The Watching chip as shown: mapped widgets outside toasts (revision H3)."""
    # [ORIGINAL, superseded by H3]: sorted(t for t in all_texts(window) if t.startswith('Watching')) also read
    # hidden labels and toast titles, so a hidden chip or a "Watching …" toast counted as the chip. Harness fix.
    found = set()
    for w in walk(window):
        if not w.get_mapped() or in_toast(w, window.toasts):
            continue
        for getter in ('get_label', 'get_text'):
            value = getattr(w, getter, lambda: None)()
            if isinstance(value, str) and value.startswith('Watching'):
                found.add(value)
    return sorted(found)


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


SHOW_AS_SCENE = 'Show as scene'
B_MENU = ['Show as scene', 'Play as ambience', 'Save as station', 'Open elsewhere']
B_SHEET = ['Play', 'Watch', 'Show as scene', 'Play as ambience', 'Open elsewhere']


def b_order(labels, wanted):
    """Labels match Proposal B's words in order; "Show as scene" may carry its "(silent)" note."""
    normal = [SHOW_AS_SCENE if text.startswith(SHOW_AS_SCENE) else text for text in labels]
    return normal == wanted


# =======================================================================================================
def w1_rows():
    """Criterion 3 (and the Watch entry of criterion 1): row and link-sheet layout, order and words."""
    leave()
    yt_knobs()
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    mark, effect_mark, unit_mark = len(COMMANDS), len(backend.calls), len(PROCESSES.calls)
    type_query(page, 'rain', wait=2.0)
    found = youtube_rows(page)
    check('3a a YouTube search lists results', len(found) >= 3, [t for t, _r in found])
    check('3b on-demand results have Play as the primary action, never Tune in',
          bool(found) and all(primary(r) == 'Play' for _t, r in found), [(t, primary(r)) for t, r in found])
    orders = [[('watch' if kind == 'button' and is_watch(w) else kind if kind == 'menu' else text)
               for kind, text, w in row_controls(r)] for _t, r in found]
    check('3c every result row reads Play, Watch (Watch with sound), then the menu',
          bool(found) and all(order == ['Play', 'watch', 'menu'] for order in orders), orders)
    tips = [watch_button(r).get_tooltip_text() if watch_button(r) else None for _t, r in found]
    check('3d the row Watch button says "Watch with sound"', bool(found) and all(t == 'Watch with sound' for t in tips), tips)
    menus = [menu_labels(r) for _t, r in found[:2]]
    check('3e the row menu is Show as scene, Play as ambience, Save as station, Open elsewhere, in that order',
          bool(menus) and all(b_order(m, B_MENU) for m in menus), menus)
    check('3f searching and opening row menus is inert (no command, no effect, no player)',
          not sent(mark) and not effects_since(effect_mark) and not PROCESSES.calls[unit_mark:],
          (mutating_actions(mark), PROCESSES.calls[unit_mark:]))
    type_query(page, 'live jazz', wait=2.0)
    live = youtube_rows(page)
    check('3g a live result keeps Tune in as its primary action', bool(live) and all(primary(r) == 'Tune in' for _t, r in live),
          [(t, primary(r)) for t, r in live])
    page.search.set_text('https://www.youtube.com/watch?v=abcdefghijk')
    pump(.9)
    opener = buttons(page.index_body, 'Open link…')
    sheet, labels = None, []
    if opener:
        click(opener[0])
        sheet = window.get_visible_dialog()
        labels = [button_text(b) for b in walk(sheet) if isinstance(b, Gtk.Button) and b.get_mapped()
                  and button_text(b) not in ('Cancel', 'Close', '')] if sheet else []
    check('3h the YouTube link sheet offers Play, Watch, Show as scene, Play as ambience, Open elsewhere, in that order',
          b_order(labels, B_SHEET), labels)
    if sheet is not None:
        sheet.force_close()
        pump(.3)
    page.search.set_text('')
    pump(.3)
    # Ambience mode: "Use as ambience" stays primary; Watch and Play as music move into the menu.
    controller.enter({'room_id': ROOM_ID, 'with_view': True})
    settle(1.2)
    page.begin_ambience('now')
    pump(.6)
    scope_to(page, 'youtube')
    type_query(page, 'rain', wait=2.0)
    found = youtube_rows(page)
    check('3i in ambience mode a YouTube result keeps the ambience action primary',
          bool(found) and all(primary(r) in ('Use as ambience', 'Play as ambience') for _t, r in found),
          [(t, primary(r)) for t, r in found])
    check('3j in ambience mode no Watch button shows on the row', bool(found) and not any(watch_button(r) for _t, r in found))
    words = menu_labels(found[0][1]) if found else []
    check('3k in ambience mode the row menu holds Watch and Play as music',
          any(w in ('Watch', 'Watch with sound') for w in words) and any(w in ('Play as music', 'Play') for w in words), words)
    mark = len(COMMANDS)
    chose = bool(found) and choose_menu(found[0][1], 'Watch')
    settle(1.2)
    check('3l choosing Watch in ambience mode ends the mode and starts the video',
          chose and page.mode != 'ambience' and len(video_commands(mark, 'start')) == 1,
          (chose, page.mode, mutating_actions(mark)))


def w2_watch():
    """Criterion 1: Watch is its own window with sound; the room pauses like Quiet; Stop offers Resume."""
    enter_room()
    yt_knobs()
    before = {k: (snap()['layers'].get(k) or {}).get('status') for k in ('music', 'atmosphere')}
    playing = [k for k, v in before.items() if v == 'Playing']
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    type_query(page, 'rain', wait=2.0)
    found = youtube_rows(page)
    title = found[0][0] if found else ''
    button = watch_button(found[0][1]) if found else None
    mark, effect_mark, unit_mark = len(COMMANDS), len(backend.calls), len(PROCESSES.calls)
    if button is not None:
        button.emit('clicked')
    settle(1.5)
    starts = video_commands(mark, 'start')
    others = [c for c in sent(mark) if c[0] != 'video']
    check('1a Watch sends one video start for that result and nothing else',
          len(starts) == 1 and 'watch?v=' in command_url(starts[0]) and not others, mutating_actions(mark))
    check('1b no controller request was made on the GTK thread', bool(sent(mark)) and not any(c[2] for c in sent(mark)), mutating_actions(mark))
    launches = [a for a in PROCESSES.calls[unit_mark:] if a[:1] == ['systemd-run']]
    check('1c one player window with sound opens (the video window), and no music, ambience or scene starts',
          len(launches) == 1 and '--unit=' + VIDEO_UNIT in launches[0] and '--no-audio' not in launches[0]
          and not effects_since(effect_mark), (launches, effects_since(effect_mark)))
    snapshot = snap()
    check('1d the room pauses the way Quiet does, with the resume offer recorded',
          bool(playing) and all(snapshot['layers'][k]['status'] == 'Paused' for k in playing)
          and bool(snapshot.get('resume_offer')), (playing, {k: snapshot['layers'][k].get('status') for k in playing},
                                                   snapshot.get('resume_offer')))
    player = PROCESSES.player(VIDEO_UNIT)
    check('1e the video itself keeps playing', bool(player) and not player['pause'], player and player['pause'])
    window.show_now()
    pump(1.2)
    texts = watching_text(window)
    check('1f Now shows "Watching · title" with Stop watching',
          any(t.startswith('Watching · ') and (title in t or 'Rain on a library window' in t) for t in texts)
          and bool(buttons(window, 'Stop watching')), (title, texts))
    stop = buttons(window, 'Stop watching')
    mark, backend_mark = len(COMMANDS), len(backend.calls)
    if stop:
        click(stop[0])
    settle(1.5)
    check('1g Stop watching sends video stop and closes the window',
          len(video_commands(mark, 'stop')) == 1 and PROCESSES.player(VIDEO_UNIT) is None, mutating_actions(mark))
    resume = wait_toast(window, 'Resume')
    check('1h stopping offers Resume in a toast', resume is not None and any('Resume' in t for t in toast_titles(window)),
          sorted(toast_titles(window)))
    pump(2.0)
    snapshot = snap()
    check('1i it never auto-resumes', bool(playing) and not [c for c in sent(mark) if c[0] == 'resume']
          and not [c for c in backend.calls[backend_mark:] if c[1:2] == ('play',)]
          and all(snapshot['layers'][k]['status'] == 'Paused' for k in playing),
          (mutating_actions(mark), backend.calls[backend_mark:]))
    check('1j the Watching chip is gone after Stop watching', bool(stop) and not watching_text(window) and not buttons(window, 'Stop watching'),
          watching_text(window))
    mark = len(COMMANDS)
    if resume is not None:
        click(resume)
    settle(1.2)
    snapshot = snap()
    check('1k Resume resumes the room explicitly', [c[0] for c in sent(mark)] == ['resume']
          and all(snapshot['layers'][k]['status'] == 'Playing' for k in playing), mutating_actions(mark))
    # The window closes by itself (closed by the user, or the video ended).
    window.show_listen()
    pump(.5)
    found = youtube_rows(page)
    button = watch_button(found[0][1]) if found else None
    if button is not None:
        button.emit('clicked')
    settle(1.5)
    window.show_now()
    pump(.8)
    mark, backend_mark = len(COMMANDS), len(backend.calls)
    PROCESSES.end(VIDEO_UNIT)
    pump(3.0)
    snapshot = snap()
    check('1l when the video window closes, Now drops the Watching chip and offers Resume',
          bool(button) and not watching_text(window) and wait_toast(window, 'Resume') is not None,
          (watching_text(window), sorted(toast_titles(window))))
    check('1m ... and still does not resume by itself', bool(playing) and not [c for c in sent(mark) if c[0] == 'resume']
          and not [c for c in backend.calls[backend_mark:] if c[1:2] == ('play',)]
          and all(snapshot['layers'][k]['status'] == 'Paused' for k in playing), mutating_actions(mark))


def w2b_watch_without_room():
    """Criterion 1: with no room active, Watch just opens the window and plays."""
    leave()
    yt_knobs()
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    type_query(page, 'rain', wait=2.0)
    found = youtube_rows(page)
    button = watch_button(found[0][1]) if found else None
    mark, effect_mark = len(COMMANDS), len(backend.calls)
    if button is not None:
        button.emit('clicked')
    settle(1.5)
    snapshot = snap()
    check('1n with no room, Watch only starts the video (no room, no pause, no resume offer)',
          [c[0] for c in sent(mark)] == ['video'] and snapshot.get('active') is None and not snapshot.get('resume_offer')
          and PROCESSES.player(VIDEO_UNIT) is not None and not effects_since(effect_mark), mutating_actions(mark))
    window.show_now()
    pump(1.0)
    check('1o with no room, Now still shows Watching with Stop watching', bool(watching_text(window))
          and bool(buttons(window, 'Stop watching')), watching_text(window))
    stop = buttons(window, 'Stop watching')
    if stop:
        click(stop[0])
    settle(1.2)
    check('1p with no room, stopping offers no Resume', bool(stop) and wait_toast(window, 'Resume', 6.) is None
          and PROCESSES.player(VIDEO_UNIT) is None, sorted(toast_titles(window)))


def w3_scene():
    """Criterion 2: Show as scene replaces only the picture, marks the room Changed, starts no sound."""
    enter_room()
    yt_knobs()
    before = {k: json.dumps((snap().get('active') or {}).get(k), sort_keys=True) for k in ('music', 'atmosphere')}
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    type_query(page, 'rain', wait=2.0)
    found = youtube_rows(page)
    mark, effect_mark, unit_mark = len(COMMANDS), len(backend.calls), len(PROCESSES.calls)
    chose = bool(found) and choose_menu(found[0][1], SHOW_AS_SCENE)
    settle(1.5)
    pictures = [c for c in effects_since(effect_mark) if c[0] == 'start_picture']
    check('2a Show as scene sends one picture link for that result',
          chose and [(c[0], c[1].get('destination')) for c in sent(mark)] == [('link', 'picture')]
          and 'watch?v=' in command_url(sent(mark)[0]), mutating_actions(mark))
    check('2b only the picture layer changes', len(pictures) == 1 and 'watch?v=' in json.dumps(pictures[0][1])
          and len(effects_since(effect_mark)) == 1, effects_since(effect_mark))
    snapshot = snap()
    check('2c no sound starts or stops: music and ambience untouched, no video window',
          chose and not [c for c in backend.calls[effect_mark:] if c[0] in ('music', 'atmosphere')]
          and not [a for a in PROCESSES.calls[unit_mark:] if '--unit=' + VIDEO_UNIT in a]
          and all(json.dumps((snapshot.get('active') or {}).get(k), sort_keys=True) == before[k] for k in before),
          (backend.calls[effect_mark:], PROCESSES.calls[unit_mark:]))
    window.show_now()
    pump(1.5)
    chip = getattr(window, 'changed_chip', None)
    check('2d the room is marked Changed', chip is not None and chip.get_visible() and chip.get_mapped())
    check('2e and Save as my room is offered', wait_toast(window, 'Save as my room', 11.) is not None, sorted(toast_titles(window)))


def youtube_ids(texts):
    return sorted(t for t in texts if ' result ' in t or t.startswith('Live '))


def files_containing(needle):
    hits = []
    for path in Path(folder).rglob('*'):
        if path.is_file() and path.parent not in (BIN, EMPTY) and path not in (YT_LOG, YT_KNOBS):
            try:
                if needle in path.read_text(errors='ignore'):
                    hits.append(str(path.relative_to(folder)))
            except OSError:
                pass
    return hits


def w4_shelves():
    """Criterion 4: For this room, Live now and saved YouTube before typing; cached; inert; no account."""
    enter_room()
    room = next(r for r in room_catalogue.rooms() if r['id'] == ROOM_ID)
    words = [str(t).lower() for t in room.get('tags') or []]
    saved_name = 'Saved fixture video'
    service.execute('station.save', {'station': {
        'name': saved_name, 'sources': [next_station.source_from_video({'name': saved_name,
                                                                         'url': 'https://www.youtube.com/watch?v=savedfixtur'})],
        'fallbacks': [], 'preferences': {}, 'end_policy': 'stop', 'arc': 'none', 'generation': {'kind': 'collection'}}})
    YT_LOG.write_text('')
    yt_knobs(delay=.8)
    window = launch('listen')
    page = window.listen
    mark, effect_mark, unit_mark = len(COMMANDS), len(backend.calls), len(PROCESSES.calls)
    windows_before = len(build_app().get_windows())
    with StallMeter() as stall:
        scope_to(page, 'youtube')
        early = label_texts(page.index_body)
        pump(4.0)
    texts = label_texts(page.index_body)
    calls = yt_calls()
    check('4a the YouTube scope shows your saved YouTube at once, before any search finishes',
          any(saved_name in t for t in early), sorted(early)[:15])
    check('4b a "For Midnight library" shelf appears before typing', any(t.startswith('For Midnight library') for t in texts),
          sorted(texts)[:20])
    searches = [c['argv'][-1] for c in calls if c['argv'] and c['argv'][-1].startswith('ytsearch')]
    check('4c its queries are built from the room (its tags), not tracked', any(w in s.lower() for s in searches for w in words),
          (words, searches))
    check('4d a Live now shelf appears with live streams that Tune in', 'Live now' in texts
          and any(primary(r) == 'Tune in' for _t, r in youtube_rows(page)), sorted(texts)[:20])
    shelf_rows = youtube_rows(page)
    demand = [(t, r) for t, r in shelf_rows if primary(r) != 'Tune in']
    check('4e shelf videos offer Play and Watch, never Tune in', bool(demand) and all(primary(r) == 'Play' and watch_button(r)
                                                                                       for _t, r in demand),
          [(t, primary(r), bool(watch_button(r))) for t, r in shelf_rows])
    check('4f shelves load off the GTK thread (worst gap @@ ms)'.replace('@@', '%.0f' % (stall.worst * 1000)),
          bool(shelf_rows) and stall.worst <= .25, stall.worst)
    for _t, r in shelf_rows[:3]:
        menu_labels(r)
    check('4g browsing shelves is inert: no command, no effect, no player, no window',
          bool(shelf_rows) and not sent(mark) and not effects_since(effect_mark) and not PROCESSES.calls[unit_mark:]
          and len(build_app().get_windows()) == windows_before and window.get_visible_dialog() is None,
          (mutating_actions(mark), PROCESSES.calls[unit_mark:]))
    check('4h no yt-dlp call uses an account (cookies or personal feeds)',
          bool(calls) and not any(any('cookies' in a or a.startswith(':yt') for a in c['argv']) for c in calls),
          [c['argv'] for c in calls][:6])
    check('4i the YouTube scope never asks to sign in', not any('sign in' in t.lower() or 'cookies' in t.lower()
                                                               for t in all_texts(page.index_body)))
    shown = youtube_ids(texts)
    probe_title = next((t for t in shown if ' result ' in t), '')
    on_disk = files_containing(probe_title) if probe_title else []
    check('4j the shelves are cached on disk', bool(on_disk), (probe_title, on_disk))
    # Reopen within the hour with a slow network: the cache shows at once and is not refetched.
    close_windows()
    YT_LOG.write_text('')
    yt_knobs(delay=4)
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    pump(.4)
    quick = label_texts(page.index_body)
    check('4k reopening shows the cached shelves at once', bool(probe_title) and probe_title in quick, (probe_title, sorted(quick)[:15]))
    check('4l a cached shelf says when it was updated', any(t.startswith('Updated') or 'Updated ' in t for t in quick),
          sorted(quick)[:20])
    pump(1.2)
    check('4m a fresh cache is not refetched (refresh at most hourly)', bool(probe_title) and probe_title in quick and not yt_calls(), [c['argv'] for c in yt_calls()])
    # Two hours later the refresh runs; when it fails, the shelf keeps its results.
    close_windows()
    CLOCK['offset'] = 2 * 3600.
    for name in files_containing(probe_title) if probe_title else []:
        path = Path(folder) / name
        stamp = path.stat().st_mtime - 2 * 3600
        os.utime(path, (stamp, stamp))
    YT_LOG.write_text('')
    yt_knobs(fail=True)
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    pump(3.0)
    texts = label_texts(page.index_body)
    check('4n a cache older than an hour is refreshed in the background', bool(yt_calls()), [c['argv'] for c in yt_calls()])
    check('4o a failed refresh never empties the shelf', bool(probe_title) and probe_title in texts, sorted(texts)[:15])
    CLOCK['offset'] = 0.
    yt_knobs()
    # Zero network: with yt-dlp missing, saved YouTube still shows.
    close_windows()
    yt_present(False)
    window = launch('listen')
    page = window.listen
    scope_to(page, 'youtube')
    pump(.6)
    check('4p saved YouTube shows without yt-dlp (zero network)', any(saved_name in t for t in label_texts(page.index_body)),
          sorted(label_texts(page.index_body))[:15])
    yt_present(True)


# =======================================================================================================
def main():
    build_app()
    selected = [x for x in args.only.split(',') if x]
    # Revision H2 (acceptance author, phase 2, 2026-11-21): w4_shelves runs first. In the original order
    # [w1_rows, w2_watch, w2b_watch_without_room, w3_scene, w4_shelves] the earlier scenarios already opened the
    # YouTube scope in Midnight library, so w4 met a fresh cache and 4c/4h saw no yt-dlp call. A harness fix:
    # w4 must start with no shelf cache; no criterion changed.
    for fn in [w4_shelves, w1_rows, w2_watch, w2b_watch_without_room, w3_scene]:
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
    (output / 'acceptance-o10y.json').write_text(json.dumps({'results': RESULTS}, indent=2))
    failed = [r for r in RESULTS if not r['pass']]
    print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed')
    os._exit(1 if failed else 0)
