"""O11-MEDIA acceptance: paused loops cost almost nothing, with headroom under load, and no teardown.

Written by the independent acceptance author from the O11-MEDIA brief, the 2026-11-21 performance
assessment (section 6 findings 1, 8 and 9; sections 2 and 4) and the O10 perf closeout ("M4-R1",
"Watch: M2r"), not from the implementation. Real GTK on a private Xvfb with its own D-Bus (this file
re-executes itself there once), the real session controller on the session test double and fixture
providers. Nothing reaches the desktop session, the network, systemd or credentials; no audio is played
on the host; children disable core dumps.

The cost measurements reuse the O10 perf probe's method (tests/probes/listening-o10p-acceptance.py: whole
process tree RSS and threads from /proc, gc and settle before each point, every loop must visibly play
first), imported from that file so the numbers compare with 2.5.0's.

Parts (each child is a subprocess, so a segfault is a result):

  pages   finding 8: GTK-thread CPU of the Listen, Now and Mix switches, measured as the assessment's
          perfprobe did (from the action to the next frame painted, n = 10 per page), recorded and guarded
          against regression from 2.5.0 (F8r).
  media   criteria 1-3 and 5 in one window; the cost under host load (the parent adds CPU-bound helper processes
          at nice 19, outside the window's process tree, until the 1-minute load is at least 20, and stops them
          as soon as the cost samples are taken):
            - every catalogue loop is previewed once (it must visibly play) and left; after each one the
              cost is sampled. Criterion 3 judges it the O10 way (first loop, then per loop, then total)
              AND as a robust slope (Theil-Sen over the 8 points), so a single low sample cannot pass it.
            - with all 8 loops off screen (up to 15 s to let go), the threads the media still holds:
              all added threads, and the GStreamer streaming threads by name. Criterion 1's memory is the
              slope of the off-screen samples: the memory each paused loop keeps.
            - returning to loops that were let go (4 previews and Now): an external grabber process
              (no GIL shared with the window) samples the scene at about 30 Hz for black or blank frames,
              and every GTK main-loop dispatch is timed; a stall is its wall time minus the time the GTK
              thread waited for a CPU (schedstat run-queue wait), so host load does not count as a stall.
            - a resumed loop keeps looping past its clip length; reduced motion on a released loop shows
              the still and acquires nothing; resuming is inert (no command, process or audio sink).
            - 24 more previews: Python GObject wrappers per preview (finding 9).
            - no teardown, observed three ways while the window lives: no GstPlay thread ever goes away
              (a disposed GstPlay joins its thread), no player pipeline is built twice for a loop file
              (GST_DEBUG element creation), and no Gtk.MediaFile with an open file is cleared, re-opened
              or finalized (Python hooks on Gtk.MediaFile: the calls that tear GstPlay down).
  stress  criterion 2: tests/probes/listening-scene-media-stress.py, 10 children x 30 cycles = 300 cycles,
          3 at a time, at the host's own load (it must be above 15 for part of the run); every signal is a crash.
  notes   the CHANGELOG records the change (a source-text check: documentation has no behaviour).

  --inject teardown   DIAGNOSTIC ONLY, never in acceptance: on every leave, the probe itself clears the
                      scene's Gtk.MediaFile and re-opens its file (a "release by teardown"), to show the
                      no-teardown oracles catch that design without relying on the stress run.

Writes OUTPUT/o11m-acceptance.json (judged by acceptance_verdict.py against expected/o11m.checks) and
OUTPUT/o11m-summary.json with every measurement, the load samples and uptime. Ends with "N passed, M failed".

  scripts/listening-dev.sh acceptance OUT o11m, or scripts/listening-dev.sh probe OUT listening-o11m-acceptance
  (LISTENING_O11M_ARGS='--only pages,media,stress,notes' for a focused look; the full run takes about 12 minutes)

Criterion revisions are made in place, keeping the original visible as "[ORIGINAL, superseded by ...]".
"""
import argparse
import collections
import faulthandler
import gc
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
_spec = importlib.util.spec_from_file_location('o10p_method', str(HERE.parent / 'listening-o10p-acceptance.py'))
o10p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(o10p)   # definitions only; its main is guarded

FLAGS = {
    '--child': {'default': ''},             # internal: pages, media, grabber
    '--only': {'default': ''},              # comma list: setup,pages,media,stress,notes
    '--inject': {'default': ''},            # DIAGNOSTIC: teardown
    '--load-target': {'type': float, 'default': 20.},
    '--stress-runs': {'type': int, 'default': 10},
    '--stress-cycles': {'type': int, 'default': 30},
    '--stress-jobs': {'type': int, 'default': 3},
    '--box': {'default': ''},               # grabber: x0,y0,x1,y1
    '--seconds': {'type': float, 'default': 3.},
    '--profile-stalls': {'action': 'store_true'},   # DIAGNOSTIC: sample the GTK thread's Python stack during resumes
}

# ---------------------------------------------------------------------------- thresholds
# From the brief:
PER_LOOP_MB = 40          # criterion 3: per-loop cost after the first <= 40 MB at load 20+
TOTAL_MB = 400            # criterion 3: total for the 8 loops < 400 MB
LOAD_TARGET = 20.         # criterion 3: "at load 20+"
RESUME_STALL_MS = 100     # criterion 1: no stall over 100 ms on the GTK thread when returning to a loop
STRESS_CYCLES = 300       # criterion 2: at least 300 stress cycles, zero crashes
STRESS_LOAD = 15.         # criterion 2: host load above 15 for part of the run
CLIP_S = 20.              # the catalogue loops are 20 s (480 frames at 24 fps; o10p S5)
# Set by the acceptance author (FLAGGED FOR A RULING in the phase-1 report). "Well below 2.5.0" made concrete:
# 2.5.0 holds +80 threads for 8 paused loops, 10 per loop: GstPlay, gldisplay-event, gstglcontext and 7 streaming
# threads (typefind x2, qtdemux, multiqueue x2, vqueue, video-dmabuf-po); and 42-49 MB per loop after the first.
RELEASE_WAIT_S = 15       # author: how long a loop may take to let go once off screen
PAUSED_THREADS_MAX = 24   # author: all threads the media adds with 8 loops off screen, <= 3 per loop (2.5.0: +80).
                          # 3 per loop leaves room for a design that keeps each Gtk.MediaFile (its GstPlay thread
                          # and GL display/context threads) and drops only the pipeline's streaming threads.
