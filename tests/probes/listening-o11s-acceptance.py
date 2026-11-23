"""O11-SHELL acceptance: independent oracles for the shortcuts dialog, first-run focus, wide Rooms, the scene
thumbnail grid, the no-room Now state and the 560-700 px Listen leftovers (UX assessment 2026-11-21, N13).

Written by the acceptance author, not the implementer, from the brief and the cited assessment only.
Real GTK on a private Xvfb with its own D-Bus (tests/probes/o6_env.py re-executes this file there), the real
session controller on the session test double and fixture providers. Nothing reaches the desktop session,
the network or real credentials.

Keys: the dev shell has no xdotool, so a Tab or arrow press is `window.child_focus(direction)`, which is what
GTK's own Tab and arrow bindings call (GtkWindow move-focus) once no key controller has taken the key; the
probe checks that no key controller would have taken it (nothing plays at first run). Activation is
`widget.activate()`, the same signal Enter emits.

Sections are isolated: one that raises records a FAIL and the rest still run.

    python3 tests/probes/listening-o11s-acceptance.py --repo WORKTREE --output OUTDIR
    (O11S_SECTIONS=A,C limits the run to those sections)

Numeric limits and where they come from (the phase-1 report lists the self-chosen ones for a ruling):
- 45% of the window area at 1440x900: the brief (criterion 3).
- 360 px, 560-700 px, 600-800 px widths: the brief (criteria 3 and 6).
- card-size thumbnails, no wider than poster_cards.CARD_SIZE (640 px): the brief ("pre-scaled to card size in O10").
- SELF-CHOSEN: FINGERPRINT_LIMIT, the mean absolute grey-level difference (0-255) between 16x9 thumbnails below
  which two images count as "the same art" (used for "this tile shows that scene's poster" and "Now shows a
  room's art").
- SELF-CHOSEN: NOT_WORSE_TOLERANCE, how far below main's measured Rooms content share at 600/700/800 px a
  branch may fall and still be "not worse". The baselines themselves are measured on main 70db15e.
"""
import ast
import io
import json
import os
from pathlib import Path
import re
import time
import tokenize
import traceback

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o11s-acceptance', extra_args={'--repo': {'default': None}})
checks = []
ONLY = set(filter(None, os.environ.get('O11S_SECTIONS', '').split(',')))

# SELF-CHOSEN, then calibrated (coordinator ruling, phase 1): across the catalogue art a poster and its .card.jpg differ
# by at most 0.08, a room's poster and its still by at most 0.33, and two different rooms by at least 13.24
# (moonlit-lake vs room-midnight-library, both dark). [ORIGINAL, superseded by this calibration] 14, which sat above
# that closest distinct pair, so two dark rooms could have counted as the same art.
FINGERPRINT_LIMIT = 3
NOT_WORSE_TOLERANCE = 0.01      # SELF-CHOSEN: one percentage point of the window area
# Rooms content share of the window area on main 70db15e (2.5.0), first run, measured by this probe's
# content_share(); the evidence directory keeps the run and the screenshots. Width -> share (height 800).
MAIN_SHARE = {600: 0.615, 700: 0.639, 800: 0.423}
WIDE = (1440, 900)
WIDE_SHARE = 0.45               # the brief, criterion 3
RESERVED_MIX = ('m', 'a', 'shift+up', 'shift+down')   # O11-CONTROL keys the brief reserves in the table


def check(name, ok, detail=''):
    checks.append({'criterion': name, 'pass': bool(ok), 'detail': str(detail)[:900]})
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:900]), flush=True)


def superseded(name, ok, detail=''):
    """An ORIGINAL kept visible after a revision, printed but not counted: its result depends on the design rather
    than on the outcome, so it can be neither a check nor a stable known failure."""
    print('SUPERSEDED %s %s  %s' % ('pass' if ok else 'fail', name, str(detail)[:400]), flush=True)


def section(name):
    def wrap(function):
        if ONLY and name not in ONLY:
            return function
        try:
            function()
        except Exception as error:   # one broken section never hides the others
            check('%s: section ran without raising' % name, False, '%s: %s\n%s' % (
                type(error).__name__, error, traceback.format_exc()[-1500:]))
        finally:
            close_all()
        return function
    return wrap


def close_all():
    for window in list(env.app.get_windows()):
        window.close()
    env.pump(.4)


