"""Independent acceptance checks for Listening Room outcome O1 (new window).

Owned by the acceptance author, not the implementer. Oracles are behavioural
and structure-agnostic: the controller command log and FakeBackend effects,
visible widget-tree text, accessible names captured at the GTK boundary,
allocation bounds and screenshot contrast. The only implementation-specific
part is the ADAPTER section, which constructs the new window.

Run inside `scripts/listening-dev.sh shell` under private Xvfb/D-Bus:
  env -u WAYLAND_DISPLAY GDK_BACKEND=x11 GDK_DEBUG=no-portals \
    xvfb-run -a -s '-screen 0 1440x1000x24' dbus-run-session \
    python3 o1_acceptance.py --repo WORKTREE --output OUTDIR
"""
import contextlib
import io
import argparse
import copy
import json
import os
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


folder = tempfile.mkdtemp(prefix='o1-acceptance-')
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
        self.fail_atmosphere = False   # R4: lets 6h exercise an ambience layer that failed

    def start_atmosphere(self, selection, cancel):
        if self.fail_atmosphere and selection.get('kind') != 'none':
            raise ValueError('Sound could not load')
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


def request(action, args=None, *a, **kw):
    COMMANDS.append((action, copy.deepcopy(args or {})))
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
    backend.fail_picture = backend.fail_music = backend.fail_atmosphere = False
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


# ---------------------------------------------------------------- scenarios
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


def s1_launch():
    import types
    # Dispatch. REVISED for O5 twice. O1 wrote 1a as `--ui=next` -> new window, default -> classic. The switch
    # commit made the default the new window (the plan's Delivery section, once parity held). The removal commit
    # deleted the classic window: the default, `--ui=next` and `--mini` all reach next_window.run with their
    # arguments, and `--ui=classic` is refused with a message and a nonzero exit instead of opening anything.
    calls = []
    real_run = next_window.run
    next_window.run = lambda argv: calls.append(('next', list(argv))) or 11
    try:
        saved = sys.argv
        sys.argv = ['listening-room', '--ui=next']
        rc_next = listening_room.main()
        sys.argv = ['listening-room']
        rc_default = listening_room.main()
        sys.argv = ['listening-room', '--mini']
        rc_mini = listening_room.main()
        sys.argv = ['listening-room', '--ui=classic']
        with contextlib.redirect_stderr(io.StringIO()) as refusal:
            rc_classic = listening_room.main()
        sys.argv = saved
    finally:
        next_window.run = real_run
    check('1a launch dispatch: --ui=next, the default and --mini -> new window with their arguments; --ui=classic is refused',
          (rc_next, rc_default, rc_mini) == (11, 11, 11) and rc_classic == 2 and 'classic' in refusal.getvalue()
          and [c[0] for c in calls] == ['next', 'next', 'next']
          and calls[2][1] == ['listening-room', '--mini'], repr((calls, rc_classic)))

    # A last room exists, none active: Rooms with a single primary Return action; no autoplay.
    enter_room('midnight-library')
    controller.leave(0)
    settle(1.0)
    assert controller.active is None and controller.last_session and controller.last_session.get('room_id') == 'midnight-library'
    backend.calls.clear()
    mark = len(COMMANDS)
    window = launch()
    pump(1.5)
    hero_texts = [t for t in texts(window) if re_return(t)]
    suggested = [w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and w.has_css_class('suggested-action')]
    suggested_names = [' '.join(texts(w)) for w in suggested]
    check('1b launch with last room only: opens Rooms, single primary "Return to Midnight library"',
          hero_texts and not on_now(window) and len(suggested) == 1 and suggested_names == ['Return to Midnight library'],
          'return texts=%r primary=%r now_visible=%s' % (hero_texts, suggested_names, on_now(window)))
    assert_inert('1c launch never autoplays (no command, no backend effect)', mark, 0)
    check('1c launch leaves no room active', controller.active is None, str(controller.active and controller.active.get('room_id')))
    screenshot(window, 'a01-rooms-return-hero')

    # Any catalogue room can be entered from Rooms.
    names = [r['name'] for r in room_catalogue.rooms()]
    present = {n: any(isinstance(w, Gtk.Label) and w.get_text() == n for w in walk(window)) for n in names}
    check('1d Rooms lists every catalogue room', all(present.values()), json.dumps({k: v for k, v in present.items() if not v}))

    # Enter a different room by activating its card.
    entered = None
    target = 'Night drive'
    label = next((w for w in walk(window) if isinstance(w, Gtk.Label) and w.get_text() == target), None)
    mark = len(COMMANDS)
    if label is not None:
        node = label
        for parent in ancestors(label):
            if isinstance(parent, Gtk.FlowBoxChild):
                parent.get_parent().emit('child-activated', parent)
                entered = 'flowbox'
                break
            if isinstance(parent, Gtk.ListBoxRow):
                parent.get_parent().emit('row-activated', parent)
                entered = 'listbox'
                break
            if isinstance(parent, Gtk.Button):
                click(parent)
                entered = 'button'
                break
    settle(1.5)
    sent = [c for c in mutating_since(mark)]
    check('1e a catalogue room card enters that room (enter command, then Now)',
          entered and sent and sent[0][0] == 'enter' and sent[0][1].get('room_id') == 'night-drive'
          and controller.active and controller.active.get('room_id') == 'night-drive' and on_now(window),
          'via=%s commands=%r active=%s now=%s' % (entered, sent, controller.active and controller.active.get('room_id'), on_now(window)))
    leave_all()

    # A room is active: opens in Now, still without sending commands.
    enter_room('midnight-library')
    mark = len(COMMANDS)
    window = launch()
    pump(1.2)
    check('1f launch while a room is active opens Now', on_now(window) and not on_rooms_return(window), '')
    check('1g launch into active room sends no mutating command', not mutating_since(mark), repr(mutating_since(mark)))
    leave_all()


def s1b_return_hero_click():
    enter_room('midnight-library')
    controller.leave(0)
    settle(1.0)
    backend.calls.clear()
    window = launch()
    mark = len(COMMANDS)
    hero = button_with_text(window, 'Return to Midnight library')
    assert hero is not None, 'no Return hero button'
    click(hero)
    settle(1.5)
    cmds = mutating_since(mark)
    check('1h Return hero starts exactly the existing return, then Now',
          [c[0] for c in cmds] == ['return'] and on_now(window) and controller.active
          and controller.active.get('room_id') == 'midnight-library', repr(cmds))
    leave_all()