STREAMING_MAX = 7         # author: GStreamer streaming threads with 8 loops off screen: at most one loop's set,
                          # so one loop may stay warm for a quick return (2.5.0: 56)
PAUSED_MB_PER_LOOP = 10.  # author: memory a paused loop keeps, as the Theil-Sen slope over the 8 off-screen samples:
                          # its still (1440x810 RGBA = 4.7 MB) plus about 5 MB (2.5.0: 42-49 MB per loop)
WRAPPERS_PER_PREVIEW = .1  # author: finding 9 (1.2 per preview without video, +3 with) is gone when 20 previews
                           # leave at most 2 more live Python GObject wrappers
# Finding 8 (author, from the assessment's old build 0750cc4, the cost O9 doubled): median GTK-thread CPU per
# switch at most old's median plus 25%. Listen 11.5, Now 19.2, Mix 8.8 ms on old.
PAGE_CPU_MS = {'Listen': 14.4, 'Now': 24.0, 'Mix': 11.0}   # [ORIGINAL, superseded by F8r]
# Revision F8r (coordinator ruling, phase 1, O11-DECISIONS.md): the assessment's harness differs and Listen flakes
# around its old-build limit on main, so F8r records each page's median and guards against regression: no worse than
# 2.5.0's measured median plus 25%. Baseline: the worse of the two phase-1 main runs (70db15e, load 16-27):
# Listen 14.5 / 12.9, Now 16.9 / 14.1, Mix 12.4 / 12.9 ms -> Listen 14.5, Now 16.9, Mix 12.9 ms.
PAGE_CPU_BASE_MS = {'Listen': 14.5, 'Now': 16.9, 'Mix': 12.9}
PAGE_CPU_R_MS = {page: round(value * 1.25, 1) for page, value in PAGE_CPU_BASE_MS.items()}   # 18.1, 21.1, 16.1

STREAMING = re.compile(r'^(typefind|multiqueue|qtdemux|matroskadem|vqueue|aqueue|queue\d|video-dmabuf|'
                       r'.*:(src|sink|sr|si|s)$)')
DECODER_NAME, GL_NAME = o10p.DECODER_NAME, o10p.GL_NAME
PLAYER_ELEMENTS = o10p.PIPELINE_ELEMENTS
GST_READY = o10p.GST_READY
fill, uptime, no_core_dumps, tree, threads_cpu = o10p.fill, o10p.uptime, o10p.no_core_dumps, o10p.tree, o10p.threads_cpu


def parse():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source', default=str(REPO / 'src'))
    parser.add_argument('--output', default='/tmp/listening-o11m-acceptance')
    parser.add_argument('--repo', default='')
    for flag, kwargs in FLAGS.items():
        parser.add_argument(flag, **kwargs)
    # The dev script's `probe` passes no arguments; LISTENING_O11M_ARGS carries them for a focused or diagnostic run.
    import shlex
    return parser.parse_known_args(sys.argv[1:] + shlex.split(os.environ.get('LISTENING_O11M_ARGS', '')))[0]


def theil_sen(points):
    """Median of the pairwise slopes: robust to a minority of outlying samples (RSS noise under load)."""
    slopes = [(b[1] - a[1]) / float(b[0] - a[0]) for i, a in enumerate(points) for b in points[i + 1:] if b[0] != a[0]]
    return statistics.median(slopes) if slopes else float('nan')


def thread_names(pids=None):
    return [name for name, _ in threads_cpu(pids).values()]


def streaming_count(names):
    return sum(1 for n in names if STREAMING.match(n) or DECODER_NAME.search(n))


def die_with_parent():
    """A load helper never outlives the probe, even when the probe is killed."""
    import ctypes
    no_core_dumps()
    os.setpriority(os.PRIO_PROCESS, 0, 19)     # nice 19 (coordinator ruling): the load average still counts it
    ctypes.CDLL(None).prctl(1, 15, 0, 0, 0)    # PR_SET_PDEATHSIG = 1, SIGTERM


def schedstat_wait_s():
    """Time this thread has waited on a run queue (runnable, no CPU), in seconds."""
    try:
        return int(open('/proc/thread-self/schedstat').read().split()[1]) / 1e9
    except (OSError, IndexError, ValueError):
        return 0.


# ============================================================================ parent

def reexec_isolated():
    runtime = tempfile.mkdtemp(prefix='listening-o11m-runtime-')
    os.chmod(runtime, 0o700)
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'HYPRLAND_INSTANCE_SIGNATURE',
                                  'PULSE_SERVER', 'PIPEWIRE_REMOTE')}
    environment.update(XDG_RUNTIME_DIR=runtime, GDK_BACKEND='x11', GDK_DEBUG='no-portals', GSK_RENDERER='cairo',
                       LISTENING_PROBE_ISOLATED='1')
    command = ['xvfb-run', '-a', '-s', '-screen 0 1440x1000x24', 'dbus-run-session', sys.executable] + sys.argv
    try:
        sys.exit(subprocess.call(command, env=environment))
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


class Load:
    """CPU-bound helper processes (outside the window's process tree) until the 1-minute load reaches a target."""
    def __init__(self, info):
        self.info, self.procs = info, []

    def raise_to(self, target, wait_s=240):
        started, last_add = time.monotonic(), 0.
        while time.monotonic() - started < wait_s:
            load = os.getloadavg()[0]
            if load >= target:
                break
            # The 1-minute load converges slowly: start enough helpers to settle a little above the target at
            # once, and add a few more every 60 s while it is still short. At most 32.
            if not self.procs or time.monotonic() - last_add > 60:
                add = int(target + 4 - load) if not self.procs else 4
                for _ in range(max(1, min(add, 32 - len(self.procs)))):
                    self.procs.append(subprocess.Popen([sys.executable, '-c', 'while True: pass'],
                                                       preexec_fn=die_with_parent))
                last_add = time.monotonic()
            time.sleep(5)
        self.info('load helpers: %d running, load 1m %.1f after %.0f s (target %.0f)' % (
            len(self.procs), os.getloadavg()[0], time.monotonic() - started, target))
        return os.getloadavg()[0]

    def stop(self):
        for proc in self.procs:
            proc.kill()
        for proc in self.procs:
            proc.wait()
        self.procs = []


