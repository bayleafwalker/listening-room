"""O11F-TEARDOWN acceptance: no Listening Room process ends in a native crash, on any way out.

Written by the independent acceptance author from the O11F-TEARDOWN brief (Outcome and criteria 2-3), not from the
implementation. Each run is a fresh, isolated set of processes; nothing reaches the desktop session, the network,
systemd, the host's audio or credentials, and every child has core dumps disabled.

The exit paths of the Outcome, each with the scene loops in three states:

  shown    a room preview is open on Rooms and its scene loop visibly moves
  shed     every catalogue loop was previewed, left, and let go (stopped, and its video sink shed where shedding is on)
  playing  a room was entered from its preview: the session plays it and its loop moves on Now

The shipped app. Per run: a private Xvfb and a private session D-Bus started by this probe (so it can take them away),
the real `listening-session` entry point (session_service.main, MPRIS on that bus, socket at its default path under a
private XDG_RUNTIME_DIR) with only its playback backend replaced by the session test double, and the real
`listening-room` entry point (listening_room.main -> NextApp.run) as its own process. In the window process only the
host-touching leaves are faked (system volume, outputs, radio search, starting a controller) and IPv4/IPv6 connects
are refused; the application, its main loop, its session client and stream are the shipped ones. A small driver on
the window's main loop brings the scene to its state and then waits for the probe.

  quit        the user closes the window (what the close button does), and with a room playing also: the mini
              player is open, the main window is closed (hidden) and then the mini player is closed
  sigint      the user presses Ctrl+C in the terminal that started the window
  sigterm     SIGTERM to the window, then SIGTERM to the session service (how a session or systemd stops them)
  bus         the session bus goes away (its dbus-daemon is stopped)
  display     the display goes away (its Xvfb is stopped)

After the window is done, every run stops the session service with SIGTERM, so every run also judges the service.

The probe harness. A probe child built on tests/probes/o6_env.py (the real window and controller in one process, on
the session test double), run exactly as the dev loop runs probes: it re-executes itself under xvfb-run and
dbus-run-session. It brings the scene to its state and then returns from main normally, closing its windows as the
existing probes do (child-normal) or leaving them open (child-open), or raises an exception with the window open, as a
failing assertion does (child-raise).

What "clean" means here (criterion 2; brief: "no ... process ends in a native crash"):
  - no process of the run (window, session service, probe child) ends by SIGSEGV, SIGABRT, SIGBUS, SIGILL, SIGFPE,
    SIGTRAP or SIGSYS (directly, or as 128+N through the probe wrappers), and none prints "Fatal Python error";
  - every process that must end does so on its own within EXIT_S, and with the status its path implies:
      quit: the window's run() returns 0 and the process exits 0;
      sigint: the window ends with any non-crash status (on main: KeyboardInterrupt, then SIGINT);
      sigterm: the window ends by SIGTERM's default action or exits 0/143; the service exits 0 (its own handler);
      bus / display: the window and service end with any non-crash status, or keep running and then stop cleanly
        on SIGTERM (bus); without a display the window must end on its own (display);
      probe child: exits 0 and printed its last line (normal, open), exits 1 and printed the deliberate exception
        (raise).
Criterion 4: the CHANGELOG check (N3) is a source-text check, since documentation has no behaviour.
Criterion 3, on the same runs: closing the window leaves the room playing in the session (playback hand-off), a
service stopped by SIGTERM leaves a readable session.json naming the room entered and removes its socket, and no
scene loop is torn down on the way out (no Gtk.MediaFile cleared/re-opened/finalized, no GstPlay thread ended, up to
the last Python atexit handler: the O11-MEDIA no-teardown guarantee carried to exit; the brief makes a teardown of
those objects an escalation).

Negative controls: a deliberate SIGSEGV in the window while its loop plays, and in a probe child where it would
return, must each be reported as a crash, so a wrapper or handler that turns a native crash into a normal exit is
caught.

Writes OUTPUT/o11ft-acceptance.json (judged by acceptance_verdict.py against expected/o11ft.checks) and
OUTPUT/o11ft-summary.json with every run (exit statuses, timings, load). Ends with "N passed, M failed".

  scripts/listening-dev.sh acceptance OUT o11ft
  LISTENING_O11FT_ARGS='--only quit,sigterm --runs 5' for a focused look (names then carry the run counts used)

Criterion revisions are made in place, keeping the original visible as "[ORIGINAL, superseded by ...]".
"""
import argparse
import atexit
import collections
import concurrent.futures
import json
import os
from pathlib import Path
import resource
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]

# ---------------------------------------------------------------------------- thresholds and run counts
# Author-set (FLAGGED FOR A RULING in the phase-1 report): how long a process may take to end once its exit path
# starts. systemd's default stop timeout is 90 s; a clean exit of an idle GTK app takes well under a second.
EXIT_S = 15.
# Author-set: how long a run may take to bring the scene to its state (8 previews plus up to 15 s to let go).
READY_S = 120.
RELEASE_WAIT_S = 20.      # author: how long the shed state may take to let every loop go (O11-MEDIA allows 15 s)
CHILD_TIMEOUT_S = 300.

