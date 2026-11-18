"""Glance: the mini player and the Waybar popover.

One component, `MiniPlayer`, shows room art, name, the now-playing line, pause, room
volume, timer and expand. It fills the main window below 560 px wide, lives alone in
the `MiniWindow` that Ctrl+M (or the menu's Mini player) opens, and is the Waybar
left-click popover (`run_popover`, started by `listening-roomctl popover`), which adds
Break 10 min, Wind down 30 min and Leave room. Looking at it never changes playback;
only Pause, Play, Return, the volume, the timer choices and Leave room do.
"""
import concurrent.futures
import subprocess

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

import next_model as model
from next_actions import RoomActions
from next_widgets import FocalPicture, choose_renderer, show_texture, TimerPopover, accent_css, icon_button, install_css, label, name_it

MINI_CSS = '''
.mini { background: #0e0f12; border-radius: 16px; }
.mini .mini-scrim { background-image: linear-gradient(90deg, rgba(12,10,8,.94) 0%, rgba(12,10,8,.84) 55%, rgba(12,10,8,.6)),
  linear-gradient(0deg, rgba(12,10,8,.7), rgba(12,10,8,0) 70%); }
.mini .mini-name { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 26px; color: #ffffff; }
.mini.narrow .mini-name { font-size: 22px; }
.mini .mini-line { font-size: 13px; color: rgba(255,255,255,.9); }
.mini .mini-glass { background: rgba(18,18,22,.78); color: #ffffff; border-radius: 22px; border: 1px solid rgba(255,255,255,.14);
  min-height: 40px; min-width: 40px; padding: 0 12px; }
.mini .mini-glass:hover { background: rgba(60,60,66,.9); }
.mini .mini-play { background: #ffffff; color: #141416; border-radius: 24px; min-width: 48px; min-height: 48px; padding: 0; }
.mini .mini-play:hover { background: #e9e9ee; }
.mini .mini-return { background: #ffffff; color: #141416; border-radius: 24px; min-height: 48px; padding: 0 20px; font-weight: 600; }
.mini scale trough { min-height: 6px; background: rgba(255,255,255,.3); }
.mini scale highlight { background: #ffffff; }
.mini .mini-timer-label { font-weight: 600; font-size: 12px; color: #ffffff; }
.mini .mini-volume-icon { color: rgba(255,255,255,.92); }
.mini button:focus:focus-visible, .mini scale:focus:focus-visible, .mini menubutton > button:focus:focus-visible
  { outline: 3px solid #ffffff; outline-offset: -3px; }
.mini button.mini-play:focus:focus-visible, .mini button.mini-return:focus:focus-visible
  { outline: 3px solid #111114; outline-offset: -6px; }
window.mini-window { background: #0e0f12; }
'''

_mini_provider = None


