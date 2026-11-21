"""Small GTK/libadwaita building blocks for the next Listening Room window."""
import os

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Graphene', '1.0')
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk

import next_model

_TEXTURES = {}
_UNSET = object()


def texture_for(path):
    """One Gdk.Texture per image file, reused across polls; None when it cannot be read.

    Decoding and uploading the same art on every poll costs time and GPU memory."""
    if not path:
        return None
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return None
    texture = _TEXTURES.get(key)
    if texture is None:
        try:
            texture = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            return None
        _TEXTURES[key] = texture
    return texture


def show_texture(picture, path):
    """Point a picture at an image file's texture, leaving it alone when nothing changed."""
    texture = texture_for(path)
    # Not every picture widget here has get_paintable (FocalPicture only sets), so
    # remember what was shown on the widget itself.
    if getattr(picture, '_shown_texture', _UNSET) is not texture:
        picture.set_paintable(texture)
        picture._shown_texture = texture
    return texture


def choose_renderer(environ=None, nvidia_marker='/proc/driver/nvidia/version'):
    """Use GTK's OpenGL renderer on the NVIDIA proprietary driver.

    GTK's Vulkan renderer crashes inside that driver while uploading textures
    (gsk_vulkan_direct_allocator_alloc). An explicit GSK_RENDERER always wins."""
    environ = os.environ if environ is None else environ
    if not environ.get('GSK_RENDERER') and os.path.exists(nvidia_marker):
        environ['GSK_RENDERER'] = 'ngl'
    return environ.get('GSK_RENDERER')

CSS = '''
window.next { background: #17171a; color: #ffffff; }
window.next headerbar { background: #1f1f23; color: #ffffff; box-shadow: none; }
window.next.in-now headerbar { background: transparent; border: none; }
window.next.in-now button.header-pill, window.next.in-now menubutton.header-pill > button
  { background: rgba(18,18,22,.62); border-radius: 20px; min-height: 40px; min-width: 40px; color: #fff; }
window.next.in-now button.header-pill:hover, window.next.in-now menubutton.header-pill > button:hover { background: rgba(60,60,66,.8); }
window.next button:focus:focus-visible, window.next scale:focus:focus-visible, window.next row:focus:focus-visible,
window.next flowboxchild:focus:focus-visible, window.next menubutton > button:focus:focus-visible, window.next togglebutton:focus:focus-visible
  { outline: 3px solid #ffffff; outline-offset: -3px; }
window.next button.play-button:focus:focus-visible, window.next button.suggested-action:focus:focus-visible,
window.next button.accent-fill:focus:focus-visible, window.next button.mini-play:focus:focus-visible,
window.next button.mini-return:focus:focus-visible
  { outline: 3px solid #111114; outline-offset: -6px; }
.next .scrim-top { background-image: linear-gradient(rgba(8,8,10,.66), rgba(8,8,10,0)); }
.next .scrim-bottom { background-image: linear-gradient(rgba(8,8,10,0), rgba(8,8,10,.82) 50%, rgba(8,8,10,.95)); }
.next .scene-fallback { background: #0b0c0f; }
.next .room-name { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 64px; font-weight: 400; color: #ffffff; }
.next.compact .room-name { font-size: 40px; }
.next .now-line { font-size: 18px; color: rgba(255,255,255,.94); }
.next .now-detail { font-size: 18px; color: rgba(255,255,255,.76); }
.next .source-chip { background: rgba(255,255,255,.16); color: #ffffff; border-radius: 12px; padding: 2px 10px; font-weight: 600; font-size: 12px; }
.next .glass { background: rgba(18,18,22,.62); color: #ffffff; border-radius: 24px; border: 1px solid rgba(255,255,255,.14); }
.next .glass:hover { background: rgba(48,48,54,.82); }
.next .mix-button { padding: 0 18px 0 14px; min-height: 48px; }
.next .mix-summary { color: rgba(255,255,255,.8); }
.next .mix-marker { background: #ffb4a2; border-radius: 5px; min-width: 10px; min-height: 10px; }
.next .play-button { background: #ffffff; color: #121214; border-radius: 36px; min-width: 72px; min-height: 72px; padding: 0; box-shadow: 0 8px 28px rgba(0,0,0,.35); }
.next .play-button:hover { background: #e9e9ee; }
.next .skip-button { background: rgba(18,18,22,.62); color: #fff; border-radius: 28px; min-width: 56px; min-height: 56px; padding: 0; }
.next .volume-scale trough { min-height: 6px; background: rgba(255,255,255,.28); }
.next .volume-scale highlight { background: #ffffff; }
.next .banner-card { background: rgba(38,26,24,.94); color: #ffffff; border-radius: 14px; border: 1px solid rgba(255,180,160,.5); padding: 10px 14px; }
.next .banner-card .banner-title { font-weight: 700; }
.next .banner-card .banner-detail { color: rgba(255,255,255,.82); }
.next .offline-banner { background: rgba(38,26,24,.96); color: #ffffff; border-radius: 12px; border: 1px solid rgba(255,180,160,.5); padding: 8px 12px; }
.next .offline-banner .banner-title { font-weight: 700; }
.next .error-details, .next .error-details label { color: rgba(255,255,255,.78); font-size: 13px; }
.next .room-changed { background: rgba(255,255,255,.16); color: #ffffff; border-radius: 999px; padding: 2px 10px; font-size: 13px; font-weight: 600; }
.next.offline .play-button, .next.offline .bar-play { opacity: .45; }
.next .timer-chip { background: rgba(255,255,255,.16); border-radius: 20px; padding: 0 14px; font-weight: 600; }
.next .hero { border-radius: 18px; background: #0b0c0f; }
.next .hero-title { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 44px; color: #ffffff; }
.next.compact .hero-title { font-size: 32px; }
.next .hero-detail { color: rgba(255,255,255,.88); font-size: 16px; }
.next .hero-when { color: #ffffff; font-weight: 600; font-size: 12px; }
.next .room-card { border-radius: 14px; background: #1f1f23; }
.next .room-card-title { font-weight: 600; color: #ffffff; }
.next .room-card-sub, .next .dim-label { color: rgba(255,255,255,.72); }
.next .yours-tag { background: rgba(255,255,255,.14); border-radius: 8px; padding: 0 7px; font-size: 11px; font-weight: 700; }
.next .mix-sheet-title { font-size: 20px; font-weight: 600; }
.next .mix-room { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 18px; }
.next .layer-tile { border-radius: 12px; min-width: 44px; min-height: 44px; background: rgba(255,255,255,.1); }
.next .layer-title { font-weight: 600; font-size: 15px; }
.next .layer-row { padding: 12px 0; border-top: 1px solid rgba(255,255,255,.08); }
'''

