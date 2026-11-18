"""Menu > Other players…: the apps outside the room (browsers, Jellyfin, KDE Connect) and
system volume. Opening it only reads; each control acts on that one app."""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

import listening_hub as hub
from next_widgets import icon_button, name_it

OWN = ('radio', 'video')
NAMES = (('vivaldi', 'Vivaldi'), ('firefox', 'Firefox'), ('feishin', 'Jellyfin Music'), ('jellyfin', 'Jellyfin'),
         ('kdeconnect', 'KDE Connect'), ('chromium', 'Chromium'), ('chrome', 'Chrome'))


def player_name(player_id):
    lower = str(player_id or '').lower()
    for key, display in NAMES:
        if key in lower:
            return display + (str(player_id)[len(key):] if lower.startswith(key) else '')
    return str(player_id or 'Player')


def external(players):
    """Players that are not the room's own layers."""
    return [item for item in players or [] if item.get('id') not in OWN]


class PlayersDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title='Other players', content_width=480, content_height=460)
        self.window = window
        self.closed = False
        self.updating = False
        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title='Apps playing outside the room')
        self.system_group = Adw.PreferencesGroup(title='System volume')
        self.system = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.system.set_hexpand(True)
        self.system.set_valign(Gtk.Align.CENTER)
        self.system.set_size_request(180, -1)
        name_it(self.system, 'System volume', None)
        self.system.connect('value-changed', self.system_changed)
        self.mute = Gtk.ToggleButton(label='Mute', valign=Gtk.Align.CENTER)
        self.mute.connect('toggled', lambda *_: None if self.updating else self.window.run_local(
            lambda: hub.mute(master=True), None))
        row = Adw.ActionRow(title='Volume')
        row.add_suffix(self.system)
        row.add_suffix(self.mute)
        self.system_group.add(row)
        self.page.add(self.system_group)
        self.page.add(self.group)
        self.rows = []
        self.timers = {}
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.page)
        self.set_child(view)
        self.connect('closed', self.on_closed)
        self.load()
        self.source = GLib.timeout_add_seconds(3, self.load)

    def on_closed(self, *_):
        self.closed = True
        GLib.source_remove(self.source)

    def load(self):
        if self.closed:
            return False

        def get():
            state = hub.inspect_state()
            return state.get('players') or [], state.get('system_volume') or {}
        future = self.window.executor.submit(get)

        def done(f):
            try:
                value = f.result()
            except Exception:
                return
            GLib.idle_add(lambda: self.show(*value) or False)
        future.add_done_callback(done)
        return True

    def show(self, players, system):
        if self.closed:
            return
        self.updating = True
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        players = external(players)
        if not players:
            row = Adw.ActionRow(title='Nothing else is playing')
            self.group.add(row)
            self.rows.append(row)
        for item in players:
            self.rows.append(self.player_row(item))
        percent = system.get('percent')
        self.system.set_sensitive(percent is not None)
        if percent is not None and 'system' not in self.timers:
            self.system.set_value(float(percent))
        self.mute.set_active(bool(system.get('muted')))
        self.updating = False

    def player_row(self, item):
        pid = item['id']
        subtitle = ' — '.join(x for x in (item.get('artist'), item.get('title')) if x)
        row = Adw.ActionRow(title=player_name(pid), subtitle=(subtitle + ' · ' if subtitle else '') + str(item.get('status', '')))
        box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        for icon, command, name in (('media-skip-backward-symbolic', 'previous', 'Previous track'),
                                    ('media-playback-start-symbolic', 'play-pause', 'Play or pause'),
                                    ('media-skip-forward-symbolic', 'next', 'Next track'),
                                    ('media-playback-stop-symbolic', 'stop', 'Stop')):
            button = icon_button(icon, '%s, %s' % (name, player_name(pid)),
                                 lambda c=command: self.window.run_local(lambda: hub.transport(pid, c), None), ['flat'])
            if command in ('previous', 'next') and not item.get('can_' + command, True):
                button.set_visible(False)
            box.append(button)
        row.add_suffix(box)
        volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        volume.set_size_request(120, -1)
        volume.set_valign(Gtk.Align.CENTER)
        name_it(volume, 'Volume, ' + player_name(pid), None)
        if isinstance(item.get('volume'), (int, float)):
            volume.set_value(float(item['volume']))
        else:
            volume.set_sensitive(False)
        volume.connect('value-changed', lambda scale, pid=pid: self.player_volume(pid, scale))
        row.add_suffix(volume)
        self.group.add(row)
        return row

    def player_volume(self, pid, scale):
        if self.updating:
            return
        if pid in self.timers:
            GLib.source_remove(self.timers[pid])

        def send():
            self.timers.pop(pid, None)
            self.window.run_local(lambda: hub.set_volume(round(scale.get_value()), target_id=pid), None)
            return False
        self.timers[pid] = GLib.timeout_add(350, send)

    def system_changed(self, scale):
        if self.updating:
            return
        if 'system' in self.timers:
            GLib.source_remove(self.timers['system'])

        def send():
            self.timers.pop('system', None)
            self.window.set_system_volume(round(scale.get_value()))
            return False
        self.timers['system'] = GLib.timeout_add(350, send)
