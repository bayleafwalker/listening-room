"""HOTFIX-MEDIA acceptance: opening, switching and leaving rooms never crashes the window, and scenes still behave.

Written by the acceptance reviewer, not the implementer. Real GTK on a private Xvfb with its own D-Bus
(this file re-executes itself there once, like tests/probes/o6_env.py), the real session controller on
the session test double and fixture providers. Nothing reaches the desktop session, the network,
systemd or real credentials, and no audio is played on the host.

Two kinds of child process run on that display, each with its own HOME and XDG dirs (o6_env):

  checks  behavioural oracles: a preview's loop and an entered room's loop play (Gtk.MediaFile playing,
          its timestamp advancing and the pixels on screen changing), reduced motion shows the still
          (no media, pixels static), and a preview is inert (muted, no controller command, no player
          process, no GStreamer audio sink created).
  stress  rapid room-card switching, preview open/close (Esc, the Rooms button, another card),
          entering from a preview, Back (Esc on Now and the toast's "Back to"), leaving, and closing
          and reopening the window, repeated with randomised timing.

Each child is a subprocess, so a segfault is a result (exit status -11 or another signal), not the end
of the probe. Children disable core dumps for themselves. The summary reports crashes per child and per
stress cycle, plus the load average, and ends with "N passed, M failed".

The dev shell has no GStreamer, and without it Scene never creates a Gtk.MediaFile (no loop, no crash):
--gstreamer auto (the default) adds the package's GStreamer from the flake's own nixpkgs, and a setup
check fails when playbin3 is still missing, so a clean run is never vacuous.

  scripts/listening-dev.sh probe OUT listening-hotfix-media-acceptance   (defaults), or
  scripts/listening-dev.sh shell, then:
  python3 tests/probes/listening-hotfix-media-acceptance.py --output OUT [--children 12 --cycles 6 --jobs 3]
  (--source points at another src/ tree, e.g. a negative control; --load N adds N busy CPU workers)
"""
import argparse
import faulthandler
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve()
FLAGS = {
    '--child': {'default': ''},                  # internal: 'checks' or 'stress'
    '--seed': {'type': int, 'default': 0},
    '--children': {'type': int, 'default': 26},  # stress children: 26 x 8 = 208 cycles
    '--cycles': {'type': int, 'default': 8},    # stress cycles per child
    '--reopen-every': {'type': int, 'default': 3},
    '--jobs': {'type': int, 'default': 4},       # stress children run at once on the shared display
    '--load': {'type': int, 'default': 0},       # extra busy CPU workers for the whole run
    '--child-timeout': {'type': float, 'default': 0.},   # 0: 150 s + 60 s per cycle
    '--skip-checks': {'action': 'store_true'},
    '--leak-cycles': {'type': int, 'default': 40},  # measured preview open/close cycles in the leak child
    '--gstreamer': {'default': 'auto'},          # auto: add the flake's GStreamer when the shell lacks it; off
}
GST_PACKAGES = ('gstreamer.out', 'gst-plugins-base', 'gst-plugins-good', 'gst-plugins-bad', 'gst-libav')
GST_READY = ("import gi, sys; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); "
             "sys.exit(0 if Gst.ElementFactory.find('playbin3') else 1)")


def parse():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source', default=str(HERE.parents[2] / 'src'))
    parser.add_argument('--output', default='/tmp/listening-hotfix-media-acceptance')
    parser.add_argument('--repo', default='')   # accepted for parity with the dev script's acceptance runner
    for flag, kwargs in FLAGS.items():
        parser.add_argument(flag, **kwargs)
    return parser.parse_known_args()[0]


def no_core_dumps():
    """A deliberate crash must not leave a core dump on the host."""
    import ctypes
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)   # PR_SET_DUMPABLE = 4
    except Exception:
        pass


# ============================================================================ parent

