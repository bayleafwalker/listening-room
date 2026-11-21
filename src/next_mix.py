"""The Mix sheet: one row each for Music, Ambience and Scene."""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

import next_model
from next_widgets import icon_button, label, menu_item, name_it

ICONS = {'music': 'audio-x-generic-symbolic', 'atmosphere': 'weather-showers-symbolic',
         'picture': 'image-x-generic-symbolic'}


class LayerRow(Gtk.Box):
    """One layer: tile, source and state, then its contextual controls."""
    def __init__(self, layer, send, choose_other, options):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.layer, self.send, self.options = layer, send, options
        self.updating = False
        self.pending = {}
        self.timers = {}
        self.add_css_class('layer-row')
        word = next_model.LAYER_WORDS[layer]
        top = Gtk.Box(spacing=14)
        tile = Gtk.Image(icon_name=ICONS[layer], pixel_size=22)
        tile.add_css_class('layer-tile')
        top.append(tile)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        self.title = label(word, ['layer-title'])
        self.subtitle = label('', ['dim-label'], ellipsize=True)
        text.append(self.title)
        text.append(self.subtitle)
        top.append(text)
        if layer in ('music', 'atmosphere'):  # sources are chosen in Listen
            self.change = Gtk.Button(label='Change…')
            self.change.connect('clicked', lambda *_: choose_other(layer))
            self.change.set_tooltip_text('Choose %s in Listen' % ('an ambience' if layer == 'atmosphere' else 'music'))
        else:
            self.choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                                   margin_start=6, margin_end=6)
            popover = Gtk.Popover(child=self.choices)
            popover.connect('show', lambda *_: self.fill_choices(popover))
            self.change = Gtk.MenuButton(label='Change…', popover=popover)
        top.append(self.change)
        self.mute = Gtk.ToggleButton(icon_name='audio-volume-high-symbolic')
        self.mute.connect('toggled', self.mute_toggled)
        if layer == 'picture':
            self.close = icon_button('window-close-symbolic', 'Close scene', lambda: self.send(*self.args('stop')))
        top.append(self.mute if layer != 'picture' else self.close)
        self.menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                                margin_start=6, margin_end=6)
        self.popover = Gtk.Popover(child=self.menu_box)
        self.menu = Gtk.MenuButton(icon_name='view-more-symbolic', popover=self.popover)
        name_it(self.menu, word + ' options')
        self.next_item = menu_item('Next track', lambda: self.send(*self.args('next')), self.popover)
        self.retry_item = menu_item('Try again', lambda: self.send(*self.args('retry')), self.popover)
        self.stop_item = menu_item('Stop', lambda: self.send(*self.args('stop')), self.popover)
        for item in (self.next_item, self.retry_item, self.stop_item):
            self.menu_box.append(item)
        top.append(self.menu)
        self.append(top)
        self.controls = Adw.WrapBox(child_spacing=16, line_spacing=8, margin_start=58)
        self.append(self.controls)
        self.scale = self.window = self.placement = self.moving = None
        if layer in ('music', 'atmosphere'):
            self.scale = self._slider(word + ' level', lambda value: self.queue('volume', value))
            self.controls.append(self.scale)
            name_it(self.mute, 'Mute ' + word.lower(), None)
        if layer == 'atmosphere':
            self.window = Gtk.Box(spacing=8)
            self.window.append(label('Window', ['dim-label']))
            self.window_scale = self._slider('Window', lambda value: self.queue('window', value))
            self.window_scale.set_size_request(150, -1)
            self.window.append(label('Closed', ['dim-label']))
            self.window.append(self.window_scale)
            self.window.append(label('Open', ['dim-label']))
            self.controls.append(self.window)
        if layer == 'picture':
            self.placement = Adw.ToggleGroup()
            name_it(self.placement, 'Scene placement', 'Where the scene window goes')
            for key, text in next_model.PLACEMENTS:
                self.placement.add(Adw.Toggle(label=text, name=key))
            self.placement.connect('notify::active-name', self.placement_changed)
            self.controls.append(self.placement)
            if not next_model.hyprland():
                # Placement moves the scene window through Hyprland; say so where it would not work.
                self.placement_note = label(next_model.NO_HYPRLAND, ['dim-label'], wrap=True)
                self.controls.append(self.placement_note)
            self.moving = Gtk.Switch(valign=Gtk.Align.CENTER)
            name_it(self.moving, 'Moving picture', None)
            self.moving.connect('notify::active', self.moving_changed)
            moving = Gtk.Box(spacing=8)
            moving.append(label('Moving picture'))
            moving.append(self.moving)
            self.controls.append(moving)

    def fill_choices(self, popover):
        while self.choices.get_first_child():
            self.choices.remove(self.choices.get_first_child())
        for text, command in self.options(self.layer):
            self.choices.append(menu_item(text, lambda c=command: self.send(*c), popover))

    def args(self, action, **extra):
        return ('layer', dict({'layer': self.layer, 'action': action}, **extra))

    def _slider(self, name, changed):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_size_request(220, -1)
        scale.add_css_class('accented')
        name_it(scale, name, None)
        scale.connect('value-changed', lambda widget: None if self.updating else changed(widget.get_value()))
        return scale

    def queue(self, action, value):
        """Debounce slider drags into one command; the room never hears a flood."""
        self.pending[action] = value
        if action in self.timers:
            GLib.source_remove(self.timers[action])

        def fire():
            self.timers.pop(action, None)
            self.send(*self.args(action, value=round(self.pending.pop(action), 1)))
            return False
        self.timers[action] = GLib.timeout_add(160, fire)

    def mute_toggled(self, button):
        if not self.updating and self.layer in ('music', 'atmosphere'):
            # The ambience player only toggles; music takes an explicit state.
            self.send(*(self.args('mute') if self.layer == 'atmosphere' else self.args('muted', value=button.get_active())))
        button.set_icon_name('audio-volume-muted-symbolic' if button.get_active() else 'audio-volume-high-symbolic')

    def placement_changed(self, group, *_):
        if not self.updating and group.get_active_name():
            self.send(*self.args('place', placement=group.get_active_name()))

    def moving_changed(self, row, *_):
        if not self.updating:
            self.send(*(self.args('motion') if row.get_active() else self.args('still', still=True)))

    def set_change_label(self, text):
        # GTK rebuilds a MenuButton's child on every set_label, so only when the words change.
        if self.change.get_label() != text:
            self.change.set_label(text)

    def update(self, row):
        self.updating = True
        state = row['state']
        empty = self.layer == 'atmosphere' and row['closed'] and not row['failed']
        self.subtitle.set_text('No ambience' if empty else
                               ' · '.join(part for part in (row['source'], row['detail'], state) if part))
        self.subtitle.remove_css_class('error')
        if self.layer == 'atmosphere':
            # With nothing playing there is nothing to level, mute or stop: only the way to choose one.
            self.set_change_label('Choose ambience' if empty else 'Change…')
            (self.change.add_css_class if empty else self.change.remove_css_class)('suggested-action')
            for widget in (self.controls, self.mute, self.menu):
                widget.set_visible(not empty)
        if row['failed']:
            self.subtitle.add_css_class('error')
        self.next_item.set_visible(bool(row.get('can_next')))
        self.retry_item.set_visible(bool(row['failed']))
        self.stop_item.set_visible(not row['closed'])
        if self.scale is not None:
            if 'volume' not in self.timers:
                self.scale.set_value(float(row['level']))
            self.mute.set_active(row['muted'])
            self.mute.set_icon_name('audio-volume-muted-symbolic' if row['muted'] else 'audio-volume-high-symbolic')
            name_it(self.mute, ('Unmute ' if row['muted'] else 'Mute ') + row['title'].lower(), None)
        if self.window is not None:
            self.window.set_visible(row['outdoor'])
            if 'window' not in self.timers:
                self.window_scale.set_value(float(row['window']))
        if self.placement is not None:
            self.placement.set_active_name(row['placement'])
            self.moving.set_active(row['moving'])
            self.close.set_visible(not row['closed'])
            self.set_change_label('Open scene' if row['closed'] else 'Change…')
        self.updating = False


class MixSheet(Gtk.Box):
    """Sheet content: header with Save as my room…, then the three layer rows."""
    def __init__(self, send, choose_other, close, save, options):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=4, margin_bottom=22,
                         margin_start=28, margin_end=28)
        head = Gtk.Box(spacing=12)
        head.append(label('Mix', ['mix-sheet-title']))
        self.room = label('', ['mix-room'])
        head.append(self.room)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        head.append(spacer)
        head.append(menu_item('Save as my room…', save, None, css=('pill',)))
        head.append(icon_button('go-down-symbolic', 'Close mix', close, ['circular']))
        self.append(head)
        self.rows = {name: LayerRow(name, send, choose_other, options) for name in next_model.LAYER_ORDER}
        for row in self.rows.values():
            self.append(row)

    def update(self, snapshot):
        self.room.set_text((snapshot.get('active') or {}).get('name', ''))
        for row in next_model.mix_rows(snapshot):
            self.rows[row['layer']].update(row)
