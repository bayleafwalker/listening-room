"""Probe the next window's Rooms (O2) with real GTK and the real session controller.

State is isolated and playback is a fake backend; nothing touches the desktop session.
Every controller command is logged, so browsing, filtering, Surprise me and previews are
proven inert and the explicit starts (Enter room, Enter or double-click, Back to) are
proven to be the only commands sent. Writes the O2 evidence screenshots.

  scripts/listening-dev.sh shell     # then, inside it:
  python3 tests/probes/listening-next-rooms.py --output DIR

Without a DISPLAY the probe re-executes itself on a private Xvfb and D-Bus with
portals disabled, exactly as listening-next-ui.py does.
"""
import argparse
import copy
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', default=str(Path(__file__).resolve().parents[2] / 'src'))
parser.add_argument('--output', default=str(Path(tempfile.gettempdir()) / 'listening-ui-review'))
args = parser.parse_args()

if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
    runtime = tempfile.mkdtemp(prefix='listening-next-runtime-')
    os.chmod(runtime, 0o700)
    environment = {key: value for key, value in os.environ.items() if key not in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')}
    environment.update(XDG_RUNTIME_DIR=runtime, GDK_BACKEND='x11', GDK_DEBUG='no-portals', GSK_RENDERER='cairo',
                       LISTENING_PROBE_ISOLATED='1')
    command = ['xvfb-run', '-a', '-s', '-screen 0 1440x1000x24', 'dbus-run-session', sys.executable] + sys.argv
    try:
        sys.exit(subprocess.call(command, env=environment))
    finally:
        shutil.rmtree(runtime, ignore_errors=True)

source = Path(args.source).resolve()
sys.path.insert(0, str(source))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory(prefix='listening-next-') as folder:
    os.environ['HOME'] = folder
    for key, suffix in [('XDG_STATE_HOME', 'state'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache')]:
        path = Path(folder) / suffix
        path.mkdir(mode=0o700)
        os.environ[key] = str(path)
    os.environ['GTK_A11Y'] = 'none'
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gdk, GLib, Gtk
    import ambience
    import audio_devices
    import listening_hub as hub
    import next_model
    import next_widgets
    import room_catalogue
    import room_session
    import session_client
    import session_service
    from test_listening_session import FakeBackend
    from unittest.mock import patch

    class ProbeBackend(FakeBackend):
        """The session test double, with names the real players report."""
        def __init__(self):
            super().__init__()
            self.fail_atmosphere = None

        def start_atmosphere(self, selection, cancel):
            if self.fail_atmosphere and selection.get('kind') != 'none':
                raise ValueError(self.fail_atmosphere)
            super().start_atmosphere(selection, cancel)
            import atmosphere_catalogue
            sound = next((x for x in atmosphere_catalogue.SOUNDS if x['id'] == selection.get('source_id')), {})
            if sound:  # the real player reports these from the sound catalogue
                self.state['atmosphere'].update(name=sound['name'], outdoor=sound['outdoor'])

        def start_picture(self, picture, cancel=None):
            super().start_picture(picture, cancel)
            preset = next((p for p in ambience.presets() if p['id'] == picture.get('preset_id')), {})
            self.state['picture'].update(name=preset.get('name', 'Scene'), placement=picture.get('placement'))

    backend = ProbeBackend()
    controller = room_session.Controller(backend, Path(folder) / 'controller')
    controller._home_action = lambda *_: None
    controller._route_preferred = lambda: None
    service = session_service.Service(controller)
    log = []
    system_volume = {'percent': 40}
    outputs = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True},
               {'id': 'buds', 'name': 'buds', 'description': 'Headphones', 'default': False, 'available': True}]

    def request(action, arguments=None, *_args, **_kw):
        arguments = arguments or {}
        log.append((action, copy.deepcopy(arguments)))
        return service.execute(action, arguments)

    patches = [patch.object(session_client, 'request', side_effect=request),
               patch.object(audio_devices, 'outputs', return_value=outputs),
               patch.object(audio_devices, 'test_sound', side_effect=lambda name=None: log.append(('test_sound', name))),
               patch.object(hub, 'system_volume', side_effect=lambda: dict(system_volume)),
               patch.object(hub, 'set_volume', side_effect=lambda value, **kw: system_volume.update(percent=value))]
    for item in patches:
        item.start()
    import next_window
    next_window.POLL_MS = 250
    app = next_window.NextApp()
    app.register(None)
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    context = GLib.MainContext.default()

    def pump(seconds=.4):
        deadline = time.monotonic() + seconds
        last = 0
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if time.monotonic() - last > .25:
                last = time.monotonic()
                controller.refresh()
            time.sleep(.01)

    def settle():
        controller.worker.submit(lambda: None).result(timeout=3)
        pump(.8)

    def widgets(root):
        yield root
        child = root.get_first_child()
        while child:
            yield from widgets(child)
            child = child.get_next_sibling()

    def texts(root):
        for widget in widgets(root):
            for getter in ('get_label', 'get_text', 'get_title', 'get_subtitle', 'get_tooltip_text'):
                value = getattr(widget, getter, lambda: None)()
                if isinstance(value, str) and value:
                    yield value

    def launch(width=1200, height=800):
        window = next_window.NextWindow(app)
        window.set_default_size(width, height)
        window.present()
        pump(1.0)
        return window

    def shot(window, name, crop=None):
        pump(.4)
        from PIL import ImageGrab
        width, height = window.get_width(), window.get_height()
        assert width and height, 'window did not construct'
        ImageGrab.grab(bbox=(0, 0, width, height), xdisplay=os.environ['DISPLAY']).save(output / (name + '.png'))
        return output / (name + '.png')

    def visible_controls(root):
        names = []
        for widget in widgets(root):
            if isinstance(widget, (Gtk.Button, Gtk.Scale, Gtk.MenuButton)) and widget.get_mapped() and widget.get_child_visible():
                if isinstance(widget, Gtk.ScrolledWindow):
                    continue
                names.append(widget.get_tooltip_text() or (widget.get_label() if hasattr(widget, 'get_label') else '') or type(widget).__name__)
        return names

    def commands(since=0):
        return [entry for entry in log[since:] if entry[0] != 'inspect']

    FORBIDDEN = ('Main audio', 'Background audio', 'Picture', 'Retry picture', 'Refresh')
    REQUIRED = ('Pause the room', 'Next track', 'Room volume', 'Mix', 'Output', 'Timer', 'Main menu', 'Back to rooms')
    PLAYBACK_COMMANDS = {'enter', 'return', 'music', 'online', 'station.start', 'station.replay', 'link', 'resume', 'quiet',
                         'stop', 'leave', 'wind_down', 'layer', 'picture', 'atmosphere', 'room_volume', 'settings', 'save'}

    def assert_vocabulary(window, where):
        for text in texts(window):
            for word in FORBIDDEN:
                assert word not in text, '%s shows %r in %r' % (where, word, text)
            if 'Workspace 9' in text:
                assert 'Workspace 9' == text, 'Workspace 9 outside a placement value: %r' % text

    def assert_layout(window, where):
        width, height = window.get_width(), window.get_height()
        for widget in widgets(window):
            if isinstance(widget, Gtk.ScrolledWindow) and widget.get_mapped():
                adjustment = widget.get_hadjustment()
                assert adjustment.get_upper() <= adjustment.get_page_size() + 1, '%s scrolls horizontally' % where
        for widget in widgets(window):
            if isinstance(widget, (Gtk.Button, Gtk.Scale, Gtk.MenuButton)) and widget.get_mapped() and widget.get_child_visible():
                ok, bounds = widget.compute_bounds(window)
                if not ok:
                    continue
                # Anything inside a scrolled page is allowed to extend vertically only.
                scrolled = any(isinstance(p, Gtk.ScrolledWindow) for p in parents(widget))
                assert bounds.get_x() >= -1 and bounds.get_x() + bounds.get_width() <= width + 1, \
                    '%s: control %r is clipped horizontally (%s)' % (where, widget.get_tooltip_text(), bounds.get_x() + bounds.get_width())
                if not scrolled:
                    assert bounds.get_y() >= -1 and bounds.get_y() + bounds.get_height() <= height + 1, \
                        '%s: control %r is clipped vertically' % (where, widget.get_tooltip_text())

    def parents(widget):
        parent = widget.get_parent()
        while parent:
            yield parent
            parent = parent.get_parent()


    def find_button(root, text):
        for widget in widgets(root):
            if isinstance(widget, Gtk.Button) and widget.get_mapped() and (widget.get_label() == text or text in list(texts(widget))):
                return widget
        return None

    def labels(root, mapped=True):
        return [w.get_text() for w in widgets(root) if isinstance(w, Gtk.Label) and (w.get_mapped() or not mapped)]

    def card_children():
        child, found = window.cards.get_first_child(), []
        while child:
            found.append(child)
            child = child.get_next_sibling()
        return found

    def card_names():
        return [window.rooms.card_for[child]['name'] for child in card_children()]

    def card(name):
        return next(child for child in card_children() if window.rooms.card_for[child]['name'] == name)

    def gestures(widget):
        found = []
        for item in widgets(widget):
            for controller in item.observe_controllers():
                if isinstance(controller, Gtk.GestureClick):
                    found.append(controller)
        return found

    def click_card(name):
        """One click: the first half of a possible double-click, so the preview opens after a short wait."""
        gesture = [g for g in gestures(card(name)) if g.get_button() == 1][0]
        gesture.emit('pressed', 1, 10., 10.)
        gesture.emit('released', 1, 10., 10.)
        pump(.6)

    def double_click_card(name):
        child = card(name)
        gesture = [g for g in gestures(child) if g.get_button() == 1][0]
        gesture.emit('pressed', 1, 10., 10.)
        gesture.emit('released', 1, 10., 10.)
        gesture.emit('pressed', 2, 10., 10.)
        gesture.emit('released', 2, 10., 10.)
        window.cards.emit('child-activated', child)  # GTK's own double-click activation
        settle()

    def on_preview():
        return window.rooms.stack.get_visible_child_name().startswith('preview')

    def primaries():
        return [w for w in widgets(window) if isinstance(w, Gtk.Button) and w.has_css_class('suggested-action') and w.get_mapped()]

    def columns():
        xs = set()
        for child in card_children():
            ok, bounds = child.compute_bounds(window)
            if ok and child.get_mapped():
                xs.add(round(bounds.get_x()))
        return len(xs)

    def escape():
        return window.key_pressed(None, Gdk.KEY_Escape, 0, 0)

    def dialog_of(window):
        dialog = window.get_visible_dialog()
        assert dialog is not None, 'no dialog is open'
        return dialog

    def finish_dialog(dialog, response):
        dialog.emit('response', response)
        dialog.force_close()
        pump(.5)

    def check_icon_names(where):
        bad = []
        for widget in widgets(window.rooms.widget):
            if isinstance(widget, (Gtk.Button, Gtk.MenuButton, Gtk.Switch)) and widget.get_mapped():
                label_text = widget.get_label() if hasattr(widget, 'get_label') else None
                if not (label_text or widget.get_tooltip_text() or any(isinstance(w, Gtk.Label) and w.get_text() for w in widgets(widget))):
                    bad.append(type(widget).__name__)
        assert not bad, '%s: controls without a name: %r' % (where, bad)

    def shot_to(name):
        return shot(window, name)

    try:
        # ------------------------------------------------------------------ first run: no hero, a prompt, no command
        window = launch()
        assert window.browse.get_visible_child_name() == 'rooms'
        assert not window.rooms.hero.get_visible() and not window.hero_button.get_visible(), 'no hero on first run'
        assert 'Pick a room to begin' in labels(window)
        assert card_names() == [room['name'] for room in room_catalogue.rooms()], card_names()
        assert not commands() and not primaries()
        assert columns() == 4, columns()
        assert_layout(window, 'Rooms first run 1200x800')
        assert_vocabulary(window, 'Rooms first run')
        shot_to('o2-03-rooms-first-run')
        window.close()
        pump(.2)

        # ------------------------------------------------------------------ the Return hero and the filters
        controller.enter({'room_id': 'midnight-library', 'with_view': True})
        settle()
        controller.leave(0)
        settle()
        log.clear()
        window = launch()
        assert window.hero_button.get_label() == 'Return to Midnight library'
        assert primaries() == [window.hero_button], 'Return is the single primary action'
        assert any(text in ('Just now', 'Earlier today', 'Yesterday') for text in labels(window)), labels(window)
        assert columns() == 4, 'four columns at 1200 px, got %d' % columns()
        assert_layout(window, 'Rooms 1200x800')
        assert_vocabulary(window, 'Rooms')
        check_icon_names('Rooms')
        shot_to('o2-01-rooms-hero')
        assert [c for c in window.rooms.chips] == ['all', 'yours', 'focus', 'reading', 'late', 'lively', 'new']
        assert [chip.get_label() for chip in window.rooms.chips.values()] == \
            ['All', 'Yours', 'Focus', 'Reading', 'Late night', 'Lively', 'Something new']
        window.rooms.chips['late'].set_active(True)
        pump(.5)
        assert card_names() == ['Night drive', 'After hours', 'Midnight library', 'Atomic lounge'] or \
            sorted(card_names()) == ['After hours', 'Atomic lounge', 'Midnight library', 'Night drive'], card_names()
        shot_to('o2-02-rooms-late-night')
        window.rooms.chips['yours'].set_active(True)
        pump(.4)
        assert card_names() == [] and 'Nothing saved yet' in ' '.join(labels(window))
        window.rooms.chips['new'].set_active(True)
        pump(.4)
        assert 'Finnish discoveries' in card_names() and 'Midnight library' not in card_names(), card_names()
        assert not commands(), 'filtering sent %r' % commands()

        # Surprise me respects the chip, opens the preview and never enters.
        window.rooms.chips['late'].set_active(True)
        pump(.3)
        window.rooms.surprise_button.emit('clicked')
        pump(.6)
        assert on_preview() and window.rooms.opened['name'] in ('Night drive', 'After hours', 'Midnight library', 'Atomic lounge')
        assert not commands(), 'Surprise me sent %r' % commands()
        assert controller.active is None
        assert escape() and not on_preview(), 'Esc returns to the grid'
        window.rooms.chips['all'].set_active(True)
        pump(.3)

        # ------------------------------------------------------------------ the preview is inert; Enter room is the one start
        click_card('Night drive')
        assert on_preview(), 'a click opens the preview'
        assert not commands(), 'previewing sent %r' % commands()
        shown = labels(window)
        for expected in ('Night drive', 'Radio for the road', "What you'll hear", 'Music', 'Scene', 'Ambience', 'Enter room',
                         'Live radio · programming varies', 'Driving', 'Presenters', 'Landscapes'):
            assert expected in shown, (expected, shown)
        assert any('Nothing starts until you enter' in text or 'fades out' in text for text in shown), shown
        assert [w.get_label() for w in primaries()] == ['Enter room'], [w.get_label() for w in primaries()]
        assert_layout(window, 'Room preview 1200x800')
        assert_vocabulary(window, 'Room preview')
        check_icon_names('Room preview')
        shot_to('o2-05-preview-night-drive')
        print('preview loop playing: %s' % window.rooms.preview_widgets['scene'].playing_motion)
        toggle = window.rooms.preview_widgets['toggle']
        assert toggle.get_active() and toggle.get_sensitive() and toggle.get_tooltip_text() == 'Scene'
        assert not commands(), 'looking at the preview sent %r' % commands()
        assert escape() and not on_preview()
        assert window.rooms.opened is None

        # Keyboard: Space previews a focused card, Enter enters, Esc goes back.
        child = card('Northern quiet')
        window.set_focus(child)
        pump(.2)
        assert window.rooms.card_key(None, Gdk.KEY_space, 0, 0), 'Space previews the focused card'
        pump(.4)
        assert on_preview() and window.rooms.opened['name'] == 'Northern quiet' and not commands()
        assert escape() and not on_preview()
        window.close()
        pump(.2)

        # Scene toggle: off means sound only; Enter room starts exactly one room.
        window = launch()
        click_card('Night drive')
        toggle = window.rooms.preview_widgets['toggle']
        toggle.set_active(False)
        pump(.3)
        assert 'Scene off · sound only' in labels(window)
        mark = len(log)
        window.rooms.preview_widgets['enter'].emit('clicked')
        settle()
        assert [c for c in commands(mark)] == [('enter', {'room_id': 'night-drive', 'with_view': False})], commands(mark)
        assert window.current_page() == 'now' and controller.active['room_id'] == 'night-drive'
        assert controller.active['picture']['enabled'] is False
        assert not on_preview() and window.rooms.opened is None
        controller.leave(0)
        settle()
        window.show_rooms()
        pump(.4)
        click_card('Night drive')
        mark = len(log)
        window.rooms.preview_widgets['enter'].emit('clicked')
        settle()
        assert commands(mark) == [('enter', {'room_id': 'night-drive', 'with_view': True})], commands(mark)
        assert controller.active['picture']['enabled'] is True

        # Playing marker on the active room's card, and Open room for it.
        window.show_rooms()
        pump(.6)
        assert any(t == 'Playing' for t in labels(window))
        assert_layout(window, 'Rooms with Playing 1200x800')
        shot_to('o2-04-card-playing')
        click_card('Night drive')
        assert window.rooms.preview_widgets['enter'].get_label() == 'Open room'
        mark = len(log)
        window.rooms.preview_widgets['enter'].emit('clicked')
        pump(.5)
        assert not commands(mark) and window.current_page() == 'now', 'Open room only opens Now'
        window.show_rooms()
        pump(.4)

        # ------------------------------------------------------------------ switching rooms: the toast goes back
        mark = len(log)
        double_click_card('Midnight library')
        assert [c for c in commands(mark)] == [('enter', {'room_id': 'midnight-library', 'with_view': True})], commands(mark)
        pump(1.0)
        assert controller.active['room_id'] == 'midnight-library' and window.current_page() == 'now'
        back = find_button(window, 'Back to Night drive')
        assert back is not None, 'no Back to Night drive toast: %r' % labels(window)
        mark = len(log)
        back.emit('clicked')
        settle()
        assert commands(mark) == [('enter', {'room_id': 'night-drive'})], commands(mark)
        assert controller.active['room_id'] == 'night-drive'
        # Activating the room you are already in only opens Now.
        window.show_rooms()
        pump(.4)
        mark = len(log)
        double_click_card('Night drive')
        assert not commands(mark) and window.current_page() == 'now'

        # ------------------------------------------------------------------ Save as my room…
        window.show_rooms()
        pump(.3)
        controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass',
                          'name': 'Rain on glass', 'outdoor': True, 'volume': 20})
        settle()
        mark = len(log)
        window.menu_button.popup()
        pump(.3)
        save_item = find_button(window.menu_popover, 'Save as my room…')
        assert save_item is not None and save_item.get_mapped(), 'the menu offers Save as my room…'
        save_item.emit('clicked')
        pump(.6)
        dialog = dialog_of(window)
        assert window.save_entry.get_text() == 'Night drive', window.save_entry.get_text()
        body = labels(dialog)
        for expected in ('Music', 'Ambience', 'Scene', 'Room volume', 'Rain on glass'):
            assert expected in body, (expected, body)
        assert not commands(mark), 'opening the dialog sent %r' % commands(mark)
        window.save_entry.set_text('Rainy reading')
        pump(.3)
        shot_to('o2-07-save-dialog')
        finish_dialog(dialog, 'save')
        settle()
        assert commands(mark) == [('save', {'name': 'Rainy reading'})], commands(mark)
        assert [v['name'] for v in controller.snapshot()['variants']] == ['Rainy reading']
        # Blank names cannot be saved.
        window.save_dialog()
        pump(.5)
        dialog = dialog_of(window)
        window.save_entry.set_text('   ')
        pump(.2)
        assert not dialog.get_response_enabled('save')
        mark = len(log)
        finish_dialog(dialog, 'cancel')
        assert not commands(mark)
        # Saved rooms lead the grid under Yours, and the active one shows Playing.
        window.show_rooms()
        pump(.8)
        assert card_names()[0] == 'Rainy reading' and 'Yours' in labels(window)
        assert_layout(window, 'Rooms with Yours 1200x800')
        shot_to('o2-08-saved-under-yours')
        window.rooms.chips['yours'].set_active(True)
        pump(.4)
        assert card_names() == ['Rainy reading']
        window.rooms.chips['all'].set_active(True)
        pump(.3)

        # Rename and delete live in the saved card's menu.
        saved_card = card('Rainy reading')
        saved_id = window.rooms.card_for[saved_card]['id']
        assert saved_card.menu is not None and card('Night drive').menu is None
        assert saved_card.menu.get_tooltip_text() == 'Options for Rainy reading'
        saved_card.menu.popup()
        pump(.4)
        rename = find_button(saved_card.menu.get_popover(), 'Rename…')
        assert rename is not None
        mark = len(log)
        rename.emit('clicked')
        pump(.5)
        dialog = dialog_of(window)
        entry_widget = next(w for w in widgets(dialog) if isinstance(w, Gtk.Entry))
        assert entry_widget.get_text() == 'Rainy reading'
        assert not dialog.get_response_enabled('rename'), 'an unchanged name cannot be renamed'
        entry_widget.set_text('Quiet reading')
        pump(.2)
        assert dialog.get_response_enabled('rename')
        finish_dialog(dialog, 'rename')
        settle()
        assert commands(mark) == [('rename', {'id': saved_id, 'name': 'Quiet reading'})], commands(mark)
        assert [v['name'] for v in controller.snapshot()['variants']] == ['Quiet reading']
        pump(.6)
        assert card_names()[0] == 'Quiet reading'
        # Delete asks first; Cancel changes nothing.
        saved_card = card('Quiet reading')
        saved_card.menu.popup()
        pump(.3)
        find_button(saved_card.menu.get_popover(), 'Delete…').emit('clicked')
        pump(.5)
        dialog = dialog_of(window)
        assert 'Delete Quiet reading?' in dialog.get_heading()
        mark = len(log)
        finish_dialog(dialog, 'cancel')
        assert not commands(mark) and controller.snapshot()['variants']
        saved_card.menu.popup()
        pump(.3)
        find_button(saved_card.menu.get_popover(), 'Delete…').emit('clicked')
        pump(.5)
        finish_dialog(dialog_of(window), 'delete')
        settle()
        assert [c[0] for c in commands(mark)] == ['delete'] and not controller.snapshot()['variants']
        pump(.6)
        assert 'Quiet reading' not in card_names()
        controller.leave(0)
        settle()
        window.close()
        pump(.2)

        # ------------------------------------------------------------------ classic scenes migrate into Yours
        sources = [item for item in hub.sources() if item['kind'] == 'radio']
        source = sources[0]
        hub.save_json(hub.STATE / 'scenes.json', [{'id': 'user-abc', 'name': 'Road trip', 'source_id': source['id'], 'volume': 35}])
        quiet = len(log)
        window = launch()
        pump(.8)
        assert card_names()[0] == 'Road trip', card_names()
        assert 'Yours' in labels(window)
        assert card('Road trip').menu is None, 'migrated scenes are not editable here'
        window.rooms.chips['yours'].set_active(True)
        pump(.3)
        assert card_names() == ['Road trip']
        assert not commands(quiet)
        mark = len(log)
        double_click_card('Road trip')
        pump(.6)
        assert [c[0] for c in commands(mark)] == ['enter'] and controller.active['name'] == 'Road trip', commands(mark)
        controller.leave(0)
        settle()
        window.close()
        pump(.2)

        (hub.STATE / 'scenes.json').unlink()
        controller.enter({'room_id': 'midnight-library', 'with_view': True})
        settle()
        controller.leave(0)
        settle()

        # ------------------------------------------------------------------ reduced motion: the preview is still art, nothing animates
        controller.apply_settings({'reduced_motion': True})
        settle()
        window = launch()
        click_card('Night drive')
        assert on_preview()
        assert window.rooms.preview_widgets['scene'].media is None, 'reduced motion shows still art'
        assert not Gtk.Settings.get_default().get_property('gtk-enable-animations')
        assert 'A still of the scene. Nothing plays until you enter.' in labels(window)
        assert 'Scene on workspace 9 · still' in labels(window)
        assert_layout(window, 'Room preview reduced motion')
        shot_to('o2-06-preview-reduced-motion')
        window.close()
        pump(.2)
        controller.apply_settings({'reduced_motion': False})
        settle()

        # ------------------------------------------------------------------ 800 px
        window = launch(800, 700)
        assert 2 <= columns() <= 3, columns()
        assert_layout(window, 'Rooms 800x700')
        assert_vocabulary(window, 'Rooms 800')
        shot_to('o2-09-rooms-800')
        click_card('Night drive')
        assert on_preview()
        assert_layout(window, 'Room preview 800x700')
        shot_to('o2-10-preview-800')
        window.close()
        pump(.2)
        window = launch(1000, 700)
        assert columns() == 4, 'four columns at 1000 px, got %d' % columns()
        assert_layout(window, 'Rooms 1000x700')
        window.close()
        pump(.2)
        print('PASS: Rooms hero and first run, mood filters, Surprise me, grid, Room preview, Enter room, Back to, '
              'Save as my room, rename, delete, legacy scenes, reduced motion, 800 px')
    finally:
        for item in reversed(patches):
            item.stop()
        controller.close()
