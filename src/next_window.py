"""The next Listening Room window (libadwaita): Rooms, Listen and Now.

It talks to the shared session controller only through session_client, never
touches players itself, and never sends a command that starts or changes
playback while you browse. Listen is provisional here; Rooms (next_rooms), Now, Mix,
Timer, Output, toasts and banners are the finished shell.
"""
import concurrent.futures
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
from next_mini import MiniPlayer, MiniWindow, install_mini_css
from next_mix import MixSheet
from next_rooms import RoomsPage
from next_widgets import (FocalPicture, choose_renderer, show_texture, OutputPopover, Scene, TimerPopover, accent_css, icon_button,
                          install_css, label, menu_item, name_it, pop_button)

APP_ID = 'app.kotona.ListeningRoom'
POLL_MS = 900
IDLE_MS = 4000
BREAKPOINT = 1000
MINI_WIDTH = 560  # below this the mini layout replaces the window
FULL_SIZE = (1000, 720)
START_GRACE_S = 8  # how long a start command may take before Now is abandoned
MUSIC_STARTS = ('music', 'online', 'link', 'station.start', 'station.replay')  # starts that replace a room's music
SHORTCUTS = (('Space', 'Pause or play the room'), ('Ctrl+1', 'Rooms'), ('Ctrl+2', 'Listen'), ('Ctrl+3', 'Now'),
             ('Ctrl+F', 'Search Listen'), ('Ctrl+L', 'Add a link'), ('Ctrl+V', 'Add a link from the clipboard, outside text fields'),
             ('Ctrl+M', 'Mini player'), ('Ctrl+,', 'Preferences'),
             ('Esc', 'Close Mix or a dialog, then go back to where you were'),
             ('Up / Down', 'Room volume, in Now and the mini player'),
             ('Ctrl+?', 'Keyboard shortcuts'))


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
        self.closed = self.updating = self.polling = False
        self.first = True
        self.want_now = False
        self.want_since = 0.
        self.pending = None        # a start in flight: {'name', 'art', 'state', 'done', 'since', ...}
        self.pending_waiters = []  # called when a pending start ends (controls re-enable themselves)
        self.now_origin = 'rooms'      # where Back and Esc in Now return to: 'rooms' or 'listen'
        self.listen_origin = None      # where Esc in Listen returns to: 'now', 'rooms' or None (stay)
        self.section_errors = {}       # surface -> the last exception its render raised
        self.offline = False           # the room service did not answer the last poll
        self.offline_error = ''
        self.config_generation = None  # library connections and favourites, as the controller last saw them
        self.browse_narrow = False     # Browse rooms was chosen in the narrow (mini) layout
        self.listen_first = False      # the active room began with Tune in, not with a room
        self.toast_margin = None
        self.room_key = self.cards_key = self.sources_key = None
        self.volume_timer = self.idle_timer = None
        self.selected_room = self.surface = None
        self.outputs, self.radio = [], []
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        self.urgent = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        display = Gdk.Display.get_default()
        install_css(display)
        install_mini_css(display)
        self.accent_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, self.accent_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.actions = RoomActions(self.command, self.toast, lambda: self.snapshot)
        self.build()
        self.install_actions()
        self.connect('close-request', self.on_close)
        self.connect('map', self.watch_surface)
        self.add_tick_callback(self.adapt)
        GLib.idle_add(self.load_static)
        self.poll_source = GLib.timeout_add(POLL_MS, self.poll)
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

    SECTION_WORDS = {'rooms': 'Rooms', 'listen': 'Listen', 'now': 'Now', 'mini': 'The mini player', 'pending': 'Now'}

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
        self.changed_chip.set_tooltip_text('The music differs from the saved room. Save it from the Mix.')
        chips.append(self.changed_chip)
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
                                  'origin': (self.page_name, self.browse.get_visible_child_name())}
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
        ticket = self.command(action, args, done=lambda result: (self.pending_done(pending), done(result) if done else None),
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

    def pending_done(self, pending):
        if pending is not self.pending:
            return
        pending.update(inflight=False, done=True)
        if pending['state'] == 'failed' and pending.get('timed_out'):
            pending['state'] = 'connecting'   # a slow start that did succeed: the next snapshot clears the card
            self.render_pending()

    def pending_failed(self, error, pending=None):
        pending = self.pending if pending is None else pending
        if pending is not self.pending or not pending:
            return
        pending.update(inflight=False, state='failed', error=model.plain_error(error), details=model.error_details(error),
                       timed_out=False)
        self.render_pending()

    def pending_expired(self, pending=None):
        pending = self.pending if pending is None else pending
        if pending is self.pending and pending and pending['state'] == 'connecting' \
                and time.monotonic() - pending['since'] >= START_GRACE_S:
            if pending['done']:
                self.clear_pending()      # the controller said yes; do not call a slow start a failure
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
        if not pending:
            self.play.set_sensitive(not self.offline)
            return
        if connecting:
            self.pending_title.set_text('Connecting to ' + pending['name'] + '…')
            self.pending_detail.set_text('Nothing plays until it is ready.')
            self.play.set_child(Gtk.Spinner(spinning=True))
            self.play.set_sensitive(False)
            self.room_name.set_text(pending['name'])
            self.chip.set_text('Connecting')
            self.changed_chip.set_visible(False)
            self.choose_scene.set_visible(False)
            self.now_line.set_text('')
            self.now_detail.set_visible(False)
            if pending.get('art'):
                self.scene.show_room({'still': pending['art'], 'poster': pending['art'], 'loop': None}, False)
        else:
            previous = (self.snapshot.get('active') or {}).get('name')
            self.pending_title.set_text('Couldn’t start ' + pending['name'])
            self.pending_detail.set_text(pending.get('error', '') + (' ' + previous + ' is still there.' if previous else ''))
            self.play.set_sensitive(not self.offline)
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
        # A room started from Listen is named after what plays, never "untitled".
        self.room_name.set_text(model.room_title(snapshot) if ad_hoc else info['name'])
        self.changed_chip.set_visible(model.room_changed(snapshot, self.catalogue))
        self.choose_scene.set_visible((ad_hoc or self.listen_first) and bool(self.layer_options('picture')))
        self.now_line.set_text(info['line'])
        self.now_detail.set_text('· ' + info['detail'] if info['detail'] else '')
        self.now_detail.set_visible(bool(info['detail']))
        self.mix_summary.set_text(info['summary'])
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
        assets = model.scene_assets(snapshot.get('active'), self.presets)
        if ad_hoc and not (assets.get('still') or assets.get('loop')):
            assets = model.fallback_scene(snapshot, self.presets, self.catalogue)
        self.scene.show_room(assets, motion)
        self.mix.update(snapshot)
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
        self.switcher.set_visible(shown == 'browse')
        (self.add_css_class if art else self.remove_css_class)('in-now')
        (self.add_css_class if mini else self.remove_css_class)('in-mini')
        for button in (self.timer_button, self.output_button, self.menu_button):
            button.set_visible(shown not in ('loading', 'mini'))
        if not in_now:
            self.sheet.set_open(False)
        self.kick_idle()

    def apply_layout(self):
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

    def show_now(self):
        if not self.snapshot.get('active'):
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
        """What Change… offers on the Scene row: offline rooms and views. Music and Ambience open Listen."""
        return [(preset['name'], ('layer', {'layer': 'picture', 'action': 'select', 'preset_id': preset['id']}))
                for preset in self.presets if preset.get('kind') == 'local' and preset.get('available', True)]

    def choose_other(self, layer):
        """Music and Ambience are chosen in Listen (Ambience in its own mode); Scene on its Mix row."""
        if layer == 'music':
            self.sheet.set_open(False)
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
        pool = self.urgent if urgent else self.executor
        future = pool.submit(session_client.request, action, args or {}, 5)

        def finished(f):
            try:
                result = f.result()
            except Exception as error:
                GLib.idle_add(self.failed, str(error) or type(error).__name__, quiet, failed, finish)
                return
            GLib.idle_add(self.succeeded, result, done, finish)
        future.add_done_callback(finished)
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

        future = self.executor.submit(get)

        def done(f):
            try:
                value = f.result()
            except Exception:
                GLib.idle_add(lambda: self.output_popover.show_outputs([], None, None) or False)
                return
            GLib.idle_add(lambda: apply(value) or False)
        future.add_done_callback(done)

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
        future = self.executor.submit(work)

        def done(f):
            try:
                f.result()
                if message:
                    GLib.idle_add(lambda: self.toast(message) or False)
            except Exception as error:
                GLib.idle_add(lambda error=error: self.error_toast(error) or False)
        future.add_done_callback(done)

    # ---- dialogs and toasts -------------------------------------------------------

    def toast(self, title, undo_label=None, undo=None, urgent=False):
        """A toast; ``urgent`` ones (refusals, failures) show at once instead of queueing behind another."""
        toast = Adw.Toast(title=title, timeout=5)
        if urgent:
            toast.set_priority(Adw.ToastPriority.HIGH)
        if undo_label and undo:
            toast.set_button_label(undo_label)
            toast.connect('button-clicked', lambda *_: undo())
        self.toasts.add_toast(toast)
        return toast

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
        dialog = Adw.AlertDialog(heading='Keyboard shortcuts',
                                 body='\n'.join('%s    %s' % pair for pair in SHORTCUTS))
        dialog.set_follows_content_size(True)
        dialog.add_response('close', 'Close')
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

    def install_actions(self):
        for name, accel, callback in (('show-rooms', '<Control>1', self.show_rooms),
                                      ('show-listen', '<Control>2', self.show_listen),
                                      ('show-now', '<Control>3', self.show_now),
                                      ('preferences', '<Control>comma', self.open_preferences),
                                      ('mini-player', '<Control>m', self.open_mini),
                                      ('shortcuts', '<Control>question', self.shortcuts_dialog),
                                      ('toggle-room', None, self.toggle_room)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda *_, cb=callback: cb())
            self.add_action(action)
            if accel:
                self.get_application().set_accels_for_action(
                    'win.' + name, [accel, '<Control><Shift>slash'] if name == 'shortcuts' else [accel])
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
                and not self.sheet.get_open() and not self.banner.get_visible()
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
        return GLib.SOURCE_CONTINUE

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
        """Toasts sit where they cover no control: above Now's control row or above the open Mix, above
        the mini layout's controls and Listen's now-playing bar, at the bottom elsewhere.

        Every margin comes from content that does not depend on the window's height: a margin that
        followed the height would grow the window through the toast's own size request."""
        page = self.current_page()
        if page == 'now':
            margin = self.mix.get_height() + 48 if self.sheet.get_open() else 140
        elif page == 'mini':
            margin = 96
        elif page == 'browse' and self.browse.get_visible_child_name() == 'listen' and self.listen.bar.get_visible():
            margin = self.listen.bar.get_height() + 12
        else:
            margin = 12
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
        future = self.executor.submit(get)

        def done(f):
            try:
                value = f.result()
            except Exception:
                return
            GLib.idle_add(lambda: apply(value) or False)
        future.add_done_callback(done)
        return False

    def poll(self):
        if self.closed:
            return False
        if self.polling:
            return True
        self.polling = True
        future = self.executor.submit(session_client.request, 'inspect', {}, 3)

        def done(f):
            try:
                GLib.idle_add(self.render_poll, f.result(), None)
            except Exception as error:
                GLib.idle_add(self.render_poll, None, str(error))
        future.add_done_callback(done)
        return True

    def inspect_then(self, callback):
        """Read fresh state off the GTK thread, then call ``callback(snapshot)`` (None when unreadable) on it."""
        future = self.executor.submit(session_client.request, 'inspect', {}, 3)

        def done(f):
            try:
                snapshot = f.result()
            except Exception:
                snapshot = None
            GLib.idle_add(lambda: (None if self.closed else callback(snapshot)) and False)
        future.add_done_callback(done)

    def render_poll(self, snapshot, error):
        self.polling = False
        if self.closed:
            return False
        if error:
            if self.first:
                self.first = False
                self.show_rooms()
            self.set_offline(error)
            return False
        if self.offline:
            self.set_offline(None)
        self.render(snapshot)
        return False

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
        self.pending_card.set_margin_top(140 if offline else 70)
        if offline:
            self.banner.set_visible(False)
            self.marker.set_visible(False)
            self.chip.set_text('Not connected')
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
        future = self.executor.submit(session_client.ensure_service)

        def finished(f):
            try:
                f.result()
                error = None
            except Exception as failure:
                error = failure
            GLib.idle_add(lambda: self.service_started(error) or False)
        future.add_done_callback(finished)

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
        self.snapshot = snapshot
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
        self.guard('rooms', self.rooms.render, snapshot)
        self.guard('listen', self.listen.update, snapshot)
        active = snapshot.get('active')
        for item in self.menu_room_items:
            item.set_visible(bool(active))
        self.timer_button.set_sensitive(bool(active) and not self.offline)
        self.timer_button.set_tooltip_text('Timer' if active else 'Timer: enter a room first')
        if self.first:
            self.first = False
            if model.launch_view(snapshot) == 'rooms':
                self.show_rooms()
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
        self.guard('mini', self.mini_player.update, snapshot)
        if self.mini_window is not None:
            self.guard('mini', self.mini_window.player.update, snapshot)
        pending = self.pending
        if pending and pending['state'] == 'connecting' and pending['done'] and not snapshot.get('transition'):
            self.clear_pending()
            pending = None
        if pending:
            self.guard('pending', self.render_pending) if pending['state'] == 'failed' else None
            if active and pending['state'] == 'failed':
                self.guard('now', self.render_now, snapshot)
        elif active:
            self.guard('now', self.render_now, snapshot)
        elif self.current_page() == 'now':
            self.show_rooms()
        if not active and not self.pending:
            self.listen_first = False
        return False

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
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.urgent.shutdown(wait=False, cancel_futures=True)
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
