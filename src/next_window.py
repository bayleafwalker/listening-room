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
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

import listening_hub as hub
import session_client
import next_model as model
from next_actions import RoomActions
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
SHORTCUTS = (('Space', 'Pause or play the room'), ('Ctrl+1', 'Rooms'), ('Ctrl+2', 'Listen'), ('Ctrl+3', 'Now'),
             ('Ctrl+F', 'Search Listen'), ('Ctrl+L', 'Add a link'), ('Ctrl+V', 'Add a link from the clipboard, outside text fields'),
             ('Ctrl+M', 'Mini player'), ('Ctrl+,', 'Preferences'),
             ('Esc', 'Close Mix or a dialog, then go back to browsing'), ('Up / Down', 'Room volume, in Now and the mini player'),
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
        self.back = icon_button('go-previous-symbolic', 'Back to rooms', self.show_rooms, ['header-pill'])
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
        self.go_to_now()
        self.command(*entry['args'])

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
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, valign=Gtk.Align.END,
                         margin_start=40, margin_end=40, margin_bottom=36)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.chip = label('', ['source-chip'])
        self.chip.set_halign(Gtk.Align.START)
        self.room_name = label('', ['room-name'], wrap=True)
        line = Gtk.Box(spacing=8)
        self.now_line = label('', ['now-line'], ellipsize=True)
        self.now_detail = label('', ['now-detail'], ellipsize=True)
        line.append(self.now_line)
        line.append(self.now_detail)
        for widget in (self.chip, self.room_name, line):
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
        text.append(self.banner_title)
        text.append(self.banner_detail)
        banner.append(text)
        self.banner_retry = Gtk.Button(label='Try again', valign=Gtk.Align.CENTER)
        self.banner_retry.connect('clicked', lambda *_: self.try_again())
        self.banner_choose = Gtk.Button(label='Choose another', valign=Gtk.Align.CENTER)
        self.banner_choose.connect('clicked', lambda *_: self.choose_other(self.banner_layer))
        banner.append(self.banner_retry)
        banner.append(self.banner_choose)
        self.banner_layer = None
        return banner

    def build_mix(self):
        self.mix = MixSheet(self.layer_command, self.choose_other, lambda: self.sheet.set_open(False), self.save_dialog,
                            self.layer_options)
        return self.mix

    def render_now(self, snapshot):
        info = model.now_model(snapshot)
        if info is None:
            return
        self.chip.set_text(info['chip'])
        self.room_name.set_text(info['name'])
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
        self.apply_accent(info['accent'])
        shown = (failed and failed['layer'], tuple(item['kind'] for item in info['timers']), info['state'])
        if shown != getattr(self, 'shown', None):
            self.shown = shown
            self.kick_idle()  # a change worth seeing brings the controls back
        motion = model.motion_allowed(snapshot, self.system_animations)
        assets = model.scene_assets(snapshot.get('active'), self.presets)
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
        """Show a page; below 560 px wide the mini layout stands in for every page but loading."""
        self.page_name = name
        mini = self.mini_layout and name != 'loading'
        shown = 'mini' if mini else name
        self.pages.set_visible_child_name(shown)
        in_now = shown == 'now'
        art = shown in ('now', 'mini')
        self.toolbar.set_extend_content_to_top_edge(art)
        self.back.set_visible(in_now)
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
        self.browse.set_visible_child_name('listen')
        self.show_page('browse')

    def show_now(self):
        if not self.snapshot.get('active'):
            self.toast('You are not in a room yet')
            return
        self.show_page('now')
        (self.mini_player.play if self.mini_layout else self.play).grab_focus()

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

    def command(self, action, args=None, done=None, urgent=False, quiet=False):
        if self.closed:
            return
        pool = self.urgent if urgent else self.executor
        future = pool.submit(session_client.request, action, args or {}, 5)

        def finished(f):
            try:
                result = f.result()
            except Exception as error:
                GLib.idle_add(self.failed, str(error) or type(error).__name__, quiet)
                return
            GLib.idle_add(self.succeeded, result, done)
        future.add_done_callback(finished)

    def succeeded(self, result, done):
        if not self.closed:
            if done:
                done(result)
            self.poll()
        return False

    def failed(self, error, quiet=False):
        self.want_now = False
        if not self.closed and not quiet:
            self.toast(error)
        return False

    def layer_command(self, action, args):
        self.command(action, args, urgent=args.get('action') in ('stop', 'pause'))

    def send(self, action, args=None, **kwargs):
        self.command(action, args, **kwargs)

    def return_room(self):
        self.go_to_now()
        self.command('return')

    def toggle_room(self):
        self.actions.toggle()

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
                GLib.idle_add(lambda: self.toast(str(error)) or False)
        future.add_done_callback(done)

    # ---- dialogs and toasts -------------------------------------------------------

    def toast(self, title, undo_label=None, undo=None):
        toast = Adw.Toast(title=title, timeout=5)
        if undo_label and undo:
            toast.set_button_label(undo_label)
            toast.connect('button-clicked', lambda *_: undo())
        self.toasts.add_toast(toast)

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
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key_pressed)
        self.add_controller(keys)

    def key_pressed(self, _controller, keyval, _code, state):
        self.kick_idle()
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        now = self.current_page() in ('now', 'mini')
        if keyval == Gdk.KEY_Escape:
            if self.sheet.get_open():
                self.sheet.set_open(False)
                return True
            if self.current_page() == 'now':
                self.show_rooms()
                return True
            return self.rooms.close_preview()
        if not now or self.sheet.get_open() or not self.snapshot.get('active'):
            return False
        if keyval == Gdk.KEY_space:
            self.toggle_room()
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            (self.mini_player if self.mini_layout else self).nudge_volume(5 if keyval == Gdk.KEY_Up else -5)
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
        compact = widget.get_width() < BREAKPOINT
        if compact != widget.has_css_class('compact'):
            (widget.add_css_class if compact else widget.remove_css_class)('compact')
        return GLib.SOURCE_CONTINUE

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

    def render_poll(self, snapshot, error):
        self.polling = False
        if self.closed:
            return False
        if error:
            if self.first:
                self.first = False
                self.show_page('browse')
            if not getattr(self, 'offline', False):
                self.toast('Room controller unavailable: ' + error)
            self.offline = True
            return False
        self.offline = False
        self.render(snapshot)
        return False

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
        self.rooms.render(snapshot)
        self.listen.update(snapshot)
        active = snapshot.get('active')
        for item in self.menu_room_items:
            item.set_visible(bool(active))
        if self.first:
            self.first = False
            self.show_rooms() if model.launch_view(snapshot) == 'rooms' else self.show_page('now')
        elif self.want_now and active and not snapshot.get('transition'):
            self.want_now = False
            self.show_page('now')
        elif self.want_now and not active and not snapshot.get('transition'):
            if time.monotonic() - self.want_since > START_GRACE_S:
                self.want_now = False
                if (snapshot.get('errors') or {}).get('music'):
                    self.toast(str(snapshot['errors']['music']))
        self.mini_player.update(snapshot)
        if self.mini_window is not None:
            self.mini_window.player.update(snapshot)
        if active:
            self.render_now(snapshot)
        elif self.current_page() == 'now':
            self.show_rooms()
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