def reexec_isolated():
    runtime = tempfile.mkdtemp(prefix='listening-hotfix-runtime-')
    os.chmod(runtime, 0o700)
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE')}
    environment.update(XDG_RUNTIME_DIR=runtime, GDK_BACKEND='x11', GDK_DEBUG='no-portals',
                       GSK_RENDERER=os.environ.get('HOTFIX_GSK_RENDERER', 'cairo'), LISTENING_PROBE_ISOLATED='1')
    command = ['xvfb-run', '-a', '-s', '-screen 0 1440x1000x24', 'dbus-run-session', sys.executable] + sys.argv
    try:
        sys.exit(subprocess.call(command, env=environment))
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def gstreamer(args):
    """Where the window's GStreamer comes from. The dev shell has none, and without it Scene never creates a
    Gtk.MediaFile: no loop plays and the crash cannot happen, so a clean run would prove nothing. The package
    build has GStreamer (its buildInputs); 'auto' adds the same packages from the flake's own nixpkgs."""
    def ready():
        return subprocess.call([sys.executable, '-c', GST_READY], stderr=subprocess.DEVNULL) == 0
    if ready():
        return 'from the environment'
    if args.gstreamer == 'off' or not shutil.which('nix'):
        return None
    repo = args.repo or str(HERE.parents[2])
    found = subprocess.run(['nix', 'build', '--inputs-from', repo, '--no-link', '--print-out-paths']
                           + ['nixpkgs#gst_all_1.' + name for name in GST_PACKAGES],
                           capture_output=True, text=True, timeout=900)
    paths = found.stdout.split()
    if found.returncode or len(paths) != len(GST_PACKAGES):
        print('could not resolve GStreamer from the flake: ' + found.stderr[-800:], flush=True)
        return None
    join = lambda parts, old: os.pathsep.join([p for p in parts if os.path.isdir(p)] + ([old] if old else []))
    os.environ['GST_PLUGIN_SYSTEM_PATH_1_0'] = join([p + '/lib/gstreamer-1.0' for p in paths],
                                                   os.environ.get('GST_PLUGIN_SYSTEM_PATH_1_0'))
    os.environ['GI_TYPELIB_PATH'] = join([p + '/lib/girepository-1.0' for p in paths], os.environ.get('GI_TYPELIB_PATH'))
    os.environ['GST_REGISTRY_1_0'] = os.path.join(tempfile.mkdtemp(prefix='hotfix-gst-'), 'registry.bin')
    return 'from the flake (%s)' % ', '.join(Path(p).name for p in paths) if ready() else None


