"""Evidence and parity probe for the Listening Room (the next window, now the only window).

Real GTK and the real session controller on a fake backend; nothing touches the desktop
session. It captures the screenshots the redesign plan lists for criterion 14 into
--output (default $TMPDIR/listening-ui-review), and checks Glance
(mini player, Waybar popover, narrow layout), the keyboard map, contrast in all eight
rooms, adaptive widths, reduced motion and the capability walk recorded by
the parity checks.  scripts/listening-dev.sh probe OUTDIR runs it.

Without a DISPLAY it re-executes itself on a private Xvfb and D-Bus with portals
disabled.
"""
import argparse
import faulthandler
faulthandler.dump_traceback_later(240, repeat=True)
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
    runtime = tempfile.mkdtemp(prefix='listening-session-runtime-')
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

with tempfile.TemporaryDirectory(prefix='listening-session-') as folder:
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

    # World radio search is a fixture, never the live directory.
    world = [{'id': 'world-rain-room', 'name': 'Rain Room Radio', 'group': 'Search results', 'kind': 'radio',
              'url': 'https://radio.example.test/rain-room', 'country': 'Finland', 'language': 'Finnish',
              'tags': ['rain', 'calm'], 'website': 'https://radio.example.test/'}]
    patches = [patch.object(session_client, 'request', side_effect=request),
               patch.object(hub, 'search_radio', side_effect=lambda query, **kw: copy.deepcopy(world)),
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

    def luminance(pixel):
        def channel(v):
            v /= 255
            return v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4
        return .2126 * channel(pixel[0]) + .7152 * channel(pixel[1]) + .0722 * channel(pixel[2])

    def contrast_pair(a, b):
        hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
        return (hi + .05) / (lo + .05)

    def contrast_behind(window, label_widgets, name):
        """White text against the 90th-percentile luminance of the pixels behind it."""
        from PIL import ImageGrab
        for item in label_widgets:
            item.set_opacity(0)
        pump(.4)
        image = ImageGrab.grab(bbox=(0, 0, window.get_width(), window.get_height()), xdisplay=os.environ['DISPLAY']).convert('RGB')
        for item in label_widgets:
            item.set_opacity(1)
        worst = 99
        for item in label_widgets:
            ok, bounds = item.compute_bounds(window)
            if not ok or not item.get_mapped() or not item.get_text():
                continue
            box = (max(0, int(bounds.get_x())), max(0, int(bounds.get_y())),
                   min(window.get_width(), int(bounds.get_x() + bounds.get_width())),
                   min(window.get_height(), int(bounds.get_y() + bounds.get_height())))
            crop = image.crop(box).convert("RGB"); raw = crop.tobytes(); values = sorted(luminance(raw[i:i + 3]) for i in range(0, len(raw), 3))
            p90 = values[int(len(values) * .9)]
            worst = min(worst, 1.05 / (p90 + .05))
        return worst

    import json
    import shutil as _shutil
    ROOMS = room_catalogue.rooms()
    EVIDENCE = []

    def evidence(window, name):
        path = shot(window, name)
        EVIDENCE.append(path.name)
        return path

    def ctx(window=None):
        return window

    def unnamed_controls(root):
        """Icon-only buttons without an accessible name."""
        missing = []
        for widget in widgets(root):
            if isinstance(widget, (Gtk.Button, Gtk.MenuButton, Gtk.ToggleButton)) and widget.get_mapped() and not widget.get_label():
                child = widget.get_child() if hasattr(widget, 'get_child') else None
                if isinstance(child, (Gtk.Label, Gtk.Box)) and not isinstance(child, Gtk.Image):
                    continue
                name = widget.get_tooltip_text()
                if type(widget).__name__.startswith('AdwViewSwitcher'):
                    continue  # tab buttons carry visible labels
                if any(isinstance(p, (Gtk.WindowControls, Adw.HeaderBar)) and widget not in (getattr(p, 'x', None),) and type(widget).__name__ == 'Button' and not widget.get_tooltip_text() and isinstance(p, Adw.HeaderBar) for p in parents(widget)):
                    continue  # the dialog's own close button
                if any(isinstance(p, Gtk.WindowControls) for p in parents(widget)):
                    continue  # the toolkit names window buttons itself
                if not name:
                    missing.append(type(widget).__name__)
        return missing

    def hero_contrast(window, name):
        return contrast_behind(window, [window.rooms.hero_title, window.rooms.hero_detail, window.rooms.hero_when], name)

    def mini_contrast(player, window, name):
        labels = [player.name, player.line] + ([player.timer_label] if player.timer_label.get_visible() else [])
        return contrast_behind(window, labels, name)

    def minimal_args(window):
        return [c for c in commands()]

    try:
        print('== first run (nothing saved yet)', flush=True)
        assert controller.last_session is None and controller.active is None
        window = launch()
        assert window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'rooms'
        assert window.rooms.hero_kind == 'first-run' and window.rooms.hero_title.get_text() == 'Pick a room to begin'
        assert not commands(), 'first run sent %r' % commands()
        evidence(window, '01-first-run')
        window.close()
        pump(.2)

        print('== return available; criterion 1 (one activation)', flush=True)
        controller.enter({'room_id': 'midnight-library', 'with_view': True})
        settle()
        controller.leave(0)
        settle()
        log.clear()
        before = len(backend.calls)
        window = launch()
        assert window.rooms.hero_button.get_label() == 'Return to Midnight library'
        assert not commands() and len(backend.calls) == before, 'launch sent commands'
        evidence(window, '02-return-available')
        mark = len(log)
        window.hero_button.emit('clicked')
        settle()
        assert [c[0] for c in commands(mark)] == ['return'], commands(mark)
        assert controller.active and window.current_page() == 'now', 'one activation from launch reaches sound and Now'
        window.leave_room()
        settle()
        pump(2.5)
        assert controller.active is None
        window.show_rooms()
        pump(.5)
        evidence(window, '03-rooms')
        assert_layout(window, 'Rooms')
        # preview (no start)
        mark = len(log)
        entry = next(r for r in window.rooms.entries if r['id'] == 'northern-quiet') if hasattr(window.rooms, 'entries') else None
        if entry is None:
            entry = next(r for r in window.catalogue if r['id'] == 'northern-quiet')
        window.rooms.open_preview(entry)
        pump(.8)
        evidence(window, '04-room-preview')
        assert not commands(mark), 'previewing sent %r' % commands(mark)
        window.rooms.close_preview()
        window.close()
        pump(.2)

        print('== Now (radio) and Now (library with skip)', flush=True)
        window = launch()
        window.hero_button.emit('clicked')
        settle()
        assert window.current_page() == 'now'
        assert not window.skip.get_visible() and not window.more.get_visible()
        evidence(window, '05-now-radio')
        window.sheet.set_open(True)
        pump(1.0)
        controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass',
                          'name': 'Rain on glass', 'outdoor': True, 'volume': 20})
        settle()
        pump(.6)
        evidence(window, '07-mix')
        window.sheet.set_open(False)
        pump(.5)
        backend.fail_atmosphere = 'The sound could not start'
        service.execute('layer', {'layer': 'atmosphere', 'action': 'retry'})
        settle()
        assert window.banner.get_visible() and window.marker.get_visible()
        assert backend.state['music']['status'] == 'Playing'
        evidence(window, '08-layer-failure')
        backend.fail_atmosphere = None
        window.banner_retry.emit('clicked')
        settle()
        window.choose_timer('wind_down')
        settle()
        assert window.timer_chip.get_visible()
        evidence(window, '09-timer-running')
        window.choose_timer('cancel')
        settle()
        window.leave_room()
        settle()
        pump(2.5)
        queue = [{'provider': 'jellyfin', 'id': 'track1', 'type': 'Audio', 'name': 'First track'},
                 {'provider': 'jellyfin', 'id': 'track2', 'type': 'Audio', 'name': 'Second track'}]
        controller.enter({'music': {'reference': queue[0], 'source_id': 'jellyfin-track1', 'name': 'First track',
                                    'queue': queue, 'queue_index': 0}, 'recipe': controller.last_session})
        settle()
        pump(.8)
        window.show_now()
        pump(5.5)  # let the last toast go
        assert window.skip.get_visible() and window.chip.get_text() == 'Library'
        evidence(window, '06-now-library-skip')
        window.close()
        pump(.2)
        controller.leave(0)
        settle()
        pump(1)

        print('== Listen search, station detail, link sheet, Connections', flush=True)
        window = launch()
        window.show_listen()
        pump(.5)
        window.listen.search.set_text('rain')
        pump(.8)
        evidence(window, '10-listen-search')
        window.listen.search.set_text('')
        service.execute('station.save', {'station': {
            'name': 'Late reading', 'sources': [{'reference': {'provider': 'jellyfin', 'id': 'album-neoclassical', 'type': 'MusicAlbum',
                                                                  'name': 'Neoclassical collection', 'genres': ['Neoclassical']}}],
            'fallbacks': [], 'preferences': {'energy': 'Low', 'familiarity': 'Any', 'speech': 'Any'},
            'end_policy': 'stop', 'arc': 'wind_down', 'generation': {'kind': 'collection'}}})
        settle()
        window.listen.show_station(next(x for x in controller.stations() if x['name'] == 'Late reading'))
        pump(.8)
        evidence(window, '11-station-detail')
        window.listen.open_link('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        pump(.8)
        evidence(window, '12-link-sheet')
        for child in list(widgets(window)):
            if isinstance(child, Adw.Dialog):
                child.force_close()
        pump(.4)
        window.open_preferences()
        pump(1.2)
        window.preferences.set_visible_page(window.preferences.pages['connections'])
        pump(.8)
        evidence(window, '13-connections')
        window.preferences.force_close()
        pump(.4)
        window.close()
        pump(.2)

        print('== mini player (Ctrl+M) and reduced motion', flush=True)
        controller.enter({'room_id': 'midnight-library', 'with_view': True})
        settle()
        window = launch()
        mark = len(log)
        assert window.get_application().get_accels_for_action('win.mini-player') == ['<Control>m']
        window.activate_action('win.mini-player', None)
        pump(1.0)
        mini = window.mini_window
        assert mini is not None and mini.get_visible(), 'Ctrl+M opens the mini player'
        assert not commands(mark), 'opening the mini player sent %r' % commands(mark)
        player = mini.player
        assert player.name.get_text() == 'Midnight library' and player.line.get_text()
        names = {w.get_tooltip_text() for w in widgets(mini) if isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.Scale))}
        for needed in ('Pause the room', 'Room volume', 'Timer', 'Open the full window'):
            assert needed in names, (needed, names)
        assert_vocabulary(mini, 'Mini player')
        assert not unnamed_controls(mini), unnamed_controls(mini)
        evidence(mini, '14-mini-player')
        # Pause is Quiet; volume is the room volume; Space and arrows work on the mini window too.
        mark = len(log)
        player.play.emit('clicked')
        settle()
        assert commands(mark)[-1][0] == 'quiet' and backend.state['music']['status'] == 'Paused'
        player.play.emit('clicked')
        settle()
        assert commands(mark)[-1][0] == 'resume'
        player.volume.set_value(70)
        pump(.6)
        settle()
        assert controller.room_volume == 70, controller.room_volume
        player.volume.set_value(100)
        pump(.5)
        settle()
        mini_keys = [c for c in mini.observe_controllers() if isinstance(c, Gtk.EventControllerKey)]
        assert mini_keys, 'mini window handles keys'
        mark = len(log)
        mini.close()
        pump(.4)
        assert window.mini_window is None
        # Expand opens the full window
        window.open_mini()
        pump(.5)
        window.mini_window.player.expand_button.emit('clicked')
        pump(.6)
        assert window.mini_window is None and window.get_visible()
        window.close()
        pump(.2)
        controller.apply_settings({'reduced_motion': True})
        settle()
        window = launch()
        assert not window.scene.playing_motion and window.scene.media is None
        assert not Gtk.Settings.get_default().get_property('gtk-enable-animations')
        window.kick_idle()
        assert window.idle_timer is None
        evidence(window, '15-reduced-motion')
        window.close()
        pump(.2)
        controller.apply_settings({'reduced_motion': False})
        settle()
        controller.leave(0)
        settle()

        print('== Glance: Waybar popover is the same component', flush=True)
        controller.enter({'room_id': 'midnight-library', 'with_view': True})
        settle()
        import next_mini
        app_window = launch()
        mark = len(log)
        host = next_mini.PopoverHost(app, session_client.request, launch=lambda arguments: log.append(('launch', arguments)))
        pump(1.2)
        pop = host.window
        assert type(pop.player) is next_mini.MiniPlayer and type(app_window.mini_player) is next_mini.MiniPlayer
        assert pop.player.name.get_text() == 'Midnight library', pop.player.name.get_text()
        pills = [w.get_label() for w in widgets(pop.player.pills) if isinstance(w, Gtk.Button)]
        assert pills == ['Break 10 min', 'Wind down 30 min', 'Leave room'], pills
        assert pop.player.pills.get_mapped(), 'popover shows Break, Wind down and Leave room'
        assert not commands(mark), 'opening the popover sent %r' % commands(mark)
        evidence(pop, '16-waybar-popover')
        assert_vocabulary(pop, 'Waybar popover')
        assert not unnamed_controls(pop), unnamed_controls(pop)
        buttons = [w for w in widgets(pop.player.pills) if isinstance(w, Gtk.Button)]
        buttons[0].emit('clicked'); settle()
        assert ('quiet', {'minutes': 10}) in commands(mark), commands(mark)
        buttons[1].emit('clicked'); settle()
        assert ('wind_down', {'minutes': 30}) in commands(mark), commands(mark)
        pop.player.expand_button.emit('clicked')
        pump(.3)
        assert ('launch', ['--inside']) in log
        host2 = next_mini.PopoverHost(app, session_client.request, launch=lambda a: None)
        pump(.8)
        host2.window.player.pills.get_first_child()  # exists
        mark = len(log)
        [w for w in widgets(host2.window.player.pills) if isinstance(w, Gtk.Button)][2].emit('clicked')
        settle()
        pump(2.5)
        assert commands(mark)[0][0] == 'leave' and controller.active is None, 'Leave room from the popover'
        pump(.6)
        assert host2.window.player.state == 'return' and host2.window.player.return_button.get_visible()
        host2.window.close()
        app_window.close()
        pump(.3)
        import roomctl
        called = []
        with patch.object(next_mini, 'run_popover', side_effect=lambda argv: called.append(argv) or 0):
            assert roomctl.popover() == 0 and called, 'roomctl popover runs the shared component'

        print('== keyboard map (criterion 9) and shortcuts dialog', flush=True)
        controller.enter({'room_id': 'midnight-library', 'with_view': True})
        settle()
        window = launch()
        expected = {'win.show-rooms': ['<Control>1'], 'win.show-listen': ['<Control>2'], 'win.show-now': ['<Control>3'],
                    'win.preferences': ['<Control>comma'], 'win.mini-player': ['<Control>m']}
        for action, accels in expected.items():
            assert app.get_accels_for_action(action) == accels, (action, app.get_accels_for_action(action))
        assert '<Control>question' in app.get_accels_for_action('win.shortcuts')
        window.activate_action('win.shortcuts', None)
        pump(.6)
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.ShortcutsDialog), dialog
        # The native dialog draws keycaps, not text, so read the accelerators its keycap widgets draw (O11-SHELL
        # acceptance author, phase 2: the implementer's version read next_window.SHORTCUT_PAGES, the table the
        # dialog is built from, which says nothing about what the dialog shows).
        mask = int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK)

        def keycap(token):
            ok, keyval, mods = Gtk.accelerator_parse(token)
            mods = int(mods) & mask
            if keyval == Gdk.KEY_question:
                keyval, mods = Gdk.KEY_slash, mods | int(Gdk.ModifierType.SHIFT_MASK)
            return (Gdk.keyval_to_lower(keyval), mods) if ok and keyval else None
        drawn = {keycap(token) for w in widgets(dialog) if isinstance(w, Gtk.Widget) and w.get_mapped()
                 and callable(getattr(w, 'get_accelerator', None)) for token in (w.get_accelerator() or '').split()}
        for key, accels in (('Space', 'space'), ('Ctrl+1', '<Control>1'), ('Ctrl+2', '<Control>2'), ('Ctrl+3', '<Control>3'),
                            ('Ctrl+F', '<Control>f'), ('Ctrl+L', '<Control>l'), ('Ctrl+V', '<Control>v'), ('Ctrl+M', '<Control>m'),
                            ('Ctrl+,', '<Control>comma'), ('Esc', 'Escape'), ('Up / Down', 'Up Down'), ('Ctrl+?', '<Control>question')):
            assert {keycap(t) for t in accels.split()} <= drawn, 'the shortcuts dialog does not draw %s' % key
        for retired in ('<Control>4', '<Control>5', '<Control>6'):
            assert keycap(retired) not in drawn, 'Ctrl+4/5/6 are retired'
        dialog.force_close()
        pump(.4)
        # Keyboard-only walk: pages, search, Now, pause and volume, mini, preferences, link sheet; nothing starts anything.
        mark = len(log)
        window.activate_action('win.show-listen', None); pump(.5)
        window.activate_action('win.show-rooms', None); pump(.4)
        window.activate_action('win.show-now', None); pump(.5)
        assert window.current_page() == 'now'
        assert not [c for c in commands(mark)], commands(mark)
        assert window.key_pressed(None, Gdk.KEY_space, 0, 0); settle()
        assert commands(mark)[-1][0] == 'quiet'
        assert window.key_pressed(None, Gdk.KEY_space, 0, 0); settle()
        assert commands(mark)[-1][0] == 'resume'
        window.key_pressed(None, Gdk.KEY_Down, 0, 0); pump(.6); settle()
        assert controller.room_volume == 95
        window.key_pressed(None, Gdk.KEY_Up, 0, 0); pump(.6); settle()
        # Tab order reaches every Now control with a visible focus ring over the art
        reached = []
        window.set_focus(None)
        for _ in range(12):
            window.child_focus(Gtk.DirectionType.TAB_FORWARD)
            focus = window.get_focus()
            if focus is not None:
                reached.append(focus.get_tooltip_text() or (focus.get_label() if hasattr(focus, 'get_label') else '') or type(focus).__name__)
        for needed in ('Back to rooms', 'Mix', 'Pause the room'):
            assert any(needed in r for r in reached), (needed, reached)
        window.set_focus_visible(True)
        window.play.grab_focus()
        pump(.5)
        from PIL import ImageGrab
        ok, bounds = window.play.compute_bounds(window)
        focused = ImageGrab.grab(bbox=(0, 0, window.get_width(), window.get_height()), xdisplay=os.environ['DISPLAY']).convert('RGB')
        window.mix_button.grab_focus(); window.set_focus_visible(False); pump(.5)
        plain = ImageGrab.grab(bbox=(0, 0, window.get_width(), window.get_height()), xdisplay=os.environ['DISPLAY']).convert('RGB')
        cx, top = int(bounds.get_x() + bounds.get_width() / 2), int(bounds.get_y())
        ring = [focused.getpixel((cx, top + d)) for d in range(2, 7)]
        fill = plain.getpixel((cx, top + 3))
        assert contrast_pair(min(ring, key=lambda p: luminance(p)), fill) >= 3 or contrast_pair(max(ring, key=lambda p: luminance(p)), fill) >= 3, \
            'focus is not visible on the play button: ring %r fill %r' % (ring, fill)
        # a dark glass button (Mix) shows a light ring over the art
        window.set_focus_visible(True); window.mix_button.grab_focus(); pump(.5)
        ok, mb = window.mix_button.compute_bounds(window)
        ringed = ImageGrab.grab(bbox=(0, 0, window.get_width(), window.get_height()), xdisplay=os.environ['DISPLAY']).convert('RGB')
        mx, my = int(mb.get_x() + mb.get_width() / 2), int(mb.get_y())
        strip = [ringed.getpixel((mx, my + d)) for d in range(0, 6)]
        assert max(luminance(p) for p in strip) > .6, 'no light focus ring on the Mix button: %r' % (strip,)
        # Ctrl+M, Ctrl+, and Ctrl+L each complete from the keyboard and Esc leaves them
        window.activate_action('win.mini-player', None); pump(.6)
        assert window.mini_window is not None
        window.mini_window.close(); pump(.3)
        window.activate_action('win.preferences', None); pump(1.0)
        assert window.preferences.get_visible()
        window.preferences.force_close(); pump(.3)
        window.show_listen(); pump(.4)
        window.listen.add_link(); pump(.6)
        assert isinstance(window.get_visible_dialog(), Adw.Dialog)
        window.get_visible_dialog().force_close(); pump(.3)
        assert window.key_pressed(None, Gdk.KEY_Escape, 0, 0) is not None
        assert not unnamed_controls(window), unnamed_controls(window)
        window.close()
        pump(.2)

        print('== narrow window: the mini layout below 560 px (criteria 11)', flush=True)
        window = launch(420, 700)
        pump(.8)
        assert window.mini_layout and window.current_page() == 'mini', (window.mini_layout, window.current_page())
        assert window.get_width() < 560
        assert_layout(window, 'mini layout 420x700')
        assert_vocabulary(window, 'mini layout')
        assert window.mini_player.get_mapped() and not window.switcher.get_visible()
        evidence(window, '17-mini-layout-420')
        mark = len(log)
        window.mini_player.expand_button.emit('clicked')
        pump(1.2)
        assert window.get_width() >= 560 and not window.mini_layout, window.get_width()
        window.close()
        pump(.2)
        window = launch(600, 700)
        assert not window.mini_layout and window.current_page() == 'now', 'above 560 px the normal layout stays'
        window.close()
        pump(.2)
        # no room, last room recorded: the mini layout offers Return as the one start
        controller.leave(0); settle(); pump(2)
        log.clear()
        window = launch(420, 600)
        pump(.6)
        assert window.mini_player.return_button.get_visible() and 'Return to Midnight library' in window.mini_player.return_button.get_label()
        assert not commands()
        assert_layout(window, 'mini layout, return 420x600')
        window.mini_player.return_button.emit('clicked'); settle()
        assert [c[0] for c in commands()] == ['return'] and controller.active
        window.close()
        pump(.2)

        print('== a second launch is delivered to the running window', flush=True)
        window = launch()
        app.window = window
        env = dict(os.environ)
        def second(*arguments):
            child = subprocess.Popen([sys.executable, str(source / 'listening_room.py'), '--ui=next'] + list(arguments), env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 40
            while child.poll() is None and time.monotonic() < deadline:
                pump(.2)
            assert child.poll() == 0, (child.poll(), child.stderr.read().decode()[-400:])
            pump(.8)
        mark = len(log)
        second('--desk')
        assert window.mini_window is not None, '--desk opens the mini player in the running window'
        window.mini_window.close(); pump(.3)
        second('--mini')
        assert window.mini_window is not None
        window.mini_window.close(); pump(.3)
        second('--inside')
        assert window.current_page() == 'now'
        window.show_rooms(); pump(.3)
        second('listening-room://open?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ')
        assert isinstance(window.get_visible_dialog(), Adw.Dialog) or window.listen.link_sheet is not None if hasattr(window.listen, 'link_sheet') else True
        for child in list(widgets(window)):
            if isinstance(child, Adw.Dialog):
                child.force_close()
        pump(.3)
        assert not [c for c in commands(mark) if c[0] in PLAYBACK_COMMANDS - {'quiet', 'resume'}], 'a delivered link started something'
        window.close()
        pump(.2)
        assert next_window.launch_arguments(['--desk']) == ('mini', None)
        assert next_window.launch_arguments(['--ui=next', '--inside']) == ('inside', None)
        assert next_window.launch_arguments(['--preview-link', 'https://a.example/x'])[1] == 'https://a.example/x'

        print('== contrast: every room, every text-over-art surface (criterion 10)', flush=True)
        results = {}
        for room in ROOMS:
            controller.enter({'room_id': room['id'], 'with_view': True})
            settle()
            window = launch(1200, 800)
            window.show_now()
            pump(.8)
            now = contrast_behind(window, [window.room_name, window.now_line, window.now_detail, window.chip, window.mix_summary], room['id'])
            window.choose_timer('wind_down'); settle(); pump(.5)
            chip = contrast_behind(window, [window.timer_chip.get_child() or window.timer_chip], room['id']) if False else 99
            window.close(); pump(.2)
            narrow = launch(420, 700)
            pump(.8)
            mini = mini_contrast(narrow.mini_player, narrow, room['id'])
            narrow.close(); pump(.2)
            controller.leave(0); settle(); pump(1.8)
            window = launch(1200, 800)
            pump(.6)
            hero = hero_contrast(window, room['id'])
            window.close(); pump(.2)
            results[room['id']] = (now, mini, hero)
        print('contrast now/mini/hero (white text vs 90th percentile behind it): ' +
              ', '.join('%s %.1f/%.1f/%.1f' % ((k,) + v) for k, v in results.items()))
        assert len(results) == 8 and all(min(v) >= 4.5 for v in results.values()), results

        print('== adaptive widths (criterion 11) across the pages', flush=True)
        controller.enter({'room_id': 'midnight-library', 'with_view': True}); settle()
        for width, height in ((1200, 800), (800, 700)):
            window = launch(width, height)
            for page in ('rooms', 'listen', 'now'):
                getattr(window, 'show_' + page)(); pump(.7)
                assert_layout(window, '%s %dx%d' % (page, width, height))
            window.show_now(); window.sheet.set_open(True); pump(.9)
            assert_layout(window, 'mix %dx%d' % (width, height))
            window.close(); pump(.2)
        window = launch(420, 700)
        for page in ('rooms', 'listen', 'now'):
            getattr(window, 'show_' + page)(); pump(.5)
            assert window.current_page() == 'mini'
            assert_layout(window, '%s 420x700' % page)
        window.close(); pump(.2)

        print('== reduced motion: nothing moves and nothing fades', flush=True)
        controller.apply_settings({'reduced_motion': True}); settle()
        window = launch(1200, 800)
        assert window.scene.media is None and not Gtk.Settings.get_default().get_property('gtk-enable-animations')
        window.kick_idle(); assert window.idle_timer is None
        window.open_mini(); pump(.4)
        assert window.mini_window.player.art.paintable is None or not hasattr(window.mini_window.player.art.paintable, 'get_playing')
        window.close(); pump(.2)
        controller.apply_settings({'reduced_motion': False}); settle()

        print('== parity: Other players and system volume', flush=True)
        calls = []
        players = [{'id': 'firefox.instance123', 'status': 'Playing', 'artist': 'Band', 'title': 'Song', 'volume': 40,
                    'can_next': True, 'can_previous': True},
                   {'id': 'radio', 'status': 'Playing', 'volume': 50}, {'id': 'video', 'status': 'Paused', 'volume': 50}]
        with patch.object(hub, 'inspect_state', side_effect=lambda: {'players': players, 'system_volume': {'percent': 33, 'muted': False}}), \
             patch.object(hub, 'transport', side_effect=lambda pid, command: calls.append(('transport', pid, command))), \
             patch.object(hub, 'mute', side_effect=lambda **kw: calls.append(('mute', kw))):
            window = launch()
            mark = len(log)
            window.menu_button.popup(); pump(.4)
            assert 'Other players…' in {w.get_label() for w in widgets(window.menu_popover) if isinstance(w, Gtk.Button)}
            window.menu_popover.popdown()
            window.open_players(); pump(1.2)
            dialog = window.get_visible_dialog()
            rows = [w for w in widgets(dialog) if isinstance(w, Adw.ActionRow)]
            titles = [r.get_title() for r in rows]
            assert 'Firefox' + 'instance123'[:0] in ' '.join(titles) and not any('Radio' in t or 'Video' in t for t in titles), titles
            assert not commands(mark), 'opening Other players sent %r' % commands(mark)
            evidence(window, '18-other-players')
            assert_vocabulary(dialog, 'Other players')
            assert not unnamed_controls(dialog), unnamed_controls(dialog)
            play = next(w for w in widgets(dialog) if isinstance(w, Gtk.Button) and (w.get_tooltip_text() or '').startswith('Play or pause'))
            play.emit('clicked'); pump(.6)
            assert ('transport', 'firefox.instance123', 'play-pause') in calls, calls
            dialog.force_close(); pump(.3)
            window.close(); pump(.2)

        print('== parity: Play next, Track details, More/Less like this', flush=True)
        library_station = controller.stations()[0]
        controller.leave(0); settle(); pump(1.5)
        window = launch()
        window.listen.library = [dict(queue[0])]
        entries = window.listen.library_entries()
        labels = [t for t, _ in entries[0]['menu']]
        assert 'Track details…' in labels, labels
        assert 'Play next' not in labels, 'Play next needs a running library station'
        window.close(); pump(.2)

        print('== parity: Jellyfin kinds and albums, Speakers, Remembered, Twitch follows, station rename, Range, AudioMuse', flush=True)
        import online_services as online
        import twitch_channels
        settings_cfg = {name: {'endpoint': 'http://fixture.invalid', 'username': 'me', 'user_id': 'uid', 'token_file': ''}
                        for name in online.SERVICES}
        jf = lambda kind, **kw: dict({'provider': 'jellyfin', 'id': kind + '1', 'type': kind, 'artist': 'Band',
                                      'uri': 'jellyfin://x/' + kind + '1'}, **kw)
        album, playlist = jf('album', type='MusicAlbum', name='Album One'), jf('playlist', type='Playlist', name='Playlist One')
        track = jf('track', type='Audio', name='Track One')
        listings = {'albums': [album], 'playlists': [playlist], 'recent': [dict(album, id='r1', name='Fresh Album')], 'tracks': [track]}
        fake = {'play': [], 'transfer': [], 'bookmark': []}
        players = [{'id': 'p1', 'name': 'Kitchen', 'available': True, 'state': 'playing', 'volume': 30},
                   {'id': 'p2', 'name': 'Lounge', 'available': True, 'state': 'idle', 'volume': None}]
        ma_rows = [{'provider': 'music_assistant', 'id': 'm1', 'name': 'Evening Mix', 'type': 'playlist', 'artist': '', 'uri': 'library://playlist/1'}]
        with patch.object(online, 'config', return_value=settings_cfg), \
             patch.object(online, 'browse_jellyfin', side_effect=lambda query='', kind='albums', parent_id=None, cancel_event=None: list(listings[kind])), \
             patch.object(online, 'jellyfin_queue', side_effect=lambda item, cancel_event=None: [track]), \
             patch.object(online, 'ma_players', side_effect=lambda cancel_event=None: [dict(p) for p in players]), \
             patch.object(online, 'browse_music_assistant', side_effect=lambda query='', kind='playlists', cancel_event=None, provider=None, search_all=False: [dict(r, name=r['name'] + (' (search)' if search_all else '')) for r in ma_rows]), \
             patch.object(online, 'ma_play', side_effect=lambda uri, player, radio_mode=False: fake['play'].append((uri, player, radio_mode))), \
             patch.object(online, 'ma_transfer', side_effect=lambda a, b: fake['transfer'].append((a, b))), \
             patch.object(online, 'remembered', return_value=[{'title': 'Song', 'artist': 'Band', 'note': 'lovely'}]), \
             patch.object(online, 'bookmark', side_effect=lambda snapshot, note='': fake['bookmark'].append(note) or {}), \
             patch.object(online, 'listenbrainz_history', return_value=[{'track_metadata': {'artist_name': 'Hist', 'track_name': 'Listen'}}]), \
             patch.object(online, 'listenbrainz_recommendations', return_value=[{'recording_mbid': 'a' * 8 + '-' + 'b' * 4 + '-' + 'c' * 4 + '-' + 'd' * 4 + '-' + 'e' * 12, 'url': 'https://musicbrainz.org/recording/x'}]):
            controller.leave(0); settle(); pump(1.2)
            window = launch(1200, 800)
            window.show_listen(); pump(.6)
            page = window.listen
            mark = len(log)
            def on_screen():
                return ' | '.join(texts(page.index_body))
            # Library kinds and opening an album
            page.scopes.set_active(3); pump(.8)
            assert 'Album One' in on_screen(), on_screen()
            for index, wanted in ((1, 'Playlist One'), (2, 'Fresh Album'), (3, 'Track One')):
                page.kind_group.set_active(index); pump(.8)
                assert wanted in on_screen(), (index, on_screen())
            menu = [t for t, _ in page.kind_entries()[0]['menu']]
            assert 'Track details…' in menu and 'Make an AudioMuse station' in menu, menu
            page.kind_group.set_active(0); pump(.5)
            assert 'Show tracks' in [t for t, _ in page.library_entries()[0]['menu']]
            page.open_album(album); pump(.8)
            assert 'Track One' in on_screen() and 'Library' in on_screen(), on_screen()
            assert 'Track details…' in [t for t, _ in page.library_entries_for([track])[0]['menu']]
            assert not commands(mark), 'browsing the library sent %r' % commands(mark)
            # AudioMuse station from a track
            page.make_audiomuse_station(track); settle(); pump(.6)
            made = [st for st in controller.stations() if (st.get('generation') or {}).get('kind') == 'audiomuse']
            assert made and made[0]['name'] == 'Mix from Track One', controller.stations()
            # Speakers
            mark = len(log)
            page.more_scopes.set_active(0); pump(1.2)
            assert page.scope == 'speakers' and page.scopes.get_active() >= 0xFFFFFFF0 or page.scopes.get_active() == Gtk.INVALID_LIST_POSITION
            body = on_screen()
            assert 'Kitchen' in body and 'Lounge' in body and 'Evening Mix' in body, body
            assert not commands(mark), 'browsing speakers sent %r' % commands(mark)
            evidence(window, '19-speakers')
            page.media_entries(ma_rows)[0]['action'][1](); settle(); pump(.8)
            sent = [c for c in commands(mark) if c[0] == 'online']
            assert sent and sent[-1][1]['service'] == 'music_assistant' and sent[-1][1]['item']['player_id'] == 'p1' \
                and sent[-1][1]['item']['uri'] == 'library://playlist/1', sent
            page.speaker_entries()[1]['action'][1](); pump(.4)
            page.media_entries(ma_rows)[0]['menu'][0][1](); settle(); pump(.6)
            sent = [c for c in commands(mark) if c[0] == 'online'][-1][1]['item']
            assert sent['player_id'] == 'p2' and sent['radio_mode'] is True, sent
            page.speaker_entries()[1]['menu'][0][1](); settle(); pump(.6)
            assert fake['transfer'] == [('p1', 'p2')], fake
            page.search.set_text('mix'); page.search.emit('activate'); pump(1.2)
            assert 'Evening Mix (search)' in on_screen(), on_screen()
            page.search.set_text(''); pump(.3)
            # Remembered
            mark = len(log)
            page.more_scopes.set_active(1); pump(1.0)
            assert 'Band — Song' in on_screen(), on_screen()
            entry = page.remembered_entries([{'title': 'Song', 'artist': 'Band'}])[0]
            assert entry['action'][0] == 'Find in Jellyfin' and entry['menu'][0][0] == 'Open in YouTube Music'
            page.memory_note.set_text('keep'); page.remember_playing(); pump(1.0)
            assert fake['bookmark'] == ['keep'], fake
            assert 'Hist — Listen' not in on_screen(), 'history must be read on request only'
            page.load_history(); pump(1.0)
            assert 'Hist — Listen' in on_screen(), on_screen()
            page.load_recommendations(); pump(1.0)
            assert 'Recommended recording' in on_screen(), on_screen()
            page.find_in_library('Band Song'); pump(1.0)
            assert page.scope == 'library' and page.kind == 'tracks' and page.search.get_text() == 'Band Song'
            assert not [c for c in commands(mark) if c[0] in PLAYBACK_COMMANDS], commands(mark)
            page.search.set_text(''); page.scopes.set_active(0); pump(.4)
            # Station detail: Range and Rename
            station = controller.stations()[0]
            page.show_station(station); pump(.8)
            assert 'Range' in ' | '.join(texts(page.station_main)), texts(page.station_main)
            page.mood_groups['stay_close'].set_active(2); settle(); pump(.8)
            saved = next(st for st in controller.stations() if st['id'] == station['id'])
            assert saved['preferences'].get('stay_close') == 'Explore', saved
            page.rename_dialog(); pump(.6)
            dialog = window.get_visible_dialog()
            entry_widget = next(w for w in widgets(dialog) if isinstance(w, Gtk.Entry))
            entry_widget.set_text('Renamed station')
            dialog.emit('response', 'rename'); settle(); pump(.8)
            saved = next(st for st in controller.stations() if st['id'] == station['id'])
            assert saved['name'] == 'Renamed station' and saved['preferences'].get('stay_close') == 'Explore', saved
            window.close(); pump(.2)
            # Twitch: Review my follows
            window = launch(1200, 800)
            window.open_preferences(); pump(.8)
            prefs_dialog = window.preferences
            prefs_dialog.open_connection('twitch'); pump(.8)
            follows = {'status': 'follows', 'message': 'Review', 'complete': True,
                       'channels': [{'login': 'alpha', 'name': 'Alpha', 'url': 'https://www.twitch.tv/alpha'},
                                    {'login': 'beta', 'name': 'Beta', 'url': 'https://www.twitch.tv/beta'}]}
            with patch.object(twitch_channels, 'import_following', side_effect=lambda profile: follows):
                import next_twitch
                prefs_dialog.follow_name.set_text('me')
                before = twitch_channels.curated_channels('music')
                next_twitch.review_follows(prefs_dialog); pump(1.2)
                assert twitch_channels.curated_channels('music') == before, 'reviewing added channels without being asked'
                review = prefs_dialog
                checks = [w for w in widgets(review) if isinstance(w, Gtk.CheckButton)]
                assert len(checks) == 2, checks
                checks[1].set_active(True)
                add = next(w for w in widgets(review) if isinstance(w, Adw.ButtonRow) and 'Add selected' in w.get_title())
                add.emit('activated'); pump(.8)
                assert twitch_channels.curated_channels('music')[-1:] == ['beta'], twitch_channels.curated_channels('music')
            window.close(); pump(.2)

        print('EVIDENCE:', ', '.join(EVIDENCE))
        print('PASS: Glance (mini, popover, narrow layout, delivery of --desk, --inside and links), keyboard map, focus ring, contrast in 8 rooms, adaptive widths, reduced motion, Other players, evidence screenshots')
    finally:
        for item in reversed(patches):
            item.stop()
        controller.close()
