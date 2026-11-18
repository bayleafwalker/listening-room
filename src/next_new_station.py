"""New station: a name, a seed and the mood choices, made from scratch.

The seed is one of: a stream link the listener pastes, a result of the same searches Listen
runs (radio, library, live channels, YouTube), or one of the radio stations that ship with
the app. Searching reads only; nothing plays and nothing is saved until Create station.
"""
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

import listening_hub as hub
import next_links
import next_station
import youtube_search
from next_widgets import label, name_it

MODES = (('link', 'Stream link'), ('search', 'Search'), ('builtin', 'Built-in radio'))
SEARCH_SCOPES = (('radio', 'Radio'), ('library', 'Library'), ('channels', 'Live channels'), ('youtube', 'YouTube'))
SEARCH_LIMIT = 12


def library_detail(item):
    kind = {'MusicAlbum': 'Album', 'Playlist': 'Playlist', 'Audio': 'Track'}.get(item.get('type'), 'Library item')
    return ' · '.join(part for part in (kind, item.get('artist')) if part) + ' · Jellyfin'


def seed_rows(scope, query, host, cancel):
    """Rows for one search scope: {'title', 'detail', 'source'}; ``source`` feeds station_from_source."""
    rows = []
    text = (query or '').strip()
    if scope == 'radio':
        found = hub.search_radio(text) if text else []
        local = [item for item in host.window.radio if isinstance(item, dict) and item.get('kind') == 'radio'
                 and text.casefold() in ' '.join([str(item.get('name') or ''), ' '.join(map(str, item.get('tags') or []))]).casefold()]
        seen = set()
        for item in list(local) + [i for i in found if isinstance(i, dict) and i.get('kind') == 'radio']:
            url = item.get('url') or ''
            if not hub.valid_url(url) or url in seen:
                continue
            seen.add(url)
            rows.append({'title': item.get('name') or 'Radio station', 'detail': str(item.get('group') or 'Radio') + ' · live',
                         'source': item})
    elif scope == 'library':
        if not host.library_configured():
            return []
        for kind in ('albums', 'playlists', 'tracks'):
            if cancel.is_set():
                break
            for item in host.browse_kind(kind, text, cancel) or []:
                if isinstance(item, dict) and item.get('type') in ('MusicAlbum', 'Playlist', 'Audio'):
                    rows.append({'title': item.get('name') or 'Library item', 'detail': library_detail(item), 'source': item})
    elif scope == 'channels':
        for item in host.channels:
            if text.casefold() in ' '.join([str(item.get('name') or ''), str(item.get('login') or ''),
                                            str(item.get('title') or '')]).casefold():
                rows.append({'title': item.get('name') or item.get('login') or 'Channel',
                             'detail': 'Live channel · Twitch' + (' · live' if item.get('live') else ''),
                             'source': next_station.source_from_channel(item)})
    elif scope == 'youtube':
        for item in youtube_search.search(text, cancel):
            rows.append({'title': item['name'], 'detail': host.video_detail(item), 'source': next_station.source_from_video(item)})
    return rows[:SEARCH_LIMIT * 3 if scope == 'library' else SEARCH_LIMIT]