def install_mini_css(display):
    global _mini_provider
    if _mini_provider is None:
        _mini_provider = Gtk.CssProvider()
        _mini_provider.load_from_string(MINI_CSS)
        Gtk.StyleContext.add_provider_for_display(display, _mini_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    return _mini_provider


class MiniPlayer(Gtk.Overlay):
    """The Glance component. `host` supplies actions (RoomActions), send(), expand(),
    return_room() and catalogue/presets for art."""
    def __init__(self, host, extras=False, closable=False):
        super().__init__()
        self.host, self.extras = host, extras
        self.add_css_class('mini')
        self.updating = False
        self.volume_timer = None
        self.state = None
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.art = FocalPicture((.7, .5))
        self.set_child(self.art)
        scrim = Gtk.Box()
        scrim.add_css_class('mini-scrim')
        self.add_overlay(scrim)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16, margin_bottom=16,
                         margin_start=20, margin_end=16)
        top = Gtk.Box(spacing=12, valign=Gtk.Align.START)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.name = label('Listening room', ['mini-name'], ellipsize=True)
        self.line = label('', ['mini-line'], ellipsize=True)
        text.append(self.name)
        text.append(self.line)
        top.append(text)
        self.expand_button = icon_button('view-fullscreen-symbolic',
                                         'Open the full window' if not extras else 'Open the room window',
                                         host.expand, ['mini-glass', 'circular'])
        self.expand_button.set_valign(Gtk.Align.START)
        top.append(self.expand_button)
        if closable:
            controls = Gtk.WindowControls(side=Gtk.PackType.END, valign=Gtk.Align.START)
            if extras:
                controls.set_decoration_layout(':close')  # a popover is closed, not minimised or maximised
            top.append(controls)
        column.append(top)
        column.append(Gtk.Box(vexpand=True))
        bottom = Gtk.Box(spacing=14, valign=Gtk.Align.END)
        self.play = Gtk.Button(icon_name='media-playback-pause-symbolic')
        self.play.add_css_class('mini-play')
        self.play.add_css_class('circular')
        self.play.connect('clicked', lambda *_: self.play_clicked())
        name_it(self.play, 'Pause the room')
        bottom.append(self.play)
        self.return_button = Gtk.Button(label='Return', visible=False)
        self.return_button.add_css_class('mini-return')
        self.return_button.connect('clicked', lambda *_: host.return_room())
        bottom.append(self.return_button)
        self.volume_box = Gtk.Box(spacing=10, hexpand=True)
        icon = Gtk.Image(icon_name='audio-volume-high-symbolic')
        icon.add_css_class('mini-volume-icon')
        self.volume_box.append(icon)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_hexpand(True)
        self.volume.get_adjustment().set_page_increment(5)
        name_it(self.volume, 'Room volume')
        self.volume.connect('value-changed', self.volume_changed)
        self.volume_box.append(self.volume)
        bottom.append(self.volume_box)
        self.timer_popover = TimerPopover(host.actions.timer)
        self.timer_button = Gtk.MenuButton(popover=self.timer_popover)
        self.timer_button.add_css_class('mini-glass')
        inner = Gtk.Box(spacing=6)
        inner.append(Gtk.Image(icon_name='alarm-symbolic'))
        self.timer_label = label('', ['mini-timer-label'])
        self.timer_label.set_visible(False)
        inner.append(self.timer_label)
        self.timer_button.set_child(inner)
        name_it(self.timer_button, 'Timer')
        bottom.append(self.timer_button)
        column.append(bottom)
        self.pills = Gtk.Box(spacing=8, visible=False)
        for text, callback in (('Break 10 min', lambda: host.actions.timer('break')),
                               ('Wind down 30 min', lambda: host.actions.timer('wind_down')),
                               ('Leave room', host.actions.leave)):
            pill = Gtk.Button(label=text)
            pill.add_css_class('mini-glass')
            pill.connect('clicked', lambda *_, cb=callback: cb())
            self.pills.append(pill)
        if extras:
            column.append(self.pills)
            self.timer_button.set_visible(False)
        self.add_overlay(column)

    # ---- behaviour ------------------------------------------------------

    def play_clicked(self):
        if self.state == 'return':
            self.host.return_room()
        else:
            self.host.actions.toggle()

    def volume_changed(self, scale):
        if self.updating:
            return
        if self.volume_timer:
            GLib.source_remove(self.volume_timer)

        def send():
            self.volume_timer = None
            self.host.send('room_volume', {'value': round(scale.get_value(), 1)}, quiet=True)
            return False
        self.volume_timer = GLib.timeout_add(160, send)

    def nudge_volume(self, step):
        self.volume.set_value(max(0, min(100, self.volume.get_value() + step)))

    def art_path(self, snapshot, active):
        if active:
            assets = model.scene_assets(active, getattr(self.host, 'presets', []) or [])
            return assets.get('still') or assets.get('poster')
        hero = model.hero(snapshot, getattr(self.host, 'catalogue', ()) or ())
        return hero.get('poster')

    def update(self, snapshot):
        info = model.now_model(snapshot)
        active = snapshot.get('active')
        self.updating = True
        try:
            path = self.art_path(snapshot, active)
            if path != getattr(self, 'art_key', object()):
                self.art_key = path
                show_texture(self.art, path)
            if info:
                self.state = info['state']
                self.name.set_text(info['name'])
                self.line.set_text(info['line'] + (' · ' + info['detail'] if info['detail'] else ''))
                self.play.set_icon_name('media-playback-pause-symbolic' if info['state'] == 'playing'
                                        else 'media-playback-start-symbolic')
                name_it(self.play, 'Pause the room' if info['state'] == 'playing' else 'Play the room')
                if self.volume_timer is None:
                    self.volume.set_value(float(info['room_volume']))
                running = info['timers']
                self.timer_label.set_visible(bool(running))
                self.timer_label.set_text(running[0]['label'] if running else '')
                name_it(self.timer_button, (running[0]['label'].replace(' · ', ' timer, ') + ' left. Open timer options')
                        if running else 'Timer', None)
                self.timer_popover.set_running(bool(running))
            else:
                hero = model.hero(snapshot, getattr(self.host, 'catalogue', ()) or ())
                self.state = 'return' if hero['kind'] == 'return' else 'none'
                self.name.set_text(hero['title'] if hero['kind'] == 'return' else 'Listening room')
                self.line.set_text(hero.get('detail') or ('Pick a room to begin' if hero['kind'] == 'first-run' else ''))
                self.return_button.set_label(hero.get('button', 'Return'))
                self.return_button.set_tooltip_text(hero.get('button', 'Return'))
            self.play.set_visible(bool(info))
            self.return_button.set_visible(self.state == 'return')
            for widget in (self.volume_box, self.timer_button if not self.extras else self.pills):
                widget.set_visible(bool(info))
            if not info:
                self.timer_label.set_visible(False)
        finally:
            self.updating = False
        return info

    def close(self):
        if self.volume_timer:
            GLib.source_remove(self.volume_timer)
            self.volume_timer = None


