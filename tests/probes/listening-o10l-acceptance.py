"""O10-LISTEN acceptance: independent oracles for Listen's mode header, the Twitch shelf, layout at 360 / 560 /
desktop width, the words, World radio relevance and the N13 items.

Written by the acceptance author from the brief and UX.md (Proposals A and D, N4, N6-N8, N11-N13), not from the
implementation. Real GTK on a private Xvfb with its own D-Bus (tests/probes/o6_env.py re-executes this file there), the
real session controller on the session test double, fixture providers, and a fake Twitch Helix and radio-browser at
the HTTP layer (so whichever functions the implementation calls, the same wire data comes back). Nothing reaches the
desktop session, the network or real credentials; non-fixture hosts raise.

  scripts/listening-dev.sh acceptance OUT o10l        (judged against tests/probes/expected/o10l.checks)
  O10L_ACCEPTANCE_SECTIONS=T,H limits a run to those sections (G geometry, H mode header, T Twitch, W words,
  R relevance, N N13 items, F first run)

Every section is isolated: one that raises records a FAIL and the rest still run. Readings of the brief that the
probe commits to are stated next to the check that uses them ("READING:").
"""
import copy
import io
import json
import os
from pathlib import Path
import re
import time
import traceback
import urllib.error
from unittest import mock

import sys

import o6_env

# The acceptance runner passes --repo WORKTREE; this probe takes the source tree from it (same as --source WORKTREE/src).
if '--repo' in sys.argv:
    _at = sys.argv.index('--repo')
    _repo = sys.argv[_at + 1]
    del sys.argv[_at:_at + 2]
    if '--source' not in sys.argv:
        sys.argv += ['--source', str(Path(_repo) / 'src')]

env = o6_env.Env(__doc__, '/tmp/listening-o10l-acceptance')
checks = []
ONLY = set(filter(None, os.environ.get('O10L_ACCEPTANCE_SECTIONS', '').split(',')))


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:600]), flush=True)


def section(name):
    def wrap(function):
        if ONLY and name not in ONLY:
            return function
        try:
            function()
        except Exception as error:   # one broken section never hides the others
            check('%s: section ran without raising' % name, False, '%s: %s\n%s' % (
                type(error).__name__, error, traceback.format_exc()[-1800:]))
        finally:
            for window in list(env.app.get_windows()):
                window.close()
            env.pump(.4)
            try:
                env.start_error = None
                env.twitch_connected = False
                env.twitch_curated = []
                if env.controller.active:
                    env.service.execute('leave', {})
                    env.settle(1.0)
            except Exception:
                pass
        return function
    return wrap