def s2_inert_browsing():
    enter_room('midnight-library')
    window = launch()
    settle(.5)
    mark_c, mark_b = len(COMMANDS), len(backend.calls)
    # Now: open/close every popover and the menu.
    for name in ('Timer', 'Output', 'Main menu'):
        button, _ = open_popover(window, name)
        close_popover(button)
    # Mix open and closed, via the control and Esc.
    open_mix(window)
    for name in ('Timer', 'Output', 'Main menu'):
        pass
    key(window, Gdk.KEY_Escape)
    pump(.8)
    # Browse: collapse Now, switch pages by shortcut, open popovers on browse pages.
    click(one(window, 'Back to rooms'))
    pump(.6)
    for page in ('listen', 'rooms', 'listen', 'rooms'):
        goto(window, page)
    for name in ('Timer', 'Output', 'Main menu'):
        button, _ = open_popover(window, name)
        close_popover(button)
    for w in walk(window):
        if isinstance(w, Gtk.FlowBox) and shown(w) and w.get_child_at_index(1) is not None:
            w.select_child(w.get_child_at_index(1))
    pump(.4)
    goto(window, 'now')
    pump(.6)
    assert_inert('2a browsing, Mix, popovers, menu and collapsing Now send no playback command', mark_c, mark_b)
    # Cold launch with nothing active, then browse everything.
    leave_all()
    enter_room('midnight-library')
    controller.leave(0)
    settle(1.0)
    window = launch()
    mark_c, mark_b = len(COMMANDS), len(backend.calls)
    for page in ('listen', 'rooms'):
        goto(window, page)
    goto(window, 'now')
    for name in ('Timer', 'Output', 'Main menu'):
        button, _ = open_popover(window, name)
        close_popover(button)
    pump(.5)
    assert_inert('2b browsing with no active room (Rooms/Listen/Now shortcut, popovers) sends nothing', mark_c, mark_b)
    check('2c browsing with no active room leaves it idle', controller.active is None, '')
    leave_all()


def now_inventory(window):
    result = {}
    for w in controls(window):
        result.setdefault(acc_name(w) or '<unnamed %s>' % describe(w), []).append(w)
    return result


def s3_now_at_rest():
    # live radio
    enter_room('midnight-library')
    window = launch()
    wake(window)
    inv = now_inventory(window)
    expected = {'Back to rooms', 'Output', 'Timer', 'Main menu', 'Mix', 'Pause the room', 'Room volume'}
    extra = sorted(set(inv) - expected)
    missing = sorted(expected - set(inv))
    check('3a Now (live radio) offers exactly the allowed controls', not extra and not missing,
          'extra=%r missing=%r all=%r' % (extra, missing, sorted(inv)))
    next_widgets = [w for w in walk(window) if acc_name(w) == 'Next track' and not in_popover(w)
                    and not in_mix(w)]
    shown_next = [w for w in next_widgets if shown(w)]
    check('3b Next track is hidden (not merely disabled) on live radio', not shown_next,
          'widgets=%d shown=%d' % (len(next_widgets), len(shown_next)))
    chip_values = {'Live radio', 'Library', 'Live channel', 'Local sound'}
    body = [t for t in texts(window)]
    chips = [t for t in body if t in chip_values or t.lower() in {c.lower() for c in chip_values}]
    check('3c source chip present (Live radio)', any(c.lower() == 'live radio' for c in chips), repr(body))
    title = controller.active['name']
    title_labels = [w for w in walk(window) if isinstance(w, Gtk.Label) and shown(w) and w.get_text() == title]
    serif = []
    for label in title_labels:
        family = label.get_pango_context().get_font_description().get_family() or ''
        size = label.get_pango_context().get_font_description().get_size() / 1024
        serif.append((family, size))
    check('3d room name shown in a serif face', bool(serif) and any('serif' in f.lower() or 'georgia' in f.lower()
                                                                    or 'newsreader' in f.lower() or 'times' in f.lower()
                                                                    for f, _ in serif), repr(serif) + ' (CSS-resolved family list)')
    mix = one(window, 'Mix')
    summary = [t for t in texts(mix) if t != 'Mix'] if mix is not None else []
    check('3e Mix control carries a layer summary', bool(summary), repr(texts(mix) if mix is not None else None))
    now_line = backend.state['music'].get('name')
    check('3f now-playing line visible', bool(now_line) and any(now_line in t for t in body), 'expected %r in %r' % (now_line, body))
    check('3g Now is full-bleed art: nothing from Mix shown at rest', not any(t in ('Save as my room…', 'Moving picture', 'Window')
                                                                              for t in body), repr(body))
    screenshot(window, 'a02-now-live-radio')
    leave_all()
    # library queue that can skip
    enter_room('midnight-library')
    enter_library()
    window = launch()
    wake(window)
    inv = now_inventory(window)
    expected = {'Back to rooms', 'Output', 'Timer', 'Main menu', 'Mix', 'Pause the room', 'Room volume', 'Next track'}
    check('3h Now (library queue) offers the allowed controls including Next track',
          set(inv) == expected, 'extra=%r missing=%r' % (sorted(set(inv) - expected), sorted(expected - set(inv))))
    nxt = (inv.get('Next track') or [None])[0]
    check('3i Next track is sensitive on a skippable library queue', nxt is not None and nxt.get_sensitive(), '')
    check('3j source chip says Library', 'library' in {t.lower() for t in texts(window)}, repr(texts(window)))
    screenshot(window, 'a03-now-library-next-track')
    mark = len(COMMANDS)
    if nxt is not None:
        click(nxt)
        settle(1.0)
    cmds = mutating_since(mark)
    check('3k Next track sends one skip command (layer next)', len(cmds) == 1 and cmds[0][0] in ('layer', 'next')
          and (cmds[0][1].get('action') == 'next' or cmds[0][0] == 'next'), repr(cmds))
    leave_all()


def s4_vocabulary():
    enter_room('midnight-library')
    # give every layer something to say
    controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass',
                      'name': 'Rain on glass', 'outdoor': True, 'volume': 20})
    settle()
    backend.fail_picture = True
    window = launch()
    wake(window)
    bad = vocabulary_violations(window)
    check('4a Now text, tooltips and accessible names: no forbidden vocabulary', not bad, repr(bad))
    for name in ('Timer', 'Output', 'Main menu'):
        button, popover = open_popover(window, name)
        bad = vocabulary_violations(popover)
        check('4b %s popover: no forbidden vocabulary' % name, not bad, repr(bad))
        close_popover(button)
    open_mix(window)
    bad = vocabulary_violations(window)
    check('4c Mix open: no forbidden vocabulary', not bad, repr(bad))
    titles = set(texts(window))
    check('4d Mix and Now use Music / Ambience / Scene', {'Music', 'Ambience', 'Scene'} <= titles, repr(sorted(titles)))
    wrong = workspace9_outside_scene_row(window)
    check('4e "Workspace 9" appears only as a Scene placement value', not wrong, repr(wrong))
    # Row menus too.
    for w in list(walk(window)):
        if isinstance(w, Gtk.MenuButton) and shown(w) and in_mix(w):
            popover = w.get_popover()
            if popover is None:
                continue
            w.popup()
            pump(.5)
            bad = vocabulary_violations(popover)
            # R4 (2026-11-18): the Ambience row's change path is a plain "Change…" button (O6 criterion 2), so one
            # generated 4f instance (the Ambience Change menu) no longer exists. Every remaining Mix popover still
            # gets this instance, now also required to be non-empty so an empty menu cannot pass vacuously.
            check('4f Mix row menu %r: no forbidden vocabulary' % (acc_name(w) or w.get_label() or '?'),
                  not bad and any(isinstance(b, Gtk.Button) for b in walk(popover)), repr(bad))
            w.popdown()
            pump(.2)
    close_mix(window)
    leave_all()