def run_parent(args):
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit('terminated'))   # runs the finally that stops load helpers
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checks, summary = [], {'uptime_start': uptime(), 'cpus': os.cpu_count(), 'source': str(Path(args.source).resolve()),
                           'renderer': os.environ.get('GSK_RENDERER'), 'load_samples': [], 'inject': args.inject}

    def check(name, ok, detail='', measured=()):
        name = fill(name, measured)
        checks.append({'criterion': name, 'pass': bool(ok), 'detail': '' if ok else str(detail)[:1500]})
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:700]), flush=True)

    def info(text):
        print('INFO ' + text, flush=True)

    def note_load(label):
        summary['load_samples'].append((label, round(time.time()), os.getloadavg()))

    def child(mode, timeout=900, load_done=None):
        log = output / ('%s.log' % mode)
        gst = output / ('%s.gst' % mode)
        environment = dict(os.environ, PYTHONFAULTHANDLER='1', GST_DEBUG='GST_ELEMENT_FACTORY:4',
                           GST_DEBUG_FILE=str(gst), GST_DEBUG_NO_COLOR='1')
        runtime = tempfile.mkdtemp(prefix='o11m-child-', dir=os.environ.get('XDG_RUNTIME_DIR'))
        environment['XDG_RUNTIME_DIR'] = runtime
        command = ['dbus-run-session', '--', sys.executable, str(HERE), '--child', mode, '--source', args.source,
                   '--output', str(output / mode), '--inject', args.inject]
        started = time.monotonic()
        with open(log, 'w') as handle:
            process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=environment,
                                       preexec_fn=no_core_dumps)
            marker = output / mode / 'load-done'
            while True:
                try:
                    code = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if load_done is not None and marker.exists():
                        load_done()          # the child measured what needs load; the rest runs at the host's own
                        load_done = None
                    if time.monotonic() - started > timeout:
                        process.kill()
                        process.wait()
                        code = 'timeout'
                        break
        shutil.rmtree(runtime, ignore_errors=True)
        if isinstance(code, int) and code > 128:
            code = -(code - 128)
        try:
            gst.unlink()
        except OSError:
            pass
        text = log.read_text(errors='replace')
        result = {}
        for line in text.splitlines():
            if line.startswith('RESULT '):
                result = json.loads(line[7:])
            elif line.startswith('INFO '):
                print(line, flush=True)
        info('%s child: exit %s in %.0f s; %s' % (mode, code, time.monotonic() - started, uptime()))
        for line in text.splitlines():
            if line.startswith(('PASS ', 'FAIL ')):
                name, _, detail = line[5:].partition('  ')
                check(name, line.startswith('PASS '), detail)
            elif line.startswith('SUPERSEDED '):
                print(line, flush=True)
        crashed = not isinstance(code, int) or code < 0
        check('%s: the child finished without a crash or hang' % mode, not crashed, 'exit %s\n%s' % (code, text[-1200:]))
        check('%s: the child reported all its oracles and exited 0' % mode,
              code == 0 and 'CHECKS-DONE' in text, 'exit %s\n%s' % (code, text[-1200:]))
        return result

    print('source: %s  renderer: %s  cpus: %d' % (summary['source'], summary['renderer'], os.cpu_count()), flush=True)
    print('uptime at start: %s' % summary['uptime_start'], flush=True)
    if args.inject:
        print('DIAGNOSTIC RUN: --inject %s (not an acceptance run)' % args.inject, flush=True)
    parts = set(filter(None, args.only.split(','))) or {'setup', 'pages', 'media', 'stress', 'notes'}
    media_ready = subprocess.call([sys.executable, '-c', GST_READY], stderr=subprocess.DEVNULL) == 0
    if 'setup' in parts:
        check('S1 the dev shell provides GStreamer playbin3 to the window (scene loops can play)', media_ready,
              'no playbin3: every media oracle below is void')

    if 'pages' in parts:
        note_load('pages')
        summary['pages'] = child('pages', timeout=420)

    load = Load(info)
    try:
        # Coordinator ruling (phase 1): the helpers run at nice 19 and only for the cost measurements: raised just
        # before the media child (the 1-minute load needs a minute or two to get there) and stopped when the child
        # says its cost samples are taken. The stress run uses the host's own load and its own children.
        if 'media' in parts:
            note_load('before load')
            load.raise_to(args.load_target)
            note_load('load raised')
        if 'media' in parts:
            summary['media'] = child('media', timeout=1500, load_done=load.stop)
            note_load('after media')

        # ------------------------------------------------------------ criterion 2: stress, zero crashes
        if 'stress' in parts:
            stress_out = output / 'stress'
            stress_out.mkdir(exist_ok=True)
            runs, loads, active = [], [], []
            seeds = list(range(args.stress_runs))
            started = time.monotonic()

            def launch(seed):
                log = open(stress_out / ('stress-%d.log' % seed), 'w')
                runtime = tempfile.mkdtemp(prefix='o11m-stress-', dir=os.environ.get('XDG_RUNTIME_DIR'))
                environment = dict(os.environ, PYTHONFAULTHANDLER='1', XDG_RUNTIME_DIR=runtime)
                command = ['dbus-run-session', '--', sys.executable, str(HERE.parent / 'listening-scene-media-stress.py'),
                           '--source', args.source, '--output', str(stress_out / ('run-%d' % seed)),
                           '--cycles', str(args.stress_cycles), '--seed', str(1100 + seed)]
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment,
                                        preexec_fn=no_core_dumps)
                return {'seed': seed, 'proc': proc, 'log': log, 'runtime': runtime, 'start': time.monotonic()}

            while seeds or active:
                while seeds and len(active) < args.stress_jobs:
                    active.append(launch(seeds.pop(0)))
                time.sleep(1)
                loads.append(os.getloadavg()[0])
                for item in list(active):
                    code = item['proc'].poll()
                    if code is None and time.monotonic() - item['start'] > 600:
                        item['proc'].kill()
                        code = 'timeout'
                        item['proc'].wait()
                    if code is None:
                        continue
                    active.remove(item)
                    item['log'].close()
                    shutil.rmtree(item['runtime'], ignore_errors=True)
                    if isinstance(code, int) and code > 128:
                        code = -(code - 128)
                    text = (stress_out / ('stress-%d.log' % item['seed'])).read_text(errors='replace')
                    survived = re.search(r'scene media stress: (\d+) cycles .*?(\d+) previews with a loop, survived', text)
                    runs.append({'seed': item['seed'], 'code': code, 'survived': bool(survived),
                                 'cycles': int(survived.group(1)) if survived else 0,
                                 'loop_previews': int(survived.group(2)) if survived else 0,
                                 'tail': '' if survived else text[-600:]})
            note_load('after stress')
            crashed = [r for r in runs if not isinstance(r['code'], int) or r['code'] < 0]
            cycles = sum(r['cycles'] for r in runs)
            attempted = args.stress_runs * args.stress_cycles
            summary['stress'] = {'runs': runs, 'load1': loads, 'seconds': round(time.monotonic() - started)}
            info('stress: %d runs x %d cycles in %.0f s; crashed %s; load 1m min %.1f max %.1f; exits %s' % (
                args.stress_runs, args.stress_cycles, time.monotonic() - started, [(r['seed'], r['code']) for r in crashed],
                min(loads or [0]), max(loads or [0]), [r['code'] for r in runs]))
            check('X1 stress: at least %d scene-media stress cycles ran with scene loops (@@ attempted, @@ completed)'
                  % STRESS_CYCLES, media_ready and attempted >= STRESS_CYCLES and cycles >= STRESS_CYCLES
                  and all(r['loop_previews'] > 0 for r in runs if r['survived']),
                  (cycles, 'playbin3 %s' % media_ready, [r['tail'] for r in runs if not r['survived']][:2]),
                  measured=(attempted, cycles))
            check('X2 stress: zero crashes over those cycles (@@ crashed children)', media_ready and runs and not crashed,
                  [(r['seed'], r['code'], r['tail']) for r in crashed], measured=len(crashed))
            check('X3 stress: the host load was above %d for part of the run (peak @@)' % STRESS_LOAD,
                  max(loads or [0]) > STRESS_LOAD, loads[-20:], measured=float(round(max(loads or [0]), 1)))
            check('X4 stress: every stress child finished all its cycles without raising',
                  runs and all(r['survived'] and r['code'] == 0 for r in runs),
                  [(r['seed'], r['code'], r['tail']) for r in runs if not (r['survived'] and r['code'] == 0)])
    finally:
        load.stop()

    # ---------------------------------------------------------------- criterion 5: CHANGELOG
    if 'notes' in parts:
        # A source-text check, on purpose: the CHANGELOG is documentation, it has no behaviour to drive.
        text = (REPO / 'CHANGELOG.md').read_text()
        head = text.split('## [2.5.0]', 1)[0]
        newest = head.split('\n## [', 1)[1] if '\n## [' in head else ''
        body = newest.split('\n', 1)[1] if '\n' in newest else ''
        check('N1 the CHANGELOG records the change: the section above 2.5.0 says what happens to loops off screen',
              re.search(r'\bloops?\b', body, re.I) is not None, newest[:400] or 'no section above 2.5.0')

    summary['uptime_end'] = uptime()
    summary['checks'] = checks
    (output / 'o11m-summary.json').write_text(json.dumps(summary, indent=2, default=str) + '\n')
    (output / 'o11m-acceptance.json').write_text(json.dumps(checks, indent=2) + '\n')
    print('uptime at end: %s' % summary['uptime_end'], flush=True)
    passed = sum(1 for c in checks if c['pass'])
    print('%d passed, %d failed' % (passed, len(checks) - passed), flush=True)
    sys.exit(0 if passed == len(checks) else 1)


