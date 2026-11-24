"""The next Listening Room window (libadwaita): Rooms, Listen and Now.

It talks to the shared session controller only through session_client, never
touches players itself, and never sends a command that starts or changes
playback while you browse. Listen is provisional here; Rooms (next_rooms), Now, Mix,
Timer, Output, toasts and banners are the finished shell.
"""
import collections
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

import listening_hub as hub
import session_client
import next_model as model
from next_actions import RoomActions, Ticket
from next_async import call_on_main, error_text, pool, run_async
from next_mini import MINI_CSS, MiniPlayer, MiniWindow
from next_mix import MIX_SHORTCUTS, MixSheet
from next_rooms import RoomsPage
from next_watching import Watching
from next_widgets import (FocalPicture, choose_renderer, hold_main_context, show_texture, OutputPopover, Scene, TimerPopover, accent_css, icon_button,
                          install_css, label, menu_item, name_it, pop_button)

APP_ID = 'app.kotona.ListeningRoom'
POLL_MS = 900
FALLBACK_POLL_MS = 5000      # while a stream that worked is down; a controller with no stream is polled at POLL_MS
IDLE_MS = 4000
BREAKPOINT = 1000
MINI_WIDTH = 560  # below this the mini layout replaces the window
FULL_SIZE = (1000, 720)
START_GRACE_S = 8  # how long a start command may take before Now is abandoned
TOAST_GAP = 12                  # between a toast and what it sits above
NOW_CONTROLS_CLEARANCE = 140    # Now's control row (play, volume, Mix) and its bottom padding
CARD_GAP = 16                   # at least this much between the start card and the room's name and chip
MINI_CONTROLS_CLEARANCE = 96    # the mini layout's control row
TOAST_ROOM = 120                # window height kept free for a toast above the open Mix
MUSIC_STARTS = ('music', 'online', 'link', 'station.start', 'station.replay')  # starts that replace a room's music
class Shortcut(tuple):
    """One row of SHORTCUT_PAGES: (GTK accelerator the dialog draws, the key as plain text, what it does). As a
    tuple it is just those three; ``action`` names the handler that installs it ('win.NAME' is a window action,
    'listen.NAME' one of Listen's shortcuts; None for keys a key controller reads, such as Space and Esc), and
    ``alts`` are further accelerators for the same action."""
    def __new__(cls, accel, key, text, action=None, alts=()):
        row = super().__new__(cls, (accel, key, text))
        row.action, row.alts = action, tuple(alts)
        return row


# The one shortcut table: the Keyboard shortcuts dialog, SHORTCUTS and the installed accelerators are all read
# from it, so a key is added (or changed) here and nowhere else; a new handler is named in ``action``.
SHORTCUT_PAGES = (
    ('General', (
        Shortcut('<Control>1', 'Ctrl+1', 'Rooms', 'win.show-rooms'),
        Shortcut('<Control>2', 'Ctrl+2', 'Listen', 'win.show-listen'),
        Shortcut('<Control>3', 'Ctrl+3', 'Now', 'win.show-now'),
        Shortcut('<Control>m', 'Ctrl+M', 'Mini player', 'win.mini-player'),
        Shortcut('<Control>comma', 'Ctrl+,', 'Preferences', 'win.preferences'),
        Shortcut('<Control>question', 'Ctrl+?', 'Keyboard shortcuts', 'win.shortcuts', alts=('<Control><Shift>slash',)),
        Shortcut('Escape', 'Esc', 'Close Mix or a dialog, then go back to where you were'),
    )),
    ('Rooms', (
        Shortcut('Return', 'Enter', 'Enter the focused room'),
        Shortcut('space', 'Space', 'Preview the focused room'),
        Shortcut('Menu <Shift>F10', 'Menu / Shift+F10', 'Open the focused room\'s options, for rooms of yours'),
    )),
    ('Now', (
        Shortcut('space', 'Space', 'Pause or play the room'),
        Shortcut('Up Down', 'Up / Down', 'Room volume, in Now and the mini player'),
    )),
    ('Mix', tuple(Shortcut(*row) for row in MIX_SHORTCUTS)),   # read by the Mix's key controller
    ('Listen', (
        Shortcut('<Control>f', 'Ctrl+F', 'Search Listen', 'listen.search'),
        Shortcut('<Control>l', 'Ctrl+L', 'Add a link', 'listen.link'),
        Shortcut('<Control>v', 'Ctrl+V', 'Add a link from the clipboard, outside text fields', 'listen.paste'),
    )),
)
SHORTCUTS = tuple((key, text) for _page, items in SHORTCUT_PAGES for _accel, key, text in items)


def shortcuts_for(prefix):
    """{handler name: [accelerators]} for the table rows whose action starts with ``prefix`` ('win.', 'listen.')."""
    return {row.action[len(prefix):]: [row[0], *row.alts] for _page, items in SHORTCUT_PAGES for row in items
            if row.action and row.action.startswith(prefix)}


_system_animations = None


def system_animations():
    """The desktop's own animation setting, read once before this window changes it."""
    global _system_animations
    if _system_animations is None:
        _system_animations = bool(Gtk.Settings.get_default().get_property('gtk-enable-animations'))
    return _system_animations


