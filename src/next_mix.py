"""The Mix sheet: one row each for Music, Ambience and Scene."""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Gtk

import next_model
from next_widgets import CARD_SIZE, icon_button, install_css, label, menu_item, name_it, show_art

CSS = '''
.scene-choice { padding: 4px; border-radius: 12px; min-height: 0; min-width: 0; }
.scene-choice.scene-current { outline: 2px solid @accent_bg_color; outline-offset: -2px; }
.scene-thumb { border-radius: 8px; background: rgba(255,255,255,.08); }
.scene-grid flowboxchild { padding: 0; border-radius: 12px; background: transparent; }
.scene-grid flowboxchild:selected { background: transparent; }
.scene-name { font-size: 13px; font-weight: 600; }
.scene-tag { background: rgba(10,10,12,.78); color: #ffffff; border-radius: 9px; padding: 1px 8px; font-size: 11px;
  font-weight: 700; margin: 6px; }
'''
SCENE_THUMB = (148, 83)   # a card poster at 16:9; the build's pre-scaled card file is decoded for it

ICONS = {'music': 'audio-x-generic-symbolic', 'atmosphere': 'weather-showers-symbolic',
         'picture': 'image-x-generic-symbolic'}
# The Mix's own keys (O11-CONTROL), one list for the window's shortcut table to merge: (keys, what they do).
MIX_SHORTCUTS = (('M', 'Pause or play music, in the Mix'),
                 ('A', 'Pause or play the ambience, in the Mix'),
                 ('Shift+Up / Down', 'The focused row’s level, in the Mix'))
LEVEL_STEP = 5