def s5_pause_leave():
    enter_room('midnight-library')
    window = launch()
    mark = len(COMMANDS)
    pause = one(window, 'Pause the room')
    assert pause is not None, 'no "Pause the room" control while playing'
    click(pause)
    settle(1.0)
    cmds = mutating_since(mark)
    check('5a Pause sends the existing quiet (no minutes), nothing else',
          len(cmds) == 1 and cmds[0][0] == 'quiet' and not cmds[0][1].get('minutes'), repr(cmds))
    check('5b Pause quiets the layers (music Paused, not stopped)', backend.state['music']['status'] == 'Paused', backend.state['music']['status'])
    play = one(window, 'Play the room')
    check('5c the control becomes "Play the room"', play is not None and one(window, 'Pause the room') is None, '')
    mark = len(COMMANDS)
    click(play)
    settle(1.0)
    cmds = mutating_since(mark)
    check('5d Play sends the existing resume and playback resumes',
          len(cmds) == 1 and cmds[0][0] == 'resume' and backend.state['music']['status'] == 'Playing', repr(cmds))
    check('5e the control returns to "Pause the room"', one(window, 'Pause the room') is not None, '')
    # Leave room in the main menu.
    button, popover = open_popover(window, 'Main menu')
    leave = next((w for w in walk(popover) if isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)) == 'Leave room'), None)
    check('5f Main menu offers "Leave room"', leave is not None, repr(texts(popover)))
    mark = len(COMMANDS)
    if leave is not None:
        click(leave)
        pump(.3)
        cmds = mutating_since(mark)
        check('5g Leave room sends the existing leave (fade-and-stop), not a bare stop',
              bool(cmds) and cmds[0][0] == 'leave', repr(cmds))
        settle(3.0)
        check('5h room is stopped after the leave fade and the window returns to browsing',
              controller.active is None and not on_now(window), 'active=%s now=%s' % (bool(controller.active), on_now(window)))
    leave_all()


