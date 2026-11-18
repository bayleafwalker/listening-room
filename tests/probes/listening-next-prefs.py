"""Probe Preferences and Track details with real GTK, the real controller and fixtures.

Nothing touches the desktop session: the run re-executes itself on a private
Xvfb and D-Bus with portals off, HOME/XDG_* live in a temporary directory, and
session commands go to an in-process controller with a fake backend. Provider
sign-in and storage are patched to local fixtures, so no account is contacted
and no real secret is written, read or printed.

  bash scripts/listening-dev.sh shell     # then, inside it:
  python3 tests/probes/listening-next-prefs.py --output DIR

Without a DISPLAY the probe re-executes itself on a private Xvfb and D-Bus.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', default=str(Path(__file__).resolve().parents[2] / 'src'))
parser.add_argument('--output', default='/tmp/listening-next-prefs-review')
args = parser.parse_args()

if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
    runtime = tempfile.mkdtemp(prefix='listening-prefs-runtime-')
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
# Fixture values only; they are never real credentials and are never printed.
PASSWORD = 'fixture-password-not-a-real-secret'
TOKEN = 'fixture-token-not-a-real-secret'

with tempfile.TemporaryDirectory(prefix='listening-prefs-') as folder:
    os.environ['HOME'] = folder
    for key, suffix in [('XDG_STATE_HOME', 'state'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'),
                        ('XDG_DATA_HOME', 'data')]:
        path = Path(folder) / suffix
        path.mkdir(mode=0o700)
        os.environ[key] = str(path)
    os.environ['GTK_A11Y'] = 'none'
    os.environ.pop('ACOUSTID_CLIENT_KEY', None)

    # Local fixtures only: endpoints, accounts and check records, never live ones.
    config_dir = Path(os.environ['XDG_CONFIG_HOME']) / 'listening-hub'
    config_dir.mkdir(mode=0o700, parents=True)
    (config_dir / 'connections.json').write_text(json.dumps({
        'jellyfin': {'endpoint': 'https://jellyfin.example.org', 'user_id': 'fixture-user'},
        'audiomuse': {'endpoint': 'https://audiomuse.example.org'},
        'music_assistant': {'endpoint': 'https://music.example.org'}}))
    state_dir = Path(os.environ['XDG_STATE_HOME']) / 'listening-hub'
    state_dir.mkdir(mode=0o700, parents=True)
    (state_dir / 'connection-checks.json').write_text(json.dumps({
        'jellyfin': {'id': 'jellyfin', 'state': 'working', 'detail': 'Library access verified.',
                     'capabilities': {'library': 'working'}, 'checked_at': 1},
        'audiomuse': {'id': 'audiomuse', 'state': 'working', 'detail': 'AudioMuse web service responded.',
                      'capabilities': {'server_health': 'working'}, 'checked_at': 1}}))
    (Path(os.environ['XDG_CONFIG_HOME']) / 'waybar').mkdir(mode=0o700)
    (Path(os.environ['XDG_CONFIG_HOME']) / 'waybar' / 'config.jsonc').write_text(
        '{"custom/listening": {"exec": "listening-roomctl status"}}')
    (Path(os.environ['XDG_DATA_HOME']) / 'applications').mkdir(parents=True)
    (Path(os.environ['XDG_DATA_HOME']) / 'applications' / 'listening-room.desktop').write_text(
        '[Desktop Entry]\nName=Listening room\nActions=Return;Quiet;\n')

    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, GLib, Gtk

    # Capture accessible names at the GTK boundary, like a screen reader would.
    _update_property = Gtk.Accessible.update_property

    def _record_property(self, properties, values):
        for prop, value in zip(properties, values):
            if prop == Gtk.AccessibleProperty.LABEL:
                self._probe_label = value
        return _update_property(self, properties, values)

    Gtk.Accessible.update_property = _record_property

    import audio_devices
    import listening_hub as hub
    import metadata_pipeline
    import next_window
    import online_services as online
    import provider_auth
    import room_session
    import session_client
    import session_service
    from test_listening_session import FakeBackend
    from unittest.mock import patch

    FAILURES = []

    def check(label, ok, detail=''):
        print(('PASS ' if ok else 'FAIL ') + label + ((': ' + str(detail)) if detail and not ok else ''), flush=True)
        if not ok:
            FAILURES.append(label)

    backend = FakeBackend()
    controller = room_session.Controller(backend, Path(folder) / 'controller')
    controller._home_action = lambda *_: None
    controller._route_preferred = lambda: None
    service = session_service.Service(controller)
    LOG = []
    OUTPUTS = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True},
               {'id': 'buds', 'name': 'buds', 'description': 'Headphones', 'default': False, 'available': True}]
    SIDE = []
    ANSWERED = {}

    def request(action, arguments=None, *_args, **_kw):
        arguments = arguments or {}
        LOG.append((action, copy.deepcopy(arguments)))
        if action in ANSWERED:
            answer = ANSWERED[action]
            return answer(arguments) if callable(answer) else answer
        return service.execute(action, arguments)

    class FakeTwitchLogin:
        """The device-code flow, without contacting Twitch."""
        def __init__(self, client_id):
            if not isinstance(client_id, str) or not client_id:
                raise ValueError('Enter a Twitch application client ID')
            self.client_id = client_id
            self.cancelled = False

        def begin(self):
            return {'url': 'https://www.twitch.tv/activate', 'code': 'FIXTURE1'}

        def finish(self):
            return {'connected': True}

        def cancel(self):
            self.cancelled = True

    patches = [patch.object(session_client, 'request', side_effect=request),
               patch.object(provider_auth, 'TwitchDeviceLogin', FakeTwitchLogin),
               patch.object(audio_devices, 'outputs', return_value=OUTPUTS),
               patch.object(audio_devices, 'test_sound', side_effect=lambda name=None: SIDE.append(('test_sound', name))),
               patch.object(hub, 'system_volume', side_effect=lambda: {'percent': 40}),
               patch.object(hub, 'set_volume', side_effect=lambda value, **kw: SIDE.append(('volume', value))),
               patch.object(online, 'jellyfin_login',
                            side_effect=lambda user, secret, cancel=None: ANSWERED.update(
                                login=(user, bool(secret))) or {'message': 'Signed in'}),
               patch.object(online, 'save_token',
                            side_effect=lambda service_name, secret: ANSWERED.update(
                                token=(service_name, bool(secret))) or {'saved': True}),
               patch.object(online, 'test_connection',
                            side_effect=lambda service_name, cancel=None: {
                                'id': service_name, 'state': 'working', 'detail': 'Fixture check answered.',
                                'capabilities': {'library': 'working', 'playback': 'not tested'}, 'checked_at': 1}),
               patch.object(online, 'install_remote_commands',
                            side_effect=lambda device_id: {'installed': 7, 'notified': True}),
               patch.object(online, 'ha_scenes',
                            side_effect=lambda cancel=None: [
                                {'id': 'scene.late', 'name': 'Late evening', 'entities': ['light.desk']},
                                {'id': 'scene.read', 'name': 'Reading', 'entities': []}]),
               patch.object(online, 'set_room_scene',
                            side_effect=lambda room_id, scene_id=None, entities=None: SIDE.append(
                                ('lighting', room_id, scene_id, entities)))]
    for item in patches:
        item.start()
    ACOUSTID = {'ready': False}
    ANSWERED.update({
        'metadata.status': lambda _args: {'acoustid_ready': ACOUSTID['ready']},
        'metadata.enrich': {'accepted': True, 'job_id': 'job-enrich'},
        'metadata.match': {'accepted': True, 'job_id': 'job-match'},
        'metadata.analyze': {'accepted': True, 'job_id': 'job-analyze'},
        'metadata.confirm_identity': {'message': 'Recording identity confirmed'},
        'metadata.review': {'accepted': True, 'job_id': 'job-review'},
        'metadata.revoke': {'message': 'Review revoked'},
        'metadata.cancel': {'message': 'Cancellation requested'},
        'metadata.configure_acoustid': lambda args: ACOUSTID.update(ready=bool(args.get('client_key'))) or {
            'message': 'AcoustID application key saved', 'acoustid_ready': ACOUSTID['ready']},
    })

    TRACK = {'provider': 'jellyfin', 'id': 'track1', 'type': 'Audio', 'name': 'Nocturne in E flat',
             'artist': 'Fixture Quartet', 'album': 'Night studies'}
    ORIGIN = metadata_pipeline.library_origin()
    REVISION = {'id': 'source1', 'size': 12345, 'etag': 'etag', 'modified': '2026-11-01', 'duration': 1800}
    import threading
    controller.metadata_store.observe(
        ORIGIN, 'track1', None,
        identities=[{'provider': 'Jellyfin', 'provider_id': '6f1f0a4c-0000-0000-0000-000000000001',
                     'name': 'MusicBrainzTrack', 'method': 'reported ProviderIds',
                     'date': '2026-11-17T10:00:00+00:00'}],
        features=[{'provider': 'AudioMuse', 'name': 'tempo', 'value': 98.5, 'method': 'librosa beat track BPM',
                   'version': 'analysis version unknown', 'date': '2026-11-17T10:00:00+00:00'},
                  {'provider': 'Essentia MTG', 'name': 'instrumental score', 'value': .87,
                   'method': 'whole audio 16k logmel padded mean patch v1',
                   'model': 'voice instrumental msd musicnn 1', 'version': '2',
                   'date': '2026-11-17T10:00:00+00:00'}],
        revision=REVISION)
    controller.metadata_jobs['job-match'] = {
        'job_id': 'job-match', 'kind': 'match', 'status': 'completed',
        'result': {'candidates': [
            {'provider': 'MusicBrainz', 'provider_id': 'c1', 'title': 'Nocturne in E flat, op. 9 no. 2',
             'status': 'candidate'},
            {'provider': 'AcoustID', 'provider_id': 'c2', 'title': 'Nocturne no. 2', 'status': 'candidate'}]},
        '_record': (ORIGIN, 'track1'), '_revision': REVISION, '_cancel': threading.Event()}
    controller.metadata_jobs['job-run'] = {'job_id': 'job-run', 'kind': 'enrich', 'status': 'running',
                                           'message': 'Collecting reported IDs', '_item_id': 'track2',
                                           '_cancel': threading.Event()}
    controller.enter({'music': {'reference': TRACK, 'source_id': 'jellyfin-track1', 'name': TRACK['name'],
                                'queue': [TRACK], 'queue_index': 0},
                      'recipe': controller.last_session})

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

    def settle(seconds=.6):
        try:
            controller.worker.submit(lambda: None).result(timeout=3)
        except Exception:
            pass
        pump(seconds)

    def widgets(root):
        yield root
        child = root.get_first_child()
        while child:
            yield from widgets(child)
            child = child.get_next_sibling()

    def parents(widget):
        parent = widget.get_parent()
        while parent:
            yield parent
            parent = parent.get_parent()

    def texts(root):
        values = []
        for widget in widgets(root):
            for getter in ('get_label', 'get_text', 'get_title', 'get_subtitle', 'get_tooltip_text'):
                value = getattr(widget, getter, lambda: None)()
                if isinstance(value, str) and value:
                    values.append(value)
        return values

    def shown(widget):
        return (widget.get_visible() and widget.get_mapped() and widget.get_child_visible()
                and widget.get_opacity() > .05)

    def clear_toasts():
        """Evidence shots show the page, not confirmations queued a moment ago."""
        for overlay in [widget for widget in widgets(window) if isinstance(widget, Adw.ToastOverlay)]:
            overlay.dismiss_all()
        pump(.3)

    def shot(name, width=None, height=None):
        from PIL import ImageGrab
        width = width or window.get_width()
        height = height or window.get_height()
        settle(.4)
        clear_toasts()
        pump(.4)
        path = output / (name + '.png')
        ImageGrab.grab(bbox=(0, 0, width, height), xdisplay=os.environ['DISPLAY']).save(path)
        return path

    FORBIDDEN = ('Main audio', 'Background audio', 'Picture', 'Retry picture', 'Refresh')
    PLAYBACK = {'enter', 'return', 'music', 'online', 'station.start', 'station.replay', 'link', 'resume', 'quiet',
                'stop', 'leave', 'wind_down', 'layer', 'picture', 'atmosphere', 'room_volume', 'save'}
    READ_ONLY = {'inspect', 'get', 'variants', 'station.list', 'station.recent', 'metadata.status', 'metadata.list',
                 'metadata.jobs', 'metadata.inspect', 'metadata.lookup_reference', 'metadata.export'}

    def commands(since=0):
        return [entry for entry in LOG[since:] if entry[0] not in READ_ONLY]

    def assert_vocabulary(root, where):
        for value in texts(root):
            for word in FORBIDDEN:
                assert word not in value, '%s shows %r in %r' % (where, word, value)
            if 'Workspace 9' in value:
                assert value == 'Workspace 9', 'Workspace 9 outside a placement value: %r' % value

    def assert_layout(root, where, width, height):
        for widget in widgets(root):
            if isinstance(widget, Gtk.ScrolledWindow) and shown(widget):
                adjustment = widget.get_hadjustment()
                assert adjustment.get_upper() <= adjustment.get_page_size() + 1, '%s scrolls horizontally' % where
            if isinstance(widget, (Gtk.Button, Gtk.Scale, Gtk.MenuButton, Gtk.Switch, Gtk.DropDown)) \
                    and shown(widget) and not any(isinstance(p, Gtk.Popover) for p in parents(widget)):
                ok, bounds = widget.compute_bounds(window)
                if not ok:
                    continue
                assert bounds.get_x() >= -1 and bounds.get_x() + bounds.get_width() <= width + 1, \
                    '%s: %r is clipped horizontally' % (where, widget.get_tooltip_text())
                scrolled = any(isinstance(p, Gtk.ScrolledWindow) for p in parents(widget))
                if not scrolled:
                    assert bounds.get_y() >= -1 and bounds.get_y() + bounds.get_height() <= height + 1, \
                        '%s: %r is clipped vertically' % (where, widget.get_tooltip_text())

    def unnamed_icon_controls(root):
        """Our own controls must be named; the dialog's sheet handle belongs to libadwaita."""
        missing = []
        for widget in widgets(root):
            if not isinstance(widget, Gtk.Button) or not shown(widget) or texts(widget):
                continue
            if any(isinstance(parent, (Gtk.HeaderBar, Gtk.WindowHandle)) for parent in parents(widget)):
                continue
            if not (widget.get_tooltip_text() or getattr(widget, '_probe_label', None)):
                missing.append(type(widget).__name__)
        return missing

    def button(root, label):
        for widget in widgets(root):
            if isinstance(widget, Gtk.Button) and shown(widget):
                if widget.get_label() == label:
                    return widget
        return None

    def scroll_to_bottom(root):
        for widget in widgets(root):
            if isinstance(widget, Gtk.ScrolledWindow) and shown(widget):
                adjustment = widget.get_vadjustment()
                if adjustment.get_upper() > adjustment.get_page_size():
                    adjustment.set_value(adjustment.get_upper())
                    return True
        return False

    def scroll_into_view(root, target):
        """Bring a control into the middle of its page, so its screenshot shows it."""
        for widget in widgets(root):
            if not isinstance(widget, Gtk.ScrolledWindow) or not shown(widget):
                continue
            if not any(parent is widget for parent in parents(target)):
                continue
            adjustment = widget.get_vadjustment()
            ok, bounds = target.compute_bounds(widget)
            if not ok:
                return False
            adjustment.set_value(adjustment.get_value() + bounds.get_y() - adjustment.get_page_size() / 2)
            return True
        return False

    try:
        # ------------------------------------------------------------ launch and the hook
        settle(.6)
        LOG.clear()
        backend.calls.clear()
        window = next_window.NextWindow(app)
        window.set_default_size(1200, 800)
        window.present()
        pump(1.0)
        window.show_rooms()
        pump(.4)
        check('Ctrl+, is a registered window action', 'win.preferences' in app.get_actions_for_accel('<Control>comma'),
              app.get_actions_for_accel('<Control>comma'))
        menu_item = next((widget for widget in widgets(window.menu_popover)
                          if isinstance(widget, Gtk.Button) and widget.get_label() == 'Preferences…'), None)
        check('the main menu offers Preferences…', menu_item is not None,
              [text for text in texts(window.menu_popover) if 'Preferences' in text])
        mark, calls = len(LOG), len(backend.calls)
        if menu_item is not None:
            menu_item.emit('clicked')
        pump(1.2)
        dialog = getattr(window, 'preferences', None)
        check('the menu entry opens the Preferences dialog',
              isinstance(dialog, Adw.PreferencesDialog) and dialog.get_visible())
        if dialog is not None:
            Adw.Dialog.close(dialog)
        pump(.6)
        window.activate_action('win.preferences', None)
        pump(1.2)
        dialog = getattr(window, 'preferences', None)
        check('Ctrl+, opens the Preferences dialog again', isinstance(dialog, Adw.PreferencesDialog) and dialog.get_visible())
        check('opening Preferences sends no controller command and causes no playback effect',
              not commands(mark) and len(backend.calls) == calls, commands(mark))

        page_names = [page.get_title() for page in dialog.pages.values()]
        check('Preferences has the six agreed sections',
              page_names == ['Connections', 'Sound and output', 'Scene and motion', 'Phone and desktop',
                             'Recordings', 'About'], page_names)
        check('every icon-only control in Preferences carries an accessible name',
              not unnamed_icon_controls(dialog), unnamed_icon_controls(dialog))

        mark = len(LOG)
        for name, page in dialog.pages.items():
            dialog.set_visible_page(page)
            pump(.4)
            assert_vocabulary(dialog, 'Preferences ' + name)
            assert_layout(dialog, 'Preferences %s 1200x800' % name, 1200, 800)
        check('walking every Preferences page is inert', not commands(mark), commands(mark))

        # ------------------------------------------------------------ Connections, mixed states
        dialog.set_visible_page(dialog.pages['connections'])
        pump(.6)
        visible = ' '.join(texts(dialog))
        for expected in ('Signed in · library ready', 'Not set up', 'Added · not checked yet',
                         'Web service answers · analysis not confirmed'):
            check('Connections shows the mixed state %r' % expected, expected in visible, visible[:300])
        for chip in ('Browse', 'Plays here', 'Open elsewhere', 'Plays on speakers', 'Station mixes', 'History',
                     'Room lighting'):
            check('Connections shows the capability chip %r' % chip, chip in visible, visible[:300])
        check('a connected service offers Manage and an unconfigured one offers Set up',
              visible.count('Manage') >= 3 and visible.count('Set up') >= 2, visible[:300])
        shot('01-connections-mixed-states')

        # Jellyfin sign-in reuses the native flow; secrets stay out of every label.
        mark = len(LOG)
        dialog.open_connection('jellyfin')
        pump(1.0)
        fields = dialog.connection_fields
        check('the Jellyfin page offers the native username and password sign-in',
              'Sign in to Jellyfin' in ' '.join(texts(dialog)) and 'Jellyfin password' in ' '.join(texts(dialog)))
        fields['username'].set_text('fixture-user')
        fields['password'].set_text(PASSWORD)
        fields['token'].set_text(TOKEN)
        pump(.3)
        check('typing credentials into the form sends no command',
              not commands(mark), commands(mark))
        shot('02-jellyfin-sign-in')
        button(dialog, 'Sign in').emit('clicked')
        settle(1.0)
        sent = [action for action, _payload in LOG[mark:]]
        check('sign-in uses the existing Jellyfin login and never a playback command',
              not [name for name in sent if name in PLAYBACK], sent)
        check('the saved token is handed to private storage, not kept or shown',
              ANSWERED.get('token') == ('jellyfin', True) and fields['token'].get_text() == ''
              and TOKEN not in ' '.join(texts(dialog)), ANSWERED.get('token'))
        check('the password entry is cleared after sign-in and never shown',
              fields['password'].get_text() == '' and PASSWORD not in ' '.join(texts(dialog)))
        button(dialog, 'Test').emit('clicked')
        settle(1.0)
        check('testing the connection reports its result without playing anything',
              'Fixture check answered.' in ' '.join(texts(dialog)))
        check('nothing in the sign-in flow sent a playback command',
              not [entry for entry in commands(mark) if entry[0] in PLAYBACK], commands(mark))
        dialog.pop_subpage()
        pump(.6)

        # Twitch device sign-in, still without any playback and without a bogus Save.
        dialog.open_connection('twitch')
        pump(.8)
        twitch_page = ' '.join(texts(dialog))
        check('the Twitch page offers its device sign-in and no access-token form',
              'Begin sign-in' in twitch_page and 'Access token' not in twitch_page, twitch_page[:300])
        # Channels you keep (added in O5 for parity with the classic Twitch panel): saved on apply, never plays.
        import twitch_channels
        mark = len(LOG)
        dialog.channel_rows['music'].set_text('LofiGirl https://www.twitch.tv/other_name/')
        dialog.channel_rows['music'].emit('apply')
        pump(.4)
        check('Twitch channels you keep: names and links are saved for the music style without playing anything',
              twitch_channels.curated_channels('music') == ['lofigirl', 'other_name']
              and not [entry for entry in commands(mark) if entry[0] in PLAYBACK], twitch_channels.curated_channels('music'))
        dialog.channel_rows['excluded'].set_text('https://other.org/name')
        dialog.channel_rows['excluded'].emit('apply')
        pump(.3)
        check('Twitch channels you keep: a foreign link is refused and nothing is saved',
              twitch_channels.excluded_channels() == [] and 'twitch' in dialog.connection_notice.get_subtitle().lower(),
              dialog.connection_notice.get_subtitle())
        dialog.connection_fields['client_id'].set_text('fixture-client-id')
        mark = len(LOG)
        button(dialog, 'Begin sign-in').emit('clicked')
        settle(.8)
        check('beginning the Twitch sign-in shows a code without contacting an account',
              'FIXTURE1' in ' '.join(texts(dialog)), '')
        button(dialog, 'Finish').emit('clicked')
        settle(.8)
        check('finishing the Twitch sign-in reports connected and refreshes the row',
              'Signed in · renews on its own' in ' '.join(texts(dialog))
              or 'Signed in · renews on its own' in ' '.join(texts(window.preferences)),
              '')
        check('the Twitch flow sent no playback command and no token save',
              not [entry for entry in commands(mark) if entry[0] in PLAYBACK]
              and ANSWERED.get('token') != ('twitch', True), LOG[mark:])
        dialog.pop_subpage()
        pump(.6)

        # Home Assistant keeps the classic explicit scene-link flow, and links nothing on its own.
        dialog.open_connection('home_assistant')
        pump(.8)
        ha_page = ' '.join(texts(dialog))
        check('the Home Assistant page offers room lighting without changing a light',
              'Room lighting' in ha_page and 'Link this scene to the room' in ha_page
              and not [side for side in SIDE if side[0] == 'lighting'], ha_page[:300])
        button(dialog, 'Load scenes').emit('clicked')
        settle(.8)
        dialog.lighting_scene.set_selected(0)
        button(dialog, 'Link').emit('clicked')
        settle(.4)
        check('linking uses the existing scene link and never activates it',
              any(side[0] == 'lighting' and side[2] == 'scene.late' and side[3] == ['light.desk'] for side in SIDE),
              SIDE)
        dialog.pop_subpage()
        pump(.6)

        # ------------------------------------------------------------ Sound and output
        dialog.set_visible_page(dialog.pages['sound'])
        pump(.8)
        sound = ' '.join(texts(dialog))
        for expected in ('Output', 'Test sound', 'Pause if it disconnects', 'Lower room audio during calls',
                         'Pause other players when a room starts'):
            check('Sound shows %r' % expected, expected in sound, sound[:240])
        mark = len(LOG)
        dialog.pause_switch.set_active(False)
        settle(.8)
        check('a sound toggle applies at once through the settings command',
              any(action == 'settings' and payload.get('values', {}).get('pause_on_disconnect') is False
                  for action, payload in LOG[mark:]), LOG[mark:])
        mark = len(LOG)
        dialog.others_switch.set_active(True)
        settle(.8)
        check('pause other players is the existing local preference, not a controller command',
              bool(hub.state().get('pause_others_on_radio_start')) and not commands(mark), LOG[mark:])
        dialog.pause_switch.set_active(True)
        settle(.8)
        shot('03-sound')

        # ------------------------------------------------------------ Scene and motion
        dialog.set_visible_page(dialog.pages['scene'])
        pump(.8)
        scene = ' '.join(texts(dialog))
        for expected in ('Default placement', 'Preferred display', 'Moving picture', 'Reduced motion',
                         'Keep controls visible'):
            check('Scene and motion shows %r' % expected, expected in scene, scene[:240])
        window.show_now()
        pump(.8)
        window.kick_idle()
        before = window.idle_timer is not None
        mark = len(LOG)
        dialog.visible_switch.set_active(True)
        settle(.8)
        window.kick_idle()
        check('Keep controls visible applies through the settings command',
              any(action == 'settings' and payload.get('values', {}).get('keep_controls') is True
                  for action, payload in LOG[mark:]), LOG[mark:])
        check('Keep controls visible turns Now’s idle fade off',
              before and controller.settings.get('keep_controls') and window.idle_timer is None,
              (before, controller.settings.get('keep_controls'), window.idle_timer))
        dialog.set_visible_page(dialog.pages['scene'])
        pump(.6)
        dialog.reduced_switch.set_active(True)
        settle(.8)
        check('Reduced motion disables animations, so no dialog transition animates',
              not Gtk.Settings.get_default().get_property('gtk-enable-animations'))
        scroll_into_view(dialog, dialog.visible_switch)
        pump(.6)
        shot('04-scene-and-motion')
        dialog.reduced_switch.set_active(False)
        dialog.visible_switch.set_active(False)
        settle(.8)

        # ------------------------------------------------------------ Phone and desktop
        dialog.set_visible_page(dialog.pages['phone'])
        pump(.8)
        phone = ' '.join(texts(dialog))
        for expected in ('KDE Connect', 'Find paired phones', 'Install room commands', 'Waybar', 'Launcher'):
            check('Phone and desktop shows %r' % expected, expected in phone, phone[:240])
        check('the Waybar and launcher status lines read this user’s fixtures',
              'Shows the room and its layers in Waybar' in phone
              and 'Launcher entry installed · with room actions' in phone)
        scroll_into_view(dialog, dialog.launcher_row)
        pump(.6)
        shot('05-phone-and-desktop')

        # ------------------------------------------------------------ Recordings
        dialog.set_visible_page(dialog.pages['recordings'])
        pump(1.2)
        recordings = ' '.join(texts(dialog))
        for expected in ('AcoustID application key', 'Enrich in bulk', 'Upcoming tracks',
                         'Prepared library queue', 'Export recording notes', 'Import recording notes'):
            check('Recordings shows %r' % expected, expected in recordings, recordings[:300])
        check('a running metadata job is listed with Cancel',
              '1 metadata job in progress' in recordings and 'Running · Collecting reported IDs' in recordings
              and button(dialog, 'Cancel') is not None, recordings[-300:])
        mark = len(LOG)
        dialog.acoustid_key.set_text('fixture-acoustid-key')
        button(dialog, 'Save').emit('clicked')
        settle(.8)
        check('saving the AcoustID key uses the existing private-key command',
              any(action == 'metadata.configure_acoustid' for action, _payload in LOG[mark:]), LOG[mark:])
        check('the key entry is cleared after saving', dialog.acoustid_key.get_text() == '')
        shot('06-recordings')
        scroll_into_view(dialog, button(dialog, 'Cancel') or dialog.pages['recordings'])
        pump(.6)
        shot('06b-recordings-jobs-and-files')
        mark = len(LOG)
        cancel = button(dialog, 'Cancel')
        check('the running job offers Cancel', cancel is not None)
        if cancel is not None:
            cancel.emit('clicked')
            settle(.8)
            check('Cancel uses the existing metadata.cancel command',
                  any(action == 'metadata.cancel' and payload.get('job_id') == 'job-run'
                      for action, payload in LOG[mark:]), LOG[mark:])

        # ------------------------------------------------------------ Track details
        dialog.close()
        pump(.6)
        LOG.clear()
        backend.calls.clear()
        mark = len(LOG)
        window.open_track_details(dict(TRACK))
        pump(1.2)
        track = window.track_details[-1]
        check('Track details opens for a library reference and reads only metadata',
              isinstance(track, Adw.Dialog)
              and not [entry for entry in commands(mark) if not entry[0].startswith('metadata.')]
              and len(backend.calls) == 0, LOG[mark:])
        overview = ' '.join(texts(track))
        check('the overview keeps the classic scope statements',
              'Audio version has not been fingerprinted yet.' in overview
              and 'not a probability or human review' in overview
              and 'No human review for this audio version.' in overview
              and 'Review validity is checked again before use.' in overview, overview[:400])
        for expected in ('Enrich', 'Match', 'Analyze', 'Confirm identity'):
            check('Track details offers %r' % expected, expected in overview)
        assert_vocabulary(track, 'Track details')
        assert_layout(track, 'Track details 1200x800', 1200, 800)
        check('every icon-only control in Track details has an accessible name',
              not unnamed_icon_controls(track), unnamed_icon_controls(track))
        shot('07-track-details-overview')

        mark = len(LOG)
        button(track, 'Match').emit('clicked')
        settle(1.0)
        check('Match uses the existing metadata.match command with a chosen method',
              any(action == 'metadata.match' and payload.get('method') in ('musicbrainz', 'acoustid')
                  for action, payload in LOG[mark:]), LOG[mark:])
        candidates = ' '.join(texts(track))
        check('match candidates are listed with their provider',
              'Nocturne in E flat, op. 9 no. 2 (MusicBrainz)' in candidates, candidates[:300])
        track.candidate_buttons[1].set_active(True)
        mark = len(LOG)
        button(track, 'Confirm').emit('clicked')
        settle(.8)
        check('Confirm identity sends only metadata.confirm_identity',
              [entry[0] for entry in commands(mark)] == ['metadata.confirm_identity'], LOG[mark:])
        scroll_into_view(track, track.candidate_box)
        pump(.6)
        shot('08-track-details-match-candidates')

        scroll_into_view(track, track.reviewer)
        pump(.6)
        form = ' '.join(texts(track))
        for expected in ('Listen to the whole exact recording yourself before reviewing.',
                         'Reviewing does not start playback.', 'Instrumental (no vocals or speech)',
                         'I listened to the entire exact recording'):
            check('the review form states %r' % expected[:48], expected in form, form[:400])
        mark = len(LOG)
        button(track, 'Save review').emit('clicked')
        settle(.6)
        check('a review without scope, acknowledgement and name is refused locally',
              not [entry for entry in LOG[mark:] if entry[0] == 'metadata.review'], LOG[mark:])
        track.class_buttons[0].set_active(True)
        track.ack.set_active(True)
        track.reviewer.set_text('Fixture reviewer')
        pump(.4)
        scroll_into_view(track, track.reviewer)
        pump(.6)
        shown_form = ' '.join(texts(track))
        check('the filled review form shows the classification, reviewer and acknowledgement',
              'Fixture reviewer' in shown_form and track.class_buttons[0].get_active()
              and track.ack.get_active())
        shot('09-track-details-review-form')
        mark = len(LOG)
        button(track, 'Save review').emit('clicked')
        settle(.8)
        reviews = [payload for action, payload in LOG[mark:] if action == 'metadata.review']
        check('a complete review sends only metadata.review with its scope acknowledged',
              len(reviews) == 1 and reviews[0].get('acknowledged') is True
              and reviews[0].get('classification') == 'instrumental', reviews)
        check('no track-details action ever starts music',
              not [entry for entry in commands(mark) if entry[0] in PLAYBACK], LOG[mark:])
        track.cleanup()
        Adw.Dialog.close(track)
        pump(.5)

        # ------------------------------------------------------------ 800 px
        window.set_default_size(800, 700)
        pump(1.0)
        window.activate_action('win.preferences', None)
        pump(1.2)
        dialog = window.preferences
        dialog.set_visible_page(dialog.pages['connections'])
        pump(.8)
        assert_layout(dialog, 'Preferences 800x700', 800, 700)
        assert_vocabulary(dialog, 'Preferences 800')
        narrow = ' '.join(texts(dialog))
        expected_statuses = ('Signed in · library ready', 'Added · not checked yet',
                             'Web service answers · analysis not confirmed', 'Not set up')
        check('at 800 px every connection status is still shown in full',
              all(status in narrow for status in expected_statuses),
              [status for status in expected_statuses if status not in narrow])
        shot('10-preferences-800', 800, 700)
        dialog.close()
        pump(.6)

        print('screenshots in', output)
        if FAILURES:
            print('FAIL: ' + ', '.join(FAILURES))
            raise SystemExit(1)
        print('PASS: Preferences (connections, sound, scene, phone, recordings) and Track details are inert, '
              'keyboard-reachable, truthful and unclipped at 1200 and 800 px')
    finally:
        for item in reversed(patches):
            item.stop()
        controller.close()