class LayerRow(Gtk.Box):
    """One layer: tile, source and state, then its contextual controls.

    Music and Ambience each pause, resume and stop on their own, from buttons on the row, and Next track
    sits on the row when the source can skip. A layer paused here stays paused when the room resumes;
    room Pause then Play restores exactly what played. The ⋯ menu repeats the row's actions, and shows
    only while it holds more than one item: a lone action is already a visible button."""
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
            self.current_scene = None
            self.choices = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True, row_spacing=6,
                                       column_spacing=6, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
                                       activate_on_single_click=True, valign=Gtk.Align.START)
            self.choices.add_css_class('scene-grid')
            arrows = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            arrows.connect('key-pressed', self.tile_key)
            self.choices.add_controller(arrows)
            self.choices.connect('child-activated', lambda _box, child: child.get_child().emit('clicked'))
            scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                        propagate_natural_width=True, max_content_height=380, child=self.choices)
            popover = Gtk.Popover(child=scroll)
            popover.connect('show', lambda *_: self.fill_choices(popover))
            self.change = Gtk.MenuButton(label='Change…', popover=popover)
        top.append(self.change)
        self.retry = Gtk.Button(label='Try again', valign=Gtk.Align.CENTER, visible=False)
        self.retry.connect('clicked', lambda *_: self.send(*self.args('retry')))
        top.append(self.retry)
        self.playing = self.halt = None
        if layer in ('music', 'atmosphere'):
            # Explicit pause/play (never toggle): a click always means what the button showed.
            self.playing = icon_button('media-playback-pause-symbolic', 'Pause ' + word.lower(),
                                       lambda: self.send(*self.args(self.playing.action)), ['flat'])
            self.playing.action = 'pause'
            top.append(self.playing)
        self.skip = None
        if layer == 'music':
            self.skip = icon_button('media-skip-forward-symbolic', 'Next track',
                                    lambda: self.send(*self.args('next')), ['flat'])
            self.skip.set_visible(False)
            top.append(self.skip)
        self.mute = Gtk.ToggleButton(icon_name='audio-volume-high-symbolic')
        self.mute.connect('toggled', self.mute_toggled)
        if layer == 'picture':
            self.close = icon_button('window-close-symbolic', 'Close scene', lambda: self.send(*self.args('stop')))
        top.append(self.mute if layer != 'picture' else self.close)
        if layer in ('music', 'atmosphere'):
            self.halt = icon_button('media-playback-stop-symbolic', 'Stop ' + word.lower(),
                                    lambda: self.send(*self.args('stop')), ['flat'])
            top.append(self.halt)
        self.menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                                margin_start=6, margin_end=6)
        self.popover = Gtk.Popover(child=self.menu_box)
        self.menu = Gtk.MenuButton(icon_name='view-more-symbolic', popover=self.popover)
        name_it(self.menu, word + ' options')
        self.next_item = menu_item('Next track', lambda: self.send(*self.args('next')), self.popover)
        self.retry_item = menu_item('Try again', lambda: self.send(*self.args('retry')), self.popover)
        self.stop_item = menu_item('Stop', lambda: self.send(*self.args('stop')), self.popover)
        self.change_item = menu_item('Change…', lambda: choose_other(layer) if layer != 'picture' else self.change.popup(),
                                     self.popover)
        for item in (self.next_item, self.retry_item, self.change_item, self.stop_item):
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
        """Scene › Change…: a grid of the scenes' own pictures. Browsing is inert: still images only, and only
        the chosen scene's command is sent."""
        while self.choices.get_first_child():
            self.choices.remove(self.choices.get_first_child())
        root = self.get_root()
        columns = 3 if root is None or root.get_width() >= 640 else 2
        self.choices.set_min_children_per_line(columns)
        self.choices.set_max_children_per_line(columns)
        for scene in self.options(self.layer):
            button = self.scene_choice(scene, popover)
            tile = Gtk.FlowBoxChild(child=button, sensitive=scene['available'])    # the tile takes the focus and the arrows
            self.choices.append(tile)

    def tile_key(self, _controller, keyval, _code, state):
        """Arrow keys move between the scene tiles (Left/Right by one, Up/Down by a line), skipping unavailable ones."""
        step = {Gdk.KEY_Right: 1, Gdk.KEY_Left: -1, Gdk.KEY_Down: self.choices.get_max_children_per_line(),
                Gdk.KEY_Up: -self.choices.get_max_children_per_line()}.get(keyval)
        if step is None or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
            return False
        tiles = []
        child = self.choices.get_first_child()
        while child is not None:
            tiles.append(child)
            child = child.get_next_sibling()
        here = self.choices.get_focus_child()
        if here not in tiles:
            return False
        index = tiles.index(here) + step
        while 0 <= index < len(tiles) and not tiles[index].get_sensitive():
            index += 1 if step > 0 else -1
        if 0 <= index < len(tiles):
            tiles[index].grab_focus()
        return True       # at an end the arrow stays put; it never scrolls or leaves the grid

    def scene_choice(self, scene, popover):
        current = scene['id'] == self.current_scene
        button = Gtk.Button()
        button.add_css_class('flat')
        button.add_css_class('scene-choice')
        if current:
            button.add_css_class('scene-current')
        art = Gtk.Overlay()
        frame = Gtk.Box()       # sets the thumbnail's size; the picture over it never grows it to the image's own
        frame.add_css_class('scene-thumb')
        frame.set_size_request(*SCENE_THUMB)
        art.set_child(frame)
        art.set_overflow(Gtk.Overflow.HIDDEN)
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        art.add_overlay(picture)
        show_art(picture, scene['poster'], CARD_SIZE)
        if not scene['poster']:
            art.add_overlay(Gtk.Image(icon_name=ICONS['picture'], pixel_size=28, opacity=.5))
        note = 'Current' if current else '' if scene['available'] else 'Unavailable'
        if note:
            tag = label(note, ['scene-tag'])
            tag.set_halign(Gtk.Align.START)
            tag.set_valign(Gtk.Align.START)
            art.add_overlay(tag)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(art)
        name = label(scene['name'], ['scene-name'], ellipsize=True)
        name.set_max_width_chars(16)
        box.append(name)
        button.set_child(box)
        name_it(button, scene['name'] + (', current scene' if current else '' if scene['available'] else ', unavailable'),
                None if scene['available'] else 'This scene is not installed')
        button.set_sensitive(scene['available'])
        button.set_focusable(False)
        button.set_can_target(False)     # clicks and keys go to the tile (a FlowBoxChild), which presses this button
        button.connect('clicked', lambda *_: (popover.popdown(), None if current else self.send(*scene['command'])))
        return button

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
        self.subtitle.remove_css_class('accent')
        if row['failed']:
            self.subtitle.add_css_class('error')
        elif state == 'paused':
            self.subtitle.add_css_class('accent')   # "Paused" in the accent colour
        self.retry.set_visible(bool(row['failed']))
        if self.playing is not None:
            paused = state == 'paused'
            self.playing.action = 'play' if paused else 'pause'
            self.playing.set_icon_name('media-playback-start-symbolic' if paused else 'media-playback-pause-symbolic')
            name_it(self.playing, ('Play ' if paused else 'Pause ') + row['title'].lower())
            self.playing.set_visible(state in ('playing', 'paused') and not row['closed'] and not empty)
            self.halt.set_visible(not row['closed'] and not empty)
        if self.skip is not None:
            self.skip.set_visible(bool(row.get('can_next')) and not row['closed'])
        self.next_item.set_visible(bool(row.get('can_next')))
        self.retry_item.set_visible(bool(row['failed']))
        self.stop_item.set_visible(not row['closed'])
        self.change_item.set_visible(not empty)
        items = [w for w in (self.next_item, self.retry_item, self.change_item, self.stop_item) if w.get_visible()]
        self.menu.set_visible(len(items) > 1 and not empty)
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

    def toggle(self):
        """M or A: what the row's play/pause button shows, when it shows."""
        if self.playing is not None and self.playing.get_visible() and self.playing.get_sensitive():
            self.send(*self.args(self.playing.action))
            return True
        return False

    def nudge(self, step):
        """Shift+Up/Down: the row's level, through the same debounced command as a drag."""
        if self.scale is None or not self.scale.get_visible() or not self.controls.get_visible():
            return False
        self.scale.set_value(max(0, min(100, self.scale.get_value() + step)))
        return True


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
        self.save_button = menu_item('Save as my room…', save, None, css=('pill',))
        head.append(self.save_button)
        head.append(icon_button('go-down-symbolic', 'Close mix', close, ['circular']))
        self.append(head)
        install_css(Gdk.Display.get_default(), CSS)
        self.rows = {name: LayerRow(name, send, choose_other, options) for name in next_model.LAYER_ORDER}
        for row in self.rows.values():
            self.append(row)
        # While a room connects the Mix shows only progress and Cancel (N9): nothing it shows could act yet.
        self.connecting = Gtk.Box(spacing=14, margin_top=18, margin_bottom=18, visible=False)
        self.connecting.add_css_class('layer-row')
        self.connecting_spinner = Gtk.Spinner(spinning=False, valign=Gtk.Align.CENTER)
        self.connecting_spinner.set_size_request(22, 22)
        self.connecting.append(self.connecting_spinner)
        self.connecting_text = label('', ['layer-title'], ellipsize=True)
        self.connecting_text.set_hexpand(True)
        self.connecting.append(self.connecting_text)
        self.cancel_button = Gtk.Button(label='Cancel', valign=Gtk.Align.CENTER)
        self.cancel_button.connect('clicked', lambda *_: self.cancel_clicked())
        self.connecting.append(self.cancel_button)
        self.append(self.connecting)
        self.on_cancel = None
        self.keys = None
        self.connect('realize', lambda *_: self.install_keys())

    def show_connecting(self, name, cancel=None):
        """``name`` while a start connects (with ``cancel``, the honest Cancel), None once it ended."""
        connecting = name is not None
        self.on_cancel = cancel if connecting else None
        self.connecting.set_visible(connecting)
        self.connecting_spinner.set_spinning(connecting)
        for widget in list(self.rows.values()) + [self.save_button]:
            widget.set_visible(not connecting)
        if connecting:
            self.room.set_text('')
            self.connecting_text.set_text('Connecting to ' + name + '…')
            self.cancel_button.set_visible(cancel is not None)
            self.cancel_button.set_sensitive(True)
            self.cancel_button.set_label('Cancel')

    def cancel_clicked(self):
        if self.on_cancel is not None:
            self.cancel_button.set_sensitive(False)   # one Cancel per start
            self.cancel_button.set_label('Cancelling…')
            self.on_cancel()

    def update(self, snapshot):
        if not self.connecting.get_visible():
            self.room.set_text((snapshot.get('active') or {}).get('name', ''))
        self.rows['picture'].current_scene = ((snapshot.get('active') or {}).get('picture') or {}).get('preset_id')
        for row in next_model.mix_rows(snapshot):
            self.rows[row['layer']].update(row)

    # ---- keys -----------------------------------------------------------------------------

    def install_keys(self):
        """M and A pause or play Music and Ambience, Shift+Up/Down the focused row's level, while the Mix shows.

        On the window in the capture phase, so a focused button or slider does not take the key first."""
        root = self.get_root()
        if self.keys is not None or root is None:
            return
        self.keys = Gtk.EventControllerKey()
        self.keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.keys.connect('key-pressed', lambda _c, keyval, _code, state: self.key_pressed(keyval, state))
        root.add_controller(self.keys)

    def shown(self):
        node = self.get_parent()
        while node is not None and not isinstance(node, Adw.BottomSheet):
            node = node.get_parent()
        return self.get_mapped() and (node is None or node.get_open())

    def key_pressed(self, keyval, state):
        if not self.shown() or self.connecting.get_visible():
            return False
        root = self.get_root()
        if root is not None and hasattr(root, 'get_visible_dialog') and root.get_visible_dialog() is not None:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        focus = root.get_focus() if root is not None else None
        node = focus
        while node is not None:
            if isinstance(node, (Gtk.Editable, Gtk.Popover)):
                return False      # typing, or a menu: the key is theirs
            node = node.get_parent()
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down) and shift:
            row = self.focused_row(focus)
            return bool(row and row.nudge(LEVEL_STEP if keyval == Gdk.KEY_Up else -LEVEL_STEP))
        if shift:
            return False
        if keyval in (Gdk.KEY_m, Gdk.KEY_M):
            return self.rows['music'].toggle()
        if keyval in (Gdk.KEY_a, Gdk.KEY_A):
            return self.rows['atmosphere'].toggle()
        return False

    def focused_row(self, focus):
        node = focus
        while node is not None and node is not self:
            if isinstance(node, LayerRow):
                return node
            node = node.get_parent()
        return None
