"""Time selection in the next window: input to visible result, and blocking work on the GTK thread.

Each interaction is repeated with fixtures; the probe reports the median time from the input
event to the first painted frame that shows the result, and every file, network, decode or
process call the GTK thread itself made while handling it. Budgets: a room preview within
150 ms of the release, every other selection within 100 ms, and no blocking call.

  scripts/listening-dev.sh probe OUT listening-o6-timing
  python3 tests/probes/listening-o6-timing.py --output DIR [--source SRC] [--enforce]

Pass --source with another checkout's src to measure an older build the same way.
"""
import json
import os
import statistics
import sys
import threading
import time

import o6_env

env = o6_env.Env(__doc__, '/tmp/listening-o6-timing', {'--enforce': {'action': 'store_true'},
                                                         '--repeat': {'type': int, 'default': 5}})
ROOM_BUDGET_MS, SELECT_BUDGET_MS = 150., 100.
results, audits, failures = [], [], []

with env:
    Gtk, Gdk, GLib = env.Gtk, env.Gdk, env.GLib
    MAIN = threading.main_thread()

    # ---- main-thread I/O audit ----------------------------------------------------------
    import builtins
    import io
    import socket
    import subprocess
    from pathlib import Path
    gi_gdk = Gdk
    watched = []

    def wrap(owner, name, kind):
        original = getattr(owner, name)

        def audited(*args, **kwargs):
            if env.recording is not None and threading.current_thread() is MAIN:
                target = args[0] if args else kwargs.get('file') or ''
                text = str(target)
                if not text.startswith(('/proc/', '/sys/', '/dev/')) and 'site-packages' not in text \
                        and '/nix/store' not in text and not text.endswith(('.so', '.pyc')):
                    env.recording.append('%s %s' % (kind, text[:90]))
            return original(*args, **kwargs)
        try:
            setattr(owner, name, audited)
            watched.append((owner, name, original))
        except (TypeError, AttributeError):
            pass

    env.recording = None
    wrap(builtins, 'open', 'open')
    wrap(io, 'open', 'open')
    wrap(os, 'stat', 'stat')
    wrap(os, 'listdir', 'listdir')
    wrap(os, 'scandir', 'scandir')
    wrap(Path, 'read_text', 'read_text')
    wrap(Path, 'read_bytes', 'read_bytes')
    wrap(Path, 'is_file', 'is_file')
    wrap(Path, 'exists', 'exists')
    wrap(subprocess, 'run', 'subprocess')
    wrap(subprocess.Popen, '__init__', 'subprocess')
    wrap(socket.socket, 'connect', 'connect')
    wrap(socket, 'getaddrinfo', 'dns')
    wrap(Gdk.Texture, 'new_from_filename', 'decode')

    def frame_after(window):
        """Iterate the main loop until the next frame has been painted."""
        clock = window.get_frame_clock()
        painted = []
        handler = clock.connect('after-paint', lambda *_: painted.append(time.monotonic()))
        window.queue_draw()
        deadline = time.monotonic() + 1.0
        while not painted and time.monotonic() < deadline:
            while env.context.pending():
                env.context.iteration(False)
            time.sleep(.002)
        clock.disconnect(handler)

    def measure(name, window, act, done, budget, repeat=None, reset=None, settle_after=.25):
        """Median ms from act() to done() being true and a frame painted; plus main-thread I/O calls."""
        samples, calls = [], []
        for _ in range(repeat or env.args.repeat):
            if reset:
                reset()
                env.pump(settle_after)
            env.recording = []
            start = time.monotonic()
            act()
            handler_ms = (time.monotonic() - start) * 1000
            deadline = start + 3.
            while not done() and time.monotonic() < deadline:
                while env.context.pending():
                    env.context.iteration(False)
                time.sleep(.001)
            reached = done()
            frame_after(window)
            samples.append((time.monotonic() - start) * 1000 if reached else float('inf'))
            calls.extend(env.recording)
            env.recording = None
            samples_handler = handler_ms
        median = statistics.median(samples)
        unique = sorted(set(calls))
        results.append({'name': name, 'median_ms': round(median, 1), 'samples_ms': [round(s, 1) for s in samples],
                        'handler_ms': round(samples_handler, 1), 'budget_ms': budget,
                        'blocking_calls': unique})
        if median > budget:
            failures.append('%s: %.0f ms over %.0f ms' % (name, median, budget))
        if unique:
            failures.append('%s: GTK thread did blocking work: %s' % (name, unique[:4]))
        print('%-52s %7.1f ms (budget %3.0f)  handler %5.1f ms  blocking calls on GTK thread: %d'
              % (name, median, budget, samples_handler, len(unique)))
        for call in unique[:6]:
            print('      ', call)

    def card_children(window):
        child, found = window.cards.get_first_child(), []
        while child:
            found.append(child)
            child = child.get_next_sibling()
        return found

    def gestures(widget):
        found = []
        for item in env.widgets(widget):
            for controller in item.observe_controllers():
                if isinstance(controller, Gtk.GestureClick):
                    found.append(controller)
        return found

    try:
        env.start_station_room()
        env.twitch_curated = ['lofigirl']
        window = env.launch(page='rooms')
        env.pump(1.5)

        # ---- Rooms: one click on a card previews it ---------------------------------------
        cards = card_children(window)
        by_name = {window.rooms.card_for[c]['name']: c for c in cards}
        names = list(by_name)[:2]

        def click(name):
            gesture = [g for g in gestures(by_name[name]) if g.get_button() == 1][0]
            gesture.emit('pressed', 1, 10., 10.)
            gesture.emit('released', 1, 10., 10.)

        def on_preview():
            return window.rooms.stack.get_visible_child_name().startswith('preview') and window.rooms.preview_widgets is not None \
                and window.rooms.preview_widgets['page'].get_mapped()

        def back_to_grid():
            window.rooms.close_preview(animate=False)

        measure('room card: single click to preview visible', window, lambda: click(names[0]), on_preview,
                ROOM_BUDGET_MS, reset=back_to_grid, settle_after=.5)
        measure('room card: single click on another card', window, lambda: click(names[1]), on_preview,
                ROOM_BUDGET_MS, reset=back_to_grid, settle_after=.5)
        back_to_grid()
        env.pump(.5)

        # ---- Listen: scope chips, rows -----------------------------------------------------
        window.show_listen()
        env.pump(1.0)
        page = window.listen
        titles = lambda: {l.get_text() for l in env.widgets(page.index_body) if isinstance(l, Gtk.Label)}
        measure('Listen: scope chip Your stations to rows visible', window, lambda: page.scopes.set_active(1),
                lambda: 'Morning lift' in titles(), SELECT_BUDGET_MS,
                reset=lambda: page.scopes.set_active(0))
        measure('Listen: scope chip Radio', window, lambda: page.scopes.set_active(2),
                lambda: any('Groove' in t for t in titles()), SELECT_BUDGET_MS, reset=lambda: page.scopes.set_active(0))
        measure('Listen: scope chip World radio', window, lambda: page.scopes.set_active(5),
                lambda: any('World radio' == t for t in titles()), SELECT_BUDGET_MS, reset=lambda: page.scopes.set_active(0))
        measure('Listen: scope chip Library', window, lambda: page.scopes.set_active(3),
                lambda: 'Neoclassical collection' in titles(), SELECT_BUDGET_MS, reset=lambda: page.scopes.set_active(0))
        page.scopes.set_active(0)
        env.pump(.5)
        start = len(env.log)
        tune = env.buttons(page.index_body, 'Tune in')[0]
        measure('Listen: Tune in on a row (command sent)', window, lambda: tune.emit('clicked'),
                lambda: any(c[0] in ('station.start', 'music', 'link', 'online') for c in env.commands(start)),
                SELECT_BUDGET_MS, repeat=1)
        env.pump(1.0)
        window.show_listen()
        env.pump(.5)

        # ---- Station page: mood choice -----------------------------------------------------
        station = next(s for s in env.controller.stations() if s['name'] == 'Morning lift')
        page.show_station(station)
        env.pump(.8)
        group = page.mood_groups['energy']
        flip = [0]

        def choose_mood():
            flip[0] ^= 1
            group.set_active(0 if flip[0] else 2)
        measure('Station mood choice (Energy) reflected', window, choose_mood,
                lambda: group.get_active() == (0 if flip[0] else 2), SELECT_BUDGET_MS, repeat=4, settle_after=.9)
        page.show_index()
        env.pump(.6)

        # ---- Mix --------------------------------------------------------------------------
        window.show_now()
        env.pump(1.0)
        measure('Mix: open the sheet', window, lambda: window.open_mix(), lambda: window.sheet.get_open(),
                SELECT_BUDGET_MS, reset=lambda: window.sheet.set_open(False))
        window.open_mix()
        env.pump(.8)
        music = window.mix.rows['music']
        measure('Mix: Music Change… opens Listen', window, lambda: music.change.emit('clicked'),
                lambda: window.browse.get_visible_child_name() == 'listen' and window.current_page() == 'browse',
                SELECT_BUDGET_MS, reset=lambda: (window.show_now(), window.open_mix()), settle_after=.6)
        ambience_row = window.mix.rows['atmosphere']
        if hasattr(window.listen, 'begin_ambience'):
            measure('Mix: Ambience Change… opens Listen (ambience mode)', window,
                    lambda: ambience_row.change.emit('clicked'),
                    lambda: window.current_page() == 'browse' and window.listen.mode == 'ambience',
                    SELECT_BUDGET_MS, reset=lambda: (window.listen.end_ambience(), window.show_now(), window.open_mix()),
                    settle_after=.6)
        window.show_now()
        window.open_mix()
        env.pump(.6)
        mute = window.mix.rows['music'].mute
        measure('Mix: Music mute toggle reflected', window, lambda: mute.set_active(not mute.get_active()),
                lambda: True, SELECT_BUDGET_MS, repeat=3, settle_after=.4)
    finally:
        for owner, name, original in watched:
            try:
                setattr(owner, name, original)
            except (TypeError, AttributeError):
                pass
    out = env.output
    (out / 'timing.json').write_text(json.dumps({'results': results, 'failures': failures}, indent=2))
    lines = ['%-52s %7.1f ms  budget %3.0f  blocking calls %d' % (r['name'], r['median_ms'], r['budget_ms'], len(r['blocking_calls']))
             for r in results]
    (out / 'timing.txt').write_text('\n'.join(lines) + '\n')
    print('\n%d measurements, %d budget or blocking-work findings' % (len(results), len(failures)))
    for failure in failures:
        print('FINDING', failure)
    if env.args.enforce and failures:
        sys.exit(1)