def run_parent(args):
    args.child_timeout = args.child_timeout or 150. + 60. * args.cycles
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, bool(ok)))
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:600]), flush=True)

    def child(mode, seed):
        log = output / ('%s-%03d.log' % (mode, seed))
        gst = output / ('%s-%03d.gst' % (mode, seed))
        environment = dict(os.environ, PYTHONFAULTHANDLER='1', GST_DEBUG='GST_ELEMENT_FACTORY:4',
                           GST_DEBUG_FILE=str(gst), GST_DEBUG_NO_COLOR='1')
        runtime = tempfile.mkdtemp(prefix='hotfix-child-', dir=os.environ.get('XDG_RUNTIME_DIR'))
        environment['XDG_RUNTIME_DIR'] = runtime
        # Each child gets its own session bus: two NextApps on one bus would make the second a remote instance.
        command = ['dbus-run-session', '--', sys.executable, str(HERE), '--child', mode, '--seed', str(seed), '--source', args.source,
                   '--output', str(output / ('%s-%03d' % (mode, seed))), '--cycles', str(args.cycles),
                   '--reopen-every', str(args.reopen_every), '--leak-cycles', str(args.leak_cycles), '--gstreamer', 'off']
        handle = open(log, 'w')
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=environment,
                                   preexec_fn=no_core_dumps)
        return {'mode': mode, 'seed': seed, 'log': log, 'gst': gst, 'process': process, 'handle': handle, 'runtime': runtime,
                'started': time.monotonic()}

    def finish(item, timeout):
        try:
            code = item['process'].wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            item['process'].kill()
            code = 'timeout'
            item['process'].wait()
        item['handle'].close()
        if isinstance(code, int) and code > 128:
            code = -(code - 128)   # dbus-run-session reports a signalled child as 128 + signal
        shutil.rmtree(item['runtime'], ignore_errors=True)
        text = item['log'].read_text(errors='replace')
        item.update(code=code, text=text, seconds=time.monotonic() - item['started'],
                    cycles=text.count('\nCYCLE-DONE ') + text.startswith('CYCLE-DONE '),
                    last_step=next((line for line in reversed(text.splitlines()) if line.startswith('STEP ')), ''))
        threads = [int(line.rsplit(' ', 1)[1]) for line in text.splitlines()
                   if line.startswith('CYCLE-DONE ') and line.rsplit(' ', 1)[1].isdigit()]
        item['threads'] = (threads[0], threads[-1]) if threads else None
        # Where the crash happened: the main thread's innermost frame in src/, and the faulting C frames.
        frame = ''
        if 'Fatal Python error' in text:
            main = text.split('[python3', 1)[-1] if '[python3' in text else text
            frame = next((line.strip() for line in main.splitlines() if 'File "' in line and '/src/' in line), '')
            c_stack = text.split("Current thread's C stack trace", 1)[-1]
            item['gstplay_thread'] = 'libgstplay' in c_stack and 'g_object_unref' in c_stack
        item['frame'] = frame
        try:
            item['gst'].unlink()   # large and only read by the child itself
        except OSError:
            pass
        return item

    source_of_gst = gstreamer(args)
    print('GStreamer playbin3: %s' % (source_of_gst or 'missing'), flush=True)
    check('setup: GStreamer playbin3 is available to the window, so scene loops really play', source_of_gst,
          'without it Scene falls back to the still and no Gtk.MediaFile is ever created')
    burners = [subprocess.Popen([sys.executable, '-c', 'while True: pass'], preexec_fn=lambda: os.nice(10))
               for _ in range(args.load)]
    started = time.monotonic()
    load_samples = [os.getloadavg()]
    print('source: %s' % Path(args.source).resolve(), flush=True)
    print('renderer: %s  load average at start: %.2f %.2f %.2f  cpus: %d  extra busy workers: %d' % (
        os.environ.get('GSK_RENDERER'), *load_samples[0], os.cpu_count(), args.load), flush=True)
    try:
        for mode, label in (() if args.skip_checks else (('checks', 'behavioural'), ('leak', 'lifetime'))):
            item = finish(child(mode, 0), args.child_timeout)
            for line in item['text'].splitlines():
                if line.startswith('INFO '):
                    print(line, flush=True)
                elif line.startswith(('PASS ', 'FAIL ')):
                    check(line[5:].split('  ')[0], line.startswith('PASS '), line[5:].partition('  ')[2])
            crashed = not isinstance(item['code'], int) or item['code'] < 0
            check('%s: the %s child finished without crashing' % (mode, label), not crashed,
                  'exit %s at %s\n%s' % (item['code'], item['last_step'], item['text'][-1500:]))
            check('%s: the %s child reported its oracles and exited 0' % (mode, label),
                  item['code'] == 0 and 'CHECKS-DONE 0 failed' in item['text'],
                  'exit %s\n%s' % (item['code'], item['text'][-1500:]))

        results, running, seeds = [], [], list(range(1, args.children + 1))
        while seeds or running:
            while seeds and len(running) < max(1, args.jobs):
                running.append(child('stress', seeds.pop(0)))
            time.sleep(.2)
            for item in list(running):
                if item['process'].poll() is not None or time.monotonic() - item['started'] > args.child_timeout:
                    running.remove(item)
                    results.append(finish(item, 1))
                    load_samples.append(os.getloadavg())
                    print('stress child %03d: exit %s after %d/%d cycles in %.0f s%s' % (
                        item['seed'], item['code'], item['cycles'], args.cycles, item['seconds'],
                        '  (%s)' % item['last_step'] if item['code'] != 0 else ''), flush=True)
    finally:
        for burner in burners:
            burner.kill()
            burner.wait()

    results.sort(key=lambda item: item['seed'])
    crashes = [r for r in results if isinstance(r['code'], int) and r['code'] < 0]
    segv = [r for r in crashes if r['code'] == -signal.SIGSEGV]
    timeouts = [r for r in results if r['code'] == 'timeout']
    exit_hangs = [r for r in timeouts if 'STEP exiting the process' in r['text']]
    errors = [r for r in results if isinstance(r['code'], int) and r['code'] > 0]
    cycles = sum(r['cycles'] for r in results)
    # A crashing child stops early: its cycles count up to the crash, and the crash cycle itself.
    attempted = cycles + len(crashes) + len(timeouts)
    loads = [sample[0] for sample in load_samples]
    summary = {
        'source': str(Path(args.source).resolve()), 'children': len(results), 'cycles_per_child': args.cycles,
        'cycles_completed': cycles, 'cycles_attempted': attempted, 'crashed_children': len(crashes),
        'segv_children': len(segv), 'signals': sorted({-r['code'] for r in crashes}), 'timeouts': len(timeouts),
        'errors': len(errors), 'exit_hangs': len(exit_hangs), 'crash_rate_per_child': round(len(crashes) / max(1, len(results)), 4),
        'crashes_per_100_cycles': round(100. * len(crashes) / max(1, attempted), 3),
        'load_average_1m': {'min': min(loads), 'max': max(loads), 'mean': round(sum(loads) / len(loads), 2)},
        'extra_busy_workers': args.load, 'seconds': round(time.monotonic() - started),
        'crash_steps': [{'seed': r['seed'], 'code': r['code'], 'step': r['last_step'], 'main_frame': r.get('frame'),
                         'faulting_thread_unref_in_libgstplay': r.get('gstplay_thread'), 'log': str(r['log'])}
                        for r in crashes + timeouts],
        'threads_first_last_cycle': {r['seed']: r['threads'] for r in results if r.get('threads')},
    }
    (output / 'hotfix-media-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print('crash rate: %d/%d children crashed (%d SIGSEGV), %d crashes in %d stress cycles = %.2f per 100 cycles; '
          'load average 1m %.1f..%.1f; %d s' % (
              len(crashes), len(results), len(segv), len(crashes), attempted, summary['crashes_per_100_cycles'],
              min(loads), max(loads), summary['seconds']), flush=True)
    for r in crashes + timeouts:
        print('  child %03d exit %s at %s; main thread in %s; GstPlay-thread unref: %s  (log %s)' % (
            r['seed'], r['code'], r['last_step'] or 'start', r.get('frame') or '?', r.get('gstplay_thread'), r['log']))
    grown = [r['threads'] for r in results if r.get('threads')]
    if grown:
        print('INFO threads per stress child, first -> last completed cycle: %s' % ', '.join(
            '%d->%d' % pair for pair in grown), flush=True)
    check('stress: %d children x %d cycles ran without a crash (segfault or other signal)' % (
        len(results), args.cycles), not crashes, '%d crashed: %s' % (len(crashes), summary['crash_steps']))
    check('stress: no child hung in the app (killed after %.0f s before its last step)' % args.child_timeout,
          len(timeouts) == len(exit_hangs), summary['crash_steps'])
    check('stress: no child hung at interpreter exit after closing its windows', not exit_hangs,
          [r['seed'] for r in exit_hangs])
    check('stress: every child finished all its cycles without raising', not errors and cycles == len(results) * args.cycles,
          '; '.join('%03d exit %s: %s' % (r['seed'], r['code'], r['text'][-600:]) for r in errors[:3]))
    passed = sum(1 for _, ok in checks if ok)
    print('%d passed, %d failed' % (passed, len(checks) - passed), flush=True)
    sys.exit(0 if passed == len(checks) else 1)