class NextWindow(Adw.ApplicationWindow):
    mini_layout = GObject.Property(type=bool, default=False)

    def __init__(self, app):
        super().__init__(application=app, title='Listening room')
        self.add_css_class('next')
        self.system_animations = system_animations()
        self.set_default_size(1200, 800)
        self.set_size_request(MINI_WIDTH - 200, 300)
        self.mini_window = None
        self.page_name = 'loading'
        self.full_size = FULL_SIZE
        self.snapshot, self.catalogue, self.presets = {}, [], []
        self.closed = self.updating = self.polling = self.poll_again = False
        self.polls_sent = self.snapshot_poll = 0
        self.first = True
        self.want_now = False
        self.want_since = 0.
        self.pending = None        # a start in flight: {'name', 'art', 'state', 'done', 'since', ...}
        self.pending_waiters = []  # called when a pending start ends (controls re-enable themselves)
        self.now_origin = 'rooms'      # where Back and Esc in Now return to: 'rooms' or 'listen'
        self.listen_origin = None      # where Esc in Listen returns to: 'now', 'rooms' or None (stay)
        self.section_errors = {}       # surface -> the last exception its render raised
        self.rendered = None           # the snapshot last drawn: the next one draws only what differs from it
        self.drawn = {}                # surface -> the window state it last drew with (see due)
        self.rebuilds = collections.Counter()   # draws per surface: the partial-rebuild tests read it
        self.offline = False           # the room service did not answer the last poll
        self.offline_error = ''
        self.config_generation = None  # library connections and favourites, as the controller last saw them
        self.browse_narrow = False     # Browse rooms was chosen in the narrow (mini) layout
        self.listen_first = False      # the active room began with Tune in, not with a room
        self.toast_margin = self.toast_key = self.pending_key = None
        self.mode_buttons_hidden = False
        self.action_toasts = 0         # toasts with an action (Undo, Details) on screen or queued
        self.room_key = self.cards_key = self.sources_key = None
        self.volume_timer = self.idle_timer = None
        self.selected_room = self.surface = None
        self.outputs, self.radio = [], []
        self.executor = pool('room-window', 3)
        self.urgent = pool('room-urgent')
        self.poll_pool = pool('room-poll')     # reads only: a slow command never delays a poll
        display = Gdk.Display.get_default()
        install_css(display)
        install_css(display, MINI_CSS, 1)
        self.accent_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self.accent_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.actions = RoomActions(self.command, self.toast, lambda: self.snapshot)
        self.build()
        self.install_actions()
        self.connect('close-request', self.on_close)
        self.connect('map', self.watch_surface)
        self.add_tick_callback(self.adapt)
        GLib.idle_add(self.load_static)
        self.stream, self.stream_up, self.stream_seen = None, False, False   # pushed snapshots (subscribe)
        self.push_mark = 0             # polls_sent when the last pushed snapshot arrived
        self.polled_at = 0.
        self.poll_source = GLib.timeout_add(POLL_MS, self.timed_poll)
        self.subscribe()
        self.poll()

    # ---- structure --------------------------------------------------------

    def build(self):
        self.toasts = Adw.ToastOverlay()
        self.toolbar = Adw.ToolbarView()
        self.toolbar.set_top_bar_style(Adw.ToolbarStyle.FLAT)
        self.header = Adw.HeaderBar()
        self.back = icon_button('go-previous-symbolic', 'Back to rooms', self.go_back, ['header-pill'])
        self.header.pack_start(self.back)
        self.browse = Adw.ViewStack()
        self.browse.add_titled_with_icon(self.build_rooms(), 'rooms', 'Rooms', 'view-grid-symbolic')
        self.browse.add_titled_with_icon(self.build_listen(), 'listen', 'Listen', 'audio-headphones-symbolic')
        self.switcher = Adw.ViewSwitcher(stack=self.browse, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header.set_title_widget(self.switcher)
        # Choosing an ambience is a destination: the header names it and Back leaves it (see sync_mode_header).
        self.mode_back = icon_button('go-previous-symbolic', 'Back to Mix', lambda: self.listen.end_ambience(returned=True),
                                     ['header-pill'])
        self.mode_back.set_visible(False)
        self.header.pack_start(self.mode_back)
        self.mode_title = Gtk.Label(label='', ellipsize=3, max_width_chars=40)
        self.mode_title.add_css_class('title')
        self.mode_title.add_css_class('mix-room')
        self.mode_title.update_property([Gtk.AccessibleProperty.LABEL], ['Choose ambience'])
        self.menu_popover = Gtk.Popover()
        self.menu_button = pop_button('open-menu-symbolic', 'Main menu', self.menu_popover)
        self.timer_popover = TimerPopover(self.choose_timer)
        self.timer_button = pop_button('alarm-symbolic', 'Timer', self.timer_popover)
        self.output_popover = OutputPopover(self.choose_output, self.test_output, self.set_system_volume, self.load_outputs)
        self.output_button = pop_button('audio-speakers-symbolic', 'Output', self.output_popover)
        self.timer_chip = Gtk.Button(visible=False)
        self.timer_chip.add_css_class('timer-chip')
        self.timer_chip.add_css_class('header-pill')
        self.timer_chip.connect('clicked', lambda *_: self.timer_button.popup())
        for popover in (self.menu_popover, self.timer_popover, self.output_popover):
            popover.connect('show', lambda *_: self.kick_idle())
            popover.connect('closed', lambda *_: self.kick_idle())
        for widget in (self.menu_button, self.timer_button, self.output_button, self.timer_chip):
            self.header.pack_end(widget)
        self.toolbar.add_top_bar(self.header)
        # Below MINI_WIDTH the Rooms | Listen switcher moves from the header (where it is cut off) to a bar at the bottom.
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.browse)
        self.toolbar.add_bottom_bar(self.switcher_bar)
        self.toolbar.add_top_bar(self.build_offline_banner())
        self.toolbar.add_top_bar(self.build_section_error())
        self.pages = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=250,
                               hhomogeneous=False, vhomogeneous=False)  # the mini layout must be able to be narrower than Rooms
        self.pages.add_named(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER), 'loading')
        self.pages.add_named(self.browse, 'browse')
        self.sheet = Adw.BottomSheet(content=self.build_now(), sheet=self.build_mix())
        self.sheet.set_show_drag_handle(True)
        self.sheet.set_full_width(False)
        self.sheet.connect('notify::open', lambda *_: self.kick_idle())
        self.pages.add_named(self.sheet, 'now')
        self.mini_player = MiniPlayer(self, closable=False)
        self.pages.add_named(self.mini_player, 'mini')
        self.listen_shown = False
        for stack in (self.pages, self.browse):
            stack.connect('notify::visible-child', lambda *_: self.listen_visibility())
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: %dpx' % (MINI_WIDTH - 1)))
        breakpoint.add_setter(self, 'mini-layout', True)
        self.add_breakpoint(breakpoint)
        self.connect('notify::mini-layout', lambda *_: self.apply_layout())
        self.toolbar.set_content(self.pages)
        self.toasts.set_child(self.toolbar)
        self.toast_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.toast_provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
        self.set_content(self.toasts)
        self.build_menu()
        self.show_page('loading')

    def build_menu(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        self.menu_room_items = [menu_item('Save as my room…', self.save_dialog, self.menu_popover),
                                menu_item('Leave room', self.leave_room, self.menu_popover)]
        for item in self.menu_room_items:
            box.append(item)
        box.append(menu_item('Add a link…', self.listen.add_link, self.menu_popover))
        box.append(menu_item('Other players…', self.open_players, self.menu_popover))
        box.append(menu_item('Mini player', self.open_mini, self.menu_popover))
        box.append(menu_item('Preferences…', self.open_preferences, self.menu_popover))
        box.append(menu_item('Keyboard shortcuts', self.shortcuts_dialog, self.menu_popover))
        box.append(menu_item('About', self.about_dialog, self.menu_popover))
        self.menu_popover.set_child(box)

    @staticmethod
    def details_expander():
        """A collapsed "Details" holding the raw cause; the headline stays a plain sentence."""
        text = label('', ['banner-detail'], wrap=True)
        text.set_selectable(True)
        text.set_xalign(0)
        expander = Gtk.Expander(label='Details', child=text, visible=False)
        expander.add_css_class('error-details')
        expander.text = text
        return expander

    @staticmethod
    def show_details(expander, raw):
        expander.text.set_text(raw or '')
        expander.set_visible(bool(raw))
        if not raw:
            expander.set_expanded(False)

    def build_offline_banner(self):
        """Persistent while the room service does not answer: says so, and offers Start again."""
        self.offline_bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, reveal_child=False)
        box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        box.add_css_class('offline-banner')
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.offline_title = label('The room service isn’t running. Nothing can play until it is back.',
                                   ['banner-title'], wrap=True)
        self.offline_details = self.details_expander()
        text.append(self.offline_title)
        text.append(self.offline_details)
        box.append(text)
        self.offline_start = Gtk.Button(label='Start again', valign=Gtk.Align.CENTER)
        self.offline_start.add_css_class('suggested-action')
        self.offline_start.connect('clicked', lambda *_: self.start_service())
        box.append(self.offline_start)
        self.offline_bar.set_child(box)
        return self.offline_bar

    def build_section_error(self):
        """One inline line when a part of the window failed to draw; the rest keeps working."""
        self.section_bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, reveal_child=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=12, margin_end=12,
                      margin_top=4, margin_bottom=4)
        box.add_css_class('offline-banner')
        self.section_title = label('', ['banner-title'], wrap=True)
        self.section_details = self.details_expander()
        box.append(self.section_title)
        box.append(self.section_details)
        self.section_bar.set_child(box)
        return self.section_bar

    SECTION_WORDS = {'rooms': 'Rooms', 'listen': 'Listen', 'now': 'Now', 'mini': 'The mini player',
                     'mini-window': 'The mini player', 'pending': 'Now'}

    def guard(self, section, render, *args):
        """Run one surface's render; if it raises, log it and say so inline instead of blanking the window."""
        try:
            result = render(*args)
        except Exception as error:
            first = section not in self.section_errors
            self.section_errors[section] = error
            if first:
                print('listening-room: drawing %s failed' % section, file=sys.stderr)
                traceback.print_exc()
            self.render_section_errors()
            return None
        if self.section_errors.pop(section, None) is not None:
            self.render_section_errors()
        return result

    def render_section_errors(self):
        errors = self.section_errors
        self.section_bar.set_reveal_child(bool(errors))
        if errors:
            names = sorted({self.SECTION_WORDS.get(name, name.title()) for name in errors})
            self.section_title.set_text('%s couldn’t be drawn. The rest of the window still works.' % ' and '.join(names))
            self.show_details(self.section_details,
                              '\n'.join('%s: %s: %s' % (name, type(error).__name__, error) for name, error in errors.items()))

    # ---- Rooms ------------------------------------------------------------

    def build_rooms(self):
        self.rooms = RoomsPage(self)
        self.cards, self.hero_button = self.rooms.cards, self.rooms.hero_button
        return self.rooms.widget

    def apply_accent(self, accent):
        css = accent_css(accent)
        if css != getattr(self, 'accent_text', None):
            self.accent_text = css
            self.accent_provider.load_from_string(css)

    @staticmethod
    def set_artwork(picture, path):
        show_texture(picture, path)

    def go_to_now(self):
        """A start command is in flight; open Now when the room is actually up.

        Polls can arrive before the controller has the room, so waiting is not
        the same as giving up: only a failed command or the grace window ends it.
        """
        self.want_now = True
        self.want_since = time.monotonic()

    # ---- Listen (provisional) ----------------------------------------------

    def build_listen(self):
        from next_listen import ListenPage
        self.listen = ListenPage(self)
        return self.listen.surface()

    def tune(self, entry):
        self.start(entry['args'][0], entry['args'][1] if len(entry['args']) > 1 else {}, entry.get('name') or 'your room')

    # ---- Now ----------------------------------------------------------------

    def build_now(self):
        overlay = Gtk.Overlay()
        self.scene = Scene()
        overlay.set_child(self.scene)
        top = Gtk.Box(valign=Gtk.Align.START)
        top.add_css_class('scrim-top')
        top.set_size_request(-1, 140)
        bottom = Gtk.Box(valign=Gtk.Align.END)
        bottom.add_css_class('scrim-bottom')
        bottom.set_size_request(-1, 420)
        overlay.add_overlay(top)
        overlay.add_overlay(bottom)
        self.banner = self.build_banner()
        overlay.add_overlay(self.banner)
        self.pending_card = self.build_pending()
        overlay.add_overlay(self.pending_card)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, valign=Gtk.Align.END,
                         margin_start=40, margin_end=40, margin_bottom=36)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        chips = Gtk.Box(spacing=8, halign=Gtk.Align.START)
        self.chip = label('', ['source-chip'])
        self.chip.set_halign(Gtk.Align.START)
        chips.append(self.chip)
        # Tune in replaced this room's music: say so; Save as my room… is in the Mix and the toast.
        self.changed_chip = label('Changed', ['room-changed'])
        self.changed_chip.set_visible(False)
        self.changed_chip.set_tooltip_text('The music or scene differs from the saved room. Save it from the Mix.')
        chips.append(self.changed_chip)
        self.watching = Watching(self)      # "Watching · title" with Stop watching (the Watch window)
        chips.append(self.watching.chip)
        self.room_name = label('', ['room-name'], wrap=True)
        line = Gtk.Box(spacing=8)
        self.now_line = label('', ['now-line'], ellipsize=True)
        self.now_detail = label('', ['now-detail'], ellipsize=True)
        line.append(self.now_line)
        line.append(self.now_detail)
        # A room started from Listen has no scene of its own: offer one, never a black room.
        self.choose_scene = Gtk.Button(label='Choose a scene', halign=Gtk.Align.START, visible=False)
        self.choose_scene.add_css_class('glass')
        self.choose_scene.add_css_class('pill')
        self.choose_scene.connect('clicked', lambda *_: self.choose_other('picture'))
        for widget in (chips, self.room_name, line, self.choose_scene):
            text.append(widget)
        column.append(text)
        self.now_overlay, self.now_column, self.now_text = overlay, column, text
        self.controls = Gtk.CenterBox()
        self.mix_button = Gtk.Button(halign=Gtk.Align.START)
        self.mix_button.add_css_class('glass')
        self.mix_button.add_css_class('mix-button')
        name_it(self.mix_button, 'Mix', 'Mix')
        inner = Gtk.Box(spacing=12)
        inner.append(Gtk.Image(icon_name='preferences-system-symbolic'))
        inner.append(label('Mix', ['heading']))
        self.mix_summary = label('', ['mix-summary'], ellipsize=True)
        self.mix_summary.set_max_width_chars(28)
        inner.append(self.mix_summary)
        self.marker = Gtk.Box(valign=Gtk.Align.CENTER, visible=False)
        self.marker.add_css_class('mix-marker')
        inner.append(self.marker)
        self.mix_button.set_child(inner)
        self.mix_button.connect('clicked', lambda *_: self.open_mix())
        self.controls.set_start_widget(self.mix_button)
        centre = Gtk.Box(spacing=14, halign=Gtk.Align.CENTER)
        self.play = Gtk.Button(icon_name='media-playback-pause-symbolic')
        self.play.add_css_class('play-button')
        self.play.add_css_class('circular')
        self.play.set_size_request(72, 72)
        self.play.connect('clicked', lambda *_: self.toggle_room())
        name_it(self.play, 'Pause the room')
        self.skip = icon_button('media-skip-forward-symbolic', 'Next track', self.next_track, ['skip-button', 'circular'])
        self.less = icon_button('action-unavailable-symbolic', 'Less like this', lambda: self.feedback('less'),
                                ['skip-button', 'circular'])
        self.more = icon_button('emblem-favorite-symbolic', 'More like this', lambda: self.feedback('more'),
                                ['skip-button', 'circular'])
        centre.append(self.less)
        centre.append(self.play)
        centre.append(self.skip)
        centre.append(self.more)
        self.controls.set_center_widget(centre)
        volume = Gtk.Box(spacing=10, halign=Gtk.Align.END)
        volume.append(Gtk.Image(icon_name='audio-volume-high-symbolic'))
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_size_request(170, -1)
        self.volume.add_css_class('volume-scale')
        self.volume.get_adjustment().set_page_increment(5)
        name_it(self.volume, 'Room volume')
        self.volume.connect('value-changed', self.volume_changed)
        volume.append(self.volume)
        self.controls.set_end_widget(volume)
        column.append(self.controls)
        overlay.add_overlay(column)
        motion = Gtk.EventControllerMotion()
        motion.connect('motion', lambda *_: self.kick_idle())
        overlay.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect('pressed', lambda *_: self.kick_idle())
        overlay.add_controller(click)
        return overlay

    def build_banner(self):
        banner = Gtk.Box(spacing=16, halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=70,
                         margin_start=16, margin_end=16, visible=False)
        banner.add_css_class('banner-card')
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.banner_title = label('', ['banner-title'], wrap=True)
        self.banner_detail = label('', ['banner-detail'], wrap=True)
        self.banner_details = self.details_expander()
        text.append(self.banner_title)
        text.append(self.banner_detail)
        text.append(self.banner_details)
        banner.append(text)
        self.banner_retry = Gtk.Button(label='Try again', valign=Gtk.Align.CENTER)
        self.banner_retry.connect('clicked', lambda *_: self.try_again())
        self.banner_choose = Gtk.Button(label='Choose another', valign=Gtk.Align.CENTER)
        self.banner_choose.connect('clicked', lambda *_: self.choose_other(self.banner_layer))
        banner.append(self.banner_retry)
        banner.append(self.banner_choose)
        self.banner_layer = None
        return banner

    def build_pending(self):
        """The in-flight card on Now: a spinner and "Connecting to X…", or what went wrong and what to do."""
        card = Gtk.Box(spacing=16, halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=70,
                       margin_start=16, margin_end=16, visible=False)
        card.add_css_class('banner-card')
        self.pending_spinner = Gtk.Spinner(spinning=False, valign=Gtk.Align.CENTER)
        self.pending_spinner.set_size_request(24, 24)
        card.append(self.pending_spinner)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.pending_title = label('', ['banner-title'], wrap=True)
        self.pending_detail = label('', ['banner-detail'], wrap=True)
        self.pending_details = self.details_expander()
        text.append(self.pending_title)
        text.append(self.pending_detail)
        text.append(self.pending_details)
        card.append(text)
        self.pending_retry = Gtk.Button(label='Try again', valign=Gtk.Align.CENTER)
        self.pending_retry.connect('clicked', lambda *_: self.retry_pending())
        self.pending_back = Gtk.Button(label='Back', valign=Gtk.Align.CENTER)
        self.pending_back.connect('clicked', lambda *_: self.dismiss_pending())
        card.append(self.pending_retry)
        card.append(self.pending_back)
        return card

    # ---- a start in flight ---------------------------------------------------------------

    def start(self, action, args, name, art=None, done=None):
        """Begin something that starts sound: show Now at once as "Connecting to name…".

        Returns the command's Ticket, or False when it was not sent. A repeat of the start in flight
        is ignored; a different one is refused with a toast that says why. Nothing else changes until
        the controller answers: on failure the card says why, with the previous state intact; on
        success the card clears when the room is up, even when the answer came after the grace time."""
        pending = self.pending
        if pending and (pending['state'] == 'connecting' or pending['inflight']):
            if (action, args) != (pending['action'], pending['args']):
                self.toast('Still connecting to %s. Choose again once it has started, or press Back.' % pending['name'],
                           urgent=True)
            return False
        origin = self.browse.get_visible_child_name() if self.page_name in ('browse', 'loading') else 'now'
        pending = self.pending = {'name': name, 'art': art, 'state': 'connecting', 'done': False, 'inflight': True,
                                  'since': time.monotonic(), 'action': action, 'args': args,
                                  'origin': (self.page_name, self.browse.get_visible_child_name()),
                                  'entry_before': (self.snapshot.get('entry') or {}).get('id')}
        if origin in ('rooms', 'listen'):
            self.now_origin = origin
        self.want_now = False
        self.render_pending()
        self.show_page('now')
        room = self.snapshot.get('active')
        if action in MUSIC_STARTS and not room:
            self.listen_first = True     # Tune in with no room: Now offers Choose a scene
        elif action in ('enter', 'return'):
            self.listen_first = False
        if room and action in MUSIC_STARTS and not model.ad_hoc(self.snapshot, self.catalogue):
            done = self.then_mark_changed(room, name, done)
        ticket = self.command(action, args, done=lambda result: (self.pending_done(pending, result), done(result) if done else None),
                              failed=lambda error: self.pending_failed(error, pending))
        GLib.timeout_add(int(START_GRACE_S * 1000) + 300, lambda: self.pending_expired(pending))
        return ticket

    def then_mark_changed(self, room, name, done=None):
        """After Tune in replaced a saved room's music (and it really did, with no new music error):
        say the room changed, with Save as my room."""
        error_before = (self.snapshot.get('errors') or {}).get('music')

        def changed(snapshot):
            if snapshot is None or (snapshot.get('active') or {}).get('id') != room.get('id'):
                return
            error = (snapshot.get('errors') or {}).get('music')
            if (error and error != error_before) or not model.room_changed(snapshot, self.catalogue):
                return
            self.toast('Changed: %s now plays in %s' % (name, room.get('name') or 'your room'),
                       'Save as my room', self.save_dialog)

        def finished(result):
            if done:
                done(result)
            self.when_pending_ends(lambda: self.inspect_then(changed))   # once the new music is up
        return finished

    def pending_done(self, pending, result=None):
        if pending is not self.pending:
            return
        pending.update(inflight=False, done=True)
        if isinstance(result, dict) and isinstance(result.get('id'), int) and not isinstance(result.get('id'), bool):
            pending['id'] = result['id']     # the controller's entry for this start says how it ended
        if pending['state'] == 'failed' and pending.get('timed_out'):
            pending['state'] = 'connecting'   # a slow start that did succeed: the next snapshot clears the card
            self.render_pending()
        if pending.get('cancel_requested'):
            self.cancel_pending()

    def cancel_pending(self):
        """The Mix's Cancel (O11-CONTROL): the controller abandons the start (``entry.cancel``) and keeps the
        room before it, or idle; then the card goes, as Back does. A start not yet answered is cancelled
        as soon as its reply names it, so Cancel never races the start it means."""
        pending = self.pending
        if not pending or pending['state'] != 'connecting':
            return
        if pending.get('id') is None and pending['inflight']:
            pending['cancel_requested'] = True
            return
        pending['cancel_requested'] = False

        def done(result):
            if pending is self.pending and isinstance(result, dict) and result.get('cancelled'):
                self.dismiss_pending()
                self.toast('Stopped connecting to ' + pending['name'])
        self.command('entry.cancel', {'id': pending['id']} if pending.get('id') is not None else {},
                     urgent=True, done=done)

    def pending_failed(self, error, pending=None, plain=None):
        pending = self.pending if pending is None else pending
        if pending is not self.pending or not pending:
            return
        pending.update(inflight=False, state='failed', error=plain or model.plain_error(error), details=model.error_details(error),
                       timed_out=False)
        self.render_pending()

    def pending_expired(self, pending=None):
        pending = self.pending if pending is None else pending
        if pending is self.pending and pending and pending['state'] == 'connecting' \
                and time.monotonic() - pending['since'] >= START_GRACE_S:
            if pending['done']:
                # The controller said yes; do not call a slow start a failure. While it still connects the card
                # stays (render settles it), so a slow start that then fails is never silent.
                outcome = model.start_outcome(self.snapshot, pending)
                if outcome and outcome[0] == 'failed':
                    self.pending_failed(outcome[1], pending, model.start_error(outcome[1]))
                elif outcome or not self.snapshot.get('transition'):
                    self.clear_pending()
            else:
                pending.update(state='failed', timed_out=True, details='',
                               error='It is taking longer than usual. If it starts after all, this card clears itself.')
                self.render_pending()
        return False

    def retry_pending(self):
        pending = self.pending
        if pending and pending['state'] == 'failed' and not pending['inflight']:
            self.pending = None
            self.start(pending['action'], pending['args'], pending['name'], pending['art'])

    def dismiss_pending(self):
        pending = self.pending
        self.clear_pending()
        if pending and not self.snapshot.get('active'):
            page, child = pending['origin']
            if page == 'browse' and child == 'listen':
                self.show_listen()
            else:
                self.show_rooms()
        self.render_now(self.snapshot) if self.snapshot.get('active') else None

    def clear_pending(self):
        self.pending = None
        self.render_pending()
        if self.current_page() in ('now', 'mini'):
            GLib.idle_add(lambda: self.focus_play() and False)
        waiters, self.pending_waiters = self.pending_waiters, []
        for waiter in waiters:
            waiter()

    def render_pending(self):
        pending = self.pending
        self.drawn.pop('now', None)     # the card writes over Now's name and chip: Now draws in full after it
        card = self.pending_card
        connecting = bool(pending and pending['state'] == 'connecting')
        failed = bool(pending and pending['state'] == 'failed')
        card.set_visible(bool(pending))
        self.pending_spinner.set_spinning(connecting)
        self.pending_spinner.set_visible(connecting)
        self.pending_retry.set_visible(failed)
        self.pending_back.set_visible(failed)
        # Try again waits for the first answer: a start the controller is still working on is never sent twice.
        self.pending_retry.set_sensitive(failed and not pending['inflight'])
        self.pending_retry.set_tooltip_text('Still waiting for an answer' if failed and pending['inflight'] else None)
        self.show_details(self.pending_details, pending.get('details', '') if failed else '')
        self.play.set_sensitive(not (self.offline or connecting))
        self.mix.show_connecting(pending['name'] if connecting else None, self.cancel_pending)  # O11-CONTROL
        if not pending:
            return
        if connecting:
            self.pending_title.set_text('Connecting to ' + pending['name'] + '…')
            self.pending_detail.set_text('Nothing plays until it is ready.')
            self.play.set_child(Gtk.Spinner(spinning=True))
            # Until the room's snapshot arrives nothing says what it can do: no track controls, the stored
            # volume, and no summary of the last room's layers.
            for control in (self.skip, self.less, self.more):
                control.set_visible(False)
            self.show_room_volume(self.snapshot)
            self.mix_summary.set_text('Connecting…')
            self.room_name.set_text(pending['name'])
            self.chip.set_text('Connecting')
            self.chip.set_visible(True)
            self.changed_chip.set_visible(False)
            self.banner.set_visible(False)      # the last room's layer news is not about this start
            self.marker.set_visible(False)
            self.choose_scene.set_visible(False)
            self.now_line.set_text('')
            self.now_detail.set_visible(False)
            if pending.get('art'):
                self.scene.show_room({'still': pending['art'], 'poster': pending['art'], 'loop': None}, False)
        else:
            previous = (self.snapshot.get('active') or {}).get('name')
            if previous:
                self.render_now(self.snapshot)  # the room that is still there, not "Connecting", under the card
            else:
                self.chip.set_visible(False)    # nothing is connecting any more; render_now brings it back
                self.room_name.set_text(pending['name'])
            self.pending_title.set_text('Couldn’t start ' + pending['name'])
            self.pending_detail.set_text(pending.get('error', '') + (' ' + previous + ' is still there.' if previous else ''))
            self.play.set_icon_name('media-playback-start-symbolic')

    def when_pending_ends(self, waiter):
        if self.pending:
            self.pending_waiters.append(waiter)
        else:
            waiter()

    def build_mix(self):
        self.mix = MixSheet(self.layer_command, self.choose_other, lambda: self.sheet.set_open(False), self.save_dialog,
                            self.layer_options)
        return self.mix

    def render_now(self, snapshot):
        info = model.now_model(snapshot)
        if info is None:
            return
        ad_hoc = model.ad_hoc(snapshot, self.catalogue)
        self.chip.set_text('Not connected' if self.offline else info['chip'])
        self.chip.set_visible(True)
        # A room started from Listen, or saved under a generic name, is named after what plays; the line
        # under it then says the track, never the same name again.
        name, line, detail = model.now_text(snapshot, self.catalogue)
        self.room_name.set_text(name)
        self.changed_chip.set_visible(model.room_changed(snapshot, self.catalogue))
        assets = model.scene_assets(snapshot.get('active'), self.presets)
        own_scene = bool(assets.get('still') or assets.get('loop'))
        self.choose_scene.set_visible((ad_hoc or self.listen_first or not own_scene) and any(scene['available'] for scene in self.layer_options('picture')))
        self.now_line.set_text(line)
        self.now_detail.set_text('· ' + detail if detail else '')
        self.now_detail.set_visible(bool(detail))
        self.mix_summary.set_text(model.layer_summary(snapshot, name))
        self.play.set_icon_name('media-playback-pause-symbolic' if info['state'] == 'playing' else 'media-playback-start-symbolic')
        name_it(self.play, 'Pause the room' if info['state'] == 'playing' else 'Play the room')
        self.skip.set_visible(info['can_next'])
        self.more.set_visible(info['can_feedback'])
        self.less.set_visible(info['can_feedback'])
        self.updating = True
        if self.volume_timer is None:
            self.volume.set_value(float(info['room_volume']))
        self.updating = False
        failed = info['failure']
        self.banner.set_visible(bool(failed))
        self.marker.set_visible(bool(failed))
        if failed:
            self.banner_layer = failed['layer']
            self.banner_title.set_text(failed['title'])
            self.banner_detail.set_text(failed['detail'])
            self.show_details(self.banner_details, failed['error'])
        self.apply_accent(info['accent'])
        shown = (failed and failed['layer'], tuple(item['kind'] for item in info['timers']), info['state'])
        if shown != getattr(self, 'shown', None):
            self.shown = shown
            self.kick_idle()  # a change worth seeing brings the controls back
        motion = model.motion_allowed(snapshot, self.system_animations)
        if not own_scene:   # never a black room, saved or ad hoc
            assets = model.fallback_scene(snapshot, self.presets, self.catalogue)
        self.scene.show_room(assets, motion)
        self.mix.update(snapshot)
        self.toast_key = None           # the Mix's rows may have changed: place toasts again
        self.render_timers(info['timers'])

    def render_timers(self, running):
        self.timer_popover.set_running(bool(running))
        self.timer_chip.set_visible(bool(running))
        if running:
            self.timer_chip.set_label(running[0]['label'])
            name_it(self.timer_chip, running[0]['label'].replace(' · ', ' timer, ') + ' left. Open timer options', None)

    # ---- navigation ---------------------------------------------------------

    def show_page(self, name):
        """Show a page; below 560 px wide the mini layout stands in for every page but loading,
        unless Browse rooms was chosen there. Entering Now focuses its play control."""
        previous = self.page_name
        if name == 'now' and previous == 'browse':
            self.now_origin = 'listen' if self.browse.get_visible_child_name() == 'listen' else 'rooms'
        if name == 'now':
            self.browse_narrow = False
        self.page_name = name
        mini = self.mini_layout and name != 'loading' and not (name == 'browse' and self.browse_narrow)
        shown = 'mini' if mini else name
        self.pages.set_visible_child_name(shown)
        in_now = shown == 'now'
        art = shown in ('now', 'mini')
        self.toolbar.set_extend_content_to_top_edge(art)
        self.back.set_visible(in_now)
        name_it(self.back, 'Back to Listen' if self.now_origin == 'listen' else 'Back to rooms')
        if name == 'now' and previous != 'now':
            GLib.idle_add(lambda: self.focus_play() and False)
        self.sync_mode_header()
        (self.add_css_class if art else self.remove_css_class)('in-now')
        (self.add_css_class if mini else self.remove_css_class)('in-mini')
        for button in (self.timer_button, self.output_button, self.menu_button):
            button.set_visible(shown not in ('loading', 'mini'))
        self.sync_mode_header()
        if not in_now:
            self.sheet.set_open(False)
        self.kick_idle()

    def apply_layout(self):
        self.listen.fit_bar()
        self.mini_player.add_css_class('narrow') if self.mini_layout else self.mini_player.remove_css_class('narrow')
        self.show_page(self.page_name)
        if self.mini_layout:
            self.mini_player.update(self.snapshot) if self.snapshot else None

    def show_rooms(self):
        self.browse.set_visible_child_name('rooms')
        self.show_page('browse')

    def show_listen(self):
        if self.page_name == 'now':
            self.listen_origin = 'now'
        elif self.page_name == 'browse' and self.browse.get_visible_child_name() == 'rooms':
            self.listen_origin = 'rooms'
        self.browse.set_visible_child_name('listen')
        self.show_page('browse')

    def listen_visibility(self):
        """Leaving Listen by any route (a tab, Back, a shortcut, the mini layout) ends ambience mode,
        so the next Listen or Music › Change… offers music again."""
        shown = self.pages.get_visible_child_name() == 'browse' and self.browse.get_visible_child_name() == 'listen'
        if self.listen_shown and not shown:
            self.listen.end_ambience()
        self.listen_shown = shown
        self.sync_mode_header()

    def set_mode_title(self, title):
        """Listen's mode header: the words, or None when Listen is back to music."""
        self.mode_words = title or ''
        self.mode_title.set_label(title or '')
        self.mode_title.update_property([Gtk.AccessibleProperty.LABEL], [title or 'Choose ambience'])
        self.sync_mode_header()

    def sync_mode_header(self):
        """The header's own state while Browse shows. Choosing an ambience replaces the Rooms | Listen switcher
        with a back arrow and "Choose ambience · room", so the way out is one obvious control. In the narrow
        layout the switcher is a bar at the bottom instead of a header title that gets cut off."""
        listen = getattr(self, 'listen', None)
        browse = self.pages.get_visible_child_name() == 'browse'
        showing = (listen is not None and listen.mode == 'ambience' and browse
                   and self.browse.get_visible_child_name() == 'listen')
        self.mode_back.set_visible(showing)
        # Narrow: the mode word alone (the page below still names the room), at a size that fits beside Back.
        words = getattr(self, 'mode_words', '')
        self.mode_title.set_label(words.split(' · ')[0] if self.mini_layout else words)
        (self.mode_title.add_css_class if self.mini_layout else self.mode_title.remove_css_class)('mode-narrow')
        wanted = self.mode_title if showing else self.switcher
        if self.header.get_title_widget() is not wanted:
            self.header.set_title_widget(wanted)
        self.switcher.set_visible(browse and not self.mini_layout and not showing)
        if showing and self.mini_layout:
            # Below 560 px the header's own buttons leave the mode title room to be read.
            for button in (self.timer_button, self.output_button, self.menu_button):
                button.set_visible(False)
        elif self.mode_buttons_hidden:
            for button in (self.timer_button, self.output_button, self.menu_button):
                button.set_visible(self.pages.get_visible_child_name() not in ('loading', 'mini'))
        self.mode_buttons_hidden = bool(showing and self.mini_layout)
        self.switcher_bar.set_reveal(browse and self.mini_layout and not showing)

    def show_now(self):
        if not self.snapshot.get('active') and not self.snapshot.get('video'):
            self.toast('You are not in a room yet')
            return
        self.show_page('now')
        self.focus_play()

    def browse_rooms(self, player=None):
        """Browse rooms from a mini player: Rooms itself, even in the narrow layout; from the separate
        mini window, the full window on Rooms."""
        if player is not None and player is not self.mini_player:
            self.expand()
        self.browse_narrow = True
        self.show_rooms()

    def go_back(self):
        """Back (and Esc) in Now: to where you came from, Rooms or Listen."""
        if self.now_origin == 'listen':
            self.listen_origin = None
            self.browse.set_visible_child_name('listen')
            self.show_page('browse')
        else:
            self.show_rooms()

    def listen_back(self):
        """Esc in Listen with nothing left to clear: back to Now or Rooms, wherever Listen was opened from."""
        origin, self.listen_origin = self.listen_origin, None
        if origin == 'now' and self.snapshot.get('active'):
            self.show_now()
            return True
        if origin == 'rooms':
            self.show_rooms()
            return True
        return False

    def focus_play(self):
        """Keyboard focus on the room's play control (Space then pauses it); False when it cannot take it."""
        if self.closed or self.current_page() not in ('now', 'mini'):
            return False
        target = self.mini_player.play if self.current_page() == 'mini' else self.play
        if target.get_mapped() and target.get_sensitive():
            return target.grab_focus()
        self.set_focus(None)   # never leave focus on a control that has gone, a toast or Back
        return False

    def current_page(self):
        return self.pages.get_visible_child_name()

    def open_mix(self):
        self.sheet.set_open(True)

    def layer_options(self, layer):
        """What Change… offers on the Scene row: every offline room and view, with its picture; ones whose files
        are missing are listed as unavailable. Music and Ambience open Listen."""
        return [{'id': preset['id'], 'name': preset['name'].split(' · ')[0], 'poster': preset.get('poster_path'),
                 'available': preset.get('available', True),
                 'command': ('layer', {'layer': 'picture', 'action': 'select', 'preset_id': preset['id']})}
                for preset in self.presets if preset.get('kind') == 'local']

    def choose_other(self, layer):
        """Music and Ambience are chosen in Listen (Ambience in its own mode); Scene on its Mix row."""
        if layer == 'music':
            self.sheet.set_open(False)
            self.listen.end_ambience()
            self.show_listen()
        elif layer == 'atmosphere':
            origin = 'mix' if self.sheet.get_open() else 'now'
            self.sheet.set_open(False)
            self.listen.begin_ambience(origin)
        else:
            self.sheet.set_open(True)
            GLib.timeout_add(300, lambda: self.mix.rows[layer].change.popup() or False)

    # ---- commands -------------------------------------------------------------

    def command(self, action, args=None, done=None, urgent=False, quiet=False, failed=None):
        """Send a controller command off the GTK thread; ``failed(message)`` replaces the default toast.

        Returns a Ticket: a control that started the command re-enables itself from ``add_finish``."""
        if self.closed:
            return None
        ticket = Ticket()
        finish = ticket.finish
        run_async(lambda: session_client.request(action, args or {}, 5),
                  lambda result: self.succeeded(result, done, finish),
                  lambda error: self.failed(error_text(error), quiet, failed, finish),
                  self.urgent if urgent else self.executor)
        return ticket

    def succeeded(self, result, done, finish=None):
        if not self.closed:
            if finish:
                finish()
            if done:
                done(result)
            self.poll()
        return False

    def failed(self, error, quiet=False, handler=None, finish=None):
        self.want_now = False
        if finish:
            finish()
        if not self.closed and handler:
            handler(error)
        elif not self.closed and not quiet:
            self.error_toast(error)
        return False

    def layer_command(self, action, args):
        self.command(action, args, urgent=args.get('action') in ('stop', 'pause'))

    def send(self, action, args=None, **kwargs):
        self.command(action, args, **kwargs)

    def return_room(self):
        return self.start('return', {}, 'your room')

    def toggle_room(self):
        return self.actions.toggle()

    def next_track(self):
        self.command('layer', {'layer': 'music', 'action': 'next'})

    def feedback(self, rating):
        self.command('station.feedback', {'rating': rating},
                     done=lambda _r: self.toast('Noted: ' + rating + ' like this'))

    def try_again(self):
        layer = self.banner_layer
        if layer:
            self.command('layer', {'layer': layer, 'action': 'retry'})

    def leave_room(self):
        self.actions.leave()

    def volume_changed(self, scale):
        if self.updating:
            return
        if self.volume_timer:
            GLib.source_remove(self.volume_timer)

        def send():
            self.volume_timer = None
            self.command('room_volume', {'value': round(scale.get_value(), 1)}, quiet=True)
            return False
        self.volume_timer = GLib.timeout_add(160, send)

    def show_room_volume(self, snapshot):
        stored = snapshot.get('room_volume')
        if isinstance(stored, (int, float)) and not isinstance(stored, bool) and self.volume_timer is None:
            self.updating = True
            self.volume.set_value(float(stored))
            self.updating = False

    def nudge_volume(self, step):
        self.volume.set_value(max(0, min(100, self.volume.get_value() + step)))

    def choose_timer(self, kind):
        self.actions.timer(kind)

    # ---- Output -------------------------------------------------------------

    def load_outputs(self):
        import audio_devices

        def get():
            try:
                system = hub.system_volume().get('percent')
            except (hub.HubError, OSError, ValueError, KeyError):
                system = None
            return audio_devices.outputs(), system

        def apply(value):
            self.outputs = value[0]
            preferred = (self.snapshot.get('settings') or {}).get('preferred_output')
            self.output_popover.show_outputs(self.outputs, preferred, value[1])

        run_async(get, apply, lambda _error: self.output_popover.show_outputs([], None, None), self.executor)

    def choose_output(self, name):
        previous = (self.snapshot.get('settings') or {}).get('preferred_output')
        text = next((item.get('description') or item['name'] for item in self.outputs if item['name'] == name), 'System output')
        self.command('settings', {'values': {'preferred_output': name}},
                     done=lambda _r: self.toast('Output: ' + text, 'Undo',
                                                lambda: self.command('settings', {'values': {'preferred_output': previous}})))

    def test_output(self):
        import audio_devices
        preferred = (self.snapshot.get('settings') or {}).get('preferred_output')
        self.run_local(lambda: audio_devices.test_sound(preferred), 'Playing a quiet test sound')

    def set_system_volume(self, value):
        self.run_local(lambda: hub.set_volume(value, master=True), None)

    def run_local(self, work, message):
        run_async(work, (lambda _result: self.toast(message)) if message else None, self.error_toast, self.executor)

    # ---- dialogs and toasts -------------------------------------------------------

    def toast(self, title, undo_label=None, undo=None, urgent=False):
        """A toast; ``urgent`` ones (refusals, failures) show at once instead of queueing behind another."""
        toast = Adw.Toast(title=title, timeout=5)
        if urgent:
            toast.set_priority(Adw.ToastPriority.HIGH)
        if undo_label and undo:
            toast.set_button_label(undo_label)
            toast.connect('button-clicked', lambda *_: undo())
            # A toast with an action keeps Now's controls (and so the toast's neighbours) from fading out under it.
            self.action_toasts += 1
            toast.connect('dismissed', lambda *_: self.action_toast_gone())
            self.kick_idle()
        self.toasts.add_toast(toast)

    def action_toast_gone(self):
        self.action_toasts = max(0, self.action_toasts - 1)
        self.kick_idle()

    def error_toast(self, error):
        """A failure in plain words, at once; the raw cause only behind Details."""
        raw = model.error_details(error)
        self.toast(model.plain_error(error), 'Details' if raw else None,
                   (lambda: self.details_dialog(model.plain_error(error), raw)) if raw else None, urgent=True)

    def details_dialog(self, heading, raw):
        dialog = Adw.AlertDialog(heading=heading, body=raw)
        dialog.set_body_use_markup(False)
        dialog.add_response('close', 'Close')
        dialog.present(self)

    def save_dialog(self):
        self.rooms.save_dialog()

    def shortcuts_dialog(self):
        dialog = Adw.ShortcutsDialog()
        for page, items in SHORTCUT_PAGES:
            section = Adw.ShortcutsSection(title=page)
            for accel, _key, text in items:
                section.add(Adw.ShortcutsItem(title=text, accelerator=accel))
            dialog.add(section)
        dialog.present(self)

    def open_preferences(self, service=None):
        """Preferences; with a service name, straight to Connections and that service's page."""
        import next_prefs
        existing = getattr(self, 'preferences', None)
        if existing is not None and not existing.closed and existing.get_root() is not None:
            dialog = existing
        else:
            dialog = self.preferences = next_prefs.PreferencesDialog(self)
        dialog.present(self)
        if service:
            dialog.show_connection(service)

    def return_from_listen(self, origin):
        """Back to where a Listen choice began: the Mix over Now, or Now itself."""
        if not self.snapshot.get('active'):
            return
        self.show_now()
        if origin == 'mix':
            self.sheet.set_open(True)

    def open_track_details(self, reference):
        """Track details for one library reference; Listen's row menus call this."""
        import next_track_details
        dialog = next_track_details.TrackDetailsDialog(self, reference)
        self.track_details = (getattr(self, 'track_details', []) + [dialog])[-4:]
        dialog.present(self)

    def about_dialog(self):
        Adw.AboutDialog(application_name='Listening room', developer_name='Kotona',
                        comments='A quiet room for the radio and sounds you keep on.').present(self)

    def open_players(self):
        import next_players
        dialog = next_players.PlayersDialog(self)
        dialog.present(self)

    def open_mini(self):
        """Ctrl+M: the mini player in its own small window; the main window is left alone."""
        if self.mini_window is not None:
            self.mini_window.present()
            return
        self.mini_window = MiniWindow(self.get_application(), self)
        self.mini_window.connect('close-request', self.mini_closed)
        self.mini_window.player.update(self.snapshot) if self.snapshot else None
        self.mini_window.present()

    def mini_closed(self, window):
        window.player.close()
        self.mini_window = None
        if not self.get_visible():
            self.on_close()
            self.destroy()
        return False

    def expand(self):
        """Open the full window from a mini player."""
        if self.mini_layout:
            self.set_default_size(*self.full_size)
        elif not self.get_visible():
            self.set_visible(True)
        self.present()
        if self.mini_window is not None:
            self.mini_window.close()

    # ---- keyboard -------------------------------------------------------------------

    @staticmethod
    def shortcut_accels(prefix):
        return shortcuts_for(prefix)

    def install_actions(self):
        accels = shortcuts_for('win.')
        for name, callback in (('show-rooms', self.show_rooms), ('show-listen', self.show_listen),
                               ('show-now', self.show_now), ('preferences', self.open_preferences),
                               ('mini-player', self.open_mini), ('shortcuts', self.shortcuts_dialog),
                               ('toggle-room', self.toggle_room)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda *_, cb=callback: cb())
            self.add_action(action)
            if name in accels:
                self.get_application().set_accels_for_action('win.' + name, accels[name])
        captured = Gtk.EventControllerKey()
        captured.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        captured.connect('key-pressed', self.key_captured)
        self.add_controller(captured)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key_pressed)
        self.add_controller(keys)

    def key_pressed(self, _controller, keyval, _code, state):
        self.kick_idle()
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval == Gdk.KEY_Escape:
            if self.sheet.get_open():
                self.sheet.set_open(False)
                return True
            if self.current_page() == 'now':
                self.go_back()
                return True
            if self.current_page() == 'browse' and self.browse.get_visible_child_name() == 'listen':
                return self.listen_back()
            return self.rooms.close_preview()
        # Space and the arrows normally stop in the capture phase; the same rules apply when they arrive here.
        return self.key_captured(_controller, keyval, _code, state)

    def key_captured(self, _controller, keyval, _code, state):
        """Space plays or pauses and Up/Down set the room volume, before any focused button sees them.

        In Now and the mini layout always; at Rooms only while nothing or the hero button has focus
        (as at launch). Never while a text entry, a slider, a popover or a dialog has focus."""
        if keyval not in (Gdk.KEY_space, Gdk.KEY_Up, Gdk.KEY_Down):
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        if not self.snapshot.get('active') or self.offline or self.get_visible_dialog() is not None:
            return False
        focus = self.get_focus()
        node = focus
        while node is not None:
            if isinstance(node, (Gtk.Editable, Gtk.Range, Gtk.Popover, Gtk.DropDown)):
                return False
            node = node.get_parent()
        page = self.current_page()
        if page in ('now', 'mini') and not self.sheet.get_open():
            self.kick_idle()
            if keyval == Gdk.KEY_space:
                if self.play.get_sensitive() or page == 'mini':
                    self.toggle_room()
                return True
            (self.mini_player if page == 'mini' else self).nudge_volume(5 if keyval == Gdk.KEY_Up else -5)
            return True
        if page == 'browse' and keyval == Gdk.KEY_space and (focus is None or focus is self.hero_button):
            self.toggle_room()
            return True
        return False

    # ---- idle fade ------------------------------------------------------------------------

    def kick_idle(self):
        if self.idle_timer:
            GLib.source_remove(self.idle_timer)
            self.idle_timer = None
        self.set_cursor_from_name(None)
        self.controls.set_opacity(1)
        self.header.set_opacity(1)
        if self.current_page() == 'now' and self.fades():
            self.idle_timer = GLib.timeout_add(IDLE_MS, self.fade_out)

    def fades(self):
        """No idle fade under reduced motion, when it is turned off, with Mix open, or while a banner shows."""
        settings = self.snapshot.get('settings') or {}
        return (self.system_animations and not settings.get('reduced_motion') and not settings.get('keep_controls')
                and not self.sheet.get_open() and not self.banner.get_visible() and not self.action_toasts
                and not any(button.get_active() for button in (self.timer_button, self.output_button, self.menu_button)))

    def fade_out(self):
        self.idle_timer = None
        if self.current_page() == 'now' and self.fades():
            self.controls.set_opacity(0)
            self.header.set_opacity(0)
            self.set_cursor_from_name('none')
        return False

    # ---- polling and lifecycle -----------------------------------------------------------------

    def adapt(self, widget, _clock):
        width = widget.get_width()
        compact = width < BREAKPOINT
        if compact != widget.has_css_class('compact'):
            (widget.add_css_class if compact else widget.remove_css_class)('compact')
        self.fit_controls(width)
        self.place_toasts()
        self.place_pending()
        return GLib.SOURCE_CONTINUE

    def place_pending(self):
        """Now's cards (the start card, or a layer's failure banner) never sit over the room's name and chip
        ("Connecting"): a card moves up into the space above them, and in a window too short even for that, the
        name and chip step aside while the card says the same thing. Measured rather than read from allocations,
        and redone only when its inputs change."""
        overlay = self.now_overlay
        card = self.pending_card if self.pending_card.get_visible() else self.banner if self.banner.get_visible() else None
        words = (self.pending_title, self.pending_detail, self.banner_title, self.banner_detail)
        # While a card shows, what Now's text says (and which of its rows show) moves the room for it.
        text_rows = (self.room_name, self.now_line, self.now_detail, self.chip, self.changed_chip, self.choose_scene)
        key = (card, self.offline, overlay.get_width(), overlay.get_height(),
               card is not None and tuple(label.get_text() for label in words),
               card is not None and tuple((row.get_visible(), getattr(row, 'get_text', row.get_label)())
                                          for row in text_rows))
        if key == self.pending_key or not overlay.get_height():
            return
        self.pending_key = key
        top = 140 if self.offline else 70     # below the offline banner when it shows
        for other in (self.pending_card, self.banner):
            if other is not card:
                other.set_margin_top(top)
        self.now_text.set_visible(True)       # measured as shown: a hidden box measures as nothing
        if card is None:
            return
        width, height = overlay.get_width(), overlay.get_height()
        card_width = min(card.measure(Gtk.Orientation.HORIZONTAL, -1)[1], width - 32)
        card_height = card.measure(Gtk.Orientation.VERTICAL, card_width)[1]
        column_width = width - self.now_column.get_margin_start() - self.now_column.get_margin_end()
        controls = self.controls.measure(Gtk.Orientation.VERTICAL, column_width)[1]
        text = self.now_text.measure(Gtk.Orientation.VERTICAL, column_width)[1]
        text_top = height - self.now_column.get_margin_bottom() - controls - self.now_column.get_spacing() - text
        margin = min(top, text_top - CARD_GAP - card_height)
        fits = margin >= 8
        card.set_margin_top(margin if fits else top)
        self.now_text.set_visible(fits)

    def fit_controls(self, width):
        """Now's control row at medium widths: drop the Mix summary, then shorten the volume slider, so the
        play button never overlaps the volume icon."""
        tier = 'wide' if width >= 900 else 'medium' if width >= 720 else 'narrow'
        if tier == getattr(self, 'controls_tier', None):
            return
        self.controls_tier = tier
        self.mix_summary.set_visible(tier == 'wide')
        self.volume.set_size_request({'wide': 170, 'medium': 120, 'narrow': 84}[tier], -1)

    def place_toasts(self):
        """Toasts sit where they cover no control: above Now's control row or the open Mix, above the mini
        layout's controls and Listen's now-playing bar, and at the bottom elsewhere.

        Recomputed only when what it depends on changes, not on every frame. The open Mix's margin uses
        its natural height for the current width, and stays TOAST_ROOM short of the window's height:
        a margin that followed the height without that bound would grow the window through the toast's
        own size request."""
        page = self.current_page()
        sheet = page == 'now' and self.sheet.get_open()
        listen_bar = (page == 'browse' and self.browse.get_visible_child_name() == 'listen'
                      and self.listen.bar.get_visible())
        text = self.now_text.compute_bounds(self.toasts) if page == 'now' and self.now_text.get_visible() else (False, None)
        text_top = round(text[1].get_y()) if text[0] else None
        key = (page, sheet, listen_bar and self.listen.bar.get_height(), self.get_width(), self.get_height(), text_top)
        if key == self.toast_key:
            return
        self.toast_key = key
        if sheet:
            mix = self.mix.measure(Gtk.Orientation.VERTICAL, self.get_width())[1]
            margin = max(0, min(mix, self.get_height() - TOAST_ROOM)) + TOAST_GAP
        elif page == 'now':
            # Above the room's name and now-playing lines (and so above the controls under them), never over them.
            margin = NOW_CONTROLS_CLEARANCE
            if text_top is not None:
                margin = max(margin, min(self.toasts.get_height() - text_top + TOAST_GAP, self.get_height() - TOAST_ROOM))
        elif page == 'mini':
            margin = MINI_CONTROLS_CLEARANCE
        elif listen_bar:
            margin = self.listen.bar.get_height() + TOAST_GAP
        else:
            margin = TOAST_GAP
        if margin != self.toast_margin:
            self.toast_margin = margin
            self.toast_provider.load_from_string('.next toast { margin-bottom: %dpx; }' % margin)

    def load_static(self):
        import ambience
        import room_catalogue

        def get():
            return room_catalogue.rooms(), ambience.presets(), [s for s in hub.sources()]

        def apply(value):
            self.catalogue, self.presets, self.radio = value
            self.cards_key = self.sources_key = self.room_key = None
            if self.snapshot:
                self.render(self.snapshot)
        run_async(get, apply, None, self.executor)
        return False

    # ---- snapshots: pushed while the stream is up, polled otherwise ------------------------------

    def subscribe(self):
        """Take snapshots from the controller's stream when it has one; polling stays as the fallback."""
        subscribe = getattr(session_client, 'subscribe', None)
        if subscribe is None:
            return
        try:
            self.stream = subscribe(lambda update: call_on_main(self.pushed, update))
        except Exception as error:     # no stream: the window polls, as with an older controller
            print('listening-room: no snapshot stream: %s' % error_text(error), file=sys.stderr)

    def pushed(self, update):
        """A snapshot (or an outage) from the stream, on the main loop."""
        if self.closed:
            return
        if not update.connected:
            self.stream_up = False
            self.poll()                 # the stream is down: read now, then every FALLBACK_POLL_MS
            return
        self.stream_up = self.stream_seen = True
        # A poll sent before this push may have read older state; its answer must not replace this one.
        self.push_mark = self.polls_sent
        self.show_snapshot(update.snapshot)

    def timed_poll(self):
        """The poll timer: nothing while the stream is up, every FALLBACK_POLL_MS while a stream that worked is
        down, and every POLL_MS with a controller that has no stream."""
        if self.closed:
            return False
        if self.stream_up:
            return True
        if self.stream_seen and (time.monotonic() - self.polled_at) * 1000 < FALLBACK_POLL_MS:
            return True
        return self.poll()

    def poll(self):
        if self.closed:
            return False
        if self.polling:
            self.poll_again = True      # the read in flight may predate what the caller just changed
            return True
        self.polling = True
        self.polled_at = time.monotonic()
        self.polls_sent += 1
        serial = self.polls_sent
        run_async(lambda: session_client.request('inspect', {}, 3),
                  lambda snapshot: self.render_poll(snapshot, None, serial),
                  lambda error: self.render_poll(None, error_text(error), serial),
                  self.poll_pool)
        return True

    def fresh_since(self, mark):
        """True once the shown snapshot was read by a poll sent after ``mark`` (a ``polls_sent`` value)."""
        return self.snapshot_poll > mark

    def inspect_then(self, callback):
        """Read fresh state off the GTK thread, then call ``callback(snapshot)`` (None when unreadable) on it."""
        def answer(snapshot):
            if not self.closed:
                callback(snapshot)
        run_async(lambda: session_client.request('inspect', {}, 3), answer, lambda _error: answer(None), self.poll_pool)

    def render_poll(self, snapshot, error, serial=0):
        self.polling = False
        if self.closed:
            return False
        if self.poll_again:
            self.poll_again = False
            GLib.idle_add(lambda: self.poll() and False)
        if error:
            if self.first:
                self.first = False
                self.show_rooms()
            self.set_offline(error)
            return False
        if self.offline:
            self.set_offline(None)      # the service answered, whatever this read is worth
        if serial and serial < self.snapshot_poll:
            return False        # an older read than the one shown never replaces it
        if serial and serial <= self.push_mark:
            # A pushed snapshot arrived after this read was sent: which is newer is unknown, so neither
            # replace it nor count as fresh; ask again, and the next read is sent after the push.
            GLib.idle_add(lambda: self.poll() and False)
            return False
        self.snapshot_poll = max(self.snapshot_poll, serial)
        self.show_snapshot(snapshot)
        return False

    def show_snapshot(self, snapshot):
        if self.offline:
            self.set_offline(None)
        self.render(snapshot)

    def set_offline(self, error):
        """While the room service does not answer: a persistent banner, and nothing that looks live."""
        offline = error is not None
        self.offline, self.offline_error = offline, error or ''
        self.offline_bar.set_reveal_child(offline)
        self.show_details(self.offline_details, model.error_details(error) or (error or ''))
        (self.add_css_class if offline else self.remove_css_class)('offline')
        for widget in (self.play, self.skip, self.less, self.more, self.volume, self.mix_button,
                       self.timer_button, self.mini_player.play, self.mini_player.volume, self.listen.bar_play,
                       self.listen.bar_volume):
            widget.set_sensitive(not offline and (widget is not self.play or not self.pending))
        # The banner sits over Now's top edge: stale layer news goes, and the start card moves below it.
        self.pending_key = None         # place_pending moves the start card below the banner
        if offline:
            self.banner.set_visible(False)
            self.marker.set_visible(False)
            self.chip.set_text('Not connected')
            self.chip.set_visible(True)
            self.now_line.set_text('The room service isn’t running')
            self.now_detail.set_visible(False)
        elif self.snapshot.get('active') and not self.pending:
            self.render_now(self.snapshot)

    def start_service(self):
        """Start again: the existing ensure_service path, which starts idle infrastructure and never playback."""
        button = self.offline_start
        if not button.get_sensitive():
            return
        button.set_sensitive(False)
        button.set_label('Starting…')
        run_async(session_client.ensure_service, lambda _result: self.service_started(None), self.service_started,
                  self.executor)

    def service_started(self, error):
        if self.closed:
            return
        self.offline_start.set_sensitive(True)
        self.offline_start.set_label('Start again')
        if error is not None:
            self.show_details(self.offline_details, str(error))
            self.toast('The room service still isn’t running.', urgent=True)
            return
        self.poll()

    def render(self, snapshot):
        """Draw a snapshot. Each surface draws only when something it reads changed since the last draw;
        drawing the same snapshot again (static data arrived) redraws everything."""
        changed = model.snapshot_changes(self.rendered, snapshot)
        playing_changed = model.now_changes(changed, self.rendered, snapshot)
        self.snapshot = self.rendered = snapshot
        settings = snapshot.get('settings') or {}
        fading = (bool(settings.get('keep_controls')), bool(settings.get('reduced_motion')))
        if fading != getattr(self, 'fading_settings', fading):
            self.kick_idle()  # controls dimmed under the old setting come back now
        self.fading_settings = fading
        # Reduced motion means no animation at all in this window; audio fades stay.
        animations = bool(self.system_animations and not settings.get('reduced_motion'))
        gtk_settings = Gtk.Settings.get_default()
        if bool(gtk_settings.get_property('gtk-enable-animations')) != animations:
            gtk_settings.set_property('gtk-enable-animations', animations)
        generation = snapshot.get('config_generation')
        if generation is not None and generation != self.config_generation:
            # A library was connected or a radio favourite saved: reload them (off the GTK thread).
            known, self.config_generation = self.config_generation, generation
            if known is not None:
                self.load_static()
                self.listen.config_changed()
        self.guard('rooms', self.rooms.render, snapshot, changed)
        self.guard('listen', self.listen.update, snapshot, changed)
        self.guard('watching', self.watching.update, snapshot)
        active = snapshot.get('active')
        if not active:
            self.show_room_volume(snapshot)     # the slider never reads 0 while the stored volume is not 0
        for item in self.menu_room_items:
            item.set_visible(bool(active))
        self.timer_button.set_sensitive(bool(active) and not self.offline)
        self.timer_button.set_tooltip_text('Timer' if active else 'Timer: enter a room first')
        if self.first:
            self.first = False
            if model.launch_view(snapshot) == 'rooms':
                self.show_rooms()
                if model.hero(snapshot, self.catalogue)['kind'] == 'first-run':
                    self.rooms.focus_first_card_soon()
                else:
                    GLib.idle_add(lambda: (self.hero_button.get_mapped() and self.hero_button.grab_focus()) and False)
            else:
                self.show_page('now')
        elif self.want_now and active and not snapshot.get('transition'):
            self.want_now = False
            self.show_page('now')
        elif self.want_now and not active and not snapshot.get('transition'):
            if time.monotonic() - self.want_since > START_GRACE_S:
                self.want_now = False
                if (snapshot.get('errors') or {}).get('music'):
                    self.error_toast(snapshot['errors']['music'])
        if self.due('mini', playing_changed, self.catalogue, self.presets):
            self.guard('mini', self.mini_player.update, snapshot)
        if self.mini_window is not None and self.due('mini-window', playing_changed, self.mini_window, self.catalogue, self.presets):
            self.guard('mini-window', self.mini_window.player.update, snapshot)
        pending = self.pending
        if pending and snapshot.get('transition'):
            pending['seen_transition'] = True
        if pending and pending['state'] == 'connecting' and pending['done'] and not snapshot.get('transition'):
            # A start that ended without a room (from idle) or with the old room kept is a failure: keep the card
            # on Now, named after the room tried, with Try again and Back. Never fall back to Rooms silently.
            outcome = model.start_outcome(snapshot, pending)
            if outcome and outcome[0] == 'failed':
                self.pending_failed(outcome[1], pending, model.start_error(outcome[1]))
            elif outcome:
                self.clear_pending()
                pending = None
        now_state = (pending and pending['state'], self.offline, self.listen_first, self.catalogue, self.presets)
        if pending:
            self.guard('pending', self.render_pending) if pending['state'] == 'failed' else None
            if active and pending['state'] == 'failed' and self.due('now', playing_changed, *now_state):
                self.guard('now', self.render_now, snapshot)
        elif active:
            if self.due('now', playing_changed, *now_state):
                self.guard('now', self.render_now, snapshot)
        elif self.current_page() == 'now' and not snapshot.get('video'):
            self.show_rooms()          # with a video playing and no room, Now shows Watching (next_watching)
        if not active and not self.pending:
            self.listen_first = False
        return False

    def due(self, section, changed, *local):
        """Whether a playing-room surface must draw: a snapshot key it reads changed (``changed`` None: all), or
        the window state it also draws from (``local``) is not what it last drew. Counts the draws it allows."""
        if not model.touches(changed, ignores=model.NOW_IGNORES) and self.drawn.get(section) == local:
            return False
        self.drawn[section] = local
        self.rebuilds[section] += 1
        return True

    def watch_surface(self, *_):
        """Follow the toplevel's state, once, so the scene pauses when it is hidden."""
        surface = self.surface or self.get_surface()
        if surface is not None:
            self.surface = surface
            surface.connect('notify::state', self.surface_state)

    def surface_state(self, surface, *_):
        hidden = Gdk.ToplevelState.MINIMIZED | Gdk.ToplevelState.SUSPENDED
        self.scene._visibility(minimized=bool(surface.get_state() & hidden))

    def on_close(self, *_):
        if self.mini_window is not None and not self.closed:
            self.set_visible(False)  # the mini player keeps the session in view; closing it ends the app
            return True
        self.closed = True
        self.mini_player.close()
        for source in (self.poll_source, self.volume_timer, self.idle_timer):
            if source:
                GLib.source_remove(source)
        self.listen.close()
        self.scene.close()
        if self.stream is not None:
            self.stream.close()
        for workers in (self.executor, self.urgent, self.poll_pool):
            workers.shutdown(wait=False, cancel_futures=True)
        return False


