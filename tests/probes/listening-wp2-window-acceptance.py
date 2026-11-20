"""WP2-WINDOW acceptance: independent oracles for partial rebuilds, one async handoff with polls on their
own worker, the CSS and texture dedupes, the O9 UX leftovers and the O9 race guarantees.

Written by the acceptance reviewer, not the implementer. Real GTK on a private Xvfb with its own D-Bus
(tests/probes/o6_env.py re-executes this file there), the real session controller on the session test
double and fixture providers. Nothing reaches the desktop session, the network, systemd or real
credentials. Everything is measured from outside the window code:

- rebuilds are counted as widget objects that appear (or disappear) under each page of the window,
  holding strong references so an identity is never reused;
- GTK thread identity is asserted by wrapping GTK widget methods and constructors at the class level;
- worker-to-main handoffs are attributed by wrapping GLib.idle_add and friends;
- the texture cache is probed by object identity, whatever stores it.

  scripts/listening-dev.sh shell, then: python3 tests/probes/listening-wp2-window-acceptance.py --output OUT
  (WP2W_SECTIONS=R,P limits the run to those sections; --source points at another tree for a negative control)

Every section is isolated: one that raises records a FAIL and the rest still run.
Criterion 5 (push updates) is written against the observable effect of the WP2-PROTOCOL stream; it fails until that
lane and the window's use of it are merged.
"""
import ast
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from unittest import mock

import o6_env

BASE = '25cda4a'   # main when WP2-WINDOW was cut
env = o6_env.Env(__doc__, '/tmp/listening-wp2w-acceptance')
checks = []
pending_checks = []
report = {}
ONLY = set(filter(None, os.environ.get('WP2W_SECTIONS', '').split(',')))
SOURCE = Path(env.source)
REPO = SOURCE.parent
MAIN = threading.main_thread()


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:600]), flush=True)


def pending(name, detail=''):
    pending_checks.append(name)
    print('PEND ' + name + ('  ' + str(detail)[:300] if detail else ''), flush=True)


def note(text):
    print('NOTE ' + text, flush=True)


# ------------------------------------------------------------------ instrumentation installed before any window exists