with env:
    Gtk, Gdk, GLib, Adw = env.Gtk, env.Gdk, env.GLib, env.Adw
    pump, settle = env.pump, env.settle
    import http_fetch
    import player_core
    import socket
    import urllib.request
    import twitch_channels
    import netguard
    from PIL import Image

    # ------------------------------------------------------------------------------------------ generic helpers

    def widgets(root):
        return list(env.widgets(root))

    def bounds(widget, window):
        if widget is None or not widget.get_mapped():
            return None
        ok, rect = widget.compute_bounds(window)
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok else None

    def overlaps(a, b, slack=.5):
        return bool(a and b and a[0] < b[0] + b[2] - slack and b[0] < a[0] + a[2] - slack
                    and a[1] < b[1] + b[3] - slack and b[1] < a[1] + a[3] - slack)

    def inside(widget, ancestor):
        node = widget
        while node is not None:
            if node is ancestor:
                return True
            node = node.get_parent()
        return False

    def text_of(widget):
        """What a user reads on a control: its label, else its tooltip, else the labels inside it."""
        label = getattr(widget, 'get_label', lambda: None)()
        if isinstance(label, str) and label:
            return label
        found = ' '.join(w.get_text() for w in widgets(widget) if isinstance(w, Gtk.Label) and w.get_text())
        return found or widget.get_tooltip_text() or ''

    def mapped_texts(root):
        found = []
        for w in widgets(root):
            if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text():
                found.append(w.get_text())
        return found

    def text_rect(label, window):
        """The ink rectangle of a label's text in window coordinates (a label's box is wider than its words)."""
        box = bounds(label, window)
        if box is None or not label.get_text():
            return None
        ink, _logical = label.get_layout().get_pixel_extents()
        ox, oy = label.get_layout_offsets()
        return (box[0] + ox + ink.x, box[1] + oy + ink.y, max(ink.width, 1), max(ink.height, 1))

    def toast_widgets(window):
        return [w for w in widgets(window.toasts) if w.get_css_name() == 'toast' and w.get_mapped()]

    def scroller_of(widget):
        node = widget
        while node is not None:
            if isinstance(node, Gtk.ScrolledWindow):
                return node
            node = node.get_parent()
        return None

    def hscrollable(widget):
        node = widget.get_parent()
        while node is not None:
            if isinstance(node, Gtk.ScrolledWindow):
                policy = node.get_policy()[0]
                if policy != Gtk.PolicyType.NEVER:
                    return True
            node = node.get_parent()
        return False

    def wait(predicate, seconds=6.):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            pump(.2)
        return bool(predicate())

    def key_escape(window):
        controllers = [c for c in window.observe_controllers() if isinstance(c, Gtk.EventControllerKey)]
        controllers.sort(key=lambda c: c.get_propagation_phase() != Gtk.PropagationPhase.CAPTURE)
        for controller in controllers:
            if controller.emit('key-pressed', Gdk.KEY_Escape, 0, 0):
                break
        pump(.6)

    def shot(window, name):
        try:
            env.shot(window, name)
        except Exception as error:
            print('screenshot %s failed: %s' % (name, error))

    # Decorations: show the window controls the way a default GTK desktop does, so the mini layout's top row is real.
    Gtk.Settings.get_default().set_property('gtk-decoration-layout', 'menu:minimize,maximize,close')

    CONTROL_TYPES = (Gtk.Button, Gtk.MenuButton, Gtk.Scale, Gtk.Editable, Gtk.WindowControls, Gtk.DropDown)

    def controls_of(window, root=None):
        """Interactive widgets that are mapped and visible, outermost only (a button's own children are not separate)."""
        root = root or window
        found = []
        for w in widgets(root):
            if not isinstance(w, CONTROL_TYPES) or not w.get_mapped() or w.get_width() <= 0 or w.get_opacity() == 0:
                continue
            if isinstance(w, Gtk.Editable) and w.get_parent() is not None and isinstance(w.get_parent(), (Gtk.SearchEntry, Gtk.Entry)):
                continue
            if any(inside(w, other) and other is not w for other in found):
                continue
            found = [other for other in found if not inside(other, w)] + [w]
        return found

    def clashes(window, root=None):
        boxes = [(w, bounds(w, window)) for w in controls_of(window, root)]
        out = []
        for i, (a, ra) in enumerate(boxes):
            for b, rb in boxes[i + 1:]:
                # A control scrolled beneath a floating bar (Listen's now-playing bar) is not an overlap: both
                # must be in the same scrolling context, or both outside any.
                if scroller_of(a) is not scroller_of(b):
                    continue
                if overlaps(ra, rb):
                    out.append((type(a).__name__, text_of(a)[:24], tuple(round(v) for v in ra),
                                type(b).__name__, text_of(b)[:24], tuple(round(v) for v in rb)))
        return out

    def clipped(window, root=None):
        """Words and controls that run off the window's sides and cannot be scrolled to."""
        width = window.get_width()
        out = []
        for w in widgets(root or window):
            if not w.get_mapped() or w.get_width() <= 0:
                continue
            if not isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.Scale, Gtk.Editable, Gtk.Label, Gtk.WindowControls)):
                continue
            if isinstance(w, Gtk.Label) and not w.get_text():
                continue
            box = bounds(w, window)
            if box and (box[0] < -1 or box[0] + box[2] > width + 1) and not hscrollable(w):
                out.append((type(w).__name__, text_of(w)[:24], tuple(round(v) for v in box)))
        return out

    def too_wide(window):
        """What Adwaita logs as "requested N px, M px available": the window's content wants more than it has."""
        wanted = window.toasts.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        return wanted if wanted > window.get_width() + 1 else 0

    def band_limit(window):
        return max(window.header.get_height() if window.header.get_mapped() else 0, 46) + 3

    def band(window):
        """READING: "a visible header" is the window's header bar, or anything else in its top band that stays put
        above the Listen content. A banner in the scrolling page body does not count."""
        limit = band_limit(window)
        out = []
        for w in widgets(window):
            if not w.get_mapped() or w.get_width() <= 0:
                continue
            box = bounds(w, window)
            if box and box[1] + box[3] <= limit:
                out.append(w)
        return out

    def band_texts(window):
        return [w.get_text() for w in band(window) if isinstance(w, Gtk.Label) and w.get_text()]

    def mode_texts(window):
        return [t for t in band_texts(window) if 'ambience' in t.casefold()]

    WAYS_OUT = ('back', 'cancel', 'done', 'exit', 'leave', 'close choosing')

    def ways_out(window):
        found = []
        for w in band(window):
            if not isinstance(w, Gtk.Button) or isinstance(w, Gtk.ToggleButton):
                continue
            if any(isinstance(node, (Gtk.WindowControls, Gtk.PopoverMenu)) for node in _ancestors(w)):
                continue
            name = (text_of(w) or '').casefold()
            if any(word in name for word in WAYS_OUT) and w.get_sensitive():
                found.append(w)
        return found

    def _ancestors(widget):
        node = widget
        while node is not None:
            yield node
            node = node.get_parent()

    def snapshot_layers():
        return json.dumps(copy.deepcopy(env.controller.active), sort_keys=True, default=str)

    def enter_ambience(window, origin):
        """The user's route: Mix > Ambience > Change… (origin mix) or Now's Choose another/Change… (origin now)."""
        window.show_now()
        pump(.5)
        if origin == 'mix':
            window.open_mix()
            pump(.8)
        window.choose_other('atmosphere')
        pump(1.2)

    # ============================================================================ F: first run (N13)

    @section('F')
    def first_run():
        # Must run before any room exists in this probe's private folder.
        window = env.launch(width=1200, height=800, page=None)
        pump(1.0)
        focus = window.get_focus()
        check('F1 at first launch something in the window content has the keyboard focus (not nothing, not the header)',
              focus is not None and not inside(focus, window.header), (focus, window.page_name))
        window.close()
        pump(.4)

    # ============================================================================ G: layout (N6, N7, N8)

    @section('G')
    def geometry():
        env.start_station_room()
        # ---- N6: the mini layout's window controls never overlap the expand button.
        for width in (360, 420, 540):
            window = env.launch(width=width, height=640, page=None)
            pump(1.0)
            player = window.mini_player
            wc = [w for w in widgets(window) if isinstance(w, Gtk.WindowControls) and w.get_mapped() and w.get_width() > 0]
            shown = [b for c in wc for b in widgets(c) if isinstance(b, Gtk.Button) and b.get_mapped()]
            check('G1 at %d px the mini layout is showing and its window controls are on screen (precondition)' % width,
                  window.current_page() == 'mini' and len(shown) >= 1, (window.current_page(), len(shown)))
            expand = bounds(player.expand_button, window)
            hits = [(tuple(round(v) for v in bounds(b, window)), tuple(round(v) for v in expand)) for b in shown
                    if overlaps(bounds(b, window), expand)]
            hits += [(tuple(round(v) for v in bounds(c, window)), tuple(round(v) for v in expand)) for c in wc
                     if overlaps(bounds(c, window), expand)]
            check('G2 at %d px the minimize/maximize/close controls do not overlap "Open the full window"' % width,
                  expand is not None and not hits, (expand, hits[:3]))
            name_rect = text_rect(player.name, window)
            ctrl_rects = [bounds(c, window) for c in wc]
            check('G3 at %d px the room name never runs under the window controls or the expand button' % width,
                  name_rect is not None and not any(overlaps(name_rect, r) for r in ctrl_rects + [expand]), (name_rect, ctrl_rects, expand))
            mini_root = window.mini_player
            top = [c for c in controls_of(window) if (bounds(c, window) or (0, 999, 0, 0))[1] < 80]
            bad = []
            boxes = [(c, bounds(c, window)) for c in top]
            for i, (a, ra) in enumerate(boxes):
                bad += [(type(a).__name__, type(b).__name__) for b, rb in boxes[i + 1:] if overlaps(ra, rb)]
            check('G4 at %d px no two controls in the top row overlap, and none is outside the window' % width,
                  not bad and not [1 for _c, r in boxes if r and (r[0] < -1 or r[0] + r[2] > window.get_width() + 1)],
                  ([(type(c).__name__, tuple(round(v) for v in r)) for c, r in boxes if r], bad))
            shot(window, 'acc-o10l-g-mini-%dpx' % width)
            window.close()
            pump(.5)
        # The separate mini window (Ctrl+M) has its own window controls in its top row.
        window = env.launch(width=1100, height=700, page=None)
        window.open_mini()
        pump(1.2)
        mini = window.mini_window
        for width in (480, 320):
            mini.set_default_size(width, 190)
            pump(1.0)
            player = mini.player
            wc = [w for w in widgets(mini) if isinstance(w, Gtk.WindowControls) and w.get_mapped() and w.get_width() > 0]
            expand = bounds(player.expand_button, mini)
            hits = [c for c in wc if overlaps(bounds(c, mini), expand)]
            check('G5 the Ctrl+M mini window at %d px: its window controls do not overlap the expand button' % width,
                  bool(wc) and expand is not None and not hits, (len(wc), expand, [bounds(c, mini) for c in wc]))
        mini.close()
        pump(.5)
        window.close()
        pump(.5)
        # ---- N7 and the general rule at 360, 560 and desktop width: nothing clipped, nothing overlapping.
        for width in (360, 560, 1200):
            window = env.launch(width=width, height=720, page=None)
            pump(.8)
            narrow = width < 560
            if narrow:
                window.browse_rooms()
            else:
                window.show_rooms()
            pump(1.2)
            page = 'rooms'
            check('G6 at %d px the Rooms page is the page shown (not the mini player)' % width,
                  window.current_page() == 'browse', window.current_page())
            wide = too_wide(window)
            check('G7 at %d px Rooms asks for no more width than the window has (no "requested N px, M available")' % width,
                  not wide, wide)
            out = clipped(window)
            check('G8 at %d px nothing on Rooms (header tabs, mood chips, buttons, words) runs off the window' % width, not out, out[:6])
            both = [t for t in ('Rooms', 'Listen') if any(isinstance(w, Gtk.Label) and w.get_text() == t and w.get_mapped()
                                                           and bounds(w, window) and 0 <= bounds(w, window)[0]
                                                           and bounds(w, window)[0] + bounds(w, window)[2] <= width + 1
                                                           for w in widgets(window))]
            check('G9 at %d px both view targets, Rooms and Listen, are on screen' % width, both == ['Rooms', 'Listen'], both)
            chips = [b for b in widgets(window.rooms.widget) if isinstance(b, Gtk.ToggleButton) and b.get_mapped()]
            lost = [text_of(b) for b in chips if (bounds(b, window) or (0, 0, 0, 0))[0] + (bounds(b, window) or (0, 0, 0, 0))[2] > width + 1
                    and not hscrollable(b)]
            check('G10 at %d px the mood chips wrap or scroll; none is cut off' % width, chips and not lost, (len(chips), lost))
            check('G11 at %d px no two controls on Rooms overlap' % width, not clashes(window), clashes(window)[:4])
            shot(window, 'acc-o10l-g-rooms-%dpx' % width)
            # Listen
            window.show_listen()
            pump(1.2)
            check('G12 at %d px Listen is the page shown' % width, window.current_page() == 'browse'
                  and window.browse.get_visible_child_name() == 'listen', (window.current_page(), window.browse.get_visible_child_name()))
            check('G13 at %d px Listen asks for no more width than the window has' % width, not too_wide(window), too_wide(window))
            check('G14 at %d px nothing on Listen runs off the window' % width, not clipped(window), clipped(window)[:6])
            check('G15 at %d px no two controls on Listen overlap' % width, not clashes(window), clashes(window)[:4])
            shot(window, 'acc-o10l-g-listen-%dpx' % width)
            # Listen in ambience mode (the mode header must fit too). Narrow windows reach Listen through Browse rooms.
            window.listen.begin_ambience('now')
            pump(1.2)
            check('G16 at %d px ambience mode asks for no more width than the window has' % width, not too_wide(window), too_wide(window))
            check('G17 at %d px nothing in ambience mode runs off the window' % width, not clipped(window), clipped(window)[:6])
            check('G18 at %d px no two controls overlap in ambience mode' % width, not clashes(window), clashes(window)[:4])
            shot(window, 'acc-o10l-g-ambience-%dpx' % width)
            window.listen.end_ambience()
            if not narrow:
                window.show_now()
                pump(1.0)
                check('G19 at %d px nothing on Now runs off the window and no controls overlap' % width,
                      not clipped(window) and not clashes(window), (clipped(window)[:4], clashes(window)[:4]))
            window.close()
            pump(.5)
        # ---- N8: toasts never cover the room name or the now-playing line (at 360 the mini layout's own lines).
        # The room of the evidence: a catalogue room entered from Rooms (its large serif name is what a toast sat on).
        env.service.execute('leave', {})
        settle(1.0)
        window = env.launch(width=1200, height=800, page='rooms')
        window.rooms.open_preview(next((e for e in window.rooms.card_for.values() if e.get('source_id')), None)
                                  or list(window.rooms.card_for.values())[0])
        pump(.6)
        window.rooms.preview_widgets['enter'].emit('clicked')
        settle(2.0)
        wait(lambda: window.pending is None and env.controller.active is not None, 8)
        pump(1.0)
        check('G19b (precondition) a catalogue room is playing for the toast checks', bool(env.controller.active), None)
        window.close()
        pump(.5)
        for width, height in ((360, 640), (560, 720), (900, 720), (1200, 800)):
            for action in (False, True):
                window = env.launch(width=width, height=height, page=None)
                window.show_now()
                pump(1.0)
                page = window.current_page()
                if page == 'mini':
                    lines = [window.mini_player.name, window.mini_player.line]
                else:
                    lines = [window.room_name, window.now_line, window.now_detail]
                window.toast('A toast with a button' if action else 'A plain acceptance toast', 'Undo' if action else None,
                             (lambda: None) if action else None)
                pump(1.0)
                toasts = [bounds(t, window) for t in toast_widgets(window)]
                # READING: a line's box counts (a longer name or detail would run under the toast), not only its ink.
                texts = [(l.get_text(), bounds(l, window)) for l in lines if l.get_mapped() and l.get_text()]
                hit = [(t, r) for t, r in texts if r and any(overlaps(r, tb) for tb in toasts)]
                check('G20 at %d px (%s, toast %s) a toast is on screen and covers neither the room name nor the now-playing line'
                      % (width, page, 'with a button' if action else 'plain'),
                      bool(toasts) and len(texts) >= 2 and not hit, (toasts, texts, hit))
                if action and width in (360, 1200):
                    shot(window, 'acc-o10l-g-toast-%dpx' % width)
                window.close()
                pump(.5)
        # The idle fade does not strand a toast with an action over bare art.
        window = env.launch(width=1100, height=720, page=None)
        window.show_now()
        window.kick_idle()
        pump(.5)
        pump(4.7)
        faded = window.controls.get_opacity() < .5
        check('G21 (precondition) with no toast the controls fade after the idle time in this environment', faded, window.controls.get_opacity())
        window.kick_idle()
        window.toast('Ambience: something', 'Undo', lambda: None)
        pump(.3)
        pump(4.5)
        shown = bool(toast_widgets(window))
        check('G22 while a toast with an action is showing the controls and header do not idle-fade away',
              shown and window.controls.get_opacity() > .5 and window.header.get_opacity() > .5,
              (shown, window.controls.get_opacity(), window.header.get_opacity()))
        window.close()
        pump(.4)

    # ============================================================================ H: Listen's mode header (A)

    @section('H')
    def mode_header():
        env.start_station_room()
        mark = len(env.log)
        base = snapshot_layers()
        # ---- from Now
        window = env.launch(width=1200, height=800, page=None)
        window.show_now()
        pump(.6)
        window.show_listen()
        pump(.8)
        check('H0 plain Listen (music mode) shows no ambience header and no "Play as ambience" buttons',
              not mode_texts(window) and not env.buttons(window.listen.index_body, 'Play as ambience'),
              (mode_texts(window), len(env.buttons(window.listen.index_body, 'Play as ambience'))))
        window.show_now()
        pump(.6)
        enter_ambience(window, 'now')
        check('H1 in ambience mode, Listen is the page shown', window.current_page() == 'browse'
              and window.browse.get_visible_child_name() == 'listen', (window.current_page(), window.browse.get_visible_child_name()))
        check('H2 ... and the header (top band, not the scrolling body) names the mode: it says "ambience"',
              bool(mode_texts(window)), band_texts(window))
        out = ways_out(window)
        check('H3 ... and the header holds a way out (a Back/Cancel/Done/Exit control)', bool(out), [text_of(b) for b in band(window)
                                                                                                    if isinstance(b, Gtk.Button)])
        check('H4 ... while browsing there still plays nothing', not env.commands(mark), env.commands(mark))
        shot(window, 'acc-o10l-h-ambience-now')
        if out:
            out[0].emit('clicked')
            pump(1.0)
        check('H5 the header way out leaves the mode and returns to Now, closed Mix, with nothing sent and the room untouched',
              window.current_page() == 'now' and not window.sheet.get_open() and not env.commands(mark) and snapshot_layers() == base,
              (window.current_page(), window.sheet.get_open(), env.commands(mark)))
        check('H6 ... and the header no longer says it is choosing an ambience', not mode_texts(window), band_texts(window))
        if window.listen.mode == 'ambience':   # keep later scenarios independent of this one's outcome
            window.listen.end_ambience(returned=True)
            pump(.6)
        window.show_listen()
        pump(.8)
        check('H7 ... and Listen opened again is plain music mode: no ambience header, no "Play as ambience" buttons',
              not mode_texts(window) and not env.buttons(window.listen.index_body, 'Play as ambience'),
              (mode_texts(window), len(env.buttons(window.listen.index_body, 'Play as ambience'))))
        # ---- from the Mix: the way out returns to the Mix
        window.show_now()
        pump(.5)
        enter_ambience(window, 'mix')
        out = ways_out(window)
        check('H8 from the Mix the header names the mode and offers a way out too', bool(mode_texts(window)) and bool(out),
              (band_texts(window), len(out)))
        if out:
            out[0].emit('clicked')
            pump(1.0)
        check('H9 ... and its way out returns to the Mix over Now', window.current_page() == 'now' and window.sheet.get_open(),
              (window.current_page(), window.sheet.get_open()))
        if window.listen.mode == 'ambience':
            window.listen.end_ambience(returned=True)
            pump(.6)
        window.sheet.set_open(False)
        pump(.5)
        # ---- N1's other routes keep working and leave no stale header behind
        enter_ambience(window, 'now')
        window.show_rooms()
        pump(.8)
        check('H10 going to Rooms ends the mode: the Rooms header shows no ambience text', not mode_texts(window), band_texts(window))
        window.show_listen()
        pump(.8)
        check('H11 ... and Listen opened afterwards is music mode again',
              not mode_texts(window) and not env.buttons(window.listen.index_body, 'Play as ambience'), band_texts(window))
        window.show_now()
        pump(.5)
        enter_ambience(window, 'now')
        key_escape(window)
        check('H12 Escape in ambience mode still leaves it (to Now) and the header returns to normal',
              window.current_page() == 'now' and not mode_texts(window), (window.current_page(), band_texts(window)))
        enter_ambience(window, 'now')
        window.choose_other('music')
        pump(1.0)
        check('H13 Music > Change… while the mode is on opens plain Listen: the mode never survives into it',
              window.current_page() == 'browse' and not mode_texts(window)
              and not env.buttons(window.listen.index_body, 'Play as ambience'), (window.current_page(), band_texts(window)))
        # ---- success ends the mode
        window.show_now()
        pump(.5)
        enter_ambience(window, 'now')
        mark = len(env.log)
        wait(lambda: env.buttons(window.listen.index_body, 'Play as ambience'), 6)
        buttons = env.buttons(window.listen.index_body, 'Play as ambience')
        check('H14 in ambience mode the offline Sounds rows offer "Play as ambience"', bool(buttons), sorted(set(env.texts(window.listen.index_body)))[:12])
        if buttons:
            buttons[0].emit('clicked')
            settle(1.5)
            wait(lambda: window.current_page() == 'now', 5)
            sent = [a for a, args in env.commands(mark) if a == 'atmosphere' or (a == 'layer' and args.get('layer') == 'atmosphere')]
            check('H15 choosing an ambience sends exactly one ambience (atmosphere layer) command, returns to Now and ends the mode',
                  len(sent) == 1 and window.current_page() == 'now' and not mode_texts(window),
                  (env.commands(mark), window.current_page(), band_texts(window)))
        window.close()
        pump(.5)
        # ---- the header fits at 560 and 360 (Listen is reached through Browse rooms in the narrow layout)
        for width in (560, 360):
            window = env.launch(width=width, height=720, page=None)
            pump(.6)
            if width < 560:
                window.browse_rooms()
                pump(.8)
            window.listen.begin_ambience('now')
            pump(1.2)
            texts = mode_texts(window)
            out = ways_out(window)
            labels = [w for w in band(window) if isinstance(w, Gtk.Label) and 'ambience' in w.get_text().casefold()]
            fit = labels and all((bounds(l, window) or (-9, 0, 0, 0))[0] >= -1 and (bounds(l, window) or (0, 0, 0, 0))[0]
                                 + (bounds(l, window) or (0, 0, 0, 0))[2] <= width + 1 and l.get_width() >= 80 for l in labels)
            check('H16 at %d px the header still names the mode, with a way out, inside the window' % width,
                  bool(texts) and bool(out) and bool(fit) and all(
                      0 <= (bounds(b, window) or (-9, 0, 0, 0))[0] and (bounds(b, window) or (0, 0, 0, 0))[0]
                      + (bounds(b, window) or (0, 0, 0, 0))[2] <= width + 1 for b in out), (texts, [text_of(b) for b in out], width))
            header_clashes = [c for c in clashes(window) if (c[2][1] < band_limit(window)) or (c[5][1] < band_limit(window))]
            check('H17 at %d px the header controls (mode, way out, tabs, window controls) do not overlap' % width,
                  not header_clashes, header_clashes[:4])
            shot(window, 'acc-o10l-h-header-%dpx' % width)
            window.close()
            pump(.5)

    # ============================================================================ T: Twitch (N4, Proposal D)

    class Helix:
        """A fake Twitch Helix at the HTTP layer. Music has 230 live streams over three pages, ASMR 45 over one."""
        MUSIC, ASMR = '1001', '1002'

        def __init__(self):
            self.requests = []
            self.fail_cursor = None      # a cursor whose page fails once
            self.failed = False
            letters = 'abcdefghijklmnopqrstuvwxyz'

            def tag(i):
                return letters[i // 676 % 26] + letters[i // 26 % 26] + letters[i % 26]
            self.music = [self.stream('Mchan' + tag(i), 'Music', self.MUSIC, 1234 - 4 * i, 'Session ' + tag(i).upper(), 'en')
                          for i in range(230)]
            self.asmr = [self.stream('Achan' + tag(i), 'ASMR', self.ASMR, 87 - i, 'Whisper ' + tag(i).upper(), 'fi')
                         for i in range(45)]

        @staticmethod
        def stream(name, game, game_id, viewers, title, language):
            return {'id': name.lower() + '-s', 'user_id': name.lower() + '-u', 'user_login': name.lower(), 'user_name': name,
                    'game_id': game_id, 'game_name': game, 'type': 'live', 'title': title, 'viewer_count': viewers,
                    'language': language, 'thumbnail_url': 'https://static-cdn.example.test/%s-{width}x{height}.jpg' % name.lower()}

        def fetch(self, request, timeout=5, limit=0, direct=False, refuse=None):
            url = getattr(request, 'full_url', request)
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname not in ('api.twitch.tv', 'id.twitch.tv'):
                raise OSError('fixture: %s is not reachable' % parsed.hostname)
            query = urllib.parse.parse_qs(parsed.query)
            endpoint = parsed.path.replace('/helix/', '')
            self.requests.append((endpoint, {k: v for k, v in query.items()}))
            first = int((query.get('first') or ['20'])[0])
            if first > 100:
                raise urllib.error.HTTPError(url, 400, 'first is at most 100', {}, None)
            if endpoint == 'games':
                names = {'Music': self.MUSIC, 'ASMR': self.ASMR}
                found = [{'id': names[n], 'name': n} for n in query.get('name', []) if n in names]
                return {'data': found}
            if endpoint == 'search/categories':
                return {'data': [{'id': '1003', 'name': 'Talk Shows & Podcasts'}]}
            if endpoint == 'streams':
                if query.get('user_login') or query.get('user_id'):
                    return {'data': []}
                pool = {self.MUSIC: self.music, self.ASMR: self.asmr}.get((query.get('game_id') or [''])[0], [])
                after = (query.get('after') or [''])[0]
                page = int(after.rsplit('-', 1)[1]) if after else 0
                if after and after == self.fail_cursor and not self.failed:
                    self.failed = True
                    raise OSError('fixture: page failed once')
                chunk = pool[page * 100:(page + 1) * 100][:first]
                more = (page + 1) * 100 < len(pool)
                tagname = 'music' if pool is self.music else 'asmr'
                return {'data': copy.deepcopy(chunk), 'pagination': {'cursor': '%s-%d' % (tagname, page + 1)} if more else {}}
            return {'data': [], 'pagination': {}}

    def chip_labels(window):
        """Category chips: labels of toggles/buttons in Listen outside the scope chip group."""
        found = []
        skip = [g for g in (window.listen.scopes, getattr(window.listen, 'more_scopes', None)) if g is not None]
        for w in widgets(window.listen.surface()):
            if not w.get_mapped() or any(inside(w, g) for g in skip):
                continue
            if isinstance(w, Adw.ToggleGroup):
                found += [(w.get_toggle(i).get_label() or '', w, i) for i in range(w.get_n_toggles())]
            elif isinstance(w, (Gtk.ToggleButton, Gtk.Button)) and not isinstance(w, Gtk.MenuButton):
                found.append((text_of(w), w, None))
        return found

    def press_chip(window, label):
        for text, widget, index in chip_labels(window):
            if text.strip().casefold() == label.casefold():
                if index is not None:
                    widget.set_active(index)
                else:
                    widget.emit('clicked')
                pump(.5)
                return True
        return False

    def listing(window, prefixes=('Mchan', 'Achan')):
        """Every channel row the page can show, found by scrolling the list top to bottom. Returns name -> row text."""
        body = window.listen.index_body
        scroller = scroller_of(body)
        rows = {}

        def collect():
            for w in widgets(body):
                if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text().startswith(prefixes):
                    node = w
                    while node is not None and not any(isinstance(b, Gtk.Button) and (b.get_label() in ('Tune in', 'Play as ambience'))
                                                       for b in widgets(node)):
                        node = node.get_parent()
                    if not any(node is seen for seen in rows.setdefault(w.get_text(), [])):
                        rows[w.get_text()].append(node)
        adjustment = scroller.get_vadjustment() if scroller else None
        if adjustment:
            adjustment.set_value(0)
        pump(.3)
        collect()
        while adjustment and adjustment.get_value() + adjustment.get_page_size() < adjustment.get_upper() - 2:
            adjustment.set_value(min(adjustment.get_value() + adjustment.get_page_size() * .8,
                                     adjustment.get_upper() - adjustment.get_page_size()))
            pump(.25)
            collect()
        return rows

    def show_more(window):
        found = [b for b in widgets(window.listen.surface()) if isinstance(b, Gtk.Button) and b.get_mapped()
                 and text_of(b).strip() == 'Show more']
        return found[0] if found else None

    def page_all(window, helix, limit=30):
        clicks = 0
        while clicks < limit:
            button = show_more(window)
            if button is None:
                break
            before = (len(helix.requests), len(listing(window)))
            button.emit('clicked')
            clicks += 1
            wait(lambda: (len(helix.requests), len(listing(window))) != before, 4)
            pump(.4)
        return clicks

    def open_live(window):
        keys = [k for k, _t in window.listen.scope_list]
        window.listen.scopes.set_active(keys.index('channels'))
        pump(.8)

    @section('T')
    def twitch():
        helix = Helix()
        stopped = [p for p in env.patches if getattr(p, 'attribute', '') == 'discover']
        for p in stopped:
            p.stop()
        guard = mock.patch.object(http_fetch, 'fetch_json', side_effect=helix.fetch)
        guard.start()
        netguard.install()
        env.twitch_connected = True
        try:
            window = env.launch(width=1200, height=900, page='listen')
            mark = len(env.log)
            open_live(window)
            wait(lambda: listing(window), 8)
            labels = [t.strip().casefold() for t, _w, _i in chip_labels(window)]
            check('T1 Live channels has category chips Music, ASMR, All and Your channels',
                  all(x in labels for x in ('music', 'asmr', 'all', 'your channels')), sorted({x for x in labels if x and x != 'tune in'}))
            # READING: the design (Proposal D) names Music, ASMR, All and "Your channels"; "＋ Category" is optional.
            press_chip(window, 'Music')
            wait(lambda: 'Mchanaaa' in listing(window), 8)
            rows = listing(window)
            first = rows.get('Mchanaaa') or [None]
            text = ' | '.join(mapped_texts(first[0])) if first[0] is not None else ''
            check('T2 the Music chip lists Music channels (and no ASMR ones)',
                  'Mchanaaa' in rows and not any(n.startswith('Achan') for n in rows), sorted(rows)[:6])
            viewers = ('1234', '1,234', '1.2 k', '1.2k', '1.2K', '1.2 K', '1 234')
            check('T3 a row shows the channel name, its stream title, its category (Music) and its viewers (1234)',
                  first[0] is not None and 'Session AAA' in text and 'Music' in text and any(v in text for v in viewers), text)
            art = first[0] is not None and any(isinstance(w, (Gtk.Picture, Adw.Avatar)) or (isinstance(w, Gtk.Image)
                                               and w.get_paintable() is not None and not w.get_icon_name()) for w in widgets(first[0]))
            check('T4 a row has art (a picture or avatar slot), not only a monogram tile', art, text)
            check('T5 the page loaded the Music category with at most 100 streams per request',
                  any(e == 'streams' and q.get('game_id') == [helix.MUSIC] and int(q['first'][0]) <= 100 for e, q in helix.requests),
                  helix.requests[:6])
            # ---- paging past 60 with real cursors
            shown_first = len(listing(window))
            clicks = page_all(window, helix)
            rows = listing(window)
            music = [n for n in rows if n.startswith('Mchan')]
            cursors = [q.get('after') for e, q in helix.requests if e == 'streams' and q.get('game_id') == [helix.MUSIC] and q.get('after')]
            check('T6 "Show more" pages past 60: every one of the 230 Music streams can be reached',
                  len(music) == 230, (shown_first, clicks, len(music)))
            check('T7 ... through the cursors Twitch returned, in order (music-1 then music-2), never an invented one',
                  cursors[:2] == [['music-1'], ['music-2']] and len(cursors) == 2, cursors)
            check('T8 ... and no channel appears twice', len(rows) > 60 and all(len(v) == 1 for v in rows.values()),
                  (len(rows), [n for n, v in rows.items() if len(v) > 1][:5]))
            check('T9 ... and once the last page is in, there is no "Show more" left (and the pages really were fetched)',
                  show_more(window) is None and len(music) == 230, (clicks, len(music)))
            check('T10 browsing and paging Twitch sent no command to the room', clicks > 0 and not env.commands(mark), (clicks, env.commands(mark)[:4]))
            shot(window, 'acc-o10l-t-music-paged')
            # ---- ASMR is no longer lost
            press_chip(window, 'ASMR')
            wait(lambda: 'Achanaaa' in listing(window), 8)
            rows = listing(window)
            first = rows.get('Achanaaa') or [None]
            text = ' | '.join(mapped_texts(first[0])) if first[0] is not None else ''
            check('T11 the ASMR chip shows ASMR streams (the first without any paging), with category ASMR and its 87 viewers, and no Music rows',
                  first[0] is not None and 'ASMR' in text and re.search(r'\b87\b', text) and not any(n.startswith('Mchan') for n in rows), text)
            page_all(window, helix)
            rows = listing(window)
            check('T12 ... all 45 ASMR streams are reachable and the last page leaves no "Show more"',
                  len([n for n in rows if n.startswith('Achan')]) == 45 and show_more(window) is None, len(rows))
            press_chip(window, 'All')
            wait(lambda: listing(window), 8)
            page_all(window, helix, 40)
            rows = listing(window)
            kinds = (len([n for n in rows if n.startswith('Mchan')]), len([n for n in rows if n.startswith('Achan')]))
            check('T13 the All chip reaches every Music and every ASMR stream (230 + 45), once each',
                  kinds == (230, 45) and all(len(v) == 1 for v in rows.values()), kinds)
            check('T14 (the original bug) ASMR streams are no longer cut off by a 60 channel cap in the merged list',
                  kinds[1] == 45, kinds)
            window.close()
            pump(.5)
            # ---- Your channels
            env.twitch_curated = ['zzcurated']
            window = env.launch(width=1200, height=900, page='listen')
            open_live(window)
            wait(lambda: listing(window, ('Mchan', 'Achan', 'zzcurated')), 8)
            ok = press_chip(window, 'Your channels')
            pump(.8)
            rows = listing(window, ('Mchan', 'Achan', 'zzcurated'))
            check('T15 the Your channels chip lists the curated channel and none of the category streams',
                  ok and 'zzcurated' in rows and not any(n.startswith(('Mchan', 'Achan')) for n in rows), (ok, sorted(rows)[:5]))
            env.twitch_curated = []
            window.close()
            pump(.5)
            # ---- a failed page keeps what is shown and can be retried
            helix.fail_cursor, helix.failed = 'music-2', False
            window = env.launch(width=1200, height=900, page='listen')
            open_live(window)
            press_chip(window, 'Music')
            wait(lambda: 'Mchanaaa' in listing(window), 8)
            for _ in range(4):
                if show_more(window) is None:
                    break
                show_more(window).emit('clicked')
                pump(1.5)
            kept = len([n for n in listing(window) if n.startswith('Mchan')])
            check('T16 a page that fails does not drop the rows already shown (still at least 100 Music rows)', helix.failed and kept >= 100,
                  (helix.failed, kept))
            for _ in range(4):
                again = show_more(window) or next((w for _t, w, _i in chip_labels(window) if isinstance(w, Gtk.Button)
                                                   and text_of(w).strip().casefold() in ('try again', 'retry')), None)
                if again is None:
                    break
                again.emit('clicked')
                pump(1.5)
            kept = len([n for n in listing(window) if n.startswith('Mchan')])
            check('T17 ... and asking again fetches the page that failed (all 230 reachable after the retry)', kept == 230, kept)
            window.close()
            pump(.5)
        finally:
            env.twitch_connected = False
            guard.stop()
            for p in stopped:
                p.start()

    # ============================================================================ W: words (N11)

    @section('W')
    def words():
        window = env.launch(width=1200, height=800, page='listen')
        keys = [k for k, _t in window.listen.scope_list]

        def primary(scope):
            window.listen.scopes.set_active(keys.index(scope))
            pump(1.0)
            wait(lambda: [b for b in widgets(window.listen.index_body) if isinstance(b, Gtk.Button) and b.get_mapped()
                          and b.get_label() in ('Play', 'Tune in')], 6)
            found = [b.get_label() for b in widgets(window.listen.index_body) if isinstance(b, Gtk.Button) and b.get_mapped()
                     and b.get_label() in ('Play', 'Tune in')]
            return found
        library = primary('library')
        check('W1 Library rows (on demand) say "Play", never "Tune in"', library and set(library) == {'Play'}, library)
        radio = primary('radio')
        check('W2 Radio rows (live) say "Tune in", never "Play"', radio and set(radio) == {'Tune in'}, radio)
        # World radio rows: through the radio-browser fixture below, "Tune in".
        # The link sheet uses the same verb as the rows for the same kind of thing. READING: a Twitch link is a live
        # source, so its sheet says "Tune in" like the Twitch rows (and not "Play").
        window.listen.scopes.set_active(keys.index('all'))
        pump(.6)
        window.listen.search.set_text('https://www.twitch.tv/somechannel')
        pump(1.8)
        opener = env.buttons(window.listen.index_body, 'Open link…')
        if opener:
            opener[0].emit('clicked')
            pump(1.2)
        dialog = window.get_visible_dialog()
        verbs = sorted({b.get_label() for b in widgets(dialog) if isinstance(b, Gtk.Button) and b.get_label()}) if dialog else []
        check('W3 the link sheet for a live (Twitch) link says "Tune in", not "Play", matching the Twitch rows',
              'Tune in' in verbs and 'Play' not in verbs, verbs)
        if dialog is not None:
            dialog.force_close()
            pump(.4)
        window.listen.search.set_text('')
        # Live Twitch rows: "Tune in".
        helix = Helix()
        stopped = [p for p in env.patches if getattr(p, 'attribute', '') == 'discover']
        for p in stopped:
            p.stop()
        guard = mock.patch.object(http_fetch, 'fetch_json', side_effect=helix.fetch)
        guard.start()
        env.twitch_connected = True
        try:
            window.close()
            window = env.launch(width=1200, height=900, page='listen')
            open_live(window)
            wait(lambda: listing(window), 8)
            rows = listing(window)
            verbs = {b.get_label() for n, nodes in rows.items() for node in nodes if node is not None
                     for b in widgets(node) if isinstance(b, Gtk.Button) and b.get_label() in ('Play', 'Tune in')}
            check('W4 live Twitch rows say "Tune in"', rows and verbs == {'Tune in'}, (len(rows), verbs))
        finally:
            env.twitch_connected = False
            guard.stop()
            for p in stopped:
                p.start()
        window.close()
        pump(.4)

    # ============================================================================ R: World radio relevance (N12)

    @section('R')
    def relevance():
        # radio-browser returns its own order (click count, highest first); the fixture mirrors the "rain" case.
        def row(uuid, name, clicks, tags='', votes=0):
            return {'stationuuid': uuid, 'name': name, 'url_resolved': 'https://radio.example.test/' + uuid, 'homepage': '',
                    'tags': tags, 'country': 'Fixtureland', 'countrycode': 'FI', 'language': 'finnish', 'votes': votes,
                    'clickcount': clicks}
        data = [row('u1', 'Kiss FM Ukrainian', 9000, votes=900), row('t2', 'Trainspotting FM', 8000, votes=800),
                row('r3', 'Akashvani FM Rainbow Kolkata', 700, votes=70), row('r4', 'Rain Sounds Radio', 60, 'nature,ambient', 6),
                row('r5', 'Calm Mix', 50, 'chill,rain', 5), row('r6', 'Heavy Rain Radio', 40, 'storm', 4)]
        asked = []

        def urlopen(request, timeout=None):
            url = getattr(request, 'full_url', request)
            if 'radio-browser.info' not in url:
                raise OSError('fixture: not reachable')
            asked.append(url)
            return io.BytesIO(json.dumps(data).encode())

        stopped = [p for p in env.patches if getattr(p, 'attribute', '') == 'search_radio']
        for p in stopped:
            p.stop()
        patches = [mock.patch.object(urllib.request, 'urlopen', side_effect=urlopen),
                   mock.patch.object(socket, 'getaddrinfo', side_effect=OSError('fixture dns')),
                   mock.patch.object(socket, 'gethostbyaddr', side_effect=OSError('fixture dns'))]
        for p in patches:
            p.start()
        try:
            window = env.launch(width=1200, height=800, page='listen')
            keys = [k for k, _t in window.listen.scope_list]
            window.listen.scopes.set_active(keys.index('world'))
            pump(.6)
            window.listen.search.set_text('rain')
            wait(lambda: any(isinstance(w, Gtk.Label) and w.get_text() == 'Rain Sounds Radio' and w.get_mapped()
                             for w in widgets(window.listen.index_body)), 8)
            order = sorted(((bounds(w, window)[1], w.get_text()) for w in widgets(window.listen.index_body)
                            if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text() in [r['name'] for r in data]))
            names = [n for _y, n in order]
            check('R1 the fixture search ran through the fake radio-browser and all six stations are listed', len(names) == 6 and asked, (names, asked[:1]))

            def at(name):
                return names.index(name) if name in names else 99
            noise = ('Kiss FM Ukrainian', 'Trainspotting FM')
            check('R2 whole-word name matches (Rain Sounds Radio, Heavy Rain Radio) rank above substring noise (Ukrainian, Trainspotting)',
                  max(at('Rain Sounds Radio'), at('Heavy Rain Radio')) < min(at(n) for n in noise), names)
            check('R3 a tag match ("rain" in the tags of Calm Mix) ranks above substring noise',
                  at('Calm Mix') < min(at(n) for n in noise), names)
            check('R4 a word-start match (Rainbow) ranks above substring noise',
                  at('Akashvani FM Rainbow Kolkata') < min(at(n) for n in noise), names)
            check('R5 within the same kind of match the more popular station stays first (Rain Sounds before Heavy Rain; Ukrainian before Trainspotting)',
                  at('Rain Sounds Radio') < at('Heavy Rain Radio') and at('Kiss FM Ukrainian') < at('Trainspotting FM'), names)
            check('R6 ranking drops no station and repeats none', sorted(names) == sorted(r['name'] for r in data), names)
            shot(window, 'acc-o10l-r-rain')
            window.close()
        finally:
            for p in reversed(patches):
                p.stop()
            for p in stopped:
                p.start()

    # ============================================================================ N: the N13 items

    @section('N')
    def small_items():
        env.start_station_room()
        window = env.launch(width=1200, height=800, page=None)
        window.shortcuts_dialog()
        pump(1.0)
        dialog = window.get_visible_dialog()
        check('N1 Keyboard shortcuts is an Adw.ShortcutsDialog, not a text block in an alert',
              hasattr(Adw, 'ShortcutsDialog') and isinstance(dialog, Adw.ShortcutsDialog), (hasattr(Adw, 'ShortcutsDialog'), type(dialog).__name__))
        if dialog is not None:
            dialog.force_close()
        pump(.5)
        window.close()
        pump(.4)
        for width in (560, 640):
            window = env.launch(width=width, height=720, page='listen')
            pump(1.0)
            chips = [w for w in widgets(window.listen.scopes) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text()]
            cut = [w.get_text() for w in chips if w.get_layout().is_ellipsized()]
            check('N2 at %d px the Listen scope chips (Your stations, Live channels, …) are not ellipsized' % width, chips and not cut, (cut, len(chips)))
            names = [w for w in widgets(window.listen.index_body) if isinstance(w, Gtk.Label) and w.get_mapped()
                     and w.get_text() == 'Morning lift']
            cut = [w.get_layout().is_ellipsized() for w in names]
            check('N3 at %d px the station card and rows show the station name in full ("Morning lift", not "Mo…")' % width,
                  bool(names) and not any(cut), (len(names), cut))
            shot(window, 'acc-o10l-n-listen-%dpx' % width)
            window.close()
            pump(.4)
        # Recently played never repeats a station (READING: the same name twice in one shelf).
        env.service.execute('leave', {})
        settle(1.0)
        env.start_station_room()
        env.service.execute('leave', {})
        settle(1.0)
        env.start_station_room()
        window = env.launch(width=1200, height=800, page='listen')
        keys = [k for k, _t in window.listen.scope_list]
        window.listen.scopes.set_active(keys.index('all'))
        pump(1.2)
        shelf = [w for w in widgets(window.listen.index_body) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text() == 'Recently played']
        names = []
        if shelf:
            node = shelf[0]
            while node is not None and node.get_parent() is not window.listen.index_body:
                node = node.get_parent()
            names = [l.get_text() for l in widgets(node) if isinstance(l, Gtk.Label) and l.get_mapped() and 'row-name' in l.get_css_classes()]
        check('N4 Recently played shows a station once even when it was played twice', bool(shelf) and bool(names) and len(names) == len(set(names)), (bool(shelf), names))
        window.close()
        pump(.4)
        # The Rooms page uses the space at 1440 px. READING: the evidence shows the lower half empty (the width is
        # fine), so the cards (or a first-run hero) together must cover at least 45% of the area under the header.
        env.service.execute('leave', {})
        settle(1.0)
        window = env.launch(width=1440, height=900, page='rooms')
        pump(1.2)
        cards = [bounds(c, window) for c in window.rooms.card_for if c.get_mapped()]
        hero = [bounds(w, window) for w in widgets(window.rooms.widget) if w.get_mapped() and 'hero' in ' '.join(w.get_css_classes())]
        spans = [b for b in cards + hero if b]
        area = ((max(b[0] + b[2] for b in spans) - min(b[0] for b in spans)) * (max(b[1] + b[3] for b in spans) - min(b[1] for b in spans))
                if spans else 0) / float(window.get_width() * (window.get_height() - 48))
        check('N5 at 1440 px the Rooms cards (or a hero) cover at least 45% of the area under the header', area >= .45, round(area, 2))
        shot(window, 'acc-o10l-n-rooms-1440')
        window.close()
        pump(.4)
        # The scene Change… list is not text only.
        env.start_station_room()
        window = env.launch(width=1200, height=800, page=None)
        window.show_now()
        window.open_mix()
        pump(1.2)
        row = window.mix.rows.get('picture')
        popover = row.change.get_popover() if row is not None and isinstance(row.change, Gtk.MenuButton) else None
        if popover is not None:
            row.change.popup()
            pump(1.0)
        pictures = [w for w in widgets(popover.get_child()) if isinstance(w, (Gtk.Picture, Adw.Avatar)) or (isinstance(w, Gtk.Image)
                    and w.get_paintable() is not None and not w.get_icon_name())] if popover is not None and popover.get_child() else []
        options = [w for w in widgets(popover.get_child()) if isinstance(w, Gtk.Button)] if popover is not None and popover.get_child() else []
        check('N6 the scene Change… list shows a picture for each scene, not text only', len(options) >= 2 and len(pictures) >= len(options),
              (len(options), len(pictures)))
        if popover is not None:
            row.change.popdown()
        window.close()
        pump(.4)

    failed = [name for name, ok in checks if not ok]
    (env.output / 'acceptance-o10l.json').write_text(json.dumps({'checks': checks}, indent=1))
    print('%d passed, %d failed' % (len(checks) - len(failed), len(failed)))