STATES = ('shown', 'shed', 'playing')
# (code, path, words, states); the run counts are set from the crash rate measured on main (phase-1 report).
PATHS = [
    ('Q', 'quit', 'the user closes the window', STATES),
    ('M', 'quit-mini', 'the user quits from the mini player (main window closed, then the mini player)', ('playing',)),
    ('I', 'sigint', 'the user presses Ctrl+C in the terminal that started the window (SIGINT)', STATES),
    ('T', 'sigterm', 'SIGTERM to the window, then to the session service', STATES),
    ('B', 'bus', 'the session bus goes away', STATES),
    ('D', 'display', 'the display goes away', STATES),
    ('P', 'child-normal', 'a probe child finishes normally', STATES),
    ('O', 'child-open', 'a probe child finishes normally with its window still open', STATES),
    ('R', 'child-raise', 'a probe child raises', STATES),
]
# Runs per (path, state), from the crash rates measured on main 6f9c7a3 (2.7.0) in phase 1, load 4-17 (author-set,
# FLAGGED FOR A RULING; the phase-1 report has the readings):
#   - the probe child that raises, or returns with its window open, with a loop shown or playing: 79 crashes in 80
#     runs (Clopper-Pearson 95% lower bound 0.75 per state). 10 runs: an unfixed build passes with p <= 1e-6.
#   - the shipped window on Ctrl+C with a room playing: 3 crashes in 200 runs (1.5%; 95% CI 0.3-4.3%). 200 runs: an
#     unfixed build at 1.5% passes with p = 0.049.
#     [ORIGINAL, superseded by 200 runs after the full main run] 120 runs, from 2 crashes in 80 (2.5%, p = 0.048):
#     the full run's 1 in 120 brought the pooled rate to 1.5%, at which 120 runs pass an unfixed build with p = 0.16.
#   - every other path and state: 0 crashes in 20 runs on main (0 in 320 shipped-app runs). These are regression
#     guards: 20 runs miss a regression that crashes 15% of exits with p = 0.04.
RUNS_DEFAULT = 20
RUNS = {('child-raise', 'shown'): 10, ('child-raise', 'playing'): 10,
        ('child-open', 'shown'): 10, ('child-open', 'playing'): 10,
        ('sigint', 'playing'): 200}

CRASH_SIGNALS = {signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGILL, signal.SIGFPE, signal.SIGTRAP,
                 signal.SIGSYS}
DELIBERATE = 'O11FT deliberate failure: the probe child raises here'


def no_core_dumps():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def no_network():
    """Refuse IPv4/IPv6 connects in this process (local sockets, the session's included, still work)."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refused(sock):
        return sock.family in (socket.AF_INET, socket.AF_INET6)

    def connect(sock, address):
        if refused(sock):
            raise OSError('O11FT: network access is disabled in this probe')
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if refused(sock):
            return 111
        return real_connect_ex(sock, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


def thread_names(pid='self'):
    names = []
    try:
        tasks = os.listdir('/proc/%s/task' % pid)
    except OSError:
        return names
    for task in tasks:
        try:
            with open('/proc/%s/task/%s/comm' % (pid, task)) as handle:
                names.append(handle.read().strip())
        except OSError:
            pass
    return names


def write_json(path, data):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(data, default=str))
    os.replace(temporary, path)


# ---------------------------------------------------------------------------- scene driver (window and probe child)

def drive(window, state, facts, active):
    """Bring `window` to the scene state; a generator yielding seconds to wait. `active()` says a room plays."""
    import next_widgets
    began = time.monotonic()
    window.show_rooms()
    while not window.rooms.card_for and time.monotonic() - began < 30:
        yield .2
    rooms = window.rooms
    cards = list(rooms.card_for.values())
    facts['cards'] = len(cards)
    if not cards:
        facts['error'] = 'Rooms shows no room cards'
        return

    def moving(scene, timeout=25.):
        """Wait until the scene's loop plays and its timestamp advances."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            media = scene.media
            if media is not None and scene.playing_motion:
                first = media.get_timestamp()
                yield .4
                if scene.media is media and media.get_timestamp() > first:
                    facts.setdefault('moved', []).append(True)
                    return
            else:
                yield .2
        facts.setdefault('moved', []).append(False)

    if state in ('shown', 'playing'):
        rooms.open_preview(cards[0])
        yield from moving(rooms.preview_widgets['scene'])
    if state == 'shown':
        facts['reached'] = all(facts.get('moved', [False]))
    elif state == 'shed':
        for card in cards:
            window.show_rooms()
            rooms.open_preview(card)
            yield from moving(rooms.preview_widgets['scene'], 25. if card is cards[0] else 12.)
            rooms.close_preview(animate=False)
            yield .3
        deadline = time.monotonic() + RELEASE_WAIT_S
        loops = list(next_widgets._LOOPS.values())
        shedding = bool(next_widgets._PLAY.get('shed'))

        def done():
            return loops and all(l.released and (l.shed or not shedding or not l.player) for l in loops)
        while not done() and time.monotonic() < deadline:
            yield .25
            shedding = bool(next_widgets._PLAY.get('shed'))
        facts.update(loops=len(loops), released=sum(l.released for l in loops), shed_loops=sum(l.shed for l in loops),
                     shedding=shedding)
        facts['reached'] = bool(done()) and len(facts.get('moved', [])) == len(cards) and all(facts['moved'])
    elif state == 'playing':
        rooms.enter(rooms.opened)
        deadline = time.monotonic() + 20
        while not active() and time.monotonic() - began < 60 and time.monotonic() < deadline:
            yield .2
        yield .5
        if active() and not window.snapshot.get('active'):
            for _ in range(20):
                yield .2
                if window.snapshot.get('active'):
                    break
        if window.snapshot.get('active'):
            window.show_now()
        yield .5
        yield from moving(window.scene)
        facts['active'] = bool(active())
        facts['room'] = ((window.snapshot or {}).get('active') or {}).get('id')
        facts['reached'] = facts['active'] and all(facts.get('moved', [False]))
    facts['gstplay'] = thread_names().count('GstPlay')
    facts['ready_s'] = round(time.monotonic() - began, 1)


