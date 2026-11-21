"""Listen, sources: choosing an ambience for the Mix, Twitch availability and YouTube search.

* Ambience mode: Listen with every source's primary action "Play as ambience". Browsing is
  inert; only that action starts sound, replaces the room's ambience layer and returns to
  where the listener came from.
* Twitch: what is reachable with and without an account, said truthfully, with a way to set it up.
* YouTube: search through yt-dlp, off the GTK thread and cancellable.

Mixed into ListenPage; it uses the page's rows, sections, window and executor.
"""
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk

import channel_browse
import next_station
from next_async import run_async
import youtube_search
from next_widgets import label

TWITCH_REFRESH_S = 45.


class SourcesMixin:
    # ---- ambience mode -------------------------------------------------------------

    def build_ambience_banner(self):
        self.ambience_banner = Gtk.Box(spacing=12)
        self.ambience_banner.add_css_class('ambience-banner')
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.ambience_title = label('Choose an ambience', ['listen-title'], ellipsize=True)
        text.append(self.ambience_title)
        text.append(label('Browsing plays nothing. Press Play as ambience on the one you want.',
                          ['section-note'], wrap=True))
        self.ambience_banner.append(text)
        cancel = Gtk.Button(label='Cancel', valign=Gtk.Align.CENTER)
        cancel.connect('clicked', lambda *_: self.end_ambience(returned=True))
        self.ambience_banner.append(cancel)
        self.ambience_banner.set_visible(False)
        return self.ambience_banner

    def fill_scopes(self):
        """The scope chips for the current mode; the active one follows self.scope."""
        self.scope_syncing = True
        try:
            self.scopes.remove_all()
            for key, title in self.scope_list:
                self.scopes.add(Adw.Toggle(label=title, name=key))
            keys = [key for key, _title in self.scope_list]
            self.scopes.set_active(keys.index(self.scope) if self.scope in keys else Gtk.INVALID_LIST_POSITION)
            if getattr(self, 'more_scopes', None) is not None:
                self.more_scopes.set_active(Gtk.INVALID_LIST_POSITION)
        finally:
            self.scope_syncing = False

    def reset_view(self):
        self.cancel_debounce()
        self.stop_search()
        self.remote = {}
        self.opened = None
        self.query = ''
        self.search.set_text('')
        self.fill_scopes()
        self.show_scope_controls()
        self.index_sig = None
        self.refresh()

    def begin_ambience(self, origin='now'):
        """Open Listen to choose the room's ambience; ``origin`` is where Cancel and success return to."""
        self.mode, self.mode_origin = 'ambience', origin
        self.scope_list, self.scope = self.ambience_scopes(), 'sounds'
        room = (self.snapshot.get('active') or {}).get('name')
        self.ambience_title.set_text('Choose an ambience' + (' for ' + room if room else ''))
        self.ambience_banner.set_visible(True)
        self.set_page('index')
        self.reset_view()
        self.load_sounds()
        self.refresh_twitch()
        self.window.show_listen()

    @staticmethod
    def ambience_scopes():
        from next_listen import AMBIENCE_SCOPES
        return AMBIENCE_SCOPES

    def end_ambience(self, returned=False):
        if self.mode != 'ambience':
            return
        from next_listen import SCOPES
        self.mode = 'music'
        self.scope_list, self.scope = SCOPES, 'all'
        self.ambience_banner.set_visible(False)
        self.reset_view()
        if returned:
            self.window.return_from_listen(self.mode_origin)

    def previous_ambience(self):
        """What would put the current ambience back (or stop it), for Undo."""
        before = dict((self.snapshot.get('active') or {}).get('atmosphere') or {})
        if before.get('kind') in (None, 'none'):
            return {'layer': 'atmosphere', 'action': 'stop'}
        keep = {key: before[key] for key in ('kind', 'source_id', 'url', 'name', 'reference', 'resolver') if key in before}
        return dict(keep, layer='atmosphere', action='select')

    def send_ambience(self, args, name):
        """Replace the room's ambience layer through the existing command."""
        undo = self.previous_ambience()
        return self.window.command('atmosphere', args, done=lambda _result: self.ambience_started(name, undo))

    def ambience_started(self, name, undo=None):
        undo = undo or self.previous_ambience()
        self.window.toast('Ambience: ' + name, 'Undo', lambda: self.window.command('layer', undo))
        if self.mode == 'ambience':
            self.end_ambience(returned=True)

    def load_sounds(self):
        if self.sounds is not None:
            return
        self.sounds, self.sounds_state = [], 'loading'

        def read():
            import atmosphere_catalogue
            return [sound for sound in atmosphere_catalogue.presets() if sound.get('available')]

        run_async(read, self.sounds_loaded, lambda _error: self.sounds_loaded([]), self.executor)

    def sounds_loaded(self, found):
        if not self.closed:
            self.sounds, self.sounds_state = list(found), 'ready'
            self.refresh()
        return False

    def sound_entries(self):
        entries = []
        for sound in self.sounds or []:
            args = {'layer': 'atmosphere', 'action': 'select', 'kind': 'local', 'source_id': sound['id'],
                    'name': sound['name']}
            entries.append({'kind': 'sound', 'name': sound['name'], 'detail': 'Offline sound · loops · ' + (
                'outdoor, with a window' if sound.get('outdoor') else 'indoor'),
                'keywords': sound.get('description') or '',
                'ambience': lambda args=args, name=sound['name']: self.choose_sound(args, name)})
        return entries

    def choose_sound(self, args, name):
        undo = self.previous_ambience()
        return self.window.command('layer', args, done=lambda _result: self.ambience_started(name, undo))

    # ---- Twitch ----------------------------------------------------------------------

    def refresh_twitch(self, force=False):
        """Read what Twitch can show, off the GTK thread: curated channels, then live discovery if signed in."""
        if self.twitch_loading:
            return
        if not force and self.twitch['state'] != 'unknown' and time.monotonic() - self.twitch['at'] < TWITCH_REFRESH_S:
            return
        self.twitch_loading = True

        run_async(self.read_twitch, self.twitch_read,
                  lambda problem: self.twitch_read({'state': 'attention', 'channels': [],
                                                    'message': str(problem) or 'Twitch could not be read.'}),
                  self.executor)

    @staticmethod
    def read_twitch():
        import twitch_channels
        import twitch_credentials
        try:
            excluded = {str(name).lower() for name in twitch_channels.excluded_channels()}
            curated = []
            for style in ('music', 'asmr'):
                for login in twitch_channels.curated_channels(style):
                    if login.lower() not in excluded and not any(item['login'] == login for item in curated):
                        curated.append({'login': login, 'name': login, 'url': 'https://www.twitch.tv/' + login,
                                        'live': None})
        except (OSError, ValueError) as problem:
            return {'state': 'attention', 'message': 'The saved Twitch channels could not be read: ' + str(problem),
                    'channels': []}
        try:
            saved = twitch_credentials.load()
            connected = bool(saved.client_id and saved.access_token)
        except twitch_credentials.CredentialError as problem:
            return {'state': 'attention', 'message': str(problem), 'channels': curated}
        found, problem = {}, ''
        if connected:
            for style in ('music', 'asmr'):
                try:
                    result = twitch_channels.discover(style)
                except (OSError, ValueError) as error:
                    problem = str(error) or 'Twitch could not be reached.'
                    continue
                if result.get('status') == 'unknown':
                    problem = result.get('message') or problem
                for channel in result.get('channels') or []:
                    if channel.get('live') and channel.get('login', '').lower() not in excluded:
                        found.setdefault(channel['login'], channel)
        channels = list(found.values())
        for item in curated:
            if item['login'] not in found:
                channels.append(item)
        state = channel_browse.availability(connected, len(curated), problem)
        return {'state': state, 'message': problem, 'channels': channels[:60]}

    def twitch_read(self, found):
        self.twitch_loading = False
        if self.closed:
            return False
        self.twitch = {'state': found['state'], 'message': found.get('message') or '', 'at': time.monotonic()}
        self.channels = found.get('channels') or []
        self.channels_loaded = True
        self.refresh()
        return False

    def twitch_setup_entry(self):
        attention = self.twitch['state'] == 'attention'
        return {'kind': 'setup', 'name': 'Check Twitch' if attention else 'Set up Twitch to see live channels',
                'detail': channel_browse.twitch_note(self.twitch['state'], self.twitch['message']),
                'action': ('Open Preferences', self.open_twitch_setup)}

    def open_twitch_setup(self):
        """Preferences › Connections › Twitch."""
        self.window.open_preferences('twitch')

    def channel_rows(self):
        rows = [self.channel_entry(item) for item in self.channels]
        if self.twitch['state'] in ('setup', 'attention'):
            rows.append(self.twitch_setup_entry())
        return rows

    def twitch_footnote(self):
        return channel_browse.footnote(self.twitch['state'], bool(self.channels))

    # ---- YouTube ---------------------------------------------------------------------

    def search_youtube(self, query, cancel):
        return youtube_search.search(query, cancel)

    @staticmethod
    def video_detail(item):
        return ' · '.join(part for part in (item.get('channel'), item.get('duration') or ('Live' if item.get('live') else ''),
                                            'YouTube') if part)

    def youtube_entry(self, item):
        url = item.get('url') or ''
        menu = [('Play as ambience', lambda item=item: self.play_ambience(item)),
                ('Save as station', lambda item=item: self.save_as_station(next_station.source_from_video(item))),
                ('Open elsewhere', lambda url=url: self.open_external(url))]
        return {'kind': 'youtube', 'name': item.get('name') or 'YouTube video', 'detail': self.video_detail(item),
                'keywords': item.get('channel') or '', 'item': item,
                'action': ('Tune in', lambda item=item: self.tune_youtube(item)), 'menu': menu,
                'ambience': lambda item=item: self.play_ambience(item)}

    def tune_youtube(self, item):
        return self.window.start('link', {'url': item.get('url'), 'destination': 'music'}, item.get('name') or 'the video')

    def youtube_note(self):
        """The truthful line for the YouTube scope when there is nothing to list."""
        status = youtube_search.status()
        if not status['available']:
            return status['text']
        return 'Search YouTube by title, artist or topic. Nothing plays until you choose a result.'

    def youtube_trailing(self):
        if youtube_search.available():
            return None
        return self.link_button('Open Preferences', self.window.open_preferences)
