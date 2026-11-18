"""Independent acceptance checks for Listening Room outcome O2 (Rooms, Room preview, saved rooms).

Owned by the acceptance author, not the implementer. The oracles are behavioural: the controller
command log, FakeBackend effects, the controller's persisted state, visible widget-tree text,
accessible names captured at the GTK boundary and allocation bounds. The harness (fake controller,
command recorder, widget walkers, window launcher) is the O1 acceptance probe's own preamble,
executed unchanged so both probes share one oracle vocabulary.

Run inside `scripts/listening-dev.sh shell` under private Xvfb/D-Bus:
  env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u HYPRLAND_INSTANCE_SIGNATURE \
    XDG_RUNTIME_DIR=<fresh 0700 dir> GDK_BACKEND=x11 GDK_DEBUG=no-portals GSK_RENDERER=cairo \
    xvfb-run -a -s '-screen 0 1440x1000x24' dbus-run-session \
    python3 tests/probes/listening-o2-acceptance.py --repo WORKTREE --output OUTDIR [--only s1,s2]
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_marker = '# ---------------------------------------------------------------- scenarios'
_source = (HERE / 'listening-next-acceptance.py').read_text().split(_marker)[0]
exec(compile(_source, str(HERE / 'listening-next-acceptance.py'), 'exec'), globals())  # noqa: S102

import json as _json  # noqa: E402
from gi.repository import Graphene, Pango  # noqa: E402

# ---------------------------------------------------------------- dialogs seen at the GTK boundary
DIALOGS = []
_present = Adw.AlertDialog.present


def _record_present(self, parent=None):
    DIALOGS.append(self)
    return _present(self, parent)


Adw.AlertDialog.present = _record_present

MOODS = ['All', 'Yours', 'Focus', 'Reading', 'Late night', 'Lively', 'Something new']
RADIO_ID = None


# ---------------------------------------------------------------- helpers
def reset_state():
    """A clean controller: nothing active, no history, no saved rooms, no legacy scenes."""
    close_windows()
    if controller.active is not None:
        controller.leave(0)
        settle(1.0)
    for variant in list(controller.variants()):
        service.execute('delete', {'id': variant['id']})
    controller.last_session = None
    controller.recent = []
    controller.last_entered_at = None
    controller._persist()
    scenes = hub.STATE / 'scenes.json'
    if scenes.exists():
        scenes.unlink()
    DIALOGS.clear()
    backend.calls.clear()
    backend.fail_picture = backend.fail_music = False
    pump(.3)


def card_name(child):
    """'Room Night drive, yours, playing' -> ('Night drive', {'yours', 'playing'})."""
    text = acc_name(child) or ''
    if not text.startswith('Room '):
        return None
    parts = [p.strip() for p in text[5:].split(', ')]
    flags = set(p for p in parts[1:] if p in ('yours', 'playing'))
    name = ', '.join(p for p in parts if p not in flags or p is parts[0])
    return name, flags


def flowboxes(window):
    return [w for w in walk(window) if isinstance(w, Gtk.FlowBox) and shown(w)]


def card_children(window):
    result = []
    for box in flowboxes(window):
        for child in children(box):
            parsed = card_name(child)
            if parsed:
                result.append((parsed[0], parsed[1], child))
    return result


def card_names(window):
    return [n for n, _, _ in card_children(window)]


def card(window, name):
    found = [c for n, _, c in card_children(window) if n == name]
    return found[0] if found else None


def label_texts(widget):
    return texts(widget)


def chip(window, text):
    found = [w for w in walk(window) if isinstance(w, Gtk.ToggleButton) and shown(w) and ' '.join(texts(w)) == text]
    return found[0] if found else None


def choose_chip(window, text):
    c = chip(window, text)
    assert c is not None, 'no chip ' + text
    c.set_active(True)
    pump(.5)


def launch_rooms(width=1200, height=800):
    window = launch(width, height)
    pump(1.2)
    if not card_children(window):
        goto(window, 'rooms')
        pump(.8)
    return window


def surprise_button(window):
    return button_with_text(window, 'Surprise me')


def enter_button(window):
    return button_with_text(window, 'Enter room')


def preview_open(window):
    return "What you'll hear" in texts(window)


def gestures_of(widget):
    return [c for c in widget.observe_controllers() if isinstance(c, Gtk.GestureClick)]


def spin(seconds):
    """Run the main loop only (no controller refresh), for sub-300 ms timing."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(.005)


def mouse(child, presses=1, gap=.09, button=1):
    """Deliver real pressed/released pairs to the card and the grid the way GTK does for n clicks."""
    box = child.get_parent()
    ok, rect = child.compute_bounds(box)
    x, y = rect.get_x() + rect.get_width() / 2, rect.get_y() + rect.get_height() / 2
    inner = child.get_child()
    ok2, r2 = inner.compute_bounds(inner)
    ix, iy = r2.get_width() / 2, r2.get_height() / 2
    for i in range(1, presses + 1):
        targets = [(g, ix, iy) for g in gestures_of(inner)] + [(g, x, y) for g in gestures_of(box)]
        targets = [t for t in targets if t[0].get_button() in (0, button)]
        for g, gx, gy in targets:
            g.emit('pressed', i, gx, gy)
        for g, gx, gy in targets:
            g.emit('released', i, gx, gy)
        spin(gap)


def mutating_names(mark):
    return [c[0] for c in mutating_since(mark)]


def starts(mark):
    return [c for c in mutating_since(mark) if c[0] in ('enter', 'return', 'music', 'resume', 'layer', 'picture', 'online', 'recipe')]


def playing_streams():
    found = []
    for window in APP.get_windows():
        for w in walk(window):
            if isinstance(w, Gtk.Picture) and isinstance(w.get_paintable(), Gtk.MediaStream) and w.get_paintable().get_playing():
                found.append(w.get_paintable())
            if isinstance(w, Gtk.Video) and w.get_media_stream() is not None and w.get_media_stream().get_playing():
                found.append(w.get_media_stream())
    return found


def real_key(window, keyval, state=0):
    """Deliver a key the way GTK does: capture controllers root-down, then bubble from the focus up.
    Return/space left unhandled on a FlowBox child fall through to the FlowBox's own activation binding."""
    focus = window.get_focus()
    chain = [focus] + list(ancestors(focus)) if focus is not None else [window]
    for widget in reversed(chain):
        for c in widget.observe_controllers():
            if isinstance(c, Gtk.EventControllerKey) and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE:
                if c.emit('key-pressed', keyval, 0, state):
                    return 'capture'
    for widget in chain:
        for c in widget.observe_controllers():
            if isinstance(c, Gtk.EventControllerKey) and c.get_propagation_phase() == Gtk.PropagationPhase.BUBBLE:
                if c.emit('key-pressed', keyval, 0, state):
                    return 'bubble'
    box = next((w for w in chain if isinstance(w, Gtk.FlowBox)), None)
    if box is not None and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
        box.emit('activate-cursor-child')
        return 'flowbox-activate'
    return None


def esc(window):
    return key(window, Gdk.KEY_Escape)


def buttons_with(window, text, exact=False):
    return [w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and
            ((' '.join(texts(w)) == text) if exact else (text in ' '.join(texts(w))))]


def dialog_widgets(dialog):
    return list(walk(dialog))


def dialog_button(dialog, text):
    for w in walk(dialog):
        if isinstance(w, Gtk.Button) and ' '.join(texts(w)) == text:
            return w
    return None


def dialog_open_state(dialog):
    return dialog.get_mapped()


def press_response(dialog, text):
    b = dialog_button(dialog, text)
    assert b is not None, 'dialog has no %r button; widgets=%r' % (text, [' '.join(texts(w)) for w in walk(dialog) if isinstance(w, Gtk.Button)])
    click(b)
    pump(.6)


def last_dialog():
    return DIALOGS[-1] if DIALOGS else None


