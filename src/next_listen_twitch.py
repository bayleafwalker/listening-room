"""Listen, Twitch: category chips, rows with what is on, sorting and "Show more".

Mixed into ListenPage. Each category keeps its own list and Helix cursor (Music, ASMR and the categories the
listener adds with ＋ Category), so one busy category never pushes another out. "All" takes turns between
them, "Your channels" lists the saved and followed channels (live ones first), and "Show more" asks Helix
for the next page of what is on screen. Reads happen off the GTK thread; browsing sends nothing to a room.
"""
import hashlib
import os
from pathlib import Path
import urllib.request

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk

import channel_browse
from next_async import error_text, run_async
from next_widgets import label, name_it, texture_for

STYLE_NAMES = {'music': 'Music', 'asmr': 'ASMR'}
FIXED_CHIPS = (('all', 'All'), ('music', 'Music'), ('asmr', 'ASMR'))
THUMB_LIMIT = 300 * 1024


def thumbnail_folder():
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'listening-hub' / 'twitch-previews'


def fetch_thumbnail(url):
    """The preview image of a stream as a cached file, or None. Only public Twitch image addresses (see
    twitch_channels.thumbnail) are ever asked for, with no credentials, no redirects and a size limit."""
    import http_fetch
    import twitch_channels
    if not twitch_channels.thumbnail(url) or twitch_channels.thumbnail(url) != url:
        return None
    folder = thumbnail_folder()
    path = folder / (hashlib.sha256(url.encode()).hexdigest()[:24] + '.img')
    if path.is_file():
        return str(path)
    request = urllib.request.Request(url, headers={'User-Agent': 'listening-room'})
    with http_fetch.open_url(request, 5) as response:
        body = http_fetch.read(response, THUMB_LIMIT)
    if not (body.startswith(b'\xff\xd8') or body.startswith(b'\x89PNG')):
        return None
    folder.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.part')
    temporary.write_bytes(body)
    os.replace(temporary, path)
    return str(path)


