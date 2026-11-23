"""O11-SHELL evidence: shortcuts dialog, first-run focus, wide Rooms, scene grid, no-room Now, narrow Listen.

Real GTK on private Xvfb, the session controller on its test double and fixture providers (see o6_env).

  scripts/listening-dev.sh probe OUT listening-o11s-ux
"""
import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o11s-ux')
checks = []


def check(name, ok, detail=''):
    checks.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:400]))


with env:
    Gtk, Gdk, GLib, Adw = env.Gtk, env.Gdk, env.GLib, env.Adw
    pump, settle, shot = env.pump, env.settle, env.shot
    import next_window

    def bounds(widget, window):
        ok, rect = widget.compute_bounds(window)
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()) if ok and widget.get_mapped() else None

    def press(window, name):
        """A real key event through the window's controllers (as the first press of a keyboard user)."""
        keyval = {'Tab': Gdk.KEY_Tab, 'Down': Gdk.KEY_Down, 'Right': Gdk.KEY_Right}[name]
        for controller in list(window.observe_controllers()):
            if isinstance(controller, Gtk.EventControllerKey):
                controller.emit('key-pressed', keyval, 0, Gdk.ModifierType(0))
        pump(.3)

    def ancestors(widget):
        while widget is not None:
            yield widget
            widget = widget.get_parent()

    # ---- first run: focus, and wide Rooms
    window = env.launch(width=1440, height=900, page=None)
    pump(1.0)
    first = window.rooms.cards.get_first_child()
    check('F1 first run: the first room card has the keyboard focus at launch', window.get_focus() is first, window.get_focus())
    card_keys = [c for c in window.rooms.cards.observe_controllers() if isinstance(c, Gtk.EventControllerKey)]
    card_keys[0].emit('key-pressed', Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
    pump(.3)
    check('F2 the first Tab press lands on the first card, not the header or the second card',
          window.get_focus() is first, window.get_focus())
    check('F3 nothing started', not [a for a, _ in env.commands(0) if a in ('music', 'enter', 'link', 'station.start')])
    cards = [bounds(c, window) for c in window.rooms.card_for if c.get_mapped()]
    span = (max(b[0] + b[2] for b in cards) - min(b[0] for b in cards)) * (max(b[1] + b[3] for b in cards) - min(b[1] for b in cards))
    ratio = span / float(window.get_width() * window.get_height())
    total = sum(b[2] * b[3] for b in cards) / float(window.get_width() * window.get_height())
    check('W1 at 1440x900 the cards cover at least 45 percent of the window (bounding box %.2f, card area %.2f)' % (ratio, total),
          ratio >= .45, (ratio, total))
    shot(window, 'rooms-1440-first-run')
    window.close()
    pump(.4)
    for width in (360, 640, 800, 1000):
        window = env.launch(width=width, height=800, page='rooms')
        if width < 560:
            window.browse_rooms()
        pump(1.0)
        shot(window, 'rooms-%d' % width)
        widths = [b[2] for b in (bounds(c, window) for c in window.rooms.card_for) if b]
        check('W2 at %d px every card is at least 150 px wide and inside the window' % width,
              widths and min(widths) >= 150 and all(b[0] + b[2] <= width + 1 for b in (bounds(c, window) for c in window.rooms.card_for) if b),
              widths)
        window.close()
        pump(.4)

    # ---- shortcuts dialog
    window = env.launch(width=1000, height=720, page=None)
    pump(.6)
    window.activate_action('win.shortcuts', None)
    pump(1.0)
    dialog = window.get_visible_dialog()
    check('S1 Keyboard shortcuts is an Adw.ShortcutsDialog', isinstance(dialog, Adw.ShortcutsDialog), type(dialog).__name__)
    shot(window, 'shortcuts')
    app = window.get_application()
    parse = lambda names: sorted(Gtk.accelerator_parse(n)[1:] for n in names)
    bad = [(a, app.get_accels_for_action('win.' + a)) for a, want in next_window.shortcuts_for('win.').items()
           if parse(app.get_accels_for_action('win.' + a)) != parse(want)]
    check('S3 every window shortcut in the table is installed as the table says', not bad and
          len(next_window.shortcuts_for('win.')) == 6, bad)
    triggers = [c for c in window.observe_controllers() if isinstance(c, Gtk.ShortcutController)]
    count = sum(c.get_n_items() for c in triggers)
    check('S4 the Listen shortcuts are installed from the table (3)', count >= 3, count)
    titles = [(p, [r[2] for r in items]) for p, items in next_window.SHORTCUT_PAGES]
    check('S5 pages follow the app (General, Rooms, Now, Listen) and Rooms lists the card options key',
          [p for p, _ in titles] == ['General', 'Rooms', 'Now', 'Listen'] and any('options' in t for t in titles[1][1]), titles)
    pages = [(p, len(i)) for p, i in next_window.SHORTCUT_PAGES]
    check('S2 the table has pages and every SHORTCUTS entry comes from it', len(next_window.SHORTCUTS) == sum(n for _p, n in pages), pages)
    if dialog is not None:
        dialog.force_close()
    pump(.4)
    window.close()
    pump(.4)

    # ---- scene grid: the current scene is marked, a missing one is unavailable, choosing sends one layer command
    env.controller.enter({'room_id': 'midnight-library', 'with_view': True})
    settle(1.5)
    window = env.launch(width=1200, height=800, page=None)
    window.show_now()
    window.open_mix()
    pump(1.2)
    row = window.mix.rows['picture']
    window.presets = [dict(p, available=False) if p['id'] == 'room-velvet-circuit' else p for p in window.presets]
    before = len(env.commands(0))
    row.change.popup()
    pump(1.2)
    popover = row.change.get_popover()
    grid = popover.get_child()
    buttons = [w for w in env.widgets(grid) if isinstance(w, Gtk.Button)]
    pictures = [w for w in env.widgets(grid) if isinstance(w, Gtk.Picture) and w.get_paintable() is not None]
    check('G1 the scene grid has a button per scene (%d) and a picture on those with one (%d)' % (len(buttons), len(pictures)),
          len(buttons) >= 8 and len(pictures) >= 8, (len(buttons), len(pictures)))
    check('G2 browsing the grid sends nothing', len(env.commands(0)) == before, env.commands(0)[before:])
    marked = [b for b in buttons if 'scene-current' in b.get_css_classes()]
    check('G3 exactly the current scene is marked', len(marked) == 1 and window.snapshot['active']['picture'].get('preset_id') in
          [p['id'] for p in window.presets if p['name'].split(' · ')[0] == marked[0].get_child().get_last_child().get_label()],
          [m.get_child().get_last_child().get_label() for m in marked])
    off = [b for b in buttons if not b.get_sensitive()]
    check('G4 a scene whose files are missing is shown but unavailable', len(off) == 1, len(off))
    shot(window, 'scene-grid')
    tiles = [t for t in env.widgets(grid) if isinstance(t, Gtk.FlowBoxChild) and t.get_sensitive()]
    tiles[0].grab_focus()
    box = tiles[0].get_parent()
    keys = [c for c in box.observe_controllers() if isinstance(c, Gtk.EventControllerKey)][0]
    keys.emit('key-pressed', Gdk.KEY_Right, 0, Gdk.ModifierType(0))
    pump(.2)
    right_ok = window.get_focus() is tiles[1]
    keys.emit('key-pressed', Gdk.KEY_Left, 0, Gdk.ModifierType(0))
    pump(.2)
    left_ok = window.get_focus() is tiles[0]
    keys.emit('key-pressed', Gdk.KEY_Down, 0, Gdk.ModifierType(0))
    pump(.2)
    check('G6 Right and Left move between scene tiles, Down by a line', right_ok and left_ok and window.get_focus() is tiles[
        box.get_max_children_per_line()], (right_ok, left_ok))
    target = next(b for b in buttons if b.get_sensitive() and 'scene-current' not in b.get_css_classes())
    target.emit('clicked')
    pump(.5)
    sent = env.commands(0)[before:]
    check('G5 choosing a scene sends one layer select and nothing else', [c[0] for c in sent] == ['layer'] and sent[0][1]['action'] == 'select', sent)
    window.close()
    pump(.4)
    env.service.execute('leave', {})
    settle(1.0)

    # ---- Now with no room (a video playing): neutral, not the last room's art
    video = {'state': None}
    env.controller.backend.start_video = lambda item, placement='here', display=None, cancel=None: video.update(
        state={'status': 'Playing', 'title': item['name'], 'ended': False})
    env.controller.backend.stop_video = lambda: video.update(state=None)
    env.controller.backend.video_state = lambda: dict(video['state']) if video['state'] else None
    env.controller.enter({'room_id': 'midnight-library', 'with_view': True})
    settle(1.5)
    window = env.launch(width=1200, height=800, page=None)
    window.show_now()
    pump(1.0)
    check('N0 precondition: the room scene shows in Now', window.scene.still is not None, window.scene.still)
    env.controller.leave(0)
    settle(1.0)
    video['state'] = {'status': 'Playing', 'title': 'A video', 'ended': False}
    with env.controller.lock:      # as start_video leaves it (the link preview itself needs the network)
        env.controller.video = {'status': 'Playing', 'name': 'A video', 'url': 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
                                'started_at': 0, 'room_paused': False}
    env.controller._changed()
    settle(1.5)
    window.show_now()
    pump(1.0)
    check('N1 Now with only a video shows the Watching state and no scene art', window.current_page() == 'now'
          and window.watching.alone and window.scene.still is None and window.scene.picture.paintable is None,
          (window.current_page(), window.watching.alone, window.scene.still))
    shot(window, 'now-no-room-watching')
    video['state'] = None
    with env.controller.lock:
        env.controller.video = None
    env.controller._changed()
    settle(1.0)
    window.close()
    pump(.4)

    # ---- Listen at 560-700 px: the chips never ellipsize
    env.start_station_room()
    for width in (560, 600, 640, 700):
        window = env.launch(width=width, height=800, page='listen')
        pump(1.2)
        window.listen.fit_scope_chips()
        pump(.4)
        listen = window.listen
        ellipsized = [w.get_text() for root in (listen.index_body, listen.scopes, getattr(listen, 'more_scopes', None))
                      if root is not None for w in env.widgets(root) if isinstance(w, Gtk.Label) and w.get_mapped()
                      and not w.get_wrap() and bounds(w, window) and 0 <= bounds(w, window)[1] < 800 and w.get_text() and w.get_layout().is_ellipsized()]
        check('L1 at %d px no Listen label is ellipsized (%r)' % (width, ellipsized[:4]), not ellipsized, ellipsized)
        shot(window, 'listen-%d' % width)
        window.close()
        pump(.4)
    print('%d passed, %d failed' % (sum(ok for _n, ok in checks), sum(not ok for _n, ok in checks)))
