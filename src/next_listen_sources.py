"""Listen, sources: choosing an ambience for the Mix, Twitch availability and YouTube search.

* Ambience mode: Listen with every source's primary action "Play as ambience". Browsing is
  inert; only that action starts sound, replaces the room's ambience layer and returns to
  where the listener came from.
* Twitch: what is reachable with and without an account, said truthfully, with a way to set it up.
* YouTube: search through yt-dlp, off the GTK thread and cancellable; suggestion shelves before
  you type (no account); every video row offers Play, Watch (with sound, its own window) and
  Show as scene (silent).

Mixed into ListenPage; it uses the page's rows, sections, window and executor.
"""
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

import channel_browse
import next_model as model
import next_station
from next_async import run_async
import youtube_search
import youtube_shelves
from next_widgets import label, name_it

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
        self.window.set_mode_title('Choose ambience' + (' · ' + room if room else ''))
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
        self.window.set_mode_title(None)
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
                                        'live': None, 'style': style, 'curated': True})
        except (OSError, ValueError) as problem:
            return {'state': 'attention', 'message': 'The saved Twitch channels could not be read: ' + str(problem),
                    'channels': [], 'pages': {}, 'yours': []}
        try:
            saved = twitch_credentials.load()
            connected = bool(saved.client_id and saved.access_token)
        except twitch_credentials.CredentialError as problem:
            return {'state': 'attention', 'message': str(problem), 'channels': curated, 'pages': {}, 'yours': curated}
        pages, problem = {}, ''

        def read_page(key, name, style, category=None, game_id=None):
            nonlocal problem
            try:
                if category:
                    result = twitch_channels.discover(style, curated=[], category=category, game_id=game_id)
                else:
                    result = twitch_channels.discover(style)
            except (OSError, ValueError) as error:
                problem = str(error) or 'Twitch could not be reached.'
                return
            if result.get('status') == 'unknown':
                problem = result.get('message') or problem
            live = [channel for channel in result.get('channels') or []
                    if channel.get('live') and channel.get('login', '').lower() not in excluded]
            pages[key] = {'name': name, 'style': style, 'category': category, 'channels': live,
                          'cursor': result.get('cursor'), 'game_id': result.get('game_id') or game_id}

        if connected:
            for style in ('music', 'asmr'):
                read_page(style, {'music': 'Music', 'asmr': 'ASMR'}[style], style)
            for row in twitch_channels.saved_categories():
                read_page('cat:' + row['id'], row['name'], 'music', row['name'], row['id'])
        found = {channel['login']: channel for page in pages.values() for channel in page['channels']}
        # Every category takes turns, so a short list (the shelf) shows each of them.
        channels = channel_browse.interleave([page['channels'] for page in pages.values()])
        for item in curated:
            if item['login'] not in found:
                channels.append(item)
        state = channel_browse.availability(connected, len(curated), problem)
        return {'state': state, 'message': problem, 'channels': channels, 'pages': pages, 'yours': curated}

    def twitch_read(self, found):
        self.twitch_loading = False
        if self.closed:
            return False
        self.twitch = {'state': found['state'], 'message': found.get('message') or '', 'at': time.monotonic()}
        self.channels = found.get('channels') or []
        self.channels_loaded = True
        self.twitch_applied(found)
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
        """A video row: Play (Tune in only for a live stream), a Watch button, and ⋯ with Show as scene first,
        because it never interrupts sound. Choosing an ambience, Watch and Play move into ⋯."""
        url = item.get('url') or ''
        play = ('Tune in' if item.get('live') else 'Play', lambda item=item: self.tune_youtube(item))
        menu = [('Show as scene', lambda item=item: self.scene_from_video(item)),
                ('Play as ambience', lambda item=item: self.play_ambience(item)),
                ('Save as station', lambda item=item: self.save_as_station(next_station.source_from_video(item))),
                ('Open elsewhere', lambda url=url: self.open_external(url))]
        entry = {'kind': 'youtube', 'name': item.get('name') or 'YouTube video', 'detail': self.video_detail(item),
                 'keywords': item.get('channel') or '', 'item': item, 'action': play, 'menu': menu,
                 'ambience': lambda item=item: self.play_ambience(item)}
        if self.mode == 'ambience':
            entry['menu'] = [('Play as music' if not item.get('live') else 'Tune in as music', play[1]),
                             ('Watch', lambda item=item: self.watch_youtube(item))] + menu
        else:
            entry['watch'] = lambda item=item: self.watch_youtube(item)
        return entry

    def watch_button(self, entry):
        """The row's Watch control: video with sound in its own window."""
        button = Gtk.Button(icon_name='video-x-generic-symbolic', valign=Gtk.Align.CENTER)
        button.add_css_class('flat')
        name_it(button, 'Watch with sound: ' + (entry.get('name') or 'this video'), tooltip='Watch with sound')
        button.connect('clicked', lambda b, *_: self.pressed(b, entry['watch']))
        return button

    def tune_youtube(self, item):
        return self.window.start('link', {'url': item.get('url'), 'destination': 'music'}, item.get('name') or 'the video')

    def watch_youtube(self, item):
        """Watch with sound. In ambience mode this leaves the mode: the room's audio is paused, not changed."""
        if self.mode == 'ambience':
            self.end_ambience()
        return self.window.watching.watch(item)

    def scene_from_video(self, item):
        """Show as scene (silent): only the picture layer changes, and nothing starts sound."""
        room = (self.snapshot.get('active') or {})
        name = item.get('name') or 'The video'
        return self.window.command('link', {'url': item.get('url'), 'destination': 'picture'},
                                   done=lambda _result: self.scene_shown(name, room))

    def scene_shown(self, name, room):
        if room and model.room_base(self.snapshot, self.window.catalogue) is not None:
            self.window.toast('Changed: %s is the scene in %s' % (name, room.get('name') or 'your room'),
                              'Save as my room', self.window.save_dialog)
        else:
            self.window.toast('Scene: ' + name)

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

    # ---- YouTube shelves (before you type) ----------------------------------------------

    def youtube_room(self):
        """The catalogue room behind the active room, whose tags seed "For ‹room›"."""
        active = self.snapshot.get('active') or {}
        identity = active.get('room_id') or active.get('id')
        return next((room for room in self.window.catalogue or () if room.get('id') == identity), None)

    def youtube_signature(self):
        state = self.youtube_state()
        room = self.youtube_room() or {}
        return (room.get('id'), state['version'], state['loading'], youtube_search.available())

    def youtube_state(self):
        state = getattr(self, '_youtube_shelves', None)
        if state is None:
            state = self._youtube_shelves = {'entries': None, 'loading': False, 'tried': {}, 'failed': set(),
                                             'version': 0}
        return state

    def render_youtube_home(self):
        """The YouTube scope before a search: how it works, your saved YouTube (no network), then the
        cached suggestion shelves, refreshed off the GTK thread. Nothing here plays until chosen."""
        self.index_body.append(self.section('YouTube', [], note=self.youtube_note(), trailing=self.youtube_trailing()))
        saved = youtube_shelves.saved_videos(self.snapshot.get('stations'), self.snapshot.get('station_recent'))
        if saved:
            self.index_body.append(self.section('Your saved YouTube', [self.youtube_entry(item) for item in saved]))
        if not youtube_search.available():
            return
        state = self.youtube_state()
        shelves = youtube_shelves.plan(self.youtube_room())
        entries = state['entries'] or {}
        now = time.time()
        for shelf in shelves:
            entry = entries.get(shelf['key'])
            rows = [self.youtube_entry(item) for item in (entry or {}).get('rows') or []]
            if rows:
                note = youtube_shelves.age_words(entry['fetched_at'], now)
                if shelf['key'] in state['failed']:
                    note += '. Couldn’t refresh just now.'
            elif state['loading'] or state['entries'] is None:
                note = 'Looking for suggestions…'
            else:
                note = 'No suggestions right now.' if shelf['key'] in state['failed'] else None
            if rows or note:
                self.index_body.append(self.section(shelf['title'], rows, note=note))
        self.refresh_youtube_shelves(shelves)

    def refresh_youtube_shelves(self, shelves):
        """Read the cache, show it, then refresh stale shelves; all off the GTK thread, at most hourly."""
        state = self.youtube_state()
        if state['loading'] or self.closed:
            return
        now = time.monotonic()
        known = state['entries']
        due = [shelf for shelf in shelves if known is None or (
            youtube_shelves.stale(known.get(shelf['key'])) and now - state['tried'].get(shelf['key'], -1e9) >= (
                youtube_shelves.RETRY_S if shelf['key'] in state['failed'] else 0))]
        if not due:
            return
        for shelf in due:
            state['tried'][shelf['key']] = now
        state['loading'] = True
        cancel = self.youtube_cancel = threading.Event()

        def work():
            cached = youtube_shelves.load() if known is None else known
            if known is None:
                GLib.idle_add(self.youtube_shelves_read, cached, None)
            return youtube_shelves.refresh(due, cached, cancel)

        run_async(work, lambda result: self.youtube_shelves_read(*result, finished=True),
                  lambda _error: self.youtube_shelves_read(state['entries'] or {}, [s['key'] for s in due], finished=True),
                  self.executor)

    def youtube_shelves_read(self, entries, failed, finished=False):
        state = self.youtube_state()
        if self.closed:
            return False
        state['entries'] = dict(entries)
        if finished:
            state['loading'] = False
            state['failed'] = set(failed or ())
        state['version'] += 1
        if self.scope == 'youtube' and not self.query:
            self.refresh()
        return False

    def youtube_close(self):
        cancel = getattr(self, 'youtube_cancel', None)
        if cancel is not None:
            cancel.set()
