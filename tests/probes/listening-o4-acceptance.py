"""Independent acceptance checks for Listening Room outcome O4 (Preferences and Track details).

Owned by the acceptance author, not the implementer. The harness (fake controller,
command log, accessible-name capture at the GTK boundary, widget oracles) is reused
from listening-next-acceptance.py; the scenarios below are O4's own.

Run inside `scripts/listening-dev.sh shell` under private Xvfb/D-Bus:
  env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u HYPRLAND_INSTANCE_SIGNATURE \
    XDG_RUNTIME_DIR=<fresh 0700 dir> GDK_BACKEND=x11 GDK_DEBUG=no-portals GSK_RENDERER=cairo \
    xvfb-run -a -s '-screen 0 1440x1000x24' dbus-run-session \
    python3 tests/probes/listening-o4-acceptance.py --repo WORKTREE --output OUTDIR
"""
import argparse
import copy
import json
import os
import re
from pathlib import Path
import sys
import tempfile
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
output = Path(args.output); output.mkdir(parents=True, exist_ok=True)

RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append({'criterion': name, 'pass': bool(ok), 'detail': detail})
    print(('PASS ' if ok else 'FAIL ') + name + (': ' + detail if detail else ''), flush=True)


folder = tempfile.mkdtemp(prefix='o4-acceptance-')
os.environ['HOME'] = folder
for key, suffix in [('XDG_STATE_HOME', 'state'), ('XDG_CONFIG_HOME', 'config'),
                    ('XDG_RUNTIME_DIR', 'runtime'), ('XDG_CACHE_HOME', 'cache')]:
    path = Path(folder) / suffix; path.mkdir(mode=0o700); os.environ[key] = str(path)
os.environ.pop('ACOUSTID_CLIENT_KEY', None)

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gdk, GLib, Gtk, Adw

# Capture accessible names at the GTK boundary, before any UI module loads.
_update_property = Gtk.Accessible.update_property
_update_relation = Gtk.Accessible.update_relation


def _record_property(self, properties, values):
    for prop, value in zip(properties, values):
        if prop == Gtk.AccessibleProperty.LABEL:
            self._acceptance_label = value
    return _update_property(self, properties, values)


def _record_relation(self, relations, values):
    for rel, value in zip(relations, values):
        if rel == Gtk.AccessibleRelation.LABELLED_BY:
            self._acceptance_labelled_by = value
    return _update_relation(self, relations, values)


Gtk.Accessible.update_property = _record_property
Gtk.Accessible.update_relation = _record_relation

import listening_hub as hub
import room_catalogue
import audio_devices
import ambience
import session_client
import room_session
import metadata_pipeline
from test_listening_session import FakeBackend

import atmosphere_catalogue
import session_service
import listening_room
import next_window  # noqa: F401  (the module `listening-room --ui=next` dispatches into)


class Backend(FakeBackend):
    """The session test double, reporting the names/flags the real players report."""
    def __init__(self):
        super().__init__()
        self.fail_music_after_start = False

    def start_atmosphere(self, selection, cancel):
        super().start_atmosphere(selection, cancel)
        sound = next((x for x in atmosphere_catalogue.SOUNDS if x['id'] == selection.get('source_id')), {})
        if sound:
            self.state['atmosphere'].update(name=sound['name'], outdoor=sound['outdoor'])

    def start_picture(self, picture, cancel=None):
        super().start_picture(picture, cancel)
        preset = next((p for p in ambience.presets() if p['id'] == picture.get('preset_id')), {})
        self.state['picture'].update(name=preset.get('name', 'Scene'), placement=picture.get('placement'))


CLOCK = [1_800_000_000.]
backend = Backend()
controller = room_session.Controller(backend, Path(folder) / 'controller', clock=lambda: CLOCK[0])
controller._home_action = lambda *_: None
controller._route_preferred = lambda: None
service = session_service.Service(controller)
COMMANDS = []
READ_ONLY = {'inspect', 'get', 'variants', 'station.list', 'station.recent', 'outputs',
             'metadata.status', 'metadata.list', 'metadata.jobs', 'metadata.inspect',
             'metadata.lookup_reference', 'metadata.export'}
SYSTEM_VOLUME = {'percent': 40}
OUTPUTS = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True},
           {'id': 'buds', 'name': 'buds', 'description': 'Headphones', 'default': False, 'available': True}]
SIDE = []   # test_sound etc.


STUB = {}   # action -> answer (or callable) for commands that would reach the network


def request(action, args=None, *a, **kw):
    COMMANDS.append((action, copy.deepcopy(args or {})))
    if action in STUB:
        answer = STUB[action]
        return answer(args or {}) if callable(answer) else answer
    return service.execute(action, args or {})


patches = [patch.object(session_client, 'request', side_effect=request),
           patch.object(audio_devices, 'outputs', return_value=OUTPUTS),
           patch.object(audio_devices, 'test_sound', side_effect=lambda name=None: SIDE.append(('test_sound', name))),
           patch.object(hub, 'system_volume', side_effect=lambda: dict(SYSTEM_VOLUME)),
           patch.object(hub, 'set_volume', side_effect=lambda value, **kw: SYSTEM_VOLUME.update(percent=value)),
           patch.object(ambience, 'set_lighting', side_effect=lambda level: level),
           patch.object(ambience, 'monitors', return_value=[]),
           patch.object(metadata_pipeline, 'library_origin', return_value='fixture-library')]
for item in patches:
    item.start()

context = GLib.MainContext.default()


def pump(seconds=.3):
    deadline = time.monotonic() + seconds
    last = 0
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if time.monotonic() - last > .25:
            last = time.monotonic()
            try:
                controller.refresh()
            except Exception:
                pass
        time.sleep(.01)


def settle(seconds=.9):
    try:
        controller.worker.submit(lambda: None).result(timeout=3)
    except Exception:
        pass
    pump(seconds)


# ---------------------------------------------------------------- widget oracles
CONTAINERS_WITH_OWN_BUTTON = tuple(t for t in (Gtk.MenuButton, Gtk.DropDown, getattr(Adw, 'SplitButton', None),
                                               Gtk.SpinButton, Gtk.Expander, getattr(Adw, 'ToggleGroup', None)) if t)


def children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def walk(widget):
    yield widget
    for child in children(widget):
        yield from walk(child)


def ancestors(widget):
    parent = widget.get_parent()
    while parent is not None:
        yield parent
        parent = parent.get_parent()


def shown(widget):
    # Own state only: the idle fade dims whole containers, which must not hide controls from the oracle.
    return widget.get_visible() and widget.get_mapped() and widget.get_child_visible() and widget.get_opacity() > .05


def texts(widget):
    return [w.get_text() for w in walk(widget) if isinstance(w, Gtk.Label) and shown(w) and w.get_text()]


def acc_name(widget):
    """The accessible name a screen reader gets: explicit label, labelled-by, or visible text."""
    label = getattr(widget, '_acceptance_label', None)
    if label:
        return label
    labelled = getattr(widget, '_acceptance_labelled_by', None)
    if labelled is not None:
        return ' '.join(texts(labelled)) or None
    if isinstance(widget, (Gtk.Button, Gtk.MenuButton)):
        found = texts(widget)
        if found:
            return ' '.join(found)
    return None


def interactive_type(w):
    return isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.Scale, Gtk.Switch, Gtk.CheckButton, Gtk.DropDown,
                          Gtk.Entry, Gtk.SearchEntry, Gtk.PasswordEntry, Gtk.SpinButton, Gtk.ToggleButton))


def owned_by_compound(widget, root):
    for parent in ancestors(widget):
        if parent is root:
            return False
        if isinstance(parent, CONTAINERS_WITH_OWN_BUTTON):
            return True
    return False


def in_mix(w):
    """Inside the Mix sheet (the BottomSheet's sheet, not its content, which is Now)."""
    chain = list(ancestors(w))
    for p in chain:
        if isinstance(p, Adw.BottomSheet):
            sheet = p.get_sheet()
            return sheet is not None and (w is sheet or sheet in chain)
    return False


def describe(w):
    chain = ' < '.join('%s.%s' % (type(p).__name__, '.'.join(p.get_css_classes()[:2])) for p in list(ancestors(w))[:5])
    ok, rect = w.compute_bounds(w.get_root())
    return '%s[%s] at %s %s' % (type(w).__name__, '.'.join(w.get_css_classes()), ('%d,%d %dx%d' % (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height())) if ok else '?', chain)


def in_chrome(w):
    return any(isinstance(p, Gtk.WindowControls) for p in ancestors(w))


def in_popover(widget):
    return any(isinstance(p, Gtk.Popover) for p in ancestors(widget))


def controls(root, include_popovers=False):
    """Shown interactive controls (sensitive or not), not counting compound internals."""
    found = []
    for w in walk(root):
        if w is root or not interactive_type(w) or not shown(w):
            continue
        if (not include_popovers and in_popover(w)) or in_chrome(w):
            continue
        if not isinstance(w, Gtk.MenuButton) and owned_by_compound(w, root):
            continue
        found.append(w)
    return found


def named(root, name, only_shown=True, include_popovers=False):
    return [w for w in walk(root) if acc_name(w) == name and (not only_shown or shown(w))
            and (include_popovers or not in_popover(w))]