# ============================================================================ children

def run_grabber(args):
    """A separate process sampling a screen box: prints 't mean stddev' of its luma, so the window's GIL is untouched."""
    from PIL import ImageGrab, ImageStat
    box = tuple(int(v) for v in args.box.split(','))
    print('ready', flush=True)
    start = time.monotonic()
    while time.monotonic() - start < args.seconds:
        t = time.monotonic()
        image = ImageGrab.grab(bbox=box, xdisplay=os.environ['DISPLAY']).convert('L')
        image = image.resize((max(1, image.width // 4), max(1, image.height // 4)))
        stat = ImageStat.Stat(image)
        print('%.4f %.2f %.2f' % (t - start, stat.mean[0], stat.stddev[0]), flush=True)
        time.sleep(max(0., .033 - (time.monotonic() - t)))


def run_child(mode, args):
    no_core_dumps()
    faulthandler.enable(all_threads=True)
    sys.path.insert(0, str(HERE.parent))
    import o6_env
    env = o6_env.Env(__doc__, '/tmp/listening-o11m-acceptance', extra_args=dict(FLAGS, **{'--repo': {'default': ''}}))
    began = time.monotonic()
    failures = [0]

    def step(text):
        print('STEP %s  @%.1fs' % (text, time.monotonic() - began), flush=True)

    def check(name, ok, detail='', measured=()):
        name = fill(name, measured)
        failures[0] += 0 if ok else 1
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:500]), flush=True)

    def info(text):
        print('INFO ' + text, flush=True)

    with env:
        result = (pages if mode == 'pages' else media)(env, step, check, info)
        print('RESULT ' + json.dumps(result, default=str), flush=True)
        for window in list(env.app.get_windows()):
            window.close()
        env.pump(.3)
        print('CHECKS-DONE %d failed' % failures[0], flush=True)
    step('exiting the process')


# ---------------------------------------------------------------------------- child: page switches (finding 8)