def s6_mix():
    enter_room('midnight-library')
    outdoor = next(x for x in atmosphere_catalogue.SOUNDS if x['outdoor'])
    indoor = next(x for x in atmosphere_catalogue.SOUNDS if not x['outdoor'])
    controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': outdoor['id'],
                      'name': outdoor['name'], 'outdoor': True, 'volume': 20})
    settle()
    window = launch()
    mark_c, mark_b = len(COMMANDS), len(backend.calls)
    open_mix(window)
    screenshot(window, 'a04-mix-open')
    rows = {t: row_of(window, t) for t in ('Music', 'Ambience', 'Scene')}
    check('6a Mix has rows Music, Ambience, Scene', all(rows.values()), repr({k: v is not None for k, v in rows.items()}))
    if not all(rows.values()):
        return
    info = {t: texts(r) for t, r in rows.items()}
    check('6b each row names its source and a state',
          any(backend.state['music']['name'] in x for x in info['Music'])
          and any(outdoor['name'] in x for x in info['Ambience'])
          and all(any(s in ' '.join(v).lower() for s in ('playing', 'paused', 'stopped', 'closed', 'off', 'on'))
                  for v in info.values()), json.dumps(info, ensure_ascii=False))

    def scales(r):
        return [w for w in walk(r) if isinstance(w, Gtk.Scale) and shown(w)]

    def mutes(r):
        return [w for w in walk(r) if interactive_type(w) and shown(w) and 'mute' in (acc_name(w) or '').lower()]
    check('6c Music has level and mute', len(scales(rows['Music'])) == 1 and len(mutes(rows['Music'])) >= 1,
          'scales=%d mutes=%r' % (len(scales(rows['Music'])), [acc_name(m) for m in mutes(rows['Music'])]))
    # R4 (2026-11-18, operator feedback + O6 criterion 2): the Ambience row also has a primary, plain "Change…"
    # button (not a menu) that opens Listen in ambience mode; browsing it sends nothing, and Cancel returns to the Mix.
    # ORIGINAL 6d: len(scales)==2 and mutes and 'Window' in info['Ambience'] (kept below).
    change = [w for w in walk(rows['Ambience']) if isinstance(w, Gtk.Button) and not isinstance(w, Gtk.MenuButton)
              and shown(w) and ' '.join(texts(w)) == 'Change…']
    ambience_path = False
    ambience_detail = 'no plain Change… button on the Ambience row'
    if change:
        mark_nav = len(COMMANDS)
        click(change[0])
        pump(1.0)
        opened = 'Choose an ambience' in ' '.join(texts(window)) and any(
            isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)) == 'Play as ambience' for w in walk(window))
        cancel = next((w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)) == 'Cancel'), None)
        if cancel is not None:
            click(cancel)
            pump(1.0)
        ambience_path = opened and not mutating_since(mark_nav) and mix_open(window) and cancel is not None
        ambience_detail = 'opened=%s cancel=%s commands=%r mix=%s' % (opened, cancel is not None, mutating_since(mark_nav), mix_open(window))
    check('6d Ambience (outdoor sound) has level, mute and Window, and a plain Change… that opens Listen in ambience mode',
          len(scales(rows['Ambience'])) == 2 and mutes(rows['Ambience']) and 'Window' in info['Ambience'] and ambience_path,
          'scales=%d texts=%r path=%s' % (len(scales(rows['Ambience'])), info['Ambience'], ambience_detail))
    check('6e Music row has no Window control', 'Window' not in info['Music'], '')
    check('6f Scene has placement (Workspace 9 / Here), Moving picture, a close control and no level/mute',
          'Workspace 9' in info['Scene'] and 'Moving picture' in info['Scene']
          and not scales(rows['Scene']) and not mutes(rows['Scene'])
          and any('close' in (acc_name(w) or '').lower() for w in walk(rows['Scene']) if interactive_type(w) and shown(w)),
          json.dumps(info['Scene'], ensure_ascii=False))

    # Row menus.
    def menu_items(row):
        found = {}
        for w in walk(row):
            if isinstance(w, Gtk.MenuButton) and shown(w) and 'options' in (acc_name(w) or '').lower() + (w.get_tooltip_text() or '').lower() \
                    or (isinstance(w, Gtk.MenuButton) and shown(w) and w.get_icon_name() == 'view-more-symbolic'):
                w.popup()
                pump(.5)
                found[acc_name(w)] = [(' '.join(texts(b)), b.get_sensitive()) for b in walk(w.get_popover())
                                      if isinstance(b, Gtk.Button) and shown(b)]
                w.popdown()
                pump(.2)
        return found
    music_menu = menu_items(rows['Music'])
    amb_menu = menu_items(rows['Ambience'])
    scene_menu = menu_items(rows['Scene'])
    # R4: evaluated before the failure induction below, whose retries are deliberate playback commands.
    assert_inert('6j opening Mix and row menus sends no playback command', mark_c, mark_b)

    def labels(m):
        return {t for items in m.values() for t, _ in items}
    # R4 (2026-11-18): operator feedback plus the UX assessment (item 3) and O6 criterion 9 require "Try again" only
    # for a layer that failed. ORIGINAL 6g/6h/6i required 'Try again' in every Music, Ambience and Scene row menu
    # even when the room was healthy:
    #   ORIGINAL 6g {'Try again','Stop'} <= labels(music_menu) and 'Next track' not in labels(music_menu)
    #   ORIGINAL 6h {'Try again','Stop'} <= labels(amb_menu)
    #   ORIGINAL 6i {'Try again','Stop'} <= labels(scene_menu)
    # The revision is stronger: a healthy layer's menu has Stop and no Try again, and the same layer, after a
    # real failure, offers Try again and Stop; after it recovers the error is gone.
    def failed_menu(row_title, flag, layer):
        setattr(backend, flag, True)
        try:
            controller.layer({'layer': layer, 'action': 'retry'})
        except Exception:
            pass
        settle(1.0)
        wake(window)
        shown_menu = menu_items(row_of(window, row_title))
        setattr(backend, flag, False)
        try:
            controller.layer({'layer': layer, 'action': 'retry'})
        except Exception:
            pass
        settle(1.0)
        return shown_menu, bool((snapshot().get('errors') or {}).get(layer))
    music_failed, music_recorded = failed_menu('Music', 'fail_music', 'music')
    amb_failed, amb_recorded = failed_menu('Ambience', 'fail_atmosphere', 'atmosphere')
    scene_failed, scene_recorded = failed_menu('Scene', 'fail_picture', 'picture')
    check('6g Music row menu: Stop always; Try again only after a failure; no Next track on live radio',
          'Stop' in labels(music_menu) and 'Try again' not in labels(music_menu) and 'Next track' not in labels(music_menu)
          and {'Try again', 'Stop'} <= labels(music_failed) and 'Next track' not in labels(music_failed) and not music_recorded,
          'healthy=%r failed=%r recovered_error=%s' % (music_menu, music_failed, music_recorded))
    check('6h Ambience row menu: Stop always; Try again only after a failure',
          'Stop' in labels(amb_menu) and 'Try again' not in labels(amb_menu)
          and {'Try again', 'Stop'} <= labels(amb_failed) and not amb_recorded,
          'healthy=%r failed=%r recovered_error=%s' % (amb_menu, amb_failed, amb_recorded))
    check('6i Scene row menu: Stop always; Try again only after a failure',
          'Stop' in labels(scene_menu) and 'Try again' not in labels(scene_menu)
          and {'Try again', 'Stop'} <= labels(scene_failed) and not scene_recorded,
          'healthy=%r failed=%r recovered_error=%s' % (scene_menu, scene_failed, scene_recorded))

    # Level and mute reach the controller through the existing layer command.
    mark = len(COMMANDS)
    scale = scales(rows['Music'])[0]
    scale.set_value(35)
    pump(.6)
    settle(.6)
    cmds = mutating_since(mark)
    check('6k Music level slider sends a layer volume command that persists (35)',
          any(c[0] == 'layer' and c[1].get('layer') == 'music' and c[1].get('action') == 'volume' and c[1].get('value') == 35 for c in cmds)
          and controller.active['music'].get('volume') == 35, repr(cmds) + ' stored=%r' % controller.active['music'].get('volume'))
    mark = len(COMMANDS)
    mute = mutes(rows['Music'])[0]
    mute.set_active(True) if hasattr(mute, 'set_active') else click(mute)
    settle(.8)
    cmds = mutating_since(mark)
    check('6l Music mute sends a layer command and the layer is muted',
          any(c[0] == 'layer' and c[1].get('layer') == 'music' for c in cmds) and backend.state['music'].get('muted'),
          repr(cmds))
    # Indoor ambience: Window disappears.
    controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': indoor['id'],
                      'name': indoor['name'], 'outdoor': False, 'volume': 20})
    settle(1.2)
    check('6m Ambience shows Window only for outdoor sounds (indoor sound hides it)',
          'Window' not in texts(row_of(window, 'Ambience')), repr(texts(row_of(window, 'Ambience'))))

    # Scene close.
    mark = len(COMMANDS)
    scene_row = row_of(window, 'Scene')
    closer = next((w for w in walk(scene_row) if interactive_type(w) and shown(w) and 'close' in (acc_name(w) or '').lower()), None)
    if closer is not None:
        click(closer)
        settle(1.0)
    cmds = mutating_since(mark)
    check('6n Scene can be closed (one layer command, picture stops, others play)',
          len(cmds) == 1 and cmds[0][0] in ('layer', 'picture') and backend.state['picture']['status'] != 'Playing'
          and backend.state['music']['status'] == 'Playing', repr(cmds) + ' picture=%s' % backend.state['picture'].get('status'))

    # Save as my room...
    save = button_with_text(window, 'Save as my room…')
    check('6o Mix offers "Save as my room…"', save is not None, '')
    if save is not None:
        mark = len(COMMANDS)
        click(save)
        pump(1.0)
        dialog = window.get_visible_dialog()
        entries = [w for w in walk(dialog) if isinstance(w, Gtk.Editable) and shown(w)] if dialog is not None else []
        prefilled = [e.get_text() for e in entries]
        check('6p Save dialog opens with the room name prefilled', dialog is not None and 'Midnight library' in prefilled,
              repr(prefilled))
        if dialog is not None and entries:
            entries[0].set_text('Acceptance room')
            confirm = next((w for w in walk(dialog) if isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)).startswith('Save')), None)
            before = len(controller.variants())
            if confirm is not None:
                click(confirm)
            settle(1.0)
            cmds = mutating_since(mark)
            check('6q Save goes through the existing save command and stores the room',
                  any(c[0] == 'save' and c[1].get('name') == 'Acceptance room' for c in cmds)
                  and any(v.get('name') == 'Acceptance room' for v in controller.variants()), repr(cmds))
    for d in [w for w in walk(window) if isinstance(w, Adw.Dialog)]:
        d.force_close()
    leave_all()


