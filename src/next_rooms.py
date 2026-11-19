"""Rooms for the next window: the return hero, mood filters, the card grid, Room preview and saved rooms.

Browsing is inert. The only things here that send a command which starts or
changes playback are entering a room (Enter room, or Enter / double-click on a
card) and going Back to the previous room from the toast. Surprise me, filters,
selection and previews never do.
"""
import json
import random
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk

import next_model as model
import next_rooms_model as rooms
from next_mix import ICONS
from next_widgets import FocalPicture, Scene, label, menu_item, name_it

CSS = '''
.next .rooms-chip { min-height: 34px; padding: 0 14px; border-radius: 17px; border: none; box-shadow: none;
  background: rgba(255,255,255,.1); color: rgba(255,255,255,.92); font-weight: 500; }
.next .rooms-chip:hover { background: rgba(255,255,255,.18); }
.next .rooms-chip:checked { background: #ffffff; color: #141416; font-weight: 600; }
.next .rooms-ghost { min-height: 34px; padding: 0 14px; border-radius: 17px; background: transparent;
  border: 1px solid rgba(255,255,255,.28); color: #ffffff; box-shadow: none; }
.next .rooms-ghost:hover { background: rgba(255,255,255,.1); }
.next .rooms-grid { background: transparent; }
.next .rooms-grid flowboxchild { padding: 0; border-radius: 12px; background: transparent; }
.next .rooms-grid flowboxchild:selected { background: transparent; outline: 2px solid rgba(255,255,255,.55); outline-offset: 1px; }
.next .rooms-card { border-radius: 12px; background: #222226; }
.next .rooms-card-name { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 22px; color: #ffffff; }
.next .rooms-card-sub { font-size: 13px; color: rgba(255,255,255,.86); }
.next .rooms-tag { background: rgba(10,10,12,.78); color: #ffffff; border-radius: 11px; padding: 2px 9px;
  font-size: 11px; font-weight: 700; }
.next .rooms-more { background: rgba(10,10,12,.7); color: #ffffff; border-radius: 16px; min-width: 32px; min-height: 32px; padding: 0; }
.next .rooms-more:hover { background: rgba(60,60,66,.9); }
.next .first-run-title { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 40px; color: #ffffff; }
.next.compact .first-run-title { font-size: 30px; }
.next .rooms-empty { color: rgba(255,255,255,.76); font-size: 15px; }
.next .preview-title { font-family: "Newsreader", "Source Serif 4", "Noto Serif", Georgia, serif; font-size: 46px; color: #ffffff; }
.next .preview-sub { font-size: 16px; font-weight: 500; color: rgba(255,255,255,.92); }
.next.compact .preview-title { font-size: 34px; }
.next .preview-desc { font-size: 15px; color: rgba(255,255,255,.86); }
.next .preview-caption, .next .preview-note { font-size: 13px; color: rgba(255,255,255,.72); }
.next .preview-art { border-radius: 16px; background: #0b0c0f; }
.next .preview-row { padding: 6px 0; }
.next .preview-layer { font-size: 12px; font-weight: 600; color: rgba(255,255,255,.7); }
.next .preview-name { font-size: 15px; font-weight: 600; color: #ffffff; }
.next .preview-detail { font-size: 13px; color: rgba(255,255,255,.72); }
.next .preview-hear { border-top: 1px solid rgba(255,255,255,.1); border-bottom: 1px solid rgba(255,255,255,.1); padding: 12px 0; }
.next .preview-tag { background: rgba(255,255,255,.1); border-radius: 12px; padding: 4px 10px; font-size: 12px; font-weight: 500; color: rgba(255,255,255,.88); }
.next .preview-enter { min-height: 52px; border-radius: 26px; font-weight: 600; }
.next .save-line-title { color: rgba(255,255,255,.72); }
'''
_css = None
WIDE_PREVIEW = 740
SAVE_WAIT_S = 8
DETAILS_WIDTH = 380
CARD_WIDTH = 224  # four cards fit from about 1000 px, three from about 740, two below that