class MiniKeys:
    """Space pauses or plays, Up and Down change room volume, Esc closes the window."""
    @staticmethod
    def install(window, player, escape=None):
        keys = Gtk.EventControllerKey()

        def pressed(_c, keyval, _code, state):
            if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
                return False
            if keyval == Gdk.KEY_Escape and escape:
                escape()
                return True
            if player.state in ('playing', 'paused', 'idle') and keyval == Gdk.KEY_space:
                player.host.actions.toggle()
                return True
            if player.state in ('playing', 'paused', 'idle') and keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
                player.nudge_volume(5 if keyval == Gdk.KEY_Up else -5)
                return True
            return False
        keys.connect('key-pressed', pressed)
        window.add_controller(keys)


class MiniWindow(Adw.Window):
    """The mini player as its own small window (Ctrl+M, the Waybar popover)."""
    def __init__(self, app, host, extras=False, auto_close=False):
        super().__init__(application=app, title='Room controls' if extras else 'Mini player')
        self.add_css_class('next')
        self.add_css_class('mini-window')
        self.set_default_size(480, 250 if extras else 190)
        self.set_size_request(320, 150)
        install_css(Gdk.Display.get_default())
        install_mini_css(Gdk.Display.get_default())
        self.toasts = Adw.ToastOverlay()
        self.player = MiniPlayer(host, extras=extras, closable=True)
        self.toasts.set_child(Gtk.WindowHandle(child=self.player))
        self.set_content(self.toasts)
        MiniKeys.install(self, self.player, escape=self.close)
        self.was_active = False
        if auto_close:
            self.connect('notify::is-active', self.activity)

    def activity(self, *_):
        if self.is_active():
            self.was_active = True
        elif self.was_active and self.get_visible():
            self.close()

    def toast(self, title, undo_label=None, undo=None):
        toast = Adw.Toast(title=title, timeout=5)
        if undo_label and undo:
            toast.set_button_label(undo_label)
            toast.connect('button-clicked', lambda *_: undo())
        self.toasts.add_toast(toast)