def s7_layer_failure():
    backend.fail_picture = True
    enter_room('midnight-library')   # existing staging: music plays, picture error recorded
    errors = snapshot().get('errors') or {}
    assert errors.get('picture'), 'precondition: controller did not record a picture error %r' % errors
    window = launch()
    wake(window)
    open_btn = one(window, 'Try again')
    again = [w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)) == 'Try again' and not in_popover(w)
             and not in_mix(w)]
    choose = [w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)) == 'Choose another'
              and not in_mix(w)]
    body = texts(window)
    banner_text = [t for t in body if 'Scene' in t]
    check('7a exactly one banner: one Try again and one Choose another', len(again) == 1 and len(choose) == 1,
          'try=%d choose=%d' % (len(again), len(choose)))
    keeps = [t for t in body if 'Music' in t and 'keep' in t]
    check('7b banner names the failing layer (Scene) and what keeps playing (Music)',
          bool(banner_text) and bool(keeps), repr(body))
    check('7c banner vocabulary is clean', not vocabulary_violations(window), repr(vocabulary_violations(window)))
    check('7d other layers keep playing', backend.state['music']['status'] == 'Playing', repr(backend.state['music']))
    # marker on Mix
    mix = one(window, 'Mix')
    signature = [(type(w).__name__, tuple(w.get_css_classes()), w.get_width(), w.get_height()) for w in walk(mix) if shown(w)]
    # Compare against a fresh window on a healthy room.
    screenshot(window, 'a05-layer-failure-banner')
    backend.fail_picture = False
    mark = len(COMMANDS)
    mark_calls = len(backend.calls)
    if again:
        click(again[0])
        settle(1.5)
    cmds = mutating_since(mark)
    effects = backend.calls[mark_calls:]
    check('7e Try again re-runs only the failing layer (one picture/layer command; only start_picture runs)',
          len(cmds) == 1 and cmds[0][0] in ('layer', 'picture') and (cmds[0][1].get('layer') == 'picture' or cmds[0][0] == 'picture')
          and all(e[0] == 'start_picture' for e in effects) and bool(effects), repr(cmds) + ' effects=' + repr(effects)[:200])
    healthy_sig = [(type(w).__name__, tuple(w.get_css_classes()), w.get_width(), w.get_height()) for w in walk(mix) if shown(w)]
    check('7f Mix button carries a marker only while a layer failed (widget tree differs failed vs healthy)',
          len(signature) > len(healthy_sig), 'failed=%d shown widgets, healthy=%d' % (len(signature), len(healthy_sig)))
    pump(.8)
    still = [w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)) == 'Choose another']
    check('7g banner clears once the layer starts', not still and snapshot().get('errors', {}).get('picture') is None,
          repr(snapshot().get('errors')))
    # Choose another
    backend.fail_picture = True
    controller.layer({'layer': 'picture', 'action': 'retry'}) if False else None
    leave_all()
    backend.fail_picture = True
    enter_room('midnight-library')
    window = launch()
    wake(window)
    choose = next((w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and ' '.join(texts(w)) == 'Choose another'), None)
    mark = len(COMMANDS)
    if choose is not None:
        click(choose)
        pump(1.0)
    check('7h Choose another opens a chooser (popover, Mix or dialog) without changing playback',
          choose is not None and not mutating_since(mark) and (bool(mapped_popovers(window)) or mix_open(window)
                                                                or window.get_visible_dialog() is not None),
          'commands=%r popovers=%d mix=%s' % (mutating_since(mark), len(mapped_popovers(window)), mix_open(window)))
    leave_all()

    # A music failure: staging/fallback is the controller's business; the window must show Music.
    enter_room('midnight-library')
    enter_library()
    backend.fail_music = True
    window = launch()
    mark_c = len(COMMANDS)
    # a second music start that fails (controller keeps the old music playing)
    try:
        controller.layer({'layer': 'music', 'action': 'retry'})
    except Exception:
        pass
    settle(1.0)
    errors = snapshot().get('errors') or {}
    if errors.get('music'):
        wake(window)
        texts_now = texts(window)
        check('7i music failure banner names Music', any('Music' in t and ('couldn' in t or 'start' in t) for t in texts_now), repr(texts_now))
    else:
        print('NOTE 7i: controller recorded no music error for this setup (%r); music-failure banner not exercised' % errors)
    backend.fail_music = False
    leave_all()


def s8_room_volume():
    backend.calls.clear()
    enter_room('midnight-library')
    outdoor = next(x for x in atmosphere_catalogue.SOUNDS if x['outdoor'])
    controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': outdoor['id'],
                      'name': outdoor['name'], 'outdoor': True, 'volume': 20})
    settle()
    music0 = controller.active['music'].get('volume', 50)
    amb0 = controller.active['atmosphere'].get('volume', 20)
    window = launch()
    wake(window)
    scale = one(window, 'Room volume')
    mark = len(COMMANDS)
    scale.set_value(50)
    pump(.8)
    settle(.8)
    cmds = [c for c in mutating_since(mark)]
    snap = snapshot()
    check('8a the window sets room volume through the controller (room_volume command, value 50)',
          any(c[0] == 'room_volume' and c[1].get('value') == 50 for c in cmds) and snap.get('room_volume') == 50, repr(cmds))
    live_m, live_a = backend.state['music'].get('volume'), backend.state['atmosphere'].get('volume')
    check('8b room volume scales Music and Ambience, keeping their ratio',
          live_m is not None and live_a is not None and abs(live_m - music0 * .5) < .6 and abs(live_a - amb0 * .5) < .6
          and abs(live_m / live_a - music0 / amb0) < .05, 'music %s->%s ambience %s->%s' % (music0, live_m, amb0, live_a))
    check('8c per-layer levels persist unchanged under room volume',
          controller.active['music'].get('volume') == music0 and controller.active['atmosphere'].get('volume') == amb0,
          'stored music=%s ambience=%s' % (controller.active['music'].get('volume'), controller.active['atmosphere'].get('volume')))
    # MPRIS and roomctl read/set the same value.
    check('8d MPRIS Volume reads the room volume', abs(session_service.mpris_values(controller.snapshot())['Volume'] - .5) < .005,
          str(session_service.mpris_values(controller.snapshot())['Volume']))
    import roomctl
    import io
    import contextlib

    def roomctl_run(*argv):
        saved = sys.argv
        sys.argv = ['listening-roomctl'] + list(argv)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = roomctl.main()
        finally:
            sys.argv = saved
        return rc, out.getvalue().strip()
    rc, printed = roomctl_run('volume')
    check('8e listening-roomctl volume reads the same value (50)', rc in (0, None) and printed == '50', 'rc=%r out=%r' % (rc, printed))
    mark = len(COMMANDS)
    rc, printed = roomctl_run('volume', '70')
    settle(.8)
    check('8f listening-roomctl volume 70 sets it; the window slider follows',
          snapshot().get('room_volume') == 70 and abs(scale.get_value() - 70) < 1
          and abs(backend.state['music'].get('volume') - music0 * .7) < .6,
          'rc=%r room=%s slider=%s music=%s' % (rc, snapshot().get('room_volume'), scale.get_value(), backend.state['music'].get('volume')))
    # MPRIS write path
    mpris = object.__new__(session_service.Mpris)
    mpris.service = service
    mpris.set_property(None, None, None, None, 'Volume', GLib.Variant('d', .3))
    settle(.8)
    check('8g MPRIS Volume write sets the same value; window slider follows',
          snapshot().get('room_volume') == 30 and abs(scale.get_value() - 30) < 1,
          'room=%s slider=%s' % (snapshot().get('room_volume'), scale.get_value()))
    # Saved rooms store it.
    controller.set_room_volume(40)
    settle(.5)
    saved = controller.save({'name': 'Volume room'})['variant']
    check('8h saved rooms store room volume', saved.get('room_volume') == 40, repr(saved.get('room_volume')))
    leave_all()
    controller.set_room_volume(100)
    variant_id = next(v['id'] for v in controller.variants() if v['name'] == 'Volume room')
    controller.enter({'variant_id': variant_id})
    settle()
    check('8i entering the saved room restores its room volume', snapshot().get('room_volume') == 40, repr(snapshot().get('room_volume')))
    leave_all()
    runbook = repo / 'docs/runbooks/listening-room-session.md'
    text = runbook.read_text()
    check('8j runbook documents the listening-roomctl volume verb', 'listening-roomctl volume' in text, str(runbook))
    controller.set_room_volume(100)