def launch_arguments(arguments):
    """(mode, link) for a launch: mode is 'browse', 'mini' or 'inside'. `--desk` is the old name of
    the mini player; `--ui=next` is accepted and ignored. Raises ValueError for anything else."""
    import argparse
    import urllib.parse

    class Quiet(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError(message)
    parser = Quiet(description='Your desktop listening room', add_help=False)
    parser.add_argument('--mini', '--desk', dest='mini', action='store_true')
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--preview-link')
    parser.add_argument('--ui', choices=('next',))
    parser.add_argument('uri', nargs='?')
    args = parser.parse_args(arguments)
    link = args.preview_link
    if args.uri:
        uri = urllib.parse.urlsplit(args.uri)
        if uri.scheme == 'listening-room' and uri.netloc == 'open':
            link = urllib.parse.parse_qs(uri.query).get('url', [''])[0]
        else:
            link = args.uri
    return ('mini' if args.mini else 'inside' if args.inside else 'browse'), link


class NextApp(Adw.Application):
    def __init__(self):
        choose_renderer()
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        hold_main_context()     # frames from playing scene loops reach Python on this thread only, even at exit
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def do_activate(self):
        if self.window is None:
            self.window = NextWindow(self)
        if not self.window.get_visible():
            self.window.set_visible(True)
        self.window.present()

    def do_command_line(self, command_line):
        """A second launch (a URI, --preview-link, --desk, --inside) is delivered to the running window."""
        try:
            mode, link = launch_arguments(list(command_line.get_arguments())[1:])
        except (ValueError, SystemExit):
            command_line.printerr('listening-room: unknown option; see the runbook for --mini, --inside and --preview-link\n')
            return 1
        fresh = self.window is None
        if mode == 'mini':
            if fresh:
                self.window = NextWindow(self)  # the mini player alone; the main window opens from Expand
        else:
            self.activate()
        window = self.window
        if link:
            if fresh:
                window.listen.launch_link = link  # the first snapshot opens it
            else:
                window.listen.launch_link = link
                window.listen.launch_queued = True
                GLib.idle_add(window.listen.open_launch_link)
        if mode == 'mini':
            window.open_mini()
        elif mode == 'inside' and window.snapshot.get('active'):
            window.show_now()
        return 0


def run(argv):
    return NextApp().run(argv)
