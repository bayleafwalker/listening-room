"""Probe the Listen destination of the next window (--ui=next) with real GTK.

Uses the real session controller with the session test double, fixture
providers for World Radio, the library and Twitch, and an isolated HOME, XDG
and D-Bus. Every controller command is logged so browsing, searching, opening
pages and menus can be proven inert.

  scripts/listening-dev.sh shell     # then, inside it:
  python3 tests/probes/listening-listen-ui.py --output DIR

Without a DISPLAY the probe re-executes itself on a private Xvfb and D-Bus.
"""
import argparse
import copy
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', default=str(Path(__file__).resolve().parents[2] / 'src'))
parser.add_argument('--output', default='/tmp/listening-listen-review')
args = parser.parse_args()

if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
    runtime = tempfile.mkdtemp(prefix='listening-listen-runtime-')
    os.chmod(runtime, 0o700)
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')}
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

TRACKS = [
    {'provider': 'jellyfin', 'id': 'track-1', 'type': 'Audio', 'name': 'Nocturne in C',
     'artist': 'Someone', 'album': 'Neoclassical collection'},
    {'provider': 'jellyfin', 'id': 'track-2', 'type': 'Audio', 'name': 'Quiet study',
     'artist': 'Someone Else', 'album': 'Neoclassical collection'},
    {'provider': 'jellyfin', 'id': 'track-3', 'type': 'Audio', 'name': 'Slow light',
     'artist': 'Third', 'album': 'Neoclassical collection'},
]
ALBUMS = [
    {'provider': 'jellyfin', 'id': 'album-neoclassical', 'type': 'MusicAlbum',
     'name': 'Neoclassical collection', 'artist': 'Various', 'genres': ['Neoclassical'],
     'image_id': 'album-neoclassical'},
    {'provider': 'jellyfin', 'id': 'album-ambient', 'type': 'MusicAlbum',
     'name': 'Ambient works', 'artist': 'Various', 'genres': ['Ambient'], 'image_id': 'album-ambient'},
]
WORLD = [
    {'id': 'world-ambient-drift', 'name': 'Ambient Drift', 'group': 'Search results', 'kind': 'radio',
     'url': 'https://radio.example.test/ambient-drift', 'country': 'Finland', 'language': 'Finnish',
     'tags': ['ambient', 'calm'], 'website': 'https://radio.example.test/'},
    {'id': 'world-night-ambient', 'name': 'Night Ambient', 'group': 'Search results', 'kind': 'radio',
     'url': 'https://radio.example.test/night-ambient', 'country': 'Japan', 'language': 'Japanese',
     'tags': ['ambient', 'night'], 'website': ''},
]
CHANNELS = [
    {'login': 'lofigirl', 'name': 'lofigirl', 'url': 'https://www.twitch.tv/lofigirl', 'live': True,
     'title': 'beats to relax'},
]
CONFIG = {name: {'endpoint': 'https://jellyfin.example.test', 'username': 'probe', 'user_id': 'probe-user',
                 'token_file': ''} for name in ('jellyfin', 'music_assistant', 'audiomuse', 'home_assistant')}
LIBRARY_STATION = {
    'name': 'Late reading',
    'sources': [{'reference': {'provider': 'jellyfin', 'id': 'album-neoclassical', 'type': 'MusicAlbum',
                               'name': 'Neoclassical collection', 'genres': ['Neoclassical']}}],
    'fallbacks': [
        {'source_id': 'somafm-drone-zone', 'name': 'SomaFM Drone Zone',
         'url': 'https://somafm.com/dronezone.pls', 'kind': 'radio'},
        {'source_id': 'ylex', 'name': 'YleX', 'url': 'https://icecast.live.yle.fi/radio/YleX/i', 'kind': 'radio'},
    ],
    'preferences': {'energy': 'Low', 'familiarity': 'Any', 'speech': 'Any'},
    'end_policy': 'stop', 'arc': 'wind_down', 'generation': {'kind': 'collection'},
}
RADIO_STATION = {
    'name': 'Morning lift',
    'sources': [{'source_id': 'somafm-groove-salad', 'name': 'SomaFM Groove Salad',
                 'url': 'https://somafm.com/groovesalad.pls', 'kind': 'radio'}],
    'fallbacks': [], 'preferences': {}, 'end_policy': 'stop', 'arc': 'none',
    'generation': {'kind': 'collection'},
}