def install_css(display):
    global _css
    if _css is None:
        _css = Gtk.CssProvider()
        _css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(display, _css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class RoomsPage:
    def __init__(self, window):
        self.window = window
        self.mood = 'all'
        self.entries, self.shown = [], []
        self.legacy = []           # scenes saved in the classic window
        self.key = None
        self.opened = None
        self.preview_widgets = None
        self.recent_click = None   # (entry, time, window point) of the last single click
        self.activated = (None, 0.)
        self.opened_from = None
        self.card_for = {}
        self.serial = 0
        self.pending_save = None
        install_css(Gdk.Display.get_default())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=200)
        self.widget = self.stack
        # The preview covers the card at once, so a quick second click lands on the preview:
        # it still counts as the double-click that enters the room.
        again = Gtk.GestureClick(button=1, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        again.connect('pressed', self.second_press)
        self.stack.add_controller(again)
        self.stack.add_named(self.build_grid(), 'grid')
        self.stack.connect('map', lambda *_: self.load_scenes())
        self.load_scenes()


    # ---- grid -------------------------------------------------------------------

    def build_grid(self):
        page = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=1180, tightening_threshold=900, margin_start=16, margin_end=16, margin_bottom=24)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.first_run = label('Pick a room to begin', ['first-run-title'], wrap=True)
        self.first_run.set_margin_top(18)
        self.first_run.set_visible(False)
        column.append(self.first_run)
        self.hero = Gtk.Overlay()
        self.hero.add_css_class('hero')
        self.hero.set_overflow(Gtk.Overflow.HIDDEN)
        self.hero.set_size_request(-1, 240)
        self.hero_art = FocalPicture()
        self.hero.set_child(self.hero_art)
        scrim = Gtk.Box(valign=Gtk.Align.END)
        scrim.add_css_class('scrim-bottom')
        scrim.set_size_request(-1, 320)
        self.hero.add_overlay(scrim)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.END, halign=Gtk.Align.START,
                       margin_start=28, margin_bottom=26, margin_end=28)
        self.hero_when = label('', ['hero-when'])
        self.hero_title = label('', ['hero-title'], wrap=True)
        self.hero_detail = label('', ['hero-detail'], wrap=True)
        self.hero_button = Gtk.Button(halign=Gtk.Align.START, margin_top=8)
        self.hero_button.add_css_class('suggested-action')
        self.hero_button.add_css_class('pill')
        self.hero_button.connect('clicked', lambda *_: self.hero_action())
        for widget in (self.hero_when, self.hero_title, self.hero_detail, self.hero_button):
            text.append(widget)
        self.hero.add_overlay(text)
        self.hero_kind = 'first-run'
        column.append(self.hero)
        column.append(self.build_filters())
        self.cards = Gtk.FlowBox(min_children_per_line=2, max_children_per_line=4, column_spacing=16, row_spacing=16,
                                 homogeneous=True, selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False,
                                 valign=Gtk.Align.START)
        self.cards.add_css_class('rooms-grid')
        self.cards.connect('child-activated', lambda _box, child: self.activate(self.card_for.get(child)))
        self.cards.add_tick_callback(self.shape_cards)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.card_key)
        self.cards.add_controller(keys)
        column.append(self.cards)
        self.empty = label('', ['rooms-empty'], wrap=True)
        self.empty.set_visible(False)
        column.append(self.empty)
        clamp.set_child(column)
        page.set_child(clamp)
        return page

    def build_filters(self):
        bar = Gtk.Box(spacing=8)
        wrap = Adw.WrapBox(child_spacing=8, line_spacing=8, hexpand=True)
        first = None
        self.chips = {}
        for key, text in rooms.MOODS:
            chip = Gtk.ToggleButton(label=text)
            chip.add_css_class('rooms-chip')
            if first is None:
                first = chip
            else:
                chip.set_group(first)
            chip.set_active(key == self.mood)
            chip.connect('toggled', lambda b, k=key: self.chose(k) if b.get_active() else None)
            wrap.append(chip)
            self.chips[key] = chip
        bar.append(wrap)
        surprise = Gtk.Button(valign=Gtk.Align.START)
        surprise.add_css_class('rooms-ghost')
        inner = Gtk.Box(spacing=6)
        inner.append(Gtk.Image(icon_name='media-playlist-shuffle-symbolic'))
        inner.append(Gtk.Label(label='Surprise me'))
        surprise.set_child(inner)
        surprise.connect('clicked', lambda *_: self.surprise())
        self.surprise_button = surprise
        bar.append(surprise)
        return bar

    def shape_cards(self, grid, _clock):
        """Keep every card 16:9 at the width the grid actually gives it (a homogeneous grid measures at the minimum)."""
        first = grid.get_first_child()
        width = first.get_width() if first is not None else 0
        height = round(width * 9 / 16)
        if width and height != getattr(self, 'card_height', None):
            self.card_height = height
            child = first
            while child is not None:
                art = child.get_child().get_first_child() if child.get_child() is not None else None
                if art is not None and getattr(art, 'card_art', False):
                    art.set_size_request(CARD_WIDTH, height)
                child = child.get_next_sibling()
        return GLib.SOURCE_CONTINUE

    def chose(self, mood):
        if mood != self.mood:
            self.mood = mood
            self.key = None
            self.render(self.window.snapshot)

    # ---- data ---------------------------------------------------------------------

    def load_scenes(self):
        """Read classic scenes off the main thread; they join Yours at the next render."""
        window = self.window
        if getattr(window, 'closed', False):
            return

        def get():
            import listening_hub as hub
            return [scene for scene in hub.scenes() if isinstance(scene, dict)]

        def apply(value):
            if value != self.legacy:
                self.legacy = value
                self.key = None
                if window.snapshot:
                    self.render(window.snapshot)
            return False

        def done(future):
            try:
                value = future.result()
            except Exception:
                return
            GLib.idle_add(apply, value)
        try:
            window.executor.submit(get).add_done_callback(done)
        except RuntimeError:  # executor already shut down
            pass

    def all_entries(self, snapshot):
        window = self.window
        scenes = rooms.legacy_scenes(self.legacy, window.radio, window.presets, window.catalogue)
        return rooms.entries(snapshot, window.catalogue, window.radio, scenes)

    # ---- render ---------------------------------------------------------------------

    def render(self, snapshot):
        self.render_hero(snapshot)
        self.entries = self.all_entries(snapshot)
        self.shown = rooms.filtered(self.entries, self.mood, snapshot)
        active = (snapshot.get('active') or {})
        playing = {active.get('id')} - {None}
        key = json.dumps([[e['id'], e['name'], e['sub'], e['poster'], e['yours'], e['legacy']] for e in self.shown]
                         + [sorted(playing), self.mood], default=str)
        if key != self.key:
            self.key = key
            self.fill(playing)
        self.refresh_preview(snapshot)
        self.settle_save(snapshot)

    def settle_save(self, snapshot):
        pending = self.pending_save
        if not pending:
            return
        window = self.window
        if pending['id'] in {item['id'] for item in snapshot.get('variants') or []}:
            self.pending_save = None
            undo = None if pending['replaced'] else (lambda: window.command('delete', {'id': pending['id']}))
            window.toast('Saved ' + pending['name'] + ' to Yours', 'Undo' if undo else None, undo)
        elif time.monotonic() > pending['until']:
            self.pending_save = None
            window.toast('Could not save ' + pending['name'])

    def render_hero(self, snapshot):
        window = self.window
        hero = model.hero(snapshot, window.catalogue)
        self.hero_kind = hero['kind']
        first = hero['kind'] == 'first-run'
        self.hero.set_visible(not first)
        self.first_run.set_visible(first)
        self.hero_title.set_text(hero['title'])
        self.hero_detail.set_text(hero['detail'])
        self.hero_when.set_text(model.when_words(hero.get('when'), snapshot.get('now', time.time())) if hero.get('when') else
                                ('In the room' if hero['kind'] == 'now' else ''))
        self.hero_when.set_visible(bool(self.hero_when.get_text()))
        self.hero_button.set_visible(not first)
        self.hero_button.set_label(hero.get('button', ''))
        window.set_artwork(self.hero_art, hero.get('poster'))
        if self.opened is None:
            window.apply_accent(hero.get('accent'))

    def hero_action(self):
        if self.hero_kind == 'now':
            self.window.show_now()
        elif self.hero_kind == 'return':
            self.window.start('return', {}, self.hero_title.get_text() or 'your room')

    def fill(self, playing):
        self.card_for = {}
        while self.cards.get_first_child():
            self.cards.remove(self.cards.get_first_child())
        for entry in self.shown:
            child = self.card(entry, entry['id'] in playing)
            self.card_for[child] = entry
            self.cards.append(child)
        message = ''
        if not self.shown:
            message = ('Nothing saved yet. Enter a room, set the mix you like, then choose Save as my room… from the menu.'
                       if self.mood == 'yours' else 'No rooms fit this mood right now.')
        self.empty.set_text(message)
        self.empty.set_visible(bool(message))

    def card(self, entry, playing):
        overlay = Gtk.Overlay()
        overlay.add_css_class('rooms-card')
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        art = FocalPicture()
        art.set_size_request(CARD_WIDTH, getattr(self, 'card_height', None) or round(CARD_WIDTH * 9 / 16))
        art.card_art = True
        self.window.set_artwork(art, entry['poster'])
        overlay.set_child(art)
        scrim = Gtk.Box(valign=Gtk.Align.END, can_target=False)
        scrim.add_css_class('scrim-bottom')
        scrim.set_size_request(-1, 96)
        overlay.add_overlay(scrim)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.END, margin_start=14,
                       margin_end=14, margin_bottom=11, can_target=False)
        text.append(label(entry['name'], ['rooms-card-name'], ellipsize=True))
        text.append(label(entry['sub'], ['rooms-card-sub'], ellipsize=True))
        overlay.add_overlay(text)
        tags = Gtk.Box(spacing=6, valign=Gtk.Align.START, halign=Gtk.Align.START, margin_top=10, margin_start=10,
                       can_target=False)
        if entry['yours']:
            tags.append(label('Yours', ['rooms-tag']))
        if playing:
            marker = label('Playing', ['rooms-tag', 'accent-fill'])
            tags.append(marker)
        overlay.add_overlay(tags)
        child = Gtk.FlowBoxChild(child=overlay)
        child.menu = None
        if entry['editable']:
            popover = Gtk.Popover()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            box.append(menu_item('Preview', lambda e=entry: self.open_preview(e), popover))
            box.append(menu_item('Rename…', lambda e=entry: self.rename_dialog(e), popover))
            box.append(menu_item('Delete…', lambda e=entry: self.delete_dialog(e), popover))
            popover.set_child(box)
            more = Gtk.MenuButton(icon_name='view-more-symbolic', popover=popover, valign=Gtk.Align.START,
                                  halign=Gtk.Align.END, margin_top=8, margin_end=8)
            more.add_css_class('rooms-more')
            more.set_focusable(True)
            name_it(more, 'Options for ' + entry['name'])
            overlay.add_overlay(more)
            child.menu = more
            secondary = Gtk.GestureClick(button=3)
            secondary.connect('pressed', lambda *_: more.popup())
            overlay.add_controller(secondary)
        click = Gtk.GestureClick(button=1)
        click.connect('released', lambda _g, presses, x, y, e=entry, o=overlay: self.single_click(e, o, x, y)
                      if presses == 1 else None)
        overlay.add_controller(click)
        name_it(child, 'Room ' + entry['name'] + (', yours' if entry['yours'] else '') + (', playing' if playing else ''), '')
        return child

    # ---- activation -----------------------------------------------------------------------

    def single_click(self, entry, source=None, x=0., y=0.):
        """One click previews at once; a second click within the double-click time enters instead."""
        point = None
        if source is not None:
            ok, found = source.compute_point(self.window, Graphene.Point().init(x, y))
            point = (found.x, found.y) if ok else None
        self.recent_click = (entry, time.monotonic(), point)
        self.open_preview(entry)

    def second_press(self, gesture, _presses, x, y):
        """A quick second press on the preview that a first click just opened: enter that room."""
        recent, self.recent_click = self.recent_click, None
        if recent is None or self.opened is None or recent[0]['id'] != self.opened['id']:
            return
        settings = Gtk.Settings.get_default()
        if time.monotonic() - recent[1] > int(settings.get_property('gtk-double-click-time')) / 1000.:
            return
        if recent[2] is not None:
            ok, here = self.stack.compute_point(self.window, Graphene.Point().init(x, y))
            limit = int(settings.get_property('gtk-double-click-distance')) * 2
            if ok and (abs(here.x - recent[2][0]) > limit or abs(here.y - recent[2][1]) > limit):
                return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.activate(recent[0])

    def card_key(self, _controller, keyval, _code, state):
        """Space previews the focused card (Enter enters, by the grid's own activation); Menu opens its options."""
        child = self.cards.get_focus_child()
        entry = self.card_for.get(child)
        if isinstance(self.window.get_focus(), Gtk.MenuButton):
            return False  # Space belongs to the options button when it has focus
        if entry is None or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            self.open_preview(entry)
            return True
        if (keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK)) and child.menu:
            child.menu.popup()
            return True
        return False

    def activate(self, entry):
        """Enter or double-click on a card: an explicit start, except for the room you are already in."""
        if entry is None:
            return
        last, stamp = self.activated
        if last == entry.get('id') and time.monotonic() - stamp < .4:
            return  # the same double-click reported twice
        self.activated = (entry.get('id'), time.monotonic())
        if rooms.is_active(entry, self.window.snapshot):
            self.close_preview(animate=False)   # the first click of the double-click already opened it
            self.window.show_now()
        else:
            self.enter(entry)

    def enter(self, entry, scene_on=None):
        window = self.window
        back = rooms.previous_room(window.snapshot, window.catalogue)
        self.close_preview(animate=False)
        errors_before = dict((window.snapshot or {}).get('errors') or {})

        def entered(snapshot):
            # The controller can say yes and still leave the old room with a layer error: only claim
            # success when the room really changed and no new layer error came with it.
            if snapshot is None or not rooms.is_active(entry, snapshot):
                return
            errors = snapshot.get('errors') or {}
            if any(errors[layer] and errors[layer] != errors_before.get(layer) for layer in errors):
                return
            window.toast('Entered ' + entry['name'], 'Back to ' + back['name'], lambda: self.go_back(back))
        done = (lambda _result: window.inspect_then(entered)) if back else None
        window.start('enter', rooms.enter_args(entry, scene_on), entry['name'], entry.get('poster'), done=done)

    def go_back(self, back):
        self.window.start('enter', back['args'], back['name'], back.get('poster'))

    def surprise(self):
        entry = rooms.surprise(self.entries, self.mood, self.window.snapshot, random)
        if entry is None:
            self.window.toast('No other rooms to suggest here')
        else:
            self.open_preview(entry)

    # ---- preview ---------------------------------------------------------------------------

    def open_preview(self, entry):
        if self.opened is not None:
            self.drop_preview()
        window = self.window
        self.opened = entry
        self.opened_from = next((child for child, shown in self.card_for.items() if shown['id'] == entry['id']), None)
        self.preview_widgets = self.build_preview(entry)
        self.serial += 1
        self.preview_widgets['name'] = 'preview-%d' % self.serial
        self.stack.add_named(self.preview_widgets['page'], self.preview_widgets['name'])
        self.stack.set_visible_child_name(self.preview_widgets['name'])
        window.apply_accent(entry.get('accent'))
        self.preview_widgets['enter'].grab_focus()
        window.kick_idle()

    def close_preview(self, animate=True):
        """Back to the grid. True when a preview was open (so Esc was used)."""
        if self.opened is None:
            return False
        self.drop_preview(animate)
        self.window.apply_accent((model.hero(self.window.snapshot, self.window.catalogue) or {}).get('accent'))
        child = self.opened_from
        self.opened_from = None
        if child is not None and child.get_parent() is not None:
            child.grab_focus()
        return True

    def drop_preview(self, animate=False):
        widgets, self.opened, self.preview_widgets = self.preview_widgets, None, None
        self.stack.set_visible_child_name('grid')
        if widgets:
            widgets['scene'].close()

            def remove():
                if widgets['page'].get_parent() is self.stack and self.preview_widgets is not widgets:
                    self.stack.remove(widgets['page'])
                return False
            GLib.timeout_add(260 if animate else 0, remove)

    def build_preview(self, entry):
        window = self.window
        snapshot = window.snapshot
        view = rooms.preview(entry, snapshot)
        page = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=1180, tightening_threshold=900, margin_start=16, margin_end=16, margin_top=8,
                          margin_bottom=24)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        back = Gtk.Button(halign=Gtk.Align.START)
        back.add_css_class('flat')
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image(icon_name='go-previous-symbolic'))
        back_box.append(Gtk.Label(label='Rooms'))
        back.set_child(back_box)
        back.connect('clicked', lambda *_: self.close_preview())
        column.append(back)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=32)
        # Art, left (above on narrow widths).
        art_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, valign=Gtk.Align.START)
        art_frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False)
        art_frame.add_css_class('preview-art')
        art_frame.set_overflow(Gtk.Overflow.HIDDEN)
        scene = Scene()
        preset = next((item for item in window.presets if item.get('id') == entry.get('view_id')), {})
        settings = snapshot.get('settings') or {}
        motion = bool(window.system_animations and not settings.get('reduced_motion') and not settings.get('still'))
        scene.show_room({'still': preset.get('still_url') or entry['poster'], 'poster': entry['poster'],
                         'loop': preset.get('url') if preset.get('kind') == 'local' else None}, motion)
        art_frame.set_child(scene)
        art_column.append(art_frame)
        caption = label('The scene moves here as a muted preview. Nothing plays until you enter.' if scene.want_motion
                        else 'A still of the scene. Nothing plays until you enter.', ['preview-caption'], wrap=True)
        art_column.append(caption)
        body.append(art_column)
        # Details, right.
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.START)
        details_clamp = Adw.Clamp(maximum_size=DETAILS_WIDTH, tightening_threshold=DETAILS_WIDTH, child=details,
                                  valign=Gtk.Align.START)
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title = label(view['title'], ['preview-title'], wrap=True)
        head.append(title)
        subtitle = label(view['subtitle'], ['preview-sub'], wrap=True)
        subtitle.set_visible(bool(view['subtitle']))
        head.append(subtitle)
        description = label(view['description'], ['preview-desc'], wrap=True)
        description.set_visible(bool(view['description']))
        head.append(description)
        details.append(head)
        hear = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hear.add_css_class('preview-hear')
        hear.append(label("What you'll hear", ['heading']))
        music_name, music_detail = self.row(hear, 'music', 'Music', view['music'], view['music_detail'])
        scene_name, scene_detail = self.row(hear, 'picture', 'Scene', entry['view_name'] if entry['scene_available'] else 'No scene',
                                            view['scene'])
        toggle = Gtk.Switch(active=view['scene_on'], valign=Gtk.Align.CENTER, sensitive=view['scene_available'])
        name_it(toggle, 'Scene', 'Scene' if view['scene_available'] else 'This room has no scene')
        scene_name.get_parent().get_parent().append(toggle)
        self.row(hear, 'atmosphere', 'Ambience', view['ambience'], view['ambience_detail'])
        details.append(hear)
        if view['tags']:
            tags = Adw.WrapBox(child_spacing=8, line_spacing=8)
            for tag in view['tags']:
                tags.append(label(tag, ['preview-tag']))
            details.append(tags)
        enter = Gtk.Button(label=view['button'], hexpand=True)
        enter.add_css_class('suggested-action')
        enter.add_css_class('pill')
        enter.add_css_class('preview-enter')
        enter.connect('clicked', lambda *_: self.press_enter())
        details.append(enter)
        note = label(view['note'], ['preview-note'], wrap=True)
        details.append(note)
        body.append(details_clamp)
        column.append(body)
        clamp.set_child(column)
        page.set_child(clamp)
        widgets = dict(page=page, scene=scene, enter=enter, note=note, scene_detail=scene_detail, toggle=toggle,
                       title=title, body=body, clamp=details_clamp, caption=caption)
        toggle.connect('state-set', lambda _s, state: self.scene_toggled(state) or False)
        body.add_tick_callback(lambda widget, _clock: self.adapt_preview(widget, details_clamp))
        return widgets

    @staticmethod
    def row(parent, layer, heading, name, detail):
        """A What you'll hear row: tile, then the layer word, its name and one detail line."""
        row = Gtk.Box(spacing=12)
        row.add_css_class('preview-row')
        tile = Gtk.Image(icon_name=ICONS[layer], pixel_size=22, valign=Gtk.Align.CENTER)
        tile.add_css_class('layer-tile')
        row.append(tile)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(label(heading, ['preview-layer']))
        name_label = label(name, ['preview-name'], wrap=True)
        detail_label = label(detail, ['preview-detail'], wrap=True)
        detail_label.set_visible(bool(detail))
        text.append(name_label)
        text.append(detail_label)
        row.append(text)
        parent.append(row)
        return name_label, detail_label

    @staticmethod
    def adapt_preview(body, clamp):
        wide = body.get_width() >= WIDE_PREVIEW
        orientation = Gtk.Orientation.HORIZONTAL if wide else Gtk.Orientation.VERTICAL
        if body.get_orientation() != orientation:
            body.set_orientation(orientation)
            clamp.set_maximum_size(DETAILS_WIDTH if wide else 10000)
            clamp.set_tightening_threshold(DETAILS_WIDTH if wide else 10000)
        return GLib.SOURCE_CONTINUE

    def scene_toggled(self, state):
        widgets = self.preview_widgets
        if widgets and self.opened:
            view = rooms.preview(dict(self.opened, scene_on=bool(state)), self.window.snapshot)
            widgets['scene_detail'].set_text(view['scene'])

    def refresh_preview(self, snapshot):
        """Keep the button and the note true as the room underneath changes; nothing else moves."""
        widgets = self.preview_widgets
        if not widgets or not self.opened:
            return
        view = rooms.preview(dict(self.opened, scene_on=widgets['toggle'].get_active()), snapshot)
        widgets['note'].set_text(view['note'])
        widgets['enter'].set_label(view['button'])

    def press_enter(self):
        entry, widgets = self.opened, self.preview_widgets
        if not entry or not widgets:
            return
        if rooms.is_active(entry, self.window.snapshot):
            self.close_preview(animate=False)
            self.window.show_now()
            return
        self.enter(entry, widgets['toggle'].get_active() if entry['scene_available'] else False)

    # ---- saved rooms ----------------------------------------------------------------------------

    def save_dialog(self):
        window = self.window
        snapshot = window.snapshot
        active = snapshot.get('active')
        if not active:
            return
        existing = {item['id'] for item in snapshot.get('variants') or []}
        dialog = Adw.AlertDialog(heading='Save as my room', body='It will keep:')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for title, detail in rooms.save_lines(snapshot):
            line = Gtk.Box(spacing=12)
            heading = label(title, ['save-line-title'])
            heading.set_size_request(96, -1)
            line.append(heading)
            line.append(label(detail, [], wrap=True))
            box.append(line)
        entry = Gtk.Entry(text=active.get('name', ''), activates_default=True)
        name_it(entry, 'Room name', None)
        notice = label('This replaces your saved room with the same name.', ['preview-note'], wrap=True)
        box.append(entry)
        box.append(notice)
        dialog.set_extra_child(box)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('save', 'Save')
        dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('save')
        dialog.set_close_response('cancel')
        window.save_entry = entry

        def changed(*_):
            name = entry.get_text().strip()
            dialog.set_response_enabled('save', bool(name))
            notice.set_visible(bool(name) and rooms.variant_id(name) in existing)
        entry.connect('changed', changed)
        changed()

        def answered(_dialog, response):
            name = entry.get_text().strip()
            if response != 'save' or not name:
                return
            expected = rooms.variant_id(name)
            # Save is queued by the controller, so its reply says nothing about the result:
            # the toast waits until the room shows up in Yours.
            self.pending_save = dict(id=expected, name=name, replaced=expected in existing,
                                     until=time.monotonic() + SAVE_WAIT_S)
            window.command('save', {'name': name})
        dialog.connect('response', answered)
        dialog.present(window)

    def rename_dialog(self, entry):
        window = self.window
        taken = {item['id'] for item in window.snapshot.get('variants') or []} - {entry['id']}
        dialog = Adw.AlertDialog(heading='Rename room')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        field = Gtk.Entry(text=entry['name'], activates_default=True)
        name_it(field, 'Room name', None)
        notice = label('You already have a room with that name.', ['preview-note'], wrap=True)
        box.append(field)
        box.append(notice)
        dialog.set_extra_child(box)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('rename', 'Rename')
        dialog.set_response_appearance('rename', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('rename')
        dialog.set_close_response('cancel')

        def changed(*_):
            name = field.get_text().strip()
            clash = bool(name) and rooms.variant_id(name) in taken
            notice.set_visible(clash)
            dialog.set_response_enabled('rename', bool(name) and name != entry['name'] and not clash)
        field.connect('changed', changed)
        changed()

        def answered(_dialog, response):
            name = field.get_text().strip()
            if response != 'rename' or not name:
                return

            def renamed(result):
                new_id = result['variant']['id']
                window.toast('Renamed to ' + name, 'Undo',
                             lambda: window.command('rename', {'id': new_id, 'name': entry['name']}))
            window.command('rename', {'id': entry['id'], 'name': name}, done=renamed)
        dialog.connect('response', answered)
        dialog.present(window)

    def delete_dialog(self, entry):
        window = self.window
        dialog = Adw.AlertDialog(heading='Delete ' + entry['name'] + '?',
                                 body='This removes your saved room. The rooms that come with the app stay.')
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('delete', 'Delete')
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')

        def answered(_dialog, response):
            if response == 'delete':
                window.command('delete', {'id': entry['id']}, done=lambda _r: window.toast('Deleted ' + entry['name']))
        dialog.connect('response', answered)
        dialog.present(window)