def s9_timer_output():
    enter_room('midnight-library')
    window = launch()
    wake(window)
    button, popover = open_popover(window, 'Timer')
    items = {' '.join(texts(w)): w for w in walk(popover) if isinstance(w, Gtk.Button) and shown(w)}
    check('9a Timer popover offers Break 10 min, Wind down 30 min, Dim the scene 30 min, Cancel',
          {'Break 10 min', 'Wind down 30 min', 'Dim the scene 30 min', 'Cancel'} <= set(items), repr(sorted(items)))
    check('9b no timer chip before a timer runs', not chip_texts(window), repr(chip_texts(window)))
    mark = len(COMMANDS)
    click(items['Break 10 min'])
    settle(1.2)
    cmds = mutating_since(mark)
    check('9c Break 10 min sends the existing timed quiet with minutes 10',
          len(cmds) == 1 and cmds[0][0] == 'quiet' and cmds[0][1].get('minutes') == 10, repr(cmds))
    check('9d Break pauses the room and records a resume offer',
          backend.state['music']['status'] == 'Paused' and bool(snapshot().get('resume_offer')), repr(snapshot().get('resume_offer')))
    chip = chip_texts(window)
    check('9e a timer chip with time remaining appears in the header', bool(chip), repr(texts(window)))
    toast = texts(window)
    undo = button_with_text(window, 'Undo')
    check('9f Break confirms with a toast carrying Undo', undo is not None and any('reak' in t for t in toast), repr(toast))
    screenshot(window, 'a06-timer-chip')
    # never auto-resumes
    CLOCK[0] += 11 * 60
    settle(2.0)
    pump(1.2)
    check('9g Break never auto-resumes after its time (music still Paused); Play is offered',
          backend.state['music']['status'] == 'Paused' and one(window, 'Play the room') is not None
          and not [c for c in mutating_since(mark) if c[0] == 'resume'],
          'music=%s cmds=%r' % (backend.state['music']['status'], mutating_since(mark)))
    mark = len(COMMANDS)
    undo = button_with_text(window, 'Undo')
    # Resume, then Wind down and Dim.
    click(one(window, 'Play the room'))
    settle(1.0)
    button, popover = open_popover(window, 'Timer')
    items = {' '.join(texts(w)): w for w in walk(popover) if isinstance(w, Gtk.Button) and shown(w)}
    mark = len(COMMANDS)
    click(items['Wind down 30 min'])
    settle(1.2)
    cmds = mutating_since(mark)
    check('9h Wind down 30 min sends wind_down with minutes 30; chip shows it',
          len(cmds) == 1 and cmds[0][0] == 'wind_down' and cmds[0][1].get('minutes') == 30
          and any('ind' in t for t in chip_texts(window)), repr(cmds) + repr(chip_texts(window)))
    check('9i Wind down toast carries Undo', button_with_text(window, 'Undo') is not None, repr(texts(window)))
    service.execute('wind_down', {'minutes': 0})
    settle(1.0)
    button, popover = open_popover(window, 'Timer')
    items = {' '.join(texts(w)): w for w in walk(popover) if isinstance(w, Gtk.Button) and shown(w)}
    mark = len(COMMANDS)
    click(items['Dim the scene 30 min'])
    settle(1.2)
    cmds = mutating_since(mark)
    check('9j Dim the scene 30 min sends one command with minutes 30 and shows a chip',
          len(cmds) == 1 and cmds[0][1].get('minutes') == 30 and any('im' in t for t in chip_texts(window)),
          repr(cmds) + repr(chip_texts(window)))
    check('9j2 Dim toast carries Undo', button_with_text(window, 'Undo') is not None, '')
    button, popover = open_popover(window, 'Timer')
    items = {' '.join(texts(w)): w for w in walk(popover) if isinstance(w, Gtk.Button) and shown(w)}
    cancel = items.get('Cancel')
    check('9k Cancel is available while a timer runs', cancel is not None and cancel.get_sensitive(), '')
    mark = len(COMMANDS)
    click(cancel)
    settle(1.2)
    check('9l Cancel stops the timers and removes the chip',
          bool(mutating_since(mark)) and not snapshot().get('timers') and not chip_texts(window),
          'timers=%r chips=%r cmds=%r' % (snapshot().get('timers'), chip_texts(window), mutating_since(mark)))
    # Output popover
    button, popover = open_popover(window, 'Output')
    body = texts(popover)
    check('9m Output popover lists the owned-stream outputs', any('Desk speakers' in t for t in body) and any('Headphones' in t for t in body), repr(body))
    test_btn = button_with_text(popover, 'Test sound')
    check('9n Output popover has Test sound', test_btn is not None, repr(body))
    SIDE.clear()
    if test_btn is not None:
        click(test_btn)
        pump(.6)
    check('9o Test sound plays a test sound and sends no playback command to the room', bool(SIDE), repr(SIDE))
    sliders = [w for w in walk(popover) if isinstance(w, Gtk.Scale) and shown(w)]
    check('9p Output popover has a system volume control',
          bool(sliders) and ('System volume' in body or any('System volume' == acc_name(s) for s in sliders)), repr(body))
    screenshot(window, 'a07-output-popover')
    if sliders:
        sliders[0].set_value(55)
        pump(.6)
        check('9q system volume slider changes the system volume', SYSTEM_VOLUME['percent'] == 55, repr(SYSTEM_VOLUME))
    close_popover(button)
    leave_all()


def chip_texts(window):
    """Time-remaining text in the top 90px of the window (the header), outside popovers and toasts."""
    import re
    found = []
    for w in walk(window):
        if isinstance(w, Gtk.Label) and shown(w) and w.get_text() and not in_popover(w) and re.search(r'\d+\s*(min|m\b)|\d+:\d\d', w.get_text()):
            ok, rect = w.compute_bounds(window)
            if ok and rect.get_y() < 90:
                found.append(w.get_text())
    return found


def playing_streams():
    import gc
    return [o for o in gc.get_objects() if isinstance(o, Gtk.MediaStream) and o.get_playing()]


def animated_transitions(window):
    found = []
    for w in walk(window):
        if isinstance(w, Gtk.Stack) and w.get_transition_type() != Gtk.StackTransitionType.NONE and w.get_transition_duration() > 0:
            found.append(('Stack', int(w.get_transition_type()), w.get_transition_duration(), describe(w)))
        if isinstance(w, Gtk.Revealer) and w.get_transition_type() != Gtk.RevealerTransitionType.NONE and w.get_transition_duration() > 0:
            found.append(('Revealer', int(w.get_transition_type()), w.get_transition_duration(), describe(w)))
    return found


