"""The Listen destination: search, shelves, station detail and history.

It reuses the hub's data sources through their modules (the
listening_hub catalogue and favourites, World Radio search, the Jellyfin
library browse, Twitch channels, the controller's station and history
commands) and never re-implements a provider. Browsing, searching and opening
pages stay inert: only Tune in, Replay, the row menu's Play as ambience and
Show as scene, and a chosen link destination start anything.
"""
import concurrent.futures
import json
import sys
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

import listening_hub as hub
import next_links
import next_model
import next_station
import session_client
from next_listen_more import MORE_SCOPES, ServicesMixin
from next_widgets import FocalPicture, icon_button, label, name_it

SCOPES = (('all', 'All'), ('stations', 'Your stations'), ('radio', 'Radio'),
          ('library', 'Library'), ('channels', 'Live channels'), ('world', 'World radio'))
NETWORK_SCOPES = {'all': ('world',), 'world': ('world',), 'library': ('library',), 'channels': ('channels',),
                  'speakers': ('speakers',)}
DEBOUNCE_MS = 500
SHELF_LIMIT = 6
FULL_LIMIT = 20
REVIEW_PAGES = 50
FAVORITES_REFRESH_S = 3.

LISTEN_CSS = '''
.next .listen-title { font-size: 17px; font-weight: 600; color: #ffffff; }
.next .listen-heading { font-size: 22px; font-weight: 600; color: #ffffff; }
.next .section-note { font-size: 13px; color: rgba(255,255,255,.68); }
.next .row-name { font-size: 15px; font-weight: 600; color: #ffffff; }
.next .row-detail { font-size: 13px; color: rgba(255,255,255,.64); }
.next .listen-row { padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,.07); }
.next .station-card { background: rgba(255,255,255,.05); border-radius: 14px; padding: 12px; }
.next .station-card:hover { background: rgba(255,255,255,.09); }
.next .new-station { border: 1px dashed rgba(255,255,255,.24); border-radius: 14px; background: transparent;
  color: rgba(255,255,255,.82); padding: 12px; }
.next .new-station:hover { background: rgba(255,255,255,.05); }
.next .row-tile { min-width: 44px; min-height: 44px; border-radius: 10px; font-size: 14px; font-weight: 700; }
.next .card-tile { min-width: 56px; min-height: 56px; border-radius: 12px; font-size: 15px; font-weight: 700; }
.next .hero-tile { min-width: 96px; min-height: 96px; border-radius: 20px; font-size: 30px; font-weight: 700; }
.next .tint-0 { background: rgba(228,168,83,.16); color: #f2c98a; }
.next .tint-1 { background: rgba(169,214,203,.14); color: #a9d6cb; }
.next .tint-2 { background: rgba(185,212,242,.14); color: #b9d4f2; }
.next .tint-3 { background: rgba(197,165,223,.16); color: #d9c2ee; }
.next .tint-4 { background: rgba(244,167,167,.14); color: #f7c0c0; }
.next .tint-5 { background: rgba(255,255,255,.10); color: #ffffff; }
.next .station-name { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif;
  font-size: 42px; font-weight: 400; color: #ffffff; }
.next.compact .station-name { font-size: 30px; }
.next .mood-card, .next .panel-card { background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.06);
  border-radius: 14px; padding: 14px 18px; }
.next .mood-title { font-size: 14px; font-weight: 500; color: rgba(255,255,255,.88); }
.next .mood-hint { font-size: 12px; color: rgba(255,255,255,.62); }
.next .fallback-row { padding: 8px 0; border-top: 1px solid rgba(255,255,255,.07); }
.next .fallback-handle { color: rgba(255,255,255,.4); }
.next .fallback-drop { background: rgba(255,255,255,.10); border-radius: 10px; }
.next .now-playing { background: #24211d; border-top: 1px solid rgba(255,255,255,.08); padding: 10px 14px; }
.next .bar-title { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 19px;
  color: #ffffff; }
.next .bar-line { font-size: 13px; color: rgba(255,255,255,.72); }
.next .bar-tile { min-width: 52px; min-height: 52px; border-radius: 10px; font-size: 15px; font-weight: 700; }
.next .bar-play { background: #ffffff; color: #141416; border-radius: 22px; min-width: 44px; min-height: 44px;
  padding: 0; }
.next .bar-play:hover { background: #e9e9ee; }
.next .bar-volume trough { min-height: 6px; background: rgba(255,255,255,.28); }
.next .bar-volume highlight { background: #ffffff; }
.next .error { color: #ffb4a6; }
'''

_provider = None