# ---------------------------------------------------------------------------- teardown watch (window and probe child)

class TeardownWatch:
    """Records Python-level teardown of the scene loops from `arm()` on: Gtk.MediaFile clear/set_* calls, MediaFile
    finalization, and the GstPlay thread count, last at the latest Python atexit handler."""

    def __init__(self, path):
        self.path, self.armed, self.events, self.gstplay = path, False, [], {}
        atexit.register(self.final)     # registered first, so it runs after every later atexit handler

    def hook(self):
        from gi.repository import Gtk
        for name in ('clear', 'set_file', 'set_filename', 'set_resource', 'set_input_stream'):
            original = getattr(Gtk.MediaFile, name, None)
            if original is None:
                continue

            def wrapper(media, *args, _original=original, _name=name):
                if self.armed:
                    self.events.append(_name)
                    self.save()
                return _original(media, *args)
            setattr(Gtk.MediaFile, name, wrapper)

    def arm(self):
        import gc
        from gi.repository import Gtk
        self.armed = True
        self.gstplay['armed'] = thread_names().count('GstPlay')
        for media in [o for o in gc.get_objects() if isinstance(o, Gtk.MediaFile)]:
            try:
                media.weak_ref(lambda: (self.events.append('finalized'), self.save()))
            except Exception:
                pass
        self.save()

    def mark(self, label):
        self.gstplay[label] = thread_names().count('GstPlay')
        self.save()

    def final(self):
        if self.armed:
            self.mark('atexit')

    def save(self):
        try:
            write_json(self.path, {'events': self.events, 'gstplay': self.gstplay})
        except OSError:
            pass


# ---------------------------------------------------------------------------- role: the shipped session service

def role_service(source):
    """The real listening-session entry point, its backend replaced by the session test double."""
    no_network()
    sys.path[:0] = [str(source), str(REPO / 'tests')]
    import room_session
    import session_service
    from test_listening_session import FakeBackend
    state = Path(os.environ['O11FT_STATE'])

    def controller():
        made = room_session.Controller(FakeBackend(), state)
        made._home_action = lambda *_: None
        made._route_preferred = lambda: None
        return made
    session_service.Controller = controller
    sys.argv = [str(source / 'session_service.py')]
    return session_service.main()


# ---------------------------------------------------------------------------- role: the shipped window

def role_window(source):
    """The real listening-room entry point; a driver on its main loop brings the scene to its state."""
    no_network()
    sys.path[:0] = [str(source)]
    run = Path(os.environ['O11FT_RUN'])
    state = os.environ['O11FT_STATE_NAME']
    watch = TeardownWatch(run / 'teardown.json')
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gio, GLib
    import audio_devices
    import listening_hub as hub
    import session_client

    def no_controller(*_args, **_kwargs):
        raise session_client.SessionUnavailable('O11FT: the probe starts the session service, never the window')
    session_client.ensure_service = no_controller
    outputs = [{'id': 'desk', 'name': 'desk', 'description': 'Desk speakers', 'default': True, 'available': True}]
    audio_devices.outputs = lambda *a, **k: [dict(o) for o in outputs]
    audio_devices.test_sound = lambda *a, **k: None
    volume = {'percent': 40}
    hub.system_volume = lambda *a, **k: dict(volume)
    hub.set_volume = lambda value, **k: volume.update(percent=value)
    hub.search_radio = lambda *a, **k: []
    watch.hook()
    import listening_room
    facts = {'pid': os.getpid()}

    def active():
        window = Gio.Application.get_default().window
        return bool(window and (window.snapshot or {}).get('active'))

    def start():
        app = Gio.Application.get_default()
        if app is None or getattr(app, 'window', None) is None:
            return True
        steps = drive(app.window, state, facts, active)

        def step():
            try:
                wait = next(steps)
            except StopIteration:
                write_json(run / 'ready.json', facts)
                GLib.timeout_add(100, command)
                return False
            except Exception:
                facts['error'] = traceback.format_exc()
                write_json(run / 'ready.json', facts)
                GLib.timeout_add(100, command)
                return False
            GLib.timeout_add(int(wait * 1000), step)
            return False
        GLib.timeout_add(10, step)
        return False

    done = set()

    def command():
        path = run / 'command'
        if not path.exists():
            return True
        what = path.read_text().strip()
        if what in done:
            return True
        done.add(what)
        window = Gio.Application.get_default().window
        if what == 'quit':
            watch.arm()
            window.close()
        elif what == 'quit-mini':
            watch.arm()
            window.open_mini()

            def close_main():
                window.close()          # hides the main window while the mini player is open
                GLib.timeout_add(600, close_mini)
                return False

            def close_mini():
                facts['main_hidden'] = not window.get_visible()
                write_json(run / 'quit-mini.json', facts)
                if window.mini_window is not None:
                    window.mini_window.close()
                return False
            GLib.timeout_add(800, close_main)
        elif what == 'arm':
            watch.arm()
        elif what == 'segv':
            os.kill(os.getpid(), signal.SIGSEGV)
        return True

    GLib.timeout_add(300, start)
    sys.argv = [str(source / 'listening_room.py')]
    code = listening_room.main()
    watch.mark('returned')
    write_json(run / 'returned.json', {'code': code, 'gstplay': thread_names().count('GstPlay')})
    print('O11FT window run() returned %r' % (code,), flush=True)
    return code


# ---------------------------------------------------------------------------- role: the probe child (o6_env)