def src_frame(skip_self=True):
    """The innermost stack frame that belongs to the code under test (the source tree)."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if name.startswith(str(SOURCE) + os.sep):
            return frame
        frame = frame.f_back
    return None


def where(frame):
    if frame is None:
        return '?'
    qual = getattr(frame.f_code, 'co_qualname', frame.f_code.co_name)
    return '%s:%s:%d' % (os.path.basename(frame.f_code.co_filename), qual, frame.f_lineno)


OFF_MAIN = []          # (method, where) for GTK calls made off the main thread
GTK_CALLS = [0]        # all wrapped calls, a sanity count
HANDOFFS = []          # (outer function key, where) for idle_add & co called off the main thread
CSS_LOADS = []         # (provider, text)
CSS_ADDS = []          # provider objects added to a display
JSON_DUMPS = []        # (thread is main, where) for json.dumps called from the source tree
WATCH_JSON = [False]


def install_instrumentation():
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, GLib, Gtk

    def wrap(cls, name, label=None):
        original = getattr(cls, name, None)
        if original is None:
            return
        label = label or '%s.%s' % (cls.__name__, name)

        def wrapper(*args, **kwargs):
            GTK_CALLS[0] += 1
            if threading.current_thread() is not MAIN:
                OFF_MAIN.append((label, where(src_frame())))
            return original(*args, **kwargs)
        wrapper.__name__ = name
        setattr(cls, name, wrapper)

    watched = {
        Gtk.Widget: ['__init__', 'set_visible', 'set_sensitive', 'add_css_class', 'remove_css_class', 'set_css_classes',
                     'set_tooltip_text', 'set_tooltip_markup', 'queue_draw', 'queue_resize', 'grab_focus', 'set_size_request',
                     'set_opacity', 'set_child_visible', 'set_margin_top', 'set_margin_bottom', 'set_margin_start',
                     'set_margin_end', 'set_halign', 'set_valign', 'set_hexpand', 'set_vexpand', 'insert_after',
                     'insert_before', 'unparent', 'set_focusable', 'set_can_target', 'set_cursor_from_name',
                     'activate_action', 'set_name'],
        Gtk.Label: ['set_text', 'set_label', 'set_markup', 'set_xalign', 'set_wrap', 'set_ellipsize'],
        Gtk.Button: ['set_label', 'set_icon_name', 'set_child'],
        Gtk.MenuButton: ['set_label', 'set_icon_name', 'popup', 'popdown'],
        Gtk.Box: ['append', 'remove', 'prepend', 'insert_child_after', 'reorder_child_after'],
        Gtk.FlowBox: ['append', 'remove', 'insert', 'remove_all', 'prepend', 'invalidate_filter', 'invalidate_sort'],
        Gtk.ListBox: ['append', 'remove', 'insert', 'remove_all', 'prepend'],
        Gtk.Picture: ['set_paintable', 'set_filename', 'set_file', 'set_resource'],
        Gtk.Image: ['set_from_icon_name', 'set_from_paintable', 'set_from_file', 'set_icon_size', 'set_pixel_size'],
        Gtk.Stack: ['set_visible_child_name', 'set_visible_child', 'add_named', 'add_titled', 'add_child', 'remove'],
        Gtk.Range: ['set_value', 'set_range'],
        Gtk.Editable: ['set_text', 'set_position'],
        Gtk.Spinner: ['set_spinning', 'start', 'stop'],
        Gtk.Revealer: ['set_reveal_child', 'set_child'],
        Gtk.Overlay: ['add_overlay', 'remove_overlay', 'set_child'],
        Gtk.ScrolledWindow: ['set_child'],
        Gtk.Window: ['present', 'close', 'set_title', 'set_default_size', 'destroy'],
        Gtk.Popover: ['popup', 'popdown', 'set_child'],
        Gtk.ToggleButton: ['set_active'],
        Gtk.CheckButton: ['set_active'],
        Gtk.Switch: ['set_active'],
        Gtk.DropDown: ['set_selected', 'set_model'],
        Gtk.StringList: ['append', 'splice', 'remove'],
        Gtk.Expander: ['set_expanded', 'set_child', 'set_label'],
        Adw.ToastOverlay: ['add_toast', 'set_child'],
        Adw.PreferencesRow: ['set_title'],
        Adw.ActionRow: ['set_subtitle', 'add_suffix', 'add_prefix'],
        Adw.ComboRow: ['set_selected', 'set_model'],
        Adw.SwitchRow: ['set_active'],
        Adw.ToggleGroup: ['set_active_name', 'set_active', 'add', 'remove_all'],
        Adw.ViewStack: ['set_visible_child_name', 'set_visible_child'],
        Adw.BottomSheet: ['set_open'],
        Adw.ToolbarView: ['set_content', 'set_extend_content_to_top_edge'],
        Adw.Dialog: ['present', 'close', 'force_close', 'set_title'],
    }
    for cls, names in watched.items():
        for name in names:
            wrap(cls, name)
    for name in ('AlertDialog', 'StatusPage', 'Banner', 'WindowTitle', 'ButtonContent', 'Avatar'):
        cls = getattr(Adw, name, None)
        if cls is not None:
            for method in ('set_heading', 'set_body', 'set_title', 'set_description', 'set_label', 'set_icon_name',
                           'set_revealed', 'set_subtitle', 'set_text'):
                if method in cls.__dict__:
                    wrap(cls, method)

    def handoff(original, label):
        def wrapper(*args, **kwargs):
            if threading.current_thread() is not MAIN:
                frame = src_frame()
                qual = getattr(frame.f_code, 'co_qualname', frame.f_code.co_name) if frame else '?'
                outer = qual.split('.<locals>')[0]
                key = (os.path.basename(frame.f_code.co_filename) if frame else '?') + ':' + outer
                HANDOFFS.append((key, where(frame), label))
            return original(*args, **kwargs)
        return wrapper
    for name in ('idle_add', 'timeout_add', 'timeout_add_seconds', 'idle_add_full'):
        original = getattr(GLib, name, None)
        if original is not None:
            setattr(GLib, name, handoff(original, 'GLib.' + name))
    for name in ('invoke', 'invoke_full'):
        original = getattr(GLib.MainContext, name, None)
        if original is not None:
            setattr(GLib.MainContext, name, handoff(original, 'GLib.MainContext.' + name))

    for name in ('load_from_string', 'load_from_data', 'load_from_bytes', 'load_from_path', 'load_from_file',
                 'load_from_resource'):
        original = getattr(Gtk.CssProvider, name, None)
        if original is None:
            continue

        def loader(self, data, *rest, _original=original, _name=name):
            text = data
            if _name == 'load_from_path':
                text = Path(data).read_text(errors='replace')
            elif _name == 'load_from_bytes':
                text = bytes(data.get_data()).decode(errors='replace')
            elif isinstance(data, (bytes, bytearray)):
                text = bytes(data).decode(errors='replace')
            elif not isinstance(data, str):
                text = repr(data)
            CSS_LOADS.append((self, text))
            return _original(self, data, *rest)
        setattr(Gtk.CssProvider, name, loader)
    add_original = Gtk.StyleContext.add_provider_for_display

    def add(display, provider, priority, _original=add_original):
        CSS_ADDS.append(provider)
        return _original(display, provider, priority)
    Gtk.StyleContext.add_provider_for_display = staticmethod(add)

    real_dumps = json.dumps

    def dumps(*args, **kwargs):
        if WATCH_JSON[0]:
            frame = sys._getframe(1)
            if frame.f_code.co_filename.startswith(str(SOURCE) + os.sep):
                JSON_DUMPS.append((threading.current_thread() is MAIN, where(frame)))
        return real_dumps(*args, **kwargs)
    json.dumps = dumps


install_instrumentation()


def section(name):
    def wrap(function):
        if ONLY and name not in ONLY:
            return function
        started = time.monotonic()
        try:
            function()
        except Exception as error:   # one broken section never hides the others
            check('%s: section ran without raising' % name, False, '%s: %s\n%s' % (
                type(error).__name__, error, traceback.format_exc()[-1800:]))
        finally:
            for window in list(env.app.get_windows()):
                window.close()
            env.pump(.4)
            try:
                env.start_error = None
                env.start_delay = 0.
                env.atmosphere_error = None
                if env.controller.active or env.controller.transition:
                    deadline = time.monotonic() + 8
                    while env.controller.transition and time.monotonic() < deadline:
                        env.pump(.2)
                    env.service.execute('leave', {})
                    env.settle(1.0)
            except Exception:
                pass
            note('section %s took %.1f s' % (name, time.monotonic() - started))
        return function
    return wrap


with env:
    Gtk, Gdk, GLib, Adw = env.Gtk, env.Gdk, env.GLib, env.Adw
    pump, settle = env.pump, env.settle
    import session_client
    import next_window as next_window_module

    def widgets(root):
        return list(env.widgets(root))

    def mapped_labels(root):
        return [w for w in widgets(root) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text()]

    def bounds(widget, window):
        if widget is None or not widget.get_mapped():
            return None
        ok, rect = widget.compute_bounds(window)
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok else None

    def overlaps(a, b):
        return bool(a and b and a[0] < b[0] + b[2] - .5 and b[0] < a[0] + a[2] - .5
                    and a[1] < b[1] + b[3] - .5 and b[1] < a[1] + a[3] - .5)

    def wait(predicate, seconds=6.):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            pump(.15)
        return bool(predicate())

    def in_toast(widget):
        node = widget
        while node is not None:
            if node.get_css_name() == 'toast':
                return True
            node = node.get_parent()
        return False

    def radio_tune_buttons(window):
        keys = [t for _k, t in window.listen.scope_list]
        window.listen.scopes.set_active(keys.index('Radio'))
        pump(.6)
        return env.buttons(window.listen.index_body, 'Tune in')

    def find_stack(root, cls, child_name):
        for w in widgets(root):
            if isinstance(w, cls) and w.get_child_by_name(child_name) is not None:
                return w
        return None

    def sections_of(window):
        """Pages of the window found from its widget tree (stack child names, not implementer attributes)."""
        browse = find_stack(window, Adw.ViewStack, 'rooms')
        pages = find_stack(window, Gtk.Stack, 'now')
        roots = {'rooms': browse.get_child_by_name('rooms'), 'listen': browse.get_child_by_name('listen'),
                 'now': pages.get_child_by_name('now')}
        if pages.get_child_by_name('mini') is not None:
            roots['mini'] = pages.get_child_by_name('mini')
        found = {name: [w for w in widgets(root) if not in_toast(w)] for name, root in roots.items()}
        inside = {id(w) for items in found.values() for w in items}
        found['chrome'] = [w for w in widgets(window) if id(w) not in inside and not in_toast(w)]
        return found

    def shelves_of(window):
        body = getattr(window.listen, 'index_body', None)
        if body is None:
            return []
        shelves, child = [], body.get_first_child()
        while child is not None:
            shelves.append((child, widgets(child)))
            child = child.get_next_sibling()
        return shelves

    def diff(before, after):
        out = {}
        for name in set(before) | set(after):
            old = {id(w) for w in before.get(name, [])}
            new = {id(w) for w in after.get(name, [])}
            kinds = {}
            for w in after.get(name, []):
                if id(w) not in old:
                    kinds[type(w).__name__] = kinds.get(type(w).__name__, 0) + 1
            out[name] = {'new': len(new - old), 'gone': len(old - new), 'total': len(new), 'new_types': kinds}
        return out

    def inspects_since(mark):
        return len([a for a, _ in env.log[mark:] if a == 'inspect'])

    # =========================================================================== C: CSS dedupe (criterion 3)
    # Runs first: modules install their CSS once per process, when the first window, preferences or mini open.
    SELECTORS = {
        'window': ['in-now', 'header-pill', 'play-button', 'scrim-top', 'scene-fallback', 'room-name'],
        'rooms': ['rooms-chip', 'rooms-grid', 'rooms-card', 'rooms-card-name', 'preview-title'],
        'listen': ['listen-title', 'listen-heading', 'listen-row', 'station-card', 'row-tile'],
        'preferences': ['prefs-tile', 'prefs-name', 'cap-chip', 'status-dot', 'prefs-note'],
        'mini': ['mini-scrim', 'mini-name', 'mini-line', 'mini-glass', 'mini-volume-icon'],
    }

    @section('C')
    def css():
        defs = []
        for path in sorted(SOURCE.glob('*.py')):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            defs += ['%s:%d' % (path.name, n.lineno) for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'install_css']
        check('C1 one install_css in the source tree (was four copies)', len(defs) == 1, defs)
        window = env.launch(page='listen')
        settle(1.0)
        window.show_rooms()
        pump(.6)
        for _ in range(2):
            window.open_preferences()
            pump(1.0)
            prefs = getattr(window, 'preferences', None)
            if prefs is not None:
                prefs.close()
            pump(.5)
            window.open_mini()
            pump(1.0)
            if window.mini_window is not None:
                window.mini_window.close()
            pump(.5)
        second = env.launch(page='rooms')
        pump(.6)
        second.close()
        pump(.4)
        loaded = '\n'.join(text for _p, text in CSS_LOADS)
        missing = {block: [s for s in names if not re.search(r'\.%s\b' % re.escape(s), loaded)]
                   for block, names in SELECTORS.items()}
        missing = {k: v for k, v in missing.items() if v}
        check('C2 every CSS block the window used before is still installed (window, rooms, listen, preferences, mini)',
              not missing, missing)
        texts = [text for _p, text in CSS_LOADS if len(text) > 200]
        repeated = len(texts) - len(set(texts))
        adds = {}
        for provider in CSS_ADDS:
            adds[id(provider)] = adds.get(id(provider), 0) + 1
        loaded_providers = {id(p) for p, text in CSS_LOADS if len(text) > 200}
        twice = [count for pid, count in adds.items() if pid in loaded_providers and count > 1]
        check('C3 installing is idempotent: no stylesheet loaded twice, none added to the display twice '
              '(two windows, preferences and mini opened twice)', repeated == 0 and not twice,
              {'repeated texts': repeated, 'providers added more than once': twice, 'loads': len(CSS_LOADS)})

    # =========================================================================== R: partial rebuilds (criterion 1)
    @section('R')
    def rebuilds():
        env.start_station_room()
        # A saved room that is not the active one, renamed later as the Rooms-only change.
        import room_catalogue
        env.service.execute('save', {'name': 'Acceptance Spare'})
        settle(1.0)
        variants = env.service.execute('inspect', {}).get('variants') or []
        spare = next((v for v in variants if v.get('name') == 'Acceptance Spare'), None)
        # Saving moves the session onto the saved room: play a catalogue room instead, so the spare is not playing.
        env.service.execute('leave', {})
        settle(1.0)
        room = next(r for r in room_catalogue.rooms() if r.get('source_id')) if any(
            r.get('source_id') for r in room_catalogue.rooms()) else room_catalogue.rooms()[0]
        env.service.execute('enter', {'room_id': room['id']})
        settle(2.0)
        wait(lambda: not env.controller.transition and env.controller.active is not None, 8)
        env.log.clear()
        note('active=%r spare=%r' % ((env.controller.active or {}).get('name'), spare and spare.get('id')))
        window = env.launch(page='listen')
        settle(3.0)
        table = {}

        def measure(label, change=None, seconds=2.2):
            before = sections_of(window)
            shelves = shelves_of(window)
            mark = len(env.log)
            JSON_DUMPS.clear()
            WATCH_JSON[0] = True
            try:
                if change:
                    change()
                pump(seconds)
            finally:
                WATCH_JSON[0] = False
            after = sections_of(window)
            result = diff(before, after)
            result['polls'] = inspects_since(mark)
            result['json_dumps'] = len(JSON_DUMPS)
            table[label] = result
            note('%s: %s' % (label, json.dumps({k: v for k, v in result.items()})))
            return result, shelves

        for page in ('listen', 'rooms', 'now'):
            {'listen': window.show_listen, 'rooms': window.show_rooms, 'now': window.show_now}[page]()
            pump(1.5)
            result, _ = measure('no change, %s shown' % page)
            fresh = {k: v['new_types'] for k, v in result.items() if isinstance(v, dict) and v['new']}
            check('R1 %s shown: a no-change snapshot rebuilds nothing (no new widgets in any page; %d polls)'
                  % (page, result['polls']), result['polls'] >= 3 and not fresh, fresh)
            check('R2 %s shown: a no-change poll serialises nothing with json.dumps in window code' % page,
                  result['json_dumps'] == 0, JSON_DUMPS[:4])
        window.show_listen()
        pump(1.5)
        # Listen-only change: a station saved elsewhere.
        station = dict(o6_env.RADIO_STATION, name='Acceptance Evening')
        result, shelves = measure('station saved elsewhere, listen shown',
                                  lambda: env.service.execute('station.save', {'station': station}))
        listen_root = sections_of(window)['listen']
        shown = any(isinstance(w, Gtk.Label) and 'Acceptance Evening' in (w.get_text() or '') for w in listen_root)
        check('R3 fixture: the saved station reaches the Listen page', shown)
        others = {k: v['new_types'] for k, v in result.items() if isinstance(v, dict) and k != 'listen' and v['new']}
        check('R4 a station change rebuilds no other page (Rooms, Now, mini, chrome)', not others, others)
        untouched, rebuilt = 0, []
        for shelf, items in shelves:
            texts = ' '.join((w.get_text() or '') for w in items if isinstance(w, Gtk.Label))
            if 'Acceptance Evening' in texts or 'stations' in texts.lower():
                continue
            now_items = {id(w) for w in widgets(shelf)} if shelf.get_parent() is not None else set()
            kept = all(id(w) in now_items for w in items) and len(now_items) == len(items)
            if kept:
                untouched += 1
            else:
                rebuilt.append(texts[:60])
        check('R5 a station change leaves the other Listen shelves alone (same widgets, none replaced)',
              (untouched + len(rebuilt)) >= 1 and not rebuilt, {'kept': untouched, 'rebuilt': rebuilt})
        # Rooms-only change: a saved room that is not playing is renamed elsewhere.
        window.show_rooms()
        pump(1.5)
        if spare is not None:
            result, _ = measure('saved room renamed elsewhere, rooms shown',
                                lambda: env.service.execute('rename', {'id': spare['id'], 'name': 'Acceptance Attic'}))
            rooms_root = sections_of(window)['rooms']
            shown = any(isinstance(w, Gtk.Label) and w.get_text() == 'Acceptance Attic' for w in rooms_root)
            check('R6 fixture: the renamed room reaches the Rooms page', shown)
            others = {k: v['new_types'] for k, v in result.items() if isinstance(v, dict) and k != 'rooms' and v['new']}
            check('R7 a Rooms change rebuilds no other page (Listen, Now, mini, chrome)', not others, others)
        else:
            check('R6 fixture: a saved spare room exists to rename', False, variants)
        # Now-only change: the room volume changes elsewhere; no page needs new widgets for that.
        window.show_now()
        pump(1.5)
        target = 37 if abs(window.volume.get_value() - 37) > 3 else 63
        result, _ = measure('room volume changed elsewhere, now shown',
                            lambda: env.service.execute('room_volume', {'value': target}))
        check('R8 fixture: the new room volume reaches Now', abs(window.volume.get_value() - target) < 1.5,
              (window.volume.get_value(), target))
        fresh = {k: v['new_types'] for k, v in result.items() if isinstance(v, dict) and v['new']}
        check('R9 a volume change rebuilds nothing (no new widgets in any page)', not fresh, fresh)
        report['rebuilds'] = table

    # =========================================================================== P: polls have their own worker (criterion 2)
    @section('P')
    def polls():
        env.start_station_room()
        window = env.launch(page=None)
        settle(1.5)
        real = session_client.request
        entered = []

        def slow(action, arguments=None, *rest, **kw):
            if action == 'inspect':
                entered.append(time.monotonic())
            if action == 'acceptance.slow':
                time.sleep(3.0)
                return {}
            return real(action, arguments, *rest, **kw)
        with mock.patch.object(session_client, 'request', side_effect=slow):
            pump(.6)
            for _ in range(6):
                window.command('acceptance.slow', {}, quiet=True)
            started = time.monotonic()
            target = 41 if abs(window.volume.get_value() - 41) > 3 else 59
            env.service.execute('room_volume', {'value': target})
            reached = None
            deadline = started + 2.6
            while time.monotonic() < deadline:
                pump(.05)
                if reached is None and abs(window.volume.get_value() - target) < 1.5:
                    reached = time.monotonic() - started
            after = [t - started for t in entered if t >= started]
            pump(3.5)
        first = after[0] if after else None
        check('P1 with six slow (3 s) commands in flight a poll still reads within 0.8 s', first is not None and first < .8,
              first)
        check('P2 ... and polling keeps its timer rate meanwhile (>= 4 reads in 2.6 s)', len(after) >= 4, len(after))
        check('P3 ... and a change made elsewhere shows in Now within 1.5 s', reached is not None and reached < 1.5, reached)
        window.close()
        pump(.4)
        # The Waybar popover host polls on its own as well.
        import next_mini
        entered.clear()

        def host_request(action, arguments=None, *rest, **kw):
            if action == 'inspect':
                entered.append(time.monotonic())
            if action == 'acceptance.slow':
                time.sleep(3.0)
                return {}
            return env.service.execute(action, arguments or {})
        with mock.patch.object(next_mini.PopoverHost, 'POLL_MS', 200):
            host = next_mini.PopoverHost(env.app, host_request, launch=lambda arguments: None)
            pump(1.0)
            for _ in range(4):
                host.send('acceptance.slow', {}, quiet=True)
            started = time.monotonic()
            pump(.1)
            host.poll()
            pump(2.0)
        after = [t - started for t in entered if t >= started]
        check('P4 the popover host still polls within 0.8 s while four slow commands are in flight',
              bool(after) and after[0] < .8, after[:3])
        try:
            host.window.close()
        except Exception:
            pass
        pump(3.0)

    # =========================================================================== X: texture cache (criterion 3)
    @section('X')
    def textures():
        from PIL import Image
        import next_widgets
        texture_for = getattr(next_widgets, 'texture_for', None)
        choose_renderer = getattr(next_widgets, 'choose_renderer', None)
        if texture_for is None or choose_renderer is None:
            for name in sorted(SOURCE.glob('next_*.py')):
                module = __import__(name.stem)
                texture_for = texture_for or getattr(module, 'texture_for', None)
                choose_renderer = choose_renderer or getattr(module, 'choose_renderer', None)
        folder = Path(tempfile.mkdtemp(prefix='acc-textures-'))

        def image(name, shade):
            path = folder / (name + '.png')
            Image.new('RGB', (4, 4), (shade % 256, (shade * 7) % 256, (shade * 13) % 256)).save(path)
            return str(path)
        first = image('one', 1)
        a, b = texture_for(first), texture_for(first)
        check('X1 the same file twice gives the same texture object', a is not None and a is b, (a, b))
        time.sleep(.02)
        Image.new('RGB', (4, 4), (200, 10, 10)).save(first)
        os.utime(first, ns=(time.time_ns(), time.time_ns() + 5_000_000))
        c = texture_for(first)
        check('X2 a changed file (new mtime) gives a new texture', c is not None and c is not a, (a, c))
        bad = folder / 'broken.png'
        bad.write_bytes(b'not a png')
        check('X3 a missing, empty or unreadable path gives None',
              texture_for(str(folder / 'missing.png')) is None and texture_for('') is None and texture_for(None) is None
              and texture_for(str(bad)) is None)
        paths = [image('n%03d' % i, i + 10) for i in range(300)]
        held = [texture_for(p) for p in paths]
        hits = 0
        for index in range(len(paths) - 1, -1, -1):
            if texture_for(paths[index]) is held[index]:
                hits += 1
            else:
                break
        report['texture_capacity'] = hits
        check('X4 the cache is bounded at about 64 entries (measured %d of 300 still cached)' % hits, 48 <= hits <= 128, hits)
        capacity = hits if 48 <= hits <= 128 else 64
        fresh = [image('l%03d' % i, i + 400) for i in range(capacity + 3)]
        keep = texture_for(fresh[0])
        for p in fresh[1:capacity]:
            texture_for(p)
        touched = texture_for(fresh[0]) is keep
        for p in fresh[capacity:capacity + 2]:
            texture_for(p)
        check('X5 the bound is least-recently-used: a texture used again stays while older ones go',
              touched and texture_for(fresh[0]) is keep, touched)
        marker = folder / 'nvidia-version'
        marker.write_text('fixture')
        environ = {}
        chosen = choose_renderer(environ, str(marker))
        explicit = choose_renderer({'GSK_RENDERER': 'cairo'}, str(marker))
        none = choose_renderer({}, str(folder / 'absent'))
        check('X6 the NVIDIA renderer choice is unchanged: ngl with the driver, an explicit GSK_RENDERER wins, none otherwise',
              chosen == 'ngl' and environ.get('GSK_RENDERER') == 'ngl' and explicit == 'cairo' and not none,
              (chosen, environ, explicit, none))
        del held

    # =========================================================================== U: the O9 UX leftovers (criterion 4)
    @section('U1')
    def placement_undo():
        controller = env.controller
        state_file = Path(env.folder) / 'controller' / 'session.json'

        def stored_chosen():
            try:
                return bool((json.loads(state_file.read_text()).get('placement') or {}).get('chosen'))
            except (OSError, ValueError):
                return None
        with controller.lock:
            controller.placement_chosen = False
            controller.settings['placement'] = 'here'
        env.service.execute('settings', {'values': {}})   # persists the reset without a placement value
        before = (controller.placement_chosen, stored_chosen(), controller.settings.get('placement'))
        window = env.launch(page='rooms')
        window.open_preferences()
        pump(1.2)
        prefs = window.preferences
        row = prefs.placement_row
        words = [row.get_model().get_string(i) for i in range(row.get_model().get_n_items())]
        nine = next(i for i, w in enumerate(words) if '9' in w)
        row.set_selected(nine)
        settle(1.2)
        chose = (controller.placement_chosen, controller.settings.get('placement'))
        undo = env.buttons(prefs, 'Undo') or env.buttons(window, 'Undo')
        check('U1a fixture: choosing Workspace 9 is an explicit choice and offers Undo',
              chose == (True, '9') and bool(undo), (before, chose, words, bool(undo)))
        if undo:
            undo[0].emit('clicked')
        settle(1.5)
        after = (controller.placement_chosen, stored_chosen(), controller.settings.get('placement'))
        check('U1b Undo puts the placement back', after[2] == before[2], (before, after))
        check('U1 Undo of a placement change does not count as choosing a placement (live and stored mark as before)',
              after[0] == before[0] and after[1] == before[1], {'before': before, 'after': after})
        env.shot(prefs, 'acc-wp2w-u1-placement-undo')
        prefs.close()

    @section('U2')
    def failure_card():
        # The card appears when the start command itself is slow (past the grace time) or is refused; a slow
        # controller-side transition alone never shows it. Modelled like the O9 probe's O3: at the client.
        shots = []
        real = session_client.request
        refusal = getattr(session_client, 'SessionError', None) or getattr(session_client, 'SessionUnavailable', RuntimeError)
        STARTS = ('music', 'link', 'station.start', 'online')
        for width, height in ((1200, 800), (800, 700), (640, 520), (1000, 420), (760, 380)):
            for scenario in ('slow', 'refused'):
                window = env.launch(width, height, page='listen')
                buttons = radio_tune_buttons(window)

                def client(action, arguments=None, *rest, **kw):
                    if action in STARTS:
                        if scenario == 'refused':
                            time.sleep(.4)
                            raise refusal('The stream refused the connection')
                        time.sleep(2.5)
                    return real(action, arguments, *rest, **kw)
                with mock.patch.object(next_window_module, 'START_GRACE_S', 1.0), \
                        mock.patch.object(session_client, 'request', side_effect=client):
                    buttons[0].emit('clicked')
                    pump(.15)
                    chips = env.labels(window, 'Connecting')
                    chip = chips[0] if chips else None
                    wait(lambda: bool(env.buttons(window, 'Try again')), 4)
                    retry = env.buttons(window, 'Try again')
                    card = None
                    if retry:
                        node = retry[0].get_parent()
                        while node is not None and len([w for w in widgets(node) if isinstance(w, Gtk.Label)
                                                        and w.get_mapped() and w.get_text()]) < 2:
                            node = node.get_parent()
                        card = node
                    pump(.3)
                    tag = 'acc-wp2w-u2-%s-%dx%d' % (scenario, width, height)
                    shots.append(str(env.shot(window, tag)))
                    a, b = bounds(card, window), bounds(chip, window)
                    check('U2 %s start at %dx%d: the failure card does not sit over the Connecting chip'
                          % (scenario, width, height), card is not None and chip is not None and not overlaps(a, b),
                          {'card': a, 'chip': b, 'chip text': chip and chip.get_text(),
                           'chip mapped': chip and chip.get_mapped(), 'card found': card is not None})
                    wait(lambda: window.pending is None or window.pending.get('inflight') is False, 5)
                pump(.5)
                window.close()
                pump(.3)
                wait(lambda: not env.controller.transition, 8)
                if env.controller.active:
                    env.service.execute('leave', {})
                    settle(.8)
        report['failure_card_shots'] = shots

    @section('U3')
    def now_name_once():
        def duplicates(window, name):
            folded = name.casefold()
            def path(w):
                names, node = [], w.get_parent()
                while node is not None and len(names) < 4:
                    names.append(type(node).__name__)
                    node = node.get_parent()
                return '/'.join(names)
            # The oracle counts text labels that say exactly the name (the title and the what-plays line); a control's
            # label (the Mix button summary, "Name" or "Name · Scene") and labels that only contain it are reported only.
            contains = [(w.get_text(), path(w)) for w in mapped_labels(window)
                        if not in_toast(w) and folded in w.get_text().casefold()]
            note('labels containing %r: %r' % (name, contains))
            def in_button(w):
                node = w.get_parent()
                while node is not None:
                    if isinstance(node, Gtk.Button):
                        return True
                    node = node.get_parent()
                return False
            return [(w.get_text(), path(w)) for w in mapped_labels(window) if not in_toast(w) and not in_button(w)
                    and w.get_text().strip().casefold() == folded]
        with env.controller.lock:
            env.controller.last_session = None
        window = env.launch(page='listen')
        radio_tune_buttons(window)[0].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None and env.controller.active is not None, 8)
        pump(1.2)
        playing = window.room_name.get_text()
        found = duplicates(window, playing)
        env.shot(window, 'acc-wp2w-u3-ad-hoc-room')
        check('U3a a room named after what plays (Tune in, no room) shows the name once in Now',
              window.current_page() == 'now' and playing and len(found) == 1, (playing, found))
        # A saved room named after what plays.
        env.service.execute('save', {'name': playing})
        settle(1.5)
        env.service.execute('leave', {})
        settle(1.0)
        variant = next((v for v in env.service.execute('inspect', {}).get('variants') or [] if v.get('name') == playing), None)
        if variant is not None:
            env.service.execute('enter', {'variant_id': variant['id']})
            settle(2.0)
            window.show_now()
            pump(1.5)
            found = duplicates(window, playing)
            env.shot(window, 'acc-wp2w-u3-saved-room-named-after-music')
            check('U3b a saved room named after what plays shows the name once in Now',
                  window.current_page() == 'now' and len(found) == 1, (playing, found, env.controller.active and env.controller.active.get('name')))
        else:
            check('U3b fixture: the room saved under the playing name exists', False)

    @section('U4')
    def new_station_dialog():
        probe = REPO / 'tests' / 'probes' / 'listening-listen-ui.py'
        out = env.output / 'o3-evidence'
        out.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in os.environ.items()
                 if k not in ('DISPLAY', 'LISTENING_PROBE_ISOLATED', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR',
                              'WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE')}
        clean['HOME'] = tempfile.mkdtemp(prefix='acc-o3-home-')
        try:
            run = subprocess.run([sys.executable, str(probe), '--source', str(SOURCE), '--output', str(out)], env=clean,
                                 capture_output=True, text=True, timeout=900)
            log = run.stdout + run.stderr
            code = run.returncode
        except subprocess.TimeoutExpired as error:
            log, code = str(error), 'timeout'
        (env.output / 'o3-evidence.log').write_text(log)
        check('U4b the o3 evidence probe has no NewStationDialog "unknown signal name: response" TypeError',
              'unknown signal name' not in log, [line for line in log.splitlines() if 'signal' in line][:3])
        check('U4c ... and runs to completion (exit 0)', code == 0, (code, log.strip().splitlines()[-3:]))

    # =========================================================================== S: O9 race guarantees kept
    @section('S')
    def races():
        import next_station
        env.start_station_room()
        env.service.execute('leave', {})
        settle(1.0)
        window = env.launch(page='listen')
        station = next(item for item in env.controller.stations() if item['name'] == 'Morning lift')
        window.listen.show_station(station)
        pump(.8)
        real = session_client.request
        moods = {m['key']: m for m in next_station.MOODS}

        def shown(key):
            group = window.listen.mood_groups[key]
            index = group.get_active()
            return moods[key]['choices'][index][0] if 0 <= index < len(moods[key]['choices']) else None

        def other(key):
            current = next_station.choices(window.listen.station)[key]
            return next(i for i, (text, _v) in enumerate(moods[key]['choices']) if text != current)

        # S1: a slow save (4 s, beyond the old pending timeout) keeps the chosen value shown.
        index = other('energy')
        chosen = moods['energy']['choices'][index][0]

        def slow_save(action, arguments=None, *rest, **kw):
            if action == 'station.save':
                time.sleep(4.0)
            return real(action, arguments, *rest, **kw)
        with mock.patch.object(session_client, 'request', side_effect=slow_save):
            window.listen.mood_groups['energy'].set_active(index)
            seen = []
            deadline = time.monotonic() + 3.8
            while time.monotonic() < deadline:
                pump(.1)
                seen.append((next_station.choices(window.listen.station)['energy'], shown('energy')))
            pump(1.5)
        settle(.8)
        stored = next_station.choices(next(item for item in env.controller.stations() if item['id'] == station['id']))
        check('S1 a slow station save (4 s) keeps the chosen value shown throughout',
              seen and all(page == chosen and widget == chosen for page, widget in seen), sorted(set(seen)) + [chosen])
        check('S2 ... and lands', stored['energy'] == chosen, stored)
        # S3: a quick second choice, made while the first (slow) save is in flight, keeps the first.
        pump(1.0)
        first_index, second_index = other('energy'), other('familiarity')
        first = moods['energy']['choices'][first_index][0]
        second = moods['familiarity']['choices'][second_index][0]
        calls = []

        def first_slow(action, arguments=None, *rest, **kw):
            if action == 'station.save':
                calls.append(time.monotonic())
                result = real(action, arguments, *rest, **kw)
                if len(calls) == 1:
                    time.sleep(2.5)      # the controller applied it; its answer is slow
                return result
            return real(action, arguments, *rest, **kw)
        s3_mark = len(env.log)
        with mock.patch.object(session_client, 'request', side_effect=first_slow):
            window.listen.mood_groups['energy'].set_active(first_index)
            pump(.15)
            window.listen.mood_groups['familiarity'].set_active(second_index)
            seen = []
            deadline = time.monotonic() + 3.5
            while time.monotonic() < deadline:
                pump(.1)
                seen.append((shown('energy'), shown('familiarity')))
            pump(1.5)
        settle(1.0)
        stored = next_station.choices(next(item for item in env.controller.stations() if item['id'] == station['id']))
        check('S3 a quick second choice during a slow first save keeps the first one shown',
              seen and all(pair == (first, second) for pair in seen), sorted(set(seen)) + [(first, second)])
        sent = [(a.get('station') or {}).get('preferences') for action, a in env.log[s3_mark:] if action == 'station.save']
        check('S4 ... and both choices are stored in the end', stored['energy'] == first and stored['familiarity'] == second,
              (stored, first, second, sent))

    # =========================================================================== Z: push updates (criterion 5)
    @section('Z')
    def push_updates():
        """Criterion 5, against the observable effect (written before WP2-PROTOCOL merged): a real session socket
        served in-process by session_service.make_server, reached through the real session_client stream API;
        commands still reach the controller through the probe's request double, which fails while 'down'."""
        import socket as socket_module
        import session_service
        import next_mini
        api = [name for name in dir(session_client) if 'subscribe' in name.lower()]
        note('session_client subscribe API: %r' % api)
        tree = ast.parse((SOURCE / 'next_window.py').read_text())
        default_poll = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
                             and getattr(n.targets[0], 'id', '') == 'POLL_MS' and isinstance(n.value, ast.Constant)), None)
        note('next_window POLL_MS default in source: %r (the probe environment set 250)' % default_poll)
        folder = Path(tempfile.mkdtemp(prefix='acc-stream-'))
        os.chmod(folder, 0o700)
        path = folder / 'session.sock'
        state = {'down': False, 'server': None, 'lock': None, 'conns': []}
        real_request = session_client.request
        attempts = []
        executed = []
        real_execute = env.service.execute

        def recording(action, arguments=None):
            executed.append(action)
            return real_execute(action, arguments)

        def up():
            server, lock = session_service.make_server(path, env.service)
            original = server.process_request

            def process(request, address):
                state['conns'].append(request)
                return original(request, address)
            server.process_request = process
            threading.Thread(target=server.serve_forever, daemon=True).start()
            state.update(server=server, lock=lock, down=False)

        def down():
            state['down'] = True
            server = state['server']
            server.shutdown()
            server.server_close()
            for conn in state['conns']:
                try:
                    conn.shutdown(socket_module.SHUT_RDWR)
                    conn.close()
                except OSError:
                    pass
            state['conns'].clear()
            os.close(state['lock'])
            try:
                path.unlink()
            except OSError:
                pass

        def client(action, arguments=None, *rest, **kw):
            attempts.append((action, time.monotonic()))
            if state['down']:
                raise ConnectionRefusedError(111, 'Connection refused')
            return real_request(action, arguments, *rest, **kw)

        def reach(read, target, limit=1.5):
            started = time.monotonic()
            env.service.execute('room_volume', {'value': target})
            while time.monotonic() - started < limit:
                pump(.02)
                if abs(read() - target) < 1.5:
                    return time.monotonic() - started
            return None

        env.start_station_room()
        active_before = (env.controller.active or {}).get('id')
        os.environ['LISTENING_SESSION_SOCKET'] = str(path)
        up()
        if default_poll is not None:
            next_window_module.POLL_MS = default_poll
        host = None
        try:
            with mock.patch.object(session_client, 'request', side_effect=client), \
                    mock.patch.object(env.service, 'execute', side_effect=recording):
                window = env.launch(page=None)
                window.show_now()
                settle(2.0)
                levels = [33, 58, 41, 66, 47]
                times = [reach(window.volume.get_value, level) for level in levels]
                for _ in range(2):
                    pump(.4)
                ok = [t for t in times if t is not None]
                median = sorted(ok)[len(ok) // 2] if len(ok) == len(times) else None
                report['stream_latency'] = times
                check('Z1 a controller change reaches Now promptly through the stream (all 5 under 0.5 s, median under 0.3 s)',
                      median is not None and max(ok) < .5 and median < .3, times)
                window.open_mini()
                pump(1.0)
                mini = window.mini_window.player if window.mini_window is not None else None
                times = [reach(mini.volume.get_value, level) for level in (52, 36)] if mini is not None else [None]
                check('Z2 the mini window follows promptly too (under 0.5 s)', all(t is not None and t < .5 for t in times), times)
                if window.mini_window is not None:
                    window.mini_window.close()
                pump(.5)
                mark = len(attempts)
                pump(6.0)
                idle = [a for a, _t in attempts[mark:] if a == 'inspect']
                check('Z3 while the stream is up, the window does not keep polling (at most one read in 6 s idle)',
                      len(idle) <= 1, len(idle))
                # Outage: the controller socket goes away; polls fall back to 5 s or more.
                down()
                outage = time.monotonic()
                pump(16.0)
                reads = [t for a, t in attempts if a == 'inspect' and t >= outage]
                gaps = [round(b - a, 2) for a, b in zip(reads, reads[1:])]
                report['outage_reads'] = [round(t - outage, 2) for t in reads]
                check('Z4 while the stream is down the window falls back to polling (it still tries)', len(reads) >= 1,
                      report['outage_reads'])
                check('Z5 ... at 5 s or more between reads', len(reads) >= 2 and min(gaps) >= 4.8 if len(reads) >= 2 else bool(reads),
                      {'reads at': report['outage_reads'], 'gaps': gaps})
                up()
                back = time.monotonic()
                recovered = wait(lambda: window.offline_bar is not None and not window.offline_bar.get_reveal_child(), 14)
                pump(max(0., 12. - (time.monotonic() - back)))
                times = [reach(window.volume.get_value, level) for level in (44, 61, 39)]
                check('Z6 when the controller returns the window recovers (no offline banner)', recovered)
                check('Z7 ... and changes reach it promptly again (stream reconnected, all under 0.5 s)',
                      all(t is not None and t < .5 for t in times), times)
                # The Waybar popover host follows the stream as well.
                host = next_mini.PopoverHost(env.app, client, launch=lambda arguments: None)
                pump(2.0)
                times = [reach(host.window.player.volume.get_value, level) for level in (55, 35, 62)]
                check('Z8 the popover mini player follows promptly through the stream (all under 0.5 s)',
                      all(t is not None and t < .5 for t in times), times)
            starts = [a for a in executed + [a for a, _t in attempts]
                      if a in ('music', 'online', 'link', 'station.start', 'station.replay', 'enter', 'return', 'resume')]
            check('Z9 nothing starts playback: no start command and the same room throughout',
                  not starts and (env.controller.active or {}).get('id') == active_before,
                  (starts, active_before, (env.controller.active or {}).get('id')))
        finally:
            if host is not None:
                try:
                    host.window.close()
                except Exception:
                    pass
            if not state['down']:
                down()
            os.environ.pop('LISTENING_SESSION_SOCKET', None)
            next_window_module.POLL_MS = 250


    # =========================================================================== W: the Waybar popover (decision 2026-11-20)
    # A user report ("It doesn't crash, but window closes when losing focus", Hyprland, follow_mouse=1) led to the
    # decision: the popover does not close because it lost focus; it is one instance at a time, and a second Waybar
    # left-click (`listening-roomctl popover` again) while it is open closes it. Each click is a real process running
    # roomctl.popover(), on this probe's private display and D-Bus, against a session socket served in-process. The
    # child reports its visible toplevel windows from a timer it adds itself (no reliance on implementer names).
    @section('W')
    def waybar_popover():
        import session_service
        folder = Path(tempfile.mkdtemp(prefix='acc-popover-'))
        os.chmod(folder, 0o700)
        sock = folder / 'session.sock'
        server, lock = session_service.make_server(sock, env.service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        fakebin = folder / 'bin'
        fakebin.mkdir()
        (fakebin / 'systemctl').write_text('#!/bin/sh\nexit 0\n')   # never reach a user manager, whatever happens
        os.chmod(fakebin / 'systemctl', 0o755)
        child_env = dict(os.environ, LISTENING_SESSION_SOCKET=str(sock), GDK_BACKEND='x11', GSK_RENDERER='cairo',
                         PYTHONPATH=os.pathsep.join([str(SOURCE), str(REPO / 'tests')]),
                         PATH=str(fakebin) + os.pathsep + os.environ.get('PATH', ''))
        child_env.pop('WAYLAND_DISPLAY', None)
        child_env.pop('HYPRLAND_INSTANCE_SIGNATURE', None)
        script = (
            'import sys, threading\n'
            'import gi\n'
            'gi.require_version("Gtk", "4.0")\n'
            'from gi.repository import GLib, Gtk\n'
            'def report():\n'
            '    shown = [w for w in Gtk.Window.list_toplevels() if w.get_visible()]\n'
            '    print("WINDOWS %d" % len(shown), flush=True)\n'
            '    return True\n'
            'GLib.timeout_add(200, report)\n'
            'sys.argv = ["listening-roomctl", "popover"]\n'
            'import roomctl\n'
            'code = roomctl.popover()\n'
            'print("EXIT %r" % (code,), flush=True)\n')
        procs = []

        def click():
            proc = subprocess.Popen([sys.executable, '-c', script], env=child_env, cwd=str(folder),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            lines = []
            threading.Thread(target=lambda: [lines.append((time.monotonic(), line.strip())) for line in proc.stdout],
                             daemon=True).start()
            procs.append((proc, lines))
            return proc, lines

        def shown(lines, since=0.):
            counts = [int(text.split()[1]) for t, text in lines if text.startswith('WINDOWS') and t >= since]
            return counts[-1] if counts else None

        def until(predicate, seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if predicate():
                    return True
                pump(.1)
            return bool(predicate())
        try:
            first, first_lines = click()
            opened = until(lambda: shown(first_lines) == 1, 15)
            check('W0 fixture: a Waybar left-click opens one popover window', opened, first_lines[-4:])
            pump(1.0)
            mark = time.monotonic()
            second, second_lines = click()
            until(lambda: second.poll() is not None, 10)
            pump(1.0)
            most_first = max([int(x.split()[1]) for t, x in first_lines if x.startswith('WINDOWS') and t >= mark] or [0])
            most_second = max([int(x.split()[1]) for _t, x in second_lines if x.startswith('WINDOWS')] or [0])
            check('W1 one instance at a time: the second click opens no second popover and its process ends promptly',
                  second.poll() is not None and most_second == 0 and most_first <= 1,
                  {'second exit': second.poll(), 'second max windows': most_second, 'first max windows': most_first,
                   'second output': [x for _t, x in second_lines][-4:]})
            closed = until(lambda: first.poll() is not None or shown(first_lines, time.monotonic() - .3) == 0, 4)
            check('W2 a second left-click while the popover is open closes it (a toggle)', closed,
                  {'first running': first.poll() is None, 'first windows now': shown(first_lines)})
            pump(1.0)
            third, third_lines = click()
            again = until(lambda: shown(third_lines) == 1 or (first.poll() is None and shown(first_lines) == 1), 15)
            total = (shown(third_lines) or 0) + ((shown(first_lines) or 0) if first.poll() is None else 0)
            check('W3 after it closed, the next left-click opens it again (exactly one popover)', again and total == 1,
                  {'third windows': shown(third_lines), 'first windows': shown(first_lines), 'first running': first.poll() is None})
        finally:
            for proc, _lines in procs:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            server.shutdown()
            server.server_close()
            os.close(lock)
        runbook = (REPO / 'docs' / 'runbooks' / 'listening-room-session.md').read_text()
        flat = ' '.join(runbook.split())
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', flat)
                     if re.search(r'\bclos', s, re.I) and re.search(r'\b(los(e|es|ing)|lost)\s+(the\s+|its\s+)?focus', s, re.I)]
        check('W4 the runbook no longer says the popover closes when it loses focus', not sentences, sentences[:2])

    # =========================================================================== G: checks (criterion 6)
    @section('G')
    def hygiene():
        def git(*args):
            return subprocess.run(['git', '-C', str(REPO)] + list(args), capture_output=True, text=True, timeout=60)
        known = git('cat-file', '-e', BASE + '^{commit}').returncode == 0
        if not known:
            check('G0 the base commit %s is available to compare against' % BASE, False)
            return
        added = 0
        for line in git('diff', '--numstat', BASE, '--', 'CHANGELOG.md').stdout.splitlines():
            parts = line.split('\t')
            if parts and parts[0].isdigit():
                added += int(parts[0])
        check('G1 the CHANGELOG has an entry beyond %s' % BASE, added > 0, added)
        changed = git('diff', '--name-only', '--diff-filter=MD', BASE, '--', 'tests/probes/*-acceptance.py',
                      'tests/test_listening_*_oracles.py').stdout.split()
        mine = 'tests/probes/listening-wp2-window-acceptance.py'
        changed = [path for path in changed if path != mine]
        check('G2 no existing acceptance probe or oracle suite was edited or removed', not changed, changed)

    # =========================================================================== T: threads and one handoff (criterion 2)
    # Evaluated last, over everything the sections above did (polls, commands, preferences, mini, the popover host,
    # New station, Tune in, station saves).
    @section('T')
    def threads():
        check('T0 instrumentation saw GTK calls (sanity: %d wrapped calls)' % GTK_CALLS[0], GTK_CALLS[0] > 1000, GTK_CALLS[0])
        grouped = {}
        for label, place in OFF_MAIN:
            grouped.setdefault(label + ' @ ' + place, 0)
            grouped[label + ' @ ' + place] += 1
        check('T1 nothing touched GTK off the main thread during the whole run', not OFF_MAIN,
              sorted(grouped.items(), key=lambda kv: -kv[1])[:8])
        keys = {}
        for key, place, label in HANDOFFS:
            keys.setdefault(key, set()).add(label)
        report['handoffs'] = {k: sorted(v) for k, v in keys.items()}
        check('T2 every worker-to-main handoff goes through one helper (%d handoffs seen)' % len(HANDOFFS),
              len(HANDOFFS) > 0 and len(keys) == 1, sorted(keys)[:12])
        sites = []
        for path in sorted(SOURCE.glob('next_*.py')):
            tree = ast.parse(path.read_text())
            tops = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for klass in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                tops += [n for n in klass.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for top in tops:
                calls = {n.func.attr for n in ast.walk(top) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
                if 'idle_add' in calls and calls & {'add_done_callback', 'submit'}:
                    sites.append('%s:%s' % (path.name, top.name))
        outer = sorted(set(sites))
        report['handoff_sites_static'] = outer
        check('T3 no hand-written submit/done-callback + idle_add handoffs remain in next_*.py (at most the helper itself)',
              len(outer) <= 1, outer[:12])

    failed = [name for name, ok in checks if not ok]
    (env.output / 'acceptance-wp2-window.json').write_text(json.dumps(
        {'checks': checks, 'pending': pending_checks, 'report': report}, indent=1, default=str))
    print('%d passed, %d failed, %d pending' % (len(checks) - len(failed), len(failed), len(pending_checks)))
