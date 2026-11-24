"""Exit paths: no Listening Room process ends in a native crash, however it ends.

Each run is one child process on its own private Xvfb and D-Bus (started here, so a run can take
them away), with the o6_env fixtures. Before the exit, scene loops are playing: a room is entered
from its preview (the room's scene plays on Now) and a second room's preview is open with its loop.
Then the process ends one way:

  quit      the real main loop (Gtk.Application.run, as `listening-room` runs it) until the user
            closes the window; run() returns and the process exits with its status
  sigterm   the real main loop until SIGTERM arrives
  sigint    the real main loop, back on Now with the entered room's loop playing, until Ctrl+C (SIGINT)
  display   the real main loop until the X server goes away
  bus       the real main loop until the session bus goes away
  finish    a probe-style child (the main loop pumped by hand) that finishes and exits 0
  raise     a probe-style child that raises an exception with loops playing
  service   listening-session (the controller daemon) on a private socket, until SIGTERM

A run fails when the child dies by SIGSEGV, SIGABRT, SIGBUS, SIGILL, SIGFPE or SIGTRAP, never
gets its loops playing, or does not exit within the time limit. Prints one line per run and a
summary per mode; exits 1 on any failure.

    listening-exit-paths.py --output DIR [--runs N] [--modes quit,raise,...] [--jobs J]
"""
import argparse
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
MODES = ('quit', 'sigterm', 'sigint', 'display', 'bus', 'finish', 'raise', 'service')
CRASHES = {signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGILL, signal.SIGFPE, signal.SIGTRAP}
READY_SECONDS = 90      # set-up until loops play, generous for a loaded host
EXIT_SECONDS = 60       # from the exit trigger until the process is gone


def no_core_dumps():
    """A crash this probe provokes must not leave a core dump on the host."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


# ---------------------------------------------------------------------------------------- parent

def verdict(code):
    """'ok', or why this exit status is a failure."""
    if code is None:
        return 'hang'
    signum = -code if code < 0 else code - 128 if code > 128 else 0
    if signum in CRASHES:
        return 'crash (%s)' % signal.Signals(signum).name
    return 'ok'


_DISPLAYS = {'lock': threading.Lock(), 'next': 200 + os.getpid() % 500}


def claim_display():
    """A display number no X server holds now, distinct across this probe's concurrent runs."""
    with _DISPLAYS['lock']:
        while True:
            number = _DISPLAYS['next']
            _DISPLAYS['next'] = 200 + (number - 199) % 700
            if not Path('/tmp/.X%d-lock' % number).exists() and not Path('/tmp/.X11-unix/X%d' % number).exists():
                return number


