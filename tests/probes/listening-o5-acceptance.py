"""Independent acceptance probe for O5 (Glance and finish): the mini player, the Waybar popover, startup,
keyboard, focus rings, contrast in all eight rooms, adaptive widths, reduced motion, room-volume agreement
and the parity oracles for plan criterion 13. Owned by the acceptance author, not the implementer.

It reuses the fake-controller harness of tests/probes/listening-next-acceptance.py (everything above its
"scenarios" marker is executed here), so the controller, the fake backend and the widget oracles are the same.

Run inside `scripts/listening-dev.sh shell` under private Xvfb and D-Bus:

    env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u HYPRLAND_INSTANCE_SIGNATURE XDG_RUNTIME_DIR=<0700 dir> \
    GDK_BACKEND=x11 GDK_DEBUG=no-portals GSK_RENDERER=cairo xvfb-run -a -s '-screen 0 1440x1000x24' \
    dbus-run-session python3 tests/probes/listening-o5-acceptance.py --repo WORKTREE --output OUTDIR [--only s3,s4]

Checks named "PARITY" are the criterion 13 oracles. They fail while a capability of the personal-radio contract
has no path in the window. Checks named "NOTE" print information and never fail.
"""
import re
import subprocess
from pathlib import Path

_HARNESS = Path(__file__).with_name('listening-next-acceptance.py')
_TEXT = _HARNESS.read_text()
exec(compile(_TEXT[:_TEXT.index('# ---------------------------------------------------------------- scenarios')], str(_HARNESS), 'exec'), globals())

import next_mini  # noqa: E402
import next_model  # noqa: E402
import roomctl  # noqa: E402
from PIL import ImageGrab  # noqa: E402

ROOMS = [r['id'] for r in room_catalogue.rooms()]
GONE = ('room_experience', 'connections_ui', 'twitch_ui', 'world_radio_ui', 'room_discovery')
SOURCE = Path(args.repo).resolve() / 'src'


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


def shortcut_bound(window, trigger_text):
    """The Gtk.Shortcut on a window-level ShortcutController for this trigger, or None."""
    for controller in window.observe_controllers():
        if isinstance(controller, Gtk.ShortcutController):
            for index in range(controller.get_n_items()):
                shortcut = controller.get_item(index)
                if shortcut.get_trigger() is not None and shortcut.get_trigger().to_string() == trigger_text:
                    return shortcut
    return None


def shortcut(window, trigger_text):
    found = shortcut_bound(window, trigger_text)
    assert found is not None, 'no shortcut for ' + trigger_text
    found.get_action().activate(Gtk.ShortcutActionFlags.EXCLUSIVE, window, None)
    pump(.6)


def note(text):
    print('NOTE ' + text, flush=True)


def mutating(mark):
    return [c for c in COMMANDS[mark:] if c[0] not in READ_ONLY]


def app():
    return build_app()


def minis():
    return [w for w in app().get_windows() if isinstance(w, next_mini.MiniWindow)]


def mains():
    return [w for w in app().get_windows() if isinstance(w, next_window.NextWindow)]


def set_idle():
    leave_all()


def last_room_recorded(room='midnight-library'):
    enter_room(room)
    controller.leave(0)
    settle(1.0)


def play_pause_button(root):
    return one(root, 'Pause the room') or one(root, 'Play the room')


