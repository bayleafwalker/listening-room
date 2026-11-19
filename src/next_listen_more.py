"""Listen, continued: what the music services add beyond search.

* Library kinds and opening an album or playlist (Jellyfin).
* Speakers: Music Assistant players, browse and search, play on a speaker, radio mix, transfer.
* History (the remembered scope): local bookmarks and, on request only, ListenBrainz history and recommendations.

Everything here reads on request and plays nothing until you press a play action. The
controls that stay on screen (kind choice, speaker choice, note entry) are built once and
shown for their scope, so a refresh never takes the focus from them.
"""
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

import next_station
from next_widgets import name_it

LIBRARY_KINDS = (('albums', 'Albums'), ('playlists', 'Playlists'), ('recent', 'Recently added'), ('tracks', 'Tracks'))
MA_KINDS = (('playlists', 'Playlists'), ('tracks', 'Tracks'), ('albums', 'Albums'))
MORE_SCOPES = (('speakers', 'Speakers'), ('remembered', 'History'))


def configured(service, field='endpoint'):
    try:
        import online_services
        return bool(online_services.config()[service].get(field))
    except (ImportError, KeyError, OSError, ValueError):
        return False


def media_title(item):
    return ' — '.join(str(x) for x in (item.get('artist'), item.get('title') or item.get('station') or item.get('name')) if x)


def history_rows(rows):
    """ListenBrainz listens as plain artist and title rows."""
    found = []
    for row in rows or []:
        meta = row.get('track_metadata') if isinstance(row, dict) else None
        if isinstance(meta, dict):
            found.append({'artist': str(meta.get('artist_name', '')), 'title': str(meta.get('track_name', ''))})
    return found