# ============================================================================ children

def run_child(mode):
    no_core_dumps()
    faulthandler.enable(all_threads=True)
    sys.path.insert(0, str(HERE.parent))
    import o6_env
    env = o6_env.Env(__doc__, '/tmp/listening-hotfix-media-acceptance', extra_args=dict(FLAGS, **{'--repo': {'default': ''}}))
    args = env.args

    began = time.monotonic()

    def step(text):
        print('STEP %s  @%.1fs' % (text, time.monotonic() - began), flush=True)

    with env:
        Gtk, Gdk, GLib = env.Gtk, env.Gdk, env.GLib
        pump, settle = env.pump, env.settle

        def cards(window):
            child, found = window.cards.get_first_child(), []
            while child:
                if child in window.rooms.card_for:
                    found.append(child)
                child = child.get_next_sibling()
            return found

        def click(window, child):
            """The card's own GestureClick, pressed and released once: the real single-click path."""
            overlay = child.get_child()
            gesture = next(c for c in overlay.observe_controllers()
                           if isinstance(c, Gtk.GestureClick) and c.get_button() == 1)
            gesture.emit('pressed', 1, 12., 12.)
            gesture.emit('released', 1, 12., 12.)

        def on_preview(window):
            return (window.rooms.stack.get_visible_child_name() or '').startswith('preview')

        def to_rooms(window):
            window.show_rooms()
            if window.rooms.opened is not None and window.rooms.preview_widgets is None:
                window.rooms.opened = None

        def loop_rooms(window):
            found = []
            for child in cards(window):
                entry = window.rooms.card_for[child]
                preset = next((p for p in window.presets if p.get('id') == entry.get('view_id')), {})
                if preset.get('kind') == 'local' and Path(str(preset.get('url'))).is_file():
                    found.append(child)
            return found

        def wait_idle(seconds=6.):
            deadline = time.monotonic() + seconds
            while env.controller.transition and time.monotonic() < deadline:
                pump(.1)

        if mode == 'stress':
            stress(env, args, step, cards, click, on_preview, to_rooms, loop_rooms, wait_idle)
        elif mode == 'leak':
            lifetime(env, args, step, cards, click, on_preview, to_rooms, loop_rooms)
        else:
            behaviour(env, step, cards, click, on_preview, to_rooms, loop_rooms, wait_idle)
        for window in list(env.app.get_windows()):
            window.close()
        pump(.3)
        step('windows closed; tearing down the fixture')
    step('exiting the process')   # the parent tells a hang after this line apart from a hang in the app