def deliver(*argv, wait=12):
    """A second `listening-room ...` process: the application forwards it to the running window."""
    code = 'import sys, next_window; sys.exit(next_window.run(["listening-room"] + sys.argv[1:]))'
    env = dict(os.environ, PYTHONPATH=str(SOURCE))
    process = subprocess.Popen([sys.executable, '-c', code] + list(argv), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + wait
    while process.poll() is None and time.monotonic() < deadline:
        pump(.1)
    if process.poll() is None:
        process.kill()
        return None, 'timed out'
    pump(.8)
    return process.returncode, process.stderr.read()


def grab(window):
    pump(.3)
    return ImageGrab.grab(bbox=(0, 0, window.get_width(), window.get_height()), xdisplay=os.environ['DISPLAY']).convert('RGB')


def text_labels(root, skip_popovers=True):
    return [w for w in walk(root) if isinstance(w, Gtk.Label) and shown(w) and w.get_text()
            and not (skip_popovers and in_popover(w))]


def contrast_of(root, labels):
    """Hide the labels, photograph what is behind them, compare each label's colour with the
    90th-percentile luminance of its own box. The box is padded by one pixel on each side."""
    for label in labels:
        label.set_opacity(0)
    pump(.5)
    image = grab(root)
    for label in labels:
        label.set_opacity(1)
    pump(.2)
    out = []
    for label in labels:
        ok, rect = label.compute_bounds(root)
        if not ok:
            continue
        x0, y0 = max(0, int(rect.get_x())), max(0, int(rect.get_y()))
        x1, y1 = min(root.get_width(), int(rect.get_x() + rect.get_width())), min(root.get_height(), int(rect.get_y() + rect.get_height()))
        if x1 <= x0 or y1 <= y0:
            continue
        pixels = sorted(image.crop((x0, y0, x1, y1)).getdata(), key=luminance)
        p90 = pixels[int(.9 * (len(pixels) - 1))]
        rgba = label.get_color()
        fg = (rgba.red * 255, rgba.green * 255, rgba.blue * 255)
        eff = tuple(rgba.alpha * f + (1 - rgba.alpha) * b for f, b in zip(fg, p90))
        out.append((label.get_text(), round(contrast_ratio(eff, p90), 2)))
    return out


def full_page_scrollbars(window):
    bars = []
    for w in walk(window):
        if isinstance(w, Gtk.ScrolledWindow) and shown(w):
            hbar = w.get_hadjustment()
            if hbar is not None and hbar.get_upper() > hbar.get_page_size() + 1 and w.get_hscrollbar().get_visible():
                bars.append((describe(w)[:80], hbar.get_upper(), hbar.get_page_size()))
    return bars


def clipped_controls(window, root=None):
    root = root or window
    clipped = []
    width, height = root.get_width(), root.get_height()
    for w in controls(root):
        ok, rect = w.compute_bounds(root)
        scrolls = any(isinstance(p, Gtk.ScrolledWindow) for p in ancestors(w))   # a list below the fold is reached by scrolling
        if ok and (rect.get_x() < -1 or rect.get_x() + rect.get_width() > width + 1
                   or (not scrolls and (rect.get_y() < -1 or rect.get_y() + rect.get_height() > height + 1))):
            clipped.append((acc_name(w) or type(w).__name__, int(rect.get_x()), int(rect.get_y()), int(rect.get_width()), int(rect.get_height())))
    return clipped


# ====================================================================== S1 startup
def s1_startup():
    env = dict(os.environ, PYTHONPATH=str(SOURCE))
    for argv in (['--ui=classic'], ['--ui', 'classic'], ['--desk', '--ui=classic']):
        done = subprocess.run([sys.executable, '-c', 'import sys, listening_room; sys.argv = ["listening-room"] + %r; sys.exit(listening_room.main())' % argv],
                              env=env, capture_output=True, text=True, timeout=60)
        check('S1 %s exits with status 2 and a message, and opens nothing' % ' '.join(argv),
              done.returncode == 2 and 'classic' in done.stderr and not done.stdout.strip(), repr((done.returncode, done.stderr[:200])))
    check('S1 classic modules are not importable', not any((SOURCE / (m + '.py')).exists() for m in GONE) and not any(m in sys.modules for m in GONE))
    leave_all()
    window = launch(1200, 800)
    check('S1 the default launch opens the one window (NextWindow, application id app.kotona.ListeningRoom)',
          type(window).__module__ == 'next_window' and window.get_application().get_application_id() == 'app.kotona.ListeningRoom',
          repr((type(window), window.get_application().get_application_id())))
    check('S1 first run shows Rooms, with nothing playing', on_rooms_return(window) or any('Pick a room' in t for t in texts(window)), repr(texts(window)[:6]))
    note('S1 an unknown flag makes NextApp.do_command_line return 1 with no message (no stderr line is printed by next_window)')
    leave_all()


# ====================================================================== S2 mini player
def s2_mini():
    leave_all()
    window = launch(1200, 800)
    size = (window.get_width(), window.get_height())
    mark, calls = len(COMMANDS), len(backend.calls)
    accel(window, '<Control>m')
    pump(1.0)
    mini = getattr(window, 'mini_window', None)
    check('S2a Ctrl+M opens the mini player in its own window and leaves the main window alone',
          mini is not None and mini is not window and mini.get_visible() and window.get_visible()
          and (window.get_width(), window.get_height()) == size, repr((mini, size, (window.get_width(), window.get_height()))))
    accel(window, '<Control>m')
    check('S2b a second Ctrl+M does not open a second mini window', len(minis()) == 1, repr(minis()))
    assert_inert('S2c opening the mini player on first run starts nothing', mark, calls)
    mini.close()
    pump(.5)
    check('S2e closing the mini player leaves the main window open', window.get_visible() and not minis())

    # --- return state: a last room exists and nothing plays
    last_room_recorded('midnight-library')
    window = launch(1200, 800)
    accel(window, '<Control>m')
    pump(1.2)
    mini = window.mini_window
    mark, calls = len(COMMANDS), len(backend.calls)
    ret = button_with_text(mini, 'Return', exact=False)
    check('S2f with a last room recorded the mini player offers Return and shows the room name, no pause or volume',
          ret is not None and play_pause_button(mini) is None, repr(texts(mini)))
    assert_inert('S2g it sat there without starting anything', mark, calls)
    click(ret)
    settle(1.5)
    check('S2h Return sends the return command once and the room plays', [c[0] for c in mutating(mark)].count('return') == 1
          and snapshot().get('active') is not None, repr(mutating(mark)))

    # --- playing: every element and what it sends
    pump(1.2)
    texts_now = texts(mini)
    name = controller.active['name']
    check('S2i the mini player shows art, the room name, the now-playing line, pause, volume, timer and expand',
          name in texts_now and len(texts_now) >= 2 and play_pause_button(mini) and one(mini, 'Room volume') and one(mini, 'Timer')
          and one(mini, 'Open the full window') and mini.player.art.paintable is not None,
          repr((texts_now, mini.player.art.paintable)))
    mark = len(COMMANDS)
    click(play_pause_button(mini))
    settle(1.0)
    check('S2j pause sends quiet', 'quiet' in [c[0] for c in mutating(mark)] and next_model.room_state(snapshot()) != 'playing', repr(mutating(mark)))
    pump(.8)
    mark = len(COMMANDS)
    click(play_pause_button(mini))
    settle(1.2)
    check('S2k play resumes', [c[0] for c in mutating(mark)][:1] in (['resume'], ['enter']) and next_model.room_state(snapshot()) == 'playing', repr(mutating(mark)))
    mark = len(COMMANDS)
    one(mini, 'Room volume').set_value(35)
    settle(1.0)
    check('S2l the mini slider sets room volume 35 on the controller', ('room_volume', {'value': 35.0}) in mutating(mark)
          and snapshot().get('room_volume') == 35, repr((mutating(mark), snapshot().get('room_volume'))))
    mark = len(COMMANDS)
    button, popover = open_popover(mini, 'Timer')
    click(button_with_text(popover, 'Break 10 min'))
    settle(1.0)
    check('S2m the mini timer starts a 10 minute Break (quiet, minutes 10)', ('quiet', {'minutes': 10}) in mutating(mark), repr(mutating(mark)))
    close_popover(button)
    pump(.5)
    check('S2n a running Break shows as a timer label on the mini player', any('Break' in t for t in texts(mini)), repr(texts(mini)))
    mark = len(COMMANDS)
    controller.resume() if hasattr(controller, 'resume') else None
    settle(.8)
    click(one(mini, 'Open the full window'))
    pump(1.0)
    check('S2o Expand closes the mini window and shows the main window', not minis() and window.get_visible(), repr((minis(), window.get_visible())))

    # --- Up / Down in the mini window
    accel(window, '<Control>m')
    pump(.8)
    mini = window.mini_window
    base = snapshot().get('room_volume')
    mark = len(COMMANDS)
    key(mini, Gdk.KEY_Up)
    settle(.8)
    check('S2p Up in the mini window raises room volume by 5', snapshot().get('room_volume') == min(100, base + 5), repr((base, snapshot().get('room_volume'))))
    key(mini, Gdk.KEY_space)
    settle(.8)
    check('S2q Space in the mini window pauses', next_model.room_state(snapshot()) != 'playing')
    key(mini, Gdk.KEY_space)
    settle(1.0)
    key(mini, Gdk.KEY_Escape)
    pump(.6)
    check('S2r Esc closes the mini window', not minis())
    leave_all()


# ====================================================================== S3 narrow layout and breakpoint
def s3_breakpoint():
    last_room_recorded('midnight-library')
    window = launch(1200, 800)
    results = {}
    def width_to(target):
        window.set_default_size(target, 700)
        pump(1.0)
        for _ in range(3):   # compensate the frame difference on bare Xvfb, then verify
            if window.get_width() == target:
                break
            window.set_default_size(target + (target - window.get_width()), 700)
            pump(1.0)
        return window.get_width() == target
    for width in (561, 560, 559, 420):
        exact = width_to(width)
        note('S3 asked for %d px, window is %d px wide (exact=%s)' % (width, window.get_width(), exact))
        mini = window.mini_layout
        page = window.current_page() if hasattr(window, 'current_page') else '?'
        results[width] = (window.get_width(), mini, page)
    note('S3 widths -> (actual, mini_layout, page): %r' % results)
    check('S3a at 561 px the normal window shows (grid columns, not the mini layout)', results[561][1] is False, repr(results))
    check('S3b "from 560 to 999 px: two or three columns" so exactly 560 px is NOT the mini layout', results[560][1] is False, repr(results))
    check('S3c below 560 px (559) the mini layout shows', results[559][1] is True, repr(results))
    check('S3d at 420 px the mini layout shows', results[420][1] is True and results[420][2] == 'mini', repr(results))
    # at 420 with a playing room: controls, no clipping, no horizontal scroll
    enter_room('midnight-library')
    pump(1.2)
    window.set_default_size(420, 700)
    pump(1.2)
    player = window.mini_player
    check('S3e at 420 px the mini layout has pause, room volume, timer and expand', all(one(window, n) for n in ('Pause the room', 'Room volume', 'Timer')) and one(window, 'Open the full window'),
          repr([acc_name(w) for w in controls(window)]))
    check('S3f at 420 px no control is clipped and nothing scrolls sideways', not clipped_controls(window) and not full_page_scrollbars(window),
          repr((clipped_controls(window), full_page_scrollbars(window))))
    minimum = window.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
    check('S3g the window can be that narrow (minimum width <= 420)', minimum <= 420, 'minimum=%s' % minimum)
    mark = len(COMMANDS)
    wake(window)
    click(one(window, 'Open the full window'))
    pump(1.5)
    check('S3h Expand at narrow width widens the window to the normal layout', window.mini_layout is False and window.get_width() > 560,
          repr((window.get_width(), window.mini_layout)))
    window.set_default_size(420, 700)
    pump(1.2)
    check('S3i back at 420 px the mini layout returns', window.mini_layout is True)
    window.set_default_size(1200, 800)
    pump(1.2)
    check('S3j widening past 560 px leaves the mini layout', window.mini_layout is False)
    # no autoplay when launching narrow with a last room
    leave_all()
    last_room_recorded('after-hours')
    mark, calls = len(COMMANDS), len(backend.calls)
    window = launch(1200, 800)
    window.set_default_size(420, 700)
    pump(1.5)
    check('S3k launching narrow with a last room offers Return and plays nothing', button_with_text(window, 'Return', exact=False) is not None, repr(texts(window)))
    assert_inert('S3l narrow launch starts nothing', mark, calls)
    leave_all()


# ====================================================================== S4 launch delivery (--mini, --desk, --inside, --preview-link)
def s4_delivery():
    leave_all()
    last_room_recorded('midnight-library')
    # first launch --mini: the mini player alone
    close_windows()
    a = app()
    rc = None
    a.window = None
    a_args = ['listening-room', '--desk']
    # drive the same path do_command_line takes for a fresh launch
    window = next_window.NextWindow(a)
    a.window = window
    window.open_mini()
    pump(1.0)
    check('S4a --mini / --desk opens the mini player; the main window stays hidden', len(minis()) == 1 and not window.get_visible(),
          repr((minis(), window.get_visible())))
    # a second launch is delivered to the running window
    rc, err = deliver('--mini')
    check('S4b a second --mini is delivered (exit 0) without opening the full window', rc == 0 and len(minis()) == 1 and not window.get_visible(),
          repr((rc, err[-200:], len(minis()), window.get_visible())))
    rc, err = deliver('--desk')
    check('S4c a second --desk behaves as --mini', rc == 0 and len(minis()) == 1 and not window.get_visible(), repr((rc, err[-200:], window.get_visible())))
    mark, calls = len(COMMANDS), len(backend.calls)
    rc, err = deliver('--preview-link', 'https://example.org/live.mp3')
    pump(1.0)
    link_open = any(isinstance(w, Adw.Dialog) and w.get_mapped() for w in walk(window)) or any('example.org' in t for t in texts(window))
    check('S4d a second --preview-link reaches the running window and opens the link sheet', rc == 0 and link_open, repr((rc, err[-200:], texts(window)[:8])))
    assert_inert('S4e a delivered link starts nothing', mark, calls)
    rc, err = deliver('listening-room://open?url=https%3A%2F%2Fexample.org%2Fother.mp3')
    check('S4f a second listening-room://open URI is delivered', rc == 0, repr((rc, err[-200:])))
    for m in minis():
        m.close()
    pump(.6)
    close_windows()

    # --inside with a playing room goes to Now; without one it stays on Rooms
    enter_room('midnight-library')
    window = launch(1200, 800)
    goto(window, 'rooms')
    pump(.5)
    rc, err = deliver('--inside')
    check('S4g a second --inside shows Now while a room plays', rc == 0 and on_now(window), repr((rc, err[-200:])))
    # a second --mini while the full window is visible keeps the full window and adds the mini
    rc, err = deliver('--mini')
    check('S4h a second --mini with the full window visible adds the mini player', rc == 0 and len(minis()) == 1 and window.get_visible(), repr((rc, len(minis()))))
    for m in minis():
        m.close()
    leave_all()


# ====================================================================== S5 Waybar popover
def s5_popover():
    leave_all()
    last_room_recorded('midnight-library')
    launched = []
    host = next_mini.PopoverHost(app(), request, launch=lambda arguments: launched.append(arguments))
    pump(1.5)
    win = host.window
    mark, calls = len(COMMANDS), len(backend.calls)
    check('S5a the popover is the mini component (same class as the main window\'s mini layout)',
          type(win.player) is next_mini.MiniPlayer and win.player.extras is True and type(mains()[0].mini_player if mains() else win.player) is next_mini.MiniPlayer)
    check('S5b with a last room and nothing playing the popover offers Return and no timers or Leave', button_with_text(win, 'Return', exact=False) is not None
          and not button_with_text(win, 'Leave room'), repr(texts(win)))
    assert_inert('S5c opening the popover with a recorded room starts nothing', mark, calls)
    click(button_with_text(win, 'Return', exact=False))
    settle(1.5)
    pump(1.2)
    check('S5d Return from the popover starts the room', snapshot().get('active') is not None and 'return' in [c[0] for c in COMMANDS[mark:]], repr(COMMANDS[mark:][:4]))
    check('S5e the popover shows Break 10 min, Wind down 30 min and Leave room', all(button_with_text(win, t) for t in ('Break 10 min', 'Wind down 30 min', 'Leave room')),
          repr(texts(win)))
    check('S5f the popover has pause, room volume and expand', play_pause_button(win) and one(win, 'Room volume') and one(win, 'Open the room window'),
          repr([acc_name(w) for w in controls(win)]))
    mark = len(COMMANDS)
    click(button_with_text(win, 'Break 10 min'))
    settle(1.0)
    check('S5g Break 10 min sends quiet with minutes 10', ('quiet', {'minutes': 10}) in mutating(mark), repr(mutating(mark)))
    resume = [c for c in COMMANDS if c[0] == 'resume']
    controller.resume() if hasattr(controller, 'resume') else None
    settle(.8)
    mark = len(COMMANDS)
    click(button_with_text(win, 'Wind down 30 min'))
    settle(1.0)
    check('S5h Wind down 30 min sends wind_down with minutes 30', ('wind_down', {'minutes': 30}) in mutating(mark), repr(mutating(mark)))
    mark = len(COMMANDS)
    click(button_with_text(win, 'Leave room'))
    settle(2.0)
    check('S5i Leave room sends leave and the session ends', 'leave' in [c[0] for c in mutating(mark)] and snapshot().get('active') is None, repr((mutating(mark), snapshot().get('active'))))
    mark = len(COMMANDS)
    click(one(win, 'Open the room window')) if one(win, 'Open the room window') else None
    pump(.6)
    check('S5j Expand starts listening-room (no autoplay args) and closes the popover', launched in ([[]], [['--inside']]) and host.closed, repr((launched, host.closed)))

    # Esc closes it; deactivation closes it
    host2 = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
    pump(1.0)
    key(host2.window, Gdk.KEY_Escape)
    pump(.6)
    check('S5k Esc closes the popover', host2.closed)
    host3 = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
    pump(.8)
    host3.window.was_active = True
    host3.window.is_active = lambda: False   # no window manager here: say the window lost focus
    host3.window.notify('is-active')
    pump(.6)
    check('S5l losing focus closes the popover', host3.closed or not host3.window.get_visible())
    # ORIGINAL oracle, kept visible but no longer counted (revision R-src1, acceptance author, WP4): it passed on the
    # word "run_popover" anywhere in roomctl.py. Superseded by S5m-R1, which runs the verb.
    print('SUPERSEDED [ORIGINAL, superseded by S5m-R1] %s S5m listening-roomctl popover runs next_mini.run_popover: source text, not behaviour'
          % ('PASS' if 'run_popover' in Path(roomctl.__file__).read_text() else 'FAIL'), flush=True)
    popovers, sent = [], len(COMMANDS)
    with patch.object(next_mini, 'run_popover', side_effect=lambda argv, *a, **k: popovers.append(list(argv)) or 0), \
            patch.object(sys, 'argv', ['listening-roomctl', 'popover']):
        status = roomctl.main()
    check('S5m-R1 `listening-roomctl popover` opens the popover through next_mini.run_popover, returns its status and sends no command',
          len(popovers) == 1 and status == 0 and len(COMMANDS) == sent, repr((popovers, status, COMMANDS[sent:])))
    # enter then verify the popover while playing issues no start on open
    enter_room('night-drive')
    mark, calls = len(COMMANDS), len(backend.calls)
    host4 = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
    pump(1.5)
    assert_inert('S5n opening the popover over a playing room changes nothing', mark, calls)
    host4.window.close()
    pump(.4)
    leave_all()


# ====================================================================== S6 room volume agreement
def s6_volume():
    leave_all()
    enter_room('midnight-library')
    window = launch(1200, 800)
    accel(window, '<Control>m')
    pump(1.0)
    mini = window.mini_window
    one(mini, 'Room volume').set_value(37)
    settle(1.2)
    import session_service as ss
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        with patch.object(sys, 'argv', ['listening-roomctl', 'volume']):
            roomctl.main()
    mpris = ss.mpris_values(snapshot())['Volume']
    check('S6a mini slider, roomctl and MPRIS agree on 37', printed.getvalue().strip() == '37' and abs(mpris - .37) < .005 and snapshot()['room_volume'] == 37,
          repr((printed.getvalue(), mpris, snapshot()['room_volume'])))
    # Waybar scroll commands
    for verb, expect in (('+5', 42), ('-5', 37), ('-5', 32)):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(sys, 'argv', ['listening-roomctl', 'volume', verb]):
            roomctl.main()
        settle(.4)
        check('S6b Waybar scroll `volume %s` gives %d everywhere' % (verb, expect), snapshot()['room_volume'] == expect and abs(ss.mpris_values(snapshot())['Volume'] - expect / 100) < .005,
              repr(snapshot()['room_volume']))
    pump(1.0)
    check('S6c the mini slider follows a change made elsewhere', round(one(mini, 'Room volume').get_value()) == 32, repr(one(mini, 'Room volume').get_value()))
    host = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
    pump(1.2)
    check('S6d the popover slider shows the same 32', round(one(host.window, 'Room volume').get_value()) == 32)
    one(host.window, 'Room volume').set_value(61)
    settle(1.0)
    check('S6e the popover sets it for MPRIS and roomctl too', snapshot()['room_volume'] == 61 and abs(ss.mpris_values(snapshot())['Volume'] - .61) < .005)
    now_slider = None
    goto(window, 'now')
    pump(1.2)
    now_slider = one(window, 'Room volume')
    check('S6f Now shows 61 as well', now_slider is not None and round(now_slider.get_value()) == 61, repr(now_slider and now_slider.get_value()))
    snap = snapshot()
    waybar = roomctl.status(snap)
    note('S6 Waybar status JSON carries no volume field: %r' % list(waybar))
    host.window.close()
    for m in minis():
        m.close()
    leave_all()


# ====================================================================== S7 contrast, all eight rooms
def s7_contrast():
    worst = {}
    for room in ROOMS:
        leave_all()
        enter_room(room)
        # Now at 1200x800
        window = launch(1200, 800)
        goto(window, 'now')
        pump(1.5)
        wake(window)
        body = [w for w in text_labels(window) if not in_mix(w) and not any(isinstance(p, Adw.HeaderBar) for p in ancestors(w))]
        res = contrast_of(window, body)
        worst[('now', room)] = min([c for _, c in res] or [0])
        check('S7 Now %s: every text >= 4.5:1 (%d labels)' % (room, len(res)), bool(res) and all(c >= 4.5 for _, c in res), repr([r for r in res if r[1] < 4.5] or res[:3]))
        # mini player window
        accel(window, '<Control>m')
        pump(1.2)
        mini = window.mini_window
        res = contrast_of(mini, text_labels(mini))
        worst[('mini', room)] = min([c for _, c in res] or [0])
        check('S7 mini window %s: every text >= 4.5:1 (%d labels)' % (room, len(res)), bool(res) and all(c >= 4.5 for _, c in res), repr([r for r in res if r[1] < 4.5] or res[:3]))
        screenshot(mini, 'o5-mini-%s' % room)
        mini.close()
        pump(.4)
        # narrow mini layout at 420
        window.set_default_size(420, 700)
        pump(1.5)
        wake(window)
        res = contrast_of(window, text_labels(window))
        worst[('narrow', room)] = min([c for _, c in res] or [0])
        check('S7 420 px mini layout %s: every text >= 4.5:1 (%d labels)' % (room, len(res)), bool(res) and all(c >= 4.5 for _, c in res), repr([r for r in res if r[1] < 4.5] or res[:3]))
        # popover
        host = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
        pump(1.5)
        res = contrast_of(host.window, text_labels(host.window))
        worst[('popover', room)] = min([c for _, c in res] or [0])
        check('S7 Waybar popover %s: every text >= 4.5:1 (%d labels)' % (room, len(res)), bool(res) and all(c >= 4.5 for _, c in res), repr([r for r in res if r[1] < 4.5] or res[:3]))
        host.window.close()
        pump(.3)
        # Rooms Return hero
        controller.leave(0)
        settle(1.0)
        window.set_default_size(1200, 800)
        pump(1.2)
        goto(window, 'rooms')
        pump(1.5)
        wake(window)
        hero = [w for w in text_labels(window) if not any(isinstance(p, Adw.HeaderBar) for p in ancestors(w))]
        hero = [w for w in hero if not any(isinstance(p, Gtk.FlowBox) for p in ancestors(w))]   # the hero strip, not the cards
        res = contrast_of(window, hero)
        worst[('hero', room)] = min([c for _, c in res] or [0])
        check('S7 Rooms hero (last room %s): every text >= 4.5:1 (%d labels)' % (room, len(res)), bool(res) and all(c >= 4.5 for _, c in res), repr([r for r in res if r[1] < 4.5] or res[:3]))
        if room == ROOMS[0]:   # the grid shows all eight rooms at once; measure it once
            cards = [w for w in text_labels(window) if any(isinstance(p, Gtk.FlowBox) for p in ancestors(w))]
            visible = [w for w in cards if (lambda ok, r: ok and r.get_y() + r.get_height() < window.get_height() - 4)(*w.compute_bounds(window))]
            res = contrast_of(window, visible)
            check('S7 Rooms grid cards: text over the art >= 4.5:1 (%d labels in view)' % len(res), bool(res) and all(c >= 4.5 for _, c in res), repr([r for r in res if r[1] < 4.5] or res[:3]))
    note('S7 worst ratios: %r' % {'%s/%s' % k: v for k, v in sorted(worst.items(), key=lambda kv: kv[1])[:8]})
    leave_all()


# ====================================================================== S8 focus ring over art
def ring(window, widget, name):
    """Keyboard focus on the widget must change pixels around its edge by >= 3:1 against the unfocused look."""
    window.set_focus_visible(True)
    window.set_focus(None)
    pump(.4)
    base = grab(window)
    widget.grab_focus()
    window.set_focus_visible(True)
    pump(.5)
    focused = grab(window)
    ok, rect = widget.compute_bounds(window)
    if not ok:
        return None
    pad = 6
    x0, y0 = max(0, int(rect.get_x()) - pad), max(0, int(rect.get_y()) - pad)
    x1, y1 = min(window.get_width(), int(rect.get_x() + rect.get_width()) + pad), min(window.get_height(), int(rect.get_y() + rect.get_height()) + pad)
    changed, ratios = 0, []
    for y in range(y0, y1):
        for x in range(x0, x1):
            a, b = base.getpixel((x, y)), focused.getpixel((x, y))
            if max(abs(i - j) for i, j in zip(a, b)) > 24:
                changed += 1
                ratios.append(contrast_ratio(a, b))
    perimeter = 2 * (x1 - x0 + y1 - y0)
    ratios.sort()
    median = ratios[len(ratios) // 2] if ratios else 0
    return changed, perimeter, round(median, 2)


def s8_focus():
    service.execute('settings', {'values': {'keep_controls': True}})   # so the idle fade cannot hide what is being measured
    settle(.6)
    for room in ('midnight-library', 'atomic-lounge', 'velvet-circuit'):
        leave_all()
        enter_room(room)
        window = launch(1200, 800)
        goto(window, 'now')
        pump(1.5)
        wake(window)
        targets = [('Pause the room', 'Pause'), ('Mix', 'Mix'), ('Room volume', 'Room volume'), ('Output', 'Output'), ('Timer', 'Timer'), ('Main menu', 'Menu')]
        for name, label_ in targets:
            widget = one(window, name)
            if widget is None:
                check('S8 Now %s: %s control exists' % (room, label_), False, repr([acc_name(w) for w in controls(window)]))
                continue
            result = ring(window, widget, name)
            ok = result is not None and result[0] >= result[1] and result[2] >= 3.0
            check('S8 Now %s: focus ring on %s is visible (>=3:1; changed px, perimeter, median)' % (room, label_), ok, repr(result))
        accel(window, '<Control>m')
        pump(1.2)
        mini = window.mini_window
        for name in ('Pause the room', 'Open the full window', 'Timer', 'Room volume'):
            widget = one(mini, name)
            if widget is not None:
                result = ring(mini, widget, name)
                check('S8 mini %s: focus ring on %s is visible' % (room, name), result is not None and result[0] >= result[1] and result[2] >= 3.0, repr(result))
        mini.close()
        pump(.4)
        controller.leave(0)
        settle(1.0)
        goto(window, 'rooms')
        pump(1.5)
        wake(window)
        hero = next((w for w in walk(window) if isinstance(w, Gtk.Button) and shown(w) and any(t.startswith('Return to ') for t in texts(w))), None)
        if hero is not None:
            result = ring(window, hero, 'Return')
            check('S8 Rooms %s: focus ring on the Return button is visible' % room, result is not None and result[0] >= result[1] and result[2] >= 3.0, repr(result))
        else:
            check('S8 Rooms %s: a Return hero button exists' % room, False, repr(texts(window)[:8]))
    service.execute('settings', {'values': {'keep_controls': False}})
    settle(.4)
    leave_all()


# ====================================================================== S9 keyboard map and focus order
def s9_keys():
    leave_all()
    enter_room('midnight-library')
    window = launch(1200, 800)
    for accelerator, label_ in (('<Control>1', 'Rooms'), ('<Control>2', 'Listen'), ('<Control>3', 'Now'), ('<Control>f', 'Search'),
                                ('<Control>l', 'Add a link'), ('<Control>v', 'Add a link from the clipboard'), ('<Control>m', 'Mini player'),
                                ('<Control>comma', 'Preferences'), ('<Control>question', 'Shortcuts')):
        names = window.get_application().get_actions_for_accel(accelerator)
        bound = bool(names and names[0].startswith('win.')) or shortcut_bound(window, accelerator) is not None
        check('S9a %s (%s) is bound to a window action or shortcut' % (accelerator, label_), bound, repr(names))
    window.activate_action('win.shortcuts', None)
    pump(.8)
    dialog_text = ' | '.join(texts(window) + [getattr(w, 'get_body', lambda: '')() or '' for w in walk(window) if isinstance(w, Adw.AlertDialog)])
    for key_name in ('Space', 'Ctrl+1', 'Ctrl+2', 'Ctrl+3', 'Ctrl+F', 'Ctrl+L', 'Ctrl+V', 'Ctrl+M', 'Ctrl+,', 'Esc', 'Up / Down', 'Ctrl+?'):
        check('S9b the shortcuts dialog lists %s' % key_name, key_name in dialog_text, dialog_text[:200])
    for w in walk(window):
        if isinstance(w, Adw.AlertDialog):
            w.close()
    pump(.5)
    # Esc: sheet first, then browsing
    goto(window, 'now')
    pump(1.0)
    open_mix(window)
    key(window, Gdk.KEY_Escape)
    pump(1.0)
    check('S9c Esc closes Mix first', not mix_open(window) and on_now(window))
    key(window, Gdk.KEY_Escape)
    pump(1.0)
    check('S9d a second Esc returns to browsing (Rooms)', not on_now(window))
    # Ctrl+F focuses search; Ctrl+L opens the link sheet; Ctrl+V outside entries too
    shortcut(window, '<Control>f')
    pump(.8)
    focus = window.get_focus()
    check('S9e Ctrl+F puts focus in the search entry', isinstance(focus, (Gtk.Text, Gtk.SearchEntry, Gtk.Editable)) or isinstance(getattr(focus, 'get_parent', lambda: None)(), Gtk.SearchEntry), repr(focus))
    window.set_focus(None)
    mark, calls = len(COMMANDS), len(backend.calls)
    shortcut(window, '<Control>l')
    pump(1.0)
    dialogs = [w for w in walk(window) if isinstance(w, Adw.Dialog) and w.get_mapped()]
    check('S9f Ctrl+L opens the Add a link dialog', bool(dialogs), repr(dialogs))
    for d in dialogs:
        d.close()
    pump(.5)
    Gdk.Display.get_default().get_clipboard().set('https://example.org/a.mp3')
    pump(.4)
    shortcut(window, '<Control>v')
    pump(1.5)
    dialogs = [w for w in walk(window) if isinstance(w, Adw.Dialog) and w.get_mapped()]
    if dialogs:
        check('S9g Ctrl+V outside an entry with a link on the clipboard opens the link sheet', True, repr(dialogs))
    else:
        note('S9g Ctrl+V with a clipboard link opened nothing here (clipboard reads are unreliable on bare Xvfb); O3 covers paste')
    for d in dialogs:
        d.close()
    pump(.5)
    assert_inert('S9h none of those keys started anything', mark, calls)
    # Tab reaches the primary controls on Now, Rooms and the mini layout
    for page, wanted in (('now', ('Pause the room', 'Mix', 'Room volume')), ('rooms', ())):
        goto(window, page)
        pump(1.0)
        wake(window)
        window.set_focus(None)
        seen = []
        for _ in range(60):
            window.child_focus(Gtk.DirectionType.TAB_FORWARD)
            pump(.05)
            f = window.get_focus()
            if f is None:
                continue
            name = None
            for w in [f] + list(ancestors(f))[:3]:
                name = acc_name(w) or name
            seen.append(name or type(f).__name__)
        check('S9i Tab on %s reaches its controls %r' % (page, wanted), all(n in seen for n in wanted) and len(set(seen)) >= 4, repr(seen[:40]))
    # Ctrl+W
    names = window.get_application().get_actions_for_accel('<Control>w')
    note('S9 Ctrl+W bound to %r (the classic window had it; the plan keyboard map does not list it)' % (names,))
    leave_all()


# ====================================================================== S10 adaptive widths
def s10_widths():
    enter_room('midnight-library')
    for width, height in ((1200, 800), (800, 700), (420, 800)):
        for page in ('rooms', 'listen', 'now'):
            window = launch(width, height)
            if window.mini_layout and page != 'now':
                # below 560 every page is the mini layout
                pass
            goto(window, page)
            pump(1.2)
            wake(window)
            check('S10 %dx%d %s: no clipped control and no sideways scroll' % (width, height, page),
                  not clipped_controls(window) and not full_page_scrollbars(window), repr((clipped_controls(window)[:4], full_page_scrollbars(window)[:2])))
            if width == 420:
                check('S10 420 px %s is the mini layout' % page, window.mini_layout is True and window.current_page() == 'mini', repr((window.mini_layout, window.current_page())))
            minimum = window.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            check('S10 %dx%d %s: minimum width %d fits' % (width, height, page, minimum), minimum <= width, repr(minimum))
        if width >= 800:
            window = launch(width, height)
            goto(window, 'rooms')
            pump(1.2)
            cards = [w for w in walk(window) if isinstance(w, Gtk.FlowBoxChild) and shown(w)]
            xs = sorted({round(w.compute_bounds(window)[1].get_x()) for w in cards})
            columns = len([x for x in xs if True])
            note('S10 %d px Rooms grid distinct card x positions = %d' % (width, columns))
            expected = (4,) if width >= 1000 else (2, 3)
            check('S10 %d px Rooms grid has %s columns' % (width, '/'.join(map(str, expected))), columns in expected, repr(xs))
    leave_all()


# ====================================================================== S11 reduced motion
def s11_reduced_motion():
    service.execute('settings', {'values': {'reduced_motion': True, 'keep_controls': False}})
    settle(1.0)
    mark_calls = len(backend.calls)
    enter_room('midnight-library')
    pictures = [c[1] for c in backend.calls[mark_calls:] if c[0] == 'start_picture']
    check('S11g reduced motion: the Scene window starts as still artwork (picture still=True)', bool(pictures) and all(p.get('still') is True for p in pictures), repr(pictures))
    window = launch(1200, 800)
    goto(window, 'now')
    pump(1.0)
    wake(window)
    pump(6.5)
    check('S11a reduced motion: no idle fade in Now after 6.5 s', window.controls.get_opacity() == 1 and not window.fades(), repr(window.controls.get_opacity()))
    check('S11b reduced motion: no video widget and no playing media stream', not [w for w in walk(window) if isinstance(w, Gtk.Video)]
          and not [w for w in walk(window) if isinstance(w, Gtk.Picture) and isinstance(w.get_paintable(), Gtk.MediaStream)])
    check('S11c reduced motion turns GTK animations off for the whole app', Gtk.Settings.get_default().get_property('gtk-enable-animations') is False)
    accel(window, '<Control>m')
    pump(1.0)
    mini = window.mini_window
    check('S11d the mini player has no video and no spinner, only still art', not [w for w in walk(mini) if isinstance(w, (Gtk.Video, Gtk.Spinner))]
          and not isinstance(mini.player.art.paintable, Gtk.MediaStream))
    mini.close()
    pump(.4)
    host = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
    pump(1.0)
    check('S11e the popover has no video and no spinner', not [w for w in walk(host.window) if isinstance(w, (Gtk.Video, Gtk.Spinner))])
    host.window.close()
    pump(.3)
    window.set_default_size(420, 700)
    pump(1.2)
    moving = [describe(w)[:120] for w in walk(window) if isinstance(w, (Gtk.Video, Gtk.Spinner))] \
        + [describe(w)[:120] for w in walk(window) if isinstance(w, Gtk.Picture) and isinstance(w.get_paintable(), Gtk.MediaStream)]
    spinning = [describe(w)[:120] for w in walk(window) if isinstance(w, Gtk.Spinner) and shown(w) and w.get_spinning()]
    check('S11f the narrow mini layout shows no moving picture or spinning spinner either', not spinning
          and not [w for w in walk(window) if isinstance(w, Gtk.Picture) and shown(w) and isinstance(w.get_paintable(), Gtk.MediaStream)], repr((moving, spinning)))
    service.execute('settings', {'values': {'reduced_motion': False}})
    settle(.8)
    leave_all()


# ====================================================================== S12 no autoplay anywhere in the new surfaces
def s12_no_autoplay():
    last_room_recorded('midnight-library')
    mark, calls = len(COMMANDS), len(backend.calls)
    window = launch(1200, 800)
    pump(1.0)
    accel(window, '<Control>m')
    pump(1.0)
    host = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
    pump(1.5)
    window.set_default_size(420, 700)
    pump(1.2)
    window.set_default_size(1200, 800)
    pump(1.0)
    for m in minis():
        for w in walk(m):
            if isinstance(w, Gtk.Scale):
                w.grab_focus()
        pump(.4)
    host.window.close()
    for m in minis():
        m.close()
    pump(.6)
    assert_inert('S12 mini player, popover, narrow layout, Ctrl+M and a recorded last room send no start command', mark, calls)
    leave_all()


# ====================================================================== S13 parity oracles (criterion 13)
# ORIGINAL (revision R-src1, acceptance author, WP4): the S13 PARITY checks read source text, so code placement
# alone satisfied or broke them. They still run and print as SUPERSEDED lines with their result but are no longer
# counted; S17 (R3) and its R-src1 additions S17s-S17v drive the same guarantees in the running window. The orphan
# check below stays counted: dead code has no behaviour to observe, so source analysis is its only faithful form.
GUI_MODULES = sorted(p for p in SOURCE.glob('next_*.py'))
GUI_SOURCE = {p.name: p.read_text() for p in GUI_MODULES}


def gui_has(pattern, exclude=()):
    return [n for n, t in GUI_SOURCE.items() if n not in exclude and re.search(pattern, t)]


def original(by, name, ok, detail=''):
    """A superseded source-text oracle: printed with its result, never counted (revision R-src1, acceptance author, WP4)."""
    print('SUPERSEDED [ORIGINAL, superseded by %s] %s %s: source text, not behaviour%s'
          % (by, 'PASS' if ok else 'FAIL', name, (' (' + detail + ')') if detail and not ok else ''), flush=True)


def s13_parity():
    # Music Assistant (modern-radio contract: library/player routes, selected remote player; runbook: browse, players, play, radio mode, transfer)
    found = gui_has(r'browse_music_assistant|ma_players|ma_play|ma_transfer|["\']transfer["\']')
    original('S17e-S17h', 'PARITY Music Assistant: the window can load players, browse or search, play on a speaker and transfer a queue', bool(found), repr(found))
    # ListenBrainz and local history
    found = gui_has(r'listenbrainz_history|listenbrainz_recommendations|\bbookmark\b|\bremembered\b|discovery_links')
    original('S17i-S17k, S17s', 'PARITY ListenBrainz and local history: Remember playing, Show remembered, history and recommendations are reachable', bool(found), repr(found))
    # Twitch follows review
    found = gui_has(r'import_following')
    original('S17n-S17r', 'PARITY Twitch: Review my follows (explicit import for review) is reachable', bool(found), repr(found))
    # AudioMuse generation
    found = gui_has(r'audiomuse', exclude=('next_prefs.py',))
    original('S17t', 'PARITY AudioMuse: a station can be made from an AudioMuse mix (generation kind audiomuse)', bool(found), repr(found))
    # Range preference
    found = gui_has(r'stay_close')
    original('S17m, S17u', 'PARITY Station range (stay_close, in the runbook list "energy, familiarity, speech and range") is settable', bool(found), repr(found))
    # Jellyfin library kinds: albums, playlists, recent, tracks
    kinds = set(re.findall(r"browse_jellyfin\([^)]*kind=['\"](\w+)['\"]", GUI_SOURCE['next_listen.py']))
    original('S17a', 'PARITY Jellyfin library browses albums, playlists, recently added albums and tracks (runbook); next_listen browses %r' % sorted(kinds),
          {'albums', 'playlists', 'recent', 'tracks'} <= kinds or {'playlists', 'tracks'} <= kinds, repr(kinds))
    # Track details and Play next are reachable only from an Audio row, which a library that lists albums never yields
    original('S17b-S17d', 'PARITY Track details / Play next are reachable: Listen can produce an Audio (track) row from the real Jellyfin browse',
          'tracks' in kinds or bool(gui_has(r'parent_id')), 'browse kinds=%r; no parent_id (album children) use in next_*: %r' % (sorted(kinds), gui_has(r'parent_id')))
    # UI copy must not promise what the window cannot do
    notes = GUI_SOURCE['next_prefs.py']
    promises = [phrase for phrase in ('play on a speaker it controls', 'Browse its library and play on a speaker', 'Read your listening history and recommendations on request',
                                      'adds discovery from the channels you follow')
                if phrase in notes]
    gui_paths = {'play on a speaker it controls': 'ma_play', 'Browse its library and play on a speaker': 'ma_play',
                 'Read your listening history and recommendations on request': 'listenbrainz_history',
                 'adds discovery from the channels you follow': 'import_following'}
    broken = [p for p in promises if not gui_has(gui_paths[p], exclude=('next_prefs.py',))]
    original('S17v', 'PARITY Connections copy only promises what the window can do (no dead promises)', not broken, repr(broken))
    chips = re.findall(r"'(Plays on speakers|History)'", notes)
    original('S17v', 'PARITY the capability chips "Plays on speakers" and "History" correspond to a GUI route', not chips or bool(gui_has(r'ma_play|listenbrainz_history')), repr(chips))
    # Orphaned controller/service code after the removal: functions with no caller outside online_services and tests
    import ast
    online = (SOURCE / 'online_services.py').read_text()
    functions = {n.name for n in ast.parse(online).body if isinstance(n, ast.FunctionDef) and not n.name.startswith('_')}
    sources = {p.name: p.read_text() for p in SOURCE.glob('*.py') if p.name != 'online_services.py'}
    # REVISED (R3, repository extraction): the five classic modules as they were at the commit that removed them
    # (original repository ac42f0c) are kept verbatim in tests/fixtures; the standalone repository has no such history.
    removed = (Path(__file__).resolve().parents[1] / 'fixtures' / 'o5-removed-classic-modules.txt').read_text()
    used_before = sorted(f for f in functions if re.search(r'\b%s\(' % f, removed) or re.search(r'\.%s\b' % f, removed))
    orphans = sorted(f for f in used_before if not any(re.search(r'\b%s\b' % f, t) for t in sources.values())
                     and len(re.findall(r'\b%s\(' % f, online)) <= 1)
    note('PARITY online_services functions the classic window called: %r' % used_before)
    note('PARITY ... of which now have no caller anywhere (orphaned by the removal): %r' % orphans)
    check('PARITY nothing in online_services was orphaned by the removal (every function the classic window called still has a caller)', not orphans, repr(orphans))
    # Twitch discovery: ASMR style and Shuffle
    note('PARITY Twitch Live channels discovery: styles used by next_listen: %r; twitch_channels.choose callers: %r'
         % (sorted(set(re.findall(r"discover\(\s*['\"](\w+)['\"]", GUI_SOURCE['next_listen.py']))), gui_has(r'twitch_channels\.choose|\.choose\(\{')))
    # Per-player mute, standalone vista/video, Ctrl+W, station rename
    players = GUI_SOURCE['next_players.py']
    note('PARITY per-player mute in Other players: %s' % ('present' if re.search(r"player.*mute|mute.*player", players, re.I) and 'master=False' in players else 'absent'))
    note('PARITY standalone vista / video with sound outside a room: %s' % ('present' if gui_has(r'ambience\.start') else 'absent'))
    note('PARITY station rename in Station detail: %s' % ('present' if gui_has(r'Adw\.EntryRow\(title=.Name') or gui_has(r"rename.*station|station.*rename") else 'absent'))


# ====================================================================== S14 Other players, system volume, More / Less, Play next
def s14_extras():
    leave_all()
    enter_room('midnight-library')
    window = launch(1200, 800)
    pump(1.0)
    menu_items = [w.get_text() for w in walk(window.menu_popover.get_child()) if isinstance(w, Gtk.Label)]
    wanted = ['Save as my room…', 'Leave room', 'Add a link…', 'Mini player', 'Other players…', 'Preferences…', 'Keyboard shortcuts', 'About']
    check('S14a the menu offers the plan\'s entries', all(w in menu_items for w in wanted), repr(menu_items))
    note('S14 menu order: %r (plan: Save as my room, Add a link, Other players, Mini player, Preferences, Shortcuts, About)' % menu_items)
    mark, calls = len(COMMANDS), len(backend.calls)
    window.open_players()
    pump(1.5)
    dialogs = [w for w in walk(window) if isinstance(w, Adw.Dialog) and w.get_mapped()]
    check('S14b Other players… opens a dialog and sends no start command', bool(dialogs) and not mutating(mark) and not backend.calls[calls:],
          repr((dialogs, mutating(mark))))
    body = ' | '.join(texts(window))
    check('S14c Other players… carries system volume and Mute', 'System volume' in body and 'Mute' in body, body[:300])
    dialog = dialogs[0] if dialogs else None
    mute = next((w for w in walk(window) if isinstance(w, Gtk.ToggleButton) and 'Mute' in ' '.join(texts(w))), None)
    if dialog is not None and mute is not None:
        ok1, frame = dialog.get_child().compute_bounds(window)
        ok2, rect = mute.compute_bounds(window)
        check('S14d the Mute and System volume controls are fully visible in the dialog without scrolling (frame bottom %.0f, Mute bottom %.0f)' % (frame.get_y() + frame.get_height(), rect.get_y() + rect.get_height()),
              ok1 and ok2 and rect.get_y() + rect.get_height() <= frame.get_y() + frame.get_height() + 1,
              repr((frame.get_y() + frame.get_height(), rect.get_y() + rect.get_height())))
    for d in dialogs:
        d.close()
    pump(.4)
    leave_all()


# ====================================================================== S17 parity paths work in the running window (R3)
# REVISED (R3), original S13 kept. The S13 oracles read source text (a literal `browse_jellyfin(kind='...')`,
# `import_following` outside next_prefs.py), so code placement alone could satisfy them. S17 drives the real window
# against fakes that record what is actually requested: toggling the library kinds must reach browse_jellyfin with
# that kind, controls must be clicked as widgets, and every read must stay inert until the named action.
def s17_parity_paths():
    import online_services as online
    import twitch_channels
    cfg = {name: {'endpoint': 'http://fixture.invalid', 'username': 'me', 'user_id': 'uid', 'token_file': ''} for name in online.SERVICES}
    requested, played, moved, marks, history_calls, rec_calls = [], [], [], [], [], []
    driven = {}   # R-src1: which promised capabilities the window really delivered, for S17v

    def record(key, name, ok, detail=''):
        driven[key] = bool(ok)
        check(name, ok, detail)
    jf = lambda kind, **kw: dict({'provider': 'jellyfin', 'id': kind + '-1', 'type': kind, 'artist': 'Band', 'uri': 'jellyfin://x/' + kind}, **kw)
    album, playlist = jf('album', type='MusicAlbum', name='Album Alpha'), jf('playlist', type='Playlist', name='Playlist Beta')
    track = jf('track', type='Audio', name='Track Gamma')
    listings = {'albums': [album], 'playlists': [playlist], 'recent': [dict(album, id='r', name='Fresh Delta')], 'tracks': [track]}

    def browse(query='', kind='albums', parent_id=None, cancel_event=None):
        requested.append(kind)
        return list(listings[kind])
    players = [{'id': 'k', 'name': 'Kitchen', 'available': True, 'state': 'idle', 'volume': 20},
               {'id': 'l', 'name': 'Lounge', 'available': True, 'state': 'idle', 'volume': None}]
    ma = [{'provider': 'music_assistant', 'id': 'm1', 'name': 'Evening Epsilon', 'type': 'playlist', 'artist': '', 'uri': 'library://playlist/1'}]
    with patch.object(online, 'config', return_value=cfg), patch.object(online, 'browse_jellyfin', side_effect=browse), \
         patch.object(online, 'jellyfin_queue', side_effect=lambda item, cancel_event=None: [track]), \
         patch.object(online, 'ma_players', side_effect=lambda cancel_event=None: [dict(x) for x in players]), \
         patch.object(online, 'browse_music_assistant', side_effect=lambda *a, **k: list(ma)), \
         patch.object(online, 'ma_transfer', side_effect=lambda a, b: moved.append((a, b))), \
         patch.object(online, 'remembered', return_value=[{'title': 'Zeta', 'artist': 'Band'}]), \
         patch.object(online, 'bookmark', side_effect=lambda snapshot, note='': marks.append(note) or {}), \
         patch.object(online, 'listenbrainz_history', side_effect=lambda: history_calls.append(1) or [{'track_metadata': {'artist_name': 'Eta', 'track_name': 'Theta'}}]), \
         patch.object(online, 'listenbrainz_recommendations', side_effect=lambda *a, **k: rec_calls.append(1) or [{'url': 'https://musicbrainz.org/recording/00000000-0000-0000-0000-000000000000'}]):
        leave_all()
        window = launch(1200, 800)
        window.show_listen()
        pump(.8)
        page = window.listen
        mark, calls = len(COMMANDS), len(backend.calls)
        body = lambda: ' | '.join(texts(page.index_body))
        page.scopes.set_active(3)
        pump(.8)
        del requested[:]
        for index, kind, wanted in ((1, 'playlists', 'Playlist Beta'), (2, 'recent', 'Fresh Delta'), (3, 'tracks', 'Track Gamma')):
            page.kind_group.set_active(index)
            pump(.9)
            check('S17a Library kind %s: the toggle reaches browse_jellyfin with that kind and the rows show on screen' % kind,
                  kind in requested and wanted in body(), repr((requested, body()[:160])))
        # an Audio row on screen carries a working Track details entry
        audio = next((w for w in walk(page.index_body) if isinstance(w, Gtk.Label) and w.get_text() == 'Track Gamma'), None)
        menu = [t for t, _ in page.kind_entries()[0]['menu']]
        check('S17b the Tracks list offers Track details… and the AudioMuse station entry on a real Audio row', audio is not None and 'Track details…' in menu, repr(menu))
        menu = dict(page.kind_entries()[0]['menu'])   # the Tracks row's entries, kept for S17t
        menu['Track details…']()
        pump(1.2)
        check('S17c Track details… opens the dialog from that row and starts nothing', any(isinstance(w, Adw.Dialog) and w.get_mapped() for w in walk(window))
              and not mutating(mark), repr(mutating(mark)))
        for w in list(walk(window)):
            if isinstance(w, Adw.Dialog) and w.get_mapped():
                w.close()
        pump(.5)
        page.kind_group.set_active(0)
        pump(.6)
        dict(page.library_entries()[0]['menu'])['Show tracks']()
        pump(1.0)
        check('S17d Show tracks on an album lists its tracks (jellyfin_queue) inertly', 'Track Gamma' in body() and not mutating(mark), repr((body()[:120], mutating(mark))))
        # Speakers
        page.more_scopes.set_active(0)
        pump(1.4)
        record('ma_browse', 'S17e Speakers lists the players and Music Assistant music, and browsing sends nothing',
              'Kitchen' in body() and 'Lounge' in body() and 'Evening Epsilon' in body() and not mutating(mark) and not backend.calls[calls:], repr(body()[:200]))
        play = button_with_text(page.index_body, 'Play on Kitchen')
        check('S17f a "Play on Kitchen" button is on screen', play is not None, repr(texts(page.index_body)))
        if play is not None:
            click(play)
            settle(1.0)
            sent = [c for c in COMMANDS[mark:] if c[0] == 'online']
            record('ma_play', 'S17g clicking it sends one online command naming the chosen speaker and the uri', len(sent) == 1 and sent[0][1]['item'].get('player_id') == 'k'
                  and sent[0][1]['item'].get('uri') == 'library://playlist/1', repr(sent))
        use = button_with_text(page.index_body, 'Use')
        if use is not None:
            click(use)
            pump(.5)
        entry = [e for e in page.speaker_entries() if e['name'] == 'Kitchen'][0]
        dict(entry['menu'])["Move Lounge's queue here"]()
        settle(.8)
        check('S17h Move Lounge\'s queue here transfers through Music Assistant', moved == [('l', 'k')], repr(moved))
        # Remembered: reads stay local until asked
        page.more_scopes.set_active(1)
        pump(1.2)
        check('S17i Remembered shows local bookmarks and has read nothing from ListenBrainz', 'Band — Zeta' in body() and not history_calls, repr((body()[:160], history_calls)))
        click(button_with_text(page.memory_box, 'ListenBrainz history'))
        pump(1.2)
        record('lb_history', 'S17j the History button reads ListenBrainz once and shows it', history_calls == [1] and 'Eta — Theta' in body(), repr((history_calls, body()[:160])))
        # S17s (R-src1, supersedes the recommendations half of S13 "PARITY ListenBrainz"): Recommendations is a real route
        recommend = button_with_text(page.memory_box, 'Recommendations')
        if recommend is not None:
            click(recommend)
            pump(1.2)
        record('lb_recs', 'S17s the Recommendations button reads ListenBrainz recommendations once and lists them',
               recommend is not None and rec_calls == [1] and 'Recommended recording' in body(), repr((rec_calls, body()[:160])))
        page.memory_note.set_text('a note')
        click(button_with_text(page.memory_box, 'Remember playing'))
        pump(1.2)
        check('S17k Remember playing stores the note locally', marks == ['a note'], repr(marks))
        check('S17l none of this started playback', not [c for c in mutating(mark) if c[0] not in ('online',)], repr(mutating(mark)))
        # S17t (R-src1, supersedes S13 "PARITY AudioMuse"): the Audio row's AudioMuse entry, taken from the Tracks list
        # above, really stores an AudioMuse station (run after S17l so its station.save does not count against it)
        make, mark2 = menu.get('Make an AudioMuse station'), len(COMMANDS)
        before = {s.get('id') for s in controller.stations()}
        if make is not None:
            make()
            settle(1.0)
        saved = [s for s in controller.stations() if s.get('id') not in before]
        record('audiomuse', 'S17t a Tracks row makes an AudioMuse station: one station.save stores a station with generation kind audiomuse and nothing starts',
               make is not None and [c[0] for c in mutating(mark2)] == ['station.save'] and len(saved) == 1
               and (saved[0].get('generation') or {}).get('kind') == 'audiomuse', repr((sorted(menu), mutating(mark2), saved)))
        page.scopes.set_active(0)
        pump(.4)
        # station detail: Range and Rename as widgets
        stations = controller.stations()
        if not stations:
            service.execute('station.save', {'station': {'name': 'Acceptance station', 'sources': [{'source_id': 'x', 'reference': album}], 'preferences': {}, 'end_policy': 'stop', 'arc': 'none', 'generation': {'kind': 'collection'}}})
            stations = controller.stations()
        if stations:
            page.show_station(stations[0])
            pump(.9)
            check('S17m Station detail shows a Range choice and a Rename button', any('Range' in t for t in texts(page.station_main)) and button_with_text(page.station_main, 'Rename') is not None,
                  repr(texts(page.station_main)[:20]))
            # S17u (R-src1, supersedes S13 "PARITY Station range"): the Range control sets stay_close on the stored station
            ranges = [w for w in named(page.station_main, 'Range') if hasattr(w, 'set_active')]
            target, mark3 = stations[0].get('id'), len(COMMANDS)
            if ranges:
                ranges[0].set_active(2)   # Any, Stay close, Explore
                settle(1.0)
            stored = next((s for s in controller.stations() if s.get('id') == target), {})
            check('S17u choosing Range: Explore in Station detail stores stay_close Explore on that station and starts nothing',
                  len(ranges) == 1 and (stored.get('preferences') or {}).get('stay_close') == 'Explore'
                  and not [c for c in mutating(mark3) if not c[0].startswith('station.')], repr((len(ranges), stored.get('preferences'), mutating(mark3))))
        else:
            note('S17m no station could be created in the harness; Range and Rename not driven')
        window.close()
        pump(.3)
        # Twitch follows, through the Preferences widgets
        window = launch(1200, 800)
        window.open_preferences()
        pump(.9)
        dialog = window.preferences
        seen = list(texts(dialog))   # the Connections overview with its capability chips
        dialog.open_connection('twitch')
        pump(.9)
        review = one(dialog, 'Review my Twitch follows')
        check('S17n the Twitch page has a Review my follows button', review is not None, repr(texts(dialog)[:20]))
        follows = {'status': 'follows', 'message': 'Review', 'complete': True,
                   'channels': [{'login': 'alpha', 'name': 'Alpha', 'url': 'https://www.twitch.tv/alpha'}, {'login': 'beta', 'name': 'Beta', 'url': 'https://www.twitch.tv/beta'}]}
        reads = []
        with patch.object(twitch_channels, 'import_following', side_effect=lambda profile: reads.append(profile) or follows):
            before = twitch_channels.curated_channels('music')
            check('S17o nothing was read from Twitch before the button was pressed', reads == [])
            dialog.follow_name.set_text('me')
            if review is not None:
                click(review)
            pump(1.5)
            record('twitch_follows', 'S17p pressing Review reads the follows once and adds none to a pool', reads == ['me'] and twitch_channels.curated_channels('music') == before, repr((reads, before)))
            ticks = [w for w in walk(dialog) if isinstance(w, Gtk.CheckButton) and shown(w)]
            check('S17q the follows appear as two tick rows', len(ticks) == 2, repr(len(ticks)))
            if len(ticks) == 2:
                ticks[1].set_active(True)
                add = next((w for w in walk(dialog) if isinstance(w, Adw.ButtonRow) and 'Add selected' in w.get_title()), None)
                if add is not None:
                    add.emit('activated')
                    pump(.9)
                check('S17r adding the ticked follow puts only that channel in the pool', twitch_channels.curated_channels('music')[-1:] == ['beta']
                      and 'alpha' not in twitch_channels.curated_channels('music')[len(before):], repr(twitch_channels.curated_channels('music')))
        # S17v (R-src1, supersedes S13 "no dead promises" and "capability chips"): every capability the Connections pages
        # promise on screen was delivered by the window above. Promise wording -> the driven check that proves it.
        seen += texts(dialog)
        for service in ('music_assistant', 'listenbrainz', 'audiomuse'):
            if hasattr(dialog, 'pop_subpage'):
                dialog.pop_subpage()
                pump(.4)
            dialog.open_connection(service)
            pump(.8)
            seen += texts(dialog)
        promises = {'Plays on speakers': 'ma_play', 'play on a speaker': 'ma_play', 'Browse its library': 'ma_browse',
                    'History': 'lb_history', 'listening history': 'lb_history', 'recommendations': 'lb_recs',
                    'channels you follow': 'twitch_follows', 'Station mixes': 'audiomuse', 'from a library track': 'audiomuse'}
        shown_promises = sorted(p for p in promises if any(p.lower() in s.lower() for s in seen))
        dead = [p for p in shown_promises if not driven.get(promises[p])]
        check('S17v the Connections pages promise only what the window delivered (chips and notes each map to a passing driven check)',
              shown_promises and not dead, repr((shown_promises, dead, driven)))
        window.close()
        pump(.3)
    leave_all()


# ====================================================================== S16 vocabulary and capability controls on the new surfaces
def s16_vocabulary():
    leave_all()
    enter_room('midnight-library')
    window = launch(1200, 800)
    pump(1.2)
    check('S16a Now on live radio hides Next track, More like this and Less like this (hidden, not disabled)',
          not any(named(window, n) for n in ('Next track', 'More like this', 'Less like this')), repr([acc_name(w) for w in controls(window)]))
    accel(window, '<Control>m')
    pump(1.0)
    bad = vocabulary_violations(window.mini_window)
    check('S16b the mini player uses none of the retired words', not bad, repr(bad))
    window.mini_window.close()
    pump(.4)
    host = next_mini.PopoverHost(app(), request, launch=lambda arguments: None)
    pump(1.2)
    bad = vocabulary_violations(host.window)
    check('S16c the Waybar popover uses none of the retired words', not bad, repr(bad))
    host.window.close()
    pump(.3)
    window.open_players()
    pump(1.2)
    bad = vocabulary_violations(window)
    check('S16d Other players uses none of the retired words', not bad, repr(bad))
    for w in walk(window):
        if isinstance(w, Adw.Dialog) and w.get_mapped():
            w.close()
    pump(.4)
    window.set_default_size(420, 700)
    pump(1.2)
    bad = vocabulary_violations(window)
    check('S16e the narrow layout uses none of the retired words', not bad, repr(bad))
    leave_all()
    enter_library()
    window = launch(1200, 800)
    goto(window, 'now')
    pump(1.5)
    check('S16f Now on a plain library queue shows Next track but not More/Less like this (no generated station has played)',
          one(window, 'Next track') is not None and not any(named(window, n) for n in ('More like this', 'Less like this')),
          repr([acc_name(w) for w in controls(window)]))
    # keyboard: the Next track button has an accessible name and is a focus target
    skip = one(window, 'Next track')
    check('S16g Next track is keyboard focusable', skip is not None and skip.get_focusable(), repr(skip))
    leave_all()


# ====================================================================== S15 evidence directory (criterion 14)
def s15_evidence():
    evidence = output   # the screenshots this run just wrote
    files = sorted(p.name for p in evidence.glob('*.png')) if evidence.is_dir() else []
    wanted = ['first-run', 'return', 'rooms', 'preview', 'now', 'library', 'mix', 'failure', 'timer', 'search', 'station', 'link', 'connections', 'mini', 'reduced']
    missing = [w for w in wanted if not any(w in f.lower() for f in files)]
    check('S15 criterion 14: the evidence directory holds all fifteen listed screenshots', not missing and len(files) >= 15, 'missing=%r files=%r' % (missing, files))
    # every screenshot is a real, non-blank PNG of the new window
    from PIL import Image
    bad = []
    for f in files:
        image = Image.open(evidence / f).convert('L')
        values = list(image.resize((64, 40)).getdata())
        if max(values) - min(values) < 20:
            bad.append(f)
    check('S15 no evidence screenshot is blank', not bad, repr(bad))


def main():
    build_app()
    selected = [x for x in args.only.split(',') if x]
    for fn in [f for f in (s1_startup, s2_mini, s3_breakpoint, s4_delivery, s5_popover, s6_volume, s7_contrast, s8_focus, s9_keys, s10_widths,
                           s11_reduced_motion, s12_no_autoplay, s13_parity, s14_extras, s15_evidence, s16_vocabulary, s17_parity_paths) if not selected or f.__name__.split('_')[0] in selected or f.__name__ in selected]:
        print('==', fn.__name__, flush=True)
        scenario(fn)
    close_windows()


try:
    main()
except Exception:
    traceback.print_exc()
    check('acceptance run completed', False, traceback.format_exc()[-600:])
finally:
    (output / 'o5-acceptance.json').write_text(json.dumps(RESULTS, indent=2))
    failed = [r for r in RESULTS if not r['pass']]
    print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed')
    for r in failed:
        print('FAILED:', r['criterion'])
    os._exit(1 if failed else 0)