def role_child():
    """A probe child as the dev loop runs one: o6_env re-executes it under xvfb-run and dbus-run-session."""
    no_core_dumps()
    sys.path.insert(0, str(HERE.parent))
    import o6_env
    env = o6_env.Env(__doc__, '/tmp/listening-o11ft-child', extra_args={
        '--role': {'default': ''}, '--state': {'default': 'shown'}, '--end': {'default': 'normal'},
        '--repo': {'default': ''}})
    no_network()
    args = env.args
    output = Path(args.output)
    watch = TeardownWatch(output / 'teardown.json')
    facts = {}
    with env:
        watch.hook()
        window = env.launch(page='rooms')
        for wait in drive(window, args.state, facts, lambda: bool(env.controller.active)):
            env.pump(wait)
        write_json(output / 'ready.json', facts)
        print('CHILD-READY ' + json.dumps(facts, default=str), flush=True)
        watch.arm()
        if args.end == 'segv':
            os.kill(os.getpid(), signal.SIGSEGV)
        if args.end == 'raise':
            raise RuntimeError(DELIBERATE)
        if args.end == 'normal':
            for each in list(env.app.get_windows()):
                each.close()
            env.pump(.3)
        print('CHILD-END normal', flush=True)


# ---------------------------------------------------------------------------- parent: one run

def find_xvfb():
    found = shutil.which('Xvfb')
    if found:
        return found
    text = subprocess.run(['xvfb-run', '-a', 'sh', '-c', 'command -v Xvfb'], capture_output=True, text=True,
                          timeout=60).stdout.strip().splitlines()
    return text[-1] if text else 'Xvfb'


def status_of(code):
    """(signal or None, exit code or None) from a Popen returncode or a 128+N wrapper status."""
    if code is None:
        return None, None
    if code < 0:
        return -code, None
    if code > 128 and code - 128 in {int(s) for s in CRASH_SIGNALS | {signal.SIGTERM, signal.SIGKILL}}:
        return code - 128, None
    return None, code


def crashed(code, log_text=''):
    sig, _ = status_of(code)
    return (sig in {int(s) for s in CRASH_SIGNALS}) or 'Fatal Python error' in log_text


def describe(code):
    sig, exit_code = status_of(code)
    if code is None:
        return 'running'
    if sig:
        return 'signal %s' % signal.Signals(sig).name
    return 'exit %s' % exit_code