with tempfile.TemporaryDirectory(prefix='listening-listen-') as folder:
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

    # Capture accessible names at the GTK boundary, before any UI module loads.
    _update_property = Gtk.Accessible.update_property

    def _capture_label(self, properties, values):
        for prop, value in zip(properties, values):
            if prop == Gtk.AccessibleProperty.LABEL:
                self._probe_label = value
        return _update_property(self, properties, values)
    Gtk.Accessible.update_property = _capture_label

    import audio_devices
    import listening_hub as hub
    import next_station
    import online_services
    import room_session
    import session_client
    import session_service
    import twitch_channels
    from test_listening_session import FakeBackend
    from unittest.mock import patch

    class ProbeBackend(FakeBackend):
        """The session test double, with names the real players report."""
        def start_atmosphere(self, selection, cancel):
            super().start_atmosphere(selection, cancel)
            import atmosphere_catalogue
            sound = next((x for x in atmosphere_catalogue.SOUNDS if x['id'] == selection.get('source_id')), {})
            if sound:
                self.state['atmosphere'].update(name=sound['name'], outdoor=sound['outdoor'])

        def start_picture(self, picture, cancel=None):
            super().start_picture(picture, cancel)
            import ambience
            preset = next((p for p in ambience.presets() if p['id'] == picture.get('preset_id')), {})
            self.state['picture'].update(name=preset.get('name', 'Scene'), placement=picture.get('placement'))

    backend = ProbeBackend()
    controller = room_session.Controller(backend, Path(folder) / 'controller')
    controller._home_action = lambda *_: None
    controller._route_preferred = lambda: None
    service = session_service.Service(controller)
    log = []
    system_volume = {'percent': 40}
    outputs = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True}]

    def request(action, arguments=None, *_args, **_kw):
        arguments = arguments or {}
        log.append((action, copy.deepcopy(arguments)))
        return service.execute(action, arguments)

    def search_radio(query, **kwargs):
        text = str(query or '').casefold()
        return [copy.deepcopy(item) for item in WORLD
                if not text or text in (item['name'] + ' ' + ' '.join(item['tags'])).casefold()]

    def browse_jellyfin(query='', kind='albums', parent_id=None, cancel_event=None):
        text = str(query or '').casefold()
        return [copy.deepcopy(item) for item in ALBUMS
                if not text or text in (item['name'] + ' ' + item.get('artist', '')).casefold()]

    def jellyfin_queue(item, cancel_event=None):
        return [copy.deepcopy(track) for track in TRACKS]

    def prepare_playback(reference, cancel_event=None):
        return dict(reference, url='file:///fixture/audio.flac')

    def discover(style, curated=None, client_id=None, access_token=None):
        return {'status': 'live', 'channels': copy.deepcopy(CHANNELS), 'curated_status': {}, 'message': ''}

    patches = [patch.object(session_client, 'request', side_effect=request),
               patch.object(audio_devices, 'outputs', return_value=outputs),
               patch.object(audio_devices, 'test_sound', side_effect=lambda name=None: None),
               patch.object(hub, 'system_volume', side_effect=lambda: dict(system_volume)),
               patch.object(hub, 'set_volume', side_effect=lambda value, **kw: system_volume.update(percent=value)),
               patch.object(hub, 'search_radio', side_effect=search_radio),
               patch.object(online_services, 'config', side_effect=lambda: copy.deepcopy(CONFIG)),
               patch.object(online_services, 'browse_jellyfin', side_effect=browse_jellyfin),
               patch.object(online_services, 'jellyfin_queue', side_effect=jellyfin_queue),
               patch.object(online_services, 'prepare_playback', side_effect=prepare_playback),
               patch.object(online_services, 'jellyfin_link',
                            side_effect=lambda item: 'https://jellyfin.example.test/web/#/details?id=' + str(item.get('id'))),
               patch.object(twitch_channels, 'curated_channels',
                            side_effect=lambda style: ['lofigirl'] if style == 'music' else []),
               patch.object(twitch_channels, 'excluded_channels', return_value=[]),
               patch.object(twitch_channels, 'discover', side_effect=discover)]
    for item in patches:
        item.start()

    for attempt in TRACKS:
        digest = hashlib.sha256(attempt['id'].encode()).hexdigest()
        controller.metadata_store.observe('jellyfin:fixture', attempt['id'], digest)
        if attempt['id'] in ('track-1', 'track-2'):
            controller.metadata_store.review('jellyfin:fixture', attempt['id'], digest, 'instrumental', 'probe')

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

    def settle(seconds=.8):
        controller.worker.submit(lambda: None).result(timeout=5)
        pump(seconds)

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

    def buttons(root, text):
        found = []
        for widget in widgets(root):
            if not isinstance(widget, Gtk.Button) or not widget.get_mapped():
                continue
            value = widget.get_label()
            if value is None:
                value = ' '.join(item.get_text() for item in widgets(widget)
                                 if isinstance(item, Gtk.Label) and item.get_text())
            if value == text or (not value and widget.get_tooltip_text() == text):
                found.append(widget)
        return found

    def labels(root, text):
        return [widget for widget in widgets(root)
                if isinstance(widget, Gtk.Label) and widget.get_text() == text and widget.get_mapped()]

    def press(root, text):
        found = buttons(root, text)
        assert found, 'no button %r; have %r' % (text, sorted({t for t in texts(root)}))
        found[0].emit('clicked')
        pump(.3)
        return found[0]

    def launch(width=1200, height=800, listen=True, argv=None):
        saved = sys.argv
        if argv is not None:
            sys.argv = argv
        try:
            window = next_window.NextWindow(app)
        finally:
            sys.argv = saved
        window.set_default_size(width, height)
        window.present()
        pump(1.2)
        got = (window.get_width(), window.get_height())
        if got != (width, height):   # bare Xvfb adds window-frame size differences
            window.set_default_size(width + (width - got[0]), height + (height - got[1]))
            pump(1.0)
        assert (window.get_width(), window.get_height()) == (width, height), \
            'window is %dx%d, wanted %dx%d' % (window.get_width(), window.get_height(), width, height)
        if listen:
            window.show_listen()
            pump(1.0)
        return window

    def shot(window, name):
        pump(.5)
        from PIL import ImageGrab
        width, height = window.get_width(), window.get_height()
        assert width and height, 'window did not construct'
        path = output / (name + '.png')
        ImageGrab.grab(bbox=(0, 0, width, height), xdisplay=os.environ['DISPLAY']).save(path)
        return path

    def parents(widget):
        parent = widget.get_parent()
        while parent:
            yield parent
            parent = parent.get_parent()

    def assert_layout(window, where):
        width, height = window.get_width(), window.get_height()
        for widget in widgets(window):
            if isinstance(widget, Gtk.ScrolledWindow) and widget.get_mapped():
                adjustment = widget.get_hadjustment()
                assert adjustment.get_upper() <= adjustment.get_page_size() + 1, '%s scrolls horizontally' % where
        for widget in widgets(window):
            if isinstance(widget, (Gtk.Button, Gtk.Scale, Gtk.MenuButton, Gtk.Switch)) and widget.get_mapped() \
                    and widget.get_child_visible():
                ok, bounds = widget.compute_bounds(window)
                if not ok:
                    continue
                scrolled = any(isinstance(parent, Gtk.ScrolledWindow) for parent in parents(widget))
                assert bounds.get_x() >= -1 and bounds.get_x() + bounds.get_width() <= width + 1, \
                    '%s: control %r is clipped horizontally (%s)' % (where, widget.get_tooltip_text(),
                                                                     bounds.get_x() + bounds.get_width())
                if not scrolled:
                    assert bounds.get_y() >= -1 and bounds.get_y() + bounds.get_height() <= height + 1, \
                        '%s: control %r is clipped vertically' % (where, widget.get_tooltip_text())

    FORBIDDEN = ('Main audio', 'Background audio', 'Picture', 'Retry picture', 'Refresh')

    def assert_vocabulary(window, where):
        for text in texts(window):
            for word in FORBIDDEN:
                assert word not in text, '%s shows %r in %r' % (where, word, text)
            if 'Workspace 9' in text:
                assert 'Workspace 9' == text, 'Workspace 9 outside a placement value: %r' % text

    READ_ONLY = {'inspect', 'get', 'variants', 'station.list', 'station.recent', 'metadata.list', 'metadata.status',
                 'metadata.jobs', 'metadata.inspect'}

    def mutating(since):
        return [entry for entry in log[since:] if entry[0] not in READ_ONLY]

    def assert_inert(where, since):
        sent = mutating(since)
        assert not sent, '%s sent %r' % (where, sent)

    def row_widget(window, name):
        for widget in widgets(window.listen.index_body):
            if isinstance(widget, Gtk.Label) and widget.get_text() == name:
                for parent in parents(widget):
                    if isinstance(parent, Gtk.Box) and parent.has_css_class('listen-row'):
                        return parent
                    if parent is window.listen.index_body:
                        break
        return None

    def station(name):
        return next(item for item in controller.stations() if item['name'] == name)

    try:
        # ---------------------------------------------------------------- fixtures
        service.execute('station.save', {'station': LIBRARY_STATION})
        service.execute('station.save', {'station': RADIO_STATION})
        log.clear()

        # A library station plays, so its history keeps track references.
        service.execute('station.start', {'id': station('Late reading')['id']})
        settle(1.2)
        assert controller.active and controller.program, 'the library station did not start'
        service.execute('layer', {'layer': 'music', 'action': 'next'})
        settle(.8)
        service.execute('layer', {'layer': 'music', 'action': 'next'})
        settle(.8)
        assert len(controller.station_history[0]['tracks']) == 3, controller.station_history[0]
        # A radio station then plays, so its history entry has no captured tracks.
        service.execute('station.start', {'id': station('Morning lift')['id']})
        settle(1.2)
        assert controller.active['music']['station']['id'] == station('Morning lift')['id']
        assert len(controller.station_history) == 2 and controller.station_history[0]['tracks'] == []

        log.clear()
        window = launch()
        page = window.listen
        assert window.current_page() == 'browse' and window.browse.get_visible_child_name() == 'listen'
        assert page.bar.get_visible(), 'the now-playing bar must show while a room is active'
        assert any(label.get_text() == 'Morning lift' for label in widgets(page.bar) if isinstance(label, Gtk.Label))
        for required in ('All', 'Your stations', 'Radio', 'Library', 'Live channels', 'World radio'):
            assert labels(page.scopes, required), 'missing scope %r' % required
        for shelf in ('Your stations', 'Recently played', 'Radio', 'Library', 'Live channels'):
            assert labels(page.index_body, shelf), 'missing shelf %r; have %r' % (
                shelf, sorted({t for t in texts(page.index_body) if len(t) < 40})[:40])
        expander = next((widget for widget in widgets(page.index_body)
                         if isinstance(widget, Gtk.Expander) and widget.get_label() == 'Open elsewhere'), None)
        assert expander is not None and not expander.get_expanded(), 'Open elsewhere must be collapsed at the end'
        assert labels(page.index_body, 'Neoclassical collection'), 'the library shelf did not load'
        assert labels(page.index_body, 'lofigirl'), 'the live channel shelf did not load'
        assert buttons(page.index_body, 'Tune in'), 'station cards and rows need Tune in'
        assert buttons(page.index_body, 'New station'), 'New station is missing'
        assert_vocabulary(window, 'Listen')
        assert_layout(window, 'Listen 1200x800')
        shot(window, '01-listen-shelves')

        # Recently played: Replay with captured tracks, Tune in without them.
        actions = {row['name']: row['action'] for row in next_station.history_rows(
            controller.snapshot()['station_recent'], time.time())}
        assert actions.get('Late reading') == 'replay', actions
        assert actions.get('Morning lift') == 'tune', actions
        mark = len(log)
        for step in (lambda: page.scopes.set_active(1), lambda: page.scopes.set_active(0),
                     lambda: page.scopes.set_active(5), lambda: page.scopes.set_active(0)):
            step()
            pump(.4)
        page.search.set_text('groove')
        pump(.9)
        page.search.set_text('')
        pump(.6)
        assert_inert('browsing shelves, switching scopes and typing', mark)

        # ---------------------------------------------------------------- search
        mark = len(log)
        page.search.set_text('groove')
        pump(1.2)
        assert any('Groove Salad' in text for text in texts(page.index_body)), \
            [text for text in texts(page.index_body)][:20]
        shot(window, '02-search-results')
        assert_inert('searching All', mark)

        # The World radio scope searches off the GTK thread with fixture data.
        page.scopes.set_active(5)
        page.search.set_text('ambient')
        pump(1.6)
        world_texts = [text for text in texts(page.index_body)]
        assert any('Ambient Drift' in text for text in world_texts), world_texts[:20]
        assert labels(page.index_body, 'World radio'), 'no World radio section'
        shot(window, '03-world-radio-scope')
        assert_inert('searching World radio', mark)

        # A pasted public URL shows up as a link result.
        page.scopes.set_active(0)
        page.search.set_text('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        pump(1.2)
        assert labels(page.index_body, 'YouTube video'), [text for text in texts(page.index_body)][:20]
        assert buttons(page.index_body, 'Open link…'), 'the link result has no action'
        assert_inert('typing a public link', mark)

        # ---------------------------------------------------------------- row menu
        page.search.set_text('')
        pump(1.0)
        page.scopes.set_active(2)
        pump(.6)
        row = row_widget(window, 'SomaFM Groove Salad')
        assert row is not None, 'no radio row for the menu check'
        menu_buttons = [widget for widget in widgets(row) if isinstance(widget, Gtk.MenuButton)]
        assert menu_buttons, 'rows have no menus'
        menu_buttons[0].popup()
        pump(.6)
        popover = menu_buttons[0].get_popover()
        items = ' '.join(widget.get_text() for widget in widgets(popover) if isinstance(widget, Gtk.Label))
        for text in ('Play as ambience', 'Save as station', 'Favourite', 'Open elsewhere'):
            assert text in items, (text, items)
        shot(window, '04-row-menu')
        mark = len(log)
        press(popover, 'Favourite')
        settle(.8)
        assert not [entry for entry in log[mark:] if entry[0] not in READ_ONLY], 'Favourite sent a room command'
        assert hub.favorites(), 'Favourite did not reach the favourites file'
        # The shelves rebuild after a favourites change: use a fresh row for the
        # inert check rather than the destroyed menu.
        pump(.6)
        row = row_widget(window, 'SomaFM Groove Salad')
        menu = next((widget for widget in widgets(row) if isinstance(widget, Gtk.MenuButton)), None)
        assert menu is not None
        mark = len(log)
        menu.popup()
        pump(.6)
        menu.popdown()
        pump(.4)
        assert_inert('opening a row menu', mark)
        page.scopes.set_active(0)
        pump(.6)

        # ---------------------------------------------------------------- station detail
        mark = len(log)
        card = None
        for widget in widgets(page.index_body):
            if isinstance(widget, Gtk.Button) and any(
                    isinstance(item, Gtk.Label) and item.get_text() == 'Late reading' for item in widgets(widget)):
                card = widget
                break
        assert card is not None, 'no station card for Late reading'
        card.emit('clicked')
        pump(.8)
        shown = list(texts(page.station_holder))
        for text in ('Mood', 'Energy', 'Familiarity', 'Voices', 'Over time', 'When it runs out',
                     'If it can’t play, try', 'What it has learned', 'Show what’s up next',
                     'Tune in', 'Play as ambience', 'Late reading', 'Library station'):
            assert any(text in value for value in shown), 'station detail is missing %r' % text
        assert_vocabulary(window, 'Station detail')
        assert_layout(window, 'Station detail 1200x800')
        shot(window, '05-station-detail')
        assert_inert('opening a station', mark)

        # Mood choices save automatically, with one toast and Undo, and no Save button.
        assert not buttons(page.station_holder, 'Save'), 'station detail must not have a Save button'
        mark = len(log)
        page.mood_groups['energy'].set_active(2)
        settle(1.0)
        saved = [entry for entry in log[mark:] if entry[0] == 'station.save']
        assert saved, 'a mood change must save'
        assert saved[-1][1]['station']['preferences']['energy'] == 'High', saved[-1]
        assert station('Late reading')['preferences']['energy'] == 'High'
        assert buttons(window.toasts, 'Undo'), 'the mood toast needs Undo'
        shot(window, '06-station-mood-toast')
        press(window.toasts, 'Undo')
        settle(1.0)
        assert station('Late reading')['preferences']['energy'] == 'Low', 'Undo must restore the previous mood'

        # Instrumental only shows how many reviewed recordings are available.
        page.mood_groups['speech'].set_active(2)
        settle(1.2)
        pump(1.0)
        assert station('Late reading')['preferences']['speech'] == 'Strict instrumental'
        hints = [label.get_text() for label in widgets(page.station_holder)
                 if isinstance(label, Gtk.Label) and 'reviewed recording' in label.get_text()]
        assert hints and '2 reviewed recordings available' in hints[0], hints
        shot(window, '07-station-instrumental-hint')

        # Fallbacks: ordered, movable from the keyboard and by drag, saved as they move.
        before = [item['name'] for item in station('Late reading')['fallbacks']]
        assert before == ['SomaFM Drone Zone', 'YleX'], before
        mark = len(log)
        press(page.station_holder, 'Move YleX up')
        settle(1.0)
        after = [item['name'] for item in station('Late reading')['fallbacks']]
        assert after == ['YleX', 'SomaFM Drone Zone'], after
        moves = [entry for entry in log[mark:] if entry[0] == 'station.save']
        assert moves and [item['name'] for item in moves[-1][1]['station']['fallbacks']] == after, moves
        assert buttons(page.station_holder, 'Move SomaFM Drone Zone down'), 'fallback order buttons are missing'
        rows = [widget for widget in widgets(page.station_holder) if isinstance(widget, Gtk.Box)
                and widget.has_css_class('fallback-row')]
        assert rows, 'no fallback rows'
        assert any(isinstance(item, Gtk.DragSource) for item in rows[0].observe_controllers()), \
            'fallback rows need a drag source'
        assert any(isinstance(item, Gtk.DropTarget) for item in rows[0].observe_controllers()), \
            'fallback rows need a drop target'
        assert page.dropped_fallback('0', 1), 'the drop handler must reorder'
        settle(1.0)
        dropped = [item['name'] for item in station('Late reading')['fallbacks']]
        assert dropped == ['SomaFM Drone Zone', 'YleX'], dropped
        shot(window, '08-fallbacks-reordered')

        # Up next is only shown on request.
        assert not page.upcoming.get_expanded(), 'up next must stay collapsed until asked'
        page.upcoming.set_expanded(True)
        pump(.5)
        assert any('Quiet study' == label.get_text() for label in widgets(page.upcoming_box)
                   if isinstance(label, Gtk.Label)) or \
            next_station.upcoming(controller.snapshot()['station'], station('Late reading')) == [], \
            'expanding up next shows the queue'
        page.upcoming.set_expanded(False)

        # ---------------------------------------------------------------- history
        mark = len(log)
        page.show_index()
        pump(.6)
        press(page.index_body, 'History')
        pump(.8)
        history_texts = [text for text in texts(page.history_body)]
        assert any('Late reading' in text for text in history_texts), history_texts[:20]
        assert buttons(page.history_body, 'Replay') and buttons(page.history_body, 'Tune in'), history_texts[:20]
        assert_vocabulary(window, 'History')
        assert_layout(window, 'History 1200x800')
        shot(window, '09-history')
        assert_inert('opening History', mark)
        page.show_index()
        pump(.4)

        # ---------------------------------------------------------------- link sheet
        page.search.set_text('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        pump(1.0)
        mark = len(log)
        press(page.index_body, 'Open link…')
        pump(.8)
        dialog = window.get_visible_dialog()
        assert dialog is not None, 'the link sheet did not open'
        offer = ' '.join(label.get_text() for label in widgets(dialog) if isinstance(label, Gtk.Label))
        for text in ('Play', 'Play as ambience', 'Show as scene', 'Open elsewhere'):
            assert text in offer, (text, offer)
        assert_inert('opening the link sheet', mark)
        shot(window, '10-link-sheet-youtube')
        for child in [widget for widget in widgets(window) if isinstance(widget, Adw.Dialog)]:
            child.force_close()
        pump(.4)
        page.search.set_text('')
        pump(.6)

        # A listening-room://open URI arrives as a launch argument.
        window.close()
        pump(.4)
        uri = 'listening-room://open?url=' + 'https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ'
        mark = len(log)
        window = launch(argv=['listening-room', '--ui=next', uri])
        pump(.8)
        sheet = window.get_visible_dialog()
        assert sheet is not None, 'the launch URI did not open the link sheet'
        assert any('YouTube video' == label.get_text() for label in widgets(sheet) if isinstance(label, Gtk.Label))
        assert_inert('launching with a listening-room://open URI', mark)
        for child in [widget for widget in widgets(window) if isinstance(widget, Adw.Dialog)]:
            child.force_close()
        pump(.4)

        # Ctrl+F, Ctrl+L and Ctrl+V are registered; Ctrl+F focuses the search.
        triggers = set()
        for widget in widgets(window):
            for controller_ in widget.observe_controllers():
                if isinstance(controller_, Gtk.ShortcutController):
                    for index in range(controller_.get_n_items()):
                        triggers.add(controller_.get_item(index).get_trigger().to_string())
        for trigger in ('<Control>f', '<Control>l', '<Control>v'):
            assert trigger in triggers, (trigger, triggers)
        page = window.listen
        page.shortcut_search()
        pump(.5)
        node = window.get_focus()
        while node is not None and node is not page.search:
            node = node.get_parent()
        assert node is page.search, 'Ctrl+F must focus the search entry'

        # Typing focuses the search too, from a row's own button.
        page.show_index()
        page.search.set_text('')
        pump(.5)
        first_row_button = next((widget for widget in widgets(page.index_body)
                                 if isinstance(widget, Gtk.Button) and widget.get_label() in ('Tune in', 'Replay')),
                                None)
        assert first_row_button is not None
        first_row_button.grab_focus()
        page.typed(None, Gdk.KEY_y, 0, 0)
        pump(.4)
        assert page.search.get_text() == 'y', page.search.get_text()
        page.search.set_text('')
        pump(.4)

        # Icon-only controls carry accessible names (menu buttons, the bar's controls).
        window.show_listen()
        pump(.6)
        unnamed = []
        compound = (Gtk.MenuButton, Gtk.WindowControls, Gtk.DropDown, Gtk.Expander, Adw.ToggleGroup)
        for widget in widgets(window):
            if not isinstance(widget, (Gtk.Button, Gtk.MenuButton, Gtk.ToggleButton)) or not widget.get_mapped():
                continue
            if any(isinstance(parent, compound) for parent in parents(widget)):
                continue
            if [item for item in widgets(widget) if isinstance(item, Gtk.Label) and item.get_text()]:
                continue
            if getattr(widget, '_probe_label', None):
                continue
            unnamed.append((type(widget).__name__, widget.get_tooltip_text()))
        assert not unnamed, 'icon-only controls without accessible names: %r' % (unnamed,)

        # ---------------------------------------------------------------- new station
        mark = len(log)
        press(page.index_body, 'New station')
        pump(.8)
        dialog = window.get_visible_dialog()
        assert dialog is not None, 'New station did not open a dialog'
        entries = [widget for widget in widgets(dialog) if isinstance(widget, Gtk.Entry)]
        assert entries, 'New station has no name entry'
        entries[0].set_text('Probe station')
        # The dialog is an Adw.Dialog with its own Create button (it has no "response" signal): give it a start
        # through the link page, then create.
        entries[1].set_text('https://radio.example.test/probe-station.mp3')
        use = [w for w in widgets(dialog) if isinstance(w, Gtk.Button) and w.get_label() == 'Use this link']
        assert use, 'New station has no Use this link'
        use[0].emit('clicked')
        pump(.4)
        create = [w for w in widgets(dialog) if isinstance(w, Gtk.Button) and w.get_label() == 'Create station']
        assert create and create[0].get_sensitive(), 'Create station is not offered after a start was chosen'
        create[0].emit('clicked')
        settle(1.2)
        assert any(entry[0] == 'station.save' and entry[1]['station']['name'] == 'Probe station'
                   for entry in log[mark:]), mutating(mark)
        assert station('Probe station')
        pump(.6)
        for child in [widget for widget in widgets(window) if isinstance(widget, Adw.Dialog)]:
            child.force_close()
        page.show_index()
        pump(.6)

        # ---------------------------------------------------------------- Tune in is explicit
        mark = len(log)
        page.scopes.set_active(2)
        pump(.6)
        tune = None
        row = row_widget(window, 'SomaFM Groove Salad')
        if row is not None:
            tune = next((widget for widget in widgets(row) if isinstance(widget, Gtk.Button)
                         and widget.get_label() == 'Tune in'), None)
        assert tune is not None, 'no Tune in for a radio row'
        tune.emit('clicked')
        settle(1.2)
        sent = mutating(mark)
        assert sent and sent[0][0] in ('music', 'station.start'), sent
        assert controller.active, 'Tune in must start the room'
        page.show_index()
        page.scopes.set_active(0)
        pump(.6)

        # ---------------------------------------------------------------- 800 px
        window.close()
        pump(.4)
        window = launch(800, 700)
        page = window.listen
        assert page.bar.get_visible()
        assert_layout(window, 'Listen 800x700')
        assert_vocabulary(window, 'Listen 800')
        shot(window, '11-listen-800')
        page.show_station(station('Late reading'))
        pump(.8)
        assert_layout(window, 'Station detail 800x700')
        shot(window, '12-station-800')
        page.show_history()
        pump(.6)
        assert_layout(window, 'History 800x700')
        page.show_index()
        pump(.4)
        window.close()
        pump(.3)
        print('PASS: Listen shelves and scopes, inert search, link results and sheet, launch URI, station'
              ' autosave and Undo, fallbacks, history, New station, keyboard, accessibility, 800 px')
    finally:
        for item in reversed(patches):
            item.stop()
        controller.close()