class TwitchShelfMixin:
    def init_twitch_shelf(self):
        self.twitch_pages = {}          # 'music' | 'asmr' | 'cat:<id>' -> {'name', 'channels', 'cursor', 'game_id', ...}
        self.twitch_yours = []          # saved and followed channels, as read
        self.channel_filter = 'all'
        self.channel_sort = 'most'
        self.twitch_more_loading = False
        self.twitch_more_problem = ''
        self.thumbs = {}                # preview address -> cached file, '' while loading or when there is none
        self.thumbs_version = 0
        self.channel_syncing = False

    # ---- controls ----------------------------------------------------------------------

    def build_channel_controls(self):
        self.channel_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.channel_status = label(channel_browse.SIGNED_OUT_NOTE, ['section-note'], wrap=True)
        self.channel_box.append(self.channel_status)
        wrap = Adw.WrapBox(child_spacing=8, line_spacing=8)
        self.channel_chips = Adw.ToggleGroup(halign=Gtk.Align.START, homogeneous=False)
        name_it(self.channel_chips, 'Live channels from', '')
        self.channel_chips.add_css_class('listen-scopes')
        self.channel_chips.connect('notify::active', self.channel_chip_changed)
        wrap.append(self.channel_chips)
        self.category_button = Gtk.MenuButton(label='＋ Category', popover=self.build_category_popover(), valign=Gtk.Align.CENTER)
        self.category_button.add_css_class('flat')
        name_it(self.category_button, 'Add a Twitch category', None)
        wrap.append(self.category_button)
        self.sort_drop = Gtk.DropDown.new_from_strings([title for _key, title in channel_browse.SORTS])
        name_it(self.sort_drop, 'Sort live channels', None)
        self.sort_drop.connect('notify::selected', self.channel_sort_changed)
        wrap.append(self.sort_drop)
        self.follows_button = self.link_button('Review my follows', lambda: self.window.open_preferences('twitch'))
        wrap.append(self.follows_button)
        self.channel_box.append(wrap)
        self.fill_channel_chips()
        self.channel_box.set_visible(False)
        return self.channel_box

    def chips(self):
        extra = [('cat:' + page_key.split(':', 1)[1], page['name']) for page_key, page in self.twitch_pages.items()
                 if page_key.startswith('cat:')]
        return list(FIXED_CHIPS) + extra + [('yours', 'Your channels')]

    def fill_channel_chips(self):
        keys = [key for key, _title in self.chips()]
        if self.channel_filter not in keys:
            self.channel_filter = 'all'
        self.channel_syncing = True
        try:
            self.channel_chips.remove_all()
            for key, title in self.chips():
                self.channel_chips.add(Adw.Toggle(label=title, name=key))
            self.channel_chips.set_active(keys.index(self.channel_filter))
            self.sort_drop.set_selected([key for key, _t in channel_browse.SORTS].index(self.channel_sort))
        finally:
            self.channel_syncing = False
        self.sync_channel_controls()

    def sync_channel_controls(self):
        connected = self.twitch['state'] in ('connected', 'attention')
        self.channel_status.set_visible(self.twitch['state'] in ('setup', 'curated'))
        self.category_button.set_visible(connected)
        self.follows_button.set_visible(self.channel_filter == 'yours')

    def channel_chip_changed(self, *_args):
        if self.channel_syncing:
            return
        keys = [key for key, _title in self.chips()]
        index = self.channel_chips.get_active()
        if 0 <= index < len(keys) and keys[index] != self.channel_filter:
            self.channel_filter = keys[index]
            self.twitch_more_problem = ''
            self.sync_channel_controls()
            self.refresh()

    def channel_sort_changed(self, *_args):
        if self.channel_syncing:
            return
        sorts = [key for key, _title in channel_browse.SORTS]
        index = self.sort_drop.get_selected()
        if 0 <= index < len(sorts) and sorts[index] != self.channel_sort:
            self.channel_sort = sorts[index]
            self.refresh()

    # ---- categories the listener adds -------------------------------------------------------

    def build_category_popover(self):
        popover = Gtk.Popover()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                       margin_start=10, margin_end=10)
        body.set_size_request(280, -1)
        self.category_entry = Gtk.SearchEntry(placeholder_text='Search Twitch categories')
        name_it(self.category_entry, 'Search Twitch categories', None)
        self.category_entry.connect('activate', self.search_category)
        body.append(self.category_entry)
        self.category_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.append(self.category_results)
        popover.set_child(body)
        popover.connect('show', lambda *_: self.show_saved_categories())
        self.category_popover = popover
        return popover

    def show_saved_categories(self):
        import twitch_channels
        while (child := self.category_results.get_first_child()) is not None:
            self.category_results.remove(child)
        saved = twitch_channels.saved_categories()
        if saved:
            self.category_results.append(label('Your categories', ['section-note']))
        for row in saved:
            line = Gtk.Box(spacing=6)
            line.append(label(row['name'], ellipsize=True))
            line.get_first_child().set_hexpand(True)
            remove = Gtk.Button(label='Remove')
            remove.add_css_class('flat')
            name_it(remove, 'Remove the category ' + row['name'], None)
            remove.connect('clicked', lambda _b, row=row: self.remove_category(row))
            line.append(remove)
            self.category_results.append(line)

    def search_category(self, *_args):
        query = self.category_entry.get_text().strip()
        if not query:
            return
        import twitch_channels
        while (child := self.category_results.get_first_child()) is not None:
            self.category_results.remove(child)
        self.category_results.append(label('Searching…', ['section-note']))
        run_async(lambda: twitch_channels.search_categories(query), self.categories_found,
                  lambda error: self.categories_found(None, error_text(error)), self.executor)

    def categories_found(self, rows, problem=''):
        if self.closed:
            return False
        while (child := self.category_results.get_first_child()) is not None:
            self.category_results.remove(child)
        if rows is None:
            self.category_results.append(label('Twitch could not search categories. ' + problem, ['section-note'], wrap=True))
        elif not rows:
            self.category_results.append(label('No category matched.', ['section-note']))
        for row in rows or []:
            button = Gtk.Button(label=row['name'])
            button.add_css_class('flat')
            button.get_child().set_xalign(0.)
            button.connect('clicked', lambda _b, row=row: self.add_category(row))
            self.category_results.append(button)
        return False

    def add_category(self, row):
        import twitch_channels
        self.category_popover.popdown()
        saved = twitch_channels.saved_categories()
        if row['id'] not in [item['id'] for item in saved]:
            try:
                twitch_channels.save_categories(saved + [row])
            except (OSError, ValueError) as problem:
                self.window.toast('Could not save the category: ' + error_text(problem))
                return
        self.channel_filter = 'cat:' + row['id']
        self.refresh_twitch(force=True)

    def remove_category(self, row):
        import twitch_channels
        try:
            twitch_channels.save_categories([item for item in twitch_channels.saved_categories() if item['id'] != row['id']])
        except (OSError, ValueError) as problem:
            self.window.toast('Could not remove the category: ' + error_text(problem))
            return
        self.category_popover.popdown()
        if self.channel_filter == 'cat:' + row['id']:
            self.channel_filter = 'all'
        self.refresh_twitch(force=True)

    # ---- what is shown ------------------------------------------------------------------------

    def twitch_applied(self, found):
        """A read finished (see twitch_read): keep each category's list and cursor, and the saved channels."""
        self.twitch_pages = found.get('pages') or {}
        self.twitch_yours = found.get('yours') or []
        self.twitch_more_loading = False
        self.fill_channel_chips()

    def live_index(self):
        """login -> the live channel, from every category read so far."""
        return {channel['login']: channel for page in self.twitch_pages.values() for channel in page['channels']}

    def yours_rows(self):
        live = self.live_index()
        connected = self.twitch['state'] in ('connected', 'attention')
        rows = []
        for item in self.twitch_yours:
            found = live.get(item['login'])
            if found is not None:
                rows.append(dict(found, curated=True))
            else:
                rows.append(dict(item, curated=True, live=False if connected and not self.twitch['message'] else item.get('live')))
        return channel_browse.arrange(rows, 'yours')

    def shown_channels(self):
        """The channels the chosen chip and sort show."""
        pick = self.channel_filter
        if pick == 'yours':
            return self.yours_rows()
        if pick == 'all':
            channels = channel_browse.interleave([page['channels'] for page in self.twitch_pages.values()])
            seen = {channel['login'] for channel in channels}
            channels += [dict(item) for item in self.twitch_yours if item['login'] not in seen]
        else:
            page = self.twitch_pages.get(pick)
            channels = list(page['channels']) if page else []
            if pick in STYLE_NAMES:      # saved channels filed under this category, live or not
                live = self.live_index()
                connected = self.twitch['state'] == 'connected'
                channels += [dict(item, live=False if connected else item.get('live')) for item in self.twitch_yours
                             if item.get('style') == pick and item['login'] not in live]
        return channel_browse.arrange(channels, self.channel_sort)

    def more_pages(self):
        """The pages "Show more" would read for the chip on screen."""
        if self.channel_filter in ('yours',):
            return []
        keys = list(self.twitch_pages) if self.channel_filter == 'all' else [self.channel_filter]
        return [key for key in keys if key in self.twitch_pages and self.twitch_pages[key].get('cursor')]

    def channel_rows(self, shelf=False):
        if shelf:
            channels = channel_browse.interleave([page['channels'] for page in self.twitch_pages.values()])
            seen = {channel['login'] for channel in channels}
            channels += [dict(item) for item in self.twitch_yours if item['login'] not in seen]
            if not self.twitch_pages:
                channels = [dict(item) for item in self.twitch_yours] or list(self.channels)
        else:
            channels = self.shown_channels()
            if not self.twitch_pages and not self.twitch_yours:
                channels = list(self.channels)
        rows = [self.channel_entry(item) for item in channels]
        if self.twitch['state'] in ('setup', 'attention'):
            rows.append(self.twitch_setup_entry())
        return rows

    def channel_note(self):
        """The line under the list when the chip on screen has nothing, or the general footnote."""
        if self.channel_filter == 'yours' and not self.twitch_yours:
            return 'You have no saved or followed channels yet. Review my follows adds some.'
        if (self.channel_filter in self.twitch_pages and not self.twitch_pages[self.channel_filter]['channels']
                and self.twitch['state'] == 'connected'):
            return 'Nobody is live in %s right now.' % self.twitch_pages[self.channel_filter]['name']
        return self.twitch_footnote()

    def render_channels_scope(self):
        rows = self.channel_rows()
        self.index_body.append(self.section('Live channels', rows, note=(
            self.channel_note() or ('No live channels yet.' if not rows else None))))
        if self.more_pages():
            box = Gtk.Box(halign=Gtk.Align.CENTER, spacing=8)
            more = Gtk.Button(label='Loading…' if self.twitch_more_loading else 'Show more')
            more.set_sensitive(not self.twitch_more_loading)
            more.connect('clicked', lambda *_: self.show_more_channels())
            box.append(more)
            self.index_body.append(box)
        if self.twitch_more_problem:
            self.index_body.append(label(self.twitch_more_problem, ['section-note'], wrap=True))

    def show_more_channels(self):
        pages = self.more_pages()
        if not pages or self.twitch_more_loading:
            return
        self.twitch_more_loading = True
        self.twitch_more_problem = ''
        want = {key: (self.twitch_pages[key]['style'], self.twitch_pages[key].get('category'),
                      self.twitch_pages[key]['cursor'], self.twitch_pages[key].get('game_id')) for key in pages}
        run_async(lambda: self.read_more(want), self.more_read,
                  lambda problem: self.more_read({'problem': error_text(problem), 'pages': {}}), self.executor)
        self.refresh()

    @staticmethod
    def read_more(want):
        import twitch_channels
        found, problem = {}, ''
        for key, (style, category, cursor, game_id) in want.items():
            try:
                result = twitch_channels.discover(style, curated=[], after=cursor, game_id=game_id, category=category)
            except (OSError, ValueError) as error:
                problem = error_text(error)
                continue
            if result.get('status') == 'unknown':
                problem = result.get('message') or problem
                continue
            found[key] = {'channels': [c for c in result.get('channels') or [] if c.get('live')],
                          'cursor': result.get('cursor')}
        return {'problem': problem, 'pages': found}

    def more_read(self, found):
        if self.closed:
            return False
        self.twitch_more_loading = False
        self.twitch_more_problem = ('Twitch could not load more channels. ' + found['problem']).strip() if found.get('problem') else ''
        pages = {}
        for key, page in self.twitch_pages.items():
            more = found['pages'].get(key)
            if more is None:
                pages[key] = page
                continue
            seen = {channel['login'] for channel in page['channels']}
            pages[key] = dict(page, channels=page['channels'] + [c for c in more['channels'] if c['login'] not in seen],
                              cursor=more['cursor'])
        self.twitch_pages = pages
        self.channels = channel_browse.interleave([page['channels'] for page in pages.values()]) + [
            item for item in self.channels if item.get('live') is not True and item['login'] not in self.live_index()]
        self.index_sig = None
        self.refresh()
        return False

    # ---- your channels -------------------------------------------------------------------------

    def channel_menu(self, item):
        login = item.get('login')
        if not login:
            return []
        yours = login in [row['login'] for row in self.twitch_yours]
        return [('Remove from your channels' if yours else 'Add to your channels', lambda: self.toggle_yours(item)),
                ('Hide this channel', lambda: self.hide_channel(item))]

    def toggle_yours(self, item):
        """Add the channel to Your channels (filed under ASMR when that is its category, else Music), or remove it."""
        import twitch_channels
        login = item['login']
        style = 'asmr' if str(item.get('category') or '').casefold() == 'asmr' else 'music'
        try:
            excluded = twitch_channels.excluded_channels()
            current = {key: twitch_channels.curated_channels(key) for key in STYLE_NAMES}
            if any(login in names for names in current.values()):
                for key, names in current.items():
                    if login in names:
                        twitch_channels.save_preferences(key, [n for n in names if n != login], excluded)
                done = 'Removed ' + (item.get('name') or login) + ' from your channels'
            else:
                twitch_channels.save_preferences(style, current[style] + [login], excluded)
                done = 'Added ' + (item.get('name') or login) + ' to your channels'
        except (OSError, ValueError) as problem:
            self.window.toast('Could not change your channels: ' + error_text(problem))
            return
        self.window.toast(done)
        self.refresh_twitch(force=True)

    def hide_channel(self, item):
        import twitch_channels
        login = item['login']
        try:
            excluded = twitch_channels.excluded_channels()
            twitch_channels.save_preferences('music', twitch_channels.curated_channels('music'),
                                             list(dict.fromkeys(excluded + [login])))
        except (OSError, ValueError) as problem:
            self.window.toast('Could not hide the channel: ' + error_text(problem))
            return
        self.window.toast('Hidden: ' + (item.get('name') or login), 'Undo', lambda: self.unhide_channel(login))
        self.refresh_twitch(force=True)

    def unhide_channel(self, login):
        import twitch_channels
        try:
            twitch_channels.save_preferences('music', twitch_channels.curated_channels('music'),
                                             [name for name in twitch_channels.excluded_channels() if name != login])
        except (OSError, ValueError) as problem:
            self.window.toast('Could not undo: ' + error_text(problem))
            return
        self.refresh_twitch(force=True)

    # ---- preview images ------------------------------------------------------------------------

    def channel_art(self, item):
        """The cached preview file of a live channel, starting its read when it has not begun. Only with a
        connected account: previews exist only in Twitch's own listings."""
        url = item.get('thumbnail') or ''
        if not url or self.twitch['state'] not in ('connected', 'attention'):
            return None
        known = self.thumbs.get(url)
        if known is None:
            self.thumbs[url] = ''
            run_async(lambda: fetch_thumbnail(url), lambda path, url=url: self.thumbnail_read(url, path),
                      lambda _error, url=url: self.thumbnail_read(url, None), self.executor)
        return known or None

    def thumbnail_read(self, url, path):
        if self.closed:
            return False
        if path and not self.thumbs.get(url):
            self.thumbs[url] = path
            self.thumbs_version += 1
            self.refresh()
        return False

    def channel_key(self):
        """What the channel view is drawn from besides the channels themselves (for the index signature)."""
        return (self.channel_filter, self.channel_sort, self.twitch_more_loading, self.twitch_more_problem,
                tuple((key, page.get('cursor'), len(page['channels'])) for key, page in self.twitch_pages.items()),
                self.thumbs_version, tuple(item['login'] for item in self.twitch_yours))

    def art_slot(self, path):
        """The row's picture: the stream preview once it is loaded, an avatar placeholder until then (or when
        the channel has none)."""
        avatar = Adw.Avatar(size=44, show_initials=False, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        avatar.set_icon_name('video-display-symbolic')
        avatar.set_hexpand(False)
        texture = texture_for(path) if path else None
        if texture is not None:
            avatar.set_custom_image(texture)
        return avatar