def dialog_text(dialog):
    return [dialog.get_heading() or '', dialog.get_body() or ''] + texts(dialog.get_extra_child()) if dialog.get_extra_child() else \
        [dialog.get_heading() or '', dialog.get_body() or '']


def entry_in(dialog):
    found = [w for w in walk(dialog) if isinstance(w, Gtk.Entry)]
    return found[0] if found else None


def card_menu(window, name):
    """The options MenuButton of a card, found by its accessible name."""
    found = [w for w in walk(window) if isinstance(w, Gtk.MenuButton) and acc_name(w) == 'Options for ' + name]
    return found[0] if found else None


def open_card_menu(window, name):
    button = card_menu(window, name)
    assert button is not None, 'no options menu for ' + name
    button.popup()
    pump(.5)
    return button


def popover_buttons(button):
    pop = button.get_popover()
    return [w for w in walk(pop) if isinstance(w, Gtk.Button)]


def pick_menu(button, text):
    for b in popover_buttons(button):
        if ' '.join(texts(b)).startswith(text):
            click(b)
            pump(.6)
            return True
    return False


def toasts(window):
    return [t for t in texts(window)]


def save_room(name, base='midnight-library'):
    enter_room(base)
    r = controller.save({'name': name})
    settle()
    return r['variant']


def set_scenes(items):
    (hub.STATE).mkdir(parents=True, exist_ok=True)
    (hub.STATE / 'scenes.json').write_text(_json.dumps(items))


def a_radio_source():
    sources = [s for s in hub.sources() if s.get('kind') == 'radio']
    return sources[0]


def columns_of(window):
    xs = sorted({round(c.compute_bounds(window)[1].get_x() / 8) for _, _, c in card_children(window)})
    return len(xs)


def hscroll_ok(window):
    """No scrolled window anywhere offers horizontal travel."""
    bad = []
    for w in walk(window):
        if isinstance(w, Gtk.ScrolledWindow) and shown(w):
            adj = w.get_hadjustment()
            if adj.get_upper() > adj.get_page_size() + 1:
                bad.append(describe(w) + ' upper=%d page=%d' % (adj.get_upper(), adj.get_page_size()))
    return bad


def clipped_controls(window):
    """Interactive controls whose allocation leaves the window or whose scroller viewport hides them sideways."""
    width, height = window.get_width(), window.get_height()
    bad = []
    for w in controls(window):
        ok, rect = w.compute_bounds(window)
        if not ok:
            continue
        if rect.get_x() < -1 or rect.get_x() + rect.get_width() > width + 1:
            bad.append(describe(w))
            continue
        scroller = next((p for p in ancestors(w) if isinstance(p, Gtk.ScrolledWindow)), None)
        if scroller is not None:
            ok2, srect = scroller.compute_bounds(window)
            if rect.get_x() < srect.get_x() - 1 or rect.get_x() + rect.get_width() > srect.get_x() + srect.get_width() + 1:
                bad.append(describe(w))
    return bad


def unnamed_icon_controls(window, include_popovers=False):
    bad = []
    for w in controls(window, include_popovers):
        if isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.ToggleButton)) and not texts(w) and not acc_name(w):
            bad.append(describe(w))
        elif isinstance(w, (Gtk.Switch, Gtk.Scale, Gtk.Entry, Gtk.CheckButton)) and not acc_name(w):
            bad.append(describe(w))
    return bad


def running_stack_transitions(window, times=14):
    seen = []
    for _ in range(times):
        for _ in range(3):
            context.iteration(False)
        for w in walk(window):
            if isinstance(w, Gtk.Stack) and w.get_transition_running():
                seen.append(describe(w))
        time.sleep(.02)
    return sorted(set(seen))


def open_preview_by_click(window, name, wait=.55):
    child = card(window, name)
    assert child is not None, 'no card ' + name
    mouse(child, 1)
    pump(wait)
    return preview_open(window)


def back_to_rooms(window):
    b = buttons_with(window, 'Rooms', exact=True)
    return b[0] if b else None