class PopoverHost:
    """What the Waybar popover needs: its own polling, commands and toasts."""
    POLL_MS = 1000

    def __init__(self, app, request, launch=None):
        self.app = app
        self.request = request
        self.launch = launch or (lambda arguments: subprocess.Popen(['listening-room'] + arguments))
        self.snapshot = {}
        self.catalogue, self.presets = [], []
        self.closed = self.polling = self.offline = False
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.urgent = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.accent = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.accent,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
        self.actions = RoomActions(self.send, self.toast, lambda: self.snapshot)
        self.window = MiniWindow(app, self, extras=True, auto_close=True)
        self.window.connect('close-request', self.on_close)
        self.window.present()
        self.poll_source = GLib.timeout_add(self.POLL_MS, self.poll)
        GLib.idle_add(self.load_static)
        self.poll()

    def load_static(self):
        def get():
            import room_catalogue
            return room_catalogue.rooms()

        def done(f):
            try:
                value = f.result()
            except Exception:
                return
            GLib.idle_add(self.loaded, value)
        self.executor.submit(get).add_done_callback(done)
        return False

    def loaded(self, catalogue):
        self.catalogue = catalogue
        self.render(self.snapshot)
        return False

    def toast(self, title, undo_label=None, undo=None):
        if not self.closed:
            self.window.toast(title, undo_label, undo)

    def send(self, action, args=None, done=None, urgent=False, quiet=False):
        if self.closed:
            return
        future = (self.urgent if urgent else self.executor).submit(self.request, action, args or {}, 5)

        def finished(f):
            try:
                result = f.result()
            except Exception as error:
                if not quiet:
                    GLib.idle_add(lambda: self.toast(str(error) or type(error).__name__) or False)
                return
            GLib.idle_add(lambda: self.succeeded(result, done) or False)
        future.add_done_callback(finished)

    def succeeded(self, result, done):
        if not self.closed:
            if done:
                done(result)
            self.poll()

    def return_room(self):
        self.send('return')

    def expand(self):
        arguments = ['--inside'] if self.snapshot.get('active') else []
        try:
            self.launch(arguments)
        except OSError as error:
            self.toast('Could not open the room window: ' + str(error))
            return
        self.window.close()

    def poll(self):
        if self.closed:
            return False
        if self.polling:
            return True
        self.polling = True

        def done(f):
            try:
                GLib.idle_add(self.polled, f.result(), None)
            except Exception as error:
                GLib.idle_add(self.polled, None, str(error) or type(error).__name__)
        self.executor.submit(self.request, 'inspect', {}, 3).add_done_callback(done)
        return True

    def polled(self, snapshot, error):
        self.polling = False
        if self.closed:
            return False
        if error:
            if not self.offline:
                self.toast('Room controller unavailable: ' + error)
            self.offline = True
            return False
        self.offline = False
        self.snapshot = snapshot
        self.render(snapshot)
        return False

    def render(self, snapshot):
        if snapshot is None or self.closed:
            return
        self.window.player.update(snapshot)
        accent = (snapshot.get('active') or {}).get('accent')
        self.accent.load_from_string(accent_css(accent))

    def on_close(self, *_):
        if self.closed:
            return False
        self.closed = True
        GLib.source_remove(self.poll_source)
        self.window.player.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.urgent.shutdown(wait=False, cancel_futures=True)
        return False


def run_popover(argv, request=None):
    """`listening-roomctl popover`: the Waybar left-click controls. One instance at a time."""
    if request is None:
        import session_client
        request = session_client.request
    choose_renderer()
    app = Adw.Application(application_id='app.kotona.ListeningPopover')
    state = {}

    def startup(_app):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def activate(_app):
        if state.get('host') is not None and not state['host'].closed:
            state['host'].window.present()
            return
        state['host'] = PopoverHost(app, request)
    app.connect('startup', startup)
    app.connect('activate', activate)
    return app.run(argv)