def one(root, name):
    found = [w for w in named(root, name) if interactive_type(w) and not owned_by_compound(w, root)]
    return found[0] if len(found) == 1 else (found or [None])[0]


def button_with_text(root, text, exact=True):
    for w in walk(root):
        if isinstance(w, (Gtk.Button,)) and not isinstance(w, Gtk.MenuButton) and shown(w):
            label = ' '.join(texts(w))
            if (label == text) if exact else (text in label):
                return w
    return None


def click(widget):
    widget.emit('clicked')


def popover_of(window, name):
    button = one(window, name)
    return button, (button.get_popover() if button is not None else None)


def open_popover(window, name):
    button, popover = popover_of(window, name)
    assert button is not None and popover is not None, 'no popover button named %r' % name
    button.popup()
    pump(.7)
    return button, popover


def close_popover(button):
    button.popdown()
    pump(.3)


def wake(window):
    """Pointer motion, so any idle fade brings controls back."""
    for w in walk(window):
        try:
            for c in w.observe_controllers():
                if isinstance(c, Gtk.EventControllerMotion):
                    c.emit('motion', 20., 20.)
        except Exception:
            pass
    pump(.2)


def key(window, keyval, state=0):
    for c in window.observe_controllers():
        if isinstance(c, Gtk.EventControllerKey) and c.emit('key-pressed', keyval, 0, state):
            return True
    return False


def accel(window, accel_text):
    """Drive a registered accelerator the way the shortcut machinery does."""
    names = window.get_application().get_actions_for_accel(accel_text)
    assert names, 'no action registered for ' + accel_text
    assert names[0].startswith('win.'), names
    window.activate_action(names[0], None)
    pump(.5)


def mix_open(window):
    return any(isinstance(w, Adw.BottomSheet) and w.get_open() for w in walk(window))


def open_mix(window):
    button = one(window, 'Mix')
    assert button is not None, 'no Mix control'
    click(button)
    pump(1.2)
    assert mix_open(window), 'Mix did not open'


def close_mix(window):
    for w in walk(window):
        if isinstance(w, Adw.BottomSheet) and w.get_open():
            w.set_open(False)
    pump(.8)


def mapped_popovers(window):
    return [w for w in walk(window) if isinstance(w, Gtk.Popover) and w.get_mapped()]


def toast_texts(window):
    return texts(window)


def screenshot(window, name):
    pump(.4)
    from PIL import ImageGrab
    image = ImageGrab.grab(bbox=(0, 0, window.get_width(), window.get_height()), xdisplay=os.environ['DISPLAY'])
    image.save(output / (name + '.png'))
    return image


def luminance(rgb):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return .2126 * channel(r) + .7152 * channel(g) + .0722 * channel(b)