_provider = None


def install_css(display):
    global _provider
    if _provider is None:
        _provider = Gtk.CssProvider()
        _provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return _provider


def tint(accent):
    """A light tint of the room accent: the only form used for accent-coloured text."""
    rgb = [int(next_model.accent(accent)[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join('%02x' % round(c + (255 - c) * .55) for c in rgb)


def accent_css(accent):
    """Room accent for primary actions, slider fills and chips; text uses the tint."""
    accent = next_model.accent(accent)
    return ('.next .suggested-action, .next .accent-fill { background: %s; color: #111114; }'
            ' .next .mix-room { color: %s; }'
            ' .next scale.accented highlight { background: %s; }') % (accent, tint(accent), accent)


def name_it(widget, name, tooltip=None):
    """Give an icon-only control one stable accessible name and a matching tooltip."""
    widget.update_property([Gtk.AccessibleProperty.LABEL], [name])
    widget.set_tooltip_text(name if tooltip is None else tooltip)
    return widget


def icon_button(icon, name, callback=None, css=()):
    button = Gtk.Button(icon_name=icon)
    name_it(button, name)
    for cls in css:
        button.add_css_class(cls)
    if callback:
        button.connect('clicked', lambda *_: callback())
    return button


def label(text='', css=(), xalign=0., wrap=False, ellipsize=False):
    widget = Gtk.Label(label=text, xalign=xalign)
    widget.set_wrap(wrap)
    if ellipsize:
        widget.set_ellipsize(3)
    for cls in css:
        widget.add_css_class(cls)
    return widget


class FocalPicture(Gtk.Widget):
    """Cover-fit paintable with a focal point, so tall crops keep the subject."""
    def __init__(self, focal=(.5, .42)):
        super().__init__(accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.paintable = None
        self.invalidated = 0
        self.focal = focal
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def set_paintable(self, paintable):
        # The handler lives on the paintable (a cached texture or a shared loop that outlives this
        # picture): connect only when it can change, pass no closure over self, and never leave one.
        if self.invalidated:
            self.paintable.disconnect(self.invalidated)
            self.invalidated = 0
        self.paintable = paintable
        if paintable is not None and not paintable.get_flags() & Gdk.PaintableFlags.CONTENTS:
            self.invalidated = paintable.connect('invalidate-contents', FocalPicture._changed, self)
        self.queue_draw()

    @staticmethod
    def _changed(_paintable, picture):
        picture.queue_draw()

    def do_measure(self, orientation, for_size):
        return 0, 0, -1, -1

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        paintable = self.paintable
        if paintable is None or not width or not height:
            return
        ratio = paintable.get_intrinsic_aspect_ratio() or (width / height)
        draw_w, draw_h = (width, width / ratio) if width / ratio >= height else (height * ratio, height)
        x = -(draw_w - width) * self.focal[0]
        y = -(draw_h - height) * self.focal[1]
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x, y))
        paintable.snapshot(snapshot, draw_w, draw_h)
        snapshot.restore()


def gst_ready():
    """Gtk.MediaFile aborts the process when GStreamer playback is missing."""
    try:
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        return Gst.ElementFactory.find('playbin3') is not None
    except (ValueError, ImportError, GLib.Error):
        return False


# Scene loops and GStreamer. Clearing or finalizing a Gtk.MediaFile drops GTK's reference on its
# GstPlay. If GstPlay's own thread is posting a message at that moment (preroll, the position tick, a
# state change, or the stop that clear() itself requests), that thread holds the last reference: the
# player frees itself on its own thread, gst_play_main keeps using it and the window segfaults
# (g_object_unref <- gst_play_main). Each pipeline also leaves a GL display and context thread behind
# for good. So there is one Gtk.MediaFile per loop file for the life of the process, shared by the
# Scenes that show it and only ever played or paused, never cleared: the cost is bounded by the
# catalogue's loops and flat once each has been shown.
_LOOPS = {}               # loop path -> _Loop


class _Loop:
    def __init__(self, path):
        self.media = Gtk.MediaFile.new_for_filename(path)
        self.media.set_loop(True)
        self.media.set_muted(True)
        self.wanted = set()   # ids of the Scenes that want it playing now

    def want(self, scene, playing):
        (self.wanted.add if playing else self.wanted.discard)(id(scene))
        if self.wanted:
            self.media.play()
        else:
            self.media.pause()


def shared_loop(path):
    loop = _LOOPS.get(path)
    if loop is None:
        loop = _LOOPS[path] = _Loop(path)
    return loop


# A source that never fires holds the loops from C, so interpreter shutdown never finalizes one
# (that would be a teardown too); the process exits with them paused.
GLib.timeout_add_seconds(GLib.MAXINT32, lambda *_: True, _LOOPS)


class Scene(Gtk.Overlay):
    """The room scene, full bleed: still art, or the reviewed loop (muted) when allowed."""
    def __init__(self):
        super().__init__()
        self.add_css_class('scene-fallback')
        self.picture = FocalPicture()
        self.set_child(self.picture)
        self.media = self.loop = None
        self.error_handler = 0
        self.still = None
        self.key = None
        self.want_motion = False
        self.mapped = self.minimized = False
        # Plain functions, not closures over self: a handler holding its own widget is a cycle that
        # keeps every closed preview (and its loop) alive.
        self.connect('map', Scene._mapped, True)
        self.connect('unmap', Scene._mapped, False)

    @staticmethod
    def _mapped(scene, mapped):
        scene._visibility(mapped=mapped)

    @staticmethod
    def _failed(media, _spec, scene):
        if media.get_error() is not None:
            scene._fall_back()

    def show_room(self, assets, motion):
        key = (assets.get('still'), assets.get('loop'), bool(motion))
        if key == self.key:
            return
        self.key = key
        self._drop_media()
        self.want_motion = bool(motion and assets.get('loop') and gst_ready())
        still = assets.get('still') or assets.get('poster')
        self.still = texture = texture_for(still)
        self.picture.set_paintable(texture)
        if self.want_motion:
            self.loop = shared_loop(assets['loop'])
            self.media = media = self.loop.media
            if media.get_error() is not None:   # this loop failed before: still art, like _fall_back
                self.want_motion = False
                self._drop_media()
                return
            self.error_handler = media.connect('notify::error', Scene._failed, self)
            self.picture.set_paintable(media)
            self._visibility(mapped=self.get_mapped())

    def _fall_back(self):
        self._drop_media()
        self.picture.set_paintable(self.still)

    def _drop_media(self):
        """Let go of the shared loop: it pauses when no Scene wants it; it is never cleared."""
        if self.loop is not None:
            loop, self.loop, self.media = self.loop, None, None
            if self.error_handler:
                loop.media.disconnect(self.error_handler)
            self.error_handler = 0
            if self.picture.paintable is loop.media:
                self.picture.set_paintable(self.still)
            loop.want(self, False)

    def _visibility(self, mapped=None, minimized=None):
        """Play only while the window is on screen: mapped and not minimized or suspended."""
        self.mapped = self.mapped if mapped is None else mapped
        self.minimized = self.minimized if minimized is None else minimized
        if self.loop is not None:
            self.loop.want(self, self.mapped and not self.minimized)

    @property
    def playing_motion(self):
        return self.loop is not None and id(self) in self.loop.wanted and self.media.get_playing()

    def close(self):
        self._drop_media()


def pop_button(icon, name, popover):
    button = Gtk.MenuButton(icon_name=icon, popover=popover)
    button.add_css_class('header-pill')
    name_it(button, name)
    return button


def menu_item(text, callback, popover=None, css=('flat',)):
    item = Gtk.Button(label=text)
    for cls in css:
        item.add_css_class(cls)
    item.get_child().set_xalign(0.)

    def clicked(*_):
        if popover is not None:
            popover.popdown()
        callback()
    item.connect('clicked', clicked)
    return item


class TimerPopover(Gtk.Popover):
    """Break, Wind down, Dim the scene and Cancel; nothing starts until chosen."""
    def __init__(self, choose):
        super().__init__()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        box.append(label('Timer', ['heading', 'dim-label']))
        self.cancel = None
        for kind, text in (('break', 'Break 10 min'), ('wind_down', 'Wind down 30 min'), ('dim', 'Dim the scene 30 min')):
            box.append(menu_item(text, lambda kind=kind: choose(kind), self))
        self.cancel = menu_item('Cancel', lambda: choose('cancel'), self)
        box.append(Gtk.Separator())
        box.append(self.cancel)
        self.set_child(box)
        self.set_running(False)

    def set_running(self, running):
        self.cancel.set_sensitive(running)
        self.cancel.set_tooltip_text(None if running else 'No timer is running')


class OutputPopover(Gtk.Popover):
    """Owned-stream outputs, Test sound and system volume."""
    def __init__(self, choose, test, set_system_volume, opened):
        super().__init__()
        self.choose = choose
        self.updating = False
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8, margin_bottom=8,
                           margin_start=8, margin_end=8)
        self.box.set_size_request(280, -1)
        self.box.append(label('Output', ['heading']))
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.box.append(self.list)
        self.box.append(Gtk.Separator())
        self.box.append(menu_item('Test sound', test, None))
        self.box.append(label('System volume', ['dim-label']))
        self.system = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        name_it(self.system, 'System volume', None)
        self.system.connect('value-changed', lambda scale: None if self.updating else set_system_volume(round(scale.get_value())))
        self.box.append(self.system)
        self.set_child(self.box)
        self.connect('show', lambda *_: opened())
        self.buttons = []
        self.group = None

    def show_outputs(self, outputs, preferred, system_percent):
        self.updating = True
        while self.list.get_first_child():
            self.list.remove(self.list.get_first_child())
        self.buttons, first = [], None
        for name, text in [(None, 'System output')] + [(item['name'], item.get('description') or item['name'])
                                                       for item in outputs]:
            button = Gtk.CheckButton(label=text)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.set_active(name == preferred)
            button.connect('toggled', lambda b, n=name: b.get_active() and not self.updating and self.choose(n))
            self.list.append(button)
            self.buttons.append(button)
        self.system.set_sensitive(system_percent is not None)
        if system_percent is not None:
            self.system.set_value(float(system_percent))
        self.updating = False
