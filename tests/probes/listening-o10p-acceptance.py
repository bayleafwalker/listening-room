"""O10-PERF acceptance: a fast cold start, bounded scene-media cost, and real media in the dev loop.

Written by the independent acceptance author from the O10-PERF brief and the 2026-11-21 performance
assessment (sections 2-4 and 6, items 4 and 8), not from the implementation. Real GTK on a private Xvfb with
its own D-Bus (this file re-executes itself there once, like tests/probes/o6_env.py), the real session
controller on the session test double and fixture providers. Nothing reaches the desktop session, the
network, systemd or credentials; no audio is played on the host; children disable core dumps.

The probe never adds GStreamer itself (unlike the 2.3.2 hotfix probe's --gstreamer auto): criterion 3 is
that the dev shell provides it, so a shell without it fails the setup checks and every media oracle with it.

Children (each a subprocess, so a segfault is a result):

  cold    x --cold-runs (3): a fresh process builds the window and waits for Rooms. Every image decode or
          scale call (Gdk.Texture, GdkPixbuf, Gtk.Picture/Image file loaders, PIL) is timed with the thread it
          ran on, every GTK main-loop iteration is timed (wall and GTK-thread CPU), and a sampler records the
          GTK thread's innermost application frame every 5 ms. A "hold by art" is the art time inside one
          main-loop iteration (or the window constructor): decode/scale calls on the GTK thread, or sampled
          time in an art-named application frame. Judged on GTK-thread CPU (load-independent); wall time is
          judged only when the 1-minute load is at most half the cores, and always reported with uptime.
  scene   scene guarantees: preview and entered loops play (pixels move), keep looping past the clip length,
          play at the loop's native resolution, previews are inert (no command, no process, no GStreamer
          audio sink), reduced motion shows the full-resolution still with no decoding, off-screen and hidden
          scenes stop decoding and resume when shown again.
  media   steady-state cost over every catalogue loop, measured on the whole process tree (a helper process
          cannot hide memory): every loop must visibly play before the cost counts (memory cut by never
          playing is caught), the warm cost, decoder and total threads, idle CPU of media threads on the grid,
          40 more previews (flat RSS, threads, fds, GL threads, and no new GStreamer pipeline: tearing media
          down and rebuilding it again is caught), then 16 user clips beyond the catalogue (copies under the
          child's HOME, shown through extra room cards) whose cost must stay bounded.
  stress  the 2.3.2 hotfix probe's stress child (tests/probes/listening-hotfix-media-acceptance.py
          --skip-checks), 26 children x 8 cycles = 208 cycles of rapid card switching, preview open/close,
          enter/back and window reopen with randomised timing; every crash (signal) counts.

Writes OUTPUT/o10p-acceptance.json (judged by acceptance_verdict.py against expected/o10p.checks) and
OUTPUT/o10p-summary.json with every measurement, the load samples and uptime. Ends with "N passed, M failed".

  scripts/listening-dev.sh acceptance OUT o10p, or scripts/listening-dev.sh probe OUT listening-o10p-acceptance
  (--cold-runs N --stress-children N --skip-stress for a quick look; the full run takes about 15 minutes)

Criterion revisions are made in place, keeping the original visible as "[ORIGINAL, superseded by ...]".
"""
import argparse
import collections
import faulthandler
import json
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
FLAGS = {
    '--child': {'default': ''},             # internal: cold, scene, media
    '--seed': {'type': int, 'default': 0},
    '--cold-runs': {'type': int, 'default': 3},
    '--stress-children': {'type': int, 'default': 26},   # x 8 cycles = 208 >= 200 (criterion 2)
    '--stress-cycles': {'type': int, 'default': 8},
    '--stress-jobs': {'type': int, 'default': 3},
    '--skip-stress': {'action': 'store_true'},
    '--steady-cycles': {'type': int, 'default': 40},
    '--extra-clips': {'type': int, 'default': 16},
    '--only': {'default': ''},              # comma list of parts for a focused look: setup,cold,scene,media,stress
}

# Thresholds, each from the brief or the assessment (see the check names).
ART_HOLD_MS = 50          # criterion 1: the GTK thread is never held more than 50 ms by art
MEDIA_TOTAL_MB = 400      # criterion 2 target: total under 400 MB for the 8 catalogue loops ([ORIGINAL], superseded by M2r)
# Revision M2r (2026-11-21, coordinator decision in phase 2, like the 2.3.2 pool-cap re-base): the implementer measured
# 421-493 MB for 8 loops (main 927-993), of which ~140 MB is llvmpipe's one-time GL set-up on Xvfb and ~37-40 MB per
# loop is reference frames plus the GL video sink, with decoders capped at one thread per loop. Re-based to: total media
# cost <= 520 MB with 8 loops AND each loop after the first adds <= 50 MB. Further cuts are backlog.
MEDIA_TOTAL_R_MB, MEDIA_PER_LOOP_R_MB = 520, 50
DECODER_THREADS_MAX = 16  # bounded: on main avdec_h264 starts 16 threads per loop (one per core), 128 for 8
MEDIA_THREADS_MAX = 64    # all threads the media adds after every loop was shown (main: +215 for 8 loops)
# M4-R1 (coordinator ruling, phase 2): GStreamer keeps ~10 threads per paused playbin3 (GstPlay, typefind, demux,
# multiqueue, queue, GL sink) even with one decoder thread per loop; the bound is per loop that has been shown,
# with an explicit ceiling at the implementer's loop capacity of 12 loop files per process.
MEDIA_THREADS_PER_LOOP, LOOP_CAPACITY = 12, 12
IDLE_MEDIA_CPU_MS = 50    # media threads with every loop off screen, per 5 s (1% of a core)
STEADY_RSS_MB, STEADY_THREADS, STEADY_FDS = 64, 6, 12   # the 2.3.2 lifetime bounds, after 40 more previews
STEADY_FDS_STEP_R = 40    # M7-R1 (coordinator ruling, phase 2): one-time fd step allowed since warm-up (3507ca3: +28)
EXTRA_TOTAL_MB = 400 + 64   # the original 400 MB target plus 64 MB, as written in phase 1; 8 catalogue loops plus 16 user clips (24 distinct files)
EXTRA_PER_CLIP_MB, EXTRA_PER_CLIP_THREADS, EXTRA_PER_CLIP_FDS = 4, 1, 2   # marginal, over the last 8 clips
WIDEST_CARD = (334, 188)  # the widest Rooms card, measured on 3507ca3 (a 600 px window, two columns; 260 px at 1200)
LOOP_FORMAT = (1440, 810)  # every catalogue room loop at e45d947: h264 1440x810, 24 fps, 480 frames
DECODER_NAME = re.compile(r'^av:|avdec|dav1d|openh264|vaapi|v4l2|nvh264|vtdec|libde265')
GL_NAME = re.compile(r'^(gldisplay-event|gstglcontext)')
ART_FRAME = re.compile(r'textur|poster|artwork|pixbuf|thumbnail|decode|image|picture_for|scale', re.I)
PIPELINE_ELEMENTS = ('playbin', 'playbin3', 'pipeline')
GST_READY = ("import gi, sys; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); "
             "sys.exit(0 if Gst.ElementFactory.find('playbin3') else 1)")


def parse():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source', default=str(REPO / 'src'))
    parser.add_argument('--output', default='/tmp/listening-o10p-acceptance')
    parser.add_argument('--repo', default='')
    for flag, kwargs in FLAGS.items():
        parser.add_argument(flag, **kwargs)
    return parser.parse_known_args()[0]


def no_core_dumps():
    import ctypes
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)   # PR_SET_DUMPABLE = 0
    except Exception:
        pass


