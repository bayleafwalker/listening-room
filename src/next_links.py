"""Links in Listen: which URLs open the sheet, and the sheet's own wording.

The preview and the destination list come from the controller's existing link
route (``online_services.preview_link``), so Listen offers exactly what a link
supports. Previewing is local and starts nothing.
"""
import os
import urllib.parse

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

DESTINATIONS = (('music', 'Play'), ('atmosphere', 'Play as ambience'),
                ('picture', 'Show as scene'), ('original', 'Open elsewhere'))
BROWSER_NOTE = 'This link opens in its original service.'
ENTRY_HINT = 'Paste a public link from YouTube, Twitch or a radio stream.'


def _public(value):
    """A public HTTP(S) URL, or None; credentials and whitespace are refused."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port
    except ValueError:
        return None
    return value


def _uri_url(value):
    """The URL carried by listening-room://open?url=…, or None."""
    try:
        parsed = urllib.parse.urlsplit(str(value))
    except ValueError:
        return None
    if parsed.scheme != 'listening-room' or parsed.netloc != 'open':
        return None
    return urllib.parse.parse_qs(parsed.query).get('url', [''])[0] or None


def launch_link(argv):
    """The link a `listening-room --ui=next …` launch asked to preview, if any."""
    values = list(argv or [])
    program = os.path.basename(values[0]) if values else ''
    arguments = values[1:]
    explicit = program in ('listening-room', 'listening_room.py') or any(
        argument == '--ui' or argument.startswith('--ui=') for argument in arguments)
    for index, argument in enumerate(arguments):
        candidate = None
        if argument == '--preview-link' and index + 1 < len(arguments):
            candidate = arguments[index + 1]
        elif argument.startswith('--preview-link='):
            candidate = argument.split('=', 1)[1]
        elif argument.startswith('listening-room://'):
            candidate = _uri_url(argument)
        elif explicit and not argument.startswith('-'):
            candidate = _public(argument)
        if candidate and _public(candidate):
            return candidate
    return None


def descriptor(value):
    """Classify a URL without fetching it or starting playback."""
    import online_services
    return online_services.preview_link(value)


def destinations(preview):
    """Only the destinations this link supports, in listener words."""
    actions = (preview or {}).get('actions') or []
    return [(key, text) for key, text in DESTINATIONS if key in actions]


# A YouTube video's sheet, in the same order and words as its Listen row: Play, Watch (with
# sound, its own window), Show as scene (silent), Play as ambience, Open elsewhere.
YOUTUBE_ORDER = (('music', 'Play'), ('video', 'Watch'), ('picture', 'Show as scene'),
                 ('atmosphere', 'Play as ambience'), ('original', 'Open elsewhere'))


def sheet_destinations(preview):
    """What the link sheet offers: the link's destinations, plus Watch for a YouTube video."""
    offered = destinations(preview)
    if (preview or {}).get('kind') != 'youtube' or 'music' not in dict(offered):
        return offered
    keys = set(dict(offered)) | {'video'}
    return [(key, text) for key, text in YOUTUBE_ORDER if key in keys]


def non_public(value):
    """True when the link's host is this machine or a private network: localhost, loopback,
    private, link-local, unspecified or `.local` names, including odd numeric spellings."""
    import ipaddress
    import socket
    name = host(value).strip('[]').rstrip('.')
    if not name:
        return True
    if name == 'localhost' or name.endswith(('.localhost', '.local', '.internal', '.lan', '.home.arpa')):
        return True
    if '.' not in name and ':' not in name and not name.isdigit() and not name.startswith('0x'):
        return True   # a bare machine name such as `intranet`
    address = None
    try:
        address = ipaddress.ip_address(name.split('%', 1)[0])
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(name))
        except (OSError, ValueError):
            return False
    if getattr(address, 'ipv4_mapped', None):
        address = address.ipv4_mapped
    return not address.is_global


def host(value):
    try:
        return (urllib.parse.urlsplit(str(value)).hostname or '').lower()
    except ValueError:
        return ''


