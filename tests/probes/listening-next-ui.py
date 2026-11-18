"""Probe the next Listening Room window (--ui=next) with real GTK and the real session controller.

State is isolated and playback is a fake backend; nothing touches the desktop
session. Every controller command is logged so browsing can be proven inert.

  scripts/listening-dev.sh shell     # then, inside it:
  python3 tests/probes/listening-next-ui.py --output DIR

Without a DISPLAY the probe re-executes itself on a private Xvfb and D-Bus with
portals disabled, exactly as scripts/listening-dev.sh probe does.
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
parser.add_argument('--output', default='/tmp/listening-next-review')
parser.add_argument('--motion-probe', action='store_true', help='also measure loop playback (needs GStreamer)')
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

    def luminance(pixel):
        def channel(v):
            v /= 255
            return v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4
        return .2126 * channel(pixel[0]) + .7152 * channel(pixel[1]) + .0722 * channel(pixel[2])

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

    try:
        # ------------------------------------------------------------------ launch: nothing active, a last room exists
        controller.enter({'room_id': 'midnight-library', 'with_view': True})
        settle()
        controller.leave(0)
        settle()
        assert controller.active is None and controller.last_session['room_id'] == 'midnight-library'
        log.clear()
        before_calls = len(backend.calls)
        window = launch()
        assert window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'rooms', 'did not open Rooms'
        assert window.hero_button.get_label() == 'Return to Midnight library', window.hero_button.get_label()
        suggested = [w for w in widgets(window) if isinstance(w, Gtk.Button) and w.has_css_class('suggested-action') and w.get_mapped()]
        assert suggested == [window.hero_button], 'Return must be the single primary action'
        assert not commands(), 'launching sent %r' % commands()
        shot(window, '01-rooms-return-hero')
        assert_vocabulary(window, 'Rooms')
        assert_layout(window, 'Rooms 1200x800')
        # Inert browsing: switching pages, selecting a card, opening menus send nothing that plays.
        for step in (window.show_listen, window.show_rooms, lambda: window.menu_button.popup(), lambda: window.menu_button.popdown(),
                     lambda: window.output_button.popup(), lambda: window.output_button.popdown(),
                     lambda: window.timer_button.popup(), lambda: window.timer_button.popdown()):
            step()
            pump(.3)
        window.show_listen()
        pump(.4)
        assert_vocabulary(window, 'Listen')
        shot(window, '01b-listen-provisional')
        window.show_rooms()
        pump(.3)
        window.cards.select_child(window.cards.get_child_at_index(1))
        pump(.3)
        window.activate_action('show-now', None)
        pump(.2)
        assert window.current_page() == 'browse', 'Now opened without a room'
        assert not commands() and len(backend.calls) == before_calls, 'browsing sent %r' % commands()
        assert not [c for c in log if c[0] in PLAYBACK_COMMANDS]

        # ------------------------------------------------------------------ Return: one activation, then Now on live radio
        window.hero_button.emit('clicked')
        settle()
        assert [c[0] for c in commands()] == ['return'], commands()
        assert window.current_page() == 'now', (window.current_page(), window.want_now, window.snapshot.get('transition'),
                                                bool(window.snapshot.get('active')), window.snapshot.get('errors'), log[-4:])
        assert controller.active['room_id'] == 'midnight-library'
        assert window.chip.get_text() == 'Live radio', window.chip.get_text()
        assert not window.skip.get_visible(), 'Next track must be hidden on live radio'
        shot(window, '02-now-live-radio')
        names = visible_controls(window)
        assert not any(word in names for word in ('Next track',)), names
        for required in ('Pause the room', 'Room volume', 'Mix', 'Output', 'Timer', 'Main menu', 'Back to rooms'):
            assert required in names, (required, names)
        assert_vocabulary(window, 'Now (live radio)')
        assert_layout(window, 'Now 1200x800')
        assert window.room_name.get_text() == 'Midnight library'

        # Keyboard: Space pauses (the existing Quiet) and resumes, arrows change room volume.
        mark = len(log)
        assert window.key_pressed(None, Gdk.KEY_space, 0, 0)
        settle()
        assert commands(mark)[-1][0] == 'quiet' and commands(mark)[-1][1] == {}, commands(mark)
        assert backend.state['music']['status'] == 'Paused'
        assert window.play.get_tooltip_text() == 'Play the room'
        assert window.key_pressed(None, Gdk.KEY_space, 0, 0)
        settle()
        assert commands(mark)[-1][0] == 'resume' and backend.state['music']['status'] == 'Playing'
        window.key_pressed(None, Gdk.KEY_Down, 0, 0)
        pump(.6)
        settle()
        assert controller.room_volume == 95, controller.room_volume
        assert backend.state['music']['volume'] == 47.5, backend.state['music']['volume']
        assert controller.active['music']['volume'] == 50, 'layer level must not change'
        window.key_pressed(None, Gdk.KEY_Up, 0, 0)
        pump(.6)
        settle()
        assert controller.room_volume == 100

        # ------------------------------------------------------------------ Mix sheet, ambience, scene
        controller.layer({'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': 'rain-glass',
                          'name': 'Rain on glass', 'outdoor': True, 'volume': 20})
        settle()
        mark = len(log)
        window.mix_button.emit('clicked')
        pump(1.0)
        assert window.sheet.get_open()
        rows = {row['layer']: row for row in next_model.mix_rows(window.snapshot)}
        assert rows['atmosphere']['outdoor'] and rows['music']['can_next'] is False
        assert window.mix.rows['atmosphere'].window.get_visible() and not window.mix.rows['music'].window
        assert not window.mix.rows['music'].next_item.get_visible(), 'Next track is hidden for live radio'
        shot(window, '04-mix-open')
        assert_vocabulary(window, 'Mix')
        assert not commands(mark), 'opening Mix sent %r' % commands(mark)
        window.key_pressed(None, Gdk.KEY_Escape, 0, 0)
        pump(.5)
        assert not window.sheet.get_open() and window.current_page() == 'now', 'Esc closes Mix first'

        # Save as my room… prefilled and routed through the existing save command.
        window.save_dialog()
        pump(.5)
        assert window.save_entry.get_text() == 'Midnight library'
        window.save_entry.set_text('Rainy reading')
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.AlertDialog), dialog
        mark = len(log)
        dialog.emit('response', 'save')
        settle()
        assert commands(mark)[0] == ('save', {'name': 'Rainy reading'}), commands(mark)
        assert controller.variants()[0]['name'] == 'Rainy reading'
        for child in list(widgets(window)):
            if isinstance(child, Adw.Dialog):
                child.force_close()
        pump(.3)

        # ------------------------------------------------------------------ layer failure keeps the others playing
        backend.fail_atmosphere = 'The sound could not start'
        mark = len(log)
        service.execute('layer', {'layer': 'atmosphere', 'action': 'retry'})
        settle()
        failure = next_model.failure(window.snapshot)
        assert failure and failure['layer'] == 'atmosphere', window.snapshot.get('errors')
        assert window.banner.get_visible() and window.marker.get_visible()
        assert window.banner_title.get_text() == 'Ambience couldn’t start', window.banner_title.get_text()
        assert 'Music' in window.banner_detail.get_text() and 'keeps' in window.banner_detail.get_text() or 'keep' in window.banner_detail.get_text()
        assert backend.state['music']['status'] == 'Playing' and backend.state['picture']['status'] == 'Playing'
        shot(window, '05-layer-failure-banner')
        assert_vocabulary(window, 'Banner')
        backend.fail_atmosphere = None
        mark = len(log)
        window.banner_retry.emit('clicked')
        settle()
        assert commands(mark)[0][1] == {'layer': 'atmosphere', 'action': 'retry'}, 'Try again must re-run only that layer'
        assert not window.banner.get_visible(), 'banner should clear once the layer starts'

        # ------------------------------------------------------------------ timer chip, Output popover
        window.choose_timer('wind_down')
        settle()
        assert window.timer_chip.get_visible() and window.timer_chip.get_label().startswith('Wind down · '), window.timer_chip.get_label()
        window.timer_button.popup()
        pump(.6)
        shot(window, '06-timer-chip')
        window.timer_button.popdown()
        pump(.3)
        window.output_button.popup()
        pump(1.0)
        assert [b.get_label() for b in window.output_popover.buttons] == ['System output', 'Desk speakers', 'Headphones']
        shot(window, '07-output-popover')
        window.test_output()
        pump(.5)
        assert ('test_sound', None) in log
        window.output_popover.system.set_value(55)
        pump(.5)
        assert system_volume['percent'] == 55
        window.output_popover.buttons[2].set_active(True)
        settle()
        assert controller.settings['preferred_output'] == 'buds'
        window.output_button.popdown()
        window.choose_timer('cancel')
        settle()
        assert not window.timer_chip.get_visible()

        # ------------------------------------------------------------------ leave room uses the fade-and-stop leave
        mark = len(log)
        window.leave_room()
        pump(.2)
        assert commands(mark)[0][0] == 'leave'
        settle()
        pump(2.5)
        assert controller.active is None, 'Leave room should stop the room'
        assert window.current_page() == 'browse', 'left room should return to browsing'
        window.close()
        pump(.3)

        # ------------------------------------------------------------------ launch while a room is active opens Now; library queue
        queue = [{'provider': 'jellyfin', 'id': 'track1', 'type': 'Audio', 'name': 'First track'},
                 {'provider': 'jellyfin', 'id': 'track2', 'type': 'Audio', 'name': 'Second track'}]
        controller.enter({'music': {'reference': queue[0], 'source_id': 'jellyfin-track1', 'name': 'First track',
                                    'queue': queue, 'queue_index': 0},
                          'recipe': controller.last_session})
        settle()
        log.clear()
        window = launch()
        assert window.current_page() == 'now', 'an active room should open Now'
        assert not [c for c in commands()], 'launch sent %r' % commands()
        assert window.chip.get_text() == 'Library', window.chip.get_text()
        assert window.skip.get_visible(), 'Next track expected for a library queue'
        shot(window, '03-now-library-next-track')
        assert_vocabulary(window, 'Now (library)')
        assert_layout(window, 'Now library')
        window.skip.emit('clicked')
        settle()
        assert commands()[-1] == ('layer', {'layer': 'music', 'action': 'next'}), commands()
        # Esc returns to browsing, Ctrl+1/2/3 move between Rooms, Listen and Now; none of it sends a command.
        settle()
        mark = len(log)
        window.key_pressed(None, Gdk.KEY_Escape, 0, 0)
        pump(.4)
        assert window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'rooms'
        window.activate_action('win.show-listen', None)
        pump(.4)
        assert window.browse.get_visible_child_name() == 'listen'
        window.activate_action('win.show-now', None)
        pump(.4)
        assert window.current_page() == 'now'
        window.activate_action('win.show-rooms', None)
        pump(.4)
        assert window.browse.get_visible_child_name() == 'rooms' and window.current_page() == 'browse'
        assert not commands(mark), 'navigating sent %r' % commands(mark)
        window.close()
        pump(.2)

        # ------------------------------------------------------------------ reduced motion and 800 px
        controller.apply_settings({'reduced_motion': True})
        settle()
        window = launch()
        assert window.current_page() == 'now'
        assert not window.scene.playing_motion and window.scene.media is None, 'reduced motion must show still art'
        assert not Gtk.Settings.get_default().get_property('gtk-enable-animations')
        window.kick_idle()
        assert window.idle_timer is None, 'no idle fade under reduced motion'
        assert_layout(window, 'Now 1200x800 reduced motion')
        shot(window, '08-now-reduced-motion')
        window.close()
        pump(.2)

        window = launch(800, 700)
        assert window.current_page() == 'now'
        assert_layout(window, 'Now 800x700')
        shot(window, '09-now-800')
        window.sheet.set_open(True)
        pump(.8)
        assert_layout(window, 'Mix 800x700')
        shot(window, '10-mix-800')
        window.sheet.set_open(False)
        window.show_listen()
        pump(.6)
        assert_layout(window, 'Listen 800x700')
        window.show_rooms()
        pump(.6)
        assert_layout(window, 'Rooms 800x700')
        shot(window, '11-rooms-800')
        window.close()
        pump(.2)

        # ------------------------------------------------------------------ contrast for every room scene (still art)
        window = launch(1200, 800)
        results = {}
        for room in room_catalogue.rooms():
            controller.enter({'room_id': room['id'], 'with_view': True})
            settle()
            window.show_now()
            pump(.8)
            results[room['id']] = contrast_behind(window, [window.room_name, window.now_line, window.now_detail, window.chip,
                                                           window.mix_summary], room['id'])
        print('contrast (white text vs 90th percentile background): ' +
              ', '.join('%s %.1f' % item for item in results.items()))
        assert all(value >= 4.5 for value in results.values()), results
        window.close()
        pump(.2)

        if args.motion_probe:
            import resource

            def cpu():
                usage = resource.getrusage(resource.RUSAGE_SELF)
                return usage.ru_utime + usage.ru_stime

            def cost(seconds):
                started, before = time.monotonic(), cpu()
                pump(seconds)
                return (cpu() - before) / (time.monotonic() - started)

            assert next_widgets.gst_ready(), 'GStreamer playback is not available in this environment'
            # Decode alone, with no window open, so nothing else is in the number.
            # The sink is paced to realtime: this is the CPU cost of one second of
            # the reviewed loop, before GTK touches a frame.
            from gi.repository import Gst
            import ambience
            loop = next((row.get('url') for row in ambience.presets() if row.get('id') == 'room-midnight-library'), None)
            pipeline = Gst.parse_launch('filesrc location=%s ! decodebin ! fakesink sync=true name=sink' % loop)
            frames = [0]

            def count(_pad, _info):
                frames[0] += 1
                return Gst.PadProbeReturn.OK
            pipeline.get_by_name('sink').get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, count)
            pipeline.set_state(Gst.State.PLAYING)
            pipeline.get_state(Gst.SECOND)  # pipeline ready, not just spawning
            decode = cost(8)
            pipeline.set_state(Gst.State.NULL)
            print('decode alone, no window, realtime paced: %.2f CPU s per second of playback (%d frames in 8 s)'
                  % (decode, frames[0]))

            controller.apply_settings({'reduced_motion': False})
            controller.enter({'room_id': 'midnight-library', 'with_view': True})
            settle()
            window = launch(1200, 800)
            window.show_now()
            pump(1.0)
            assert window.scene.playing_motion, 'the reviewed loop should play when motion is allowed'
            # The same window on still art is the baseline, so the loop's own cost
            # can be told apart from polling and from software compositing.
            window.scene._visibility(minimized=True)
            pump(1)
            still = cost(6)
            assert not window.scene.playing_motion, 'loop must pause when the window is hidden'
            window.scene._visibility(minimized=False)
            pump(1)
            assert window.scene.playing_motion, 'loop must resume when the window is visible again'
            motion = cost(8)
            print('scene cost in the window, software renderer: still art %.2f CPU s/s, reviewed loop %.2f CPU s/s, '
                  'loop %.2f CPU s/s' % (still, motion, motion - still))
            window.close()
        print('PASS: next window launch, inert browsing, Now, Mix, banner, timer, output, keyboard, reduced motion, 800 px, contrast')
    finally:
        for item in reversed(patches):
            item.stop()
        controller.close()