def pages(env, step, check, info):
    ctx = env.context

    def watch_until(cond, timeout=5.):
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            ctx.iteration(False) or time.sleep(.001)
        return cond()

    def frame_after(window):
        clock = window.get_frame_clock()
        painted = []
        handler = clock.connect('after-paint', lambda *_: painted.append(time.monotonic()))
        window.queue_draw()
        deadline = time.monotonic() + 2.
        while not painted and time.monotonic() < deadline:
            ctx.iteration(False) or time.sleep(.001)
        clock.disconnect(handler)
        return bool(painted)

    def measure(act, cond):
        """GTK-thread CPU from the action to the next frame painted (the assessment's perfprobe method)."""
        c0 = time.thread_time()
        act()
        ok = watch_until(cond) and frame_after(window)
        return round((time.thread_time() - c0) * 1000, 1) if ok else None

    step('launch')
    window = env.launch(page='rooms')
    env.start_station_room()
    env.pump(2.5)
    window.show_rooms()
    env.pump(1.)
    browse = lambda name: lambda: window.current_page() == 'browse' and window.browse.get_visible_child_name() == name
    cost = collections.defaultdict(list)
    for _ in range(10):
        measure(window.show_rooms, browse('rooms'))
        env.pump(.3)
        cost['Listen'].append(measure(window.show_listen, browse('listen')))
        env.pump(.3)
        cost['Now'].append(measure(window.show_now, lambda: window.current_page() == 'now'))
        env.pump(.3)
        cost['Mix'].append(measure(window.open_mix, lambda: window.sheet.get_open()))
        env.pump(.3)
        window.sheet.set_open(False)
        env.pump(.2)
    load = os.getloadavg()[0]
    medians = {page: (statistics.median(v) if v and None not in v else None) for page, v in cost.items()}
    info('page switch GTK-thread CPU (ms, to the next frame), load 1m %.1f: %s; medians %s' % (
        load, dict(cost), medians))
    for page in ('Listen', 'Now', 'Mix'):
        median = medians.get(page)
        action = 'opening' if page == 'Mix' else 'switching to'
        check('F8r %s %s: median GTK-thread CPU recorded and no worse than 2.5.0\'s %.1f ms + 25%% = %.1f ms '
              '(median of 10, @@ ms)' % (action, page, PAGE_CPU_BASE_MS[page], PAGE_CPU_R_MS[page]),
              median is not None and median <= PAGE_CPU_R_MS[page], cost[page], measured=float(median or -1))
        # F8 [ORIGINAL] still runs and prints as a SUPERSEDED line, not counted (Listen lands either side of 14.4 ms).
        print('SUPERSEDED %s F8 [ORIGINAL, superseded by F8r] %s %s costs at most %.1f ms GTK-thread CPU (median of 10, '
              '%s ms)' % ('PASS' if median is not None and median <= PAGE_CPU_MS[page] else 'FAIL', action, page,
                          PAGE_CPU_MS[page], median), flush=True)
    return {'page_cpu_ms': dict(cost), 'medians': medians, 'load1': load, 'uptime': uptime()}


# ---------------------------------------------------------------------------- child: media

class Teardown:
    """Python hooks on Gtk.MediaFile: every constructor registers a finalize notify (no strong reference is
    kept), and the calls that drop an open MediaFile's GstPlay (clear, or setting a new input) are counted.
    A source-of-truth for the 2.3.2 crash class, which the stress run only finds by luck."""
    SETTERS = ('clear', 'set_file', 'set_filename', 'set_resource', 'set_input_stream')
    CONSTRUCTORS = ('new', 'new_for_file', 'new_for_filename', 'new_for_resource', 'new_for_input_stream')

    def __init__(self, Gtk):
        self.created, self.finalized, self.dropped, self.refs = 0, [], [], []
        self.window_alive = True
        cls = Gtk.MediaFile
        for name in self.CONSTRUCTORS:
            original = getattr(cls, name, None)
            if original is not None:
                setattr(cls, name, staticmethod(self._constructor(original, name)))
        for name in self.SETTERS:
            original = getattr(cls, name, None)
            if original is not None:
                setattr(cls, name, self._setter(original, name))

    def _constructor(self, original, name):
        def made(*args):
            media = original(*args)
            self.created += 1
            label = '%s(%s)' % (name, ', '.join(Path(str(a)).name for a in args))
            self.refs.append(media.weak_ref(self._gone, label))
            return media
        return made

    def _gone(self, label):
        if self.window_alive:
            self.finalized.append(label)

    def _setter(self, original, name):
        def call(media, *args):
            opened = media.get_file() is not None or media.get_input_stream() is not None
            if opened and self.window_alive:
                path = media.get_file().get_path() if media.get_file() is not None else 'stream'
                self.dropped.append('%s on %s' % (name, Path(str(path)).name))
            return original(media, *args)
        return call


