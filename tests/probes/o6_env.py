"""Shared fixture environment for the O6 probes (timing, selection audit and evidence screenshots).

Real GTK on a private Xvfb and D-Bus, the real session controller on the session test double,
and fixture providers (radio, Jellyfin library, Twitch, yt-dlp). Nothing reaches the network,
the desktop session or real credentials. Used as:

    env = o6_env.Env(__doc__)          # parses --source/--output and re-executes on Xvfb if needed
    with env:
        window = env.launch()
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

TRACKS = [
    {'provider': 'jellyfin', 'id': 'track-1', 'type': 'Audio', 'name': 'Nocturne in C',
     'artist': 'Someone', 'album': 'Neoclassical collection'},
    {'provider': 'jellyfin', 'id': 'track-2', 'type': 'Audio', 'name': 'Quiet study',
     'artist': 'Someone Else', 'album': 'Neoclassical collection'},
]
ALBUMS = [
    {'provider': 'jellyfin', 'id': 'album-neoclassical', 'type': 'MusicAlbum',
     'name': 'Neoclassical collection', 'artist': 'Various', 'genres': ['Neoclassical'], 'image_id': 'album-neoclassical'},
    {'provider': 'jellyfin', 'id': 'album-ambient', 'type': 'MusicAlbum',
     'name': 'Ambient works', 'artist': 'Various', 'genres': ['Ambient'], 'image_id': 'album-ambient'},
]
PLAYLISTS = [{'provider': 'jellyfin', 'id': 'playlist-evening', 'type': 'Playlist', 'name': 'Evening wind-down',
              'artist': '', 'genres': []}]
WORLD = [
    {'id': 'world-ambient-drift', 'name': 'Ambient Drift', 'group': 'Search results', 'kind': 'radio',
     'url': 'https://radio.example.test/ambient-drift', 'country': 'Finland', 'language': 'Finnish',
     'tags': ['ambient', 'calm'], 'website': 'https://radio.example.test/'},
    {'id': 'world-night-ambient', 'name': 'Night Ambient', 'group': 'Search results', 'kind': 'radio',
     'url': 'https://radio.example.test/night-ambient', 'country': 'Japan', 'language': 'Japanese',
     'tags': ['ambient', 'night'], 'website': ''},
]
VIDEOS = [
    {'kind': 'youtube', 'id': 'aaaaaaaaaaa', 'name': 'Rain on a tin roof, 3 hours', 'channel': 'Quiet Sounds',
     'duration': '3:00:00', 'live': False, 'url': 'https://www.youtube.com/watch?v=aaaaaaaaaaa'},
    {'kind': 'youtube', 'id': 'bbbbbbbbbbb', 'name': 'Lo-fi study mix', 'channel': 'Example Radio',
     'duration': '58:12', 'live': False, 'url': 'https://www.youtube.com/watch?v=bbbbbbbbbbb'},
    {'kind': 'youtube', 'id': 'ccccccccccc', 'name': 'Fireplace, no talking', 'channel': 'Cosy Corner',
     'duration': '10:00', 'live': False, 'url': 'https://www.youtube.com/watch?v=ccccccccccc'},
]
LIVE_CHANNELS = [
    {'login': 'lofigirl', 'name': 'lofigirl', 'url': 'https://www.twitch.tv/lofigirl', 'live': True, 'title': 'beats to relax'},
    {'login': 'jazzhopcafe', 'name': 'jazzhopcafe', 'url': 'https://www.twitch.tv/jazzhopcafe', 'live': True,
     'title': 'jazz hop'},
]
CONFIG = {name: {'endpoint': 'https://jellyfin.example.test', 'username': 'probe', 'user_id': 'probe-user',
                 'token_file': ''} for name in ('jellyfin', 'music_assistant', 'audiomuse', 'home_assistant')}
RADIO_STATION = {
    'name': 'Morning lift',
    'sources': [{'source_id': 'somafm-groove-salad', 'name': 'SomaFM Groove Salad',
                 'url': 'https://somafm.com/groovesalad.pls', 'kind': 'radio'}],
    'fallbacks': [], 'preferences': {}, 'end_policy': 'stop', 'arc': 'none', 'generation': {'kind': 'collection'},
}


class Env:
    def __init__(self, doc, default_output='/tmp/listening-o6-review', extra_args=None):
        parser = argparse.ArgumentParser(description=(doc or '').split('\n')[0])
        parser.add_argument('--source', default=str(Path(__file__).resolve().parents[2] / 'src'))
        parser.add_argument('--output', default=default_output)
        for flag, kwargs in (extra_args or {}).items():
            parser.add_argument(flag, **kwargs)
        self.args = parser.parse_args()
        if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
            runtime = tempfile.mkdtemp(prefix='listening-o6-runtime-')
            os.chmod(runtime, 0o700)
            environment = {key: value for key, value in os.environ.items()
                           if key not in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE')}
            environment.update(XDG_RUNTIME_DIR=runtime, GDK_BACKEND='x11', GDK_DEBUG='no-portals', GSK_RENDERER='cairo',
                               LISTENING_PROBE_ISOLATED='1')
            command = ['xvfb-run', '-a', '-s', '-screen 0 1440x1000x24', 'dbus-run-session', sys.executable] + sys.argv
            try:
                sys.exit(subprocess.call(command, env=environment))
            finally:
                shutil.rmtree(runtime, ignore_errors=True)
        self.source = Path(self.args.source).resolve()
        sys.path.insert(0, str(self.source))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        os.environ['LISTENING_HUB_SOURCE'] = str(self.source / 'listening_hub.py')  # every helper follows --source
        self.output = Path(self.args.output)
        self.output.mkdir(parents=True, exist_ok=True)
        # Fixture knobs, read at call time by the fakes below.
        self.twitch_connected = False
        self.twitch_curated = []
        self.twitch_live = copy.deepcopy(LIVE_CHANNELS)
        self.yt_available = True
        self.yt_delay = 0.
        self.log = []
        self.yt_queries = []
        self.start_delay = 0.        # seconds a music start takes (a slow stream connect)
        self.start_error = None      # a message the music start fails with
        self.atmosphere_error = None

    # ---- setup -----------------------------------------------------------------------

    def __enter__(self):
        self.folder_context = tempfile.TemporaryDirectory(prefix='listening-o6-')
        folder = self.folder_context.name
        self.folder = folder
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
        self.Adw, self.Gdk, self.GLib, self.Gtk = Adw, Gdk, GLib, Gtk
        import audio_devices
        import listening_hub as hub
        import online_services
        import room_session
        import session_client
        import session_service
        import twitch_channels
        import twitch_credentials
        from test_listening_session import FakeBackend
        from unittest.mock import patch
        self.hub, self.online_services, self.twitch_channels = hub, online_services, twitch_channels
        env = self

        class ProbeBackend(FakeBackend):
            def start_music(self, source, cancel):
                deadline = time.monotonic() + env.start_delay
                while time.monotonic() < deadline and not cancel.is_set():
                    time.sleep(.02)
                if env.start_error:
                    raise ValueError(env.start_error)
                return super().start_music(source, cancel)

            def start_atmosphere(self, selection, cancel):
                if env.atmosphere_error and selection.get('kind') != 'none':
                    raise ValueError(env.atmosphere_error)
                super().start_atmosphere(selection, cancel)
                import atmosphere_catalogue
                sound = next((x for x in atmosphere_catalogue.SOUNDS if x['id'] == selection.get('source_id')), {})
                if sound:
                    self.state['atmosphere'].update(name=sound['name'], outdoor=sound['outdoor'])
                elif selection.get('name'):
                    self.state['atmosphere'].update(name=selection['name'])

            def start_picture(self, picture, cancel=None):
                super().start_picture(picture, cancel)
                import ambience
                preset = next((p for p in ambience.presets() if p['id'] == picture.get('preset_id')), {})
                self.state['picture'].update(name=preset.get('name', 'Scene'), placement=picture.get('placement'))

        self.backend = ProbeBackend()
        self.controller = room_session.Controller(self.backend, Path(folder) / 'controller')
        self.controller._home_action = lambda *_: None
        self.controller._route_preferred = lambda: None
        self.service = session_service.Service(self.controller)
        system_volume = {'percent': 40}
        outputs = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True}]

        def request(action, arguments=None, *_a, **_k):
            arguments = arguments or {}
            env.log.append((action, copy.deepcopy(arguments)))
            return env.service.execute(action, arguments)

        def search_radio(query, **kwargs):
            text = str(query or '').casefold()
            return [copy.deepcopy(item) for item in WORLD
                    if not text or text in (item['name'] + ' ' + ' '.join(item['tags'])).casefold()]

        def browse_jellyfin(query='', kind='albums', parent_id=None, cancel_event=None):
            text = str(query or '').casefold()
            pool = {'albums': ALBUMS, 'playlists': PLAYLISTS, 'tracks': TRACKS, 'recent': ALBUMS}.get(kind, ALBUMS)
            return [copy.deepcopy(item) for item in pool
                    if not text or text in (item['name'] + ' ' + item.get('artist', '')).casefold()]

        def credentials():
            if env.twitch_connected:
                return twitch_credentials.Credentials(client_id='fixture-client', access_token='fixture-token')
            return twitch_credentials.Credentials()

        def discover(style, curated=None, client_id=None, access_token=None):
            if not env.twitch_connected:
                names = list(env.twitch_curated if curated is None else curated)
                return {'status': 'unknown', 'message': 'Connect Twitch through the CLI to discover live channels.',
                        'reason': 'configuration', 'channels': [
                            {'login': n, 'name': n, 'url': 'https://www.twitch.tv/' + n, 'live': None} for n in names]}
            return {'status': 'live', 'channels': copy.deepcopy(env.twitch_live) if style == 'music' else [],
                    'curated_status': {}, 'message': ''}

        self.patches = [patch.object(session_client, 'request', side_effect=request),
                        patch.object(audio_devices, 'outputs', return_value=outputs),
                        patch.object(audio_devices, 'test_sound', side_effect=lambda name=None: None),
                        patch.object(hub, 'system_volume', side_effect=lambda: dict(system_volume)),
                        patch.object(hub, 'set_volume', side_effect=lambda value, **kw: system_volume.update(percent=value)),
                        patch.object(hub, 'search_radio', side_effect=search_radio),
                        patch.object(online_services, 'config', side_effect=lambda: copy.deepcopy(CONFIG)),
                        patch.object(online_services, 'browse_jellyfin', side_effect=browse_jellyfin),
                        patch.object(online_services, 'jellyfin_queue', side_effect=lambda item, cancel_event=None: copy.deepcopy(TRACKS)),
                        patch.object(online_services, 'prepare_playback',
                                     side_effect=lambda reference, cancel_event=None: dict(reference, url='file:///fixture/audio.flac')),
                        patch.object(online_services, 'jellyfin_link',
                                     side_effect=lambda item: 'https://jellyfin.example.test/web/#/details?id=' + str(item.get('id'))),
                        patch.object(twitch_credentials, 'load', side_effect=credentials),
                        patch.object(twitch_channels, 'curated_channels',
                                     side_effect=lambda style: list(env.twitch_curated) if style == 'music' else []),
                        patch.object(twitch_channels, 'excluded_channels', return_value=[]),
                        patch.object(twitch_channels, 'discover', side_effect=discover)]
        try:
            import youtube_search

            def fake_run(argv, cancel, timeout):
                env.yt_queries.append(argv[-1])
                deadline = time.monotonic() + env.yt_delay
                while time.monotonic() < deadline:
                    if cancel is not None and cancel.is_set():
                        raise youtube_search.SearchError('Search cancelled')
                    time.sleep(.02)
                import json
                return [json.dumps({'id': v['id'], 'title': v['name'], 'channel': v['channel'],
                                    'duration': sum(int(p) * m for p, m in zip(reversed(v['duration'].split(':')), (1, 60, 3600)))})
                        for v in VIDEOS]
            self.patches += [patch.object(youtube_search, 'run_process', side_effect=fake_run),
                             patch.object(youtube_search, 'executable',
                                          side_effect=lambda: '/fixture/yt-dlp' if env.yt_available else None)]
        except ImportError:
            pass
        for item in self.patches:
            item.start()
        for attempt in TRACKS:
            digest = hashlib.sha256(attempt['id'].encode()).hexdigest()
            self.controller.metadata_store.observe('jellyfin:fixture', attempt['id'], digest)
        import next_window
        self.next_window = next_window
        next_window.POLL_MS = 250
        self.app = next_window.NextApp()
        self.app.register(None)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self.context = GLib.MainContext.default()
        return self

    def __exit__(self, *_):
        for item in reversed(self.patches):
            item.stop()
        self.controller.close()
        self.folder_context.cleanup()

    # ---- helpers ---------------------------------------------------------------------

    def pump(self, seconds=.4):
        deadline = time.monotonic() + seconds
        last = 0
        while time.monotonic() < deadline:
            while self.context.pending():
                self.context.iteration(False)
            if time.monotonic() - last > .25:
                last = time.monotonic()
                self.controller.refresh()
            time.sleep(.01)

    def settle(self, seconds=.8):
        self.controller.worker.submit(lambda: None).result(timeout=5)
        self.pump(seconds)

    def widgets(self, root):
        yield root
        child = root.get_first_child()
        while child:
            yield from self.widgets(child)
            child = child.get_next_sibling()

    def texts(self, root):
        for widget in self.widgets(root):
            for getter in ('get_label', 'get_text', 'get_title', 'get_subtitle', 'get_tooltip_text'):
                value = getattr(widget, getter, lambda: None)()
                if isinstance(value, str) and value:
                    yield value

    def buttons(self, root, text):
        found = []
        for widget in self.widgets(root):
            if not isinstance(widget, self.Gtk.Button) or not widget.get_mapped():
                continue
            value = widget.get_label()
            if value is None:
                value = ' '.join(item.get_text() for item in self.widgets(widget)
                                 if isinstance(item, self.Gtk.Label) and item.get_text())
            if value == text or (not value and widget.get_tooltip_text() == text):
                found.append(widget)
        return found

    def labels(self, root, text):
        return [w for w in self.widgets(root) if isinstance(w, self.Gtk.Label) and w.get_text() == text and w.get_mapped()]

    def press(self, root, text):
        found = self.buttons(root, text)
        assert found, 'no button %r; have %r' % (text, sorted({t for t in self.texts(root)})[:60])
        found[0].emit('clicked')
        self.pump(.3)
        return found[0]

    def commands(self, since=0):
        return [entry for entry in self.log[since:] if entry[0] != 'inspect']

    def launch(self, width=1200, height=800, page='listen'):
        window = self.next_window.NextWindow(self.app)
        window.set_default_size(width, height)
        window.present()
        self.pump(1.2)
        got = (window.get_width(), window.get_height())
        if got != (width, height):
            window.set_default_size(width + (width - got[0]), height + (height - got[1]))
            self.pump(1.0)
        if page == 'listen':
            window.show_listen()
        elif page == 'rooms':
            window.show_rooms()
        self.pump(1.0)
        return window

    def shot(self, window, name):
        self.pump(.5)
        from PIL import ImageGrab
        width, height = window.get_width(), window.get_height()
        assert width and height, 'window did not construct'
        path = self.output / (name + '.png')
        ImageGrab.grab(bbox=(0, 0, width, height), xdisplay=os.environ['DISPLAY']).save(path)
        return path

    def start_station_room(self):
        """A room is playing a radio station, so Now and the Mix exist."""
        deadline = time.monotonic() + 8
        while self.controller.transition and time.monotonic() < deadline:
            self.pump(.2)
        if not any(s['name'] == 'Morning lift' for s in self.controller.stations()):
            self.service.execute('station.save', {'station': RADIO_STATION})
        station = next(s for s in self.controller.stations() if s['name'] == 'Morning lift')
        self.service.execute('station.start', {'id': station['id']})
        self.settle(1.2)
        assert self.controller.active, 'the station did not start'
        self.log.clear()