def stress(env, args, step, cards, click, on_preview, to_rooms, loop_rooms, wait_idle):
    pump = env.pump
    import next_rooms_model as rooms_model
    rnd = random.Random(args.seed)
    gaps = (0., 0., .003, .01, .03, .08, .2)

    def launch():
        window = env.launch(page='rooms')
        to_rooms(window)
        pump(.3)
        assert loop_rooms(window), 'no room card with a local scene loop is shown'
        return window

    step('launch')
    window = launch()
    for cycle in range(args.cycles):
        if cycle and args.reopen_every and cycle % args.reopen_every == 0:
            step('%d close window (preview open: %s)' % (cycle, on_preview(window)))
            if rnd.random() < .5 and loop_rooms(window):
                click(window, rnd.choice(loop_rooms(window)))   # close while a preview loop is starting
                pump(rnd.choice(gaps))
            window.close()
            pump(rnd.choice(gaps))
            step('%d reopen window' % cycle)
            window = launch()

        step('%d rapid switching' % cycle)
        to_rooms(window)
        pump(.05)
        for _ in range(rnd.randint(5, 10)):
            pool = loop_rooms(window) or cards(window)
            click(window, rnd.choice(pool))
            pump(rnd.choice(gaps))

        step('%d close preview' % cycle)
        how = rnd.choice(('escape', 'rooms-button', 'animated'))
        if how == 'escape':
            window.key_pressed(None, env.Gdk.KEY_Escape, 0, 0)
        elif how == 'rooms-button' and window.rooms.preview_widgets:
            back = next((b for b in env.buttons(window.rooms.preview_widgets['page'], 'Rooms')), None)
            if back is not None:
                back.emit('clicked')
        else:
            window.rooms.close_preview(animate=True)
        pump(rnd.choice(gaps))

        step('%d open preview, enter' % cycle)
        pool = loop_rooms(window) or cards(window)
        click(window, rnd.choice(pool))
        pump(rnd.choice(gaps))
        if window.rooms.preview_widgets:
            window.rooms.preview_widgets['enter'].emit('clicked')
        pump(rnd.choice((.05, .2, .5)))
        wait_idle()
        pump(rnd.choice(gaps))

        step('%d back to rooms from now, switch again' % cycle)
        if window.current_page() == 'now':
            window.key_pressed(None, env.Gdk.KEY_Escape, 0, 0)   # Back from Now
        to_rooms(window)
        pump(rnd.choice(gaps))
        for _ in range(rnd.randint(2, 4)):
            click(window, rnd.choice(loop_rooms(window) or cards(window)))
            pump(rnd.choice(gaps))

        step('%d enter another room, then Back to the previous one' % cycle)
        if window.rooms.preview_widgets:
            back = rooms_model.previous_room(window.snapshot, window.catalogue)
            window.rooms.preview_widgets['enter'].emit('clicked')
            pump(rnd.choice((.05, .3)))
            wait_idle()
            if back:
                window.rooms.go_back(back)   # the toast's "Back to ..." action
                pump(rnd.choice(gaps))
                wait_idle()

        step('%d leave' % cycle)
        if env.controller.active:
            env.service.execute('leave', {})
        pump(rnd.choice(gaps))
        wait_idle()
        to_rooms(window)
        pump(rnd.choice((0., .05, .3)))
        threads = next((line.split()[1] for line in open('/proc/self/status') if line.startswith('Threads:')), '?')
        print('CYCLE-DONE %d threads %s' % (cycle, threads), flush=True)
    step('done')
    window.close()
    pump(.3)