def wait_for(process, seconds):
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def stop_group(process, grace=3.):
    """SIGTERM a process group (a bus's activated services unmount and exit), then SIGKILL what is left."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(.1)
    kill_group(process)


def unmount_under(root):
    """Lazily unmount FUSE mounts an activated service (gvfsd-fuse, the document portal) left under `root`."""
    root = str(root)
    try:
        lines = Path('/proc/self/mounts').read_text().splitlines()
    except OSError:
        return
    for line in lines:
        fields = line.split()
        if len(fields) > 2 and fields[1].startswith(root + '/') and fields[2].startswith('fuse'):
            subprocess.run(['fusermount3', '-u', '-z', fields[1].replace('\\040', ' ')], capture_output=True, timeout=10)


def exchange(path, action, timeout=2.):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps({'action': action, 'args': {}}).encode() + b'\n')
        data = b''
        while b'\n' not in data:
            part = connection.recv(65536)
            if not part:
                break
            data += part
    return json.loads(data.split(b'\n', 1)[0])


def read(path):
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return ''


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


class Shipped:
    """One run of the shipped app: Xvfb, session D-Bus, session service and window, then an exit path."""

    def __init__(self, ctx, path, state, index):
        self.ctx, self.path, self.state, self.index = ctx, path, state, index
        self.dir = ctx.runs / ('%s-%s-%02d' % (path, state, index))
        self.result = {'path': path, 'state': state, 'index': index, 'kind': 'shipped'}

    def env(self, home, runtime, display, bus):
        env = {k: v for k, v in os.environ.items()
               if k not in ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'DBUS_SESSION_BUS_ADDRESS', 'DISPLAY',
                            'PULSE_SERVER', 'PIPEWIRE_REMOTE', 'PIPEWIRE_RUNTIME_DIR', 'LISTENING_SESSION_SOCKET',
                            'LISTENING_PROBE_ISOLATED', 'PYTHONFAULTHANDLER', 'GSK_RENDERER', 'SYSTEMD_EXEC_PID',
                            'XAUTHORITY', 'http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'), XDG_STATE_HOME=str(home / 'state'),
                   XDG_CACHE_HOME=str(home / 'cache'), XDG_DATA_HOME=str(home / 'data'), XDG_RUNTIME_DIR=str(runtime),
                   DISPLAY=display, DBUS_SESSION_BUS_ADDRESS=bus, GDK_BACKEND='x11', GDK_DEBUG='no-portals',
                   PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH='%s:%s' % (self.ctx.source, REPO / 'tests'),
                   LISTENING_HUB_SOURCE=str(self.ctx.source / 'listening_hub.py'),
                   LISTENING_AMBIENCE_SOURCE=str(self.ctx.source / 'ambience.py'))
        return env

    def spawn(self, command, env, log):
        handle = open(log, 'w')
        process = subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   preexec_fn=no_core_dumps, start_new_session=True)
        handle.close()
        return process

    def run(self):
        d = self.dir
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        home, runtime = d / 'home', Path(tempfile.mkdtemp(prefix='o11ft-', dir=self.ctx.runtime_root))
        for sub in ('config', 'state', 'cache', 'data'):
            (home / sub).mkdir(parents=True, mode=0o700)
        os.chmod(runtime, 0o700)
        state_dir = home / 'state' / 'o11ft-controller'
        procs = {}
        r = self.result
        r['load_start'] = os.getloadavg()[0]
        started = time.monotonic()
        try:
            # private display and bus
            read_fd, write_fd = os.pipe()
            xvfb = subprocess.Popen([self.ctx.xvfb, '-displayfd', str(write_fd), '-screen', '0', '1440x1000x24',
                                     '-nolisten', 'tcp'], pass_fds=(write_fd,), stdout=subprocess.DEVNULL,
                                    stderr=open(d / 'xvfb.log', 'w'), preexec_fn=no_core_dumps, start_new_session=True)
            os.close(write_fd)
            procs['xvfb'] = xvfb
            number = b''
            deadline = time.monotonic() + 30
            while not number.endswith(b'\n') and time.monotonic() < deadline:
                part = os.read(read_fd, 16)
                if not part:
                    break
                number += part
            os.close(read_fd)
            display = ':' + number.decode().strip()
            bus = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'],
                                   stdout=subprocess.PIPE, stderr=open(d / 'dbus.log', 'w'), text=True,
                                   env=self.env(home, runtime, display, ''), preexec_fn=no_core_dumps,
                                   start_new_session=True)
            procs['bus'] = bus
            address = bus.stdout.readline().strip()
            if not number.strip() or not address:
                r['error'] = 'no private display (%r) or bus (%r)' % (number, address)
                return r
            env = self.env(home, runtime, display, address)
            env.update(O11FT_STATE=str(state_dir), O11FT_RUN=str(d), O11FT_STATE_NAME=self.state)
            socket_file = runtime / 'listening-hub' / 'session.sock'
            procs['service'] = self.spawn([sys.executable, str(HERE), '--role', 'service', '--source',
                                           str(self.ctx.source)], env, d / 'service.log')
            deadline = time.monotonic() + 30
            while not socket_file.exists() and time.monotonic() < deadline and procs['service'].poll() is None:
                time.sleep(.1)
            if not socket_file.exists():
                r['error'] = 'the session service did not bind its socket: ' + read(d / 'service.log')[-600:]
                return r
            procs['window'] = self.spawn([sys.executable, str(HERE), '--role', 'window', '--source',
                                          str(self.ctx.source)], env, d / 'window.log')
            deadline = time.monotonic() + READY_S
            while not (d / 'ready.json').exists() and time.monotonic() < deadline and procs['window'].poll() is None:
                time.sleep(.2)
            ready = load_json(d / 'ready.json') or {}
            r['ready'] = ready
            r['reached'] = bool(ready.get('reached'))
            r['ready_s'] = round(time.monotonic() - started, 1)
            if procs['window'].poll() is not None or not ready:
                r['error'] = 'the window never reached its state: %s; %s' % (
                    describe(procs['window'].poll()), read(d / 'window.log')[-800:])
                r['window_code'] = procs['window'].poll()
                r['window_crashed'] = crashed(procs['window'].poll(), read(d / 'window.log'))
                return r
            r['load_trigger'] = os.getloadavg()[0]
            self.trigger(procs, socket_file, state_dir)
        except Exception:
            r['error'] = traceback.format_exc()
        finally:
            for name in ('window', 'service'):
                if name in procs:
                    kill_group(procs[name])
            for name in ('bus', 'xvfb'):
                if name in procs:
                    stop_group(procs[name])
            unmount_under(runtime)
            shutil.rmtree(runtime, ignore_errors=True)
            r['seconds'] = round(time.monotonic() - started, 1)
            r['load_end'] = os.getloadavg()[0]
            write_json(d / 'result.json', r)
        return r

    def judge(self, name, process, log, code, expected, took):
        text = read(log)
        entry = {'code': code, 'status': describe(code), 'crashed': crashed(code, text), 'seconds': took,
                 'expected': expected(code), 'fatal': 'Fatal Python error' in text}
        self.result[name] = entry
        return entry

    def end(self, process, seconds):
        t = time.monotonic()
        code = wait_for(process, seconds)
        return code, round(time.monotonic() - t, 2)

    def trigger(self, procs, socket_file, state_dir):
        d, r, path = self.dir, self.result, self.path
        window, service = procs['window'], procs['service']

        def window_ok_quit(code):
            returned = load_json(d / 'returned.json') or {}
            return code == 0 and returned.get('code') == 0

        def term_ok(code):
            sig, exit_code = status_of(code)
            return sig == signal.SIGTERM or exit_code in (0, 143)

        def any_clean(code):
            return code is not None and not crashed(code)

        if path in ('quit', 'quit-mini'):
            (d / 'command').write_text(path)
            code, took = self.end(window, EXIT_S + (2 if path == 'quit-mini' else 0))
            self.judge('window', window, d / 'window.log', code, window_ok_quit, took)
            r['returned'] = load_json(d / 'returned.json')
            if path == 'quit-mini':
                r['quit_mini'] = load_json(d / 'quit-mini.json')
            if self.state == 'playing':
                try:
                    snapshot = exchange(socket_file, 'inspect')
                    r['playing_after_quit'] = bool(((snapshot or {}).get('result') or {}).get('active'))
                except (OSError, ValueError) as error:
                    r['playing_after_quit'] = False
                    r['inspect_error'] = str(error)
        elif path == 'sigint':
            (d / 'command').write_text('arm')      # the teardown watch starts, then Ctrl+C
            deadline = time.monotonic() + 5
            while not (d / 'teardown.json').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            window.send_signal(signal.SIGINT)
            code, took = self.end(window, EXIT_S)
            self.judge('window', window, d / 'window.log', code, any_clean, took)
        elif path == 'sigterm':
            window.send_signal(signal.SIGTERM)
            code, took = self.end(window, EXIT_S)
            self.judge('window', window, d / 'window.log', code, term_ok, took)
        elif path == 'bus':
            procs['bus'].send_signal(signal.SIGTERM)
            wait_for(procs['bus'], 5)
            code, took = self.end(window, EXIT_S)
            if code is None:      # the window may live on without its bus; then the session ends it
                r['window_survived_bus'] = True
                window.send_signal(signal.SIGTERM)
                code, took2 = self.end(window, EXIT_S)
                took += took2
                self.judge('window', window, d / 'window.log', code, term_ok, took)
            else:
                self.judge('window', window, d / 'window.log', code, any_clean, took)
            code, took = self.end(service, 3)
            if code is None:
                r['service_survived_bus'] = True
            else:
                self.judge('service', service, d / 'service.log', code, any_clean, took)
        elif path == 'display':
            procs['xvfb'].send_signal(signal.SIGTERM)
            code, took = self.end(window, EXIT_S)
            self.judge('window', window, d / 'window.log', code, any_clean, took)
        # every run then stops the session service as a session or systemd would
        if 'service' not in r:
            if service.poll() is None:
                service.send_signal(signal.SIGTERM)
            code, took = self.end(service, EXIT_S)
            self.judge('service', service, d / 'service.log', code, lambda c: c == 0, took)
            r['service']['socket_removed'] = not socket_file.exists()
        session = load_json(state_dir / 'session.json')
        r['session_json'] = session is not None or not (state_dir / 'session.json').exists()
        r['last_session'] = ((session or {}).get('last_session') or {}).get('id') if session else None
        r['session_exists'] = (state_dir / 'session.json').exists()
        r['teardown'] = load_json(d / 'teardown.json')


class Child:
    """One run of a probe child (o6_env), finishing normally, raising, or (control) segfaulting."""

    def __init__(self, ctx, path, state, index, end=None):
        self.ctx, self.path, self.state, self.index = ctx, path, state, index
        self.end = end or {'child-normal': 'normal', 'child-open': 'open', 'child-raise': 'raise'}[path]
        self.dir = ctx.runs / ('%s-%s-%02d' % (path, state, index))
        self.result = {'path': path, 'state': state, 'index': index, 'kind': 'child', 'end': self.end}

    def run(self):
        d, r = self.dir, self.result
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        runtime = Path(tempfile.mkdtemp(prefix='o11ft-child-', dir=self.ctx.runtime_root))
        os.chmod(runtime, 0o700)
        env = {k: v for k, v in os.environ.items()
               if k not in ('LISTENING_PROBE_ISOLATED', 'DBUS_SESSION_BUS_ADDRESS', 'WAYLAND_DISPLAY',
                            'HYPRLAND_INSTANCE_SIGNATURE')}
        env.update(XDG_RUNTIME_DIR=str(runtime), PYTHONFAULTHANDLER='1', PYTHONDONTWRITEBYTECODE='1')
        command = [sys.executable, str(HERE), '--role', 'child', '--state', self.state, '--end', self.end,
                   '--source', str(self.ctx.source), '--output', str(d)]
        r['load_start'] = os.getloadavg()[0]
        started = time.monotonic()
        log = d / 'child.log'
        with open(log, 'w') as handle:
            process = subprocess.Popen(command, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, preexec_fn=no_core_dumps, start_new_session=True)
        ready_at = None
        code = None
        while True:
            code = wait_for(process, .25)
            if code is not None:
                break
            if ready_at is None and (d / 'ready.json').exists():
                ready_at = time.monotonic()
            if time.monotonic() - started > CHILD_TIMEOUT_S or (ready_at and time.monotonic() - ready_at > EXIT_S + 30):
                kill_group(process)
                code = None
                r['hung'] = True
                break
        stop_group(process)
        unmount_under(runtime)
        shutil.rmtree(runtime, ignore_errors=True)
        text = read(log)
        ready = load_json(d / 'ready.json') or {}
        r.update(ready=ready, reached=bool(ready.get('reached')), seconds=round(time.monotonic() - started, 1),
                 load_end=os.getloadavg()[0], teardown=load_json(d / 'teardown.json'))
        exit_s = round(time.monotonic() - ready_at, 1) if ready_at else None
        if self.end in ('normal', 'open'):
            expected = code == 0 and 'CHILD-END normal' in text
        elif self.end == 'raise':
            expected = code == 1 and DELIBERATE in text and 'Traceback' in text
        else:
            expected = False
        r['child'] = {'code': code, 'status': describe(code) if code is not None else 'hung', 'crashed':
                      crashed(code, text) if code is not None else False, 'expected': expected,
                      'fatal': 'Fatal Python error' in text, 'seconds': exit_s}
        write_json(d / 'result.json', r)
        return r


# ---------------------------------------------------------------------------- parent: judging

class Context:
    pass


def uptime():
    try:
        return subprocess.run(['uptime'], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 'load average: %.2f, %.2f, %.2f' % os.getloadavg()


def processes(r):
    return [r[name] for name in ('window', 'service', 'child') if isinstance(r.get(name), dict)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--repo', default=str(REPO))
    parser.add_argument('--source', default=None)
    parser.add_argument('--output', default=str(Path(tempfile.gettempdir()) / 'listening-o11ft-acceptance'))
    parser.add_argument('--only', default='', help='comma list of paths (quit,quit-mini,sigint,sigterm,bus,display,'
                        'child-normal,child-open,child-raise,controls,notes)')
    parser.add_argument('--states', default=','.join(STATES))
    parser.add_argument('--runs', type=int, default=0, help='runs per path and state (default: the RUNS table)')
    parser.add_argument('--jobs', type=int, default=4)
    extra = shlex.split(os.environ.get('LISTENING_O11FT_ARGS', ''))
    args = parser.parse_args(sys.argv[1:] + extra)
    repo = Path(args.repo).resolve()
    ctx = Context()
    ctx.source = Path(args.source).resolve() if args.source else repo / 'src'
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    ctx.runs = out / 'o11ft-runs'
    ctx.runs.mkdir(exist_ok=True)
    ctx.runtime_root = tempfile.mkdtemp(prefix='o11ft-runtime-', dir=os.environ.get('XDG_RUNTIME_DIR'))
    ctx.xvfb = find_xvfb()
    results, summary = [], {'uptime_start': uptime(), 'source': str(ctx.source), 'runs': []}
    only = set(filter(None, args.only.split(',')))
    states = [s for s in args.states.split(',') if s]
    print('source: %s  cpus: %d  jobs: %d' % (ctx.source, os.cpu_count(), args.jobs), flush=True)
    print('uptime at start: %s' % summary['uptime_start'], flush=True)

    def check(name, ok, detail=''):
        results.append({'criterion': name, 'pass': bool(ok), 'detail': str(detail)[:1500]})
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ': ' + str(detail)[:1500]), flush=True)

    plan = []
    for code, path, words, path_states in PATHS:
        if only and path not in only:
            continue
        for state in path_states:
            if state not in states:
                continue
            count = args.runs or RUNS.get((path, state), RUNS_DEFAULT)
            for index in range(count):
                plan.append((code, path, words, state, count, index))
    controls = not only or 'controls' in only
    try:
        jobs = []
        for code, path, words, state, count, index in plan:
            runner = (Child if path.startswith('child-') else Shipped)(ctx, path, state, index)
            jobs.append(((code, path, words, state, count), runner))
        if controls:
            jobs.append((('N', 'control-window', '', 'playing', 1), Shipped(ctx, 'control-window', 'playing', 0)))
            jobs.append((('N', 'control-child', '', 'shown', 1), Child(ctx, 'control-child', 'shown', 0, end='segv')))
        # Interleave paths and states so load and time of day spread over all of them.
        order = sorted(range(len(jobs)), key=lambda i: (jobs[i][1].index, i))
        done = collections.defaultdict(list)
        finished = [0]
        lock = threading.Lock()

        def work(i):
            key, runner = jobs[i]
            if isinstance(runner, Shipped) and runner.path == 'control-window':
                runner.path = 'sigterm'          # brought to its state like any run, then crashed on purpose
                runner.trigger = lambda procs, socket_file, state_dir: control_window(runner, procs)
            r = runner.run()
            with lock:
                finished[0] += 1
                done[key].append(r)
                print('RUN %d/%d %s %s #%d: %s, %s in %.0f s (load %.1f)%s' % (
                    finished[0], len(jobs), key[1], key[3], runner.index,
                    'reached' if r.get('reached') else 'NOT reached',
                    ', '.join('%s %s%s' % (n, p.get('status'), ' CRASH' if p.get('crashed') else '')
                              for n, p in (('window', r.get('window')), ('service', r.get('service')),
                                           ('child', r.get('child'))) if isinstance(p, dict)),
                    r.get('seconds', 0), r.get('load_end', 0), ('; ' + r['error'][-300:]) if r.get('error') else ''),
                    flush=True)
            return r
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            list(pool.map(work, order))
        summary['runs'] = [r for key in done for r in done[key]]

        for code, path, words, path_states in PATHS:
            for state in path_states:
                key = next((k for k in done if k[1] == path and k[3] == state), None)
                if key is None:
                    continue
                runs = done[key]
                count = key[4]
                tag = '%s %s, scene loops %s' % (code, words, state)
                missed = [(r['index'], (r.get('error') or '')[-200:] or r.get('ready')) for r in runs
                          if not r.get('reached')]
                check('%s1 %s: every run reached that scene state (%d runs)' % (code, tag, count),
                      len(runs) == count and not missed, 'not reached: %s' % missed[:4])
                crashes = [(r['index'], n, p['status']) for r in runs for n, p in
                           (('window', r.get('window')), ('service', r.get('service')), ('child', r.get('child')))
                           if isinstance(p, dict) and p.get('crashed')]
                crashes += [(r['index'], 'window', describe(r.get('window_code'))) for r in runs if r.get('window_crashed')]
                check('%s2 %s: no Listening Room process ended in a native crash (%d runs)' % (code, tag, count),
                      len(runs) == count and not crashes,
                      '%d crashed of %d runs: %s' % (len({c[0] for c in crashes}), len(runs), crashes[:8]))
                bad, slowest = [], 0.
                for r in runs:
                    if not r.get('reached'):
                        continue
                    for n in ('window', 'service', 'child'):
                        p = r.get(n)
                        if isinstance(p, dict):
                            slowest = max(slowest, p.get('seconds') or 0)
                            if not p.get('expected'):
                                bad.append((r['index'], n, p.get('status')))
                    if path in ('quit', 'quit-mini', 'sigint', 'sigterm', 'display') and not isinstance(r.get('window'), dict):
                        bad.append((r['index'], 'window', 'not judged'))
                check('%s3 %s: every process ended as its path expects, on its own within %.0f s (%d runs; slowest '
                      '%.1f s)' % (code, tag, EXIT_S, count, slowest),
                      len(runs) == count and not bad and all(r.get('reached') for r in runs),
                      'unexpected: %s' % bad[:8])
                if path in ('quit', 'quit-mini', 'sigint', 'child-normal', 'child-open', 'child-raise'):
                    torn = []
                    for r in runs:
                        t = r.get('teardown') or {}
                        g = t.get('gstplay') or {}
                        if not t or t.get('events') or (g.get('armed') is not None and
                                                         min(v for v in g.values()) < g['armed']):
                            torn.append((r['index'], t.get('events'), g))
                    check('%s4 %s: no scene loop is torn down on the way out: no Gtk.MediaFile cleared, re-opened '
                          'or finalized and no GstPlay thread ended, up to the last Python atexit handler (%d runs)'
                          % (code, tag, count), runs and not torn, 'torn down: %s' % torn[:4])
                if path in ('quit', 'quit-mini') and state == 'playing':
                    lost = [r['index'] for r in runs if r.get('reached') and not r.get('playing_after_quit')]
                    check('%s5 %s: after the window is gone the room still plays in the session service '
                          '(playback hand-off; %d runs)' % (code, tag, count), runs and not lost,
                          'stopped in runs %s' % lost)
                if path == 'quit-mini':
                    hidden = [r['index'] for r in runs if not (r.get('quit_mini') or {}).get('main_hidden')]
                    check('%s6 %s: closing the main window with the mini player open only hid it (%d runs)'
                          % (code, tag, count), runs and not hidden, 'not hidden in runs %s' % hidden)
        shipped = [r for r in summary['runs'] if r.get('kind') == 'shipped' and not r.get('control')]
        stopped = [r for r in shipped if isinstance(r.get('service'), dict) and r['service'].get('expected')
                   and 'socket_removed' in r['service']]
        if [r for r in shipped if isinstance(r.get('service'), dict)]:
            unsaved = [(r['path'], r['state'], r['index']) for r in stopped
                       if not r.get('session_json') or not r['service'].get('socket_removed')]
            check('S1 every session service stopped by SIGTERM removed its socket and left a readable session.json '
                  '(%d stops)' % len(stopped), stopped and not unsaved, 'not saved or socket left: %s' % unsaved[:6])
            entered = [r for r in stopped if r['state'] == 'playing' and r.get('reached')]
            wrong = [(r['path'], r['index'], r.get('last_session')) for r in entered
                     if not r.get('last_session') or r.get('last_session') != (r.get('ready') or {}).get('room')]
            if [r for r in shipped if r['state'] == 'playing']:
                check('S2 after a room was entered, the stopped service\'s session.json names it as the last session '
                      '(%d stops)' % len(entered), entered and not wrong, 'wrong or missing: %s' % wrong[:6])
        if not only or 'notes' in only:
            text = (repo / 'CHANGELOG.md').read_text()
            head, _, rest = text.partition('## [Unreleased]')
            section = rest.split('\n## [', 1)[0].lower()
            check('N3 the CHANGELOG records the fix under [Unreleased]: users could hit it (Ctrl+C on the shipped '
                  'window with a room playing crashed on main), so the section names the crash and the quit or exit',
                  'crash' in section and any(w in section for w in ('quit', 'exit', 'ctrl+c', 'close')),
                  '[Unreleased] section: %r' % section[:400])
        if controls:
            control = next((r for r in summary['runs'] if r.get('index') == 0 and r.get('control')), None)
            check('N1 negative control: a deliberate SIGSEGV in the window while its room plays is reported as a crash',
                  bool(control and control.get('reached') and (control.get('window') or {}).get('crashed')),
                  'control run: %s' % json.dumps(control, default=str)[:600])
            control = next((r for r in summary['runs'] if r.get('path') == 'control-child'), None)
            check('N2 negative control: a deliberate SIGSEGV in a probe child where it would return is reported as '
                  'a crash through xvfb-run and dbus-run-session',
                  bool(control and control.get('reached') and (control.get('child') or {}).get('crashed')),
                  'control run: %s' % json.dumps(control, default=str)[:600])
    except Exception:
        check('the probe ran to the end', False, traceback.format_exc())
    finally:
        shutil.rmtree(ctx.runtime_root, ignore_errors=True)
        summary['uptime_end'] = uptime()
        print('uptime at end: %s' % summary['uptime_end'], flush=True)
        (out / 'o11ft-summary.json').write_text(json.dumps(summary, indent=1, default=str))
        (out / 'o11ft-acceptance.json').write_text(json.dumps({'results': results}, indent=2))
        failed = [r for r in results if not r['pass']]
        print(f'{len(results) - len(failed)} passed, {len(failed)} failed', flush=True)


def control_window(runner, procs):
    """N1: the window is told to SIGSEGV itself while its room plays."""
    d, r = runner.dir, runner.result
    r['control'] = True
    (d / 'command').write_text('segv')
    code, took = runner.end(procs['window'], EXIT_S)
    runner.judge('window', procs['window'], d / 'window.log', code, lambda c: False, took)


if __name__ == '__main__':
    if '--role' in sys.argv:
        role = sys.argv[sys.argv.index('--role') + 1]
        if role == 'child':
            role_child()
        else:
            known = argparse.ArgumentParser()
            known.add_argument('--role')
            known.add_argument('--source')
            parsed, _rest = known.parse_known_args()
            source = Path(parsed.source).resolve()
            sys.exit(role_service(source) if role == 'service' else role_window(source))
    else:
        main()