def detail(preview):
    """One short line under the link's name."""
    name = host((preview or {}).get('url'))
    return (name + ' · link') if name else 'Link'


def _box(spacing=12, margins=(20, 20, 20, 20)):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    top, bottom, start, end = margins
    box.set_margin_top(top)
    box.set_margin_bottom(bottom)
    box.set_margin_start(start)
    box.set_margin_end(end)
    return box


def _text(value, css=None, wrap=True, ellipsize=False):
    widget = Gtk.Label(label=value, xalign=0.)
    widget.set_wrap(wrap)
    if ellipsize:
        widget.set_ellipsize(3)
    if css:
        widget.add_css_class(css)
    return widget


class LinkSheet(Adw.Dialog):
    """Preview a link and offer its destinations. Nothing starts until one is chosen."""

    def __init__(self, preview, choose):
        super().__init__()
        self.set_title('Add a link')
        self.set_content_width(540)
        preview = preview or {}
        body = _box()
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading.append(_text(preview.get('name') or 'Link', 'title-4'))
        url = _text(preview.get('url') or '', 'dim-label', wrap=False, ellipsize=True)
        url.set_selectable(True)
        url.set_tooltip_text(preview.get('url') or '')
        heading.append(url)
        heading.append(_text(detail(preview), 'caption'))
        body.append(heading)
        offered = sheet_destinations(preview)
        if any(key != 'original' for key, _text_ in offered):
            note = _text('Choose what to do with this link. Nothing starts until you choose.',
                         'caption')
        else:
            note = _text(BROWSER_NOTE, 'caption')
        note.set_wrap(True)
        body.append(note)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for key, text in offered:
            button = Gtk.Button(label=text)
            if key == 'video':
                button.set_tooltip_text('Watch with sound in its own window')
            if key == 'music':
                button.add_css_class('suggested-action')
                button.add_css_class('pill')
            button.connect('clicked', lambda _b, destination=key: self._chosen(destination, choose, preview))
            buttons.append(button)
        body.append(buttons)
        close = Gtk.Button(label='Cancel')
        close.add_css_class('flat')
        close.connect('clicked', lambda *_: self.close())
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        footer.set_halign(Gtk.Align.END)
        footer.append(close)
        body.append(footer)
        self.set_child(body)

    def _chosen(self, destination, choose, preview):
        self.close()
        choose(destination, preview.get('url') or preview.get('original') or '')


class AddLinkDialog(Adw.Dialog):
    """Ask for a link, then hand a validated preview back to Listen."""

    def __init__(self, submit, initial=''):
        super().__init__()
        self.set_title('Add a link')
        self.set_content_width(520)
        self.submit = submit
        body = _box()
        body.append(_text('Add a link', 'title-4'))
        body.append(_text(ENTRY_HINT, 'caption', wrap=True))
        self.entry = Gtk.Entry(text=initial)
        self.entry.set_placeholder_text('https://')
        self.entry.connect('activate', lambda *_: self.preview())
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], ['Link'])
        body.append(self.entry)
        self.error = _text('', 'error', wrap=True)
        self.error.set_visible(False)
        body.append(self.error)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label='Cancel')
        cancel.add_css_class('flat')
        cancel.connect('clicked', lambda *_: self.close())
        self.preview_button = Gtk.Button(label='Preview link')
        self.preview_button.add_css_class('suggested-action')
        self.preview_button.connect('clicked', lambda *_: self.preview())
        footer.append(cancel)
        footer.append(self.preview_button)
        body.append(footer)
        self.set_child(body)
        self.set_default_widget(self.preview_button)
        self.set_focus(self.entry)

    def prefill(self, value):
        """Fill the entry from the clipboard without overwriting typing."""
        if value and not self.entry.get_text().strip():
            self.entry.set_text(value)
            self.entry.set_position(-1)

    def preview(self):
        value = self.entry.get_text().strip()
        try:
            preview = descriptor(value)
        except Exception as error:
            self.error.set_text(str(error) or 'That is not a public link.')
            self.error.set_visible(True)
            return False
        self.error.set_visible(False)
        self.close()
        self.submit(preview)
        return True