def uptime():
    try:
        return subprocess.run(['uptime'], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 'load average: %.2f, %.2f, %.2f' % os.getloadavg()


def fill(name, measured):
    """Put measured values into a check name's @@ spans (expected/o10p.checks keeps the @@)."""
    if not isinstance(measured, (tuple, list)):
        measured = (measured,)
    for value in measured:
        name = name.replace('@@', ('%.1f' % value) if isinstance(value, float) else str(value), 1)
    return name


# ============================================================================ process measurements

def _children_map():
    kids = collections.defaultdict(list)
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            try:
                fields = open('/proc/%s/stat' % entry).read().rsplit(')', 1)[1].split()
                kids[int(fields[1])].append(int(entry))
            except (OSError, IndexError):
                pass
    return kids


def tree(root=None):
    """This process and every descendant: a helper process cannot hide the media's cost."""
    root = root or os.getpid()
    kids, found, todo = _children_map(), [], [root]
    while todo:
        pid = todo.pop()
        found.append(pid)
        todo.extend(kids.get(pid, ()))
    return found


def status_field(pid, key):
    try:
        for line in open('/proc/%d/status' % pid):
            if line.startswith(key + ':'):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def threads_cpu(pids=None):
    """{(pid, tid): (name, cpu ticks)} for every thread in the tree."""
    found = {}
    for pid in pids or tree():
        try:
            tasks = os.listdir('/proc/%d/task' % pid)
        except OSError:
            continue
        for tid in tasks:
            try:
                name = open('/proc/%d/task/%s/comm' % (pid, tid)).read().strip()
                fields = open('/proc/%d/task/%s/stat' % (pid, tid)).read().rsplit(')', 1)[1].split()
                found[(pid, int(tid))] = (name, int(fields[11]) + int(fields[12]))
            except (OSError, IndexError, ValueError):
                continue
    return found


TICK_MS = 1000. / os.sysconf('SC_CLK_TCK')


def sample(label=''):
    pids = tree()
    names = collections.Counter(name for name, _ in threads_cpu(pids).values())
    point = {'label': label, 'processes': len(pids),
             'rss_mb': round(sum(status_field(p, 'VmRSS') for p in pids) / 1024., 1),
             'threads': sum(status_field(p, 'Threads') for p in pids),
             'fds': sum(len(os.listdir('/proc/%d/fd' % p)) for p in pids if os.path.isdir('/proc/%d/fd' % p)),
             'decoder_threads': sum(n for name, n in names.items() if DECODER_NAME.search(name)),
             'gl_threads': sum(n for name, n in names.items() if GL_NAME.search(name)),
             'load1': round(os.getloadavg()[0], 2)}
    return point, dict(sorted(names.items()))


def media_cpu_ms(before, after, baseline_tids):
    """CPU of the threads that did not exist before the first preview (GStreamer, GL, decoders), in ms."""
    total = 0
    for key, (name, ticks) in after.items():
        if key in baseline_tids:
            continue
        total += ticks - before.get(key, (name, 0))[1]
    return total * TICK_MS


# ============================================================================ parent

def reexec_isolated():
    runtime = tempfile.mkdtemp(prefix='listening-o10p-runtime-')
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


def store_paths(text):
    return {m.group(0) for m in re.finditer(r'/nix/store/[0-9a-z]{32}-[^/:\s"]+', text or '')}


def gst_setup(args, check, info, summary):
    """Criterion 3: the dev shell itself provides GStreamer, pinned and with the package's plugins."""
    ready = subprocess.call([sys.executable, '-c', GST_READY], stderr=subprocess.DEVNULL) == 0
    check('S1 the dev shell provides GStreamer playbin3 to the window (the probe adds none)', ready,
          'GST_PLUGIN_SYSTEM_PATH_1_0=%r' % os.environ.get('GST_PLUGIN_SYSTEM_PATH_1_0'))
    needed = ('qtdemux', 'h264parse', 'avdec_h264', 'videoconvert', 'playbin3')
    probe = ("import gi, json; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); "
             "print(json.dumps({n: bool(Gst.ElementFactory.find(n)) for n in %r}))" % (needed,))
    found = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True)
    try:
        elements = json.loads(found.stdout)
    except ValueError:
        elements = {}
    check('S2 the elements the catalogue loops need are in the dev shell (%s)' % ', '.join(needed),
          elements and all(elements.values()), elements or found.stderr[-300:])
    assets = Path(os.environ.get('LISTENING_HUB_ASSETS', ''))
    loop = assets / 'room-night-drive.mp4'
    decode = ("import gi, sys; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); "
              "p = Gst.parse_launch('filesrc location=%s ! qtdemux ! h264parse ! avdec_h264 ! fakesink name=s "
              "signal-handoffs=true'); n = [0]; p.get_by_name('s').connect('handoff', lambda *a: n.__setitem__(0, n[0] + 1)); "
              "p.set_state(Gst.State.PLAYING); m = p.get_bus().timed_pop_filtered(20 * Gst.SECOND, "
              "Gst.MessageType.EOS | Gst.MessageType.ERROR); p.set_state(Gst.State.NULL); print(n[0]); "
              "sys.exit(0 if m and m.type == Gst.MessageType.EOS else 1)") % loop
    frames = subprocess.run([sys.executable, '-c', decode], capture_output=True, text=True, timeout=120) \
        if loop.is_file() else None
    decoded = frames is not None and frames.returncode == 0 and frames.stdout.strip().isdigit() \
        and int(frames.stdout.strip()) >= 400
    check('S3 the dev shell decodes a catalogue loop end to end (all frames of room-night-drive.mp4)', decoded,
          (str(loop), frames and (frames.stdout.strip(), frames.stderr[-300:])))
    shell = {p for p in store_paths(os.environ.get('GST_PLUGIN_SYSTEM_PATH_1_0', ''))}
    repo = args.repo or str(REPO)
    expr = ('p: map (d: toString (d.out or d)) (builtins.filter (d: d != null && '
            'builtins.substring 0 3 (d.pname or "") == "gst") (p.buildInputs ++ p.propagatedBuildInputs))')
    package = set()
    evaluated = None
    if shutil.which('nix'):
        evaluated = subprocess.run(['nix', 'eval', '--json', repo + '#packages.x86_64-linux.default', '--apply', expr],
                                   capture_output=True, text=True, timeout=600)
        if evaluated.returncode == 0:
            package.update(json.loads(evaluated.stdout))
    summary['gst'] = {'shell_plugin_paths': sorted(shell), 'package_gst_inputs': sorted(package)}
    info('dev shell GStreamer plugin paths: %s' % ', '.join(Path(p).name for p in sorted(shell)) or 'none')
    info('package GStreamer outputs: %s' % ', '.join(Path(p).name for p in sorted(package)) or
         'unknown (%s)' % (evaluated and evaluated.stderr[-300:]))
    check('S4 the dev shell GStreamer is pinned to exactly the package\'s plugin packages (same store paths)',
          bool(package) and shell == package, {'only in shell': sorted(Path(p).name for p in shell - package),
                                               'package plugins missing from shell': sorted(Path(p).name for p in package - shell)})
    for mp4 in sorted(assets.glob('room-*.mp4')):
        fmt = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                              'stream=width,height,nb_frames', '-of', 'csv=p=0', str(mp4)],
                             capture_output=True, text=True) if shutil.which('ffprobe') else None
        summary.setdefault('loop_formats', {})[mp4.name] = fmt and fmt.stdout.strip()
    formats = summary.get('loop_formats', {})
    check('S5 the 8 catalogue room loops are unchanged in format (h264 %dx%d, 480 frames): cost is not cut by '
          'shrinking the media' % LOOP_FORMAT, len(formats) == 8 and all(
              v == '%d,%d,480' % LOOP_FORMAT for v in formats.values()), formats)