def lifetime(env, args, step, cards, click, on_preview, to_rooms, loop_rooms):
    """Preview open/close with scene video must not accumulate Scenes, media, threads, fds or memory.

    Bounds, measured after `--leak-cycles` (40) cycles against a baseline taken after 8 warm-up previews
    (every loop room once, so poster textures and plugin loading are paid for), with no preview open:
    live Scenes <= 2 (the room scene plus one preview page awaiting removal), live Gtk.MediaFiles <= 1
    at the baseline and the end (both counted by GObject finalization, and by a
    gc census of Python wrappers), threads <= baseline + 6, fds <= baseline + 12, RSS <= baseline + 64 MB.
    Main leaks about 9 MB, 3 threads and 13 fds per preview, i.e. ~360 MB, ~120 threads, ~520 fds here."""
    import gc
    import next_widgets
    Gtk, GLib = env.Gtk, env.GLib
    pump = env.pump
    failures = [0]

    def check(name, ok, detail=''):
        failures[0] += 0 if ok else 1
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:400]), flush=True)

    alive = {'scene': 0, 'media': 0}
    handles = []

    def track(kind, obj):
        alive[kind] += 1

        def gone(*_):
            alive[kind] -= 1
        handles.append(obj.weak_ref(gone))   # called when the GObject is finalized, not when Python drops it
        return obj

    scene_init = next_widgets.Scene.__init__

    def counting_init(self, *a, **k):
        scene_init(self, *a, **k)
        track('scene', self)
    next_widgets.Scene.__init__ = counting_init
    new_media = Gtk.MediaFile.new_for_filename
    Gtk.MediaFile.new_for_filename = staticmethod(lambda *a, **k: track('media', new_media(*a, **k)))

    def status(key):
        return int(next(line.split()[1] for line in open('/proc/self/status') if line.startswith(key + ':')))

    def sample(label):
        for _ in range(3):
            gc.collect()
            pump(.5)
        census = {name: 0 for name in ('Scene', 'MediaFile')}
        for obj in gc.get_objects():
            if isinstance(obj, next_widgets.Scene):
                census['Scene'] += 1
            elif isinstance(obj, Gtk.MediaFile):
                census['MediaFile'] += 1
        names = {}
        for task in os.listdir('/proc/self/task'):
            try:
                name = open('/proc/self/task/%s/comm' % task).read().strip()
            except OSError:
                continue
            names[name] = names.get(name, 0) + 1
        print('INFO lifetime %s thread names: %s' % (label, json.dumps(dict(sorted(names.items())))), flush=True)
        point = {'scenes': alive['scene'], 'media': alive['media'], 'scene_wrappers': census['Scene'],
                 'media_wrappers': census['MediaFile'], 'threads': status('Threads'),
                 'fds': len(os.listdir('/proc/self/fd')), 'rss_mb': round(status('VmRSS') / 1024., 1)}
        print('INFO lifetime %s: %s' % (label, json.dumps(point)), flush=True)
        return point

    def preview_cycle(child, dwell):
        click(window, child)
        pump(dwell)        # long enough for the loop to start playing
        window.key_pressed(None, env.Gdk.KEY_Escape, 0, 0)
        pump(.35)

    step('launch')
    window = env.launch(page='rooms')
    to_rooms(window)
    pump(.5)
    rooms = loop_rooms(window)
    check('lifetime: rooms with scene loops to preview', len(rooms) >= 2, len(rooms))
    step('warm-up')
    for child in rooms[:8]:
        preview_cycle(child, .8)
    played = []
    click(window, rooms[0])
    pump(1.5)
    widgets = window.rooms.preview_widgets
    played.append(bool(widgets and widgets['scene'].playing_motion))
    window.key_pressed(None, env.Gdk.KEY_Escape, 0, 0)
    pump(2.)
    base = sample('baseline after %d warm-up previews' % (min(8, len(rooms)) + 1))
    halfway = None
    for cycle in range(args.leak_cycles):
        step('leak cycle %d' % cycle)
        preview_cycle(rooms[cycle % len(rooms)], (.5, .8, 1.2)[cycle % 3])
        if (cycle + 1) % 10 == 0 and cycle + 1 < args.leak_cycles:   # partial data survives a crash
            point = sample('after %d cycles' % (cycle + 1))
            halfway = point if cycle + 1 <= args.leak_cycles // 2 or halfway is None else halfway
    pump(3.)
    end = sample('after %d cycles' % args.leak_cycles)
    pump(12.)
    rested = sample('after %d cycles and 12 s idle' % args.leak_cycles)   # reported; lazy release vs a leak
    click(window, rooms[1 % len(rooms)])
    pump(1.5)
    widgets = window.rooms.preview_widgets
    played.append(bool(widgets and widgets['scene'].playing_motion))
    window.key_pressed(None, env.Gdk.KEY_Escape, 0, 0)
    pump(.5)
    per = lambda key: (end[key] - base[key]) / float(args.leak_cycles)
    print('INFO lifetime growth per preview: %.2f MB RSS, %.2f threads, %.2f fds, %.2f Scenes, %.2f MediaFiles' % (
        per('rss_mb'), per('threads'), per('fds'), per('scenes'), per('media')), flush=True)
    check('lifetime: the scene loop really played in a preview before and after the cycles (not vacuous)',
          all(played), played)
    check('lifetime: live Scene GObjects stay flat over %d previews (<= 2: the room scene and at most one '
          'preview page awaiting removal)' % args.leak_cycles, end['scenes'] <= 2 and base['scenes'] <= 2, (base['scenes'], halfway and halfway['scenes'], end['scenes']))
    check('lifetime: Scene wrappers stay flat (gc census <= 2)', end['scene_wrappers'] <= 2, (base['scene_wrappers'], end['scene_wrappers']))
    check('lifetime: at most one live Gtk.MediaFile with no preview open (finalization count)',
          end['media'] <= 1 and base['media'] <= 1, (base['media'], halfway and halfway['media'], end['media']))
    check('lifetime: MediaFile wrappers stay flat (gc census <= 1)', end['media_wrappers'] <= 1,
          (base['media_wrappers'], end['media_wrappers']))
    check('lifetime: threads stay flat (<= baseline + 6)', end['threads'] <= base['threads'] + 6,
          (base['threads'], halfway and halfway['threads'], end['threads']))
    check('lifetime: fds stay flat (<= baseline + 12)', end['fds'] <= base['fds'] + 12,
          (base['fds'], halfway and halfway['fds'], end['fds']))
    check('lifetime: RSS stays flat (<= baseline + 64 MB)', end['rss_mb'] <= base['rss_mb'] + 64,
          (base['rss_mb'], halfway and halfway['rss_mb'], end['rss_mb']))
    print('CHECKS-DONE %d failed' % failures[0], flush=True)