def media(env, step, check, info):
    import next_widgets
    ctx = env.context
    pump = env.pump
    Gtk = env.Gtk
    inject = env.args.inject == 'teardown'
    hooks = Teardown(Gtk)
    gst_log = Path(os.environ.get('GST_DEBUG_FILE', '/nonexistent'))
    result = {'uptime_start': uptime(), 'inject': env.args.inject}

    def created_elements(since=0):
        try:
            with open(gst_log, 'rb') as handle:
                handle.seek(since)
                text = handle.read().decode(errors='replace')
        except OSError:
            return []
        return [line.split('creating element "', 1)[1].split('"', 1)[0]
                for line in text.splitlines() if 'creating element "' in line]

    def log_offset():
        try:
            return gst_log.stat().st_size
        except OSError:
            return 0

    def audio_sinks(names):
        try:
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
            Gst.init(None)
        except (ValueError, ImportError):
            return ['unknown']
        found = []
        for name in set(names):
            factory = Gst.ElementFactory.find(name)
            klass = (factory.get_metadata('klass') or '') if factory else ''
            if 'Sink' in klass and 'Audio' in klass:
                found.append(name)
        return sorted(found)

    # -------------------------------------------------- GstPlay thread identities, sampled for the whole run
    sampler = {'seen': set(), 'lost': set(), 'pause': threading.Event(), 'stop': threading.Event()}

    def gstplay_tids():
        found = set()
        for tid in os.listdir('/proc/self/task'):
            try:
                if open('/proc/self/task/%s/comm' % tid).read().strip() == 'GstPlay':
                    found.add(int(tid))
            except OSError:
                pass
        return found

    def sample_players():
        while not sampler['stop'].wait(.25):
            if sampler['pause'].is_set():
                continue
            now = gstplay_tids()
            if hooks.window_alive:
                sampler['lost'] |= sampler['seen'] - now
            sampler['seen'] |= now
    threading.Thread(target=sample_players, daemon=True).start()

    def measure(label, repeats=1):
        """The O10 method (gc, settle, whole-tree sample); repeats > 1 keeps the lowest RSS (for the cold baseline)."""
        points = []
        for _ in range(repeats):
            for _ in range(2):
                gc.collect()
                pump(.4)
            point, names = o10p.sample(label)
            point['streaming'] = streaming_count(thread_names())
            point['gstplay'] = len(gstplay_tids())
            points.append((point, names))
        point, names = min(points, key=lambda p: p[0]['rss_mb'])
        info('media %s: %s' % (label, json.dumps(point)))
        info('media %s thread names: %s' % (label, json.dumps(names)))
        return point

    def scene_media():
        scene = rooms.scene_widget()
        _, paintable = o10p.largest_paintable(scene) if scene else (None, None)
        return paintable if isinstance(paintable, Gtk.MediaFile) else None

    def leave():
        """Esc off the preview; with --inject teardown, the 'release by teardown' design (DIAGNOSTIC)."""
        victim = scene_media() if inject else None
        rooms.escape()
        pump(.35)
        if victim is not None and victim.get_file() is not None:
            path = victim.get_file().get_path()
            victim.clear()
            victim.set_filename(path)

    def preview(card, wait=6.):
        rooms.click(card)
        moved, pct, after = rooms.moves(wait)
        box = rooms.scene_box()[1]
        leave()
        return moved, round(pct, 2), round(after, 1), box

    def wait_released(limit_s):
        """Up to limit_s for the media's threads to drop to the allowance; the sample when they did (or at the end)."""
        start = time.monotonic()
        while True:
            pump(1.)
            names = thread_names()
            added = len(names) - cold['threads']
            if (added <= PAUSED_THREADS_MAX and streaming_count(names) <= STREAMING_MAX) or \
                    time.monotonic() - start > limit_s:
                return time.monotonic() - start

    def watch(seconds, box, act):
        """Run `act`, then the main loop for `seconds`, while a grabber process samples `box`.
        Returns (worst stall ms without run-queue wait, worst wall ms, frames [(t, mean, std)], act at t)."""
        sampler['pause'].set()
        grabber = subprocess.Popen([sys.executable, str(HERE), '--child', 'grabber', '--box', ','.join(map(str, box)),
                                    '--seconds', str(seconds + 1.)], stdout=subprocess.PIPE, text=True)
        grabber.stdout.readline()           # 'ready'
        grabbed_at = time.monotonic()
        pump(.15)
        worst = [0., 0., 0.]          # stall (wall minus run-queue wait), wall, GTK-thread CPU of that dispatch
        slow, stacks, done = [], [], threading.Event()
        if env.args.profile_stalls:      # DIAGNOSTIC only: the sampler shares the GIL with the window
            main = threading.main_thread().ident

            def profile():
                src = str(Path(env.args.source).resolve())
                while not done.wait(.01):
                    frame, where = sys._current_frames().get(main), []
                    while frame is not None and len(where) < 4:
                        if frame.f_code.co_filename.startswith(src):
                            where.append('%s:%d %s' % (Path(frame.f_code.co_filename).name, frame.f_lineno,
                                                       frame.f_code.co_name))
                        frame = frame.f_back
                    stacks.append((time.monotonic(), where))
            threading.Thread(target=profile, daemon=True).start()

        def timed(run):
            t0, w0, c0 = time.monotonic(), schedstat_wait_s(), time.thread_time()
            value = run()
            t1, w1, c1 = time.monotonic(), schedstat_wait_s(), time.thread_time()
            stall = max(0., (t1 - t0) - (w1 - w0))
            if stall > worst[0]:
                worst[:] = [stall, t1 - t0, c1 - c0]
            if stall > .05:
                seen = collections.Counter(' < '.join(w) for t, w in stacks if t0 <= t <= t1 and w)
                slow.append({'at_s': round(t0 - grabbed_at, 3), 'stall_ms': round(stall * 1000, 1),
                             'cpu_ms': round((c1 - c0) * 1000, 1), 'stacks': seen.most_common(4)})
            return value, t0, t1
        _, t0, t1 = timed(act)
        acted = t0 - grabbed_at
        end = t1 + seconds
        while time.monotonic() < end:
            dispatched = timed(lambda: ctx.iteration(False))[0]
            if not dispatched:
                time.sleep(.002)
        done.set()
        if slow:
            info('dispatches over 50 ms around this resume: %s' % json.dumps(slow))
        text = grabber.communicate(timeout=10)[0]
        sampler['pause'].clear()
        frames = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 3:
                frames.append(tuple(float(p) for p in parts))
        return worst[0] * 1000, (worst[1] * 1000, worst[2] * 1000), frames, acted

    def blank_frames(frames, acted):
        """Frames after the action that are black or blank: uniform (no picture), or far darker than the scene
        once it plays (the median of the last third)."""
        after = [f for f in frames if f[0] >= acted]
        if len(after) < 6:
            return None, after
        # Revision of the phase-1 draft (before any report): "far darker than the scene" flagged the grid-to-preview
        # crossfade of a bright scene (mean 47, stddev 37) on main. A black or blank frame has no picture: nearly
        # uniform (stddev < 6), or black (mean < 12 with stddev < 12).
        bad = [f for f in after if f[2] < 6. or (f[1] < 12. and f[2] < 12.)]
        return bad, after

    # -------------------------------------------------- launch
    step('launch')
    window = env.launch(page='rooms')
    window.show_rooms()
    pump(2.)
    rooms = o10p.Rooms(env, window)
    catalogue = rooms.loop_cards()
    files = {rooms.loop_of(c) for c in catalogue}
    check('M0 fixture: 8 catalogue loop rooms with 8 distinct loop files', len(catalogue) == 8 and len(files) == 8,
          (len(catalogue), len(files)))
    cold = measure('cold, before any preview', repeats=3)
    result['cold'] = cold
    offset0 = log_offset()

    # -------------------------------------------------- every catalogue loop once, a sample after each
    step('every catalogue loop once')
    played, points, box = [], [], None
    for index, card in enumerate(catalogue):
        moved, pct, after, shown_box = preview(card)
        box = box or shown_box
        played.append((rooms.entry(card)['name'], moved, pct, after))
        pump(3.)                       # the O10 method's settle after the first loop, here after every one
        points.append(measure('%d loops shown and left' % (index + 1)))
    info('catalogue previews (name, moved, %% changed, s): %s' % played)
    all_played = bool(played) and all(p[1] for p in played)
    check('M1 every catalogue loop visibly played in its preview before its cost was measured', all_played, played)
    pump(3.)
    warm = measure('warm: every catalogue loop shown, no preview open')
    first = points[0]
    loads = [first['load1'], warm['load1']]
    total = warm['rss_mb'] - cold['rss_mb']
    per_loop = (warm['rss_mb'] - first['rss_mb']) / 7.
    rss_points = [(i + 1, p['rss_mb']) for i, p in enumerate(points)] + [(8, warm['rss_mb'])]
    slope = theil_sen(rss_points[1:])          # the loops after the first, as M2r judges
    worst_total = max(p['rss_mb'] for p in points[1:] + [warm]) - cold['rss_mb']
    result.update(points=points, warm=warm, catalogue_played=played, total_mb=round(total, 1),
                  per_loop_mb=round(per_loop, 1), slope_mb=round(slope, 1), worst_total_mb=round(worst_total, 1))
    info('media cost (O10 method): first loop %.1f MB, then %.1f MB per loop, total %.1f MB; Theil-Sen %.1f MB per '
         'loop; worst sample total %.1f MB; threads +%d; load 1m at first/warm %s' % (
             first['rss_mb'] - cold['rss_mb'], per_loop, total, slope, worst_total, warm['threads'] - cold['threads'],
             loads))

    # ---- criterion 3: headroom at load 20+
    loaded = min(loads) >= LOAD_TARGET
    check('H0 the cost was measured at load %d+ (1-minute load @@ at the first loop, @@ with all 8)' % LOAD_TARGET,
          loaded, loads, measured=tuple(float(round(v, 1)) for v in loads))
    check('H1 each catalogue loop after the first adds at most %d MB, the O10 way and as a robust slope '
          '(@@ MB per loop, Theil-Sen @@ MB)' % PER_LOOP_MB,
          all_played and loaded and per_loop <= PER_LOOP_MB and slope <= PER_LOOP_MB,
          (cold['rss_mb'], first['rss_mb'], warm['rss_mb'], rss_points, loads),
          measured=(float(round(per_loop, 1)), float(round(slope, 1))))
    check('H2 the 8 catalogue loops cost under %d MB in total, at every sample after the first loop (@@ MB at the end, '
          'worst @@ MB)' % TOTAL_MB, all_played and loaded and total < TOTAL_MB and worst_total < TOTAL_MB,
          (cold['rss_mb'], [p['rss_mb'] for p in points], warm['rss_mb'], loads),
          measured=(round(total), round(worst_total)))

    # ---- criterion 1: what paused loops keep
    step('paused loops let go')
    waited = wait_released(RELEASE_WAIT_S)
    released = measure('all 8 loops off screen, %.0f s after leaving' % waited)
    added = released['threads'] - cold['threads']
    result.update(released=released, release_wait_s=round(waited, 1))
    info('paused loops: before (2.5.0) +80 threads, 56 streaming, 42-49 MB per loop; after: +%d threads, %d streaming, '
         '%d GstPlay, %.1f MB per paused loop (Theil-Sen), %.1f MB total over cold, after %.0f s' % (
             added, released['streaming'], released['gstplay'], slope, released['rss_mb'] - cold['rss_mb'], waited))
    check('P1 with all 8 catalogue loops off screen the media holds at most %d threads (2.5.0: +80; @@)'
          % PAUSED_THREADS_MAX, all_played and added <= PAUSED_THREADS_MAX, (cold['threads'], released['threads']),
          measured='+%d' % added)
    check('P2 ... and at most %d GStreamer streaming threads: one loop may stay warm (2.5.0: 56; @@)' % STREAMING_MAX,
          all_played and released['streaming'] <= STREAMING_MAX, released['streaming'], measured=released['streaming'])
    check('P3 a paused loop keeps at most %.0f MB: its still and little else (2.5.0: 42-49 MB; Theil-Sen @@ MB per loop '
          'over the 8 off-screen samples)' % PAUSED_MB_PER_LOOP, all_played and slope <= PAUSED_MB_PER_LOOP,
          rss_points, measured=float(round(slope, 1)))

    # ---- criterion 1: returning to a loop that let go, at the host's own load (the brief sets load 20+ for the
    # cost only; under the added load Xvfb itself is starved and every X round trip reads as a GTK-thread stall)
    (Path(env.args.output) / 'load-done').write_text('cost measured\n')
    pump(5.)
    step('resume previews')
    resumes = []
    offset = log_offset()
    mark = len(env.log)
    started = []
    real_popen = subprocess.Popen

    class CountingPopen(real_popen):
        def __init__(self, argv, *a, **k):
            if not (isinstance(argv, list) and argv and argv[0] == sys.executable and '--child' in argv):
                started.append(argv)
            super().__init__(argv, *a, **k)
    subprocess.Popen = CountingPopen
    for card in (catalogue[0], catalogue[3], catalogue[5], catalogue[7]):
        if box is None:
            break
        stall, wall, frames, acted = watch(2.5, box, lambda: rooms.click(card))
        bad, after = blank_frames(frames, acted)
        moved = rooms.moves(6.)[0]
        resumes.append({'name': rooms.entry(card)['name'], 'stall_ms': round(stall, 1), 'wall_ms': round(wall[0], 1), 'cpu_ms': round(wall[1], 1),
                        'frames': len(after), 'blank': bad, 'moved': moved, 'load1': os.getloadavg()[0]})
        leave()
        pump(1.)
    subprocess.Popen = real_popen
    preview_commands = env.commands(mark)
    preview_sinks = audio_sinks(created_elements(offset))
    info('resumed previews: %s' % json.dumps(resumes))

    # ---- the entered room: Now let go and came back
    step('resume Now')
    rooms.click(catalogue[1])
    pump(1.)
    widgets = window.rooms.preview_widgets
    if widgets and widgets.get('enter') is not None:
        widgets['enter'].emit('clicked')
    env.settle(1.5)
    deadline = time.monotonic() + 6
    while env.controller.transition and time.monotonic() < deadline:
        pump(.1)
    pump(1.)
    entered = window.current_page() == 'now' and rooms.moves(6.)[0]
    now_box = rooms.scene_box()[1]
    window.show_rooms()
    now_wait = wait_released(RELEASE_WAIT_S)
    off_now = measure('entered room off screen (Rooms), %.0f s after leaving' % now_wait)
    now_resume = {}
    if entered and now_box is not None:
        stall, wall, frames, acted = watch(2.5, now_box, window.show_now)
        bad, after = blank_frames(frames, acted)
        now_resume = {'stall_ms': round(stall, 1), 'wall_ms': round(wall[0], 1), 'cpu_ms': round(wall[1], 1), 'frames': len(after), 'blank': bad,
                      'moved': rooms.moves(6.)[0], 'load1': os.getloadavg()[0]}
    info('entered room: played %s; off screen +%d threads, %d streaming; resumed on Now %s' % (
        entered, off_now['threads'] - cold['threads'], off_now['streaming'], json.dumps(now_resume)))
    all_resumes = resumes + ([now_resume] if now_resume else [])
    check('R0 the entered room played, and with it off screen the media also let go (@@ streaming threads)',
          entered and off_now['streaming'] <= STREAMING_MAX and off_now['threads'] - cold['threads'] <= PAUSED_THREADS_MAX,
          (entered, off_now), measured=off_now['streaming'])
    worst_stall = max([r['stall_ms'] for r in all_resumes] or [1e9])
    check('R1 returning to a loop that let go never holds the GTK thread over %d ms (4 previews and Now; worst @@ ms '
          'without run-queue wait)' % RESUME_STALL_MS,
          len(all_resumes) == 5 and worst_stall <= RESUME_STALL_MS, all_resumes, measured=float(worst_stall))
    check('R2 returning to a loop that let go shows no black or blank frame (sampled at about 30 Hz)',
          len(all_resumes) == 5 and all(r['blank'] == [] and r['frames'] >= 20 for r in all_resumes),
          [(r.get('name', 'Now'), r['frames'], r['blank']) for r in all_resumes])
    check('R3 a loop that let go moves again when shown (4 previews and Now)',
          len(all_resumes) == 5 and all(r['moved'] for r in all_resumes), all_resumes)
    check('R4 resuming previews is inert: no controller command, no process, no audio sink',
          len(resumes) == 4 and not preview_commands and not started and not preview_sinks,
          (preview_commands, started, preview_sinks))

    # ---- criterion 5: a resumed loop still loops (G7) without a black frame at the seam
    step('resumed loop past its end')
    seam_frames, seam = [], {}
    if now_resume:
        stall, wall, seam_frames, acted = watch(CLIP_S + 6., now_box, lambda: None)
        bad, after = blank_frames(seam_frames, acted)
        moving_late = rooms.moves(3.)[0]
        seam = {'frames': len(after), 'blank': bad, 'moving_after_clip': moving_late, 'stall_ms': round(stall, 1)}
    info('resumed loop past %d s: %s' % (CLIP_S, json.dumps(seam)))
    check('L1 a resumed scene keeps looping past its %d s clip, with no black or blank frame at the seam' % CLIP_S,
          bool(seam) and seam['moving_after_clip'] and seam['blank'] == [] and seam['frames'] >= 100, seam)

    # ---- criterion 5: reduced motion on a released loop
    step('reduced motion')
    window.show_rooms()
    wait_released(RELEASE_WAIT_S)
    env.service.execute('settings', {'values': {'reduced_motion': True}})
    env.settle(1.2)
    offset_rm = log_offset()
    names_before = thread_names()
    before_rm, streaming_before = len(names_before), streaming_count(names_before)
    rooms.click(catalogue[2])
    pump(1.5)
    still = rooms.still_for(2.)
    names_rm = thread_names()
    rm_built = [n for n in created_elements(offset_rm) if n in PLAYER_ELEMENTS]
    info('reduced motion preview of a released loop: changed %s%%, streaming threads %d, threads %+d, players built %s'
         % (still, streaming_count(names_rm), len(names_rm) - before_rm, rm_built))
    check('L2 reduced motion: returning to a loop shows its still and acquires nothing (no new streaming thread, '
          'no player built)', still is not None and still < .005 and streaming_count(names_rm) <= streaming_before
          and not rm_built, (still, streaming_before, streaming_count(names_rm), rm_built))
    leave()
    env.service.execute('settings', {'values': {'reduced_motion': False}})
    env.settle(1.)

    # ---- finding 9: wrappers per preview, and every loop shown again (rebuilds would show here)
    step('wrappers per preview')
    import gi
    from gi.repository import GObject

    def wrappers():
        gc.collect()
        return sum(1 for o in gc.get_objects() if isinstance(o, GObject.Object))
    counts, census = [], None
    for cycle in range(24):
        if cycle == 4:
            counts.append(wrappers())
        card = catalogue[cycle % 8]
        rooms.click(card)
        pump((.6, .9, 1.3)[cycle % 3])
        leave()
        pump(.4)
    pump(2.)
    counts.append(wrappers())
    live = gc.get_objects()
    census = {'Scene': sum(isinstance(o, next_widgets.Scene) for o in live),
              'MediaFile': sum(isinstance(o, Gtk.MediaFile) for o in live)}
    growth = (counts[1] - counts[0]) / 20. if len(counts) == 2 else float('nan')
    info('GObject wrappers after preview 4 and 24: %s (%+.2f per preview); live wrappers %s' % (counts, growth, census))
    check('F9 Python GObject wrappers stay flat across previews: at most %.1f more per preview over 20 (@@ per preview)'
          % WRAPPERS_PER_PREVIEW, len(counts) == 2 and growth <= WRAPPERS_PER_PREVIEW, (counts, census),
          measured=float(round(growth, 2)))

    # ---- criterion 2: the no-teardown guarantee, observed while the window lives
    step('no teardown')
    pump(1.)
    built = [n for n in created_elements(offset0) if n in PLAYER_ELEMENTS]
    sampler['lost'] |= sampler['seen'] - gstplay_tids()
    hooks.window_alive = False
    sampler['stop'].set()
    result.update(players_built=len(built), gstplay_seen=len(sampler['seen']), gstplay_lost=sorted(sampler['lost']),
                  mediafiles_created=hooks.created, mediafiles_finalized=hooks.finalized, mediafile_drops=hooks.dropped,
                  resumes=all_resumes, seam=seam, wrappers=counts, census=census, uptime_end=uptime())
    info('teardown watch: %d player pipelines built for %d loop files; GstPlay threads seen %d, lost %d; MediaFiles '
         'created %d, finalized %s, cleared/re-opened %s' % (len(built), len(files), len(sampler['seen']),
                                                              len(sampler['lost']), hooks.created, hooks.finalized[:5],
                                                              hooks.dropped[:5]))
    check('D1 no player is disposed while the window lives: no GstPlay thread went away (@@ lost of @@ seen)',
          all_played and not sampler['lost'], sorted(sampler['lost']),
          measured=(len(sampler['lost']), len(sampler['seen'])))
    check('D2 no player pipeline is built twice for a loop file, after every loop was shown, let go and shown again '
          '(@@ built for 8 loop files)', all_played and len(built) <= len(files), collections.Counter(built),
          measured=len(built))
    check('D3 no Gtk.MediaFile with an open file is cleared, re-opened or finalized while the window lives',
          all_played and not hooks.finalized and not hooks.dropped, (hooks.finalized[:8], hooks.dropped[:8]))
    return result


if __name__ == '__main__':
    arguments = parse()
    if arguments.child == 'grabber':
        run_grabber(arguments)
    elif arguments.child:
        run_child(arguments.child, arguments)
    else:
        if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
            reexec_isolated()
        run_parent(arguments)