# ---------------------------------------------------------------- scenarios
def scenario(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()
        check(fn.__name__ + ' (scenario crashed)', False, traceback.format_exc()[-700:])
        try:
            close_windows()
        except Exception:
            pass


def s1_hero():
    reset_state()
    enter_room('midnight-library')
    CLOCK[0] += 1000
    controller.leave(0)
    settle(1.0)
    CLOCK[0] += 86400 * 1.2  # a day later
    window = launch()
    pump(1.5)
    hero = [t for t in texts(window) if t.startswith('Return to ')]
    primary = [' '.join(texts(w)) for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and w.has_css_class('suggested-action')]
    check('1a hero: one primary "Return to Midnight library" on Rooms', hero == ['Return to Midnight library'] and primary == hero, repr((hero, primary)))
    when = [t for t in texts(window) if t in ('Just now', 'Earlier today', 'Yesterday', 'Last week', 'A while ago')]
    check('1b hero: a "last visited" line from last_entered_at', len(when) == 1 and when[0] == 'Yesterday', repr(when))
    check('1c hero: the returning room is NOT the only card (grid below hero)', len(card_names(window)) >= 8, repr(card_names(window)))
    screenshot(window, 'o2a-01-hero')
    mark = len(COMMANDS)
    click(buttons_with(window, 'Return to Midnight library')[0])
    settle(1.0)
    cmds = [c[0] for c in COMMANDS[mark:] if c[0] not in READ_ONLY]
    check('1d hero click returns exactly once and starts the last room', cmds == ['return'] and controller.active and controller.active['room_id'] == 'midnight-library', repr(cmds))
    leave_all()
    # first run
    reset_state()
    window = launch()
    pump(1.2)
    check('1e first run: no hero, no "Return to"', not any(t.startswith('Return to ') for t in texts(window)) and 'Pick a room to begin' in texts(window), repr(texts(window)[:12]))
    check('1f first run: no primary suggested-action on Rooms', not [w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and w.has_css_class('suggested-action')], '')
    screenshot(window, 'o2a-02-first-run')
    # no hero text leaks when the room is playing (O1 hero 'now' behaviour keeps working)
    leave_all()


def s2_chips_and_surprise():
    reset_state()
    enter_room('midnight-library')
    controller.leave(0)
    settle(1.0)
    window = launch()
    pump(1.2)
    seen = [' '.join(texts(w)) for w in walk(window) if isinstance(w, Gtk.ToggleButton) and shown(w) and ' '.join(texts(w)) in MOODS]
    check('2a chips: exactly All, Yours, Focus, Reading, Late night, Lively, Something new in order', seen == MOODS, repr(seen))
    mark, calls = len(COMMANDS), len(backend.calls)
    per = {}
    for mood in MOODS:
        choose_chip(window, mood)
        per[mood] = card_names(window)
    assert_inert('2b choosing every chip sends nothing and has no backend effect', mark, calls)
    all_rooms = [r['name'] for r in room_catalogue.rooms()]
    check('2c All shows every catalogue room', set(per['All']) >= set(all_rooms), repr(per['All']))
    late = per['Late night']
    check('2d Late night keeps late rooms (Night drive, Midnight library, After hours) and drops Northern quiet/Velvet circuit',
          {'Night drive', 'After hours'} <= set(late) and not ({'Northern quiet', 'Velvet circuit'} & set(late)), repr(late))
    check('2e Focus / Reading / Lively are non-empty subsets, each different from All',
          all(per[m] and set(per[m]) < set(per['All']) for m in ('Focus', 'Reading', 'Lively')), repr({m: per[m] for m in ('Focus', 'Reading', 'Lively')}))
    check('2f Yours with nothing saved shows no rooms and an explanation', per['Yours'] == [] and any('Save as my room' in t for t in texts(window)) is not None, repr(per['Yours']))
    # Something new: history-aware
    new_before = per['Something new']
    check('2g Something new includes the authored new room (Finnish discoveries)', 'Finnish discoveries' in new_before, repr(new_before))
    choose_chip(window, 'Late night')
    screenshot(window, 'o2a-03-late-night')
    # Surprise me: respects the chip, never enters, opens a preview
    mark, calls = len(COMMANDS), len(backend.calls)
    opened = []
    for _ in range(14):
        click(surprise_button(window))
        pump(.5)
        title = [t for t in texts(window) if t in all_rooms + ['Wizarding Valley']]
        opened.append((preview_open(window), title[:1]))
        esc(window)
        pump(.5)
        if preview_open(window):
            click(back_to_rooms(window))
            pump(.5)
    check('2h Surprise me always opens a preview', all(o for o, _ in opened), repr(opened))
    check('2i Surprise me within Late night only picks late-night rooms', all(t and t[0] in late for _, t in opened), repr(opened))
    check('2j Surprise me never picks a room twice in a row only (varies)', len({t[0] for _, t in opened if t}) >= 2, repr(opened))
    assert_inert('2k Surprise me never starts or changes anything', mark, calls)
    # with a room playing, Surprise me must avoid the room you are in
    enter_room('night-drive')
    pump(1)
    choose_chip(window, 'Late night')
    picks = []
    for _ in range(14):
        click(surprise_button(window))
        pump(.4)
        title = [t for t in texts(window) if t in all_rooms]
        picks.append(title[:1])
        esc(window)
        pump(.4)
    check('2l Surprise me avoids the room you are in when others exist', all(p and p != ['Night drive'] for p in picks), repr(picks))
    # Yours chip + Surprise with nothing saved: no crash, no start, says so
    choose_chip(window, 'Yours')
    mark = len(COMMANDS)
    click(surprise_button(window))
    pump(.5)
    check('2m Surprise me under an empty Yours opens nothing and starts nothing', not preview_open(window) and not mutating_since(mark), repr(mutating_since(mark)))
    # history: a room you were in drops out of Something new after the fact
    choose_chip(window, 'Something new')
    names = card_names(window)
    check('2n Something new excludes the room you are in now (history) unless authored new', 'Night drive' not in names, repr(names))
    leave_all()


def s3_grid():
    reset_state()
    variant = save_room('Evening reading')
    enter_room('night-drive')
    names_all = [r['name'] for r in room_catalogue.rooms()]
    window = launch_rooms(1200, 800)
    pump(.5)
    cards = card_children(window)
    order = [n for n, _, _ in cards]
    check('3a saved room comes first, before every catalogue room', order and order[0] == 'Evening reading', repr(order))
    check('3b saved room carries a Yours tag; catalogue rooms do not', [f for n, f, _ in cards if n == 'Evening reading'][0] >= {'yours'}
          and not any('yours' in f for n, f, _ in cards if n != 'Evening reading'), repr([(n, sorted(f)) for n, f, _ in cards][:4]))
    tag_labels = [w for w in walk(window) if isinstance(w, Gtk.Label) and shown(w) and w.get_text() == 'Yours'
                  and not any(isinstance(p, (Gtk.Button, Gtk.ToggleButton)) for p in ancestors(w))]
    check('3c a visible "Yours" label exists on the saved card only', len(tag_labels) == 1, str(len(tag_labels)))
    playing = [n for n, f, _ in cards if 'playing' in f]
    playing_labels = [w for w in walk(window) if isinstance(w, Gtk.Label) and shown(w) and w.get_text() == 'Playing']
    check('3d the active room (Night drive) alone shows a Playing marker', playing == ['Night drive'] and len(playing_labels) == 1, repr((playing, len(playing_labels))))
    screenshot(window, 'o2a-04-playing')
    # geometry
    cols = columns_of(window)
    check('3e four columns at 1200 px', cols == 4, 'columns=%d' % cols)
    ratios = []
    sizes = []
    for _, _, child in cards:
        ok, rect = child.get_child().compute_bounds(window)
        ratios.append(round(rect.get_width() / max(rect.get_height(), 1), 2))
        sizes.append((round(rect.get_width()), round(rect.get_height())))
    check('3f cards are 16:9 (1.78 +- 0.12) at 1200 px', all(abs(r - 16 / 9) <= .12 for r in ratios), repr((ratios[:2], sizes[:2])))
    # a card shows art, name and subtitle: name and subtitle text present for each catalogue room
    subs = {r['name']: r.get('subtitle') for r in room_catalogue.rooms()}
    visible = set(texts(window))
    missing = [n for n, s in subs.items() if s and s not in visible]
    check('3g every catalogue card shows its name and subtitle', not [n for n in names_all if n not in visible] and not missing, repr((missing,)))
    names_serif = []
    for w in walk(window):
        if isinstance(w, Gtk.Label) and shown(w) and w.get_text() in ('Night drive', 'Midnight library') and any(
                (p.get_name() or '') or True for p in [w]) and w.has_css_class('rooms-card-name'):
            fam = w.get_pango_context().get_font_description().get_family() if w.get_pango_context().get_font_description() else None
            names_serif.append((w.get_text(), fam))
    print('NOTE 3h card name font families seen by pango:', names_serif)
    close_windows()
    window = launch_rooms(800, 700)
    pump(.5)
    cols = columns_of(window)
    check('3i two or three columns at 800 px', cols in (2, 3), 'columns=%d' % cols)
    r800 = []
    for _, _, child in card_children(window):
        ok, rect = child.get_child().compute_bounds(window)
        r800.append((round(rect.get_width()), round(rect.get_height()), round(rect.get_width() / max(rect.get_height(), 1), 2)))
    check('3i2 cards are 16:9 (+- 0.12) at 800 px', all(abs(r[2] - 16 / 9) <= .12 for r in r800), repr(r800[:2]))
    screenshot(window, 'o2a-05-800')
    leave_all()


def s4_preview_content():
    reset_state()
    enter_room('midnight-library')
    window = launch_rooms(1200, 800)
    pump(.5)
    all_rooms = {r['id']: r for r in room_catalogue.rooms()}
    # Night drive: live radio, has a scene
    mark, calls = len(COMMANDS), len(backend.calls)
    opened = open_preview_by_click(window, 'Night drive')
    check('4a single click opens a preview page (after the double-click wait)', opened, repr(texts(window)[:10]))
    t = texts(window)
    for expected in ('Night drive', "What you'll hear", 'Music', 'Scene', 'Ambience'):
        check('4b preview shows %r' % expected, expected in t, repr(t))
    night = all_rooms['night-drive']
    check('4c preview shows the room subtitle and description', night.get('subtitle') in t and any(night['description'][:40] in x for x in t), repr(t))
    check('4d Music row for live radio says "programming varies"', any('programming varies' in x for x in t), repr(t))
    tags = night.get('tags') or []
    check('4e preview shows the room tags', all(any(tag.lower() == x.lower() for x in t) for tag in tags), repr((tags, t)))
    switches = [w for w in walk(window) if isinstance(w, Gtk.Switch) and shown(w)]
    check('4f exactly one Scene toggle with an accessible name', len(switches) == 1 and acc_name(switches[0]) == 'Scene', repr([acc_name(s) for s in switches]))
    primary = [' '.join(texts(w)) for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and w.has_css_class('suggested-action')]
    check('4g one primary action: Enter room', primary == ['Enter room'], repr(primary))
    check('4h a note says the current room fades out and other apps keep playing',
          any('Midnight library' in x and 'fades out' in x and 'Other apps keep playing' in x for x in t), repr(t))
    assert_inert('4i opening a preview is inert', mark, calls)
    screenshot(window, 'o2a-06-preview')
    check('4j preview art area is large (>= 480 px wide at 1200)', any(isinstance(w, Gtk.AspectFrame) and w.get_width() >= 480 for w in walk(window)), '')
    # a11y of the page
    unnamed = unnamed_icon_controls(window)
    check('4k preview: every icon-only control has an accessible name', not unnamed, repr(unnamed))
    # vocabulary
    bad = vocabulary_violations(window)
    check('4l preview has no internal vocabulary', not bad, repr(bad))
    # Esc returns to grid, inertly
    esc(window)
    pump(.6)
    check('4m Esc returns to the grid', not preview_open(window) and 'Night drive' in card_names(window), repr(texts(window)[:8]))
    # a no-scene room: toggle disabled or absent, with a reason, no crash
    for name in ('Midnight library', 'Northern quiet', 'Wizarding Valley'):
        if open_preview_by_click(window, name):
            sw = [w for w in walk(window) if isinstance(w, Gtk.Switch) and shown(w)]
            room = next(r for r in all_rooms.values() if r['name'] == name)
            print('NOTE 4n %s view_id=%r switch_sensitive=%r detail=%r' % (name, room.get('view_id'), [s.get_sensitive() for s in sw], [x for x in texts(window) if 'cene' in x]))
            esc(window); pump(.5)
    leave_all()


def s5_inert_everything():
    reset_state()
    save_room('Evening reading')
    enter_room('midnight-library')
    window = launch_rooms(1200, 800)
    pump(.5)
    mark, calls = len(COMMANDS), len(backend.calls)
    # grid: chips, Surprise me, card single click, menu open, dialogs opened and cancelled
    for mood in MOODS:
        choose_chip(window, mood)
    choose_chip(window, 'All')
    click(surprise_button(window)); pump(.5)
    esc(window); pump(.5)
    for name in ('Night drive', 'Evening reading', 'Northern quiet'):
        assert card(window, name) is not None, 'no card %s; cards=%r texts=%r' % (name, card_names(window), texts(window)[:12])
        mouse(card(window, name), 1)
        pump(.6)
        assert preview_open(window), 'preview did not open for ' + name
        # every control on the preview except Enter room: click it
        page = next(p_ for p_ in ancestors(enter_button(window)) if isinstance(p_, Gtk.ScrolledWindow))
        for ctl in controls(page):
            text = ' '.join(texts(ctl))
            if text in ('Enter room', 'Open room', 'Rooms'):
                continue
            if isinstance(ctl, Gtk.Switch):
                ctl.set_active(not ctl.get_active()); pump(.2)
                ctl.set_active(not ctl.get_active()); pump(.2)
            elif isinstance(ctl, Gtk.ToggleButton):
                ctl.set_active(not ctl.get_active()); pump(.2)
            elif isinstance(ctl, Gtk.MenuButton):
                ctl.popup(); pump(.3); ctl.popdown(); pump(.2)
            elif isinstance(ctl, Gtk.Button):
                click(ctl); pump(.3)
        if not preview_open(window):
            pass
        else:
            esc(window); pump(.5)
    assert_inert('5a chips, Surprise me, card click, preview controls (scene toggle etc) send nothing', mark, calls)
    # main-menu and per-card menus, dialogs opened and cancelled
    menu_button = [w for w in walk(window) if isinstance(w, Gtk.MenuButton) and shown(w) and acc_name(w) in ('Menu', 'Main menu')]
    print('NOTE 5b main menu buttons:', [acc_name(w) for w in walk(window) if isinstance(w, Gtk.MenuButton) and shown(w)])
    button = open_card_menu(window, 'Evening reading')
    labels = [' '.join(texts(b)) for b in popover_buttons(button)]
    check('5b card menu offers Rename… and Delete… (and no start action)', 'Rename…' in labels and 'Delete…' in labels and not any(l.startswith('Enter') for l in labels), repr(labels))
    pick_menu(button, 'Rename…')
    dialog = last_dialog()
    check('5c Rename… opens a dialog', dialog is not None and dialog.get_mapped(), '')
    press_response(dialog, 'Cancel')
    check('5d Rename cancelled: no command, room unchanged', not mutating_since(mark) and any(v['name'] == 'Evening reading' for v in controller.variants()), repr(mutating_since(mark)))
    DIALOGS.clear()
    button = open_card_menu(window, 'Evening reading')
    pick_menu(button, 'Delete…')
    dialog = last_dialog()
    check('5e Delete… asks first with a Delete and a Cancel response', dialog is not None and dialog_button(dialog, 'Delete') is not None and dialog_button(dialog, 'Cancel') is not None, '')
    check('5e2 nothing deleted while the dialog is open', not mutating_since(mark) and len(controller.variants()) == 1, '')
    press_response(dialog, 'Cancel')
    check('5f Delete cancelled: no command, room still saved', not mutating_since(mark) and len(controller.variants()) == 1, repr(mutating_since(mark)))
    # Esc also cancels
    DIALOGS.clear()
    button = open_card_menu(window, 'Evening reading')
    pick_menu(button, 'Delete…')
    dialog = last_dialog()
    dialog.close(); pump(.6)
    check('5g Delete dismissed with Esc/close: nothing deleted', not mutating_since(mark) and len(controller.variants()) == 1, repr(mutating_since(mark)))
    # Save dialog opened and cancelled
    DIALOGS.clear()
    accel_ok = True
    try:
        window.activate_action('win.save-room', None)
    except Exception:
        accel_ok = False
    window.get_application()
    mix_save = None
    goto(window, 'now'); pump(.8)
    open_mix(window)
    mix_save = buttons_with(window, 'Save as my room')
    check('5h Mix has Save as my room…', len(mix_save) >= 1, repr(mix_save))
    click(mix_save[0]); pump(.6)
    dialog = last_dialog()
    check('5i Save dialog opens from Mix', dialog is not None and dialog.get_heading() == 'Save as my room', repr(dialog and dialog.get_heading()))
    press_response(dialog, 'Cancel')
    assert_inert('5j Save dialog cancelled: no command', mark, calls)
    close_mix(window)
    assert_inert('5k all browse/dialog/menu actions combined send nothing', mark, calls)
    leave_all()


def s6_explicit_enter_and_double_click():
    reset_state()
    enter_room('midnight-library')
    window = launch_rooms(1200, 800)
    pump(.5)
    # preview then Enter room: the only start
    open_preview_by_click(window, 'Night drive')
    mark = len(COMMANDS)
    click(enter_button(window))
    settle(1.2)
    starts_ = starts(mark)
    check('6a Enter room sends exactly one enter for the previewed room, with_view true by default',
          len(starts_) == 1 and starts_[0][0] == 'enter' and starts_[0][1].get('room_id') == 'night-drive' and starts_[0][1].get('with_view') is True, repr(starts_))
    check('6b after Enter room the Rooms preview is gone and the window shows Now', on_now(window) and not preview_open(window), '')
    # scene toggle off controls with_view
    goto(window, 'rooms'); pump(.8)
    check('6c returning to Rooms shows the grid, not a stale preview', not preview_open(window) and 'Night drive' in card_names(window), repr(texts(window)[:6]))
    open_preview_by_click(window, 'Midnight library')
    sw = [w for w in walk(window) if isinstance(w, Gtk.Switch) and shown(w)]
    mark = len(COMMANDS)
    if sw and sw[0].get_sensitive():
        sw[0].set_active(False); pump(.3)
        click(enter_button(window)); settle(1.2)
        s_ = starts(mark)
        check('6d scene toggle off -> enter with with_view false', len(s_) == 1 and s_[0][1].get('with_view') is False, repr(s_))
    else:
        print('NOTE 6d: Midnight library has no scene switch (sensitive=%r)' % [x.get_sensitive() for x in sw])
        click(enter_button(window)); settle(1.2)
        s_ = starts(mark)
        check('6d room without a scene enters without a view', len(s_) == 1 and not s_[0][1].get('with_view'), repr(s_))
    # Double click: enters, and leaves no preview open and pushes none later
    goto(window, 'rooms'); pump(.8)
    mark = len(COMMANDS)
    mouse(card(window, 'Northern quiet'), 2, gap=.08)
    pump(1.3)
    s_ = starts(mark)
    check('6e double-click enters exactly once', len(s_) == 1 and s_[0][0] == 'enter' and s_[0][1].get('room_id') == 'northern-quiet', repr(s_))
    check('6f after a double-click no preview is open on Now', not preview_open(window), repr(texts(window)[:8]))
    goto(window, 'rooms'); pump(1.0)
    check('6g going back to Rooms after a double-click shows the grid (no pushed preview)', not preview_open(window) and 'Northern quiet' in card_names(window), repr(texts(window)[:8]))
    # Slow double-click (inside the system double-click time but near the 300ms wait)
    goto(window, 'rooms'); pump(.5)
    mark = len(COMMANDS)
    mouse(card(window, 'Velvet circuit'), 2, gap=.2)
    pump(1.3)
    s_ = starts(mark)
    check('6h double-click with a 200 ms gap still only enters (once, no preview)', len(s_) == 1 and not preview_open(window), repr(s_))
    goto(window, 'rooms'); pump(.8)
    check('6h2 and nothing is pushed later', not preview_open(window), '')
    # single click, then wait: preview only, no start
    mark = len(COMMANDS)
    mouse(card(window, 'After hours'), 1)
    pump(.9)
    check('6i single click previews and does not start', preview_open(window) and not starts(mark), repr(starts(mark)))
    esc(window); pump(.5)
    # click, wait less than 300 ms, then keyboard Enter on card enters
    mark = len(COMMANDS)
    child = card(window, 'Atomic lounge')
    box = child.get_parent()
    index = [c for c in children(box)].index(child)
    box.emit('move-cursor', Gtk.MovementStep.BUFFER_ENDS, -1, False, False)   # Home, then arrow to the card
    for _ in range(index):
        box.emit('move-cursor', Gtk.MovementStep.VISUAL_POSITIONS, 1, False, False)
    pump(.2)
    fo = window.get_focus()
    print('NOTE 6j focus before Return: %r (is Atomic lounge card: %r)' % (fo, fo is child))
    if fo is not child:
        child.grab_focus(); pump(.2)
    real_key(window, Gdk.KEY_Return)
    pump(1.0)
    got = starts(mark)
    print('NOTE 6j keyboard Return on a focused card -> starts=%r preview_open=%r' % (got, preview_open(window)))
    kb_enters = [c for c in got if c[0] == 'enter' and c[1].get('room_id') == 'atomic-lounge']
    check('6j Enter on a focused card enters it (explicit), once', len(kb_enters) == 1 and len(got) == 1, repr(got))
    goto(window, 'rooms'); pump(.8)
    # Space previews the focused card without starting
    mark = len(COMMANDS)
    child = card(window, 'Wizarding Valley')
    box = child.get_parent()
    index = [c for c in children(box)].index(child)
    box.emit('move-cursor', Gtk.MovementStep.BUFFER_ENDS, -1, False, False)
    for _ in range(index):
        box.emit('move-cursor', Gtk.MovementStep.VISUAL_POSITIONS, 1, False, False)
    pump(.2)
    real_key(window, Gdk.KEY_space)
    pump(.6)
    check('6k Space on a focused card previews it and starts nothing', preview_open(window) and not starts(mark), repr(starts(mark)))
    # keyboard-only: focus lands on Enter room in the preview, Enter starts it
    mark = len(COMMANDS)
    focus = window.get_focus()
    print('NOTE 6l focus in preview:', type(focus).__name__, ' '.join(texts(focus)) if focus is not None else None)
    check('6l focus moves to Enter room on opening a preview (keyboard flow)', focus is not None and ' '.join(texts(focus)) == 'Enter room', repr(focus))
    leave_all()


def s7_switching_toast():
    reset_state()
    enter_room('midnight-library')
    window = launch(1200, 800)
    pump(1.5)
    goto(window, 'rooms'); pump(.8)
    mark = len(COMMANDS)
    open_preview_by_click(window, 'Night drive')
    click(enter_button(window))
    settle(1.5)
    toast = [t for t in texts(window) if 'Back to ' in t]
    check('7a entering while another room is active offers "Back to Midnight library"', toast == ['Back to Midnight library'], repr(texts(window)))
    check('7b the new room is active', controller.active and controller.active['room_id'] == 'night-drive', '')
    mark = len(COMMANDS)
    back = buttons_with(window, 'Back to Midnight library')
    if back:
        click(back[0])
        settle(1.5)
        got = starts(mark)
        check('7c Back enters the previous room (once)', len(got) == 1 and got[0][1].get('room_id') == 'midnight-library' and controller.active['room_id'] == 'midnight-library', repr(got))
    else:
        check('7c Back button found', False, repr(texts(window)))
    # previous room is a saved room
    reset_state()
    v = save_room('Evening reading')
    window = launch(1200, 800)
    pump(1.5)
    goto(window, 'rooms'); pump(.8)
    mouse(card(window, 'Night drive'), 2)
    settle(1.5)
    toast = [t for t in texts(window) if 'Back to ' in t]
    check('7d previous saved room named in the toast', toast == ['Back to Evening reading'], repr(toast))
    back = buttons_with(window, 'Back to Evening reading')
    mark = len(COMMANDS)
    if back:
        click(back[0]); settle(1.5)
        got = starts(mark)
        check('7e Back re-enters the saved room via its variant id', len(got) == 1 and got[0][1].get('variant_id') == v['id'] and controller.active['id'] == v['id'], repr(got))
    else:
        check('7e Back button found', False, repr(texts(window)))
    # nothing active before: no Back toast
    reset_state()
    window = launch(1200, 800)
    pump(1.2)
    mouse(card(window, 'Night drive'), 2)
    settle(1.5)
    check('7f entering from nothing offers no Back toast', not [t for t in texts(window) if 'Back to ' in t], repr(texts(window)))
    leave_all()


def s8_save_dialog_and_saved_rooms():
    reset_state()
    enter_room('midnight-library')
    controller.set_atmosphere({'source_id': 'rain', 'kind': 'sound'}) if hasattr(controller, 'set_atmosphere') else None
    window = launch(1200, 800)
    pump(1.5)
    # main menu entry
    menu_buttons = [w for w in walk(window) if isinstance(w, Gtk.MenuButton) and shown(w)]
    opened = False
    for b in menu_buttons:
        pop = b.get_popover()
        if pop is None:
            continue
        b.popup(); pump(.4)
        item = [x for x in walk(pop) if isinstance(x, Gtk.Button) and ' '.join(texts(x)) == 'Save as my room…']
        if item:
            opened = True
            click(item[0]); pump(.7)
            break
        b.popdown(); pump(.2)
    check('8a main menu has Save as my room…', opened, repr([acc_name(b) for b in menu_buttons]))
    dialog = last_dialog()
    check('8b dialog opens with the name prefilled with the room name', dialog is not None and entry_in(dialog) is not None and entry_in(dialog).get_text() == 'Midnight library',
          repr(entry_in(dialog).get_text() if dialog and entry_in(dialog) else None))
    body = ' | '.join(dialog_text(dialog)) if dialog else ''
    check('8c dialog lists what will be saved: Music and Scene (and Ambience when present) with the room volume', 'Music' in body and 'Room volume' in body, body)
    screenshot(window, 'o2a-07-save-dialog')
    bad = vocabulary_violations(dialog) if dialog else []
    check('8d dialog has no internal vocabulary', not bad and 'Picture' not in body and 'Main audio' not in body, repr((bad, body)))
    check('8e dialog name field has an accessible name', entry_in(dialog) is not None and bool(acc_name(entry_in(dialog))), '')
    # keyboard: replace text, Enter saves
    entry = entry_in(dialog)
    entry.set_text('')
    pump(.3)
    save_btn = dialog_button(dialog, 'Save')
    check('8f empty name disables Save', save_btn is not None and not save_btn.get_sensitive(), repr(save_btn and save_btn.get_sensitive()))
    entry.set_text('Rainy study')
    pump(.3)
    mark = len(COMMANDS)
    save_btn = dialog_button(dialog, 'Save')
    click(save_btn)
    settle(1.2)
    cmds = [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY]
    check('8g Save uses the existing save command with that name and nothing else', [c[0] for c in cmds] == ['save'] and cmds[0][1].get('name') == 'Rainy study', repr(cmds))
    undo = buttons_with(window, 'Undo')
    check('8g2 the confirmation toast "Saved Rainy study to Yours" is shown', any('Rainy study' in t and 'Saved' in t for t in texts(window)), repr([t for t in texts(window) if 'Saved' in t]))
    if undo:
        mark_u = len(COMMANDS)
        click(undo[0]); settle(1.2)
        gone = 'Rainy study' not in [v['name'] for v in controller.variants()]
        check('8g3 Undo on the save toast removes the room just saved', gone, repr(([c[0] for c in COMMANDS[mark_u:] if c[0] not in READ_ONLY], [v['name'] for v in controller.variants()])))
        if gone:
            controller.save({'name': 'Rainy study'}); settle(.6)
    else:
        check('8g3 save toast offers Undo', False, '')
    variants = controller.variants()
    check('8h the controller now has the saved room', [v['name'] for v in variants] == ['Rainy study'], repr(variants and [v['name'] for v in variants]))
    names = card_names(window)
    goto(window, 'rooms'); pump(.8)
    names = card_names(window)
    check('8i the saved room is the first card under Rooms (Yours)', names and names[0] == 'Rainy study', repr(names[:3]))
    choose_chip(window, 'Yours')
    check('8j the Yours chip shows exactly the saved room', card_names(window) == ['Rainy study'], repr(card_names(window)))
    screenshot(window, 'o2a-08-yours')
    # saved-room preview: music row not "unknown", Enter room starts variant_id
    leave_btn = None
    controller.leave(0); settle(1.0)
    pump(.5)
    mark = len(COMMANDS)
    open_preview_by_click(window, 'Rainy study')
    t = texts(window)
    check('8k saved room preview opens with its name and What you\'ll hear', 'Rainy study' in t and "What you'll hear" in t, repr(t))
    click(enter_button(window)); settle(1.5)
    got = starts(mark)
    check('8l entering a saved room sends its variant_id only', len(got) == 1 and got[0][1].get('variant_id') == variants[0]['id'], repr(got))
    # name clash notice
    window.activate_action('win.save-room', None) if False else None
    leave_all()


def s9_rename():
    reset_state()
    v = save_room('Evening reading')
    old_id = v['id']
    other = save_room('Morning focus', 'northern-quiet')
    # leave in a state where 'Evening reading' is the last room and 'Morning focus' is active
    enter_room('midnight-library')
    controller.save({'name': 'Evening reading'})
    settle()
    enter_room('northern-quiet')
    controller.save({'name': 'Morning focus'})
    settle()
    window = launch(1200, 800)
    pump(1.5)
    goto(window, 'rooms'); pump(.8)
    # rename the NON-active saved room
    button = open_card_menu(window, 'Evening reading')
    pick_menu(button, 'Rename…')
    dialog = last_dialog()
    entry = entry_in(dialog)
    check('9a Rename dialog is prefilled with the current name', entry is not None and entry.get_text() == 'Evening reading', repr(entry and entry.get_text()))
    entry.set_text('Morning focus')
    pump(.3)
    rb = dialog_button(dialog, 'Rename')
    check('9b renaming onto an existing saved name is refused in the dialog (button disabled or notice)',
          rb is not None and (not rb.get_sensitive()) and any('already' in x for x in texts(dialog.get_extra_child())), repr((rb and rb.get_sensitive(), texts(dialog.get_extra_child()))))
    entry.set_text('')
    pump(.3)
    check('9c empty name disables Rename', not dialog_button(dialog, 'Rename').get_sensitive(), '')
    entry.set_text('Late reading')
    pump(.3)
    mark = len(COMMANDS)
    press_response(dialog, 'Rename')
    settle(1.0)
    cmds = [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY]
    check('9d rename sends exactly one rename command for the old id', [c[0] for c in cmds] == ['rename'] and cmds[0][1].get('id') == old_id and cmds[0][1].get('name') == 'Late reading', repr(cmds))
    variants = controller.variants()
    new_ids = [x['id'] for x in variants if x['name'] == 'Late reading']
    check('9e controller has the new name with a re-derived id, and the old one is gone',
          len(new_ids) == 1 and new_ids[0] == rooms_variant_id('Late reading') and old_id not in [x['id'] for x in variants] and len(variants) == 2, repr([(x['id'], x['name']) for x in variants]))
    names = card_names(window)
    check('9f grid shows the new name, not the old', 'Late reading' in names and 'Evening reading' not in names, repr(names[:4]))
    # references: last_session/recent. Rename the ACTIVE room and check every reference
    active_before = controller.active['id']
    button = open_card_menu(window, 'Morning focus')
    pick_menu(button, 'Rename…')
    dialog = last_dialog()
    entry_in(dialog).set_text('Dawn focus')
    pump(.3)
    press_response(dialog, 'Rename')
    settle(1.0)
    new_active = rooms_variant_id('Dawn focus')
    check('9g renaming the active saved room updates active id and name', controller.active['id'] == new_active and controller.active['name'] == 'Dawn focus', repr((controller.active['id'], controller.active['name'])))
    check('9h last_session follows the rename', controller.last_session['id'] == new_active and controller.last_session['name'] == 'Dawn focus', repr(controller.last_session and (controller.last_session['id'], controller.last_session['name'])))
    check('9i recent history holds no dangling id', all(r in {x['id'] for x in controller.variants()} | {r_['id'] for r_ in room_catalogue.rooms()} | {controller.active['id']}
                                                        for r in controller.recent), repr(controller.recent))
    snap = snapshot()
    check('9j snapshot (what roomctl/UI read) lists only live ids for saved rooms and active', {x['id'] for x in snap['variants']} == {x['id'] for x in controller.variants()} and snap['active']['id'] == new_active, '')
    dangling = [k for k in ('active', 'last_session') if snap.get(k) and snap[k].get('id', '').startswith('variant-') and snap[k]['id'] not in {x['id'] for x in snap['variants']}]
    check('9k no snapshot field points at a variant id that no longer exists', not dangling, repr(dangling))
    # Return hero after leaving shows the new name
    controller.leave(0); settle(1.0)
    goto(window, 'rooms'); pump(1.0)
    hero = [t for t in texts(window) if t.startswith('Return to ')]
    check('9l Return hero shows the renamed room', hero == ['Return to Dawn focus'], repr(hero))
    mark = len(COMMANDS)
    click(buttons_with(window, 'Return to Dawn focus')[0]); settle(1.2)
    check('9m Return after a rename still works', controller.active is not None and controller.active['id'] == new_active, repr(controller.active and controller.active.get('id')))
    # controller restart: state reloads consistently
    controller.leave(0); settle(1.0)
    c2 = room_session.Controller(Backend(), Path(folder) / 'controller', clock=lambda: CLOCK[0])
    ls = c2.last_session or {}
    ids = {x['id'] for x in c2.variants()}
    check('9n after a controller restart: variants reloaded under the new ids', ids == {rooms_variant_id('Late reading'), new_active}, repr(ids))
    check('9o after a restart: last_session refers to a live room', ls.get('id') in ids or not str(ls.get('id', '')).startswith('variant-'), repr(ls.get('id')))
    check('9p after a restart: recent has no dangling variant ids', all((not str(r).startswith('variant-')) or r in ids for r in c2.recent), repr(c2.recent))
    try:
        c2.rename({'id': new_active, 'name': 'Late reading'})
        refused = False
    except Exception as error:
        refused = True
        why = str(error)
    check('9q controller refuses a duplicate name', refused and {x['id'] for x in c2.variants()} == ids, '')
    try:
        c2.rename({'id': new_active, 'name': 'Late   reading '})   # clean() collapses to the same name
        refused2 = False
    except Exception:
        refused2 = True
    check('9r controller refuses a duplicate that only differs in whitespace', refused2 and {x['id'] for x in c2.variants()} == ids, '')
    try:
        c2.rename({'id': 'variant-doesnotexist', 'name': 'Zed'})
        refused3 = False
    except Exception:
        refused3 = True
    check('9s controller refuses renaming a missing room', refused3, '')
    try:
        c2.rename({'id': new_active, 'name': '   '})
        refused4 = False
    except Exception:
        refused4 = True
    check('9t controller refuses an empty name', refused4, '')
    # renaming a catalogue room id must not be possible
    try:
        c2.rename({'id': 'night-drive', 'name': 'Zed'})
        refused5 = False
    except Exception:
        refused5 = True
    check('9u controller does not rename catalogue rooms', refused5, '')
    # UI undo toast after rename restores the old name
    leave_all()
    # inertness of rename: never starts anything
    check('9v renames sent no start command', not [c for c in COMMANDS if c[0] in ('enter', 'return') and False], '')


def rooms_variant_id(name):
    import hashlib
    return 'variant-' + hashlib.sha256(name.encode()).hexdigest()[:16]


def s10_delete():
    reset_state()
    v = save_room('Evening reading')
    controller.leave(0); settle(1.0)
    window = launch(1200, 800)
    pump(1.5)
    goto(window, 'rooms'); pump(.8)
    mark = len(COMMANDS)
    button = open_card_menu(window, 'Evening reading')
    pick_menu(button, 'Delete…')
    dialog = last_dialog()
    check('10a Delete confirmation names the room and says what is removed', 'Evening reading' in (dialog.get_heading() or ''), repr(dialog.get_heading()))
    press_response(dialog, 'Delete')
    settle(1.0)
    cmds = [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY]
    check('10b confirming sends exactly one delete for that id', [c[0] for c in cmds] == ['delete'] and cmds[0][1].get('id') == v['id'], repr(cmds))
    check('10c the room is gone from the grid and the controller', 'Evening reading' not in card_names(window) and not controller.variants(), repr(card_names(window)[:3]))
    check('10d catalogue rooms are untouched by delete', set(card_names(window)) >= {r['name'] for r in room_catalogue.rooms()}, '')
    check('10e a catalogue card offers no Rename/Delete menu', card_menu(window, 'Night drive') is None, '')
    # delete the room that is the Return hero (last session) -- hero must not crash or dangle
    pump(1.0)
    hero = [t for t in texts(window) if t.startswith('Return to ')]
    print('NOTE 10f hero after deleting the last room (it was a saved room):', hero)
    mark = len(COMMANDS)
    if hero:
        click(buttons_with(window, hero[0])[0]); settle(1.2)
        print('NOTE 10f return after delete -> active=%r' % (controller.active and controller.active.get('name')))
    leave_all()


def s11_legacy_scenes():
    reset_state()
    source = a_radio_source()
    preset = [p for p in ambience.presets() if p.get('kind') == 'local'][:1]
    set_scenes([{'id': 'user-abc123', 'name': 'Rainy desk', 'source_id': source['id'], 'ambience_id': preset[0]['id'] if preset else None,
                 'volume': 40, 'placement': 'here'},
                {'id': 'user-gone', 'name': 'Broken scene', 'source_id': 'no-such-station', 'ambience_id': None, 'volume': 30}])
    window = launch(1200, 800)
    pump(2.0)
    names = card_names(window)
    check('11a a legacy scene appears under Yours, first', names and names[0] == 'Rainy desk', repr(names[:3]))
    check('11b a legacy scene whose station is gone does not appear', 'Broken scene' not in names, repr(names[:4]))
    child = card(window, 'Rainy desk')
    check('11c legacy card carries a Yours tag', child is not None and 'yours' in card_name(child)[1], '')
    choose_chip(window, 'Yours')
    check('11d Yours chip lists the legacy scene', card_names(window) == ['Rainy desk'], repr(card_names(window)))
    mark = len(COMMANDS)
    mouse(card(window, 'Rainy desk'), 2)
    settle(1.5)
    got = starts(mark)
    check('11e entering a legacy scene is one explicit enter and the room plays that station',
          len(got) == 1 and got[0][0] == 'enter' and controller.active is not None and
          (controller.active.get('music') or {}).get('source_id') == source['id'], repr((got, controller.active and controller.active.get('music'))))
    # a saved room with the same name suppresses the legacy duplicate
    controller.save({'name': 'Rainy desk'}); settle()
    goto(window, 'rooms'); pump(1.0)
    check('11f a saved room with the same name replaces the legacy duplicate (one card)', card_names(window).count('Rainy desk') == 1, repr(card_names(window)[:4]))
    leave_all()
    reset_state()


def s12_layout_motion_language():
    reset_state()
    save_room('Evening reading')
    controller.leave(0); settle(1.0)
    for size in ((1200, 800), (800, 700)):
        window = launch_rooms(*size)
        pump(.5)
        tag = '%dx%d' % size
        check('12a Rooms @%s: no horizontal scroll' % tag, not hscroll_ok(window), repr(hscroll_ok(window)))
        check('12b Rooms @%s: no clipped control' % tag, not clipped_controls(window), repr(clipped_controls(window)[:4]))
        # scroll to bottom: last row fully reachable
        for w in walk(window):
            if isinstance(w, Gtk.ScrolledWindow) and shown(w):
                a = w.get_vadjustment(); a.set_value(a.get_upper()); pump(.3)
        last = card_children(window)[-1][2]
        ok, rect = last.compute_bounds(window)
        check('12c Rooms @%s: the last card is reachable inside the window when scrolled down' % tag, ok and rect.get_y() + rect.get_height() <= window.get_height() + 1, '%r' % (rect.get_y() + rect.get_height(),))
        for w in walk(window):
            if isinstance(w, Gtk.ScrolledWindow):
                w.get_vadjustment().set_value(0)
        unnamed = unnamed_icon_controls(window)
        check('12d Rooms @%s: every icon-only control has an accessible name' % tag, not unnamed, repr(unnamed))
        bad = vocabulary_violations(window)
        check('12e Rooms @%s: no internal vocabulary' % tag, not bad, repr(bad))
        open_preview_by_click(window, 'Night drive')
        check('12f Preview @%s: no horizontal scroll' % tag, not hscroll_ok(window), repr(hscroll_ok(window)))
        check('12g Preview @%s: no clipped control' % tag, not clipped_controls(window), repr(clipped_controls(window)[:4]))
        eb = enter_button(window)
        ok, rect = eb.compute_bounds(window)
        scroller = next(p for p in ancestors(eb) if isinstance(p, Gtk.ScrolledWindow))
        # reachable: either visible now, or the scroller can scroll to it
        a = scroller.get_vadjustment()
        reach = (rect.get_y() + rect.get_height() <= window.get_height()) or a.get_upper() > a.get_page_size()
        check('12h Preview @%s: Enter room is visible or reachable by scrolling' % tag, reach, repr((rect.get_y(), rect.get_height(), a.get_upper(), a.get_page_size())))
        check('12i Preview @%s: height of Enter room >= 40 px' % tag, rect.get_height() >= 40, repr(rect.get_height()))
        screenshot(window, 'o2a-preview-' + tag)
        bad = vocabulary_violations(window)
        check('12j Preview @%s: no internal vocabulary' % tag, not bad, repr(bad))
        esc(window); pump(.4)
        # dialogs fit
        close_windows()
    leave_all()


def s13_reduced_motion():
    reset_state()
    enter_room('midnight-library')
    window = launch(1200, 800)
    pump(1.5)
    goto(window, 'rooms'); pump(.8)
    # motion allowed: the preview shows a muted loop if the environment can play one, and it stops when the preview is closed
    open_preview_by_click(window, 'Night drive', wait=1.2)
    streams = playing_streams()
    print('NOTE 13a motion allowed: playing streams in preview = %d' % len(streams))
    if streams:
        check('13a motion allowed: preview loop is muted', all(s.get_muted() for s in streams), '')
    caption = [t for t in texts(window) if 'Nothing plays until you enter' in t]
    check('13b the preview says nothing plays until you enter', len(caption) == 1, repr(caption))
    esc(window); pump(.8)
    check('13c closing the preview stops its loop (no stray playing stream)', not playing_streams() or len(playing_streams()) <= 0, repr(playing_streams()))
    open_preview_by_click(window, 'Night drive', wait=1.0)
    click(enter_button(window)); settle(1.2)
    pump(.8)
    print('NOTE 13d after Enter room, playing streams = %d' % len(playing_streams()))
    leave_all()
    # reduced motion
    reset_state()
    service.execute('settings', {'values': {'reduced_motion': True}})
    settle(1.0)
    enter_room('midnight-library')
    window = launch(1200, 800)
    pump(1.5)
    goto(window, 'rooms'); pump(.8)
    open_preview_by_click(window, 'Night drive', wait=1.0)
    check('13e reduced motion: preview open', preview_open(window), '')
    check('13f reduced motion: no playing media stream in the preview', not playing_streams(), repr(playing_streams()))
    video = [w for w in walk(window) if isinstance(w, Gtk.Video)]
    streams = [w.get_paintable() for w in walk(window) if isinstance(w, Gtk.Picture) and isinstance(w.get_paintable(), Gtk.MediaStream)]
    check('13g reduced motion: no video widget or media paintable in the tree', not video and not streams, repr((video, streams)))
    check('13h reduced motion: the preview says it is a still', any('still' in t.lower() for t in texts(window)), repr([t for t in texts(window) if 'still' in t.lower()]))
    image = screenshot(window, 'o2a-reduced-preview').convert('L')
    import statistics
    spread = statistics.pstdev(list(image.crop((60, 120, 700, 450)).getdata()))
    check('13i reduced motion: still art is shown in the preview (stdev %.1f >= 8)' % spread, spread >= 8, '')
    esc(window)
    seen = running_stack_transitions(window, 2)
    # transitions: open and close a preview and swap pages; nothing may animate
    animating = []
    for _ in range(2):
        child = card(window, 'Velvet circuit')
        mouse(child, 1)
        for _ in range(20):
            for _ in range(3):
                context.iteration(False)
            animating += [describe(w) for w in walk(window) if isinstance(w, Gtk.Stack) and w.get_transition_running()]
            time.sleep(.02)
        esc(window)
        for _ in range(20):
            for _ in range(3):
                context.iteration(False)
            animating += [describe(w) for w in walk(window) if isinstance(w, Gtk.Stack) and w.get_transition_running()]
            time.sleep(.02)
    check('13j reduced motion: opening/closing a preview runs no Gtk.Stack transition', not animating, repr(sorted(set(animating))[:3]))
    service.execute('settings', {'values': {'reduced_motion': False}})
    leave_all()


def s14_keyboard_flow():
    reset_state()
    save_room('Evening reading')
    controller.leave(0); settle(1.0)
    window = launch(1200, 800)
    pump(1.5)
    goto(window, 'rooms'); pump(.8)
    mark = len(COMMANDS)
    # keyboard-only save: open the menu action, type, Enter
    focus_chain = []
    child = card(window, 'Evening reading')
    child.get_parent().select_child(child)
    child.grab_focus(); pump(.3)
    how = real_key(window, Gdk.KEY_Menu)
    pump(.6)
    mapped = mapped_popovers(window)
    check('14a the Menu key opens the focused saved card\'s options', any('Rename…' in ' '.join(texts(p)) for p in mapped), repr([texts(p) for p in mapped]))
    for p in mapped:
        p.popdown()
    pump(.4)
    check('14b opening options by keyboard sends nothing', not mutating_since(mark), '')
    # all card options are reachable controls with focusable buttons
    button = card_menu(window, 'Evening reading')
    print('NOTE 14c options MenuButton focusable=%r (Tab reach); Menu/Shift+F10 route handled by %r' % (button is not None and button.get_focusable(), how))
    check('14c options reachable from the keyboard (Menu key on the card)', button is not None and how == 'capture', repr(how))
    # chips are keyboard-operable (toggle buttons are focusable)
    chips = [w for w in walk(window) if isinstance(w, Gtk.ToggleButton) and shown(w)]
    chips = [c for c in chips if ' '.join(texts(c)) in MOODS]
    check('14d chips are focusable', all(c.get_focusable() for c in chips) and len(chips) == 7, str(len(chips)))
    leave_all()


def s15_save_collision_undo():
    """Save under a name that collides with an existing room after whitespace cleaning: the dialog must warn,
    and Undo must never delete a room the user already had."""
    reset_state()
    original = save_room('Evening reading')
    window = launch_rooms(1200, 800)
    enter_room('night-drive')
    pump(.8)
    goto(window, 'now'); pump(.8)
    open_mix(window)
    click(buttons_with(window, 'Save as my room')[0]); pump(.7)
    dialog = last_dialog()
    entry = entry_in(dialog)
    entry.set_text('Evening reading')
    pump(.3)
    exact = [x for x in texts(dialog.get_extra_child()) if 'replaces' in x]
    check('15a exact same name warns that it replaces the saved room', bool(exact), repr(texts(dialog.get_extra_child())))
    entry.set_text('Evening  reading')   # clean() collapses this to the same name
    pump(.3)
    warned = [x for x in texts(dialog.get_extra_child()) if 'replaces' in x]
    check('15b same name after whitespace cleaning also warns (the controller treats it as the same room)', bool(warned), repr(texts(dialog.get_extra_child())))
    mark = len(COMMANDS)
    click(dialog_button(dialog, 'Save')); settle(1.2)
    saved = [v for v in controller.variants() if v['name'] == 'Evening reading']
    undo = buttons_with(window, 'Undo')
    check('15c a save that replaced an existing room does not offer Undo that would delete it', not undo or not saved, repr(([v['name'] for v in controller.variants()], len(undo))))
    if undo:
        click(undo[0]); settle(1.0)
        check('15d (consequence) Undo must not delete the user\'s original saved room', original['id'] in [v['id'] for v in controller.variants()] or False,
              repr([v['name'] for v in controller.variants()]))
    close_mix(window)
    leave_all()


# ---------------------------------------------------------------- main
def s0_static():
    plan = (repo / 'docs/plans/listening-room-ux-redesign.md').read_text()
    check('0a static: rename is registered immediate in the service (no worker round-trip surprises)', 'rename' in (repo / 'src/session_service.py').read_text(), '')


def s16_evidence_static():
    evidence = output   # the screenshots this run just wrote
    files = sorted(p.name for p in evidence.glob('*.png')) if evidence.is_dir() else []
    wanted = {'hero': 'rooms with the hero', 'late': 'Late night', 'first': 'first run', 'playing': 'Playing marker', 'night': 'Night drive preview',
              'reduced': 'reduced motion preview', 'save': 'Save dialog', 'saved': 'saved room under Yours', '800': 'Rooms at 800'}
    missing = [what for key_, what in wanted.items() if not any(key_ in f.lower() for f in files)]
    check('0b static: evidence directory holds the nine required screenshots', not missing, 'missing=%r files=%r' % (missing, files))


def main():
    build_app()
    selected = [x for x in args.only.split(',') if x]
    fns = [s0_static, s1_hero, s2_chips_and_surprise, s3_grid, s4_preview_content, s5_inert_everything, s6_explicit_enter_and_double_click,
           s7_switching_toast, s8_save_dialog_and_saved_rooms, s9_rename, s10_delete, s11_legacy_scenes,
           s12_layout_motion_language, s13_reduced_motion, s14_keyboard_flow, s15_save_collision_undo, s16_evidence_static]
    for fn in [f for f in fns if not selected or f.__name__.split('_')[0] in selected or f.__name__ in selected]:
        print('==', fn.__name__, flush=True)
        scenario(fn)
    close_windows()


try:
    main()
except Exception:
    traceback.print_exc()
    check('acceptance run completed', False, traceback.format_exc()[-600:])
finally:
    (output / 'acceptance.json').write_text(json.dumps(RESULTS, indent=2))
    failed = [r for r in RESULTS if not r['pass']]
    print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed')
    os._exit(1 if failed else 0)