def behaviour(env, step, cards, click, on_preview, to_rooms, loop_rooms, wait_idle):
    Gtk, Gdk = env.Gtk, env.Gdk
    pump, settle = env.pump, env.settle
    from PIL import ImageChops, ImageGrab, ImageStat
    gst_log = Path(os.environ.get('GST_DEBUG_FILE', '/nonexistent'))

    def check(name, ok, detail=''):
        failures[0] += 0 if ok else 1
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:400]), flush=True)

    failures = [0]

    def info(text):
        print('INFO ' + text, flush=True)

    def region(window, widget):
        ok, rect = widget.compute_bounds(window)
        assert ok and rect.get_width() > 20 and rect.get_height() > 20, 'widget not on screen'
        x, y = int(rect.get_x()) + 4, int(rect.get_y()) + 4
        return (x, y, x + int(rect.get_width()) - 8, y + int(rect.get_height()) - 8)

    def painted(scene):
        picture = scene.picture
        return picture.get_paintable() if hasattr(picture, 'get_paintable') else getattr(picture, 'paintable', None)

    def grab(box):
        return ImageGrab.grab(bbox=box, xdisplay=os.environ['DISPLAY']).convert('RGB')

    def motion(window, scene, seconds=2.4):
        """(playing, timestamp advance in seconds, % of scene pixels that changed visibly) across `seconds`."""
        box = region(window, scene)
        media = scene.media
        first_ts = media.get_timestamp() if media is not None else 0
        first = grab(box)
        changed = 0.
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pump(.4)
            diff = ImageChops.difference(first, grab(box)).convert('L').point(lambda v: 255 if v > 10 else 0)
            changed = max(changed, 100. * ImageStat.Stat(diff).mean[0] / 255.)
        media = scene.media
        playing = bool(media is not None and media.get_playing())
        advance = ((media.get_timestamp() - first_ts) / 1e6) if media is not None else 0.
        return playing, advance, changed

    def audio_sinks(since):
        """GStreamer audio sinks created since a byte offset of this process's GST debug log."""
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        try:
            with open(gst_log, 'rb') as handle:
                handle.seek(since)
                text = handle.read().decode(errors='replace')
        except OSError:
            return None
        names = set()
        for line in text.splitlines():
            if 'creating element "' in line:
                names.add(line.split('creating element "', 1)[1].split('"', 1)[0])
        sinks = sorted(name for name in names if (lambda f: f is not None and 'Sink' in (f.get_metadata('klass') or '')
                                                  and 'Audio' in (f.get_metadata('klass') or ''))(Gst.ElementFactory.find(name)))
        return sinks, sorted(names)

    def log_offset():
        try:
            return gst_log.stat().st_size
        except OSError:
            return 0

    started = []
    real_popen = subprocess.Popen

    class CountingPopen(real_popen):
        def __init__(self, argv, *a, **k):
            started.append(argv)
            super().__init__(argv, *a, **k)
    subprocess.Popen = CountingPopen

    step('launch')
    window = env.launch(page='rooms')
    to_rooms(window)
    pump(.5)
    shown = loop_rooms(window)
    import next_widgets
    info('gst_ready=%s system_animations=%s settings=%s' % (next_widgets.gst_ready(), window.system_animations,
                                                           (window.snapshot or {}).get('settings')))
    check('fixture: the Rooms grid shows rooms with a local scene loop', len(shown) >= 2,
          [window.rooms.card_for[c]['name'] for c in cards(window)])

    # ---------------------------------------------------------------- a preview plays its loop, muted and inert
    step('preview loop')
    env.log.clear()
    started.clear()
    offset = log_offset()
    target = shown[0]
    name = window.rooms.card_for[target]['name']
    click(window, target)
    pump(1.5)
    widgets = window.rooms.preview_widgets
    check('preview: a click on "%s" opens its preview' % name, on_preview(window) and widgets is not None)
    scene = widgets['scene']
    check('preview: the scene wants motion and holds a Gtk.MediaFile', scene.want_motion and
          isinstance(scene.media, Gtk.MediaFile), (scene.want_motion, scene.media))
    playing, advance, changed = motion(window, scene)
    info('preview "%s": playing=%s timestamp advance=%.2f s pixels changed=%.2f%%' % (name, playing, advance, changed))
    check('preview: the loop is playing (Gtk.MediaFile.get_playing)', playing)
    check('preview: the loop timestamp advances (frames move)', advance > .5, advance)
    check('preview: the pixels in the scene change on screen', changed > .2, changed)
    check('preview: the loop is muted', scene.media is not None and scene.media.get_muted())
    check('preview: no controller command was sent', not env.commands(), env.commands())
    check('preview: no room is active (nothing started)', env.controller.active is None, env.controller.active)
    check('preview: no player process was started', not started, started)
    found = audio_sinks(offset)
    if found is None:
        check('preview: the GStreamer element log is readable', False, gst_log)
    else:
        sinks, names = found
        info('preview GStreamer elements: %s' % ', '.join(names))
        check('preview: GStreamer created at least one element (the log observes the preview)', bool(names), names)
        check('preview: no GStreamer audio sink was created', not sinks, sinks)

    # ---------------------------------------------------------------- an entered room plays its loop
    step('enter from preview')
    widgets['enter'].emit('clicked')
    settle(1.5)
    wait_idle()
    pump(1.)
    check('entered: Enter room sent one enter and opened Now', window.current_page() == 'now' and
          [a for a, _ in env.commands()] == ['enter'], (window.current_page(), env.commands()))
    scene = window.scene
    check('entered: the room scene holds a playing Gtk.MediaFile', isinstance(scene.media, Gtk.MediaFile))
    playing, advance, changed = motion(window, scene)
    info('entered "%s": playing=%s timestamp advance=%.2f s pixels changed=%.2f%%' % (name, playing, advance, changed))
    check('entered: the loop is playing', playing)
    check('entered: the loop timestamp advances', advance > .5, advance)
    check('entered: the pixels of the room scene change on screen', changed > .2, changed)
    check('entered: the scene loop itself is muted (room sound comes from the controller)',
          scene.media is not None and scene.media.get_muted())

    # ---------------------------------------------------------------- a preview from inside a room is still inert
    step('preview from inside a room')
    window.show_rooms()
    pump(.5)
    mark = len(env.log)
    started.clear()
    other = next((c for c in loop_rooms(window) if window.rooms.card_for[c]['name'] != name), None)
    if other is not None:
        click(window, other)
        pump(1.5)
        check('in-room preview: sends no command and starts no process', not env.commands(mark) and not started,
              (env.commands(mark), started))
        check('in-room preview: its loop plays', window.rooms.preview_widgets is not None and
              window.rooms.preview_widgets['scene'].playing_motion)
        window.rooms.close_preview(animate=False)
        pump(.3)

    # ---------------------------------------------------------------- reduced motion shows the still
    step('reduced motion')
    env.service.execute('settings', {'values': {'reduced_motion': True}})
    settle(1.2)
    window.show_now()
    pump(1.)
    scene = window.scene
    check('reduced motion, entered: no media, the still is a texture', scene.media is None and
          isinstance(painted(scene), Gdk.Texture), (scene.media, painted(scene)))
    playing, advance, changed = motion(window, scene, 1.6)
    info('reduced motion entered: pixels changed=%.2f%%' % changed)
    check('reduced motion, entered: the scene pixels stay still', changed < .02, changed)
    window.show_rooms()
    pump(.4)
    click(window, loop_rooms(window)[0])
    pump(1.2)
    widgets = window.rooms.preview_widgets
    scene = widgets['scene'] if widgets else None
    check('reduced motion, preview: no media, the still is a texture', scene is not None and scene.media is None and
          isinstance(painted(scene), Gdk.Texture))
    if scene is not None:
        playing, advance, changed = motion(window, scene, 1.6)
        info('reduced motion preview: pixels changed=%.2f%%' % changed)
        check('reduced motion, preview: the scene pixels stay still', changed < .02, changed)
        check('reduced motion, preview: the caption says it is a still',
              any('A still of the scene' in t for t in env.texts(widgets['page'])))
    window.rooms.close_preview(animate=False)
    env.service.execute('settings', {'values': {'reduced_motion': False}})
    settle(1.)
    window.show_now()
    pump(1.)
    check('motion back on: the entered room plays its loop again', window.scene.playing_motion)
    subprocess.Popen = real_popen
    print('CHECKS-DONE %d failed' % failures[0], flush=True)


if __name__ == '__main__':
    arguments = parse()
    if arguments.child:
        run_child(arguments.child)
    else:
        if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
            reexec_isolated()
        run_parent(arguments)