def run_one(mode, index, args):
    runtime = tempfile.mkdtemp(prefix='listening-exit-runtime-')
    os.chmod(runtime, 0o700)
    log_path = Path(args.output) / ('%s-%03d.log' % (mode, index))
    env = {k: v for k, v in os.environ.items()
           if k not in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE', 'DISPLAY')}
    env.update(XDG_RUNTIME_DIR=runtime, GDK_BACKEND='x11', GDK_DEBUG='no-portals', GSK_RENDERER='cairo',
               LISTENING_PROBE_ISOLATED='1', PYTHONFAULTHANDLER='1', PYTHONUNBUFFERED='1')
    socket = Path(runtime) / 'session.sock'
    if mode == 'service':
        home = Path(runtime) / 'home'
        home.mkdir()
        env.update(HOME=str(home), XDG_STATE_HOME=str(home / 'state'), XDG_CONFIG_HOME=str(home / 'config'),
                   XDG_CACHE_HOME=str(home / 'cache'))
        target = [sys.executable, str(Path(args.source) / 'session_service.py'), '--socket', str(socket)]
    else:
        target = [sys.executable, str(HERE / Path(__file__).name), '--child', mode, '--source', args.source,
                  '--output', args.output]
    # xvfb-run's own status hides the child's (it fails when its X server is already gone), so a shell
    # between dbus-run-session and the child reports the child's pid and exit status itself.
    # Not `xvfb-run -a`: concurrent runs of it can all be handed the same display, and the first to finish
    # takes the X server from the others. Each run claims its own display number instead.
    command = ['xvfb-run', '-n', str(claim_display()), '-s', '-screen 0 1440x1000x24', 'dbus-run-session', 'sh', '-c',
               '"$@" & pid=$!; echo "CHILD-PID $pid"; wait $pid; echo "CHILD-EXIT $?"', '_'] + target
    began = time.monotonic()
    code = None
    lines, pids = [], {}
    ready, ended = threading.Event(), threading.Event()
    group = None
    try:
        log = open(log_path, 'w')
        group = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 preexec_fn=no_core_dumps, start_new_session=True)

        def read():
            for line in group.stdout:
                log.write(line)
                log.flush()
                lines.append(line)
                if line.startswith('CHILD-PID '):
                    pids['child'] = int(line.split()[1])
                elif line.startswith('READY'):
                    pids.update((k, int(v)) for k, v in (w.split('=') for w in line.split() if '=' in w))
                    ready.set()
                elif line.startswith('CHILD-EXIT '):
                    pids['exit'] = int(line.split()[1])
                    ended.set()
            ended.set()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + READY_SECONDS
        while not ready.is_set() and not ended.is_set() and time.monotonic() < deadline:
            if mode == 'service' and socket.exists():
                time.sleep(1.)
                ready.set()
            ready.wait(.1)
        if not ready.is_set():
            code = pids.get('exit')
            outcome = 'never ready' if code is None else 'exited %s before ready' % code
            if code is not None and verdict(code) != 'ok':
                outcome = verdict(code) + ' before ready'
            return mode, index, outcome, code, time.monotonic() - began
        trigger = time.monotonic()
        if mode in ('sigterm', 'service'):
            os.kill(pids['child'], signal.SIGTERM)
        elif mode == 'sigint':
            os.kill(pids['child'], signal.SIGINT)
        elif mode == 'display':
            os.kill(pids['xvfb'], signal.SIGTERM)
        elif mode == 'bus':
            os.kill(pids['bus'], signal.SIGTERM)
        ended.wait(EXIT_SECONDS)
        code = pids.get('exit')
        outcome = verdict(code)
        if outcome == 'ok' and mode in ('quit', 'finish') and code != 0:
            outcome = 'exit status %s' % code
        if outcome == 'ok' and mode == 'quit' and not any(l.startswith('RUN-RETURNED') for l in lines):
            outcome = 'the main loop never returned'
        return mode, index, outcome, code, time.monotonic() - trigger
    except Exception as failure:     # the harness itself failed: count it, never as a pass
        return mode, index, 'harness error: %r' % failure, code, time.monotonic() - began
    finally:
        if group is not None:
            try:
                os.killpg(group.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            group.wait()
        shutil.rmtree(runtime, ignore_errors=True)


def parent(args):
    Path(args.output).mkdir(parents=True, exist_ok=True)
    modes = [m for m in args.modes.split(',') if m]
    unknown = set(modes) - set(MODES)
    if unknown:
        sys.exit('unknown modes: %s' % ', '.join(sorted(unknown)))
    print('load', open('/proc/loadavg').read().strip(), flush=True)
    jobs = [(mode, index) for index in range(args.runs) for mode in modes]
    results, lock = [], threading.Lock()

    def worker():
        while True:
            with lock:
                if not jobs:
                    return
                mode, index = jobs.pop(0)
            result = run_one(mode, index, args)
            with lock:
                results.append(result)
                print('RUN %-8s %3d %-28s exit=%s %.1fs' % result, flush=True)
    threads = [threading.Thread(target=worker) for _ in range(max(1, args.jobs))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    failed = 0
    for mode in modes:
        mine = [r for r in results if r[0] == mode]
        bad = [r for r in mine if r[2] != 'ok']
        failed += len(bad)
        print('MODE %-8s %d/%d clean%s' % (mode, len(mine) - len(bad), len(mine),
                                            '' if not bad else '; ' + ', '.join('#%d %s' % (r[1], r[2]) for r in bad)))
    print('load', open('/proc/loadavg').read().strip())
    print('PASS' if not failed else 'FAIL: %d run(s)' % failed)
    return 1 if failed else 0


# ----------------------------------------------------------------------------------------- child

def servers():
    """'xvfb=PID bus=PID': this child's X server (its lock file) and session bus (dbus-run-session's daemon)."""
    def stat(pid):
        text = Path('/proc/%s/stat' % pid).read_text()
        return text[text.index('(') + 1:text.rindex(')')], int(text.rsplit(')', 1)[1].split()[1])
    number = os.environ['DISPLAY'].lstrip(':').split('.')[0]
    xvfb = int(Path('/tmp/.X%s-lock' % number).read_text().strip())
    session = stat(os.getppid())[1]          # this child <- sh <- dbus-run-session
    bus = None
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                name, ppid = stat(entry.name)
            except (OSError, ValueError):
                continue
            if ppid == session and name == 'dbus-daemon':
                bus = int(entry.name)
    return 'xvfb=%d bus=%d' % (xvfb, bus)


def child(mode):
    sys.path.insert(0, str(HERE))
    import o6_env
    env = o6_env.Env(__doc__, extra_args={'--child': {'default': mode}, '--runs': {}, '--modes': {}, '--jobs': {}})
    with env:
        GLib, Gtk = env.GLib, env.Gtk
        import next_widgets
        began = time.monotonic()

        def say(text):
            print('%s  @%.1fs' % (text, time.monotonic() - began), flush=True)

        def cards(window):
            child, found = window.cards.get_first_child(), []
            while child:
                if child in window.rooms.card_for:
                    found.append(child)
                child = child.get_next_sibling()
            return found

        def loop_rooms(window):
            found = []
            for card in cards(window):
                entry = window.rooms.card_for[card]
                preset = next((p for p in window.presets if p.get('id') == entry.get('view_id')), {})
                if preset.get('kind') == 'local' and Path(str(preset.get('url'))).is_file():
                    found.append(card)
            return found

        def click(card):
            overlay = card.get_child()
            gesture = next(c for c in overlay.observe_controllers()
                           if isinstance(c, Gtk.GestureClick) and c.get_button() == 1)
            gesture.emit('pressed', 1, 12., 12.)
            gesture.emit('released', 1, 12., 12.)

        def playing():
            return [loop for loop in next_widgets._LOOPS.values() if loop.wanted and loop.media.get_playing()]

        def scenario(window):
            """Loops playing: a room entered from its preview, then another room's preview open."""
            window.show_rooms()
            yield 1.
            rooms = loop_rooms(window)
            assert rooms, 'no room with a scene loop (is LISTENING_HUB_ASSETS set?)'
            click(rooms[0])
            yield 1.5
            widgets = window.rooms.preview_widgets
            assert widgets is not None, 'the preview did not open'
            widgets['enter'].emit('clicked')
            yield 2.
            window.show_now()
            yield 1.5
            window.show_rooms()
            yield .6
            click(rooms[1 % len(rooms)])
            if mode == 'sigint':     # Ctrl+C in a terminal while the room plays on Now
                yield 1.5
                window.rooms.close_preview(animate=False)
                window.show_now()
            deadline = time.monotonic() + 20
            while True:
                yield .5
                loops = playing()
                if loops:
                    before = [loop.media.get_timestamp() for loop in loops]
                    yield .8
                    if any(loop.media.get_timestamp() != t for loop, t in zip(loops, before)):
                        break
                assert time.monotonic() < deadline, 'no scene loop is playing'
            say('READY %s loops=%d' % (servers(), len(playing())))
            yield 1.5

        if mode in ('finish', 'raise'):
            window = env.launch(page='rooms')
            for delay in scenario(window):
                env.pump(delay)
            if mode == 'raise':
                raise RuntimeError('a probe check failed with loops playing (deliberate)')
            say('FINISHED')
            return 0

        app = env.app
        steps = []

        def advance():
            try:
                delay = next(steps[0])
            except StopIteration:
                if mode == 'quit':
                    say('QUIT: closing the window')
                    for window in list(app.get_windows()):
                        window.close()
                else:
                    say('WAITING for the exit trigger')
                return False
            except Exception as failure:     # a set-up failure: report it, and end the main loop
                say('SETUP-FAILED %r' % failure)
                app.quit()
                return False
            GLib.timeout_add(int(delay * 1000), advance)
            return False

        def activated(*_):
            if not steps and app.window is not None:
                steps.append(scenario(app.window))
                GLib.timeout_add(1500, advance)
        app.connect_after('activate', activated)
        def overdue():
            say('NO-EXIT: quitting')
            app.quit()
            return False
        GLib.timeout_add_seconds(READY_SECONDS + EXIT_SECONDS, overdue)
        status = app.run(['listening-room'])
        say('RUN-RETURNED %s' % status)
        return status


if __name__ == '__main__':
    if '--child' in sys.argv:
        no_core_dumps()
        # A background job starts with SIGINT ignored; a terminal launch has Python's KeyboardInterrupt.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        sys.exit(child(sys.argv[sys.argv.index('--child') + 1]))
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source', default=str(HERE.parents[1] / 'src'))
    parser.add_argument('--output', default=str(Path(tempfile.gettempdir()) / 'listening-exit-paths'))
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--modes', default=','.join(MODES))
    parser.add_argument('--jobs', type=int, default=2)
    sys.exit(parent(parser.parse_args()))