def s10_reduced_motion():
    # A room whose scene has a reviewed loop.
    looping = [p for p in ambience.presets() if p.get('kind') == 'local' and p.get('url')]
    enter_room('midnight-library')
    window = launch()
    pump(1.5)
    normal_playing = playing_streams()
    print('NOTE 10: with motion allowed, playing media streams = %d (presets with loops: %d)' % (len(normal_playing), len(looping)))
    if normal_playing:
        window.set_visible(False)
        pump(1.0)
        paused_hidden = not playing_streams()
        window.set_visible(True)
        pump(1.0)
        check('10a loop plays muted while visible and pauses while the window is hidden',
              paused_hidden and bool(playing_streams()) and all(s.get_muted() for s in playing_streams()), '')
    else:
        print('NOTE 10a: no loop played in this environment (GStreamer media backend missing or no loop); '
              'the motion-allowed play/pause-on-hidden behaviour is NOT checkable here')
    if normal_playing or True:
        # control: with motion allowed, do page switches animate? (makes the reduced-motion check meaningful)
        names_ok = []
        for page in ('rooms', 'now'):
            names = window.get_application().get_actions_for_accel({'rooms': '<Control>1', 'now': '<Control>3'}[page])
            window.activate_action(names[0], None)
            for _ in range(10):
                context.iteration(False)
                names_ok += [w for w in walk(window) if isinstance(w, Gtk.Stack) and w.get_transition_running()]
                time.sleep(.02)
            pump(.4)
        print('NOTE 10e control: with motion allowed, Gtk.Stack transitions observed running = %s' % bool(names_ok))
    service.execute('settings', {'values': {'reduced_motion': True}})
    settle(1.5)
    window2 = launch()
    pump(1.5)
    check('10b reduced motion: no playing video/media stream anywhere', not playing_streams(), repr(playing_streams()))
    video = [w for w in walk(window2) if isinstance(w, Gtk.Video)]
    streams = [w.get_paintable() for w in walk(window2) if isinstance(w, Gtk.Picture) and isinstance(w.get_paintable(), Gtk.MediaStream)]
    check('10c reduced motion: no video widget or media paintable in the tree', not video and not streams, repr((video, streams)))
    image = screenshot(window2, 'a08-now-reduced-motion').convert('L')
    import statistics
    top = image.crop((0, 80, image.width, int(image.height * .45)))
    spread = statistics.pstdev(list(top.getdata()))
    check('10d reduced motion: still art is shown behind Now (non-flat pixels above the scrim; stdev %.1f >= 8)' % spread,
          spread >= 8, 'stdev=%.1f' % spread)
    def transition_seen(win):
        seen = []
        for page in ('rooms', 'now', 'listen', 'now'):
            goto(win, page) if False else None
            names = win.get_application().get_actions_for_accel({'rooms': '<Control>1', 'listen': '<Control>2', 'now': '<Control>3'}[page])
            win.activate_action(names[0], None)
            for _ in range(12):
                for _ in range(3):
                    context.iteration(False)
                for w in walk(win):
                    if isinstance(w, Gtk.Stack) and w.get_transition_running():
                        seen.append(describe(w))
                time.sleep(.02)
            pump(.4)
        return sorted(set(seen))
    running = transition_seen(window2)
    check('10e reduced motion: switching Rooms/Listen/Now runs no animated Gtk.Stack transition', not running, repr(running))
    service.execute('settings', {'values': {'reduced_motion': False}})
    leave_all()


def s11_keyboard_a11y():
    enter_room('midnight-library')
    window = launch()
    wake(window)
    # Space pauses / resumes
    mark = len(COMMANDS)
    handled = key(window, Gdk.KEY_space)
    settle(1.0)
    cmds = mutating_since(mark)
    check('11a Space pauses the room (quiet)', handled and cmds and cmds[-1][0] == 'quiet' and backend.state['music']['status'] == 'Paused',
          repr(cmds))
    mark = len(COMMANDS)
    key(window, Gdk.KEY_space)
    settle(1.0)
    cmds = mutating_since(mark)
    check('11b Space again resumes the room', cmds and cmds[-1][0] == 'resume' and backend.state['music']['status'] == 'Playing', repr(cmds))
    # Up / Down
    before = snapshot().get('room_volume', 100)
    key(window, Gdk.KEY_Down)
    pump(.7)
    settle(.5)
    down = snapshot().get('room_volume')
    key(window, Gdk.KEY_Up)
    pump(.7)
    settle(.5)
    up = snapshot().get('room_volume')
    check('11c Down lowers and Up raises room volume in Now', down < before and up > down, 'before=%s down=%s up=%s' % (before, down, up))
    # Esc: Mix first, then browsing
    open_mix(window)
    key(window, Gdk.KEY_Escape)
    pump(.9)
    check('11d Esc closes Mix and stays in Now', not mix_open(window) and on_now(window), 'mix=%s now=%s' % (mix_open(window), on_now(window)))
    button, _ = open_popover(window, 'Timer')
    # Native popover handling of Esc is GTK's; assert the popover closes on Escape via its own key controller.
    popover = button.get_popover()
    for c in popover.observe_controllers():
        if isinstance(c, Gtk.EventControllerKey):
            c.emit('key-pressed', Gdk.KEY_Escape, 0, 0)
    pump(.5)
    still_open = popover.get_mapped()
    if still_open:
        print('NOTE 11e: popover closing on Esc is native GTK behaviour needing a real key event; synthetic emit did not close it')
        close_popover(button)
    key(window, Gdk.KEY_Escape)
    pump(.9)
    check('11f Esc from Now returns to browsing', not on_now(window), 'now=%s' % on_now(window))
    # Ctrl+1/2/3
    goto(window, 'listen')
    rooms_seen = on_rooms_return(window)
    goto(window, 'rooms')
    goto(window, 'now')
    check('11g Ctrl+3 opens Now, Ctrl+2/Ctrl+1 leave it (accelerators registered)', on_now(window), '')
    goto(window, 'listen')
    in_listen = not on_now(window)
    goto(window, 'rooms')
    check('11h Ctrl+1/Ctrl+2 reach browsing pages', in_listen and not on_now(window), '')
    goto(window, 'now')
    # Names
    required = {'Pause the room', 'Next track', 'Room volume', 'Mix', 'Output', 'Timer', 'Main menu', 'Back to rooms'}
    leave_all()
    enter_room('midnight-library')
    enter_library()
    window = launch()
    wake(window)
    present = {n: len([w for w in named(window, n) if interactive_type(w) and not owned_by_compound(w, window)]) for n in required}
    check('11i each required control exists exactly once under its exact accessible name (Pause state)',
          all(v == 1 for v in present.values()), repr(present))
    click(one(window, 'Pause the room'))
    settle(1.0)
    check('11j "Play the room" appears when paused', one(window, 'Play the room') is not None, '')
    click(one(window, 'Play the room'))
    settle(.8)

    def icon_only(w):
        return isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.ToggleButton)) and not texts(w)
    unnamed = []
    for page, preparer in (('Now', lambda: None), ('Mix', lambda: open_mix(window)), ):
        preparer()
        for w in controls(window):
            if icon_only(w) and not getattr(w, '_acceptance_label', None):
                unnamed.append((page, type(w).__name__, w.get_tooltip_text(), w.get_icon_name() if hasattr(w, 'get_icon_name') else None))
        if page == 'Mix':
            close_mix(window)
    click(one(window, 'Back to rooms'))
    pump(.6)
    for page in ('rooms', 'listen'):
        goto(window, page)
        for w in controls(window):
            if icon_only(w) and not getattr(w, '_acceptance_label', None):
                unnamed.append((page, type(w).__name__, w.get_tooltip_text(), w.get_icon_name() if hasattr(w, 'get_icon_name') else None))
    check('11k every icon-only control has an explicit accessible name (Now, Mix, Rooms, Listen)', not unnamed, repr(unnamed))
    goto(window, 'now')
    # Focus visible over art
    results = []
    for name in ('Pause the room', 'Mix', 'Next track'):
        target = one(window, name)
        if target is None:
            continue
        wake(window)
        window.set_focus_visible(True)
        window.set_focus(None)
        pump(.4)
        ok, rect = target.compute_bounds(window)
        from PIL import ImageChops
        box = (int(rect.get_x()) - 8, int(rect.get_y()) - 8, int(rect.get_x() + rect.get_width()) + 8, int(rect.get_y() + rect.get_height()) + 8)
        before_img = screenshot(window, 'tmp-focus-before').crop(box).convert('RGB')
        target.grab_focus()
        window.set_focus_visible(True)
        wake(window)
        pump(.4)
        after_img = screenshot(window, 'tmp-focus-after').crop(box).convert('RGB')
        diff = ImageChops.difference(before_img, after_img)
        changed = sum(1 for p in diff.getdata() if max(p) > 60)
        results.append((name, changed))
        if name == 'Pause the room':
            after_img.save(output / 'a09-focus-visible-play.png')
    for tmp in ('tmp-focus-before.png', 'tmp-focus-after.png'):
        try:
            (output / tmp).unlink()
        except OSError:
            pass
    check('11l keyboard focus ring is visible over art (>=40 clearly changed pixels around the focused control)',
          bool(results) and all(c >= 40 for _, c in results), repr(results))
    leave_all()