class NewStationDialog(Adw.Dialog):
    """Collect a name, a seed and moods, then hand a station to ``save``."""

    def __init__(self, host, save):
        super().__init__()
        self.host, self.save = host, save
        self.set_title('New station')
        self.set_content_width(560)
        self.set_content_height(640)
        self.seed = None            # the chosen source, as station_from_source reads it
        self.seed_text = ''
        self.moods = {}             # preference key -> value, only for choices the listener made
        self.name_touched = False
        self.cancel = None
        self.serial = 0
        self.closed_flag = False
        self.mood_groups = {}
        self.mode = 'link'
        self.search_scope = 'radio'
        self.builtin = self.builtin_choices()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=8, margin_bottom=16,
                       margin_start=20, margin_end=20)
        body.append(label('Name it, choose where it starts, and set the mood. Nothing plays until you tune in.',
                          ['section-note'], wrap=True))
        self.name = Gtk.Entry(placeholder_text='Station name')
        name_it(self.name, 'Station name')
        self.name.connect('changed', self.name_changed)
        body.append(self.name)
        body.append(label('Start from', ['listen-title']))
        self.modes = Adw.ToggleGroup(halign=Gtk.Align.START, homogeneous=False)
        name_it(self.modes, 'Start from', '')
        for key, title in MODES:
            self.modes.add(Adw.Toggle(label=title, name=key))
        self.modes.set_active(0)
        self.modes.connect('notify::active', self.mode_changed)
        body.append(self.modes)
        self.pages = Gtk.Stack(vhomogeneous=False)
        self.pages.add_named(self.build_link(), 'link')
        self.pages.add_named(self.build_search(), 'search')
        self.pages.add_named(self.build_builtin(), 'builtin')
        body.append(self.pages)
        self.chosen = label('Nothing chosen yet.', ['section-note'], wrap=True)
        name_it(self.chosen, 'Chosen start', None)
        body.append(self.chosen)
        self.similar = Gtk.CheckButton(label='Keep adding similar tracks (AudioMuse)')
        self.similar.set_visible(False)
        body.append(self.similar)
        body.append(label('Mood', ['listen-title']))
        for mood in next_station.MOODS:
            body.append(self.build_mood(mood))
        self.mood_note = label('', ['section-note'], wrap=True)
        body.append(self.mood_note)
        scroller.set_child(body)
        toolbar.set_content(scroller)
        footer = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8, margin_bottom=12, margin_end=20)
        cancel = Gtk.Button(label='Cancel')
        cancel.add_css_class('flat')
        cancel.connect('clicked', lambda *_: self.close())
        self.create = Gtk.Button(label='Create station')
        self.create.add_css_class('suggested-action')
        self.create.set_sensitive(False)
        self.create.connect('clicked', lambda *_: self.finish())
        footer.append(cancel)
        footer.append(self.create)
        toolbar.add_bottom_bar(footer)
        self.set_child(toolbar)
        self.set_default_widget(self.create)
        self.connect('closed', lambda *_: self.close_dialog())
        self.sync()

    # ---- seed pages ---------------------------------------------------------

    def build_link(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=8)
        self.link = Gtk.Entry(placeholder_text='https://', hexpand=True)
        name_it(self.link, 'Stream link')
        self.link.connect('activate', lambda *_: self.check_link())
        self.link.connect('changed', lambda *_: self.link_edited())
        check = Gtk.Button(label='Use this link')
        check.connect('clicked', lambda *_: self.check_link())
        row.append(self.link)
        row.append(check)
        box.append(row)
        self.link_note = label(next_links.ENTRY_HINT, ['section-note'], wrap=True)
        box.append(self.link_note)
        return box

    def build_search(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(spacing=8)
        self.query = Gtk.SearchEntry(placeholder_text='Search for a station, artist, album or video', hexpand=True)
        name_it(self.query, 'Search for a start')
        self.query.connect('activate', lambda *_: self.run_search())
        button = Gtk.Button(label='Search')
        button.connect('clicked', lambda *_: self.run_search())
        row.append(self.query)
        row.append(button)
        box.append(row)
        self.scopes = Adw.ToggleGroup(halign=Gtk.Align.START, homogeneous=False)
        name_it(self.scopes, 'Search in', '')
        for key, title in SEARCH_SCOPES:
            self.scopes.add(Adw.Toggle(label=title, name=key))
        self.scopes.set_active(0)
        self.scopes.connect('notify::active', self.scope_changed)
        box.append(self.scopes)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.results_note = label('Search, then choose one result to start from.', ['section-note'], wrap=True)
        box.append(self.results_note)
        box.append(self.results)
        return box

    def builtin_choices(self):
        choices = []
        for item in self.host.window.radio:
            if isinstance(item, dict) and item.get('kind') == 'radio' and hub.valid_url(item.get('url') or ''):
                choices.append((item.get('name') or 'Radio', item, item.get('group') or 'Radio'))
        return choices

    def build_builtin(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        if not self.builtin:
            box.append(label('There is no built-in radio to start from.', ['section-note']))
            self.shortcut = None
            return box
        self.shortcut = Gtk.DropDown.new_from_strings(['%s — %s' % (name, where) for name, _item, where in self.builtin])
        name_it(self.shortcut, 'Built-in radio')
        self.shortcut.connect('notify::selected', lambda *_: self.use_builtin())
        box.append(self.shortcut)
        return box

    def build_mood(self, mood):
        row = Gtk.Box(spacing=12)
        title = label(mood['title'], ['row-name'])
        title.set_size_request(96, -1)
        row.append(title)
        group = Adw.ToggleGroup(halign=Gtk.Align.START, homogeneous=False)
        name_it(group, mood['title'], '')
        neutral = 0
        for index, (text, value) in enumerate(mood['choices']):
            group.add(Adw.Toggle(label=text, name=value))
            if value in ('Any',):
                neutral = index
        group.set_active(neutral)
        group.connect('notify::active', lambda g, *_, m=mood: self.mood_changed(m, g))
        self.mood_groups[mood['key']] = group
        row.append(group)
        return row

    # ---- state ----------------------------------------------------------------

    def name_changed(self, entry):
        self.name_touched = bool(entry.get_text().strip())
        self.sync()

    def mode_changed(self, *_):
        index = self.modes.get_active()
        if not 0 <= index < len(MODES) or MODES[index][0] == self.mode:
            return
        self.mode = MODES[index][0]
        self.pages.set_visible_child_name(self.mode)
        self.stop()
        if self.mode == 'builtin':
            self.use_builtin()

    def scope_changed(self, *_):
        index = self.scopes.get_active()
        if 0 <= index < len(SEARCH_SCOPES):
            self.search_scope = SEARCH_SCOPES[index][0]
            self.stop()
            self.clear_results('Search, then choose one result to start from.')
            if self.query.get_text().strip():
                self.run_search()

    def mood_changed(self, mood, group):
        index = group.get_active()
        if not 0 <= index < len(mood['choices']):
            return
        value = mood['choices'][index][1]
        if value == 'Any':   # every mood's neutral choice means "leave it unset"
            self.moods.pop(mood['key'], None)
        else:
            self.moods[mood['key']] = value
        self.sync()

    def choose(self, source, text):
        """Make this the start; the name follows it until the listener types one."""
        self.seed, self.seed_text = dict(source), text
        if not self.name_touched:
            self.name.handler_block_by_func(self.name_changed)
            self.name.set_text(source.get('name') or (source.get('reference') or {}).get('name') or 'Station')
            self.name.handler_unblock_by_func(self.name_changed)
        self.sync()

    def sync(self):
        seed = self.seed
        library = bool(seed and (seed.get('reference') or seed.get('provider') == 'jellyfin'))
        self.chosen.set_text('Starts from: ' + self.seed_text if seed else 'Nothing chosen yet.')
        self.similar.set_visible(bool(seed and seed.get('type') == 'Audio' and self.host.audiomuse_ready()))
        strict = self.mood_groups['speech']
        toggles = [strict.get_toggle(i) for i in range(strict.get_n_toggles())]
        for toggle in toggles:
            if toggle.get_name() == 'Strict instrumental':
                toggle.set_enabled(library or not seed)
        if seed and not library and self.moods.get('speech') == 'Strict instrumental':
            self.moods.pop('speech')
            strict.set_active(0)
        self.mood_note.set_text('' if library or not seed else
                                'Instrumental only needs library tracks, so it is off for this start.')
        self.create.set_sensitive(bool(seed and self.name.get_text().strip()))

    # ---- link ---------------------------------------------------------------------

    def link_edited(self):
        if self.mode == 'link' and self.seed is not None:
            self.seed, self.seed_text = None, ''   # a changed link has not been checked yet
            self.sync()

    def check_link(self):
        value = self.link.get_text().strip()
        self.seed = None
        try:
            preview = next_links.descriptor(value)
        except Exception as error:
            self.link_note.set_text(str(error) or 'That is not a public link.')
            self.link_note.add_css_class('error')
            self.sync()
            return False
        source = next_station.source_from_link(preview)
        if source is None:
            self.link_note.set_text(next_station.LINK_PROBLEM)
            self.link_note.add_css_class('error')
            self.sync()
            return False
        self.link_note.remove_css_class('error')
        note = next_links.detail(preview)
        if next_links.non_public(value):
            note += ' · on your own network'
        self.link_note.set_text(note)
        self.choose(source, source['name'] + ' (' + next_links.host(value) + ')')
        return True

    # ---- built-in radio -----------------------------------------------------------

    def use_builtin(self):
        if not self.builtin or self.shortcut is None:
            return
        index = self.shortcut.get_selected()
        name, item, where = self.builtin[index if 0 <= index < len(self.builtin) else 0]
        self.choose(item, name + ' (' + str(where) + ')')

    # ---- search -------------------------------------------------------------------

    def clear_results(self, note):
        while self.results.get_first_child():
            self.results.remove(self.results.get_first_child())
        self.results_note.set_text(note)

    def stop(self):
        if self.cancel is not None:
            self.cancel.set()
            self.cancel = None
        self.serial += 1

    def run_search(self):
        text = self.query.get_text().strip()
        self.stop()
        if not text:
            self.clear_results('Type something to search for.')
            return
        scope = self.search_scope
        if scope == 'youtube' and not youtube_search.available():
            self.clear_results(youtube_search.MISSING)
            return
        self.clear_results('Searching…')
        cancel = threading.Event()
        self.cancel = cancel
        serial = self.serial
        future = self.host.executor.submit(seed_rows, scope, text, self.host, cancel)

        def done(f):
            try:
                rows, error = f.result(), None
            except Exception as problem:
                rows, error = [], str(problem) or type(problem).__name__
            GLib.idle_add(self.show_results, serial, scope, rows, error)
        future.add_done_callback(done)

    def show_results(self, serial, scope, rows, error):
        if serial != self.serial or self.closed_flag:
            return False
        self.clear_results('')
        if error:
            self.results_note.set_text('That search did not finish. Try again.')
            return False
        if not rows:
            self.results_note.set_text('Nothing found.')
            return False
        self.results_note.set_text('Choose one result to start from.')
        for row in rows:
            self.results.append(self.result_row(row))
        return False

    def result_row(self, row):
        box = Gtk.Box(spacing=12)
        box.add_css_class('listen-row')
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(label(row['title'], ['row-name'], ellipsize=True))
        text.append(label(row['detail'], ['row-detail'], ellipsize=True))
        box.append(text)
        button = Gtk.Button(label='Start from this', valign=Gtk.Align.CENTER)
        name_it(button, 'Start from ' + row['title'], None)
        button.connect('clicked', lambda *_, r=row: self.choose(r['source'], r['title']))
        box.append(button)
        return box

    # ---- finishing ------------------------------------------------------------------

    def finish(self):
        if not self.seed or not self.name.get_text().strip():
            return
        station = next_station.new_station(self.name.get_text(), self.seed, self.moods,
                                           similar=self.similar.get_visible() and self.similar.get_active())
        self.close()
        self.save(station)

    def close_dialog(self):
        self.closed_flag = True
        self.stop()