class ServicesMixin:
    """Mixed into ListenPage; uses its row, section, window and executor."""

    def init_services(self):
        self.kind = 'albums'
        self.kinds = {}        # kind -> {'state', 'items'} for the kinds other than albums
        self.opened = None     # {'item', 'state', 'tracks'}: an album or playlist looked into
        self.ma = {'state': 'idle', 'players': [], 'speaker': None, 'items': {}, 'busy': set()}
        self.memory = {'local': None, 'extra': None, 'busy': False}
        self.scope_syncing = False

    # ---- controls that stay on screen ---------------------------------------------

    def build_scope_controls(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.kind_group = Adw.ToggleGroup(halign=Gtk.Align.START, homogeneous=False)
        name_it(self.kind_group, 'Library shows', '')
        for key, title in LIBRARY_KINDS:
            self.kind_group.add(Adw.Toggle(label=title, name=key))
        self.kind_group.set_active(0)
        self.kind_group.connect('notify::active', self.kind_changed)
        box.append(self.kind_group)

        self.speaker_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=8)
        self.speaker_drop = Gtk.DropDown.new_from_strings(['No speaker chosen'])
        self.speaker_drop.set_hexpand(True)
        name_it(self.speaker_drop, 'Speaker to play on', None)
        self.speaker_drop.connect('notify::selected', self.speaker_chosen)
        reload = Gtk.Button(label='Load speakers')
        reload.connect('clicked', lambda *_: self.load_speakers(force=True))
        row.append(self.speaker_drop)
        row.append(reload)
        self.speaker_box.append(row)
        row = Gtk.Box(spacing=8)
        self.ma_kind = Adw.ToggleGroup(halign=Gtk.Align.START, homogeneous=False)
        name_it(self.ma_kind, 'Music Assistant shows', '')
        for key, title in MA_KINDS:
            self.ma_kind.add(Adw.Toggle(label=title, name=key))
        self.ma_kind.set_active(0)
        self.ma_kind.connect('notify::active', self.ma_kind_changed)
        self.ma_provider = Gtk.Entry(placeholder_text='Provider (optional)', hexpand=True)
        name_it(self.ma_provider, 'Music Assistant provider, optional', None)
        self.ma_provider.connect('activate', lambda *_: self.ma_kind_changed())
        row.append(self.ma_kind)
        row.append(self.ma_provider)
        self.speaker_box.append(row)
        box.append(self.speaker_box)

        self.memory_box = Gtk.Box(spacing=8)
        self.memory_note = Gtk.Entry(placeholder_text='Optional note about this moment', hexpand=True)
        name_it(self.memory_note, 'Note for what is playing', None)
        remember = Gtk.Button(label='Remember playing')
        remember.connect('clicked', lambda *_: self.remember_playing())
        history = Gtk.Button(label='ListenBrainz history')
        history.connect('clicked', lambda *_: self.load_history())
        recs = Gtk.Button(label='Recommendations')
        recs.connect('clicked', lambda *_: self.load_recommendations())
        for widget in (self.memory_note, remember, history, recs):
            self.memory_box.append(widget)
        box.append(self.memory_box)
        self.show_scope_controls()
        return box

    def build_more_scopes(self):
        """The scopes beyond the six: a second group on the same line; only one group is ever active."""
        self.more_scopes = Adw.ToggleGroup(halign=Gtk.Align.START, homogeneous=False)
        self.more_scopes.add_css_class('listen-scopes')
        name_it(self.more_scopes, 'More places to listen from', '')
        for key, title in MORE_SCOPES:
            self.more_scopes.add(Adw.Toggle(label=title, name=key))
        self.more_scopes.set_active(Gtk.INVALID_LIST_POSITION)
        self.more_scopes.connect('notify::active', self.scope_changed)
        return self.more_scopes

    def show_scope_controls(self):
        scope = self.scope
        self.kind_group.set_visible(scope == 'library')
        self.speaker_box.set_visible(scope == 'speakers')
        self.memory_box.set_visible(scope == 'remembered')

    def scope_entered(self):
        """A scope became the one on screen: read what it shows (reads only)."""
        self.opened = None
        self.show_scope_controls()
        if self.scope == 'library' and self.kind != 'albums':
            self.load_kind(self.kind)
        elif self.scope == 'speakers':
            self.load_speakers()
        elif self.scope == 'remembered':
            self.load_remembered()

    # ---- Jellyfin kinds, albums and playlists ----------------------------------------

    def kind_changed(self, *_args):
        index = self.kind_group.get_active()
        if not 0 <= index < len(LIBRARY_KINDS) or LIBRARY_KINDS[index][0] == self.kind:
            return
        self.kind = LIBRARY_KINDS[index][0]
        self.opened = None
        self.remote.pop('library', None)
        if self.kind != 'albums':
            self.load_kind(self.kind)
        self.schedule_search()
        self.refresh()

    def load_kind(self, kind):
        slot = self.kinds.setdefault(kind, {'state': 'idle', 'items': []})
        if not self.library_configured():
            slot['state'] = 'unavailable'
            self.refresh()
            return
        if slot['state'] not in ('idle', 'failed', 'unavailable'):
            return
        slot['state'] = 'loading'
        future = self.executor.submit(self.browse_kind, kind, '', threading.Event())

        def done(f):
            try:
                items, error = f.result(), None
            except Exception as problem:
                items, error = [], str(problem) or type(problem).__name__
            GLib.idle_add(self.kind_loaded, kind, items, error)
        future.add_done_callback(done)
        self.refresh()

    def kind_loaded(self, kind, items, error):
        if not self.closed:
            slot = self.kinds[kind]
            slot['items'] = [item for item in items or [] if isinstance(item, dict)]
            slot['state'] = 'failed' if error else ('ready' if slot['items'] else 'empty')
            self.refresh()
        return False

    def kind_entries(self):
        """Rows of the kind chosen in Library; albums are the shelf the page already holds."""
        if self.kind == 'albums':
            return self.library_entries()
        return self.library_entries_for(self.kinds.get(self.kind, {}).get('items') or [])

    def kind_note(self):
        if self.kind == 'albums':
            return {'loading': 'Loading the library…', 'failed': 'The library could not be read.',
                    'unavailable': 'Connect Jellyfin in Preferences to see the library.'}.get(self.library_state)
        slot = self.kinds.get(self.kind, {'state': 'idle'})
        title = dict(LIBRARY_KINDS)[self.kind].lower()
        return {'idle': 'Loading ' + title + '…', 'loading': 'Loading ' + title + '…',
                'failed': 'This could not be read.', 'empty': 'Nothing here yet.',
                'unavailable': 'Connect Jellyfin in Preferences to see the library.'}.get(slot['state'])

    def open_album(self, item):
        """Look inside an album or playlist: its tracks, as rows with the track menu. Plays nothing."""
        self.show_library_scope()
        self.opened = {'item': item, 'state': 'loading', 'tracks': []}
        item_id = item.get('id')

        def work():
            import online_services
            return online_services.jellyfin_queue(item)
        future = self.executor.submit(work)

        def done(f):
            try:
                tracks, error = f.result(), None
            except Exception as problem:
                tracks, error = [], str(problem) or type(problem).__name__
            GLib.idle_add(self.album_loaded, item_id, tracks, error)
        future.add_done_callback(done)
        self.refresh()

    def album_loaded(self, item_id, tracks, error):
        if not self.closed and self.opened and self.opened['item'].get('id') == item_id:
            self.opened.update(tracks=[t for t in tracks or [] if isinstance(t, dict)],
                               state='failed' if error else 'ready', error=error)
            self.refresh()
        return False

    def close_album(self):
        self.opened = None
        self.refresh()

    def show_library_scope(self):
        self.search.set_text('')
        self.scopes.set_active(3)

    def render_opened(self):
        item = self.opened['item']
        note = {'loading': 'Loading the tracks…', 'failed': self.opened.get('error') or 'The tracks could not be read.'
                }.get(self.opened['state'])
        rows = self.library_entries_for(self.opened['tracks'])
        if self.opened['state'] == 'ready' and not rows:
            note = 'No playable tracks here.'
        self.index_body.append(self.section(
            item.get('name') or 'Library', rows, note=note,
            trailing=self.link_button('‹ Library', self.close_album)))

    def find_in_library(self, query):
        """History › Find in Jellyfin: the Tracks kind, searched for this text."""
        self.kind_group.set_active(3)
        self.show_library_scope()
        self.search.set_text(query)

    def make_audiomuse_station(self, item):
        """A new station seeded by one track; AudioMuse keeps adding similar tracks while it plays."""
        station = next_station.station_from_source(item)
        station['name'] = 'Mix from ' + (item.get('name') or 'a track')
        station['generation'] = {'kind': 'audiomuse'}
        self.window.command('station.save', {'station': station}, done=lambda result: self.window.toast(
            'Saved ' + next_station.name(result.get('station') or station) + ' as a station', 'Undo',
            lambda: self.window.command('station.delete', {'id': (result.get('station') or {}).get('id')})))

    def audiomuse_menu(self, item):
        if item.get('type') == 'Audio' and configured('audiomuse'):
            return [('Make an AudioMuse station', lambda item=item: self.make_audiomuse_station(item))]
        return []

    # ---- Speakers (Music Assistant) ---------------------------------------------------

    def ma_kind_key(self):
        index = self.ma_kind.get_active()
        return MA_KINDS[index][0] if 0 <= index < len(MA_KINDS) else 'playlists'

    def provider_text(self):
        return self.ma_provider.get_text().strip() or None

    def load_speakers(self, force=False):
        ma = self.ma
        if ma['state'] in ('loading',) or (ma['state'] != 'idle' and not force):
            return
        if not configured('music_assistant'):
            ma['state'] = 'unavailable'
            self.refresh()
            return
        ma['state'] = 'loading'

        def work():
            import online_services
            return online_services.ma_players()
        future = self.executor.submit(work)

        def done(f):
            try:
                rows, error = f.result(), None
            except Exception as problem:
                rows, error = [], str(problem) or type(problem).__name__
            GLib.idle_add(self.speakers_loaded, rows, error)
        future.add_done_callback(done)
        self.load_ma_items()
        self.refresh()

    def speakers_loaded(self, rows, error):
        if self.closed:
            return False
        ma = self.ma
        ma['players'] = rows or []
        ma['state'] = 'failed' if error else 'ready'
        ma['error'] = error
        self.speaker_updating = True
        names = [row['name'] + ('' if row['available'] else ' (unavailable)') for row in ma['players']]
        self.speaker_drop.set_model(Gtk.StringList.new(names or ['No speakers found']))
        keep = next((i for i, row in enumerate(ma['players']) if row['id'] == ma['speaker']), None)
        if keep is None:
            keep = next((i for i, row in enumerate(ma['players']) if row['available']), 0)
        if ma['players']:
            self.speaker_drop.set_selected(keep)
            ma['speaker'] = ma['players'][keep]['id']
        self.speaker_updating = False
        self.refresh()
        return False

    def speaker_chosen(self, *_args):
        if getattr(self, 'speaker_updating', False):
            return
        index = self.speaker_drop.get_selected()
        players = self.ma['players']
        if 0 <= index < len(players):
            self.ma['speaker'] = players[index]['id']
            self.refresh()

    def speaker(self):
        return next((row for row in self.ma['players'] if row['id'] == self.ma['speaker']), None)

    def ma_kind_changed(self, *_args):
        self.remote.pop('speakers', None)
        self.load_ma_items(force=True)
        self.schedule_search()
        self.refresh()

    def load_ma_items(self, force=False):
        kind, provider = self.ma_kind_key(), self.provider_text()
        slot = self.ma['items'].get((kind, provider))
        if slot and not force:
            return
        self.ma['items'][(kind, provider)] = {'state': 'loading', 'rows': []}

        def work():
            import online_services
            return online_services.browse_music_assistant('', kind, None, provider, False)
        future = self.executor.submit(work)

        def done(f):
            try:
                rows, error = f.result(), None
            except Exception as problem:
                rows, error = [], str(problem) or type(problem).__name__
            GLib.idle_add(self.ma_items_loaded, (kind, provider), rows, error)
        future.add_done_callback(done)

    def ma_items_loaded(self, key, rows, error):
        if not self.closed:
            self.ma['items'][key] = {'state': 'failed' if error else ('ready' if rows else 'empty'),
                                     'rows': rows or [], 'error': error}
            self.refresh()
        return False

    def search_speakers(self, query, cancel):
        import online_services
        return online_services.browse_music_assistant(query, self.ma_kind_key(), cancel, self.provider_text(), True)

    def speaker_entries(self):
        entries = []
        players = self.ma['players']
        for player in players:
            volume = player.get('volume')
            detail = ' · '.join(str(x) for x in (player.get('state'), None if volume is None else '%d%%' % round(volume))
                                if x) + ('' if player['available'] else ' · unavailable')
            chosen = player['id'] == self.ma['speaker']
            menu = [("Move %s's queue here" % other['name'],
                     lambda source=other, target=player: self.transfer_queue(source, target))
                    for other in players if other['id'] != player['id'] and other['available']]
            entries.append({'kind': 'speaker', 'name': player['name'],
                            'detail': ('Chosen · ' if chosen else '') + detail,
                            'action': None if chosen or not player['available'] else
                            ('Use', lambda player=player: self.use_speaker(player)),
                            'menu': menu if player['available'] else []})
        return entries

    def use_speaker(self, player):
        self.ma['speaker'] = player['id']
        index = next(i for i, row in enumerate(self.ma['players']) if row['id'] == player['id'])
        self.speaker_updating = True
        self.speaker_drop.set_selected(index)
        self.speaker_updating = False
        self.refresh()

    def media_entries(self, rows):
        player = self.speaker()
        where = player['name'] if player else 'a speaker'
        entries = []
        for item in rows:
            entries.append({'kind': 'ma', 'name': item.get('name') or 'Music', 'item': item,
                            'detail': ' · '.join(x for x in (item.get('type', '').title(), item.get('artist'), 'Music Assistant') if x),
                            'action': ('Play on ' + where, lambda item=item: self.play_on_speaker(item)),
                            'menu': [('Start a radio mix on ' + where, lambda item=item: self.play_on_speaker(item, True))]})
        return entries

    def play_on_speaker(self, item, radio=False):
        player = self.speaker()
        if player is None or not player['available']:
            self.window.toast('Choose an available speaker first')
            return
        sent = dict(item, player_id=player['id'], name=item.get('name') or player['name'])
        if radio:
            sent['radio_mode'] = True
        return self.window.command('online', {'service': 'music_assistant', 'item': sent},
                                   done=lambda _r: self.window.toast('Sent to ' + player['name']))

    def transfer_queue(self, source, target):
        item = {'provider': 'music_assistant', 'action': 'transfer', 'source_player_id': source['id'],
                'player_id': target['id'], 'name': target['name']}
        self.window.command('online', {'service': 'music_assistant', 'item': item},
                            done=lambda _r: self.window.toast('Moving the queue to ' + target['name']))

    def render_speakers(self):
        ma = self.ma
        if ma['state'] == 'unavailable':
            self.index_body.append(self.section('Speakers', [], note='Connect Music Assistant in Preferences › Connections to play on its speakers.'))
            return
        note = {'idle': 'Loading speakers…', 'loading': 'Loading speakers…',
                'failed': ma.get('error') or 'Music Assistant could not be reached.'}.get(ma['state'])
        if ma['state'] == 'ready' and not ma['players']:
            note = 'Music Assistant returned no speakers.'
        self.index_body.append(self.section('Speakers', self.speaker_entries(), note=note))
        key = (self.ma_kind_key(), self.provider_text())
        if self.query:
            remote = self.remote.get('speakers') or {}
            fresh = remote.get('query') == self.query
            rows = remote.get('rows') or [] if fresh and not remote.get('error') else []
            note = ('Searching Music Assistant…' if self.searching else
                    'The search could not be completed.' if fresh and remote.get('error') else
                    None if rows else 'Nothing matched.')
            title = 'Results'
        else:
            slot = self.ma['items'].get(key) or {'state': 'loading', 'rows': []}
            rows = slot['rows']
            note = {'loading': 'Loading music…', 'failed': slot.get('error') or 'The music could not be read.',
                    'empty': 'Nothing in this library list yet. Search to find music in its providers.'}.get(slot['state'])
            title = dict(MA_KINDS)[key[0]]
        self.index_body.append(self.section(title, self.media_entries(rows), note=note))

    # ---- Remembered -----------------------------------------------------------------------

    def load_remembered(self):
        def work():
            import online_services
            return online_services.remembered()[:20]
        future = self.executor.submit(work)

        def done(f):
            try:
                rows = f.result()
            except Exception:
                rows = []
            GLib.idle_add(self.remembered_loaded, rows)
        future.add_done_callback(done)

    def remembered_loaded(self, rows):
        if not self.closed:
            self.memory['local'] = rows
            self.refresh()
        return False

    def remember_playing(self):
        note = self.memory_note.get_text()
        snapshot = self.snapshot

        def work():
            import online_services
            return online_services.bookmark(snapshot, note)
        future = self.executor.submit(work)

        def done(f):
            try:
                f.result()
                message = 'Remembered. Bookmarks stay on this computer.'
            except Exception as problem:
                message = str(problem) or 'This could not be remembered.'
            GLib.idle_add(self.remembered_done, message)
        future.add_done_callback(done)

    def remembered_done(self, message):
        if not self.closed:
            self.window.toast(message)
            self.memory_note.set_text('')
            self.load_remembered()
        return False

    def load_extra(self, title, fetch, project):
        """History or recommendations: read once, when asked."""
        if self.memory['busy']:
            return
        self.memory['busy'] = True
        self.refresh()

        def work():
            return project(fetch())
        future = self.executor.submit(work)

        def done(f):
            try:
                rows, error = f.result(), None
            except Exception as problem:
                rows, error = [], str(problem) or 'ListenBrainz could not be read.'
            GLib.idle_add(self.extra_loaded, title, rows, error)
        future.add_done_callback(done)

    def extra_loaded(self, title, rows, error):
        if self.closed:
            return False
        self.memory['busy'] = False
        self.memory['extra'] = {'title': title, 'rows': rows, 'error': error}
        self.refresh()
        return False

    def load_history(self):
        import online_services
        self.load_extra('ListenBrainz history', online_services.listenbrainz_history, history_rows)

    def load_recommendations(self):
        import online_services
        self.load_extra('Recommendations', online_services.listenbrainz_recommendations, lambda rows: list(rows or []))

    def remembered_entries(self, rows):
        import online_services
        entries = []
        for item in rows:
            links = online_services.discovery_links(item)
            name = media_title(item) or 'Remembered moment'
            entries.append({'kind': 'remembered', 'name': name,
                            'detail': ' · '.join(x for x in ('Bookmark', item.get('note')) if x),
                            'action': ('Find in Jellyfin', lambda query=links['query']: self.find_in_library(query)),
                            'menu': [('Open in YouTube Music', lambda url=links['youtube_music']: self.open_external(url))]})
        return entries

    def extra_entries(self, extra):
        import online_services
        entries = []
        for item in extra['rows']:
            if 'url' in item:   # a recommendation names a recording, not a verified title
                entries.append({'kind': 'recommendation', 'name': 'Recommended recording',
                                'detail': 'Opens its MusicBrainz page for verified details',
                                'action': ('Open', lambda url=item['url']: self.open_external(url)), 'menu': []})
            else:
                links = online_services.discovery_links(item)
                entries.append({'kind': 'history', 'name': media_title(item) or 'Listen', 'detail': 'ListenBrainz',
                                'action': ('Find in Jellyfin', lambda query=links['query']: self.find_in_library(query)),
                                'menu': [('Open in YouTube Music', lambda url=links['youtube_music']: self.open_external(url))]})
        return entries

    def render_remembered(self):
        local = self.memory['local']
        rows = self.remembered_entries(local or [])
        note = 'Loading…' if local is None else None if rows else 'Nothing remembered yet. Remember what is playing from here.'
        self.index_body.append(self.section('History', rows, note=note))
        extra = self.memory['extra']
        if self.memory['busy']:
            self.index_body.append(self.section('ListenBrainz', [], note='Reading ListenBrainz…'))
        elif extra:
            rows = self.extra_entries(extra)
            note = extra['error'] or (None if rows else 'Nothing came back for this account yet.')
            if not extra['error'] and extra['title'] == 'ListenBrainz history':
                note = note or 'Recent listens. Loading history does not submit anything.'
            self.index_body.append(self.section(extra['title'], rows, note=note))

    def services_signature(self):
        ma = self.ma
        return [self.kind, {k: [v['state'], len(v['items'])] for k, v in self.kinds.items()},
                [self.opened['item'].get('id'), self.opened['state'], len(self.opened['tracks'])] if self.opened else None,
                [ma['state'], [(p['id'], p.get('state'), p['available']) for p in ma['players']], ma['speaker'],
                 {str(k): [v['state'], len(v['rows'])] for k, v in ma['items'].items()}],
                [self.memory['busy'], len(self.memory['local'] or []),
                 [self.memory['extra']['title'], len(self.memory['extra']['rows'])] if self.memory['extra'] else None]]