with env:
    Gtk, Gdk, GLib, Adw = env.Gtk, env.Gdk, env.GLib, env.Adw
    pump, settle = env.pump, env.settle
    from unittest import mock
    import ambience
    import next_widgets
    import poster_cards
    from PIL import Image, ImageChops, ImageStat

    READ_ONLY = {'inspect', 'get', 'variants', 'station.list', 'station.recent', 'outputs'}
    STARTS = ('music', 'online', 'link', 'station.start', 'station.replay', 'enter', 'return', 'resume')

    # One local room scene is made unavailable (its file "missing"), so the grid has an offline scene to show.
    # "Offline" in the brief is read as unavailable here: the scene names already say "· offline room" for scenes
    # that need no network, and those are the ones that play.
    real_presets = ambience.presets
    UNAVAILABLE = {}

    def presets_with_one_missing():
        items = real_presets()
        rooms = [p for p in items if p.get('kind') == 'local' and p.get('poster_path') and p.get('available')]
        if rooms:
            target = rooms[-1]
            target['available'] = False
            UNAVAILABLE['id'] = target['id']
        return items
    mock.patch.object(ambience, 'presets', side_effect=presets_with_one_missing).start()

    # ---------------------------------------------------------------- helpers
    def widgets(root):
        return list(env.widgets(root)) if root is not None else []

    def inside(widget, ancestor):
        node = widget
        while node is not None:
            if node is ancestor:
                return True
            node = node.get_parent()
        return False

    def bounds(widget, window):
        if widget is None or not widget.get_mapped():
            return None
        ok, rect = widget.compute_bounds(window)
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok else None

    def commands(mark):
        return [(a, k) for a, k in env.log[mark:] if a not in READ_ONLY and not a.startswith('metadata.')]

    def starts(mark):
        return [a for a, _ in commands(mark) if a in STARTS]

    def wait(predicate, seconds=6.):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            pump(.2)
        return bool(predicate())

    def leave():
        if env.controller.active:
            env.service.execute('leave', {})
            settle(1.0)
        wait(lambda: env.controller.active is None and not env.controller.transition, 8)   # Leave fades out first
        with env.controller.lock:
            env.controller.video = None
        settle(.4)

    def launch(width=1200, height=800, page=None):
        close_all()
        return env.launch(width=width, height=height, page=page)

    def text_of(root):
        found = []
        for w in widgets(root):
            if not w.get_mapped():
                continue
            for getter in ('get_label', 'get_text', 'get_title', 'get_subtitle', 'get_tooltip_text'):
                value = getattr(w, getter, lambda: None)()
                if isinstance(value, str) and value:
                    found.append(value)
        return found

    def describe(widget):
        return None if widget is None else '%s %s' % (type(widget).__name__, ' / '.join(text_of(widget))[:60])

    def trigger_matches(trigger, keyval, mods):
        if isinstance(trigger, Gtk.AlternativeTrigger):
            return trigger_matches(trigger.get_first(), keyval, mods) or trigger_matches(trigger.get_second(), keyval, mods)
        if isinstance(trigger, Gtk.KeyvalTrigger):
            mask = int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK)
            return (Gdk.keyval_to_lower(trigger.get_keyval()) == Gdk.keyval_to_lower(keyval)
                    and int(trigger.get_modifiers()) & mask == mods & mask)
        return False

    def press(root, keyval, mods=0):
        """One key press dispatched the way GTK does (revision D7-R1): capture-phase controllers from the root down
        to the focus, then target/bubble from the focus up, each widget's key controllers and shortcut controllers
        (its class shortcuts included, so the window's own arrow and Tab bindings run last, as in GTK). True when
        something handled it. Needed because a focused button inside a container leaves arrows to the container's
        own bindings, which child_focus() on the window skips."""
        path, node = [], root.get_focus()
        while node is not None:
            path.append(node)
            node = node.get_parent()
        path = path or [root]

        def handle(widget, phases):
            model = widget.observe_controllers()
            for index in range(model.get_n_items()):
                controller = model.get_item(index)
                if controller.get_propagation_phase() not in phases:
                    continue
                if isinstance(controller, Gtk.EventControllerKey):
                    if controller.emit('key-pressed', keyval, 0, Gdk.ModifierType(mods)):
                        return True
                elif isinstance(controller, Gtk.ShortcutController):
                    for item in range(controller.get_n_items()):
                        shortcut = controller.get_item(item)
                        if trigger_matches(shortcut.get_trigger(), keyval, mods) and shortcut.get_action() is not None:
                            if shortcut.get_action().activate(Gtk.ShortcutActionFlags(0), widget, shortcut.get_arguments()):
                                return True
            return False
        capture, bubble = (Gtk.PropagationPhase.CAPTURE,), (Gtk.PropagationPhase.BUBBLE, Gtk.PropagationPhase.TARGET)
        return any(handle(w, capture) for w in reversed(path)) or any(handle(w, bubble) for w in path)

    def paintable_of(widget):
        getter = getattr(widget, 'get_paintable', None)
        value = None
        if callable(getter):
            try:
                value = getter()
            except TypeError:
                value = None
        if value is None:
            value = getattr(widget, 'paintable', None)
        return value if isinstance(value, Gdk.Paintable) else None

    def ellipsized(label):
        layout = label.get_layout()
        return bool(label.get_mapped() and label.get_ellipsize() != 0 and layout is not None and layout.is_ellipsized())

    def union_area(rects):
        """Area covered by axis-aligned rectangles (x, y, w, h), overlaps counted once."""
        rects = [r for r in rects if r and r[2] > 0 and r[3] > 0]
        if not rects:
            return 0.
        xs = sorted({r[0] for r in rects} | {r[0] + r[2] for r in rects})
        area = 0.
        for left, right in zip(xs, xs[1:]):
            spans = sorted((r[1], r[1] + r[3]) for r in rects if r[0] <= left and r[0] + r[2] >= right)
            covered, end = 0., None
            for top, bottom in spans:
                if end is None or top > end:
                    covered += bottom - top
                    end = bottom
                elif bottom > end:
                    covered += bottom - end
                    end = bottom
            area += covered * (right - left)
        return area

    def clip(rect, box):
        if not rect:
            return None
        x0, y0 = max(rect[0], box[0]), max(rect[1], box[1])
        x1, y1 = min(rect[0] + rect[2], box[0] + box[2]), min(rect[1] + rect[3], box[1] + box[3])
        return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None

    # ---- image fingerprints (16x9 grey thumbnails) -----------------------------------------
    def fingerprint_image(image):
        return image.convert('L').resize((16, 9), Image.BILINEAR)

    def fingerprint_file(path):
        try:
            with Image.open(path) as image:
                return fingerprint_image(image)
        except (OSError, ValueError):
            return None

    def fingerprint_paintable(paintable):
        if isinstance(paintable, Gdk.Texture):
            try:
                data = paintable.save_to_png_bytes().get_data()
                with Image.open(io.BytesIO(data)) as image:
                    return fingerprint_image(image)
            except Exception:
                return None
        return None

    def difference(a, b):
        return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]

    def room_art(window):
        """Every room's art the app could show: scene stills, posters and their card files, and catalogue posters."""
        paths = set()
        for preset in window.presets or []:
            for key in ('still_url', 'poster_path'):
                if preset.get(key):
                    paths.add(preset[key])
        for room in window.catalogue or []:
            if room.get('poster_path'):
                paths.add(room['poster_path'])
        for path in list(paths):
            card = poster_cards.card_file(path)
            if card:
                paths.add(str(card))
        return {path: fp for path in paths if Path(path).is_file() for fp in [fingerprint_file(path)] if fp is not None}

    def matches_art(fp, arts):
        best = min(((difference(fp, other), path) for path, other in arts.items()), default=(999, None))
        return best

    # =========================================================================== A: first-run focus (criterion 2)
    def first_card(window):
        cards = [c for c in window.rooms.card_for if c.get_mapped()]
        placed = [(bounds(c, window), c) for c in cards]
        placed = [(b, c) for b, c in placed if b]
        return min(placed, key=lambda item: (round(item[0][1]), item[0][0]))[1] if placed else None

    def hero_primary(window):
        """A first-run hero's primary action: a mapped suggested-action button in Rooms, outside the cards."""
        found = []
        for w in widgets(window.rooms.widget):
            if isinstance(w, Gtk.Button) and w.get_mapped() and w.has_css_class('suggested-action') \
                    and not any(inside(w, c) for c in window.rooms.card_for):
                found.append(w)
        return found

    def on_first_target(window, focus):
        card = first_card(window)
        if focus is None:
            return False
        if card is not None and inside(focus, card) and not isinstance(focus, Gtk.MenuButton):
            return True
        return any(inside(focus, b) for b in hero_primary(window))

    def describe_focus(window, focus):
        if focus is None:
            return 'None'
        where = 'header' if inside(focus, window.header) else ('rooms' if inside(focus, window.rooms.widget) else 'elsewhere')
        return '%s in %s (%s)' % (type(focus).__name__, where, ' / '.join(text_of(focus))[:80])

    @section('A')
    def first_run_focus():
        leave()
        snapshot = env.controller.snapshot()
        fresh = not snapshot.get('active') and not snapshot.get('last_session') and not snapshot.get('recent')
        check('A0 precondition: no room and no history (a first run)', fresh,
              {k: bool(snapshot.get(k)) for k in ('active', 'last_session', 'recent')})
        for width, height in ((1200, 800), WIDE):
            for name, direction in (('Tab', Gtk.DirectionType.TAB_FORWARD), ('Down', Gtk.DirectionType.DOWN),
                                    ('Right', Gtk.DirectionType.RIGHT)):
                mark = len(env.log)
                window = launch(width, height)
                pump(.8)
                launched = window.get_focus()
                page = (window.current_page(), window.browse.get_visible_child_name())
                if name == 'Tab':
                    check('A1 at %dx%d the first run opens on Rooms with nothing focused in the header' % (width, height),
                          page == ('browse', 'rooms') and not (launched is not None and inside(launched, window.header)),
                          (page, describe_focus(window, launched)))
                already = on_first_target(window, launched)
                window.child_focus(direction)
                pump(.4)
                focus = window.get_focus()
                landed = on_first_target(window, focus)
                detail = ('launch: ' + describe_focus(window, launched), 'after: ' + describe_focus(window, focus),
                          'launch already on target' if already else '')
                # [ORIGINAL, superseded by A2-R1] Literal reading: after the first press focus is on the first card or
                # the hero's primary action. Its result depends on the design (a launch focus on the first card that
                # the press moves away from fails it), so it is printed, uncounted, instead of being a check.
                original = landed and not inside(focus, window.header)
                print('SUPERSEDED %s A2 [ORIGINAL, superseded by A2-R1] at %dx%d the first %s press lands on the first '
                      'room card or the hero\'s primary action, not in the header  %r'
                      % ('pass' if original else 'fail', width, height, name, detail), flush=True)
                # A2-R1 (coordinator ruling, phase 1, 2026-11-22): ready for the keyboard means the first card (or a
                # hero's primary action) has focus at launch, OR the first press lands there. Header focus, at launch
                # or after the press, always fails.
                header = (launched is not None and inside(launched, window.header)) or inside(focus, window.header)
                check('A2-R1 at %dx%d the first card or the hero\'s primary action has focus at launch or after the first '
                      '%s press, and the header never has it' % (width, height, name),
                      (already or landed) and not header, detail)
                if name == 'Tab':
                    check('A3 at %dx%d moving focus onto a card is inert: no command, no preview, nothing starts'
                          % (width, height), not commands(mark) and window.rooms.opened is None and not env.controller.active,
                          (commands(mark), window.rooms.opened))

    # =========================================================================== B: wide Rooms (criterion 3)
    def content_share(window):
        """The share of the window area covered by Rooms content: cards, plus any hero (css class containing
        'hero') and any picture outside the cards, clipped to the visible Rooms viewport."""
        grid_page = window.rooms.stack.get_child_by_name('grid') or window.rooms.widget
        width, height = window.get_width(), window.get_height()
        view = clip(bounds(grid_page, window), (0, 0, width, height))
        if not view:
            return 0., {}
        rects, parts = [], {'cards': 0, 'hero': 0, 'pictures': 0}
        cards = [c for c in window.rooms.card_for if c.get_mapped()]
        for card in cards:
            rect = clip(bounds(card, window), view)
            if rect:
                rects.append(rect)
                parts['cards'] += 1
        for w in widgets(grid_page):
            if not w.get_mapped() or any(inside(w, c) for c in cards):
                continue
            hero = any('hero' in css for css in w.get_css_classes()) and w.get_opacity() > 0
            picture = paintable_of(w) is not None and w.get_width() > 32 and w.get_height() > 32
            if hero or picture:
                rect = clip(bounds(w, window), view)
                if rect:
                    rects.append(rect)
                    parts['hero' if hero else 'pictures'] += 1
        return union_area(rects) / float(width * height), parts

    def rooms_at(width, height, narrow=False):
        window = launch(width, height)
        if narrow:
            window.browse_rooms()
        else:
            window.show_rooms()
        pump(1.5)    # card art decodes off the GTK thread
        return window

    def card_layout(window):
        clipped, cut = [], []
        width = window.get_width()
        for card, entry in window.rooms.card_for.items():
            rect = bounds(card, window)
            if not rect:
                continue
            if rect[0] < -.5 or rect[0] + rect[2] > width + .5:
                clipped.append((entry.get('name'), rect))
            for w in widgets(card):
                if isinstance(w, Gtk.Label) and ellipsized(w) and w.get_text() == entry.get('name'):
                    cut.append(entry.get('name'))
        return clipped, cut

    @section('B')
    def wide_rooms():
        leave()
        window = rooms_at(*WIDE)
        share, parts = content_share(window)
        env.shot(window, 'o11s-b-rooms-1440-first-run')
        print('measure: first run 1440x900 Rooms content share %.3f %r' % (share, parts))
        check('B1 at 1440x900 on first run the Rooms content fills at least 45%% of the window (share %.3f)' % share,
              share >= WIDE_SHARE, (round(share, 3), parts))
        # Adversarial: an empty box called a hero must not be what fills it; the cards themselves stay readable.
        clipped, cut = card_layout(window)
        check('B2 at 1440x900 no card is cut off at the sides and no room name is ellipsized', not clipped and not cut,
              (clipped, cut))
        # Legible down to 360 px (Browse rooms from the narrow layout shows Rooms itself).
        window = rooms_at(360, 800, narrow=True)
        env.shot(window, 'o11s-b-rooms-360')
        clipped, cut = card_layout(window)
        shown = [c for c in window.rooms.card_for if c.get_mapped()]
        check('B3 at 360 px the cards show whole across, with their names not ellipsized', shown and not clipped and not cut,
              (len(shown), clipped, cut, window.get_width()))
        # 600-800 px: not worse than main.
        for width in (600, 700, 800):
            window = rooms_at(width, 800)
            share, parts = content_share(window)
            env.shot(window, 'o11s-b-rooms-%d' % width)
            clipped, cut = card_layout(window)
            base = MAIN_SHARE.get(width)
            print('measure: first run %dx800 Rooms content share %.3f %r (main %s)' % (width, share, parts, base))
            check('B4 at %d px the Rooms content share is not below main\'s (share %.3f)' % (width, share),
                  base is not None and share >= base - NOT_WORSE_TOLERANCE, (round(share, 3), base, parts))
            check('B5 at %d px no card is cut off at the sides and no room name is ellipsized' % width,
                  not clipped and not cut, (clipped, cut))

    # =========================================================================== C: shortcuts (criterion 1)
    MODS = None

    def norm(keyval, mods):
        global MODS
        if MODS is None:
            MODS = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK
                    | Gdk.ModifierType.SUPER_MASK)
        mods = Gdk.ModifierType(int(mods) & int(MODS))
        if keyval == Gdk.KEY_question:
            keyval, mods = Gdk.KEY_slash, mods | Gdk.ModifierType.SHIFT_MASK
        keyval = Gdk.keyval_to_lower(keyval)
        if keyval == Gdk.KEY_KP_Space:
            keyval = Gdk.KEY_space
        if keyval in (Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            keyval = Gdk.KEY_Return
        names = []
        for flag, word in ((Gdk.ModifierType.CONTROL_MASK, 'ctrl'), (Gdk.ModifierType.SHIFT_MASK, 'shift'),
                           (Gdk.ModifierType.ALT_MASK, 'alt'), (Gdk.ModifierType.SUPER_MASK, 'super')):
            if int(mods) & int(flag):
                names.append(word)
        return '+'.join(names + [(Gdk.keyval_name(keyval) or str(keyval)).lower()])

    def parse_one(token):
        parsed = Gtk.accelerator_parse(token)
        ok, keyval, mods = parsed if len(parsed) == 3 else (True,) + tuple(parsed)
        return norm(keyval, mods) if ok and keyval else None

    def parse_accelerators(text):
        """Every trigger an Adw.ShortcutsItem accelerator string shows (alternatives split by spaces, ranges a...b)."""
        found = []
        for token in (text or '').split():
            if '...' in token or '…' in token:
                first, last = re.split(r'\.\.\.|…', token, 1)
                a, b = parse_one(first), parse_one(last)
                if a and b and a[-1].isdigit() and b[-1].isdigit() and a[:-1] == b[:-1]:
                    found += [a[:-1] + str(d) for d in range(int(a[-1]), int(b[-1]) + 1)]
                else:
                    found += [x for x in (a, b) if x]
                continue
            for part in token.split('&'):
                value = parse_one(part)
                if value:
                    found.append(value)
        return found

    DIALOG_SECTIONS = {}
    real_add = Adw.ShortcutsDialog.add

    def recording_add(self, section_object):
        DIALOG_SECTIONS.setdefault(id(self), []).append(section_object)
        return real_add(self, section_object)
    mock.patch.object(Adw.ShortcutsDialog, 'add', recording_add).start()

    def dialog_rows(dialog, app):
        """(section title, row title, [triggers]) for every row, from the sections the code added, else the tree."""
        rows = []
        for section_object in DIALOG_SECTIONS.get(id(dialog), []):
            for index in range(section_object.get_n_items()):
                item = section_object.get_item(index)
                accel = item.get_accelerator() or ''
                triggers = parse_accelerators(accel)
                if not triggers and item.get_action_name():
                    for value in app.get_accels_for_action(item.get_action_name()):
                        triggers += parse_accelerators(value)
                rows.append((section_object.get_title() or '', item.get_title() or '', triggers))
        if rows:
            return rows, 'sections'
        for w in widgets(dialog):
            if type(w).__name__.endswith('ShortcutRow') and w.get_mapped():
                group = w.get_ancestor(Adw.PreferencesGroup)
                labels = [x for x in widgets(w) if hasattr(x, 'get_accelerator')]
                triggers = []
                for x in labels:
                    triggers += parse_accelerators(x.get_accelerator())
                rows.append(((group.get_title() if group else '') or '', w.get_title() or '', triggers))
        return rows, 'tree'

    def installed(window):
        app = window.get_application()
        actions = {}
        for name in app.list_action_descriptions():
            accels = app.get_accels_for_action(name)
            if accels:
                actions[name] = sorted({t for a in accels for t in parse_accelerators(a)})
        controllers = {}
        for w in [window] + widgets(window):
            for controller in w.observe_controllers():
                if not isinstance(controller, Gtk.ShortcutController):
                    continue
                # App-wide shortcuts only (GLOBAL or MANAGED scope, as Listen's Ctrl+F/L/V are). LOCAL controllers
                # include the toolkit's own class shortcuts (libadwaita's Ctrl+Shift+M adaptive preview, Escape).
                if controller.get_scope() == Gtk.ShortcutScope.LOCAL:
                    continue
                for index in range(controller.get_n_items()):
                    shortcut = controller.get_item(index)
                    stack = [shortcut.get_trigger()]
                    while stack:
                        trigger = stack.pop()
                        if isinstance(trigger, Gtk.AlternativeTrigger):
                            stack += [trigger.get_first(), trigger.get_second()]
                        elif isinstance(trigger, Gtk.KeyvalTrigger):
                            controllers[norm(trigger.get_keyval(), trigger.get_modifiers())] = type(w).__name__
        return actions, controllers

    # Keys that key controllers handle (they cannot be enumerated): main's own documented table lists these.
    KEY_CONTROLLER_KEYS = {'space': 'Pause or play', 'escape': 'Close or go back', 'up': 'Volume up', 'down': 'Volume down'}
    # Other keys the shipped key handlers take; allowed in the dialog, not required (except the card options key).
    # libadwaita's own global shortcut (Ctrl+Shift+M toggles its adaptive preview); not the app's to list.
    TOOLKIT_KEYS = {'ctrl+shift+m'}
    HANDLED_KEYS = set(KEY_CONTROLLER_KEYS) | TOOLKIT_KEYS | {'return', 'menu', 'shift+f10', 'tab', 'shift+tab', 'shift+iso_left_tab'}

    def open_shortcuts(window):
        before = window.get_visible_dialog()
        window.activate_action('win.shortcuts', None)
        pump(.8)
        dialog = window.get_visible_dialog()
        return dialog if dialog is not before else dialog

    def string_constants(tree):
        """(value, nearest enclosing statement line) for every string constant that is not a docstring."""
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        found = []

        def visit(node, stmt):
            if isinstance(node, ast.stmt):
                stmt = node
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                found.append((node.value, stmt.lineno if stmt is not None else 0))
            for child in ast.iter_child_nodes(node):
                if isinstance(node, ast.stmt) and isinstance(child, ast.stmt):
                    visit(child, child)
                else:
                    visit(child, stmt)
        visit(tree, None)
        return found

    def literal_trigger(value):
        """A Ctrl shortcut written as a string: a GTK accelerator ('<Control>m') or a display label ('Ctrl+M')."""
        text = value.strip()
        match = re.fullmatch(r'Ctrl\s*\+\s*(.+)', text)
        if match:
            key = match.group(1)
            names = {'?': 'question', ',': 'comma', '.': 'period', '/': 'slash'}
            name = names.get(key, key if len(key) > 1 else key.lower())
            keyval = Gdk.keyval_from_name(name)
            return norm(keyval, Gdk.ModifierType.CONTROL_MASK) if keyval else None
        if text.startswith('<') and '>' in text and ' ' not in text:
            value = parse_one(text)
            return value if value and value.startswith('ctrl') else None
        return None

    @section('C')
    def shortcuts():
        leave()
        window = launch(1200, 800, page='rooms')
        app = window.get_application()
        actions, controllers = installed(window)
        print('installed: app accels %r; shortcut controllers %r' % (actions, controllers))
        check('C0 precondition: Ctrl+? is the shortcuts action\'s accelerator',
              'ctrl+shift+slash' in actions.get('win.shortcuts', []), actions.get('win.shortcuts'))
        dialog = open_shortcuts(window)
        check('C1 Keyboard shortcuts opens an Adw.ShortcutsDialog (not a text alert)',
              isinstance(dialog, Adw.ShortcutsDialog), type(dialog).__name__ if dialog is not None else None)
        rows, source = dialog_rows(dialog, app) if isinstance(dialog, Adw.ShortcutsDialog) else ([], 'none')
        shown = {t for _s, _t, triggers in rows for t in triggers}
        print('dialog rows (%s): %r' % (source, rows))
        missing = {name: triggers for name, triggers in actions.items() if not set(triggers) & shown}
        check('C2 every application accelerator (each window action with a key) is listed', bool(rows) and not missing,
              missing or 'no rows')
        missing = sorted(t for t in controllers if t not in shown and t not in TOOLKIT_KEYS)
        check('C3 every key of the window\'s shortcut controllers (Listen: Ctrl+F, Ctrl+L, Ctrl+V) is listed',
              bool(rows) and not missing, missing or 'no rows')
        missing = sorted(t for t in KEY_CONTROLLER_KEYS if t not in shown)
        check('C4 the keys handled by key controllers are listed (Space, Esc, Up, Down)', bool(rows) and not missing,
              missing or 'no rows')
        # Interpretation (flagged in the phase-1 report): the Rooms card keys are installed shortcuts too.
        check('C5 the Rooms card options key (Menu or Shift+F10) is listed', bool(shown & {'menu', 'shift+f10'}),
              sorted(shown))
        known = set(controllers) | {t for ts in actions.values() for t in ts} | HANDLED_KEYS
        stale = sorted({t for t in shown if t not in known and t not in RESERVED_MIX})
        check('C6 the dialog lists no key that nothing installs (the reserved Mix keys aside)', bool(rows) and not stale,
              stale or 'no rows')
        untitled = [r for r in rows if not r[1].strip()]
        check('C7 every row has a title', bool(rows) and not untitled, untitled)
        sections = sorted({s for s, _t, _k in rows})
        listen = {s for s, _t, ks in rows if set(ks) & {'ctrl+f', 'ctrl+l', 'ctrl+v'}}
        now = {s for s, _t, ks in rows if set(ks) & {'up', 'down'}}
        check('C8 rows are grouped by page: more than one titled section, the Listen keys under a Listen section '
              'and the volume keys under a Now section',
              len([s for s in sections if s]) >= 2 and listen and all('listen' in s.casefold() for s in listen)
              and now and all('now' in s.casefold() for s in now), (sections, listen, now))
        # Adversarial: a table appended to on every open, or per window, shows duplicates or drifts.
        if dialog is not None:
            dialog.force_close() if hasattr(dialog, 'force_close') else dialog.close()
            pump(.4)
        again = open_shortcuts(window)
        rows_again, _ = dialog_rows(again, app) if isinstance(again, Adw.ShortcutsDialog) else ([], 'none')
        if again is not None:
            again.force_close() if hasattr(again, 'force_close') else again.close()
            pump(.3)
        second = env.next_window.NextWindow(env.app)
        second.set_default_size(900, 700)
        second.present()
        pump(1.2)
        other = open_shortcuts(second)
        rows_other, _ = dialog_rows(other, app) if isinstance(other, Adw.ShortcutsDialog) else ([], 'none')
        key = lambda rs: sorted((s, t, tuple(sorted(k))) for s, t, k in rs)
        duplicates = [r for r in set(key(rows)) if key(rows).count(r) > 1]
        check('C9 the dialog is the same when opened again and from a second window, with no duplicate rows',
              bool(rows) and key(rows) == key(rows_again) == key(rows_other) and not duplicates,
              (len(rows), len(rows_again), len(rows_other), duplicates))
        # One table: source text, because "a key added later appears without a second edit" is a property of where
        # the keys are written, which no run of the window can observe. Each Ctrl shortcut may be spelled (as an
        # accelerator or as a display label) in one statement only: the table entry. On main the dialog's text table
        # and the accelerator setup spell each key twice.
        source = Path(env.source)
        sites = {}
        for path in sorted(source.glob('*.py')):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for value, line in string_constants(tree):
                trigger = literal_trigger(value)
                if trigger:
                    sites.setdefault(trigger, set()).add('%s:%d' % (path.name, line))
        ctrl_keys = sorted({t for ts in actions.values() for t in ts} | set(controllers))
        ctrl_keys = [t for t in ctrl_keys if t.startswith('ctrl') and t not in TOOLKIT_KEYS]
        doubled = {t: sorted(sites.get(t, ())) for t in ctrl_keys if len(sites.get(t, ())) > 1}
        check('C10 one shortcut table: each installed Ctrl shortcut is written in one statement of the source (source text)',
              bool(ctrl_keys) and not doubled, doubled)
        comments = []
        for path in sorted(source.glob('*.py')):
            try:
                for token in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
                    if token.type == tokenize.COMMENT:
                        comments.append(token.string)
            except (tokenize.TokenError, SyntaxError):
                continue
        reserved = [c for c in comments if re.search(r'O11-CONTROL|\bMix\b', c) and 'Shift' in c]
        # [ORIGINAL, superseded by C11-R1] needed one comment line naming both Mix/O11-CONTROL and Shift; a reservation
        # written as a comment block over several lines (the natural form) failed it.
        superseded('C11 [ORIGINAL, superseded by C11-R1] the table reserves the O11-CONTROL Mix keys (M, A, Shift+Up/Down) with a '
                   'comment (source text)', bool(reserved), reserved[:3])
        blocks = []
        for path in sorted(source.glob('*.py')):
            last, block = None, []
            try:
                for token in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
                    if token.type == tokenize.COMMENT:
                        if last is not None and token.start[0] != last + 1 and block:
                            blocks.append(' '.join(block))
                            block = []
                        block.append(token.string)
                        last = token.start[0]
            except (tokenize.TokenError, SyntaxError):
                pass
            if block:
                blocks.append(' '.join(block))
        reserved = [b for b in blocks if re.search(r'O11-CONTROL|\bMix\b', b) and 'Shift' in b]
        check('C11-R1 a comment block reserves the O11-CONTROL Mix keys (M, A, Shift+Up/Down) (source text)',
              bool(reserved), [b[:200] for b in reserved[:2]])

    # =========================================================================== D: scene choice (criterion 4)
    def enter_room(window, avoid=()):
        window.show_rooms()
        pump(.6)
        entries = [e for e in window.rooms.card_for.values() if e.get('id') not in avoid]
        entry = next((e for e in entries if e.get('source_id')), entries[0])
        window.rooms.open_preview(entry)
        pump(.6)
        window.rooms.preview_widgets['enter'].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None and env.controller.active is not None, 8)
        pump(1.0)
        return entry

    def current_scene():
        return ((env.controller.active or {}).get('picture') or {}).get('preset_id')

    def scene_title(preset):
        return preset['name'].split(' · ')[0].strip()

    def picker_roots(window):
        roots = []
        dialog = window.get_visible_dialog()
        if dialog is not None:
            roots.append(dialog)
        for w in widgets(window):
            if isinstance(w, Gtk.Popover) and w.get_mapped():
                roots.append(w)
        roots += [w for w in env.app.get_windows() if w is not window and w.get_mapped()]
        roots.append(window.sheet)
        return roots

    def tiles(window, presets):
        """scene id -> the activatable tile that names it, in the picker that is open."""
        found = {}
        for root in picker_roots(window):
            for preset in presets:
                if preset['id'] in found:
                    continue
                title = scene_title(preset).casefold()
                for w in widgets(root):
                    if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text().casefold().startswith(title):
                        node = w
                        while node is not None and node is not root:
                            # Harness revision H1 (acceptance author, phase-2 rerun, 2026-11-23; no criterion changed): the
                            # tile is the nearest focusable ancestor. [ORIGINAL, superseded by H1] also stopped at any
                            # Gtk.Button, so a non-focusable button inside a focusable FlowBoxChild tile was taken for the
                            # tile and grab_focus() on it went nowhere.
                            if node.get_focusable() or isinstance(node, Gtk.FlowBoxChild):
                                break
                            node = node.get_parent()
                        if node is not None and node is not root and node is not getattr(window.mix.rows.get('picture'), 'change', None):
                            found[preset['id']] = node
                            break
            if len(found) >= 2:
                return found, root
            found = {}
        return {}, None

    def thumbnail(tile):
        for w in widgets(tile):
            paintable = paintable_of(w)
            if paintable is not None and w.get_mapped():
                return w, paintable
        return None, None

    MARKER = re.compile(r'current|showing|in use|playing|selected|chosen|\bnow\b', re.I)

    def signature(tile, preset):
        title = scene_title(preset).casefold()
        sig = set()
        for w in widgets(tile):
            if not w.get_mapped():
                continue
            sig |= {'css:' + c for c in w.get_css_classes()}
            flags = w.get_state_flags()
            for flag, word in ((Gtk.StateFlags.CHECKED, 'checked'), (Gtk.StateFlags.SELECTED, 'selected')):
                if flags & flag:
                    sig.add('state:' + word)
            if isinstance(w, (Gtk.ToggleButton, Gtk.CheckButton)) and w.get_active():
                sig.add('active')
            if isinstance(w, Gtk.Image) and w.get_icon_name():
                sig.add('icon:' + w.get_icon_name())
            for value in text_of(w):
                if MARKER.search(value) and title not in value.casefold():
                    sig.add('text:' + value)
        return frozenset(sig)

    def unavailable_marked(tile, preset, available_tiles):
        if not tile.get_sensitive() or any(not w.get_sensitive() for w in widgets(tile) if w.get_mapped()):
            return True
        if tile.get_opacity() < 1 or any(c in ('dim-label', 'dimmed', 'unavailable', 'insensitive', 'disabled')
                                         for w in widgets(tile) for c in w.get_css_classes()):
            return True
        words = re.compile(r'unavailable|not available|not downloaded|missing|offline|can.t|cannot', re.I)
        mine = {t for t in text_of(tile) if words.search(t.replace(preset['name'], ''))}
        theirs = {t for other in available_tiles for t in text_of(other)}
        return bool(mine - theirs)

    def open_scene_picker(window):
        window.open_mix()
        pump(.8)
        row = window.mix.rows.get('picture')
        change = getattr(row, 'change', None)
        if isinstance(change, Gtk.MenuButton):
            change.popup()
        elif change is not None:
            change.emit('clicked')
        pump(1.5)          # thumbnails decode off the GTK thread
        return change

    def close_picker(window, root):
        if isinstance(root, Adw.Dialog):
            root.force_close() if hasattr(root, 'force_close') else root.close()
        elif isinstance(root, Gtk.Popover):
            root.popdown()
        elif isinstance(root, Gtk.Window) and root is not window:
            root.close()
        pump(.5)

    def playing_loops():
        return {path for path, loop in getattr(next_widgets, '_LOOPS', {}).items()
                if getattr(loop, 'media', None) is not None and loop.media.get_playing()}

    @section('D')
    def scene_grid():
        leave()
        window = launch(1200, 800, page='rooms')
        pump(.8)
        enter_room(window, avoid=(UNAVAILABLE.get('id'),))
        current = current_scene()
        presets = [p for p in window.presets if p.get('kind') == 'local']
        offered = [p for p in presets if p.get('poster_path')]
        check('D0 precondition: a room with its own scene is active, and one room scene is unavailable',
              bool(current) and bool(UNAVAILABLE.get('id')) and current != UNAVAILABLE.get('id'), (current, UNAVAILABLE))
        before_loops, loop_keys = playing_loops(), set(getattr(next_widgets, '_LOOPS', {}))
        before_state = json.dumps({k: (env.controller.active or {}).get(k) for k in ('music', 'atmosphere', 'picture')},
                                  sort_keys=True, default=str)
        mark, calls = len(env.log), len(env.backend.calls)
        open_scene_picker(window)
        found, root = tiles(window, offered)
        env.shot(window, 'o11s-d-scene-picker')
        check('D1 Scene › Change… shows a tile for every room scene, the unavailable one included',
              bool(found) and all(p['id'] in found for p in offered),
              (sorted(found), [p['id'] for p in offered if p['id'] not in found], type(root).__name__ if root else None))
        thumbs, wrong, big, loops = {}, [], [], []
        for preset in offered:
            tile = found.get(preset['id'])
            if tile is None:
                continue
            widget, paintable = thumbnail(tile)
            thumbs[preset['id']] = (widget, paintable)
            if paintable is None:
                wrong.append((preset['id'], 'no picture'))
                continue
            if isinstance(paintable, Gtk.MediaStream):
                loops.append(preset['id'])
                continue
            if paintable.get_intrinsic_width() > poster_cards.CARD_SIZE[0]:
                big.append((preset['id'], paintable.get_intrinsic_width()))
            fp = fingerprint_paintable(paintable)
            poster = poster_cards.card_file(preset['poster_path']) or preset['poster_path']
            ref = fingerprint_file(str(poster))
            if fp is None or ref is None or difference(fp, ref) >= FINGERPRINT_LIMIT:
                wrong.append((preset['id'], round(difference(fp, ref), 1) if fp and ref else None))
        check('D2 each tile shows its own scene\'s card poster as a thumbnail', bool(thumbs) and not wrong, wrong)
        check('D3 thumbnails are card-sized (at most %d px wide), not full posters' % poster_cards.CARD_SIZE[0],
              any(p is not None for _w, p in thumbs.values()) and not big, big)
        placed = sorted({round(bounds(w, window)[0]) for w, _p in thumbs.values() if w is not None and bounds(w, window)})
        rows_y = sorted({round(bounds(w, window)[1]) for w, _p in thumbs.values() if w is not None and bounds(w, window)})
        check('D4 the thumbnails form a grid (more than one per row, more than one row)', len(placed) >= 2 and len(rows_y) >= 2,
              (placed, rows_y))
        # Current scene marked, and only it.
        available_tiles = {pid: t for pid, t in found.items() if pid != UNAVAILABLE.get('id')}
        window.set_focus(None)
        pump(.2)
        sigs = {pid: signature(t, next(p for p in offered if p['id'] == pid)) for pid, t in available_tiles.items()}
        mine = sigs.get(current)
        check('D5 the current scene\'s tile is marked: it differs from every other available tile (not by focus)',
              mine is not None and all(sig != mine for pid, sig in sigs.items() if pid != current),
              (current, sorted(mine or ()), {pid: sorted(s) for pid, s in list(sigs.items())[:3]}))
        # Unavailable scene visibly so.
        gone = found.get(UNAVAILABLE.get('id'))
        gone_preset = next((p for p in offered if p['id'] == UNAVAILABLE.get('id')), None)
        check('D6 the unavailable scene\'s tile is visibly unavailable (insensitive, dimmed or saying so)',
              gone is not None and unavailable_marked(gone, gone_preset, list(available_tiles.values())),
              (UNAVAILABLE.get('id'), text_of(gone) if gone else None))
        # Keyboard: focus moves between tiles with the arrows and Tab.
        order = sorted(((bounds(t, window) or (0, 0))[1], (bounds(t, window) or (0, 0))[0], pid)
                       for pid, t in available_tiles.items())
        first = available_tiles[order[0][2]] if order else None
        moved = None
        if first is not None:
            first.grab_focus()
            pump(.2)
            top = first.get_root() or window
            start_focus = top.get_focus()
            top.child_focus(Gtk.DirectionType.RIGHT)
            pump(.3)
            after = top.get_focus()
            moved = next((pid for pid, t in found.items() if after is not None and inside(after, t)), None)
            # [ORIGINAL, superseded by D7-R1] moved focus with child_focus() on the window, which skips a container's
            # own arrow bindings; on a grid of buttons in a popover it did not move where a real Right press can.
            superseded('D7 [ORIGINAL, superseded by D7-R1] keyboard: a tile takes focus and Right moves it to another scene tile',
                  start_focus is not None and inside(start_focus, first) and moved is not None and moved != order[0][2],
                  (describe(start_focus), describe(after)))
            first.grab_focus()
            pump(.2)
            start_focus = top.get_focus()
            handled = press(top, Gdk.KEY_Right)
            pump(.3)
            after = top.get_focus()
            moved = next((pid for pid, t in found.items() if after is not None and inside(after, t)), None)
            check('D7-R1 keyboard: a tile takes focus and a Right press (GTK dispatch) moves it to another scene tile',
                  start_focus is not None and inside(start_focus, first) and moved is not None and moved != order[0][2],
                  (describe(start_focus), describe(after), 'handled' if handled else 'not handled'))
            # D7b: the minimum of "keyboard navigable", Tab, separately from the arrows (D7-R1).
            first.grab_focus()
            pump(.2)
            press(top, Gdk.KEY_Tab)
            pump(.3)
            after = top.get_focus()
            moved = next((pid for pid, t in found.items() if after is not None and inside(after, t)), None)
            check('D7b keyboard: a Tab press (GTK dispatch) moves focus from one scene tile to another',
                  moved is not None and moved != order[0][2], describe(after))
            down_from = available_tiles[order[0][2]]
            down_from.grab_focus()
            pump(.2)
            press(top, Gdk.KEY_Down)
            pump(.3)
            after = top.get_focus()
            moved = next((pid for pid, t in found.items() if after is not None and inside(after, t)), None)
            check('D7c keyboard: a Down press (GTK dispatch) moves focus to a scene tile in the next row',
                  moved is not None and moved != order[0][2] and (bounds(found[moved], window) or (0, 0))[1]
                  > (bounds(down_from, window) or (0, 0))[1], describe(after))
        else:
            superseded('D7 [ORIGINAL, superseded by D7-R1] keyboard: a tile takes focus and Right moves it to another scene tile', False, 'no tiles')
            check('D7b keyboard: a Tab press (GTK dispatch) moves focus from one scene tile to another', False, 'no tiles')
            check('D7c keyboard: a Down press (GTK dispatch) moves focus to a scene tile in the next row', False, 'no tiles')
            check('D7-R1 keyboard: a tile takes focus and a Right press (GTK dispatch) moves it to another scene tile', False, 'no tiles')
        # Browsing is inert: opening the grid, moving focus over every tile, nothing is sent or played.
        for tile in found.values():
            tile.grab_focus()
            pump(.15)
        pump(1.0)
        new_loops = playing_loops() - before_loops
        streams = [pid for pid, (_w, p) in thumbs.items() if isinstance(p, Gtk.MediaStream)]
        check('D8 browsing the grid is inert: no command, no backend call, no new loop playing, no video in a tile',
              not commands(mark) and len(env.backend.calls) == calls and not new_loops and not streams
              and set(getattr(next_widgets, '_LOOPS', {})) <= loop_keys,
              (commands(mark), env.backend.calls[calls:], sorted(new_loops), streams))
        # D9-R1: the unavailable tile cannot be chosen. Insensitive means no click or keyboard focus reaches it (a user
        # cannot activate it); a sensitive one must send nothing when activated.
        mark = len(env.log)
        blocked = gone is not None and not gone.is_sensitive()
        if gone is not None and not blocked:
            gone.activate()
            pump(.8)
        check('D9-R1 the unavailable scene cannot be chosen (insensitive, or activating it sends nothing)',
              gone is not None and (blocked or (not commands(mark) and current_scene() == current)),
              ('insensitive' if blocked else 'sensitive', commands(mark), current_scene()))
        # [ORIGINAL, superseded by D9-R1] emitted activate() on the tile. GTK's activate signal reaches even an insensitive
        # button, which no user can click or focus, so it tested a path users do not have. It still runs (it may change
        # the scene; D10 below picks its target and compares afterwards).
        mark = len(env.log)
        if gone is not None:
            gone.activate()
            pump(.8)
        superseded('D9 [ORIGINAL, superseded by D9-R1] activating the unavailable scene changes nothing', gone is not None and not commands(mark)
              and current_scene() == current, (commands(mark), current_scene()))
        # Choosing another scene with Enter changes only the scene.
        target = next((pid for _y, _x, pid in order if pid != current), None)
        tile = found.get(target)
        if tile is not None and not tile.get_mapped():
            open_scene_picker(window)
            found, root = tiles(window, offered)
            tile = found.get(target)
        mark, calls = len(env.log), len(env.backend.calls)
        if tile is not None:
            tile.grab_focus()
            pump(.2)
            focus = (tile.get_root() or window).get_focus()
            (focus if focus is not None and inside(focus, tile) else tile).activate()
        settle(1.5)
        after_state = {k: (env.controller.active or {}).get(k) for k in ('music', 'atmosphere')}
        before = json.loads(before_state)
        sent = commands(mark)
        check('D10 Enter on another scene\'s tile changes the scene to it', tile is not None and current_scene() == target,
              (target, current_scene(), sent))
        check('D11 ... and only the scene: music and ambience are untouched and nothing starts',
              tile is not None and json.dumps(after_state, sort_keys=True, default=str)
              == json.dumps({k: before[k] for k in ('music', 'atmosphere')}, sort_keys=True, default=str)
              and not starts(mark) and all(a == 'layer' and k.get('layer') == 'picture' for a, k in sent) and sent,
              (sent, env.backend.calls[calls:]))
        # The mark follows the choice.
        for r in picker_roots(window):
            if r is not window.sheet:
                close_picker(window, r)
        mark = len(env.log)
        open_scene_picker(window)
        found, root = tiles(window, offered)
        window.set_focus(None)
        pump(.2)
        available_tiles = {pid: t for pid, t in found.items() if pid != UNAVAILABLE.get('id')}
        sigs = {pid: signature(t, next(p for p in offered if p['id'] == pid)) for pid, t in available_tiles.items()}
        mine = sigs.get(target)
        check('D12 reopened, the mark is on the newly chosen scene', mine is not None
              and all(sig != mine for pid, sig in sigs.items() if pid != target), (target, sorted(mine or ())))
        # Closing without choosing changes nothing.
        close_picker(window, root)
        check('D13 closing the grid without choosing sends nothing', not commands(mark) and current_scene() == target,
              commands(mark))

    # =========================================================================== E: no-room Now (criterion 5)
    def now_art(window):
        """Mapped pictures on Now (Mix closed) and what each shows: a loop, or a texture fingerprint."""
        now = window.pages.get_child_by_name('now')
        shown = []
        for w in widgets(now):
            if not w.get_mapped() or w.get_width() < 64 or w.get_height() < 64:
                continue
            node, visible = w, True
            while node is not None:
                if node.get_opacity() == 0:
                    visible = False
                node = node.get_parent()
            paintable = paintable_of(w)
            if paintable is None or not visible:
                continue
            shown.append((w, paintable))
        return shown

    def stale_art(window, arts):
        found = []
        for w, paintable in now_art(window):
            if isinstance(paintable, Gtk.MediaStream):
                found.append((type(w).__name__, 'a moving loop'))
                continue
            fp = fingerprint_paintable(paintable)
            if fp is None:
                continue
            score, path = matches_art(fp, arts)
            if score < FINGERPRINT_LIMIT:
                found.append((type(w).__name__, Path(path).name, round(score, 1)))
        return found

    def watch_without_room(name='Rain on a tin roof, 3 hours'):
        """The Watching state with no room, as the controller holds it while the Watch window plays. No player runs:
        the backend's video_state answers "Playing" for the fixture, so the controller's refresh keeps it."""
        env.backend.video_state = lambda: {'status': 'Playing'} if env.controller.video else None
        with env.controller.lock:
            env.controller.video = {'status': 'Playing', 'name': name, 'url': 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
                                    'started_at': time.time(), 'room_paused': False}
        env.controller._changed()
        settle(.8)
        wait(lambda: all((w.snapshot or {}).get('video') for w in env.app.get_windows() if hasattr(w, 'snapshot')), 4)

    @section('E')
    def no_room_now():
        leave()
        window = launch(1200, 800, page='rooms')
        enter_room(window, avoid=(UNAVAILABLE.get('id'),))
        window.show_now()
        pump(1.5)
        arts = room_art(window)
        in_room = [s for s in stale_art(window, arts)]
        env.shot(window, 'o11s-e-now-in-room')
        # Control: the fingerprint method sees the room's art while a room plays (this must pass on main too).
        check('E0 control: with a room, Now shows that room\'s scene art (the detector sees it)',
              bool(arts) and bool(in_room) or any(isinstance(p, Gtk.MediaStream) for _w, p in now_art(window)),
              (len(arts), in_room))
        # The room ends while Now shows; then a video plays with no room (the YouTube Watching chip case).
        env.service.execute('leave', {})
        settle(1.0)
        wait(lambda: env.controller.active is None and not env.controller.transition, 8)   # Leave fades out first
        settle(.6)
        watch_without_room()
        window.show_now()
        pump(1.5)
        env.shot(window, 'o11s-e-now-no-room-watching')
        page = window.current_page()
        stale = stale_art(window, arts)
        texts = text_of(window.pages.get_child_by_name('now'))
        check('E1 Now with no room and a video playing shows (precondition: Now is the page, Stop watching is there)',
              page == 'now' and env.controller.active is None and 'Stop watching' in texts,
              (page, texts[:20], 'window video: %r' % (window.snapshot or {}).get('video'),
               'controller video: %r' % env.controller.video, 'inspect video: %r' % env.service.execute('inspect', {}).get('video')))
        check('E2 ... a neutral state: no room\'s scene art and no loop on Now (left the room while on Now)',
              page == 'now' and not stale, stale)
        # Fresh window, last room remembered, video with no room.
        window = launch(1200, 800, page='rooms')
        wait(lambda: (window.snapshot or {}).get('video'), 4)
        window.show_now()
        pump(1.5)
        env.shot(window, 'o11s-e-now-no-room-fresh-window')
        stale = stale_art(window, arts)
        check('E3 a new window over the same no-room video: Now is neutral too', window.current_page() == 'now' and not stale,
              (window.current_page(), stale))
        # Adversarial: neutral must not leak into rooms; entering again shows that room's art again.
        with env.controller.lock:
            env.controller.video = None
        env.controller._changed()
        settle(.6)
        enter_room(window, avoid=(UNAVAILABLE.get('id'),))
        window.show_now()
        pump(1.5)
        back = stale_art(window, arts)
        check('E4 entering a room afterwards shows its scene art again', bool(back)
              or any(isinstance(p, Gtk.MediaStream) for _w, p in now_art(window)), back)
        leave()

    # =========================================================================== G: Listen at 560-700 px (criterion 6)
    @section('G')
    def listen_leftovers():
        leave()
        for name in ('Morning lift', 'Morning Radio'):
            if not any(s['name'] == name for s in env.controller.stations()):
                env.service.execute('station.save', {'station': dict(o6_env.RADIO_STATION, name=name)})
        settle(.6)
        for width in (560, 600, 640, 700):
            window = launch(width, 800, page='listen')
            window.listen.show_index() if hasattr(window.listen, 'show_index') else None
            pump(1.2)
            env.shot(window, 'o11s-g-listen-%d' % width)
            page = window.listen
            chip_cut = []
            for group in (page.scopes, getattr(page, 'more_scopes', None)):
                for w in widgets(group):
                    if isinstance(w, Gtk.Label) and ellipsized(w):
                        chip_cut.append(w.get_text())
            check('G1 at %d px no Listen scope chip is ellipsized' % width, not chip_cut and page.scopes.get_mapped(),
                  (chip_cut, page.scopes.get_mapped()))
            cards = [w for w in widgets(window) if w.has_css_class('station-card') and w.get_mapped()]
            cut, fragments = [], []
            for card in cards:
                for w in widgets(card):
                    if not isinstance(w, Gtk.Label) or not w.get_mapped():
                        continue
                    detail = w.has_css_class('row-detail') or w.has_css_class('dim-label')
                    if not ellipsized(w):
                        continue
                    if not detail:
                        cut.append(w.get_text())
                        continue
                    # A detail line may end in "…", but never cut inside its first word ("Radi…").
                    words = w.get_text().split()
                    probe_layout = w.create_pango_layout((words[0] if words else '') + '…')
                    need = probe_layout.get_pixel_size()[0]
                    if w.get_width() < need:
                        fragments.append(w.get_text())
            check('G2 at %d px the station cards show whole names and Tune in (nothing ellipsized)' % width,
                  bool(cards) and not cut, (len(cards), cut))
            check('G3 at %d px a station card\'s detail line is never cut inside its first word' % width,
                  bool(cards) and not fragments, fragments)

    # =========================================================================== K: wave-2 launch focus unchanged
    @section('K')
    def returning_user():
        leave()
        window = launch(1200, 800, page='rooms')
        enter_room(window, avoid=(UNAVAILABLE.get('id'),))
        leave()
        mark = len(env.log)
        window = launch(1200, 800)
        pump(1.0)
        check('K16r returning user (a last room, nothing active): Rooms with the hero button focused, nothing started',
              window.current_page() == 'browse' and window.get_focus() is window.hero_button and not starts(mark),
              (window.current_page(), describe(window.get_focus()), commands(mark)))
        window = rooms_at(*WIDE)
        share, parts = content_share(window)
        env.shot(window, 'o11s-k-rooms-1440-returning')
        print('measure: returning 1440x900 Rooms content share %.3f %r' % (share, parts))
        check('B6 at 1440x900 for a returning user (hero shown) the Rooms content fills at least 45%% of the window '
              '(share %.3f)' % share, share >= WIDE_SHARE, (round(share, 3), parts))

    # =========================================================================== H: CHANGELOG (criterion 7)
    @section('H')
    def changelog():
        repo = Path(env.args.repo) if env.args.repo else Path(env.source).parent
        text = (repo / 'CHANGELOG.md').read_text()
        head = text.split('## [2.5.0]', 1)[0]
        body = head.split('## [Unreleased]', 1)[-1] if '## [Unreleased]' in head else head
        entries = [line for line in body.splitlines() if line.strip().startswith('- ')]
        check('H1 CHANGELOG records the change above 2.5.0 and mentions the keyboard shortcuts and the scene choice',
              bool(entries) and re.search(r'shortcut', body, re.I) and re.search(r'scene', body, re.I), entries[:6])

    out = Path(env.args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'acceptance-o11s.json').write_text(json.dumps({'results': checks}, indent=2))
    failed = [c for c in checks if not c['pass']]
    print('%d passed, %d failed' % (len(checks) - len(failed), len(failed)))
    os._exit(1 if failed else 0)