def install_css(display):
    global _provider
    if _provider is None:
        _provider = Gtk.CssProvider()
        _provider.load_from_string(LISTEN_CSS)
        Gtk.StyleContext.add_provider_for_display(display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return _provider


def _clear(box):
    child = box.get_first_child()
    while child:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def _looks_like_url(value):
    return isinstance(value, str) and value.strip().lower().startswith(('http://', 'https://'))


class ListenPage(ServicesMixin):
    """Search first, then shelves; station detail and history are pushed pages."""

    def __init__(self, window):
        self.window = window
        self.snapshot = {}
        self.closed = False
        self.query = ''
        self.scope = 'all'
        self.favorites = set()
        self.favorites_at = 0.
        self.library = []
        self.library_state = 'idle'
        self.library_cancel = None
        self.channels = []
        self.channels_loaded = False
        self.remote = {}
        self.init_services()
        self.searching = False
        self.request_id = 0
        self.cancel_event = None
        self.debounce = None
        self.index_sig = None
        self.station = None
        self.station_id = None
        self.station_sig = None
        self.fallback_sig = None
        self.pending_station = None
        self.pending_timer = None
        self.focus_key = None
        self.focus_targets = {}
        self.reviewed = {}
        self.review_wanted = set()
        self.history_sig = None
        self.syncing = False
        self.updating_volume = False
        self.volume_timer = None
        self.launch_link = next_links.launch_link(getattr(sys, 'argv', []))
        self.launch_queued = False
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix='listen')
        self.save_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='listen-save')
        install_css(Gdk.Display.get_default())
        self.widget = self.build()
        self.widget.add_tick_callback(self.adapt_geometry)
        self.install_shortcuts()
        self.install_drop_targets()

    # ---- structure ---------------------------------------------------------

    def surface(self):
        return self.widget

    def build(self):
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=200)
        self.stack.set_vexpand(True)
        self.stack.add_named(self.build_index(), 'index')
        self.stack.add_named(self.build_station_page(), 'station')
        self.stack.add_named(self.build_history_page(), 'history')
        view = Adw.ToolbarView()
        view.set_bottom_bar_style(Adw.ToolbarStyle.FLAT)
        view.set_content(self.stack)
        self.bar = self.build_bar()
        view.add_bottom_bar(self.bar)
        return view

    def build_index(self):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900,
                          margin_start=20, margin_end=20, margin_top=18, margin_bottom=28)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.search = Gtk.SearchEntry(
            placeholder_text='Search stations, radio, your library and live channels — or paste a link')
        self.search.update_property([Gtk.AccessibleProperty.LABEL], ['Search stations and links'])
        self.search.connect('search-changed', self.query_changed)
        self.search.connect('activate', self.query_submitted)
        column.append(self.search)
        self.scopes = Adw.ToggleGroup()
        self.scopes.add_css_class('listen-scopes')
        self.scopes.set_halign(Gtk.Align.START)
        try:
            self.scopes.set_homogeneous(False)
        except AttributeError:
            pass
        for key, title in SCOPES:
            self.scopes.add(Adw.Toggle(label=title, name=key))
        self.scopes.set_active(0)
        self.scopes.connect('notify::active', self.scope_changed)
        wrap = Adw.WrapBox(child_spacing=8, line_spacing=8)
        wrap.append(self.scopes)
        wrap.append(self.build_more_scopes())
        column.append(wrap)
        column.append(self.build_scope_controls())
        self.index_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        column.append(self.index_body)
        clamp.set_child(column)
        scroller.set_child(clamp)
        self.index_scroller = scroller
        return scroller

    def build_station_page(self):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900,
                          margin_start=20, margin_end=20, margin_top=12, margin_bottom=28)
        self.station_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(self.station_holder)
        scroller.set_child(clamp)
        self.station_scroller = scroller
        self.build_station_widgets()
        return scroller

    def build_station_widgets(self):
        self.station_back = Gtk.Button(label='‹ Listen')
        self.station_back.add_css_class('flat')
        self.station_back.set_halign(Gtk.Align.START)
        self.station_back.connect('clicked', lambda *_: self.show_index())
        self.station_back.update_property([Gtk.AccessibleProperty.LABEL], ['Back to Listen'])
        self.station_holder.append(self.station_back)

        self.station_main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=36)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        left.set_size_request(280, -1)
        left.set_valign(Gtk.Align.START)
        self.station_main.append(left)
        self.station_tile = label('', ['hero-tile'])
        self.station_tile.set_size_request(96, 96)
        self.station_tile.set_xalign(.5)
        self.station_tile.set_yalign(.5)
        self.station_tile.set_halign(Gtk.Align.START)
        left.append(self.station_tile)
        identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.station_name = label('', ['station-name'], wrap=True)
        self.station_origin = label('', ['row-detail'], wrap=True)
        identity.append(self.station_name)
        identity.append(self.station_origin)
        self.station_rename = Gtk.Button(label='Rename')
        self.station_rename.add_css_class('flat')
        self.station_rename.set_halign(Gtk.Align.START)
        name_it(self.station_rename, 'Rename station', '')
        self.station_rename.connect('clicked', lambda *_: self.rename_dialog())
        identity.append(self.station_rename)
        left.append(identity)
        actions = Gtk.Box(spacing=10)
        self.station_tune = Gtk.Button(label='Tune in')
        self.station_tune.add_css_class('suggested-action')
        self.station_tune.add_css_class('pill')
        self.station_tune.connect('clicked', lambda *_: self.station and self.tune_station(self.station))
        self.station_ambience = Gtk.Button(label='Play as ambience')
        self.station_ambience.add_css_class('pill')
        self.station_ambience.connect('clicked', lambda *_: self.station and self.station_ambience_action(self.station))
        actions.append(self.station_tune)
        actions.append(self.station_ambience)
        left.append(actions)

        learned = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        learned.add_css_class('panel-card')
        learned.append(label('What it has learned', ['mood-title']))
        self.learned_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        learned.append(self.learned_box)
        self.forget_button = Gtk.Button(label='Forget what it learned…')
        self.forget_button.add_css_class('flat')
        self.forget_button.set_halign(Gtk.Align.START)
        self.forget_button.connect('clicked', lambda *_: self.forget_dialog())
        learned.append(self.forget_button)
        left.append(learned)
        self.learned_card = learned

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        right.set_hexpand(True)
        self.station_main.append(right)

        moods = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        moods.add_css_class('mood-card')
        self.moods_card = moods
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        head.append(label('Mood', ['listen-title']))
        note = label('Changes apply from the next track', ['section-note'], wrap=True)
        note.set_hexpand(True)
        note.set_halign(Gtk.Align.END)
        head.append(note)
        moods.append(head)
        self.mood_rows = {}
        self.mood_groups = {}
        self.mood_hints = {}
        for mood in next_station.MOODS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            title = label(mood['title'], ['mood-title'])
            title.set_size_request(130, -1)
            control = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            control.set_hexpand(True)
            group = Adw.ToggleGroup()
            group.add_css_class('mood')
            name_it(group, mood['title'], '')
            group.set_halign(Gtk.Align.START)
            group.set_homogeneous(False)
            for choice, _value in mood['choices']:
                group.add(Adw.Toggle(label=choice))
            group.connect('notify::active', lambda _group, _pspec, key=mood['key']: self.mood_changed(key))
            hint = label('', ['mood-hint'], wrap=True)
            hint.set_visible(False)
            control.append(group)
            control.append(hint)
            row.append(title)
            row.append(control)
            moods.append(row)
            self.mood_rows[mood['key']] = row
            self.mood_groups[mood['key']] = group
            self.mood_hints[mood['key']] = hint

        arc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        arc_title = label('Over time', ['mood-title'])
        arc_title.set_size_request(130, -1)
        arc_control = Gtk.FlowBox(min_children_per_line=1, max_children_per_line=2, column_spacing=18,
                                  row_spacing=8, selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.CENTER)
        arc_control.set_hexpand(True)
        self.arc_group = Adw.ToggleGroup()
        name_it(self.arc_group, 'Over time', '')
        self.arc_group.set_halign(Gtk.Align.START)
        self.arc_group.set_homogeneous(False)
        for choice, _value in next_station.ARCS:
            self.arc_group.add(Adw.Toggle(label=choice))
        self.arc_group.connect('notify::active', lambda *_a: self.arc_changed())
        arc_control.append(Gtk.FlowBoxChild(child=self.arc_group))
        self.hold_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.hold_switch = Gtk.Switch()
        self.hold_switch.set_valign(Gtk.Align.CENTER)
        name_it(self.hold_switch, 'Hold this mood')
        self.hold_switch.connect('notify::active', lambda *_a: self.hold_changed())
        self.hold_label = label('Hold this mood', ['row-detail'])
        self.hold_box.append(self.hold_switch)
        self.hold_box.append(self.hold_label)
        arc_control.append(Gtk.FlowBoxChild(child=self.hold_box))
        arc_row.append(arc_title)
        arc_row.append(arc_control)
        moods.append(arc_row)
        self.arc_row = arc_row

        end_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        end_title = label('When it runs out', ['mood-title'])
        end_title.set_size_request(130, -1)
        self.end_group = Adw.ToggleGroup()
        name_it(self.end_group, 'When it runs out', '')
        self.end_group.set_halign(Gtk.Align.START)
        self.end_group.set_homogeneous(False)
        for choice, _value in next_station.ENDS:
            self.end_group.add(Adw.Toggle(label=choice))
        self.end_group.connect('notify::active', lambda *_a: self.end_changed())
        end_row.append(end_title)
        end_row.append(self.end_group)
        moods.append(end_row)
        self.end_row = end_row
        right.append(moods)

        fallbacks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        fallbacks.add_css_class('mood-card')
        fallbacks.append(label('If it can’t play, try', ['listen-title']))
        self.fallbacks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        fallbacks.append(self.fallbacks_box)
        self.add_fallback_button = Gtk.MenuButton(label='Add a fallback')
        self.add_fallback_button.add_css_class('flat')
        self.add_fallback_button.set_halign(Gtk.Align.START)
        self.add_fallback_popover = Gtk.Popover()
        self.add_fallback_popover.connect('show', lambda *_: self.fill_fallback_choices())
        self.add_fallback_button.set_popover(self.add_fallback_popover)
        fallbacks.append(self.add_fallback_button)
        right.append(fallbacks)

        self.upcoming = Gtk.Expander(label='Show what’s up next')
        self.upcoming_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.upcoming_box.set_margin_start(4)
        self.upcoming_box.set_margin_top(6)
        self.upcoming.set_child(self.upcoming_box)
        self.upcoming.connect('notify::expanded', lambda *_: self.render_upcoming())
        right.append(self.upcoming)

        self.station_breakpoints = None
        self.station_holder.append(self.station_main)
        self.station_compact = None

    def adapt_geometry(self, widget, _clock):
        """Stack the station page's two columns and its mood rows below 1000 px."""
        compact = widget.get_width() < 1000
        if compact == self.station_compact:
            return GLib.SOURCE_CONTINUE
        self.station_compact = compact
        orientation = Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL
        self.station_main.set_orientation(orientation)
        for row in self.mood_rows.values():
            row.set_orientation(orientation)
        self.arc_row.set_orientation(orientation)
        self.end_row.set_orientation(orientation)
        self.hold_box.set_orientation(orientation)
        return GLib.SOURCE_CONTINUE

    def build_history_page(self):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900,
                          margin_start=20, margin_end=20, margin_top=12, margin_bottom=28)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        back = Gtk.Button(label='‹ Listen')
        back.add_css_class('flat')
        back.set_halign(Gtk.Align.START)
        back.connect('clicked', lambda *_: self.show_index())
        back.update_property([Gtk.AccessibleProperty.LABEL], ['Back to Listen'])
        self.history_back = back
        column.append(back)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        head.append(label('History', ['listen-heading']))
        column.append(head)
        self.history_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        column.append(self.history_body)
        clamp.set_child(column)
        scroller.set_child(clamp)
        return scroller

    def build_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        bar.add_css_class('now-playing')
        bar.set_visible(False)
        open_button = Gtk.Button()
        open_button.add_css_class('flat')
        open_button.set_hexpand(True)
        open_button.connect('clicked', lambda *_: self.window.show_now())
        open_button.update_property([Gtk.AccessibleProperty.LABEL], ['Open the room'])
        inner = Gtk.Box(spacing=14)
        self.bar_art = Gtk.Overlay()
        self.bar_art.set_size_request(52, 52)
        self.bar_art.set_hexpand(False)
        self.bar_picture = FocalPicture()
        self.bar_picture.set_hexpand(False)
        self.bar_picture.set_vexpand(False)
        self.bar_art.set_child(self.bar_picture)
        self.bar_initials = label('', ['bar-tile'])
        self.bar_initials.set_size_request(52, 52)
        self.bar_initials.set_xalign(.5)
        self.bar_initials.set_yalign(.5)
        self.bar_art.add_overlay(self.bar_initials)
        inner.append(self.bar_art)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_valign(Gtk.Align.CENTER)
        text.set_hexpand(True)
        self.bar_title = label('', ['bar-title'], ellipsize=True)
        self.bar_line = label('', ['bar-line'], ellipsize=True)
        text.append(self.bar_title)
        text.append(self.bar_line)
        inner.append(text)
        open_button.set_child(inner)
        bar.append(open_button)
        self.bar_play = Gtk.Button(icon_name='media-playback-pause-symbolic')
        self.bar_play.add_css_class('bar-play')
        name_it(self.bar_play, 'Pause the room')
        self.bar_play.connect('clicked', lambda *_: self.window.toggle_room())
        bar.append(self.bar_play)
        volume = Gtk.Box(spacing=8)
        volume.set_valign(Gtk.Align.CENTER)
        volume.append(Gtk.Image(icon_name='audio-volume-high-symbolic'))
        self.bar_volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.bar_volume.set_size_request(140, -1)
        self.bar_volume.add_css_class('bar-volume')
        name_it(self.bar_volume, 'Volume')
        self.bar_volume.connect('value-changed', self.bar_volume_changed)
        volume.append(self.bar_volume)
        bar.append(volume)
        self.bar_expand = icon_button('go-up-symbolic', 'Open the room', self.window.show_now)
        self.bar_expand.add_css_class('flat')
        bar.append(self.bar_expand)
        return bar

    # ---- keyboard and drops ------------------------------------------------

    def install_shortcuts(self):
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.GLOBAL)
        for trigger, callback in (('<Control>f', self.shortcut_search), ('<Control>l', self.shortcut_link),
                                  ('<Control>v', self.shortcut_paste)):
            controller.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                                     Gtk.CallbackAction.new(callback)))
        self.window.add_controller(controller)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.typed)
        self.widget.add_controller(keys)

    def install_drop_targets(self):
        text_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        text_target.connect('drop', self.dropped_text)
        self.window.add_controller(text_target)
        file_target = Gtk.DropTarget.new(Gdk.FileList.__gtype__, Gdk.DragAction.COPY)
        file_target.connect('drop', self.dropped_files)
        self.window.add_controller(file_target)

    def editable_focus(self):
        focus = self.window.get_focus()
        return isinstance(focus, (Gtk.Editable, Gtk.Entry)) or focus is self.search

    def shortcut_search(self, *_args):
        if self.window.get_visible_dialog() is not None:
            return False
        if not self.on_listen():
            self.window.show_listen()
        self.show_index(focus_search=True)
        return True

    def shortcut_link(self, *_args):
        if self.window.get_visible_dialog() is not None:
            return False
        self.add_link()
        return True

    def shortcut_paste(self, *_args):
        if self.window.get_visible_dialog() is not None or self.editable_focus():
            return False
        self.read_clipboard(self.pasted)
        return True

    def typed(self, _controller, keyval, _keycode, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if self.stack.get_visible_child_name() != 'index' or self.window.get_visible_dialog() is not None:
            if keyval == Gdk.KEY_Escape and self.stack.get_visible_child_name() != 'index':
                self.show_index()
                return True
            return False
        if keyval == Gdk.KEY_Escape and self.query:
            self.search.set_text('')
            return True
        if self.editable_focus():
            return False
        codepoint = Gdk.keyval_to_unicode(keyval)
        if not codepoint or keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_ISO_Enter, Gdk.KEY_Tab,
                                       Gdk.KEY_BackSpace, Gdk.KEY_Delete):
            return False
        character = chr(codepoint)
        if not character.isprintable() or character.isspace():
            return False
        position = self.search.get_position()
        text = self.search.get_text()
        self.search.grab_focus()
        self.search.set_text(text[:position] + character + text[position:])
        self.search.set_position(position + 1)
        return True

    def read_clipboard(self, callback):
        display = Gdk.Display.get_default()
        if display is None:
            return

        def finished(source, result):
            try:
                value = source.read_text_finish(result)
            except GLib.Error:
                value = ''
            callback((value or '').strip())
        display.get_clipboard().read_text_async(None, finished)

    def pasted(self, text):
        if not text:
            self.window.toast('Copy a public link first')
            return
        try:
            preview = next_links.descriptor(text)
        except Exception:
            self.window.toast('That is not a public link')
            return
        self.open_preview(preview)

    def dropped_text(self, _target, value, _x, _y):
        try:
            preview = next_links.descriptor(value)
        except Exception:
            return False
        self.open_preview(preview)
        return True

    def dropped_files(self, _target, files, _x, _y):
        items = files.get_files() if hasattr(files, 'get_files') else list(files or [])
        for item in items:
            uri = item.get_uri() if hasattr(item, 'get_uri') else ''
            if _looks_like_url(uri):
                return self.dropped_text(None, uri, 0, 0)
        return False

    # ---- search ------------------------------------------------------------

    def scope_key(self, group=None):
        """The scope chosen in the group that just changed (the other group is cleared by scope_changed)."""
        if group is self.more_scopes:
            index = self.more_scopes.get_active()
            return MORE_SCOPES[index][0] if 0 <= index < len(MORE_SCOPES) else None
        index = self.scopes.get_active()
        return SCOPES[index][0] if 0 <= index < len(SCOPES) else None

    def query_changed(self, *_args):
        text = self.search.get_text().strip()
        if text == self.query:
            return
        self.query = text
        self.schedule_search()
        self.refresh()

    def query_submitted(self, *_args):
        self.cancel_debounce()
        self.start_search()

    def scope_changed(self, group=None, *_args):
        if self.scope_syncing:
            return
        scope = self.scope_key(group)
        if scope is None:
            return
        other = self.scopes if group is self.more_scopes else self.more_scopes
        self.scope_syncing = True
        other.set_active(Gtk.INVALID_LIST_POSITION)
        self.scope_syncing = False
        if scope == self.scope:
            return
        self.scope = scope
        if scope not in NETWORK_SCOPES:
            self.stop_search()
        self.scope_entered()
        self.schedule_search()
        self.refresh()

    def schedule_search(self):
        self.cancel_debounce()
        if self.query and self.scope in NETWORK_SCOPES and not _looks_like_url(self.query):
            self.debounce = GLib.timeout_add(DEBOUNCE_MS, self.debounce_fired)
        else:
            self.stop_search()
            self.remote = {}

    def cancel_debounce(self):
        if self.debounce:
            GLib.source_remove(self.debounce)
            self.debounce = None

    def debounce_fired(self):
        self.debounce = None
        self.start_search()
        return False

    def start_search(self):
        if self.closed or not self.query:
            return
        self.stop_search()
        if _looks_like_url(self.query):
            return  # a link may carry tokens; it is never sent to a provider as a query
        self.request_id += 1
        request = self.request_id
        event = threading.Event()
        self.cancel_event = event
        scopes = NETWORK_SCOPES.get(self.scope, ())
        if not scopes:
            return
        self.searching = True
        query = self.query
        searches = {'world': self.search_world, 'library': self.search_library, 'channels': self.search_channels,
                    'speakers': self.search_speakers}
        for scope in scopes:
            future = self.executor.submit(searches[scope], query, event)
            future.add_done_callback(lambda done, name=scope: self.search_finished(request, name, query, done))
        self.refresh()

    def stop_search(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.cancel_event = None
        self.request_id += 1
        self.searching = False

    def cancel_search(self):
        """The listener asked the running search to stop; its answer is ignored."""
        self.stop_search()
        self.refresh()

    def search_world(self, query, _cancel):
        rows = hub.search_radio(query)
        return [item for item in rows if isinstance(item, dict) and item.get('kind') == 'radio']

    def search_library(self, query, cancel):
        return self.browse_kind(self.kind, query, cancel)

    def search_channels(self, query, _cancel):
        import channel_browse
        import twitch_channels
        found, problem = {}, None
        for style in ('music', 'asmr'):
            try:
                result = twitch_channels.discover(style)
            except (OSError, ValueError) as error:
                problem = error
                continue
            for channel in result.get('channels') or []:
                found.setdefault(channel.get('login') or channel.get('name'), channel)
        if not found and problem is not None:
            raise problem
        return channel_browse.project(list(found.values()), query)

    def search_finished(self, request, scope, query, future):
        try:
            rows, error = future.result(), None
        except Exception as problem:
            rows, error = [], str(problem) or type(problem).__name__
        GLib.idle_add(self.apply_search, request, scope, query, rows, error)

    def apply_search(self, request, scope, query, rows, error):
        if self.closed or request != self.request_id:
            return False
        self.remote[scope] = {'query': query, 'rows': rows or [], 'error': bool(error)}
        self.searching = False
        self.refresh()
        return False

    # ---- polling data ------------------------------------------------------

    def update(self, snapshot):
        self.snapshot = snapshot or {}
        self.refresh_favorites()
        self.ensure_static_loads()
        self.sync_bar(self.snapshot)
        self.refresh()
        self.refresh_station()
        self.refresh_history()
        if self.launch_link and not self.launch_queued:
            self.launch_queued = True
            GLib.idle_add(self.open_launch_link)

    def refresh_favorites(self):
        if time.monotonic() - self.favorites_at < FAVORITES_REFRESH_S:
            return
        self.favorites_at = time.monotonic()
        try:
            self.favorites = {str(item.get('url')) for item in hub.favorites() if isinstance(item, dict)}
        except (OSError, ValueError):
            self.favorites = set()

    def ensure_static_loads(self):
        if not self.channels_loaded:
            self.channels_loaded = True
            self.channels = self.load_channels()
        if self.library_state == 'idle' and self.widget.get_mapped():
            self.library_state = 'loading' if self.library_configured() else 'unavailable'
            if self.library_state == 'loading':
                self.load_library()
            else:
                self.refresh()

    @staticmethod
    def library_configured():
        try:
            import online_services
            return bool(online_services.config()['jellyfin'].get('user_id'))
        except (ImportError, KeyError, OSError, ValueError):
            return False

    def load_library(self):
        cancel = threading.Event()
        self.library_cancel = cancel
        future = self.executor.submit(self.browse_library, cancel)
        future.add_done_callback(self.library_finished)
        self.refresh()

    @staticmethod
    def browse_library(cancel):
        import online_services
        return online_services.browse_jellyfin('', kind='albums', cancel_event=cancel)

    @staticmethod
    def browse_kind(kind, query, cancel):
        """One Jellyfin listing of the chosen kind (a read)."""
        import online_services
        if kind == 'playlists':
            return online_services.browse_jellyfin(query, kind='playlists', cancel_event=cancel)
        if kind == 'recent':
            return online_services.browse_jellyfin(query, kind='recent', cancel_event=cancel)
        if kind == 'tracks':
            return online_services.browse_jellyfin(query, kind='tracks', cancel_event=cancel)
        return online_services.browse_jellyfin(query, kind='albums', cancel_event=cancel)

    def library_finished(self, future):
        try:
            items, error = future.result(), None
        except Exception as problem:
            items, error = [], str(problem) or type(problem).__name__
        GLib.idle_add(self.apply_library, items, error)

    def apply_library(self, items, error):
        if self.closed:
            return False
        self.library = [item for item in items or [] if isinstance(item, dict)]
        self.library_state = 'failed' if error else ('ready' if self.library else 'empty')
        self.refresh()
        return False

    @staticmethod
    def load_channels():
        try:
            import twitch_channels
            excluded = {str(name).lower() for name in twitch_channels.excluded_channels()}
            channels = []
            for style in ('music', 'asmr'):
                for login in twitch_channels.curated_channels(style):
                    if login.lower() in excluded or any(item['login'] == login for item in channels):
                        continue
                    channels.append({'login': login, 'name': login,
                                     'url': 'https://www.twitch.tv/' + login, 'live': None})
            return channels
        except (ImportError, OSError, ValueError):
            return []

    # ---- now-playing bar ---------------------------------------------------

    def sync_bar(self, snapshot):
        active = snapshot.get('active')
        self.bar.set_visible(bool(active))
        if not active:
            return
        info = next_model.now_model(snapshot)
        if info is None:
            return
        # The same naming rule listening-roomctl uses: the station that is
        # playing, else what the music layer reports, else the room's own name.
        station = (snapshot.get('station') or {}).get('definition') or {}
        music = (snapshot.get('layers') or {}).get('music') or {}
        name = (station.get('name') or (info['name'] if active.get('room_id') else '')
                or music.get('station') or music.get('name') or info['name'])
        self.bar_title.set_text(name)
        line = info['line'] + (' · ' + info['detail'] if info['detail'] else '')
        self.bar_line.set_text(line)
        playing = info['state'] == 'playing'
        self.bar_play.set_icon_name('media-playback-pause-symbolic' if playing else 'media-playback-start-symbolic')
        name_it(self.bar_play, 'Pause the room' if playing else 'Play the room')
        poster = active.get('poster_path')
        texture = None
        if poster:
            try:
                texture = Gdk.Texture.new_from_filename(poster)
            except GLib.Error:
                texture = None
        self.bar_picture.set_paintable(texture)
        self.bar_initials.set_text(next_station.initials(name))
        self.bar_initials.set_visible(texture is None)
        for index in range(6):
            self.bar_art.remove_css_class('tint-%d' % index)
        self.bar_art.add_css_class('tint-%d' % next_station.tint_index(name))
        self.updating_volume = True
        if self.volume_timer is None:
            self.bar_volume.set_value(float(info['room_volume']))
        self.updating_volume = False

    def bar_volume_changed(self, scale):
        if self.updating_volume:
            return
        if self.volume_timer:
            GLib.source_remove(self.volume_timer)

        def send():
            self.volume_timer = None
            self.window.command('room_volume', {'value': round(scale.get_value(), 1)}, quiet=True)
            return False
        self.volume_timer = GLib.timeout_add(180, send)

    # ---- index -------------------------------------------------------------

    def index_signature(self):
        snapshot = self.snapshot
        stations = snapshot.get('stations') or []
        history = snapshot.get('station_recent') or []
        active = snapshot.get('active') or {}
        return json.dumps([
            [[station.get('id'), station.get('name'), next_station.descriptor(station),
              json.dumps(station.get('preferences') or {}, sort_keys=True)] for station in stations],
            [[entry.get('id'), len(entry.get('tracks') or []), (entry.get('station') or {}).get('name')]
             for entry in history if isinstance(entry, dict)],
            [[item.get('id'), item.get('name'), item.get('group')] for item in self.window.radio],
            sorted(self.favorites), self.library_state, len(self.library),
            [item.get('login') for item in self.channels],
            self.query, self.scope, self.searching, self.services_signature(),
            {key: [value.get('query'), len(value.get('rows') or []), value.get('error')]
             for key, value in self.remote.items()},
            active.get('name'), bool(active),
        ], sort_keys=True, default=str)

    def refresh(self):
        if self.closed:
            return
        signature = self.index_signature()
        if signature == self.index_sig:
            return
        self.index_sig = signature
        self.render_index()

    def render_index(self):
        _clear(self.index_body)
        if self.scope == 'speakers':
            self.render_speakers()
        elif self.scope == 'remembered':
            self.render_remembered()
        elif self.query:
            self.render_results()
        elif self.scope == 'all':
            self.render_shelves()
        else:
            self.render_scope(self.scope)

    # ---- shelves -----------------------------------------------------------

    def render_shelves(self):
        snapshot = self.snapshot
        self.index_body.append(self.stations_shelf(snapshot.get('stations') or []))
        history = next_station.history_rows(snapshot.get('station_recent') or [], snapshot.get('now') or time.time())
        if history:
            self.index_body.append(self.section('Recently played', [self.history_entry(row) for row in history[:4]],
                                                trailing=self.link_button('History', self.show_history)))
        radio = self.radio_entries()
        if radio:
            self.index_body.append(self.section('Radio', radio[:SHELF_LIMIT]))
        library = self.library_entries()
        if library or self.library_state in ('loading', 'failed', 'empty'):
            self.index_body.append(self.section(
                'Library', library[:SHELF_LIMIT],
                note={'loading': 'Loading the library…', 'failed': 'The library could not be read.',
                      'empty': 'No albums in the library yet.'}.get(self.library_state)))
        channels = self.channel_entries()
        if channels:
            self.index_body.append(self.section('Live channels', channels[:SHELF_LIMIT]))
        elsewhere = self.elsewhere_entries()
        if elsewhere:
            expander = Gtk.Expander(label='Open elsewhere')
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            body.set_margin_top(4)
            for entry in elsewhere[:FULL_LIMIT]:
                body.append(self.row(entry))
            expander.set_child(body)
            self.index_body.append(expander)

    def render_scope(self, scope):
        if scope == 'stations':
            rows = self.station_entries()
            self.index_body.append(self.section('Your stations', rows,
                                                note=None if rows else 'Save a station from Radio or Library.'))
        elif scope == 'radio':
            rows = self.radio_entries()[:FULL_LIMIT]
            self.index_body.append(self.section('Radio', rows,
                                                note=None if rows else 'These stations come with Listening Room.'))
        elif scope == 'library':
            if self.opened:
                self.render_opened()
            elif self.kind == 'albums' and self.library_state == 'loading':
                self.index_body.append(self.section('Library', [], note='Loading the library…'))
            elif self.kind == 'albums' and not self.library:
                self.index_body.append(self.section('Library', [], note='The library is not available yet.'))
            else:
                rows = self.kind_entries()[:FULL_LIMIT]
                self.index_body.append(self.section('Library', rows, note=None if rows else self.kind_note()))
        elif scope == 'channels':
            rows = [self.channel_entry(item) for item in self.channels]
            self.index_body.append(self.section('Live channels', rows, note=(
                'No live channels yet.' if not rows else None)))
        elif scope == 'world':
            rows = self.recent_world()
            self.index_body.append(self.section('World radio', rows, note=(
                'Search by station name, country:FI, language:Finnish or tag:jazz.' if not rows else None)))
        else:
            self.render_shelves()

    def recent_world(self):
        cached = hub.load_json(hub.STATE / 'search-cache.json', [])
        if not isinstance(cached, list):
            return []
        return [self.world_entry(item) for item in cached[:FULL_LIMIT]
                if isinstance(item, dict) and item.get('kind') == 'radio']

    def render_results(self):
        link = self.link_entry()
        groups = []
        if link is not None:
            groups.append(('Link', [link], None))
        scope = self.scope
        if scope in ('all', 'stations'):
            groups.append(('Your stations', self.matching(self.station_entries()), None))
        if scope in ('all', 'radio'):
            groups.append(('Radio', self.matching(self.radio_entries()), None))
        if scope in ('all', 'library'):
            note = ('Loading the library…' if self.library_state == 'loading' else
                    'The library is not available yet.' if scope == 'library' and not self.library else None)
            remote = self.remote_rows('library')
            base = self.kind_entries() if scope == 'library' else self.library_entries()
            rows = self.merged(self.matching(base), [self.library_entries_for(remote)])
            groups.append(('Library', rows, note))
        if scope in ('all', 'channels'):
            remote = self.remote_rows('channels')
            rows = self.merged(self.matching([self.channel_entry(item) for item in self.channels]),
                               [[self.channel_entry(item) for item in remote if isinstance(item, dict)]])
            groups.append(('Live channels', rows, None))
        if scope in ('all', 'world'):
            remote = self.remote.get('world') or {}
            fresh = remote.get('query') == self.query
            rows = self.matching([self.world_entry(item) for item in remote.get('rows') or []]) if fresh else []
            note = ('Searching World radio…' if self.searching else
                    'World radio couldn’t be reached.' if fresh and remote.get('error') else None)
            groups.append(('World radio', rows, note))
        shown = 0
        for title, rows, note in groups:
            if not rows and not note:
                continue
            trailing = self.link_button('Stop', self.cancel_search) \
                if self.searching and title == 'World radio' else None
            self.index_body.append(self.section(title, rows, note=note, trailing=trailing))
            shown += len(rows)
        if not shown and not self.searching:
            self.index_body.append(self.section('Nothing matched', [],
                                                note='Try another word, scope or a public link.'))

    def remote_rows(self, scope):
        """The provider's rows for the query on screen; stale or failed answers show nothing."""
        remote = self.remote.get(scope) or {}
        if remote.get('query') != self.query or remote.get('error'):
            return []
        return [item for item in remote.get('rows') or [] if isinstance(item, dict)]

    def library_entries_for(self, items):
        saved, self.library = self.library, items
        try:
            return self.library_entries()
        finally:
            self.library = saved

    @staticmethod
    def entry_key(entry):
        item = entry.get('item') or {}
        return (entry.get('kind'), str(item.get('provider') or ''), str(item.get('id') or entry.get('name') or '').casefold())

    def merged(self, entries, more):
        """Entries followed by the extra groups, each item shown once."""
        seen, found = set(), []
        for entry in list(entries) + [e for group in more for e in group]:
            key = self.entry_key(entry)
            if key not in seen:
                seen.add(key)
                found.append(entry)
        return found

    def matching(self, entries):
        query = self.query.casefold()
        if not query:
            return entries
        found = []
        for entry in entries:
            haystack = ' '.join([entry.get('name') or '', entry.get('detail') or '', entry.get('keywords') or ''])
            if query in haystack.casefold():
                found.append(entry)
        return found

    def stations_shelf(self, stations):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        head.append(self.section_title('Your stations'))
        box.append(head)
        grid = Gtk.FlowBox(min_children_per_line=2, max_children_per_line=4, homogeneous=True,
                           column_spacing=14, row_spacing=14, selection_mode=Gtk.SelectionMode.NONE)
        for station in stations:
            if isinstance(station, dict):
                child = Gtk.FlowBoxChild(child=self.station_card(station))
                grid.append(child)
        grid.append(Gtk.FlowBoxChild(child=self.new_station_button()))
        box.append(grid)
        if not stations:
            box.append(label('No saved stations yet.', ['section-note']))
        return box

    def station_card(self, station):
        card = Gtk.Box(spacing=12)
        card.add_css_class('station-card')
        card.set_size_request(240, -1)
        open_button = Gtk.Button()
        open_button.add_css_class('flat')
        open_button.set_hexpand(True)
        inner = Gtk.Box(spacing=12)
        inner.append(self.tile(next_station.name(station), 'card-tile'))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        text.append(label(next_station.name(station), ['row-name'], ellipsize=True))
        text.append(label(next_station.descriptor(station), ['row-detail'], ellipsize=True))
        inner.append(text)
        open_button.set_child(inner)
        open_button.connect('clicked', lambda *_: self.show_station(station))
        card.append(open_button)
        tune = Gtk.Button(label='Tune in', valign=Gtk.Align.CENTER)
        tune.connect('clicked', lambda *_: self.tune_station(station))
        card.append(tune)
        return card

    def new_station_button(self):
        button = Gtk.Button()
        button.add_css_class('new-station')
        inner = Gtk.Box(spacing=10)
        inner.set_halign(Gtk.Align.CENTER)
        inner.append(Gtk.Image(icon_name='list-add-symbolic'))
        inner.append(label('New station'))
        button.set_child(inner)
        button.connect('clicked', lambda *_: self.new_station())
        return button

    # ---- sections and rows -------------------------------------------------

    def section_title(self, title):
        widget = label(title, ['listen-title'])
        widget.set_hexpand(True)
        return widget

    def link_button(self, text, callback):
        button = Gtk.Button(label=text)
        button.add_css_class('flat')
        button.update_property([Gtk.AccessibleProperty.LABEL], [text])
        button.connect('clicked', lambda *_: callback())
        return button

    def section(self, title, rows, trailing=None, note=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        head.append(self.section_title(title))
        if trailing is not None:
            head.append(trailing)
        box.append(head)
        for entry in rows:
            box.append(self.row(entry))
        if not rows and note:
            box.append(label(note, ['section-note'], wrap=True))
        elif note:
            box.append(label(note, ['section-note'], wrap=True))
        return box

    def tile(self, name, css='row-tile'):
        widget = label(next_station.initials(name), [css, 'tint-%d' % next_station.tint_index(name)])
        widget.set_size_request(56 if css == 'card-tile' else 44, 56 if css == 'card-tile' else 44)
        widget.set_xalign(.5)
        widget.set_yalign(.5)
        widget.set_valign(Gtk.Align.CENTER)
        widget.set_hexpand(False)
        return widget

    def row(self, entry):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        box.add_css_class('listen-row')
        box.append(self.tile(entry.get('name'), 'row-tile'))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        title = label(entry.get('name') or 'Untitled', ['row-name'], ellipsize=True)
        detail = label(entry.get('detail') or '', ['row-detail'], ellipsize=True)
        text.append(title)
        text.append(detail)
        box.append(text)
        action = entry.get('action')
        if action:
            action_label, callback = action
            button = Gtk.Button(label=action_label, valign=Gtk.Align.CENTER)
            button.connect('clicked', lambda *_: callback())
            box.append(button)
        menu = entry.get('menu')
        if menu:
            box.append(self.menu_button(entry.get('name') or 'this item', menu))
        return box

    def menu_button(self, name, items):
        popover = Gtk.Popover()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                       margin_start=6, margin_end=6)
        for text, callback in items:
            item = Gtk.Button(label=text)
            item.add_css_class('flat')
            item.get_child().set_xalign(0.)
            item.connect('clicked', lambda _button, run=callback: (popover.popdown(), run()))
            body.append(item)
        popover.set_child(body)
        button = Gtk.MenuButton(icon_name='view-more-symbolic', popover=popover, valign=Gtk.Align.CENTER)
        button.add_css_class('flat')
        name_it(button, 'More actions for ' + name)
        return button

    # ---- entries -----------------------------------------------------------

    def station_entries(self):
        entries = []
        for station in self.snapshot.get('stations') or []:
            if not isinstance(station, dict):
                continue
            menu = [('Play as ambience', lambda station=station: self.station_ambience_action(station)),
                    ('Open station…', lambda station=station: self.show_station(station))]
            url = next_station.first_source(station).get('url')
            if hub.valid_url(url or ''):
                menu.append(('Open elsewhere', lambda url=url: self.open_external(url)))
            entries.append({'kind': 'station', 'name': next_station.name(station),
                            'detail': next_station.descriptor(station), 'keywords': next_station.origin(station),
                            'action': ('Tune in', lambda station=station: self.tune_station(station)),
                            'menu': menu})
        return entries

    def radio_entries(self):
        entries = []
        for item in self.window.radio:
            if not isinstance(item, dict) or item.get('kind') != 'radio':
                continue
            entries.append(self.radio_entry(item))
        return entries

    def radio_entry(self, item):
        url = item.get('url') or ''
        saved = url in self.favorites
        menu = [('Play as ambience', lambda item=item: self.play_ambience(item)),
                ('Save as station', lambda item=item: self.save_as_station(item)),
                ('Remove from favourites' if saved else 'Favourite', lambda item=item: self.toggle_favourite(item))]
        if hub.valid_url(item.get('website') or ''):
            menu.append(('Open elsewhere', lambda url=item['website']: self.open_external(url)))
        elif hub.valid_url(url):
            menu.append(('Open elsewhere', lambda url=url: self.open_external(url)))
        group = item.get('group') or 'Radio'
        return {'kind': 'radio', 'name': item.get('name') or 'Radio station',
                'detail': str(group) + ' · live', 'keywords': ' '.join(str(tag) for tag in item.get('tags') or []),
                'item': item,
                'action': ('Tune in', lambda item=item: self.tune_radio(item)), 'menu': menu}

    def library_entries(self):
        entries = []
        for item in self.library:
            if not isinstance(item, dict):
                continue
            menu = [('Play as ambience', lambda item=item: self.play_ambience(item)),
                    ('Save as station', lambda item=item: self.save_as_station(item))]
            if item.get('type') == 'Audio':
                if (self.snapshot.get('station') or {}).get('explanation') is not None:
                    menu.insert(0, ('Play next', lambda item=item: self.play_next(item)))
                menu.append(('Track details…', lambda item=item: self.window.open_track_details(item)))
                menu.extend(self.audiomuse_menu(item))
            elif item.get('type') in ('MusicAlbum', 'Playlist'):
                menu.insert(0, ('Show tracks', lambda item=item: self.open_album(item)))
            link = self.jellyfin_link(item)
            if link:
                menu.append(('Open elsewhere', lambda url=link: self.open_external(url)))
            entries.append({'kind': 'library', 'name': item.get('name') or 'Library item',
                            'detail': self.library_kind(item) + ' · Jellyfin',
                            'keywords': ' '.join([str(item.get('artist') or '')] + [str(x) for x in item.get('genres') or []]),
                            'item': item,
                            'action': ('Tune in', lambda item=item: self.tune_library(item)), 'menu': menu})
        return entries

    @staticmethod
    def library_kind(item):
        return {'MusicAlbum': 'Album', 'Playlist': 'Playlist', 'Audio': 'Track'}.get(item.get('type'), 'Library item')

    @staticmethod
    def jellyfin_link(item):
        try:
            import online_services
            return online_services.jellyfin_link(item)
        except Exception:
            return None

    def channel_entries(self):
        return [self.channel_entry(item) for item in self.channels]

    def channel_entry(self, item):
        url = item.get('url') or ''
        menu = [('Play as ambience', lambda item=item: self.play_ambience(item)),
                ('Save as station', lambda item=item: self.save_as_station(dict(item, resolver=True)))]
        preview = self.preview(url)
        if preview and any(key == 'picture' for key, _text in next_links.destinations(preview)):
            menu.append(('Show as scene', lambda url=url: self.show_as_scene(url)))
        menu.append(('Open elsewhere', lambda url=url: self.open_external(url)))
        detail = 'Live channel · Twitch' + (' · live' if item.get('live') else '')
        return {'kind': 'channel', 'name': item.get('name') or item.get('login') or 'Channel',
                'detail': detail, 'keywords': item.get('title') or '',
                'action': ('Tune in', lambda item=item: self.tune_channel(item)), 'menu': menu}

    def elsewhere_entries(self):
        entries = []
        for item in self.window.radio:
            if not isinstance(item, dict) or item.get('kind') not in ('web', 'discovery', 'app'):
                continue
            url = item.get('url') or ''
            kind = {'web': 'Website', 'discovery': 'Discovery', 'app': 'App'}.get(item.get('kind'), 'Elsewhere')
            menu = [('Open elsewhere', lambda item=item: self.open_elsewhere(item))]
            entries.append({'kind': 'elsewhere', 'name': item.get('name') or 'Open elsewhere',
                            'detail': kind + ' · ' + str(item.get('group') or 'Elsewhere'),
                            'keywords': str(item.get('group') or ''),
                            'action': ('Open', lambda item=item: self.open_elsewhere(item)), 'menu': menu})
        return entries

    def world_entry(self, item):
        tags = item.get('tags') or []
        if isinstance(tags, str):   # the directory sometimes sends "jazz, ambient"
            tags = tags.split(',')
        tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        detail = ' · '.join([str(value) for value in (item.get('country') or item.get('countrycode'), item.get('language'),
                                                      ', '.join(tags[:2])) if value]) or 'World radio'
        menu = [('Play as ambience', lambda item=item: self.play_ambience(item)),
                ('Save as station', lambda item=item: self.save_as_station(item)),
                ('Remove from favourites' if (item.get('url') or '') in self.favorites else 'Favourite',
                 lambda item=item: self.toggle_favourite(item))]
        if hub.valid_url(item.get('website') or ''):
            menu.append(('Open elsewhere', lambda url=item['website']: self.open_external(url)))
        return {'kind': 'radio', 'name': item.get('name') or 'Unnamed station', 'detail': detail,
                'keywords': ' '.join(tags), 'item': item,
                'action': ('Tune in', lambda item=item: self.tune_radio(item)), 'menu': menu}

    def link_entry(self):
        if not _looks_like_url(self.query):
            return None
        preview = self.preview(self.query)
        if not preview:
            return None
        menu = []
        for key, text in next_links.destinations(preview):
            menu.append((text, lambda key=key, preview=preview: self.choose_destination(
                key, preview.get('url') or preview.get('original') or '')))
        return {'kind': 'link', 'name': preview.get('name') or 'Link', 'detail': next_links.detail(preview),
                'keywords': preview.get('url') or '', 'preview': preview,
                'action': ('Open link…', lambda preview=preview: self.open_preview(preview)), 'menu': menu}

    @staticmethod
    def preview(url):
        if not _looks_like_url(url):
            return None
        try:
            return next_links.descriptor(url)
        except Exception:
            return None

    # ---- actions -----------------------------------------------------------

    def tune_station(self, station):
        self.window.go_to_now()
        self.window.command('station.start', {'id': station.get('id')})

    def station_ambience_action(self, station):
        first = next_station.first_source(station)
        args = {'kind': 'radio', 'name': next_station.name(station)}
        if first.get('reference'):
            args['reference'] = first['reference']
        elif hub.valid_url(first.get('url') or ''):
            args['source_id'] = first.get('source_id') or first.get('id')
            args['url'] = first['url']
        else:
            self.window.toast('This station has no source to play on its own')
            return
        self.window.command('atmosphere', args)

    def replay_history(self, row):
        self.window.go_to_now()
        self.window.command('station.replay', {'id': row.get('id')})

    def tune_history(self, row):
        self.window.go_to_now()
        self.window.command('station.start', {'id': row.get('station_id') or row.get('id')})

    def tune_radio(self, item):
        self.window.go_to_now()
        self.window.command('music', {'item': item})

    def tune_library(self, item):
        self.window.go_to_now()
        self.window.command('online', {'service': 'jellyfin', 'item': item})

    def tune_channel(self, item):
        self.window.go_to_now()
        self.window.command('link', {'url': item.get('url'), 'destination': 'music'})

    def play_next(self, item):
        """A one-off track after the current one; the station's programming then resumes."""
        self.window.command('station.play_next', {'item': item},
                            done=lambda _result: self.window.toast('Playing next: ' + (item.get('name') or 'track')))

    def play_ambience(self, item):
        if item.get('reference') or item.get('provider') == 'jellyfin':
            self.window.command('atmosphere', {'kind': 'radio', 'reference': item,
                                               'name': item.get('name') or 'Library'})
        elif item.get('kind') == 'twitch' or next_links.host(item.get('url') or '').endswith('twitch.tv'):
            self.window.command('atmosphere', {'kind': 'twitch', 'url': item.get('url'),
                                               'name': item.get('name') or 'Twitch'})
        else:
            self.window.command('atmosphere', {'kind': 'radio', 'source_id': item.get('id'),
                                               'url': item.get('url'), 'name': item.get('name') or 'Radio'})

    def show_as_scene(self, url):
        self.window.command('link', {'url': url, 'destination': 'picture'},
                            done=lambda _result: self.window.toast('Scene opened'))

    def save_as_station(self, item):
        if isinstance(item, dict) and item.get('sources'):
            station = dict(item)
        else:
            station = next_station.station_from_source(item)
        self.window.command('station.save', {'station': station},
                            done=lambda result: self.window.toast(
                                'Saved ' + next_station.name(result.get('station') or station) + ' as a station',
                                'Undo', lambda: self.window.command(
                                    'station.delete', {'id': (result.get('station') or {}).get('id')})))

    def toggle_favourite(self, item):
        url = item.get('url') or ''
        if not hub.valid_url(url):
            return
        saved = url in self.favorites
        name = item.get('name') or 'Station'

        def work():
            if saved:
                hub.save_json(hub.STATE / 'favorites.json',
                              [entry for entry in hub.favorites() if entry.get('url') != url])
            else:
                hub.save_favorite(item)
        self.window.run_local(work, ('Removed from favourites: ' if saved else 'Favourite saved: ') + name)
        self.favorites_at = 0.

    def open_external(self, url):
        if not hub.valid_url(url or ''):
            return

        def work():
            Gio.AppInfo.launch_default_for_uri(url, None)
        self.window.run_local(work, None)

    def open_elsewhere(self, item):
        url = item.get('website') if hub.valid_url(item.get('website') or '') else item.get('url')
        if hub.valid_url(url or ''):
            self.open_external(url)
            return

        def work():
            hub.play(item)
        self.window.run_local(work, 'Opened ' + str(item.get('name') or 'elsewhere'))

    # ---- new station -------------------------------------------------------

    def source_choices(self):
        choices = []
        for item in self.window.radio:
            if isinstance(item, dict) and item.get('kind') == 'radio' and hub.valid_url(item.get('url') or ''):
                choices.append((item.get('name') or 'Radio', item, item.get('group') or 'Radio'))
        for item in self.library:
            if isinstance(item, dict):
                choices.append((item.get('name') or 'Library', item, 'Jellyfin'))
        return choices

    def new_station(self):
        if self.window.get_visible_dialog() is not None:
            return
        choices = self.source_choices()
        if not choices:
            dialog = Adw.AlertDialog(heading='New station', body='There is no source to save yet.')
            dialog.add_response('close', 'Close')
            dialog.present(self.window)
            return
        dialog = Adw.AlertDialog(heading='New station',
                                 body='Choose one source. You can add fallbacks on its page.')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        entry = Gtk.Entry(text=choices[0][0])
        entry.update_property([Gtk.AccessibleProperty.LABEL], ['Station name'])
        dropdown = Gtk.DropDown.new_from_strings(['%s — %s' % (name, where) for name, _item, where in choices])
        box.append(entry)
        box.append(dropdown)
        dialog.set_extra_child(box)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('save', 'Save')
        dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('save')
        dialog.set_close_response('cancel')

        def answered(_dialog, response):
            if response != 'save':
                return
            index = dropdown.get_selected()
            item = choices[index][1] if 0 <= index < len(choices) else choices[0][1]
            station = next_station.station_from_source(item)
            station['name'] = entry.get_text().strip() or (item.get('name') or 'Station')
            self.window.command('station.save', {'station': station}, done=self.new_station_saved)
        dialog.connect('response', answered)
        dialog.present(self.window)

    def new_station_saved(self, result):
        station = (result or {}).get('station')
        if not isinstance(station, dict):
            return
        self.window.toast('Saved ' + next_station.name(station))
        self.show_station(station)

    # ---- station detail ----------------------------------------------------

    def show_station(self, station):
        if not isinstance(station, dict):
            return
        # Opening the page counts again: recordings may have been reviewed since.
        self.review_wanted.discard(station.get('id'))
        self.reviewed.pop(station.get('id'), None)
        if self.station_id != station.get('id'):
            self.review_wanted.discard(self.station_id)
        self.station = dict(station)
        self.station_id = station.get('id')
        self.station_sig = self.fallback_sig = None
        self.pending_station = None
        self.render_station()
        self.set_page('station')
        self.station_scroller.get_vadjustment().set_value(0)
        self.station_back.grab_focus()

    def program(self):
        program = self.snapshot.get('station') or {}
        return program if (program.get('definition') or {}).get('id') == self.station_id else None

    def refresh_station(self):
        if not self.station_id or self.stack.get_visible_child_name() != 'station':
            return
        stations = {item.get('id'): item for item in self.snapshot.get('stations') or [] if isinstance(item, dict)}
        if self.pending_station is None and self.station_id in stations:
            self.station = stations[self.station_id]
        if self.station is None:
            return
        signature = json.dumps([self.station, self.program()], sort_keys=True, default=str)
        if signature == self.station_sig:
            return
        self.station_sig = signature
        self.render_station()

    def render_station(self):
        station = self.station
        if not isinstance(station, dict):
            return
        name = next_station.name(station)
        self.station_name.set_text(name)
        self.station_origin.set_text(next_station.origin(station))
        self.station_tile.set_text(next_station.initials(name))
        for index in range(6):
            self.station_tile.remove_css_class('tint-%d' % index)
        self.station_tile.add_css_class('tint-%d' % next_station.tint_index(name))
        programmable = next_station.programmable(station)
        self.moods_card.set_visible(programmable)
        self.learned_card.set_visible(programmable)
        self.upcoming.set_visible(programmable)
        self.syncing = True
        try:
            chosen = next_station.choices(station)
            for mood in next_station.MOODS:
                labels = [choice for choice, _value in mood['choices']]
                key = mood['key']
                index = labels.index(chosen[key]) if chosen[key] in labels else 0
                if self.mood_groups[key].get_active() != index:
                    self.mood_groups[key].set_active(index)
            arc_labels = [choice for choice, _value in next_station.ARCS]
            arc = next_station.arc_label(station)
            arc_index = arc_labels.index(arc) if arc in arc_labels else 0
            if self.arc_group.get_active() != arc_index:
                self.arc_group.set_active(arc_index)
            end_labels = [choice for choice, _value in next_station.ENDS]
            end = next_station.end_label(station)
            end_index = end_labels.index(end) if end in end_labels else 0
            if self.end_group.get_active() != end_index:
                self.end_group.set_active(end_index)
            held = next_station.hold(self.program(), station)
            if held is None:
                self.hold_switch.set_active(False)
                self.hold_switch.set_sensitive(False)
                self.hold_switch.set_tooltip_text('Tune in to this station to hold its mood')
            else:
                self.hold_switch.set_sensitive(True)
                self.hold_switch.set_tooltip_text('Hold this mood')
                if self.hold_switch.get_active() != held:
                    self.hold_switch.set_active(held)
        finally:
            self.syncing = False
        self.render_learned(station)
        self.ensure_review_count(station)
        self.render_hints(station)
        self.render_fallbacks(station)
        self.render_upcoming()
        self.station_sig = json.dumps([station, self.program()], sort_keys=True, default=str)

    def render_learned(self, station):
        _clear(self.learned_box)
        lines = next_station.learned(self.program(), station)
        if lines:
            for line in lines:
                self.learned_box.append(label(line, ['section-note'], wrap=True))
        else:
            self.learned_box.append(label('Tune in to this station to see what it learns.', ['section-note'], wrap=True))
        self.forget_button.set_sensitive(any(item.get('id') == station.get('id')
                                             for item in self.snapshot.get('stations') or []))

    def ensure_review_count(self, station):
        speech = (station.get('preferences') or {}).get('speech')
        if speech != 'Strict instrumental':
            return
        identity = station.get('id')
        if identity in self.reviewed or identity in self.review_wanted:
            return
        self.review_wanted.add(identity)
        future = self.executor.submit(self.count_reviewed)
        future.add_done_callback(lambda done: GLib.idle_add(self.review_counted, identity, done))

    @staticmethod
    def count_reviewed():
        """Reviewed instrumental recordings across the whole library, paging through every record."""
        count, cursor = 0, None
        for _page in range(REVIEW_PAGES):
            args = {'limit': 200}
            if cursor:
                args['cursor'] = cursor
            result = session_client.request('metadata.export', args, 10) or {}
            count += sum(1 for record in result.get('records') or [] if isinstance(record, dict)
                         and (record.get('review') or {}).get('classification') == 'instrumental')
            cursor = result.get('next_cursor')
            if not cursor:
                return count, False
        return count, True

    def review_counted(self, identity, future):
        self.review_wanted.discard(identity)
        if self.closed:
            return False
        try:
            self.reviewed[identity] = future.result()
        except Exception:
            return False   # the hint keeps its plain wording; reopening the page tries again
        if identity == self.station_id:
            self.render_hints(self.station)
        return False

    def render_hints(self, station):
        review, more = self.reviewed.get(station.get('id'), (None, False))
        for mood in next_station.MOODS:
            hint = self.mood_hints[mood['key']]
            if mood['key'] == 'stay_close':   # Range explains itself in a tooltip; the visible hint is Voices' alone
                self.mood_groups['stay_close'].set_tooltip_text(next_station.range_hint(station) or '')
                hint.set_visible(False)
                continue
            text = next_station.hints(station, review, more) if mood['key'] == 'speech' else None
            hint.set_text(text or '')
            hint.set_visible(bool(text))

    def render_fallbacks(self, station):
        signature = json.dumps(station.get('fallbacks') or [], sort_keys=True, default=str)
        items = next_station.fallbacks(station)
        self.focus_targets = {}
        if signature == self.fallback_sig and self.fallbacks_box.get_first_child() is not None:
            return
        self.fallback_sig = signature
        _clear(self.fallbacks_box)
        if not items:
            self.fallbacks_box.append(label(
                'No fallbacks yet. Add one to keep the sound going if this station cannot play.',
                ['section-note'], wrap=True))
        for item in items:
            self.fallbacks_box.append(self.fallback_row(item, len(items)))
        self.add_fallback_button.set_sensitive(bool(self.fallback_candidates()))
        self.add_fallback_button.set_tooltip_text(
            None if self.fallback_candidates() else 'Save another station to add its source')
        if self.focus_key and self.focus_key in self.focus_targets:
            self.focus_targets[self.focus_key].grab_focus()
        self.focus_key = None

    def fallback_row(self, item, count):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class('fallback-row')
        handle = Gtk.Image(icon_name='open-menu-symbolic')
        handle.add_css_class('fallback-handle')
        handle.set_valign(Gtk.Align.CENTER)
        row.append(handle)
        number = label(str(item['index'] + 1), ['section-note'])
        number.set_size_request(18, -1)
        row.append(number)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        text.append(label(item['name'], ['row-name'], ellipsize=True))
        text.append(label(item['detail'], ['row-detail'], ellipsize=True))
        row.append(text)
        up = icon_button('go-up-symbolic', 'Move ' + item['name'] + ' up',
                         lambda index=item['index']: self.move_fallback(index, index - 1))
        down = icon_button('go-down-symbolic', 'Move ' + item['name'] + ' down',
                           lambda index=item['index']: self.move_fallback(index, index + 1))
        remove = icon_button('window-close-symbolic', 'Remove ' + item['name'],
                             lambda index=item['index']: self.remove_fallback(index))
        up.set_sensitive(item['index'] > 0)
        down.set_sensitive(item['index'] < count - 1)
        for key, widget in (('fallback-%d' % item['index'], up),):
            self.focus_targets[key] = widget
        up.add_css_class('flat')
        down.add_css_class('flat')
        remove.add_css_class('flat')
        row.append(up)
        row.append(down)
        row.append(remove)
        self.make_draggable(row, item['index'])
        self.make_droppable(row, item['index'])
        return row

    def make_draggable(self, row, index):
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect('prepare', lambda _source, _x, _y: Gdk.ContentProvider.new_for_value(str(index)))
        row.add_controller(source)

    def make_droppable(self, row, index):
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.connect('drop', lambda _target, value, _x, _y: self.dropped_fallback(value, index))
        row.add_controller(target)

    def dropped_fallback(self, value, index):
        try:
            source = int(value)
        except (TypeError, ValueError):
            return False
        self.move_fallback(source, index)
        return True

    def fallback_candidates(self):
        return [station['sources'][0] for station in self.snapshot.get('stations') or []
                if isinstance(station, dict) and station.get('id') != self.station_id
                and (station.get('sources') or []) and isinstance(station['sources'][0], dict)]

    def fill_fallback_choices(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                       margin_start=6, margin_end=6)
        candidates = self.fallback_candidates()
        if not candidates:
            body.append(label('Save another station first.', ['section-note']))
        for item in candidates:
            text = next_station.fallback_source_name(item)
            button = Gtk.Button(label=text)
            button.add_css_class('flat')
            button.get_child().set_xalign(0.)
            button.connect('clicked', lambda _b, item=item: (self.add_fallback_popover.popdown(),
                                                             self.add_fallback(item)))
            body.append(button)
        self.add_fallback_popover.set_child(body)

    def add_fallback(self, item):
        station = self.station
        if not isinstance(station, dict):
            return
        items = list(station.get('fallbacks') or []) + [dict(item)]
        self.save_station(next_station.with_fallbacks(station, items),
                          toast=next_station.fallback_source_name(item) + ' added as a fallback')

    def move_fallback(self, source, target):
        station = self.station
        if not isinstance(station, dict):
            return
        items = list(station.get('fallbacks') or [])
        moved = next_station.moved(items, source, target)
        if moved == items:
            return
        self.focus_key = 'fallback-%d' % target
        self.save_station(next_station.with_fallbacks(station, moved))

    def remove_fallback(self, index):
        station = self.station
        if not isinstance(station, dict):
            return
        items = list(station.get('fallbacks') or [])
        if not 0 <= index < len(items):
            return
        name = next_station.fallback_source_name(items[index])
        entry = items[index]
        self.save_station(next_station.with_fallbacks(station, items[:index] + items[index + 1:]),
                          toast=name + ' removed from fallbacks',
                          undo=lambda current: next_station.reinserted(current, entry, index))

    def render_upcoming(self):
        _clear(self.upcoming_box)
        station = self.station
        if not isinstance(station, dict):
            return
        rows = next_station.upcoming(self.program(), station)
        if rows:
            for row in rows:
                line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                title = label(row['name'], ['row-name'], ellipsize=True)
                title.set_hexpand(True)
                line.append(title)
                line.append(label(row['detail'], ['row-detail'], ellipsize=True))
                self.upcoming_box.append(line)
        else:
            self.upcoming_box.append(label(
                'Tune in to this station to see what’s next.' if self.program() is None else
                'Nothing queued yet.', ['section-note'], wrap=True))

    def mood_changed(self, key):
        if getattr(self, 'syncing', False) or not isinstance(self.station, dict):
            return
        group = self.mood_groups[key]
        index = group.get_active()
        mood = next(mood for mood in next_station.MOODS if mood['key'] == key)
        if not 0 <= index < len(mood['choices']):
            return
        label_text = mood['choices'][index][0]
        if label_text == next_station.choices(self.station)[key]:
            return
        self.save_station(next_station.with_choice(self.station, key, label_text),
                          toast=next_station.toast_text(key, label_text),
                          undo=next_station.undo_of(self.station, key))

    def arc_changed(self):
        if getattr(self, 'syncing', False) or not isinstance(self.station, dict):
            return
        index = self.arc_group.get_active()
        if not 0 <= index < len(next_station.ARCS):
            return
        label_text = next_station.ARCS[index][0]
        if label_text == next_station.arc_label(self.station):
            return
        self.save_station(next_station.with_arc(self.station, label_text),
                          toast=next_station.toast_text('arc', label_text),
                          undo=next_station.undo_of(self.station, 'arc'))

    def end_changed(self):
        if getattr(self, 'syncing', False) or not isinstance(self.station, dict):
            return
        index = self.end_group.get_active()
        if not 0 <= index < len(next_station.ENDS):
            return
        label_text = next_station.ENDS[index][0]
        if label_text == next_station.end_label(self.station):
            return
        self.save_station(next_station.with_end(self.station, label_text),
                          toast=next_station.toast_text('end', label_text),
                          undo=next_station.undo_of(self.station, 'end'))

    def hold_changed(self):
        if getattr(self, 'syncing', False):
            return
        held = self.hold_switch.get_active()
        self.window.command('station.hold', {'hold': held},
                            done=lambda _result: self.window.toast(next_station.toast_text('hold', held)))

    def save_station(self, station, toast=None, undo=None):
        """Show the change now and save it. Saves go through one worker, so the last choice lands last."""
        self.station = station
        self.pending_station = station
        self.save_serial = getattr(self, 'save_serial', 0) + 1
        serial = self.save_serial
        self.render_station()
        future = self.save_executor.submit(session_client.request, 'station.save', {'station': station}, 5)
        future.add_done_callback(lambda done: GLib.idle_add(self.save_finished, serial, done, toast, undo))
        if self.pending_timer:
            GLib.source_remove(self.pending_timer)
        self.pending_timer = GLib.timeout_add(3000, self.pending_expired)

    def save_finished(self, serial, future, toast, undo):
        if self.closed:
            return False
        try:
            result = future.result()
        except Exception as problem:
            if serial == getattr(self, 'save_serial', serial):
                self.window.failed(str(problem) or type(problem).__name__)
                self.pending_expired()
            return False
        self.station_saved(serial, result, toast, undo)
        self.window.poll()
        return False

    def station_saved(self, serial, result, toast, undo):
        latest = serial == getattr(self, 'save_serial', serial)
        if latest:
            if self.pending_timer:
                GLib.source_remove(self.pending_timer)
                self.pending_timer = None
            self.pending_station = None
            saved = (result or {}).get('station')
            if isinstance(saved, dict) and saved.get('id') == self.station_id:
                self.station = saved
        if toast:
            self.window.toast(toast, 'Undo', (lambda: self.undo_change(undo)) if undo else None)
        if latest:
            self.refresh_station()

    def undo_change(self, undo):
        """Revert one change on top of the station as it is now."""
        if isinstance(self.station, dict):
            self.save_station(undo(self.station))

    def pending_expired(self):
        self.pending_timer = None
        self.pending_station = None
        self.refresh_station()
        return False

    def rename_dialog(self):
        station = self.station
        if not isinstance(station, dict) or self.window.get_visible_dialog() is not None:
            return
        dialog = Adw.AlertDialog(heading='Rename station')
        entry = Gtk.Entry(text=next_station.name(station), activates_default=True)
        entry.update_property([Gtk.AccessibleProperty.LABEL], ['Station name'])
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('rename', 'Rename')
        dialog.set_response_appearance('rename', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('rename')
        dialog.set_close_response('cancel')

        def answered(_dialog, response):
            name = entry.get_text().strip()
            current = self.station
            if response != 'rename' or not name or not isinstance(current, dict) or name == next_station.name(current):
                return
            self.save_station(dict(current, name=name), toast=next_station.toast_text('name', name),
                              undo=next_station.undo_of(current, 'name'))
        dialog.connect('response', answered)
        dialog.present(self.window)

    def forget_dialog(self):
        station = self.station
        if not isinstance(station, dict) or self.window.get_visible_dialog() is not None:
            return
        name = next_station.name(station)
        dialog = Adw.AlertDialog(heading='Forget what it learned?',
                                 body='This clears the likes and dislikes that shaped ' + name + '.')
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('forget', 'Forget')
        dialog.set_response_appearance('forget', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response('cancel')

        def answered(_dialog, response):
            if response != 'forget':
                return
            self.window.command('station.reset_memory', {'id': station.get('id')},
                                done=lambda _result: self.window.toast('Forgot what ' + name + ' learned'))
        dialog.connect('response', answered)
        dialog.present(self.window)

    # ---- history -----------------------------------------------------------

    def show_history(self):
        self.render_history()
        self.set_page('history')
        self.history_back.grab_focus()

    def history_rows(self):
        return next_station.history_rows(self.snapshot.get('station_recent') or [],
                                         self.snapshot.get('now') or time.time())

    def render_history(self):
        rows = self.history_rows()
        signature = json.dumps(rows, sort_keys=True, default=str)
        if signature == self.history_sig:
            return
        self.history_sig = signature
        _clear(self.history_body)
        if not rows:
            self.history_body.append(label('Nothing has played yet.', ['section-note'], wrap=True))
        for row in rows:
            self.history_body.append(self.row(self.history_entry(row)))

    def history_entry(self, row):
        entry = {'kind': 'history', 'name': row['name'], 'detail': row['detail']}
        if row['action'] == 'replay':
            entry['action'] = ('Replay', lambda row=row: self.replay_history(row))
        else:
            entry['action'] = ('Tune in', lambda row=row: self.tune_history(row))
        menu = [('Save as station', lambda row=row: self.save_as_station(row['station']))]
        url = next_station.first_source(row['station']).get('url')
        if hub.valid_url(url or ''):
            menu.append(('Open elsewhere', lambda url=url: self.open_external(url)))
        entry['menu'] = menu
        return entry

    def refresh_history(self):
        if self.stack.get_visible_child_name() == 'history':
            self.render_history()

    # ---- links -------------------------------------------------------------

    def add_link(self):
        if self.window.get_visible_dialog() is not None:
            return
        if not self.on_listen():
            self.window.show_listen()
        dialog = next_links.AddLinkDialog(self.open_preview)
        dialog.present(self.window)
        self.read_clipboard(dialog.prefill)

    def open_link(self, url):
        try:
            preview = next_links.descriptor(url)
        except Exception as error:
            self.window.toast(str(error) or 'That is not a public link')
            return
        self.open_preview(preview)

    def open_preview(self, preview):
        if self.window.get_visible_dialog() is not None:
            return
        sheet = next_links.LinkSheet(preview, self.choose_destination)
        sheet.present(self.window)

    def choose_destination(self, destination, url):
        if destination == 'original':
            self.open_external(url)
            return
        if destination in ('music', 'atmosphere'):
            self.window.go_to_now()
        self.window.command('link', {'url': url, 'destination': destination},
                            done=(lambda _result: self.window.toast('Scene opened'))
                            if destination == 'picture' else None)

    def open_launch_link(self):
        url, self.launch_link = self.launch_link, None
        if not url or self.closed:
            return False
        if next_links.non_public(url):
            # Started from outside the app: never offer to play something on this machine or a private network.
            self.window.show_listen()
            self.window.toast('That link points to a device on your own network, so it was not opened. '
                              'Paste it into Listen if you meant to play it.')
            return False
        if not (self.snapshot or {}).get('active'):
            self.window.show_listen()
        self.open_link(url)
        return False

    # ---- pages and lifecycle -----------------------------------------------

    def on_listen(self):
        return (self.window.current_page() == 'browse'
                and self.window.browse.get_visible_child_name() == 'listen')

    def set_page(self, name):
        animations = Gtk.Settings.get_default().get_property('gtk-enable-animations')
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE if animations
                                       else Gtk.StackTransitionType.NONE)
        self.stack.set_visible_child_name(name)

    def show_index(self, focus_search=False):
        self.set_page('index')
        self.refresh()
        if focus_search:
            self.search.grab_focus()

    def close(self):
        self.closed = True
        self.cancel_debounce()
        self.stop_search()
        if self.library_cancel is not None:
            self.library_cancel.set()
        if self.volume_timer:
            GLib.source_remove(self.volume_timer)
            self.volume_timer = None
        if self.pending_timer:
            GLib.source_remove(self.pending_timer)
            self.pending_timer = None
        self.save_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