def s12_layout():
    for (w, h) in ((1200, 800), (800, 700)):
        problems = []
        for scenario_name in ('rooms', 'listen', 'now', 'mix', 'banner', 'chip'):
            leave_all()
            if scenario_name == 'banner':
                backend.fail_picture = True
            enter_room('midnight-library')
            if scenario_name in ('rooms', 'listen'):
                controller.leave(0)
                settle(1.0)
            window = launch(w, h)
            if scenario_name == 'listen':
                goto(window, 'listen')
            if scenario_name == 'chip':
                service.execute('wind_down', {'minutes': 30})
                settle(1.0)
            if scenario_name == 'mix':
                open_mix(window)
            wake(window)
            pump(.6)
            if scenario_name in ('rooms', 'now', 'mix', 'banner'):
                screenshot(window, 'a12-%s-%dx%d' % (scenario_name, w, h))
            for c in walk(window):
                if not interactive_type(c) or not shown(c):
                    continue
                if not isinstance(c, Gtk.MenuButton) and owned_by_compound(c, window):
                    continue
                if in_popover(c):
                    continue
                ok, rect = c.compute_bounds(window)
                if not ok:
                    continue
                label = acc_name(c) or ' '.join(texts(c)) or c.get_tooltip_text() or type(c).__name__
                in_scroll = any(isinstance(p, Gtk.ScrolledWindow) for p in ancestors(c))
                if rect.get_x() < -1 or rect.get_x() + rect.get_width() > w + 1:
                    problems.append((scenario_name, label, 'x %.0f..%.0f' % (rect.get_x(), rect.get_x() + rect.get_width())))
                if not in_scroll and (rect.get_y() < -1 or rect.get_y() + rect.get_height() > h + 1):
                    problems.append((scenario_name, label, 'y %.0f..%.0f' % (rect.get_y(), rect.get_y() + rect.get_height())))
            for sw in walk(window):
                if isinstance(sw, Gtk.ScrolledWindow) and sw.get_mapped():
                    adj = sw.get_hadjustment()
                    if adj.get_upper() > adj.get_page_size() + 1:
                        problems.append((scenario_name, 'horizontal scroll', '%.0f > %.0f' % (adj.get_upper(), adj.get_page_size())))
        check('12 layout at %dx%d: no control clipped or off-window, no horizontal scroll' % (w, h), not problems, repr(problems))
    leave_all()


def s13_contrast():
    rooms = [r['id'] for r in room_catalogue.rooms()]
    worst_all = 99
    lines = []
    for size in ((1200, 800), (800, 700)):
        for room in rooms:
            leave_all()
            enter_room(room)
            window = launch(*size)
            pump(1.2)
            wake(window)
            name = controller.active['name']
            now_name = backend.state['music'].get('name') or ''
            candidates = [w for w in walk(window) if isinstance(w, Gtk.Label) and shown(w) and w.get_text()
                          and not any(isinstance(p, (Gtk.Button, Gtk.MenuButton, Gtk.Scale, Gtk.Popover, Adw.HeaderBar)) for p in ancestors(w))
                          and not in_mix(w)]
            title = [w for w in candidates if w.get_text() == name]
            playing = [w for w in candidates if w not in title and (now_name and (now_name in w.get_text() or w.get_text().lstrip('· ') in now_name))]
            image_name = 'a13-contrast-%s-%dx%d' % (room, size[0], size[1])
            screenshot(window, image_name)
            measured = measure_contrast(window, title + playing)
            title_vals = [m for m in measured if m[0] == name]
            now_vals = [m for m in measured if m[0] != name]
            lines.append((room, '%dx%d' % size, [(t, round(c, 2), a) for t, c, a in measured]))
            ok = bool(title_vals) and bool(now_vals) and all(c >= 4.5 for _, c, _ in measured)
            worst = min([c for _, c, _ in measured] or [0])
            worst_all = min(worst_all, worst)
            check('13 contrast %s @%dx%d: room title and now-playing >= 4.5:1' % (room, size[0], size[1]), ok,
                  'title=%s now=%s' % ([(round(c, 2)) for _, c, _ in title_vals], [(t, round(c, 2), a) for t, c, a in now_vals])
                  + ('' if title_vals and now_vals else ' (label not found: title=%d now-playing=%d; candidates=%r)' % (len(title_vals), len(now_vals), [w.get_text() for w in candidates])))
    leave_all()


def s14_evidence_static():
    evidence = output   # the screenshots this run just wrote
    files = sorted(p.name for p in evidence.glob('*.png')) if evidence.is_dir() else []
    wanted = ('rooms', 'live', 'library', 'mix', 'failure', 'timer', 'output', 'reduced', '800')
    missing = [w for w in wanted if not any(w in f.lower() or (w == 'live' and 'radio' in f.lower()) or (w == 'failure' and 'banner' in f.lower())
                                              or (w == 'timer' and 'chip' in f.lower()) for f in files)]
    check('14 (static) evidence directory holds the required screenshots', not missing, 'missing=%r files=%r' % (missing, files))


def main():
    build_app()
    selected = [x for x in args.only.split(',') if x]
    for fn in [f for f in (s1_launch, s1b_return_hero_click, s2_inert_browsing, s3_now_at_rest, s4_vocabulary, s5_pause_leave, s6_mix,
               s7_layer_failure, s8_room_volume, s9_timer_output, s10_reduced_motion, s11_keyboard_a11y, s12_layout,
               s13_contrast, s14_evidence_static) if not selected or f.__name__ in selected]:
        print('==', fn.__name__, flush=True)
        scenario(fn)
    close_windows()


try:
    main()
except Exception:
    traceback.print_exc()
    check('acceptance run completed', False, traceback.format_exc()[-600:])
finally:
    (output / 'acceptance.json').write_text(json.dumps(RESULTS, indent=2))
    failed = [r for r in RESULTS if not r['pass']]
    print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed')
    os._exit(1 if failed else 0)