def run_parent(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checks, summary = [], {'uptime_start': uptime(), 'cpus': os.cpu_count(), 'source': str(Path(args.source).resolve()),
                           'renderer': os.environ.get('GSK_RENDERER'), 'load_samples': [os.getloadavg()]}

    def check(name, ok, detail='', measured=()):
        name = fill(name, measured)
        checks.append({'criterion': name, 'pass': bool(ok), 'detail': '' if ok else str(detail)[:1500]})
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:700]), flush=True)

    def info(text):
        print('INFO ' + text, flush=True)

    def child(mode, seed=0, timeout=900):
        log = output / ('%s-%d.log' % (mode, seed))
        gst = output / ('%s-%d.gst' % (mode, seed))
        environment = dict(os.environ, PYTHONFAULTHANDLER='1', GST_DEBUG='GST_ELEMENT_FACTORY:4',
                           GST_DEBUG_FILE=str(gst), GST_DEBUG_NO_COLOR='1')
        runtime = tempfile.mkdtemp(prefix='o10p-child-', dir=os.environ.get('XDG_RUNTIME_DIR'))
        environment['XDG_RUNTIME_DIR'] = runtime
        command = ['dbus-run-session', '--', sys.executable, str(HERE), '--child', mode, '--seed', str(seed),
                   '--source', args.source, '--output', str(output / ('%s-%d' % (mode, seed))),
                   '--steady-cycles', str(args.steady_cycles), '--extra-clips', str(args.extra_clips)]
        started = time.monotonic()
        with open(log, 'w') as handle:
            process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=environment,
                                       preexec_fn=no_core_dumps)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                code = 'timeout'
        shutil.rmtree(runtime, ignore_errors=True)
        if isinstance(code, int) and code > 128:
            code = -(code - 128)
        try:
            gst.unlink()
        except OSError:
            pass
        text = log.read_text(errors='replace')
        summary['load_samples'].append(os.getloadavg())
        result = {}
        for line in text.splitlines():
            if line.startswith('RESULT '):
                result = json.loads(line[7:])
            elif line.startswith('INFO '):
                print(line, flush=True)
        info('%s child %d: exit %s in %.0f s; %s' % (mode, seed, code, time.monotonic() - started, uptime()))
        return code, text, result

    def child_lines(mode, code, text):
        for line in text.splitlines():
            if line.startswith(('PASS ', 'FAIL ')):
                name, _, detail = line[5:].partition('  ')
                check(name, line.startswith('PASS '), detail)
        crashed = not isinstance(code, int) or code < 0
        check('%s: the child finished without a crash or hang' % mode, not crashed, 'exit %s\n%s' % (code, text[-1200:]))
        check('%s: the child reported all its oracles and exited 0' % mode,
              code == 0 and 'CHECKS-DONE' in text, 'exit %s\n%s' % (code, text[-1200:]))

    print('source: %s  renderer: %s  cpus: %d' % (summary['source'], summary['renderer'], os.cpu_count()), flush=True)
    print('uptime at start: %s' % summary['uptime_start'], flush=True)
    parts = set(filter(None, args.only.split(','))) or {'setup', 'cold', 'scene', 'media', 'stress'}
    if 'setup' in parts:
        gst_setup(args, check, info, summary)

    # ---------------------------------------------------------------- criterion 1: cold start
    if 'cold' in parts:
        runs = []
        for seed in range(max(1, args.cold_runs)):
            code, text, result = child('cold', seed, timeout=240)
            result.update(code=code, uptime=uptime())
            runs.append(result)
            if code != 0 or not result.get('rooms_ms'):
                print(text[-1500:], flush=True)
        summary['cold'] = runs
        good = [r for r in runs if r.get('rooms_ms')]
        cpus = os.cpu_count() or 1
        check('C0 every cold start reached the Rooms grid with its cards (%d runs)' % len(runs),
              len(good) == len(runs), [(r.get('code'), r.get('error')) for r in runs])
        if good:
            median = lambda key: round(statistics.median(r[key] for r in good))
            info('cold start: time to Rooms %s ms wall (median %d), GTK-thread CPU %s ms (median %d); time to poster art '
                 '%s ms; load 1m %s' % ([r['rooms_ms'] for r in good], median('rooms_ms'), [r['rooms_cpu_ms'] for r in good],
                                        median('rooms_cpu_ms'), [r.get('art_ms') for r in good], [r['load1'] for r in good]))
            check('C1 time to Rooms after a cold start is measured and reported (median @@ ms wall, @@ ms GTK-thread CPU, '
                  'load @@)', True, measured=(median('rooms_ms'), median('rooms_cpu_ms'),
                                              float(statistics.median(r['load1'] for r in good))))
        worst_cpu = max([r.get('art_hold_cpu_ms', 1e9) for r in runs] or [1e9])
        worst_wall = max([r.get('art_hold_wall_ms', 1e9) for r in runs] or [1e9])
        check('C2 the GTK thread is never held more than %d ms by art during cold start, by GTK-thread CPU (worst @@ ms)'
              % ART_HOLD_MS, runs and worst_cpu <= ART_HOLD_MS,
              [(r.get('art_hold_cpu_ms'), r.get('worst_art_segment')) for r in runs], measured=float(worst_cpu))
        # Wall time inflates with host load: the allowance scales with the 1-minute load above half the cores.
        allowance = lambda r: ART_HOLD_MS * max(1., r.get('load1', 99) / (cpus / 2.))
        over = [r for r in runs if r.get('art_hold_wall_ms', 1e9) > allowance(r)]
        judged = max(runs, key=lambda r: r.get('art_hold_wall_ms', 1e9) / allowance(r)) if runs else {}
        check('C2w ... and by wall time: <= %d ms, scaled by the 1-minute load above %d (worst @@ ms against @@ ms)'
              % (ART_HOLD_MS, cpus // 2), runs and not over,
              [(r.get('art_hold_wall_ms'), round(allowance(r)), r.get('load1'), r.get('worst_art_segment')) for r in runs],
              measured=(float(judged.get('art_hold_wall_ms', -1)), round(allowance(judged)) if judged else -1))
        info('worst art hold over all runs: %.1f ms wall, %.1f ms CPU; load 1m %s' % (
            worst_wall, worst_cpu, [r.get('load1') for r in runs]))
        check('C3 poster decoding leaves the GTK thread: no image decode or scale call ran on it during cold start',
              all(r.get('gtk_decodes', 1) == 0 for r in runs), [r.get('gtk_decode_calls') for r in runs])
        check('C3b the GTK thread spent at most %d ms CPU on art in total during cold start (worst @@ ms)' % ART_HOLD_MS,
              max([r.get('gtk_art_cpu_ms', 1e9) for r in runs] or [1e9]) <= ART_HOLD_MS,
              [r.get('gtk_art_cpu_ms') for r in runs], measured=float(max([r.get('gtk_art_cpu_ms', 1e9) for r in runs] or [1e9])))
        check('C4 every card ends up showing its own poster art (placeholders, then textures)',
              bool(runs) and all(r.get('art_shown') for r in runs), [r.get('art_detail') for r in runs])
        # Revision C5r (acceptance author, phase 2): C5 compared the texture with the card at one window width
        # (1200 px, cards 260 px), but the grid lays cards out wider at other widths (334 px at 600 px). "Card size"
        # means the card at its widest, at up to 2x display scale. C5 still runs as a known failure.
        check('C5r card posters are pre-scaled to card size: each card texture is at most 2x the widest card the grid '
              'shows (%dx%d)' % (2 * WIDEST_CARD[0], 2 * WIDEST_CARD[1]), bool(runs) and all(r.get('card_size') for r in runs),
              [r.get('texture_sizes') for r in runs])
        check('C5 [ORIGINAL, superseded by C5r] card posters are pre-scaled: each card texture is at most 2x its on-screen size',
              bool(runs) and all(r.get('prescaled') for r in runs), [r.get('texture_sizes') for r in runs])

    # ---------------------------------------------------------------- criterion 4: scene guarantees
    if 'scene' in parts:
        code, text, result = child('scene', 0, timeout=420)
        summary['scene'] = result
        child_lines('scene', code, text)

    # ---------------------------------------------------------------- criterion 2: media cost
    if 'media' in parts:
        code, text, result = child('media', 0, timeout=1200)
        summary['media'] = result
        child_lines('media', code, text)

    # ---------------------------------------------------------------- criterion 2: zero crashes over >= 200 cycles
    if 'stress' in parts and not args.skip_stress:
        stress_out = output / 'stress'
        command = [sys.executable, str(HERE.parent / 'listening-hotfix-media-acceptance.py'), '--skip-checks',
                   '--gstreamer', 'off', '--children', str(args.stress_children), '--cycles', str(args.stress_cycles),
                   '--jobs', str(args.stress_jobs), '--source', args.source, '--output', str(stress_out)]
        started = time.monotonic()
        with open(output / 'stress.log', 'w') as handle:
            code = subprocess.call(command, stdout=handle, stderr=subprocess.STDOUT)
        try:
            stress = json.loads((stress_out / 'hotfix-media-summary.json').read_text())
        except (OSError, ValueError):
            stress = {}
        summary['stress'] = stress
        info('stress: exit %s in %.0f s: %s' % (code, time.monotonic() - started, json.dumps(
            {k: stress.get(k) for k in ('children', 'cycles_attempted', 'cycles_completed', 'crashed_children',
                                        'signals', 'timeouts', 'errors', 'load_average_1m')})))
        attempted = stress.get('cycles_attempted', 0)
        # Without GStreamer no scene media exists and the stress run cannot crash in it: not evidence.
        media = subprocess.call([sys.executable, '-c', GST_READY], stderr=subprocess.DEVNULL) == 0
        check('X1 stress: at least 200 scene-media stress cycles ran (@@ attempted)',
              media and attempted >= 200 and stress.get('cycles_completed', 0) >= 200,
              (stress.get('cycles_completed'), 'GStreamer playbin3 available: %s' % media),
              measured=attempted)
        check('X2 stress: zero crashes over those cycles (@@ crashed children)', media and bool(stress) and
              stress.get('crashed_children') == 0 and stress.get('timeouts') == 0, stress.get('crash_steps'),
              measured=stress.get('crashed_children'))
        check('X3 stress: every stress child finished all its cycles without raising', stress and
              stress.get('errors') == 0 and stress.get('cycles_completed') == stress.get('children', 0) * args.stress_cycles,
              {k: stress.get(k) for k in ('errors', 'cycles_completed', 'children')})

    summary['uptime_end'] = uptime()
    summary['checks'] = checks
    (output / 'o10p-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (output / 'o10p-acceptance.json').write_text(json.dumps(checks, indent=2) + '\n')
    print('uptime at end: %s' % summary['uptime_end'], flush=True)
    passed = sum(1 for c in checks if c['pass'])
    print('%d passed, %d failed' % (passed, len(checks) - passed), flush=True)
    sys.exit(0 if passed == len(checks) else 1)


# ============================================================================ children: shared

ART_CALLS = []          # (thread ident, start, wall s, cpu s or None, kind, name)


def instrument_art():
    """Time every public image decode, scale and upload entry point, with the thread it runs on.

    Installed before the application is imported, so a module-level alias of an entry point is wrapped too."""
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gdk', '4.0')
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import Gdk, GdkPixbuf, Gtk
    main = threading.main_thread().ident

    def wrap(owner, name, kind, static):
        original = getattr(owner, name, None)
        if original is None:
            return

        def timed(*a, **k):
            on_main = threading.get_ident() == main
            cpu = time.thread_time() if on_main else None
            start = time.monotonic()
            try:
                return original(*a, **k)
            finally:
                ART_CALLS.append((threading.get_ident(), start, time.monotonic() - start,
                                  (time.thread_time() - cpu) if on_main else None, kind,
                                  '%s.%s' % (getattr(owner, '__name__', owner), name)))
        setattr(owner, name, staticmethod(timed) if static else timed)

    for name in ('new_from_filename', 'new_from_file', 'new_from_bytes', 'new_from_resource'):
        wrap(Gdk.Texture, name, 'decode', True)
    wrap(Gdk.Texture, 'new_for_pixbuf', 'upload', True)
    wrap(Gdk.MemoryTexture, 'new', 'upload', True)
    for name in ('new_from_file', 'new_from_file_at_size', 'new_from_file_at_scale', 'new_from_stream',
                 'new_from_stream_at_scale', 'new_from_resource', 'new_from_resource_at_scale'):
        wrap(GdkPixbuf.Pixbuf, name, 'decode', True)
    for name in ('scale_simple', 'scale', 'composite'):
        wrap(GdkPixbuf.Pixbuf, name, 'scale', False)
    for name in ('write', 'write_bytes', 'close'):
        wrap(GdkPixbuf.PixbufLoader, name, 'decode', False)
    for name in ('new_for_filename', 'new_for_file', 'new_for_resource'):
        wrap(Gtk.Picture, name, 'decode', True)
    for name in ('set_filename', 'set_file', 'set_resource'):
        wrap(Gtk.Picture, name, 'decode', False)
    wrap(Gtk.Image, 'new_from_file', 'decode', True)
    wrap(Gtk.Image, 'set_from_file', 'decode', False)
    try:
        from PIL import Image
        for name in ('load', 'thumbnail', 'resize', 'reduce', 'draft'):
            wrap(Image.Image, name, 'decode' if name in ('load', 'draft') else 'scale', False)
    except ImportError:
        pass


def card_children(window):
    """The room cards that are on screen (mapped)."""
    rooms = getattr(window, 'rooms', None)
    known = getattr(rooms, 'card_for', None) or {}
    root = getattr(window, 'cards', None) or window
    found, todo = [], [root]
    import gi
    from gi.repository import Gtk
    while todo:
        widget = todo.pop()
        if isinstance(widget, Gtk.FlowBoxChild) and (not known or widget in known):
            if widget.get_mapped():
                found.append(widget)
            continue
        child = widget.get_last_child()
        while child:
            todo.append(child)
            child = child.get_prev_sibling()
    return found[::-1]


def paintable_of(widget):
    getter = getattr(widget, 'get_paintable', None)
    value = getter() if getter else None
    return value if value is not None else getattr(widget, 'paintable', None)


def largest_paintable(root):
    """(widget, paintable) of the largest descendant that shows a paintable."""
    best, todo = (None, None, 0), [root]
    while todo:
        widget = todo.pop()
        paintable = paintable_of(widget)
        if paintable is not None:
            area = widget.get_width() * widget.get_height()
            if area >= best[2]:
                best = (widget, paintable, area)
        child = widget.get_first_child()
        while child:
            todo.append(child)
            child = child.get_next_sibling()
    return best[0], best[1]


def region(window, widget, inset=4):
    ok, rect = widget.compute_bounds(window)
    if not ok or rect.get_width() < 20 or rect.get_height() < 20:
        return None
    x, y = int(rect.get_x()) + inset, int(rect.get_y()) + inset
    return (x, y, x + int(rect.get_width()) - 2 * inset, y + int(rect.get_height()) - 2 * inset)


def grab(box):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=box, xdisplay=os.environ['DISPLAY']).convert('RGB')


def changed_percent(a, b):
    from PIL import ImageChops, ImageStat
    # Many scenes move very little by design (Wizarding Valley changes ~0.02% of pixels in half a second), and a
    # still drawn by cairo on Xvfb is pixel-identical frame to frame: any change over 3 levels is motion.
    diff = ImageChops.difference(a, b).convert('L').point(lambda v: 255 if v > 3 else 0)
    return 100. * ImageStat.Stat(diff).mean[0] / 255.


def run_child(mode):
    no_core_dumps()
    faulthandler.enable(all_threads=True)
    instrument_art()   # before the application imports anything
    sys.path.insert(0, str(HERE.parent))
    import o6_env
    env = o6_env.Env(__doc__, '/tmp/listening-o10p-acceptance', extra_args=dict(FLAGS, **{'--repo': {'default': ''}}))
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

    extra = []
    if mode == 'media':
        extra = user_clips(env, env.args.extra_clips)
    with env:
        if mode == 'cold':
            result = cold(env, step)
        elif mode == 'scene':
            result = scene(env, step, check, info)
        else:
            result = media(env, step, check, info, extra)
        print('RESULT ' + json.dumps(result), flush=True)
        for window in list(env.app.get_windows()):
            window.close()
        env.pump(.3)
        print('CHECKS-DONE %d failed' % failures[0], flush=True)
    step('exiting the process')


class Rooms:
    """Card helpers on a launched window: the real single-click gesture and Esc."""
    def __init__(self, env, window):
        self.env, self.window = env, window

    def click(self, card):
        overlay = card.get_child()
        gesture = next(c for c in overlay.observe_controllers()
                       if isinstance(c, self.env.Gtk.GestureClick) and c.get_button() == 1)
        gesture.emit('pressed', 1, 12., 12.)
        gesture.emit('released', 1, 12., 12.)

    def escape(self):
        self.window.key_pressed(None, self.env.Gdk.KEY_Escape, 0, 0)

    def entry(self, card):
        return self.window.rooms.card_for[card]

    def loop_of(self, card):
        entry = self.entry(card)
        preset = next((p for p in self.window.presets if p.get('id') == entry.get('view_id')), {})
        url = preset.get('url')
        return url if preset.get('kind') == 'local' and url and str(url).endswith('.mp4') and Path(url).is_file() else None

    def loop_cards(self):
        return [c for c in card_children(self.window) if self.loop_of(c)]

    def card_named(self, name):
        return next((c for c in card_children(self.window) if self.entry(c).get('name') == name), None)

    def scene_widget(self):
        """The scene that is on screen: the largest mapped Scene (preview page or Now)."""
        import next_widgets
        best, todo = (None, 0), [self.window]
        while todo:
            widget = todo.pop()
            if isinstance(widget, next_widgets.Scene) and widget.get_mapped():
                area = widget.get_width() * widget.get_height()
                if area > best[1]:
                    best = (widget, area)
            child = widget.get_first_child()
            while child:
                todo.append(child)
                child = child.get_next_sibling()
        return best[0]

    def scene_box(self, seconds=3.):
        """The on-screen scene and its screen box, waiting up to `seconds` for a page transition to finish."""
        deadline = time.monotonic() + seconds
        while True:
            scene = self.scene_widget()
            box = scene and region(self.window, scene)
            if box is not None or time.monotonic() > deadline:
                return scene, box
            self.env.pump(.1)

    def moves(self, seconds=6., threshold=.005):
        """Wait up to `seconds` for the on-screen scene's pixels to change visibly: (moved, % changed, s)."""
        pump = self.env.pump
        start = time.monotonic()
        scene, box = self.scene_box()
        if box is None:
            print('INFO no scene on screen (page %s)' % self.window.current_page(), flush=True)
            return False, -1., time.monotonic() - start
        first, best = grab(box), 0.
        while time.monotonic() - start < seconds:
            pump(.25)
            best = max(best, changed_percent(first, grab(box)))
            if best > threshold:
                return True, best, time.monotonic() - start
        return False, best, time.monotonic() - start

    def still_for(self, seconds=2.):
        pump = self.env.pump
        scene, box = self.scene_box()
        if box is None:
            return None
        first, start, worst = grab(box), time.monotonic(), 0.
        while time.monotonic() - start < seconds:
            pump(.3)
            worst = max(worst, changed_percent(first, grab(box)))
        return worst


# ============================================================================ child: cold start

def cold(env, step):
    """Criterion 1 in a fresh process: time to Rooms, holds of the GTK thread by art, posters."""
    Gtk, GLib = env.Gtk, env.GLib
    import room_catalogue
    source = str(env.source)
    main = threading.main_thread().ident
    expected = len(room_catalogue.rooms())
    samples = []      # (t, art-named innermost application frame)
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            frame = sys._current_frames().get(main)
            art, depth = False, 0
            while frame is not None and depth < 60:
                if frame.f_code.co_filename.startswith(source):
                    art = bool(ART_FRAME.search(frame.f_code.co_name))
                    break
                frame, depth = frame.f_back, depth + 1
            samples.append((time.monotonic(), art))
            time.sleep(.005)
    segments = []     # (label, start, end, cpu s)

    def segment(label, call):
        cpu, start = time.thread_time(), time.monotonic()
        value = call()
        segments.append((label, start, time.monotonic(), time.thread_time() - cpu))
        return value

    paints = []
    thread = threading.Thread(target=sampler, daemon=True)
    del ART_CALLS[:]
    load1 = os.getloadavg()[0]
    step('cold start')
    thread.start()
    cpu0, t0 = time.thread_time(), time.monotonic()
    window = segment('NextWindow()', lambda: env.next_window.NextWindow(env.app))
    window.set_default_size(1200, 800)
    segment('present()', window.present)
    rooms_at = rooms_cpu = art_at = None
    last_refresh = time.monotonic()
    deadline = t0 + 30
    clock_connected = False
    while time.monotonic() < deadline:
        if not clock_connected and window.get_frame_clock() is not None:
            window.get_frame_clock().connect('after-paint', lambda *_: paints.append(time.monotonic()))
            clock_connected = True
        if env.context.pending():
            segment('iteration', lambda: env.context.iteration(False))
        else:
            time.sleep(.002)
        if time.monotonic() - last_refresh > .25:
            last_refresh = time.monotonic()
            segment('controller.refresh (probe)', env.controller.refresh)
        cards = card_children(window)
        if rooms_at is None and len(cards) >= expected:
            # the grid is mapped with every card; Rooms counts once the next frame is painted
            mark = time.monotonic()
            while time.monotonic() < mark + 2 and not (paints and paints[-1] >= mark):
                if env.context.pending():
                    segment('iteration', lambda: env.context.iteration(False))
                else:
                    time.sleep(.002)
            rooms_at, rooms_cpu = time.monotonic() - t0, time.thread_time() - cpu0
        if rooms_at is not None and art_at is None:
            sizes = [largest_paintable(c)[1] for c in cards]
            if cards and all(p is not None and p.get_intrinsic_width() >= 100 for p in sizes):
                art_at = time.monotonic() - t0
        if rooms_at is not None and art_at is not None and time.monotonic() - t0 > max(rooms_at, art_at) + 1.:
            break
    stop.set()
    thread.join(1)
    end = time.monotonic()
    result = {'expected_cards': expected, 'load1': round(load1, 2), 'load1_end': round(os.getloadavg()[0], 2),
              'rooms_ms': rooms_at and round(rooms_at * 1000), 'rooms_cpu_ms': rooms_cpu and round(rooms_cpu * 1000),
              'art_ms': art_at and round(art_at * 1000)}
    if rooms_at is None:
        result['error'] = 'Rooms with %d cards not reached in 30 s (cards %d)' % (expected, len(card_children(window)))
    # ---- holds by art, per segment
    interval = statistics.median([b[0] - a[0] for a, b in zip(samples, samples[1:])] or [.005])
    worst = {'wall': 0., 'cpu': 0., 'segment': None}
    gtk_calls = [c for c in ART_CALLS if c[0] == main and t0 <= c[1] <= end]
    for label, start, stop_t, cpu in segments:
        inside = [c for c in gtk_calls if start <= c[1] < stop_t]
        wall = sum(c[2] for c in inside)
        cpu_art = sum(c[3] or 0. for c in inside)
        sampled = [art for t, art in samples if start <= t < stop_t]
        sampled_art = sum(sampled) * interval
        if sampled and sampled_art > wall:      # art in application frames that no wrapper saw (C loaders)
            wall = sampled_art
            cpu_art = max(cpu_art, cpu * sum(sampled) / float(len(sampled)))
        if wall > worst['wall'] or cpu_art > worst['cpu']:
            worst['wall'], worst['cpu'] = max(worst['wall'], wall), max(worst['cpu'], cpu_art)
            worst['segment'] = {'label': label, 'segment_ms': round((stop_t - start) * 1000, 1),
                                'segment_cpu_ms': round(cpu * 1000, 1), 'art_wall_ms': round(wall * 1000, 1),
                                'art_cpu_ms': round(cpu_art * 1000, 1),
                                'calls': collections.Counter(c[5] for c in inside).most_common(4)}
    long_holds = [(label, round((b - a) * 1000), round(c * 1000)) for label, a, b, c in segments if b - a > .05]
    decodes = [c for c in gtk_calls if c[4] in ('decode', 'scale')]
    result.update(art_hold_wall_ms=round(worst['wall'] * 1000, 1), art_hold_cpu_ms=round(worst['cpu'] * 1000, 1),
                  worst_art_segment=worst['segment'], gtk_decodes=len(decodes),
                  gtk_decode_calls=collections.Counter(c[5] for c in decodes).most_common(6),
                  gtk_art_cpu_ms=round(sum(c[3] or 0. for c in gtk_calls) * 1000, 1),
                  worker_art_calls=collections.Counter(c[5] for c in ART_CALLS if c[0] != main).most_common(6),
                  sample_interval_ms=round(interval * 1000, 2), holds_over_50ms=long_holds[:20])
    # ---- posters on the cards: real art, distinct, pre-scaled
    env.pump(.5)
    cards = card_children(window)
    shots, sizes, prescaled = [], [], bool(cards)
    card_size = bool(cards)
    for card in cards:
        widget, paintable = largest_paintable(card)
        box = region(window, card, inset=2)
        if widget is None or paintable is None or box is None:
            prescaled = False
            sizes.append(None)
            continue
        size = (paintable.get_intrinsic_width(), paintable.get_intrinsic_height())
        shown = (widget.get_width(), widget.get_height())
        sizes.append({'texture': size, 'shown': shown})
        prescaled = prescaled and 0 < size[0] <= 2 * shown[0] and 0 < size[1] <= 2 * shown[1]
        card_size = card_size and 0 < size[0] <= 2 * WIDEST_CARD[0] and 0 < size[1] <= 2 * WIDEST_CARD[1]
        shots.append(grab(box))
    from PIL import ImageStat
    spread = [round(ImageStat.Stat(s.convert('L')).stddev[0], 1) for s in shots]
    distinct = len({tuple(int(v) // 12 for v in ImageStat.Stat(s.resize((8, 4))).mean) for s in shots}) if shots else 0
    pairs = [changed_percent(a.resize((160, 90)), b.resize((160, 90))) for a, b in zip(shots, shots[1:])]
    art_shown = len(shots) == expected and all(v >= 12 for v in spread) and all(p > 20 for p in pairs)
    result.update(art_shown=art_shown, art_detail={'cards': len(shots), 'luma_stddev': spread,
                                                   'neighbour_difference_pct': [round(p) for p in pairs],
                                                   'distinct_means': distinct},
                  prescaled=prescaled, card_size=card_size, texture_sizes=sizes)
    return result


# ============================================================================ child: scene guarantees

def scene(env, step, check, info):
    Gdk = env.Gdk
    pump, settle = env.pump, env.settle
    gst_log = Path(os.environ.get('GST_DEBUG_FILE', '/nonexistent'))
    started = []
    real_popen = subprocess.Popen

    class CountingPopen(real_popen):
        def __init__(self, argv, *a, **k):
            started.append(argv)
            super().__init__(argv, *a, **k)
    subprocess.Popen = CountingPopen

    def log_offset():
        try:
            return gst_log.stat().st_size
        except OSError:
            return 0

    def created(since):
        try:
            with open(gst_log, 'rb') as handle:
                handle.seek(since)
                text = handle.read().decode(errors='replace')
        except OSError:
            return None
        return [line.split('creating element "', 1)[1].split('"', 1)[0]
                for line in text.splitlines() if 'creating element "' in line]

    def audio_sinks(names):
        import gi
        try:
            gi.require_version('Gst', '1.0')
        except ValueError:
            return ['unknown: no GStreamer to classify elements']
        from gi.repository import Gst
        Gst.init(None)
        sinks = []
        for name in set(names or ()):
            factory = Gst.ElementFactory.find(name)
            klass = factory.get_metadata('klass') if factory else ''
            if 'Sink' in (klass or '') and 'Audio' in (klass or ''):
                sinks.append(name)
        return sorted(sinks)

    def wait_idle(seconds=6.):
        deadline = time.monotonic() + seconds
        while env.controller.transition and time.monotonic() < deadline:
            pump(.1)

    def media_idle_ms(seconds, baseline):
        before = threads_cpu()
        pump(seconds)
        return media_cpu_ms(before, threads_cpu(), baseline)

    step('launch')
    window = env.launch(page='rooms')
    window.show_rooms()
    pump(1.)
    rooms = Rooms(env, window)
    baseline = set(threads_cpu())
    loops = rooms.loop_cards()
    check('G0 fixture: the Rooms grid shows the catalogue rooms with scene loops (8)', len(loops) == 8,
          [rooms.entry(c).get('name') for c in card_children(window)])
    result = {}

    # ---------------------------------------------------------------- a preview plays its loop and is inert
    step('preview')
    env.log.clear()
    started.clear()
    offset = log_offset()
    # The strongest-moving loop, so "keeps moving after the clip ends" cannot be confused with a quiet passage.
    target = next((c for c in loops if rooms.loop_of(c).endswith('room-night-drive.mp4')), loops[0])
    name = rooms.entry(target)['name']
    rooms.click(target)
    moved, pct, after = rooms.moves(6.)
    info('preview "%s": moved=%s (%.2f%% after %.1f s)' % (name, moved, pct, after))
    check('G1 a preview\'s scene loop plays: its pixels move on screen', moved, pct)
    check('G2 the preview sent no controller command, started nothing and no process', not env.commands()
          and env.controller.active is None and not started, (env.commands(), env.controller.active, started))
    names = created(offset)
    sinks = audio_sinks(names)
    info('preview GStreamer elements: %s' % ', '.join(sorted(set(names or ()))))
    check('G3 the preview is silent: GStreamer worked (elements created) but no audio sink was created',
          bool(names) and not sinks, (sinks, names))
    rooms.escape()
    pump(.5)

    # ---------------------------------------------------------------- an entered room loops, at native resolution
    step('enter')
    rooms.click(target)
    pump(1.)
    widgets = window.rooms.preview_widgets
    if widgets and widgets.get('enter') is not None:
        widgets['enter'].emit('clicked')
    settle(1.5)
    wait_idle()
    pump(1.)
    check('G4 Enter room sends exactly one enter and opens Now', window.current_page() == 'now' and
          [a for a, _ in env.commands()] == ['enter'], (window.current_page(), env.commands()))
    scene_widget = rooms.scene_widget()
    _, paintable = largest_paintable(scene_widget) if scene_widget else (None, None)
    size = paintable and (paintable.get_intrinsic_width(), paintable.get_intrinsic_height())
    moved, pct, _ = rooms.moves(6.)
    entered_moved = moved      # the idle checks below only mean something after the scene really played
    check('G5 the entered room\'s loop plays (pixels move)', moved, pct)
    check('G6 the loop plays at its native resolution (%dx%d): not a downscaled stand-in' % LOOP_FORMAT,
          size == LOOP_FORMAT, size)
    step('loop past the end')
    timeline = []                   # (seconds since entering, moved, % changed) across more than one clip length
    loop_start = time.monotonic()
    while time.monotonic() - loop_start < 30.:
        moved_now, pct_now, _ = rooms.moves(2.5, threshold=1e9)
        timeline.append((round(time.monotonic() - loop_start, 1), round(pct_now, 3)))
    info('entered loop timeline (s, %% changed in 2.5 s; clip length 20 s): %s' % timeline)
    late = [pct for t, pct in timeline if t > 22.]
    moved_late = bool(late) and all(pct > .005 for pct in late)
    check('G7 scenes still loop: the entered scene keeps moving after its 20 s clip has ended', moved_late, timeline)

    # ---------------------------------------------------------------- off screen and hidden: no decoding, resumes
    step('off screen')
    window.show_rooms()
    pump(1.)
    idle_rooms = media_idle_ms(5., baseline)
    info('media threads CPU with the entered room\'s scene off screen (Rooms page): %.0f ms in 5 s' % idle_rooms)
    check('G8 an off-screen scene stops decoding: media threads use <= %d ms CPU in 5 s on the Rooms page (@@ ms)'
          % IDLE_MEDIA_CPU_MS, entered_moved and idle_rooms <= IDLE_MEDIA_CPU_MS, idle_rooms, measured=round(idle_rooms))
    window.show_now()
    pump(1.)
    resumed = rooms.moves(6.)[0]
    check('G9 back on Now, the scene moves again', resumed)
    window.set_visible(False)
    pump(1.)
    idle_hidden = media_idle_ms(5., baseline)
    info('media threads CPU with the window hidden: %.0f ms in 5 s' % idle_hidden)
    check('G10 a hidden window stops decoding: media threads use <= %d ms CPU in 5 s (@@ ms)' % IDLE_MEDIA_CPU_MS,
          resumed and idle_hidden <= IDLE_MEDIA_CPU_MS, idle_hidden, measured=round(idle_hidden))
    window.present()
    pump(1.5)
    check('G11 shown again, the scene moves again', rooms.moves(6.)[0])

    # ---------------------------------------------------------------- reduced motion: the full still, no decoding
    step('reduced motion')
    env.service.execute('settings', {'values': {'reduced_motion': True}})
    settle(1.2)
    window.show_now()
    pump(1.5)
    worst = rooms.still_for(2.)
    scene_widget = rooms.scene_widget()
    _, paintable = largest_paintable(scene_widget) if scene_widget else (None, None)
    size = paintable and (paintable.get_intrinsic_width(), paintable.get_intrinsic_height())
    shown = scene_widget and (scene_widget.get_width(), scene_widget.get_height())
    idle_still = media_idle_ms(4., baseline)
    info('reduced motion, entered: changed %.3f%%, paintable %s on %s, media CPU %.0f ms in 4 s' % (
        worst or -1, size, shown, idle_still))
    check('G12 reduced motion, entered: the scene stays still', worst is not None and worst < .005, worst)
    check('G13 reduced motion shows the still texture at full resolution (not a card-size thumbnail)',
          isinstance(paintable, Gdk.Texture) and size and shown and size[0] >= min(shown[0], 1440) * .98, (size, shown))
    check('G14 reduced motion decodes nothing: media threads <= %d ms CPU in 4 s (@@ ms)' % IDLE_MEDIA_CPU_MS,
          idle_still <= IDLE_MEDIA_CPU_MS, idle_still, measured=round(idle_still))
    window.show_rooms()
    pump(.5)
    started.clear()
    mark = len(env.log)
    rooms.click(rooms.loop_cards()[1])
    pump(1.5)
    worst = rooms.still_for(2.)
    check('G15 reduced motion, preview: the scene stays still and the preview stays inert',
          worst is not None and worst < .005 and not env.commands(mark) and not started,
          (worst, env.commands(mark), started))
    rooms.escape()
    pump(.4)
    env.service.execute('settings', {'values': {'reduced_motion': False}})
    settle(1.)
    window.show_now()
    pump(1.)
    check('G16 motion back on: the entered room\'s scene moves again', rooms.moves(6.)[0])
    subprocess.Popen = real_popen
    return result


# ============================================================================ child: media cost

def user_clips(env, count):
    """Distinct copies of catalogue loops under the child's own temporary folder: user clips beyond the catalogue."""
    assets = Path(os.environ.get('LISTENING_HUB_ASSETS', ''))
    sources = sorted(assets.glob('room-*.mp4'), key=lambda p: p.stat().st_size)[:4]
    if not sources or count <= 0:
        return []
    folder = Path(tempfile.mkdtemp(prefix='o10p-clips-'))
    clips = []
    for index in range(count):
        target = folder / ('my-clip-%02d.mp4' % index)
        shutil.copyfile(sources[index % len(sources)], target)   # a real copy: a different file, not a link
        clips.append(str(target))
    return clips


def add_user_rooms(clips):
    """Extra room cards whose scene is a user clip: the same product path (preset kind 'local') as the catalogue."""
    import ambience
    import room_catalogue
    presets, rooms = ambience.presets, room_catalogue.rooms

    def more_presets():
        found = presets()
        base = next((p for p in found if p.get('id', '').startswith('room-')), {})
        for index, clip in enumerate(clips):
            found.append(dict(base, id='user-clip-%02d' % index, name='My clip %d' % (index + 1), url=clip,
                              kind='local', available=True))
        return found

    def more_rooms():
        found = rooms()
        base = next((r for r in found if r.get('view_id')), None)
        if base is None:
            return found
        for index, _ in enumerate(clips):
            found.append(dict(base, id='user-room-%02d' % index, name='My clip room %d' % (index + 1),
                              view_id='user-clip-%02d' % index, last_entered=False))
        return found
    ambience.presets = more_presets
    room_catalogue.rooms = more_rooms


def media(env, step, check, info, clips):
    pump = env.pump
    gst_log = Path(os.environ.get('GST_DEBUG_FILE', '/nonexistent'))

    def log_offset():
        try:
            return gst_log.stat().st_size
        except OSError:
            return 0

    def pipelines(since):
        try:
            with open(gst_log, 'rb') as handle:
                handle.seek(since)
                text = handle.read().decode(errors='replace')
        except OSError:
            return None
        names = [line.split('creating element "', 1)[1].split('"', 1)[0]
                 for line in text.splitlines() if 'creating element "' in line]
        return sum(1 for n in names if n in PIPELINE_ELEMENTS), collections.Counter(names)

    peak = {'decoder_threads': 0, 'threads': 0}

    def measure(label):
        for _ in range(2):
            import gc
            gc.collect()
            pump(.4)
        point, names = sample(label)
        info('media %s: %s' % (label, json.dumps(point)))
        info('media %s thread names: %s' % (label, json.dumps(names)))
        return point

    def note_peak():
        point, _ = sample()
        peak['decoder_threads'] = max(peak['decoder_threads'], point['decoder_threads'])
        peak['threads'] = max(peak['threads'], point['threads'])

    def preview(card, wait=6.):
        rooms.click(card)
        moved, pct, after = rooms.moves(wait)
        note_peak()
        rooms.escape()
        pump(.35)
        return moved, round(pct, 2), round(after, 1)

    if clips:
        add_user_rooms(clips)
    step('launch')
    window = env.launch(page='rooms')
    window.show_rooms()
    pump(2.)
    rooms = Rooms(env, window)
    catalogue = [c for c in rooms.loop_cards() if not rooms.entry(c).get('id', '').startswith('user-room-')]
    users = [c for c in rooms.loop_cards() if rooms.entry(c).get('id', '').startswith('user-room-')]
    files = {rooms.loop_of(c) for c in catalogue}
    check('M0 fixture: 8 catalogue loop rooms (8 distinct loop files) and %d user-clip rooms' % len(clips),
          len(catalogue) == 8 and len(files) == 8 and len(users) == len(clips), (len(catalogue), len(files), len(users)))
    cold_point = measure('cold, before any preview')
    baseline = set(threads_cpu())
    result = {'uptime_start': uptime(), 'cold': cold_point}

    step('every catalogue loop once')
    played, first = [], None
    for card in catalogue:
        played.append((rooms.entry(card)['name'],) + preview(card))
        if first is None:          # the first loop carries the one-time set-up; M2r judges the loops after it
            pump(3.)
            first = measure('after the first catalogue loop')
    info('catalogue previews (name, moved, %% changed, s): %s' % played)
    check('M1 every catalogue loop visibly played in its preview before its cost was measured',
          all(p[1] for p in played), played)
    pump(3.)
    warm = measure('warm: every catalogue loop shown, no preview open')
    cost = warm['rss_mb'] - cold_point['rss_mb']
    all_played = bool(played) and all(p[1] for p in played)   # a cost measured without playback proves nothing
    result.update(warm=warm, catalogue_played=played, media_cost_mb=round(cost, 1))
    info('media cost for 8 loops: %.1f MB (%.1f MB per loop); threads +%d; decoder threads %d (peak %d)' % (
        cost, cost / 8., warm['threads'] - cold_point['threads'], warm['decoder_threads'], peak['decoder_threads']))
    # M2 [ORIGINAL] still runs and prints as a SUPERSEDED line, not counted: on 3507ca3 it lands either side of 400 MB
    # (371 in one run, 421-493 in the implementer's), so it can be neither an expected pass nor a known failure.
    print('SUPERSEDED %s M2 [ORIGINAL, superseded by M2r] steady-state media cost with all 8 catalogue loops shown is '
          'under %d MB (process tree RSS, %d MB)' % ('PASS' if all_played and cost < MEDIA_TOTAL_MB else 'FAIL',
                                                     MEDIA_TOTAL_MB, round(cost)), flush=True)
    per_loop = (warm['rss_mb'] - first['rss_mb']) / max(1., len(catalogue) - 1.)
    info('media cost: first loop %.1f MB (one-time set-up included), then %.1f MB per loop' % (
        first['rss_mb'] - cold_point['rss_mb'], per_loop))
    result.update(first=first, per_loop_mb=round(per_loop, 1))
    check('M2r media cost with all 8 catalogue loops shown is <= %d MB and each loop after the first adds <= %d MB '
          '(process tree RSS, @@ MB total, @@ MB per loop)' % (MEDIA_TOTAL_R_MB, MEDIA_PER_LOOP_R_MB),
          all_played and cost <= MEDIA_TOTAL_R_MB and per_loop <= MEDIA_PER_LOOP_R_MB,
          (cold_point['rss_mb'], first['rss_mb'], warm['rss_mb']), measured=(round(cost), float(round(per_loop, 1))))
    check('M3 decoder threads are bounded: at most %d in the whole process tree, warm and at peak (@@ warm, @@ peak)'
          % DECODER_THREADS_MAX, all_played and max(warm['decoder_threads'], peak['decoder_threads']) <= DECODER_THREADS_MAX,
          (warm['decoder_threads'], peak['decoder_threads']),
          measured=(warm['decoder_threads'], peak['decoder_threads']))
    # M4 [ORIGINAL] still runs and prints as a SUPERSEDED line, not counted (3507ca3: +80, 10 threads per paused
    # pipeline): a known failure cannot carry a measured span, and the exact count is not part of the intent.
    added = warm['threads'] - cold_point['threads']
    print('SUPERSEDED %s M4 [ORIGINAL, superseded by M4-R1] the media adds at most %d threads once every loop was shown '
          '(+%d)' % ('PASS' if all_played and added <= MEDIA_THREADS_MAX else 'FAIL', MEDIA_THREADS_MAX, added), flush=True)
    shown_cap = MEDIA_THREADS_PER_LOOP * min(len(catalogue), LOOP_CAPACITY)
    check('M4-R1 the media adds at most %d threads per loop shown, here %d for %d catalogue loops, never more than %d '
          '(@@)' % (MEDIA_THREADS_PER_LOOP, shown_cap, len(catalogue), MEDIA_THREADS_PER_LOOP * LOOP_CAPACITY),
          all_played and added <= shown_cap, (cold_point['threads'], warm['threads']), measured='+%d' % added)
    before = threads_cpu()
    pump(5.)
    idle = media_cpu_ms(before, threads_cpu(), baseline)
    info('media threads CPU on the Rooms grid with every loop off screen: %.0f ms in 5 s' % idle)
    check('M5 off-screen loops are paused: media threads use <= %d ms CPU in 5 s on the grid (@@ ms)'
          % IDLE_MEDIA_CPU_MS, all_played and idle <= IDLE_MEDIA_CPU_MS, idle, measured=round(idle))

    step('steady state')
    offset = log_offset()
    steady_played, halfway = [], None
    for cycle in range(env.args.steady_cycles):
        card = catalogue[cycle % len(catalogue)]
        if cycle % 5 == 4:
            steady_played.append(preview(card)[0])
        else:
            rooms.click(card)
            pump((.5, .8, 1.2)[cycle % 3])
            rooms.escape()
            pump(.35)
        if cycle + 1 == env.args.steady_cycles // 2:
            halfway = measure('after %d more previews' % (cycle + 1))
    pump(3.)
    end = measure('after %d more previews' % env.args.steady_cycles)
    made = pipelines(offset)
    result.update(steady=end, steady_halfway=halfway, steady_pipelines=made and made[0],
                  steady_elements=made and dict(made[1].most_common(12)))
    info('GStreamer elements created during the %d steady previews: %s' % (
        env.args.steady_cycles, made and dict(made[1].most_common(12))))
    check('M6 steady state: loops still play in the sampled previews (every 5th of %d)' % env.args.steady_cycles,
          steady_played and all(steady_played), steady_played)
    # Steady means flat: no growth over the second half of the cycles, and a bounded one-time step after warm-up
    # (main opened 12-13 fds once in the first 20 previews and none after).
    half = halfway or warm
    last_n = env.args.steady_cycles - env.args.steady_cycles // 2
    leak_ok = (all_played and end['rss_mb'] <= half['rss_mb'] + STEADY_RSS_MB
               and end['threads'] <= half['threads'] + STEADY_THREADS and end['fds'] <= half['fds'] + STEADY_FDS
               and end['rss_mb'] <= warm['rss_mb'] + 2 * STEADY_RSS_MB
               and end['threads'] <= warm['threads'] + 2 * STEADY_THREADS)
    # M7 [ORIGINAL] still runs and prints as a SUPERSEDED line, not counted: its fd allowance since warm-up (2 x 12)
    # sits next to the one-time step (main +12-13, 3507ca3 +28), so it can be neither an expected pass nor a known
    # failure. M7-R1 (coordinator ruling) keeps every leak bound and allows a one-time fd step of <= +40.
    print('SUPERSEDED %s M7 [ORIGINAL, superseded by M7-R1] steady state: over the last %d previews RSS grows <= %d MB, '
          'threads <= %d, fds <= %d; since warm-up at most twice that (fds +%d since warm-up)' % (
              'PASS' if leak_ok and end['fds'] <= warm['fds'] + 2 * STEADY_FDS else 'FAIL', last_n, STEADY_RSS_MB,
              STEADY_THREADS, STEADY_FDS, end['fds'] - warm['fds']), flush=True)
    check('M7-R1 steady state: over the last %d previews RSS grows <= %d MB, threads <= %d, fds <= %d; since warm-up '
          'RSS and threads at most twice that, fds <= +%d (RSS @@ MB, threads @@, fds @@ over the last %d; fds @@ since warm-up)' % (last_n, STEADY_RSS_MB, STEADY_THREADS, STEADY_FDS,
                                                                   STEADY_FDS_STEP_R, last_n),
          leak_ok and end['fds'] <= warm['fds'] + STEADY_FDS_STEP_R, (warm, half, end),
          measured=tuple('%+d' % round(v) for v in (end['rss_mb'] - half['rss_mb'], end['threads'] - half['threads'],
                                                     end['fds'] - half['fds'], end['fds'] - warm['fds'])))
    check('M8 no-teardown guarantee: no GL display or context is left behind per preview (GL threads <= warm)',
          all_played and end['gl_threads'] <= warm['gl_threads'], (warm['gl_threads'], end['gl_threads']))
    check('M9 no-teardown guarantee: no new GStreamer pipeline is built once every loop has been shown (@@ built)',
          all_played and made is not None and made[0] == 0, made and (made[0], dict(made[1].most_common(8))),
          measured=made[0] if made else 'unknown')

    def user_preview(card):
        """(moved, % changed, s, luma spread of the scene): a clip that does not move must still show its still."""
        rooms.click(card)
        moved, pct, after = rooms.moves(6.)
        note_peak()
        _, box = rooms.scene_box()
        spread = -1.
        if box is not None:
            from PIL import ImageStat
            spread = round(ImageStat.Stat(grab(box).convert('L')).stddev[0], 1)
        rooms.escape()
        pump(.35)
        return moved, round(pct, 2), round(after, 1), spread

    step('user clips')
    played_users, points = [], []
    for half in (users[:len(users) // 2], users[len(users) // 2:]):
        for card in half:
            played_users.append(user_preview(card))
        pump(3.)
        points.append(measure('after %d user clips (%d distinct loop files)' % (
            len(played_users), 8 + len(played_users))))
    result.update(users_played=played_users, user_points=points, uptime_end=uptime())
    if len(points) == 2 and users:
        total = points[1]['rss_mb'] - cold_point['rss_mb']
        last = len(users) - len(users) // 2
        per = lambda key: (points[1][key] - points[0][key]) / float(last)
        info('user clips: total media cost %.1f MB for %d files; the last %d clips added %.1f MB, %.1f threads, '
             '%.1f fds each' % (total, 8 + len(users), last, per('rss_mb'), per('threads'), per('fds')))
        moving = [p[0] for p in played_users]
        check('U1 [ORIGINAL, superseded by U1r] every user clip visibly played in its preview', all(moving), played_users)
        # Revision U1r/U2r (acceptance author, phase 2; the coordinator asked to judge "bounded" against the
        # implementer's LOOP_LIMIT of 12 loop files per process). Bounded cost here means a fixed number of loop
        # pipelines: an evicted one would be a teardown, which the 2.3.2 guarantee forbids. Clips beyond the
        # window's capacity fall back to their still, the same graceful layer failure as reduced motion or no
        # GStreamer. Every loop the product can show today must still fit: the 8 rooms, the 2 lake views and the
        # optional local preview clip, so at least 3 user clips beyond the catalogue must move. Clips that do not
        # move must show a real still (not blank), only after the capacity is used up (a prefix), and the
        # catalogue's loops must keep playing afterwards. U2 is re-based with M2r: <= 520 MB plus <= 50 MB per user
        # clip that got a loop (U3 still requires the cost to stop growing).
        users_moved = sum(moving)
        prefix = moving == sorted(moving, reverse=True)
        stills_ok = all(p[3] >= 12 for p in played_users if not p[0])
        after_users = [preview(card)[0] for card in catalogue[:2]]
        users_ok = users_moved >= 3 and prefix and stills_ok and all(after_users)
        info('user clips: %d of %d moved; prefix %s; stills %s; catalogue loops afterwards %s' % (
            users_moved, len(moving), prefix, [p[3] for p in played_users if not p[0]], after_users))
        check('U1r user clips: at least 3 beyond the catalogue get a moving loop (@@ did), clips past the window\'s '
              'loop capacity show their still, and the catalogue loops still play afterwards',
              users_ok, (played_users, after_users), measured=users_moved)
        # U2 [ORIGINAL] still runs and prints as a SUPERSEDED line, not counted: a known failure cannot carry a measured span.
        print('SUPERSEDED %s U2 [ORIGINAL, superseded by U2r] user clips are bounded: total media cost with 8 catalogue '
              'loops and %d user clips <= %d MB (%d MB)' % ('PASS' if all(moving) and total <= EXTRA_TOTAL_MB else 'FAIL',
                                                            len(users), EXTRA_TOTAL_MB, round(total)), flush=True)
        cap = MEDIA_TOTAL_R_MB + MEDIA_PER_LOOP_R_MB * users_moved
        check('U2r user clips are bounded: total media cost with 8 catalogue loops and %d user clips <= %d MB + %d MB '
              'per user clip with a loop (@@ MB against @@ MB)' % (len(users), MEDIA_TOTAL_R_MB, MEDIA_PER_LOOP_R_MB),
              users_ok and total <= cap, (cold_point['rss_mb'], points[1]['rss_mb'], users_moved),
              measured=(round(total), cap))
        loops_shown = min(len(catalogue) + users_moved, LOOP_CAPACITY)
        added_u = points[1]['threads'] - cold_point['threads']
        check('M4-R1u with user clips the media adds at most %d threads per loop shown and never more than %d '
              '(@@ against @@)' % (MEDIA_THREADS_PER_LOOP, MEDIA_THREADS_PER_LOOP * LOOP_CAPACITY),
              users_ok and added_u <= MEDIA_THREADS_PER_LOOP * loops_shown,
              (cold_point['threads'], points[1]['threads'], loops_shown),
              measured=('+%d' % added_u, MEDIA_THREADS_PER_LOOP * loops_shown))
        check('U3 user clips are bounded: each of the last %d clips adds <= %d MB, <= %d thread, <= %d fds '
              '(@@ MB, @@ threads, @@ fds)'
              % (last, EXTRA_PER_CLIP_MB, EXTRA_PER_CLIP_THREADS, EXTRA_PER_CLIP_FDS),
              users_ok and per('rss_mb') <= EXTRA_PER_CLIP_MB and per('threads') <= EXTRA_PER_CLIP_THREADS
              and per('fds') <= EXTRA_PER_CLIP_FDS, (points[0], points[1]),
              measured=(round(per('rss_mb'), 1), round(per('threads'), 1), round(per('fds'), 1)))
        check('U4 decoder threads stay bounded with user clips (<= %d, @@, peak @@)' % DECODER_THREADS_MAX,
              users_ok and max(points[1]['decoder_threads'], peak['decoder_threads']) <= DECODER_THREADS_MAX,
              (points[1]['decoder_threads'], peak['decoder_threads']),
              measured=(points[1]['decoder_threads'], peak['decoder_threads']))
    for clip in clips:
        try:
            os.unlink(clip)
        except OSError:
            pass
    if clips:
        shutil.rmtree(Path(clips[0]).parent, ignore_errors=True)
    return result


if __name__ == '__main__':
    arguments = parse()
    if arguments.child:
        run_child(arguments.child)
    else:
        if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
            reexec_isolated()
        run_parent(arguments)