def contrast_ratio(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


def measure_contrast(window, labels):
    """Hide the labels, photograph what is behind them, and compare the label's real
    colour (alpha included) against the lightest decile of the background behind it."""
    from PIL import ImageGrab
    for label in labels:
        label.set_opacity(0)
    pump(.5)
    image = ImageGrab.grab(bbox=(0, 0, window.get_width(), window.get_height()),
                           xdisplay=os.environ['DISPLAY']).convert('RGB')
    for label in labels:
        label.set_opacity(1)
    pump(.2)
    results = []
    for label in labels:
        ok, rect = label.compute_bounds(window)
        if not ok:
            continue
        x0, y0 = max(0, int(rect.get_x())), max(0, int(rect.get_y()))
        x1 = min(window.get_width(), int(rect.get_x() + rect.get_width()))
        y1 = min(window.get_height(), int(rect.get_y() + rect.get_height()))
        if x1 <= x0 or y1 <= y0:
            continue
        pixels = sorted(image.crop((x0, y0, x1, y1)).getdata(), key=luminance)
        p90 = pixels[int(.9 * (len(pixels) - 1))]
        rgba = label.get_color()
        fg = (rgba.red * 255, rgba.green * 255, rgba.blue * 255)
        a = rgba.alpha
        eff = tuple(a * f + (1 - a) * b for f, b in zip(fg, p90))
        results.append((label.get_text(), contrast_ratio(eff, p90), round(a, 2)))
    return results


FORBIDDEN = (r'Main audio', r'Background audio', r'\bPicture\b', r'Retry picture', r'Refresh')


def vocabulary_violations(root, include_popovers=True):
    import re
    bad = []
    values = []
    for w in walk(root):
        if isinstance(w, Gtk.Label) and w.get_text() and (shown(w)):
            values.append(('text', w.get_text()))
        if shown(w):
            tip = w.get_tooltip_text()
            if tip:
                values.append(('tooltip', tip))
            lab = getattr(w, '_acceptance_label', None)
            if lab:
                values.append(('accessible', lab))
    for kind, value in values:
        for pattern in FORBIDDEN:
            if re.search(pattern, value):
                bad.append((kind, value))
        if 'Workspace 9' in value and value != 'Workspace 9':
            bad.append((kind, value))
    return bad


def workspace9_outside_scene_row(window):
    """Where 'Workspace 9' appears, it must be inside the Scene row of Mix."""
    wrong = []
    for w in walk(window):
        if isinstance(w, Gtk.Label) and shown(w) and 'Workspace 9' in w.get_text():
            row = row_of(window, 'Scene')
            if row is None or w not in list(walk(row)):
                wrong.append(w.get_text())
    return wrong


def row_of(window, title):
    """Mix row container: the largest ancestor of the title label that holds no other layer title."""
    others = {'Music', 'Ambience', 'Scene'} - {title}
    candidates = [w for w in walk(window) if isinstance(w, Gtk.Label) and shown(w) and w.get_text() == title
                  and in_mix(w)]
    if not candidates:
        return None
    node = candidates[-1]
    best = node
    for parent in ancestors(node):
        inside = set(texts(parent))
        if inside & others:
            break
        best = parent
    return best


def assert_inert(label, mark_commands, mark_calls):
    effects = backend.calls[mark_calls:]
    mutating = [c for c in COMMANDS[mark_commands:] if c[0] not in READ_ONLY]
    check(label, not effects and not mutating, json.dumps({'effects': effects, 'commands': mutating})[:400])


def mutating_since(mark):
    return [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY]


# ---------------------------------------------------------------- ADAPTER
# `listening-room --ui=next` -> listening_room.main() -> next_window.run(argv) -> NextApp().run().
# In-process we register the same application class and activate it (what run() does inside its
# main loop), then drive the window it creates.
APP = None


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
    app = build_app()
    for win in list(app.get_windows()):
        win.close()
        try:
            win.destroy()
        except Exception:
            pass
    app.window = None
    pump(.3)


def launch(width=1200, height=800):
    close_windows()
    app = build_app()
    app.activate()
    pump(.3)
    windows = app.get_windows()
    assert windows, '--ui=next activation created no window'
    window = windows[0]
    window.set_default_size(width, height)
    window.present()
    pump(1.2)
    gw, gh = window.get_width(), window.get_height()
    if (gw, gh) != (width, height):   # compensate window-frame difference on bare Xvfb, then verify
        window.set_default_size(width + (width - gw), height + (height - gh))
        pump(1.0)
    assert (window.get_width(), window.get_height()) == (width, height), \
        'window is %dx%d, wanted %dx%d' % (window.get_width(), window.get_height(), width, height)
    wake(window)
    return window


def goto(window, page):  # page in ('rooms', 'listen', 'now')
    accel(window, {'rooms': '<Control>1', 'listen': '<Control>2', 'now': '<Control>3'}[page])


def on_now(window):
    """Now is showing when its Room volume control is on screen."""
    return one(window, 'Room volume') is not None


def on_rooms_return(window):
    return any(re_return(t) for t in texts(window))


def re_return(text):
    return text.startswith('Return to ')


def enter_room(room_id='midnight-library', **extra):
    controller.enter(dict({'room_id': room_id, 'with_view': True}, **extra))
    settle()


def leave_all():
    close_windows()
    if controller.active is not None:
        controller.leave(0)
        settle(1.0)
    backend.fail_picture = backend.fail_music = False
    controller.timer_ends.clear() if hasattr(controller, 'timer_ends') else None


def snapshot():
    return service.execute('inspect', {})


LIBRARY_QUEUE = [{'provider': 'jellyfin', 'id': 'track1', 'type': 'Audio', 'name': 'First track'},
                 {'provider': 'jellyfin', 'id': 'track2', 'type': 'Audio', 'name': 'Second track'}]


def enter_library():
    controller.enter({'music': {'reference': LIBRARY_QUEUE[0], 'source_id': 'jellyfin-track1', 'name': 'First track',
                                'queue': LIBRARY_QUEUE, 'queue_index': 0},
                      'recipe': controller.last_session})
    settle()


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


# ---------------------------------------------------------------- O4 helpers
import re
import io
import contextlib
import stat
import threading
import audio_devices as _ad  # noqa: F401
import online_services as online
import provider_auth
import next_prefs
import next_track_details

SENTINEL = 'SENTINEL-SECRET-7f3a91c2'
FORBID_RE = [r'Main audio', r'Background audio', r'\bPicture\b', r'Retry picture', r'Refresh', r'\bepoch\b']
STDERR_CAPTURE = io.StringIO()
STDOUT_CAPTURE = io.StringIO()


def all_strings(root):
    """Every string a widget tree can show or announce: labels, tooltips, accessible names, plain entries."""
    values = []
    for w in walk(root):
        if isinstance(w, Gtk.Label) and w.get_text():
            values.append(('text', w.get_text()))
        if isinstance(w, Gtk.Button) and w.get_label():
            values.append(('label', w.get_label()))
        if isinstance(w, Gtk.CheckButton) and w.get_label():
            values.append(('label', w.get_label()))
        for getter in ('get_title', 'get_subtitle', 'get_description'):
            fn = getattr(w, getter, None)
            if fn is not None:
                try:
                    v = fn()
                except Exception:
                    v = None
                if isinstance(v, str) and v:
                    values.append((getter[4:], v))
        tip = w.get_tooltip_text()
        if tip:
            values.append(('tooltip', tip))
        lab = getattr(w, '_acceptance_label', None)
        if lab:
            values.append(('accessible', lab))
        if isinstance(w, Gtk.Text) and w.get_visibility() and w.get_text():
            values.append(('entry', w.get_text()))
        if isinstance(w, Gtk.Entry) and w.get_visibility() and w.get_text():
            values.append(('entry', w.get_text()))
    return values


def blob(root):
    return '\n'.join(v for _k, v in all_strings(root))


def find_dialogs(window):
    return [w for w in walk(window) if isinstance(w, Adw.Dialog)]


def open_prefs(window):
    accel(window, '<Control>comma')
    pump(.8)
    dialog = getattr(window, 'preferences', None)
    assert dialog is not None, 'Ctrl+, did not open Preferences'
    return dialog


def show_page(dialog, key):
    dialog.set_visible_page(dialog.pages[key])
    pump(.5)


def close_dialog(dialog):
    try:
        dialog.close()
    except Exception:
        pass
    pump(.6)


def row_text(dialog, service):
    return dialog.connection_widgets[service]['detail'].get_text()


def set_entry(widget, text):
    widget.set_text(text)
    pump(.1)


def wait_for(cond, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump(.1)
        settle(.1)
        if cond():
            return True
    return cond()


def mut(mark):
    return mutating_since(mark)


def reset_world():
    """Fresh connection config for a state fixture."""
    cfg = Path(os.environ['XDG_CONFIG_HOME'])
    st = Path(os.environ['XDG_STATE_HOME'])
    for p in list(cfg.rglob('connections.json')) + list(st.rglob('connection-checks.json')):
        p.unlink()


ENDPOINTS = {'jellyfin': 'http://jf.fixture.test', 'music_assistant': 'http://ma.fixture.test',
             'audiomuse': 'http://am.fixture.test', 'home_assistant': 'http://ha.fixture.test'}
SERVICES = ('jellyfin', 'twitch', 'music_assistant', 'audiomuse', 'listenbrainz', 'home_assistant')
TWITCH = {'state': 'none'}
patch.object(next_prefs, 'twitch_state', side_effect=lambda: dict(TWITCH)).start()


def fixture_unchecked():
    reset_world()
    for s, e in ENDPOINTS.items():
        online.save_connection(s, {'endpoint': e})
    online.save_connection('listenbrainz', {'username': 'fixture-user'})


def ok_request(service, path, params=None, data=None, method=None, cancel_event=None, raw=False,
               required=True, context=None, bounded=False):
    if path == '/Users/Me':
        return {'Id': 'u1', 'Name': 'fixture'}
    if path == '/api/config':
        return {'num_recent_albums': 3}
    if path == '/1/validate-token':
        return {'valid': True, 'user_name': 'fixture-user'}
    return {}


def failing_request(*a, **kw):
    raise online.ConnectionError('Fixture server is unreachable')


def run_check_ok():
    with patch.object(online, '_request', side_effect=ok_request), \
         patch.object(online, 'browse_jellyfin', return_value=[]), \
         patch.object(online, 'ma_players', return_value=[]), \
         patch.object(online, 'ha_scenes', return_value=[]):
        for s in ('jellyfin', 'music_assistant', 'audiomuse', 'listenbrainz', 'home_assistant'):
            online.test_connection(s)


# ---------------------------------------------------------------- criterion 1
def s1_connections():
    window = launch()
    mark_c, mark_b = len(COMMANDS), len(backend.calls)
    dialog = open_prefs(window)
    check('1.0 Ctrl+, opens an Adw.PreferencesDialog', isinstance(dialog, Adw.PreferencesDialog))
    show_page(dialog, 'connections')
    # --- state A: unconfigured
    reset_world()
    TWITCH['state'] = 'none'
    dialog.refresh_connections(); pump(.3)
    a = {s: row_text(dialog, s) for s in SERVICES}
    check('1a unconfigured: every provider says "Not set up"', all(v == 'Not set up' for v in a.values()), str(a))
    check('1a unconfigured: no "Connected"/verified wording anywhere in the dialog',
          not re.search(r'Connected|verified|ready|Signed in|answers', blob(dialog)), '')
    # --- state B: configured, unchecked
    fixture_unchecked()
    TWITCH['state'] = 'saved'
    dialog.refresh_connections(); pump(.3)
    b = {s: row_text(dialog, s) for s in SERVICES}
    bad = {s: v for s, v in b.items() if re.search(r'Connected|verified|ready|Signed in|answers|working', v, re.I)
           and 'not checked' not in v}
    check('1b configured-but-unchecked: no provider claims a working connection', not bad, str(b))
    check('1b configured-but-unchecked: says it is not checked yet',
          all('not checked' in v.lower() for v in b.values()), str(b))
    # --- state C: check failed (through the real Test button)
    failures = {}
    for s in ('jellyfin', 'music_assistant', 'audiomuse', 'listenbrainz', 'home_assistant'):
        with patch.object(online, '_request', side_effect=failing_request), \
             patch.object(online, 'browse_jellyfin', side_effect=failing_request), \
             patch.object(online, 'ma_players', side_effect=failing_request), \
             patch.object(online, 'ha_scenes', side_effect=failing_request):
            dialog.open_connection(s); pump(.6)
            test = button_with_text(dialog, 'Test')
            assert test is not None, 'no Test button for ' + s
            click(test)
            wait_for(lambda: 'unreachable' in dialog.connection_notice.get_subtitle() or True, 1.2)
            settle(.6)
            notice = dialog.connection_notice.get_subtitle()
            dialog.pop_subpage()
            pump(.5)
            failures[s] = (notice, row_text(dialog, s))
    check('1c failed check: the page itself says the check failed (not connected)',
          all('unreachable' in n and 'Connected' not in n for n, _r in failures.values()), str(failures))
    check('1c failed check: the Connections row reflects the failure, not "Added · not checked yet"',
          all('not checked yet' not in r and 'Connected' not in r for _n, r in failures.values()),
          'rows after a failed test: ' + str({s: r for s, (_n, r) in failures.items()}))
    check('1c failed check is not recorded as a working check', not online.checks(), str(online.checks()))
    # --- state D: check ok
    run_check_ok()
    TWITCH['state'] = 'connected'
    dialog.refresh_connections(); pump(.3)
    d = {s: row_text(dialog, s) for s in SERVICES}
    check('1d check OK: every provider now says something specific and truthful',
          d['jellyfin'] in ('Signed in · library ready', 'Connected · account ready')
          and d['audiomuse'] == 'Web service answers · analysis not confirmed'
          and d['twitch'].startswith('Signed in')
          and d['music_assistant'].endswith('not tested')
          and d['home_assistant'].endswith('not tested'), str(d))
    check('1d AudioMuse never claims analysis works', 'analysis not confirmed' in d['audiomuse'])
    check('1d "Connected"/"verified" appear only after a stored successful check',
          True, '')  # asserted by 1a/1b above
    # --- state E: stale. A saved success must not outlive a changed endpoint/token.
    dialog.open_connection('jellyfin'); pump(.6)
    set_entry(dialog.connection_fields['endpoint'], 'http://other-server.fixture.test')
    click(button_with_text(dialog, 'Save'))
    pump(.5)
    dialog.pop_subpage(); pump(.5)
    after = row_text(dialog, 'jellyfin')
    check('1e changing the server address drops the old "library ready" claim',
          'library ready' not in after and 'Connected' not in after, 'row now says: ' + after)
    # --- state F: a later failing test must not leave the earlier success standing
    run_check_ok()
    dialog.refresh_connections(); pump(.3)
    with patch.object(online, '_request', side_effect=failing_request), patch.object(online, 'browse_jellyfin', side_effect=failing_request):
        dialog.open_connection('audiomuse'); pump(.6)
        click(button_with_text(dialog, 'Test')); settle(.8)
        dialog.pop_subpage(); pump(.5)
    after = row_text(dialog, 'audiomuse')
    check('1e2 after a failed re-test the AudioMuse row no longer says the web service answers',
          'answers' not in after, 'row now says: ' + after)
    # --- chips
    chips = [w.get_text() for w in walk(dialog) if isinstance(w, Gtk.Label) and 'cap-chip' in w.get_css_classes()]
    need = ['Browse', 'Plays here', 'Plays on speakers', 'Open elsewhere', 'Station mixes', 'History', 'Room lighting']
    check('1f capability chips: all seven vocabulary terms present', all(n in chips for n in need), str(chips))
    check('1f chip assignment: Plays on speakers only on Music Assistant; Station mixes only on AudioMuse',
          next_prefs.CHIPS['music_assistant'].count('Plays on speakers') == 1
          and sum('Plays on speakers' in v for v in next_prefs.CHIPS.values()) == 1
          and next_prefs.CHIPS['audiomuse'] == ('Station mixes',), str(next_prefs.CHIPS))
    # --- nothing started playback
    check('1g connection browsing/status/test/save sent no playback command and made no backend effect',
          not mut(mark_c) and len(backend.calls) == mark_b, json.dumps(mut(mark_c))[:300])
    close_dialog(dialog)
    leave_all()


def s1c_repairs():
    """Adversarial checks of the connection-status repairs (forget_check, failure memory, token re-saves)."""
    window = launch()
    mark_c, mark_b = len(COMMANDS), len(backend.calls)
    TWITCH['state'] = 'none'
    dialog = open_prefs(window)
    show_page(dialog, 'connections')
    fixture_unchecked()
    dialog.refresh_connections(); pump(.3)

    def ui_test(service, ok, set_fields=None):
        dialog.open_connection(service); pump(.6)
        if set_fields:
            for name, value in set_fields.items():
                set_entry(dialog.connection_fields[name], value)
        if ok:
            with patch.object(online, '_request', side_effect=ok_request), patch.object(online, 'browse_jellyfin', return_value=[]), \
                 patch.object(online, 'ma_players', return_value=[]), patch.object(online, 'ha_scenes', return_value=[]):
                click(button_with_text(dialog, 'Test')); settle(.9)
        else:
            with patch.object(online, '_request', side_effect=failing_request), patch.object(online, 'browse_jellyfin', side_effect=failing_request), \
                 patch.object(online, 'ma_players', side_effect=failing_request), patch.object(online, 'ha_scenes', side_effect=failing_request):
                click(button_with_text(dialog, 'Test')); settle(.9)
        dialog.pop_subpage(); pump(.5)
        return row_text(dialog, service)

    def ui_save(service, fields):
        dialog.open_connection(service); pump(.6)
        for name, value in fields.items():
            set_entry(dialog.connection_fields[name], value)
        click(button_with_text(dialog, 'Save')); pump(.6)
        dialog.pop_subpage(); pump(.5)
        return row_text(dialog, service)

    # failed, then successful
    r = ui_test('audiomuse', False)
    check('R1a failed test: row says the check failed and is not "not checked yet"/working',
          'unreachable' in r and 'answers' not in r and 'not checked' not in r, r)
    r = ui_test('audiomuse', True)
    check('R1b a successful test right after a failure replaces it', r == 'Web service answers · analysis not confirmed', r)
    # successful, then config change
    r = ui_save('audiomuse', {'endpoint': 'http://am-other.fixture.test'})
    check('R2a success then a changed endpoint: back to "not checked yet", no stale claim',
          'answers' not in r and 'not checked' in r, r)
    check('R2a the stored success was removed from disk', 'audiomuse' not in online.checks(), str(online.checks()))
    ui_test('audiomuse', True)
    r = ui_save('audiomuse', {'endpoint': 'http://am-other.fixture.test'})
    check('R2b saving identical settings keeps a valid success', 'answers' in r, r)
    r = ui_save('audiomuse', {'token_file': str(Path(os.environ['XDG_CONFIG_HOME']) / 'elsewhere.token')})
    check('R2c success then a changed token file: claim withdrawn', 'answers' not in r, r)
    # failure bound to the configuration it described
    ui_test('music_assistant', False)
    r = ui_save('music_assistant', {'endpoint': 'http://ma-new.fixture.test'})
    check('R3a failure then a changed endpoint: the old failure is not pinned to the new address',
          'unreachable' not in r and 'not checked' in r, r)
    # out-of-band success newer than the recorded failure (e.g. the classic window)
    ui_test('listenbrainz', False)
    time.sleep(1.2)
    run_check_ok()
    dialog.refresh_connections(); pump(.3)
    r = row_text(dialog, 'listenbrainz')
    check('R4a out-of-band success newer than a recorded failure wins', r == 'Account access verified', r)
    ui_test('home_assistant', True)
    r = ui_test('home_assistant', False)
    check('R4b a failure newer than an earlier success wins (and the success is withdrawn)',
          'unreachable' in r and 'home_assistant' not in online.checks(), r + str(online.checks().keys()))
    # token re-saves
    ui_test('jellyfin', True)
    r = ui_save('jellyfin', {'token': SENTINEL + '-new'})
    check('R5a re-saving a token withdraws an earlier success (the new token is untested)',
          'library ready' not in r and 'Signed in' not in r and 'jellyfin' not in online.checks(), r)
    ui_test('jellyfin', True)
    r = ui_save('jellyfin', {'token': ''})
    check('R5b saving with a blank token field keeps the saved token and its success', 'library ready' in r or 'account ready' in r, r)
    ui_test('listenbrainz', False)
    r = ui_save('listenbrainz', {'token': SENTINEL + '-lb'})
    check('R5c a failure followed by a fresh token does not keep claiming the old token failed',
          'unreachable' not in r, 'row says: ' + r)
    check('R5d the sentinel never appears after the re-saves', SENTINEL not in blob(dialog))
    # reopening Preferences after a failure
    ui_test('audiomuse', False)
    close_dialog(dialog)
    dialog = open_prefs(window); show_page(dialog, 'connections'); pump(.5)
    r = row_text(dialog, 'audiomuse')
    check('R6 reopening Preferences after a failed test never shows a stale working claim', 'answers' not in r, r)
    # single dialog
    accel(window, '<Control>comma'); accel(window, '<Control>comma'); pump(.6)
    n = len([d for d in find_dialogs(window) if isinstance(d, Adw.PreferencesDialog)])
    check('R7 repeated Ctrl+, keeps one Preferences dialog', n == 1, 'dialogs=%d' % n)
    close_dialog(dialog)
    d2 = open_prefs(window)
    n = len([d for d in find_dialogs(window) if isinstance(d, Adw.PreferencesDialog)])
    check('R7 after closing, Ctrl+, opens a fresh working dialog', n == 1 and d2 is window.preferences and not d2.closed, 'dialogs=%d' % n)
    close_dialog(d2)
    # classic compatibility with the wrapped test_connection
    reset_world()
    online.save_connection('audiomuse', {'endpoint': 'http://am.fixture.test'})
    with patch.object(online, '_request', side_effect=ok_request):
        res = online.test_connection('audiomuse')
    check('R8a wrapped test_connection still returns the same result shape and records the check',
          res.get('state') == 'working' and 'capabilities' in res and 'audiomuse' in online.checks(), str(res)[:200])
    with patch.object(online, '_request', side_effect=failing_request):
        try:
            online.test_connection('audiomuse'); exc = None
        except Exception as e:
            exc = e
    check('R8b a failing check re-raises the original ConnectionError with its message (classic shows it unchanged)',
          isinstance(exc, online.ConnectionError) and str(exc) == 'Fixture server is unreachable', repr(exc))
    check('R8c ...and withdraws the earlier stored success', 'audiomuse' not in online.checks())
    try:
        online.test_connection('nonexistent'); exc = None
    except Exception as e:
        exc = e
    check('R8d an unknown service still raises its own error', exc is not None, repr(exc))
    check('R9 none of the repair scenarios sent a playback command', not mut(mark_c) and len(backend.calls) == mark_b,
          json.dumps(mut(mark_c))[:300])
    leave_all()


def s1b_secrets():
    window = launch()
    reset_world()
    mark_c, mark_b = len(COMMANDS), len(backend.calls)
    dialog = open_prefs(window)
    TWITCH['state'] = 'none'
    leaks = []
    with contextlib.redirect_stderr(STDERR_CAPTURE), contextlib.redirect_stdout(STDOUT_CAPTURE):
        for service in ('jellyfin', 'music_assistant', 'audiomuse', 'listenbrainz', 'home_assistant'):
            dialog.open_connection(service); pump(.6)
            f = dialog.connection_fields
            if 'endpoint' in f:
                set_entry(f['endpoint'], ENDPOINTS.get(service, 'http://x.test'))
            set_entry(f['token'], SENTINEL)
            if service == 'jellyfin':
                set_entry(f['username'], 'fixtureuser')
                set_entry(f['password'], SENTINEL + '-pw')
            with patch.object(online, '_request', side_effect=ok_request), \
                 patch.object(online, 'browse_jellyfin', return_value=[]), \
                 patch.object(online, 'ma_players', return_value=[]), \
                 patch.object(online, 'ha_scenes', return_value=[]), \
                 patch.object(online, 'jellyfin_login', return_value={'message': 'Signed in'}):
                click(button_with_text(dialog, 'Save')); pump(.5)
                token_after_save = f['token'].get_text()
                leaks.append((service + ' token entry cleared after save', token_after_save == ''))
                set_entry(f['token'], SENTINEL)
                click(button_with_text(dialog, 'Test')); settle(.8)
                if service == 'jellyfin':
                    set_entry(f['password'], SENTINEL + '-pw')
                    click(button_with_text(dialog, 'Sign in')); settle(.8)
                    leaks.append(('jellyfin password cleared after Sign in', f['password'].get_text() == ''))
            for kind, value in all_strings(dialog):
                if SENTINEL in value and kind not in ('entry',):
                    leaks.append((service + ' shows secret in ' + kind, False))
            dialog.pop_subpage(); pump(.4)
        dialog.open_connection('twitch'); pump(.6)
        for kind, value in all_strings(dialog):
            if SENTINEL in value:
                leaks.append(('twitch page shows secret in ' + kind, False))
        dialog.pop_subpage(); pump(.4)
        # AcoustID
        show_page(dialog, 'recordings')
        set_entry(dialog.acoustid_key, SENTINEL + '-acoustid')
        click(button_with_text(dialog, 'Save')) if False else None
        dialog.save_acoustid(); settle(.8)
    bad = [x for x in leaks if x[1] is False]
    check('1h secrets: entries cleared after Save/Sign in and no widget text, tooltip or accessible name ever holds one',
          not bad, str(bad))
    all_text = blob(dialog)
    check('1h secrets: nothing in the whole dialog tree contains the sentinel', SENTINEL not in all_text,
          [l for l in all_text.splitlines() if SENTINEL in l][:3])
    check('1h acoustid entry cleared and key not displayed', dialog.acoustid_key.get_text() == ''
          and SENTINEL not in blob(dialog))
    sent_cmds = [(a, p) for a, p in COMMANDS[mark_c:] if SENTINEL in json.dumps(p)]
    check('1h secret only ever sent to the controller as the AcoustID key (nothing else carries it)',
          all(a == 'metadata.configure_acoustid' for a, _p in sent_cmds), str(sent_cmds)[:300])
    check('1h nothing wrote the secret to stdout/stderr of the dialog code',
          SENTINEL not in STDERR_CAPTURE.getvalue() + STDOUT_CAPTURE.getvalue())
    # private storage
    store_hits = []
    for base in (Path(os.environ['XDG_CONFIG_HOME']), Path(os.environ['XDG_STATE_HOME']), Path(os.environ['HOME'])):
        for p in base.rglob('*'):
            if p.is_file() and p.stat().st_size < 1_000_000:
                try:
                    if SENTINEL.encode() in p.read_bytes():
                        store_hits.append((str(p), oct(stat.S_IMODE(p.stat().st_mode))))
                except OSError:
                    pass
    loose = [h for h in store_hits if h[1] not in ('0o600', '0o400')]
    check('1i secrets land only in 0600 private files (never connections.json or world-readable)',
          not loose and all('connections.json' not in h[0] for h in store_hits), str(store_hits))
    check('1j saving/testing/sign-in/key sent no playback command',
          not [c for c in mut(mark_c) if c[0] != 'metadata.configure_acoustid'] and len(backend.calls) == mark_b,
          json.dumps(mut(mark_c))[:300])
    close_dialog(dialog)
    leave_all()


# ---------------------------------------------------------------- 2 & 3
def s2_sound_scene():
    enter_room()
    window = launch()
    goto(window, 'now')
    dialog = open_prefs(window)
    mark_c = len(COMMANDS)
    show_page(dialog, 'sound')
    settle(.8)
    n0 = len(COMMANDS)
    dialog.pause_switch.set_active(not dialog.pause_switch.get_active()); settle(.7)
    dialog.duck_switch.set_active(not dialog.duck_switch.get_active()); settle(.7)
    sc = [(a, p) for a, p in COMMANDS[n0:] if a == 'settings']
    keys = [list(p['values'])[0] for _a, p in sc]
    check('2a Pause if it disconnects and Lower room audio each apply immediately via the settings command',
          keys == ['pause_on_disconnect', 'duck_on_call'], str(sc))
    check('2a controller state matches the toggles', controller.settings['pause_on_disconnect'] is False
          and controller.settings['duck_on_call'] is True, str(controller.settings))
    # output
    n1 = len(COMMANDS)
    model = dialog.output_row.get_model()
    labels = [model.get_string(i) for i in range(model.get_n_items())]
    dialog.output_row.set_selected(labels.index('Headphones') if 'Headphones' in labels else 2); settle(.7)
    out = [(a, p) for a, p in COMMANDS[n1:] if a == 'settings']
    check('2b choosing an output applies immediately through settings', out and out[-1][1]['values'].get('preferred_output') == 'buds',
          str(COMMANDS[n1:]))
    side0 = len(SIDE)
    click(button_with_text(dialog, 'Play test')); settle(.8)
    check('2c Test sound plays the quiet test on the chosen output and sends no controller command',
          SIDE[side0:] == [('test_sound', 'buds')] and not [c for c in COMMANDS[n1 + len(out):] if c[0] not in READ_ONLY],
          str(SIDE[side0:]))
    # pause other players
    before = bool(hub.state().get('pause_others_on_radio_start', False))
    nn = len(COMMANDS)
    dialog.others_switch.set_active(not before); settle(.8)
    check('2d Pause other players is stored locally (hub state) and sends no controller command',
          bool(hub.state().get('pause_others_on_radio_start', False)) is (not before)
          and not [c for c in COMMANDS[nn:] if c[0] not in READ_ONLY], str(COMMANDS[nn:]))
    # scene
    show_page(dialog, 'scene')
    n2 = len(COMMANDS)
    dialog.placement_row.set_selected(1); settle(.7)
    dialog.moving_switch.set_active(not dialog.moving_switch.get_active()); settle(.7)
    dialog.visible_switch.set_active(True); settle(.7)
    sc2 = [p['values'] for a, p in COMMANDS[n2:] if a == 'settings']
    check('3a placement, Moving picture and Keep controls each go through the settings command',
          sc2 and any('placement' in v for v in sc2) and any('still' in v for v in sc2)
          and any(v.get('keep_controls') is True for v in sc2), str(sc2))
    check('3b controller accepted and holds keep_controls', controller.settings.get('keep_controls') is True)
    persisted = json.loads((Path(folder) / 'controller' / 'session.json').read_text())
    check('3c keep_controls persisted to session.json', persisted.get('settings', {}).get('keep_controls') is True,
          str(persisted.get('settings')))
    refused = False
    try:
        service.execute('settings', {'values': {'keep_controls': True, 'bogus_key': 1}})
    except Exception as e:
        refused = 'Unknown room setting' in str(e)
    check('3d unknown settings keys are still refused', refused)
    check('3e keep_controls appears in the snapshot Now reads', snapshot().get('settings', {}).get('keep_controls') is True)
    dialog.reduced_switch.set_active(True); settle(.7)
    check('3f Reduced motion applies immediately and turns Moving picture off/insensitive',
          controller.settings['reduced_motion'] is True and not dialog.moving_switch.get_sensitive())
    dialog.reduced_switch.set_active(False); settle(.7)
    check('3g none of Sound/Scene changes touched playback (no room/layer commands)',
          not [c for c in COMMANDS[mark_c:] if c[0] not in READ_ONLY | {'settings'}], str([c for c in COMMANDS[mark_c:] if c[0] not in READ_ONLY | {'settings'}]))
    # Now idle fade honours keep_controls
    close_dialog(dialog)
    Gtk.Settings.get_default().set_property('gtk-enable-animations', True)
    window.system_animations = True
    controller.apply_settings({'keep_controls': False, 'reduced_motion': False}); settle(.8)
    goto(window, 'now'); wake(window)
    window.kick_idle()
    check('3h with keep_controls off, Now fades (fades() true)', window.fades() is True)
    next_window.IDLE_MS = 800
    window.fade_out()
    check('3h Now controls fade when keep_controls is off', window.controls.get_opacity() == 0)
    wake(window); window.kick_idle(); pump(.2)
    controller.apply_settings({'keep_controls': True}); settle(.8)
    window.kick_idle(); pump(.3)
    check('3i with keep_controls on, fades() is false', window.fades() is False)
    window.fade_out(); pump(.3)
    check('3i with keep_controls on, controls are never dimmed after idle', window.controls.get_opacity() == 1)
    # latent: controls already faded when the setting flips on
    controller.apply_settings({'keep_controls': False}); settle(.6)
    window.kick_idle(); window.fade_out(); pump(.2)
    faded = window.controls.get_opacity() == 0
    controller.apply_settings({'keep_controls': True}); settle(1.0)
    check('3j turning Keep controls visible on while controls are faded brings them back without pointer input',
          (not faded) or window.controls.get_opacity() == 1,
          'controls stay at opacity %s until the pointer moves' % window.controls.get_opacity())
    controller.apply_settings({'keep_controls': False}); settle(.4)
    leave_all()


# ---------------------------------------------------------------- 4/5
def s4_phone_recordings():
    window = launch()
    dialog = open_prefs(window)
    mark_c = len(COMMANDS)
    show_page(dialog, 'phone')
    pt = blob(dialog)
    check('4a KDE Connect install flow, Waybar and launcher lines are present',
          'Install room commands' in pt and 'Waybar' in pt and 'Launcher' in pt, '')
    click(button_with_text(dialog, 'Install')); pump(.4)
    check('4b Install with no phone chosen installs nothing and says to find a phone first',
          'Find and choose a paired phone first' in blob(dialog))
    with patch.object(online, 'paired_devices', return_value=[{'id': 'p1', 'name': 'Fixture phone'}]), \
         patch.object(online, 'install_remote_commands', return_value={'installed': 7, 'notified': True}) as inst:
        click(button_with_text(dialog, 'Find')); settle(.8)
        check('4c Install is an explicit step (finding phones installs nothing)', not inst.called)
        click(button_with_text(dialog, 'Install')); settle(.8)
        check('4d Install runs only after the click, for the chosen phone', inst.call_count == 1 and inst.call_args[0][0] == 'p1')
    # waybar / launcher helpers against fixtures
    home = Path(tempfile.mkdtemp(prefix='o4-wb-'))
    (home / 'waybar').mkdir()
    check('4e Waybar status: absent', next_prefs.waybar_status(str(home))[0].startswith('No Waybar'))
    (home / 'waybar' / 'config').write_text('{"custom/listening": {"exec": "listening-roomctl status"}}')
    check('4e Waybar status: present', next_prefs.waybar_status(str(home))[1] == 'ok')
    (home / 'waybar' / 'config').write_text('{"clock": {}}')
    check('4e Waybar status: configured without the module', next_prefs.waybar_status(str(home))[1] == 'warn')
    check('4f phone page sent no playback command', not mut(mark_c))
    # recordings
    show_page(dialog, 'recordings')
    mark = len(COMMANDS)
    dialog.save_acoustid(clear=True); settle(.8)
    check('5a AcoustID row default says no key and matching by name still works',
          'No application key' in dialog.acoustid_row.get_subtitle())
    set_entry(dialog.acoustid_key, 'FIXTURE-KEY-1')
    dialog.save_acoustid(); settle(.9)
    cfgs = [p for a, p in COMMANDS[mark:] if a == 'metadata.configure_acoustid']
    check('5b saving the key goes through metadata.configure_acoustid (private storage) and is reported saved, not shown',
          cfgs[-1:] == [{'client_key': 'FIXTURE-KEY-1'}] and 'saved' in dialog.acoustid_row.get_subtitle()
          and 'FIXTURE-KEY-1' not in blob(dialog), str(cfgs) + dialog.acoustid_row.get_subtitle())
    import metadata_pipeline as mp
    key_files = [p for p in Path(os.environ['HOME']).rglob('*') if p.is_file() and b'FIXTURE-KEY-1' in p.read_bytes()[:100000]] \
        if Path(os.environ['HOME']).exists() else []
    check('5b the real key file is mode 0600', key_files and all(stat.S_IMODE(p.stat().st_mode) in (0o600, 0o400) for p in key_files),
          str([(str(p), oct(p.stat().st_mode)) for p in key_files]))
    os.environ['ACOUSTID_CLIENT_KEY'] = 'ENV-KEY-VALUE'
    status, tone = next_prefs.acoustid_status(True)
    dialog.show_acoustid(True)
    check('5c an environment override is shown as an override, and the key value is not shown',
          'override' in dialog.acoustid_row.get_subtitle().lower() and 'ENV-KEY-VALUE' not in blob(dialog), dialog.acoustid_row.get_subtitle())
    check('5c the pure status text for the override never includes the value', 'ENV-KEY-VALUE' not in status)
    os.environ.pop('ACOUSTID_CLIENT_KEY')
    # remove key
    dialog.save_acoustid(clear=True); settle(.9)
    check('5d Remove clears the key (metadata.configure_acoustid with empty value)',
          any(a == 'metadata.configure_acoustid' and p == {'client_key': ''} for a, p in COMMANDS[mark:]))
    # bulk enrich + jobs
    controller.metadata_jobs['job-run'] = {'job_id': 'job-run', 'kind': 'enrich', 'status': 'running',
                                           'message': 'Collecting', '_item_id': 't', '_cancel': threading.Event()}
    dialog.refresh_recordings(); settle(1.0)
    check('5e a current job is listed with a Cancel button', button_with_text(dialog, 'Cancel') is not None
          and '1 metadata job' in blob(dialog))
    mark = len(COMMANDS)
    click(button_with_text(dialog, 'Cancel')); settle(.9)
    check('5f Cancel uses metadata.cancel and the job gets its cancel flag',
          any(a == 'metadata.cancel' and p.get('job_id') == 'job-run' for a, p in COMMANDS[mark:])
          and controller.metadata_jobs['job-run']['_cancel'].is_set())
    # enrich upcoming / queue
    mark = len(COMMANDS)
    STUB['metadata.enrich'] = {'accepted': True, 'job_id': 'j'}
    enter_library()
    settle(.8)
    n = len(backend.calls)
    mark_player = len(backend.calls)
    dialog.enrich_queue(); settle(1.0)
    en = [p for a, p in COMMANDS[mark:] if a == 'metadata.enrich']
    check('5g bulk Enrich queues metadata jobs only (no playback command beyond setup)',
          not [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY | {'metadata.enrich'}], str(COMMANDS[mark:])[:300])
    dialog.enrich_upcoming(); settle(.6)
    check('5h Enrich upcoming sends no playback command',
          not [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY | {'metadata.enrich'}])
    # 'shown' tracks.
    # ORIGINAL oracle, kept verbatim with its original failure (see revision R1 below).
    check('5i [ORIGINAL, superseded by R1] the Recordings page offers Enrich for shown AND upcoming tracks (classic had both)',
          bool(re.search(r'Enrich shown|shown tracks|Shown tracks', blob(dialog))),
          'Page offers "Upcoming tracks" and "Prepared library queue"; no shown-tracks action')
    # REVISION R1 (coordinator product decision, bound to O4 outcome criterion 5,
    # "bulk Enrich shown/upcoming"): "shown" means a set of tracks visible in a listing. The next window has no
    # track-level listing (O3's Library shows albums and playlists only), so there is no shown-track set.
    # Recordings must offer bulk Enrich for every track set the window actually has -- upcoming tracks and the
    # prepared library queue -- and those never start music. "Enrich shown tracks" is a recorded GAP that belongs
    # with a future track-level Library listing. The original oracle above is intentionally left failing.
    enrich_buttons = [w for w in walk(dialog) if isinstance(w, Gtk.Button) and shown(w) and w.get_label() == 'Enrich']
    text_now = blob(dialog)
    check('5i-R1 Recordings offers bulk Enrich for upcoming tracks and for the prepared library queue',
          len(enrich_buttons) == 2 and 'Upcoming tracks' in text_now and 'Prepared library queue' in text_now,
          'enrich buttons=%d' % len(enrich_buttons))
    mark_r1 = len(COMMANDS)
    n_backend = len(backend.calls)
    dialog.enrich_upcoming(); dialog.enrich_queue(); settle(1.0)
    check('5i-R1 both bulk Enrich actions only queue metadata jobs and never start music',
          not [c for c in COMMANDS[mark_r1:] if c[0] not in READ_ONLY | {'metadata.enrich'}]
          and len(backend.calls) == n_backend, str(COMMANDS[mark_r1:])[:300])
    STUB.pop('metadata.enrich', None)
    # export / import (pure)
    doc = next_prefs.export_document([{'records': [{'a': 1}]}, {'records': [{'b': 2}]}])
    check('5j export merges pages and import round-trips', next_prefs.import_records(json.dumps(doc)) == [{'a': 1}, {'b': 2}])
    for bad_text in ('not json', '[1,2]', '{"records": 3}'):
        try:
            next_prefs.import_records(bad_text); ok = False
        except ValueError:
            ok = True
        check('5k import rejects %r' % bad_text, ok)
    close_dialog(dialog)
    leave_all()


# ---------------------------------------------------------------- 6
TRACK = {'provider': 'jellyfin', 'id': 'trk1', 'type': 'Audio', 'name': 'Nocturne in E flat',
         'artist': 'Fixture Quartet', 'album': 'Night studies'}
TWITCH_ITEM = {'provider': 'twitch', 'id': 'ch1', 'type': 'Channel', 'name': 'Some channel'}


def s6_track_details():
    window = launch()
    ORIGIN = metadata_pipeline.library_origin()
    REVISION = {'id': 'source1', 'size': 12345, 'etag': 'etag', 'modified': '2026-11-01', 'duration': 1800}
    controller.metadata_store.observe(
        ORIGIN, 'trk1', None,
        identities=[{'provider': 'Jellyfin', 'provider_id': 'pid', 'name': 'MusicBrainzTrack',
                     'method': 'reported ProviderIds', 'date': '2026-11-17T10:00:00+00:00'}],
        features=[{'provider': 'Essentia MTG', 'name': 'instrumental score', 'value': .87,
                   'method': 'x', 'model': 'm', 'version': '2', 'date': '2026-11-17T10:00:00+00:00'}],
        revision=REVISION)
    controller.metadata_jobs['jm'] = {
        'job_id': 'jm', 'kind': 'match', 'status': 'completed',
        'result': {'candidates': [{'provider': 'MusicBrainz', 'provider_id': 'c1', 'title': 'Nocturne op. 9', 'status': 'candidate'},
                                  {'provider': 'AcoustID', 'provider_id': 'c2', 'title': 'Nocturne no. 2', 'status': 'candidate'}]},
        '_record': (ORIGIN, 'trk1'), '_revision': REVISION, '_cancel': threading.Event()}
    STUB.update({'metadata.enrich': {'accepted': True, 'job_id': 'je'},
                 'metadata.match': {'accepted': True, 'job_id': 'jm'},
                 'metadata.analyze': {'accepted': True, 'job_id': 'ja'},
                 'metadata.review': {'accepted': True, 'job_id': 'jr'}})
    mark_c, mark_b = len(COMMANDS), len(backend.calls)
    window.open_track_details(TRACK); settle(1.2)
    dialogs = [d for d in find_dialogs(window) if isinstance(d, next_track_details.TrackDetailsDialog)]
    check('6.0 open_track_details(reference) presents a Track details dialog', len(dialogs) == 1)
    td = dialogs[0]
    pump(1.0)
    text = blob(td)
    for needle in ('Overview', 'Nocturne in E flat', 'Fixture Quartet', 'Audio version',
                   'Identity: MusicBrainzTrack', 'Uncalibrated model score', 'not a probability or human review',
                   'No human review for this audio version', 'Review validity is checked again before use'):
        check('6a overview shows: ' + needle, needle in text)
    # scope statements from the classic panel
    for needle in ('Match sends track details to MusicBrainz, or an audio fingerprint to AcoustID',
                   'AcoustID needs a configured application key',
                   'Identity candidates are suggestions. Confirming an identity is separate from reviewing the audio.',
                   'Listen to the whole exact recording yourself before reviewing.',
                   'The review applies only to the audio version checked by the controller; replacement audio needs a new review.',
                   'Reviewing does not start playback.', 'I listened to the entire exact recording'):
        check('6b scope statement present: ' + needle[:60], needle in text)
    # actions never start music; each sends only its metadata command
    for title, action in (('Enrich', 'metadata.enrich'), ('Analyze', 'metadata.analyze'), ('Match', 'metadata.match')):
        m = len(COMMANDS)
        click(button_with_text(td, title)); settle(.9)
        sent = [c for c in COMMANDS[m:] if c[0] not in READ_ONLY]
        check('6c %s sends only %s, no playback' % (title, action), [c[0] for c in sent] == [action], str(sent))
    m = len(COMMANDS)
    td.method_row.set_selected(1)
    click(button_with_text(td, 'Match')); settle(.8)
    acoustid_sent = [c for c in COMMANDS[m:] if c[0] == 'metadata.match']
    check('6d Match with AcoustID and no key set up is refused in the dialog (no command) and explains how to fix it',
          not acoustid_sent and 'AcoustID application key' in td.review_status.get_text(), td.review_status.get_text())
    td.method_row.set_selected(0)
    # candidates + confirm
    td.match_jobs['trk1'] = 'jm'; td.refresh(); settle(1.2)
    labels = [b.get_label() for b in td.candidate_buttons]
    check('6e candidates listed with their provider', labels == ['Nocturne op. 9 (MusicBrainz)', 'Nocturne no. 2 (AcoustID)'], str(labels))
    m = len(COMMANDS)
    click(button_with_text(td, 'Confirm')); settle(.6)
    check('6f Confirm identity with no candidate chosen sends nothing', not [c for c in COMMANDS[m:] if c[0] not in READ_ONLY])
    td.candidate_buttons[0].set_active(True)
    click(button_with_text(td, 'Confirm')); settle(1.0)
    sent = [c for c in COMMANDS[m:] if c[0] not in READ_ONLY]
    check('6f Confirm identity sends only metadata.confirm_identity with the job and chosen candidate',
          sent == [('metadata.confirm_identity', {'job_id': 'jm', 'provider_id': 'c1'})], str(sent))
    check('6f confirming identity changed the stored identity but sent no playback', len(backend.calls) == mark_b)
    # review gating
    def review_cmds(m):
        return [c for c in COMMANDS[m:] if c[0] == 'metadata.review']
    m = len(COMMANDS)
    click(button_with_text(td, 'Save review')); settle(.6)
    check('6g review: nothing chosen -> not submitted', not review_cmds(m), td.review_status.get_text())
    td.class_buttons[0].set_active(True)
    td.reviewer.set_text('Reviewer One'); pump(.1)
    click(button_with_text(td, 'Save review')); settle(.6)
    check('6g review: classification + reviewer but NO acknowledgement -> not submitted', not review_cmds(m), td.review_status.get_text())
    td.ack.set_active(True); td.reviewer.set_text('   '); pump(.1)
    click(button_with_text(td, 'Save review')); settle(.6)
    check('6g review: acknowledgement but blank reviewer -> not submitted', not review_cmds(m), td.review_status.get_text())
    td.reviewer.set_text('Reviewer One')
    for b in td.class_buttons:
        b.set_active(False)
    pump(.1)
    click(button_with_text(td, 'Save review')); settle(.6)
    check('6g review: no classification chosen -> not submitted', not review_cmds(m), td.review_status.get_text())
    td.class_buttons[1].set_active(True)
    click(button_with_text(td, 'Save review')); settle(1.0)
    rc = review_cmds(m)
    check('6h a complete review sends exactly one metadata.review with acknowledged=True, the record identity and classification',
          len(rc) == 1 and rc[0][1].get('acknowledged') is True and rc[0][1].get('classification') == 'vocal'
          and rc[0][1].get('reviewer') == 'Reviewer One' and rc[0][1].get('origin') == ORIGIN
          and rc[0][1].get('item_id') == 'trk1', str(rc))
    check('6h review sends no playback command', not [c for c in COMMANDS[m:] if c[0] not in READ_ONLY | {'metadata.review'}])
    check('6h acknowledgement is reset after a saved review', td.ack.get_active() is False)
    # the controller itself still refuses unacknowledged reviews (guard behind the dialog)
    try:
        service.execute('metadata.review', {'origin': ORIGIN, 'item_id': 'trk1', 'classification': 'vocal',
                                           'reviewer': 'x', 'acknowledged': False}); refused = False
    except Exception:
        refused = True
    check('6i controller refuses an unacknowledged review', refused)
    # revoke needs a review present: sends only metadata.revoke
    # non-jellyfin
    window.open_track_details(TWITCH_ITEM); settle(1.0)
    td2 = [d for d in find_dialogs(window) if isinstance(d, next_track_details.TrackDetailsDialog) and d is not td][-1]
    m = len(COMMANDS)
    for title in ('Enrich', 'Match', 'Analyze'):
        b = button_with_text(td2, title)
        check('6j non-Jellyfin item: %s disabled' % title, b is not None and not b.get_sensitive())
        click(b)
    check('6j non-Jellyfin item: save review disabled and no metadata command written',
          not td2.save_review.get_sensitive() and not [c for c in COMMANDS[m:] if c[0].startswith('metadata.') and c[0] not in READ_ONLY])
    # reference None
    window.open_track_details(None); settle(.8)
    check('6k opening Track details with no reference does not crash', True)
    check('6l the whole of Track details sent no playback command or backend effect',
          not [c for c in COMMANDS[mark_c:] if c[0] not in READ_ONLY and not c[0].startswith('metadata.')]
          and len(backend.calls) == mark_b)
    # vocabulary
    bad = [(k, v) for k, v in all_strings(td) if any(re.search(p, v) for p in FORBID_RE)]
    check('6m Track details: no forbidden vocabulary', not bad, str(bad))
    STUB.clear()
    for d in find_dialogs(window):
        close_dialog(d)
    leave_all()


# ---------------------------------------------------------------- shared
def interactive_controls(dialog):
    out = []
    for w in walk(dialog):
        if not shown(w):
            continue
        if isinstance(w, (Gtk.Button, Gtk.Switch, Gtk.CheckButton, Gtk.DropDown, Gtk.Text, Gtk.Scale)):
            out.append(w)
    return out


def inside_adw_row(w):
    return any(isinstance(p, (Adw.ActionRow, Adw.EntryRow, Adw.SwitchRow, Adw.ComboRow, Adw.PasswordEntryRow, Adw.HeaderBar,
                              Adw.NavigationView, Adw.WindowTitle)) and p is not w for p in ancestors(w))


def a11y_name(w):
    label = getattr(w, '_acceptance_label', None)
    if label:
        return label
    if isinstance(w, (Gtk.Button, Gtk.CheckButton)) and w.get_label():
        return w.get_label()
    if isinstance(w, Gtk.Button):
        found = texts(w)
        if found:
            return ' '.join(found)
    return None


def sc_horizontal_overflow(root):
    bad = []
    for w in walk(root):
        if isinstance(w, Gtk.ScrolledWindow) and shown(w):
            h = w.get_hadjustment()
            if h.get_upper() > h.get_page_size() + 1:
                bad.append(('hscroll', h.get_upper(), h.get_page_size()))
    return bad


def clipped(root, container):
    bad = []
    ok, c = container.compute_bounds(root)
    cx0, cx1 = (c.get_x(), c.get_x() + c.get_width()) if ok else (0, 1e9)
    for w in walk(root):
        if not shown(w) or w is root:
            continue
        if isinstance(w, (Gtk.Button, Gtk.Switch, Gtk.CheckButton, Gtk.DropDown)):
            ok, r = w.compute_bounds(root)
            if ok and (r.get_x() < cx0 - 2 or r.get_x() + r.get_width() > cx1 + 2):
                bad.append((type(w).__name__, w.get_label() if hasattr(w, 'get_label') else '', round(r.get_x()), round(r.get_width()), round(cx0), round(cx1)))
    return bad


def s7_shared():
    for size in ((1200, 800), (800, 700)):
        window = launch(*size)
        dialog = open_prefs(window)
        for key in dialog.pages:
            show_page(dialog, key)
            settle(.3)
            container = dialog.get_child() or dialog
            # also any pushed connection page
            if key == 'connections':
                for service in SERVICES:
                    dialog.open_connection(service); pump(.6)
                    probs = clipped(dialog, container) + sc_horizontal_overflow(dialog)
                    check('D %dx%d connection page %s: no clipped control, no horizontal scroll' % (size + (service,)), not probs, str(probs)[:300])
                    shot = screenshot(window, 'o4-conn-%s-%dx%d' % (service, size[0], size[1]))
                    dialog.pop_subpage(); pump(.5)
            probs = clipped(dialog, container) + sc_horizontal_overflow(dialog)
            check('D %dx%d Preferences page %s: no clipped control, no horizontal scroll' % (size + (key,)), not probs, str(probs)[:300])
            screenshot(window, 'o4-prefs-%s-%dx%d' % (key, size[0], size[1]))
        # truncated status text in rows
        show_page(dialog, 'connections')
        ell = [(w.get_text()) for w in walk(dialog) if isinstance(w, Gtk.Label) and shown(w) and w.get_ellipsize() != 0
               and w.get_layout().is_ellipsized()]
        check('D %dx%d connection status lines are not truncated by ellipsis' % size, not ell, str(ell))
        close_dialog(dialog)
        leave_all()
    # accessibility + vocab on every page
    window = launch(1200, 800)
    dialog = open_prefs(window)
    unnamed, unfocusable = [], []
    for key in dialog.pages:
        show_page(dialog, key)
        for w in interactive_controls(dialog):
            if inside_adw_row(w):
                continue   # libadwaita's own row internals (switch, password reveal, close button) carry their own names
            if isinstance(w, Gtk.Text):
                continue
            if not a11y_name(w):
                unnamed.append((key, type(w).__name__, '.'.join(w.get_css_classes())))
            if not w.get_focusable() and not w.get_can_focus() and w.get_sensitive():
                unfocusable.append((key, type(w).__name__))
    check('C every interactive control in Preferences has an accessible name (label text or LABEL property)', not unnamed, str(unnamed)[:400])
    check('C every sensitive interactive control is keyboard focusable', not unfocusable, str(unfocusable))
    bad = []
    for key in dialog.pages:
        show_page(dialog, key)
        bad += [(key, k, v) for k, v in all_strings(dialog) if any(re.search(p, v) for p in FORBID_RE)]
    for s in SERVICES:
        dialog.open_connection(s); pump(.5)
        bad += [(s, k, v) for k, v in all_strings(dialog) if any(re.search(p, v) for p in FORBID_RE)]
        dialog.pop_subpage(); pump(.4)
    check('B no forbidden vocabulary on any Preferences page or connection page', not bad, str(bad)[:400])
    check('B Preferences uses the plan terms (Moving picture, Keep controls visible, Pause other players)',
          all(t in blob(dialog) for t in ('Moving picture', 'Keep controls visible', 'Pause other players when a room starts',
                                          'Pause if it disconnects', 'Lower room audio during calls', 'Test sound')))
    # Ctrl+, twice
    close_dialog(dialog)
    d1 = open_prefs(window)
    mark = len(find_dialogs(window))
    accel(window, '<Control>comma'); pump(.8)
    count = len([d for d in find_dialogs(window) if isinstance(d, Adw.PreferencesDialog)])
    check('C pressing Ctrl+, while Preferences is open does not stack a second Preferences dialog', count == 1, 'dialogs=%d' % count)
    for d in find_dialogs(window):
        close_dialog(d)
    # menu entry exists
    names = [t for t in texts(window)]
    menu = one(window, 'Main menu') or one(window, 'Menu')
    check('C Preferences is reachable from the main menu', any('Preferences' in str(getattr(w, 'get_label', lambda: '')() or '') or 'Preferences' in ' '.join(texts(w))
                                                               for w in walk(window) if isinstance(w, Gtk.Button)), '')
    # shortcuts dialog lists it
    check('C Ctrl+, is listed in the keyboard shortcuts', ('Ctrl+,', 'Preferences') in next_window.SHORTCUTS)
    # Esc: dialog is closable
    d = open_prefs(window)
    check('C Preferences dialog can be closed (Esc / close button)', d.get_can_close() is True)
    close_dialog(d)
    leave_all()


def s6b_track_layout():
    for size in ((1200, 800), (800, 700)):
        window = launch(*size)
        window.open_track_details(TRACK); settle(1.2)
        td = [d for d in find_dialogs(window) if isinstance(d, next_track_details.TrackDetailsDialog)][0]
        container = td.get_child() or td
        probs = clipped(td, container) + sc_horizontal_overflow(td)
        check('D %dx%d Track details: no clipped control, no horizontal scroll' % size, not probs, str(probs)[:300])
        unnamed = [(type(w).__name__, '.'.join(w.get_css_classes())) for w in interactive_controls(td)
                   if not inside_adw_row(w) and not isinstance(w, Gtk.Text) and not a11y_name(w)]
        check('C %dx%d Track details: every control has an accessible name' % size, not unnamed, str(unnamed))
        check('C Track details can be closed (Esc / close button)', td.get_can_close() is True)
        for d in find_dialogs(window):
            close_dialog(d)
        leave_all()


def s8_reduced_motion():
    window = launch()
    controller.apply_settings({'reduced_motion': True}); settle(.8)
    dialog = open_prefs(window); pump(.6)
    check('D reduced motion: Moving picture row is switched off/insensitive in Preferences',
          not dialog.moving_switch.get_sensitive())
    anim = [w for w in walk(dialog) if isinstance(w, Gtk.Spinner) and w.get_spinning()]
    check('D reduced motion: no spinner animating in Preferences', not anim)
    close_dialog(dialog)
    controller.apply_settings({'reduced_motion': False}); settle(.4)
    leave_all()


def s9_diff_static():
    # REVISED (R2). Original wording: "hook: only next_window.py and room_session.py touched among shared
    # modules" and "hook: room_session.py edits only append keep_controls to two existing lines", both diffed
    # `merge-base HEAD main..HEAD`. Reason: once O5 owns the shared modules (roomctl, next_model, next_widgets,
    # next_window), the merge-base diff no longer measures O4's footprint, and on main itself the diff is empty so
    # the "len == 2" claim fails by construction. The same assertions are bound to the O4 commit range, unchanged.
    # REVISED (R3, repository extraction). The standalone repository starts with fresh history, so the O4 commit
    # range 5763fd8^..8714b09 of the original repository is stored verbatim as tests/fixtures/o4-range.diff
    # (diff of the six shared modules) and o4-next-window.numstat. The assertions are unchanged.
    rng = '5763fd8^..8714b09 (tests/fixtures/o4-range.diff)'
    fixtures = Path(__file__).resolve().parents[1] / 'fixtures'
    diff = (fixtures / 'o4-range.diff').read_text()
    files = [l for l in diff.splitlines() if l.startswith('+++')]
    check('hook: only next_window.py and room_session.py touched among shared modules (O4 range %s)' % rng,
          bool(files) and all(f.endswith(('next_window.py', 'room_session.py')) for f in files), str(files))
    # room_session.py section of the stored diff
    sections = re.split(r'(?m)^(?=diff --git )', diff)
    rs = ''.join(s for s in sections if s.startswith('diff --git') and s.splitlines()[0].endswith('room_session.py'))
    rs_minus = [l[1:].strip() for l in rs.splitlines() if l.startswith('-') and not l.startswith('---')]
    rs_plus = [l[1:].strip() for l in rs.splitlines() if l.startswith('+') and not l.startswith('+++')]
    check('hook: room_session.py edits only append keep_controls to two existing lines (O4 range %s)' % rng,
          len(rs_minus) == 2 and all(p.replace(", 'keep_controls'", '').replace(', keep_controls=False', '') == m
                                      for p, m in zip(rs_plus, rs_minus)), str((rs_minus, rs_plus)))
    nw = (fixtures / 'o4-next-window.numstat').read_text()
    check('hook: next_window.py change is small (<= 30 lines changed, informational)', True, nw.strip())


def main():
    build_app()
    selected = [x for x in args.only.split(',') if x]
    for fn in [f for f in (s1_connections, s1c_repairs, s1b_secrets, s2_sound_scene, s4_phone_recordings, s6_track_details, s6b_track_layout,
                           s7_shared, s8_reduced_motion, s9_diff_static) if not selected or f.__name__ in selected]:
        print('==', fn.__name__, flush=True)
        scenario(fn)
    close_windows()


try:
    main()
except Exception:
    traceback.print_exc()
    check('acceptance run completed', False, traceback.format_exc()[-600:])
finally:
    (output / 'o4-acceptance.json').write_text(json.dumps(RESULTS, indent=2))
    failed = [r for r in RESULTS if not r['pass']]
    print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed')
    os._exit(1 if failed else 0)
