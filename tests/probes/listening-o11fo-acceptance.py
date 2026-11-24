"""O11F-OBSERVE acceptance: the operator can see whether scene-loop shedding is working.

Written by the independent acceptance author from the O11F-OBSERVE brief, not from the implementation.
Real GTK on a private Xvfb with its own D-Bus (this file re-executes itself there once), the real session
controller on the session test double, fixture providers. Nothing reaches the desktop session, the network,
systemd or credentials; no audio is played on the host; children disable core dumps.

What is read: `listening-roomctl inspect`, run in the window's own process (roomctl.main() with argv
['inspect'], its stdout parsed as the JSON it prints), so whatever state the window publishes for it is
what the oracle sees. The brief leaves the field names and formats open, so the oracles read MEANING:

  * the shedding report is every leaf of the snapshot whose key path has a word starting "shed" or "resum"
    ("published" and "finished" do not match);
  * "active" is the boolean (or active/off string) under an active/enabled/on/engaged/shedding/status/state key;
  * a count is any whole number under a shed... or resum... key; a count TRACKS when one such number equals
    what really happened at every checkpoint, so a report that only gives "loops shed now" fails once loops
    resume, and a hard-coded number fails at the next checkpoint. Two readings of "how many loops have shed"
    are both accepted (every shed event, or each loop once): FLAGGED FOR A RULING;
  * the reason is the report's text; GTK / libgstgl / references are matched by word, the reference count by
    a whole number plus the word "ref".

The real activity is counted by the probe itself, independently of the report: it wraps next_widgets'
_PLAY['shed'] (a shed happened when it returned a renderer) and _PLAY['renew'] (a resume), and reads
_LOOPS[...].shed. The wrappers only observe.

Two ways of reading, both required (coordinator ruling, phase 1): in-process (above), and CROSS-PROCESS: the
child serves its controller on a real unix socket (session_service.make_server, in a private temp dir named by
LISTENING_SESSION_SOCKET) and runs the shipped entry point (`python <source>/roomctl.py inspect`, what the
listening-roomctl wrapper runs) as a separate process against the running window, pumping GTK while it runs.
That is what the operator does in a terminal. Checks X1-X5 are the cross-process ones.

Children (each a subprocess, so a crash is a result):

  active  one window: no window yet (inspect readable, shedding machinery untouched); 3 catalogue loops shown and
          left over 4 rounds with inspect polled throughout (counts at every checkpoint; first shows are not resumes);
          10 quick bounces (counts follow sheds/resumes, not clicks); inspect hammered with loops shed (nothing
          changes); the window closed (inspect still readable).
  gate    the GTK version gate patched not to match (as the O11-MEDIA fallback-gate child does): not active, reason
          names GTK, counts zero, plain language.
  gl      libgstgl missing (ctypes.CDLL raises OSError for it before the first loop loads): same, reason names libgstgl.
  refs    an extra reference on the first loop's video sink (as the O11-MEDIA fallback-refs child does), so the
          release is refused for real: not off before the refusal, not active after, the reason shows the observed
          reference count; the loops still play and resume.

Parent: the three reasons differ from each other; the `tests/source_tree.py` edge case in subprocesses with
scratch trees (the --source tree is on PYTHONPATH and LISTENING_HUB_SOURCE names another tree: --source wins;
the dev loop's PYTHONPATH export alone is still not a choice); the CHANGELOG entry (a source-text check:
documentation has no behaviour).

Author-set limits (FLAGGED FOR A RULING in the phase-1 report): SHED_WAIT_S = 20 (how long loops may take to
shed after the last leave), ROUNDS = 4 and LOOPS = 3 (workload size), BOUNCES = 10, a reference count is
accepted when it is a whole number from 1 to 64.

Writes OUTPUT/o11fo-acceptance.json (judged by acceptance_verdict.py against expected/o11fo.checks) and
OUTPUT/o11fo-summary.json. Ends with "N passed, M failed".

  scripts/listening-dev.sh acceptance OUT o11fo, or scripts/listening-dev.sh probe OUT listening-o11fo-acceptance
  (LISTENING_O11FO_ARGS='--only active' for one part)

Criterion revisions are made in place, keeping the original visible as "[ORIGINAL, superseded by ...]".
"""
import argparse
import contextlib
import faulthandler
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
_spec = importlib.util.spec_from_file_location('o10p_method', str(HERE.parent / 'listening-o10p-acceptance.py'))
o10p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(o10p)       # definitions only; its main is guarded
_spec = importlib.util.spec_from_file_location('o11m_method', str(HERE.parent / 'listening-o11m-acceptance.py'))
o11m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(o11m)       # definitions only (_hold_video_sink, _quiet); its main is guarded
uptime, no_core_dumps = o10p.uptime, o10p.no_core_dumps

MODES = ('active', 'gate', 'gl', 'refs', 'junk', 'stale', 'reuse', 'racy')
LOOPS = 3             # author: loops used for the count checkpoints
ROUNDS = 4            # author: show-all / leave-all rounds
BOUNCES = 10          # author: quick show/leave pairs (each far under the product's 1 s release wait)
SHED_WAIT_S = 20.     # author: how long the loops may take to shed after the last leave
SETTLE_S = 3.         # author: how long a report may lag the real activity (it is saved on a worker thread)
REFS_RANGE = (1, 64)  # author: a plausible observed reference count


def parse():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source', default=str(REPO / 'src'))
    parser.add_argument('--output', default='/tmp/listening-o11fo-acceptance')
    parser.add_argument('--repo', default='')
    parser.add_argument('--child', default='')
    parser.add_argument('--only', default='')    # comma list: active,gate,gl,refs,tree,notes
    return parser.parse_known_args(sys.argv[1:] + shlex.split(os.environ.get('LISTENING_O11FO_ARGS', '')))[0]


def reexec_isolated():
    runtime = tempfile.mkdtemp(prefix='listening-o11fo-runtime-')
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


# ---------------------------------------------------------------------------- reading the report

SHED = re.compile(r'(?<![a-z])shed')
ACTIVE_KEYS = {'active', 'enabled', 'on', 'engaged', 'working', 'shedding', 'status', 'state'}
COUNT_WORDS = {'loops', 'count', 'counts', 'total', 'n', 'number', 'events', 'so_far'}


def leaves(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, path + (str(key).lower(),))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from leaves(item, path + (str(index),))
    else:
        yield path, value


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def status_of(snapshot):
    """The shedding report in an inspect snapshot, read by meaning (see the module docstring)."""
    # 'resume_offer' is the existing "Resume where you left off" offer, not loop shedding
    view = [(p, v) for p, v in leaves(snapshot) if p[0] != 'resume_offer'
            and any(SHED.search(s) or 'resum' in s for s in p)]
    active = None
    for path, value in view:
        if path[-1] in ACTIVE_KEYS and isinstance(value, bool):
            active = value
            break
    if active is None:
        for path, value in view:
            if path[-1] in ACTIVE_KEYS and isinstance(value, str):
                text = value.strip().lower()
                # [ORIGINAL, superseded by the ruled interface (phase 2): only the first alternatives, no state names]
                # The ruled `state` values: active | gtk | no_libgstgl | refused | no_player | unreported.
                if re.match(r'(not active|inactive|off|disabled|no\b|not\b|unavailable|stopped|switched off|gtk|no_libgstgl|'
                            r'refused|no_player|unreported)', text):
                    active = False
                elif re.match(r'(active|on\b|enabled|engaged|working|yes)', text):
                    active = True
                break

    def counts(word):
        found = {}
        for path, value in view:
            if not is_int(value):
                continue
            last, joined = path[-1], '/'.join(path)
            if word == 'shed':
                named = (SHED.search(last) and 'resum' not in last) or (
                    last in COUNT_WORDS and any(SHED.search(s) for s in path[:-1]) and 'resum' not in joined)
            else:
                named = 'resum' in last or (last in COUNT_WORDS and any('resum' in s for s in path[:-1]))
            if named and 'ref' not in last and 'gtk' not in last:
                found[joined] = value
        return found
    texts = [v for _, v in view if isinstance(v, str)]
    text = ' '.join(texts).lower()
    refs = [v for p, v in view if is_int(v) and 'ref' in p[-1]] + [int(n) for n in re.findall(r'\d+', text)]
    return {'found': bool(view), 'active': active, 'shed': counts('shed'), 'resumed': counts('resum'),
            'texts': texts, 'text': text, 'refs': refs, 'view': [('/'.join(p), v) for p, v in view]}


def plain(st):
    """Plain enough to read: some text of three words or more, no Python reprs or raw addresses in it."""
    if not any(len(t.split()) >= 3 for t in st['texts']):
        return False
    return not any(re.search(r'0x[0-9a-f]{4}|<|>|\{|\[|Traceback|\bNone\b|\bTrue\b|\bFalse\b|object at|_[a-z]+_', t)
                   for t in st['texts'])


def inspect_cli():
    """`listening-roomctl inspect` in this process: (exit code, parsed JSON or None, stdout, stderr)."""
    import roomctl
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv
    sys.argv = ['listening-roomctl', 'inspect']
    code = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = roomctl.main()
    except SystemExit as exit_:
        code = exit_.code
    finally:
        sys.argv = saved
    try:
        data = json.loads(out.getvalue())
    except ValueError:
        data = None
    return code, data, out.getvalue(), err.getvalue()


def look():
    try:
        code, data, out, err = inspect_cli()
    except Exception as error:      # inspect crashed: reported as unreadable, with the exception
        return dict(status_of({}), readable=False, code='raised', err=repr(error)[-300:])
    ok = code in (0, None) and isinstance(data, dict) and len(out.strip().splitlines()) == 1
    return dict(status_of(data if isinstance(data, dict) else {}), readable=ok, code=code, err=err[-300:])


def catches_up(st, events, distinct):
    """Whether a report's shed and resumed numbers have caught up with what really happened (events or distinct loops)."""
    return (any(v in (events[0], distinct[0]) for v in st['shed'].values())
            and any(v in (events[1], distinct[1]) for v in st['resumed'].values()))


def settle(env, truth, limit=SETTLE_S):
    """Both readings of the report, once they have caught up with the real activity (up to `limit` s: the window saves its
    status on a worker thread, so a report may lag a shed by a moment). [ORIGINAL, superseded by this wait: read at once;
    the first positive run on 60d5b63 saw reports one write behind at mid-activity checkpoints.]"""
    deadline = time.monotonic() + limit
    while True:
        st, rst = look(), env.remote.look()
        events, distinct = truth.now()
        if (catches_up(st, events, distinct) and catches_up(rst, events, distinct)) or time.monotonic() > deadline:
            return st, rst
        env.pump(.2)


def tracking_paths(checkpoints, key, field='st'):
    """The report paths for `key` ('shed' or 'resumed') that match what really happened at every checkpoint, under
    either reading of "how many loops have shed": events (every shed counts) or distinct loops (each loop once)."""
    result = {}
    for reading, name in ((0, 'events'), (1, 'distinct loops')):
        paths = None
        for cp in checkpoints:
            want = cp[key][reading]
            here = {p for p, v in cp[field][key].items() if v == want}
            paths = here if paths is None else paths & here
        if paths:
            result[name] = sorted(paths)
    return result


class Remote:
    """The controller on a real socket, and roomctl (the shipped script) run against it as its own process."""
    def __init__(self, env):
        import threading
        import session_service
        self.env = env
        self.dir = tempfile.mkdtemp(prefix='o11fo-sock-')
        self.path = Path(self.dir) / 'session.sock'
        self.server, self.lock = session_service.make_server(self.path, env.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def look(self, limit=30.):
        environment = {k: v for k, v in os.environ.items() if k not in ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE')}
        environment['LISTENING_SESSION_SOCKET'] = str(self.path)
        process = subprocess.Popen([sys.executable, str(self.env.source / 'roomctl.py'), 'inspect'], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.dir)
        deadline = time.monotonic() + limit
        while process.poll() is None and time.monotonic() < deadline:
            self.env.pump(.1)       # the window's main loop keeps running while the other process asks
        if process.poll() is None:
            process.kill()
        out, err = process.communicate()
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        ok = process.returncode == 0 and isinstance(data, dict) and len(out.strip().splitlines()) == 1
        return dict(status_of(data if isinstance(data, dict) else {}), readable=ok, code=process.returncode, err=err[-300:])

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------------------- ground truth

class Truth:
    """What really happened, counted outside the product: the wrapped shed and renew calls."""
    def __init__(self, nw):
        self.nw = nw
        self.shed = self.renew = 0
        self.shed_players, self.renew_players = set(), set()

    def arm(self):
        play = getattr(self.nw, '_PLAY', {})
        truth = self

        def wrap_shed(original):
            def shed(player):
                renderer = original(player)
                if renderer:
                    truth.shed += 1
                    truth.shed_players.add(player)
                return renderer
            shed.o11fo = True
            return shed

        def wrap_renew(original):
            def renew(player, renderer):
                truth.renew += 1
                truth.renew_players.add(player)
                return original(player, renderer)
            renew.o11fo = True
            return renew
        if play.get('shed') and not getattr(play['shed'], 'o11fo', False):
            play['shed'] = wrap_shed(play['shed'])
        if play.get('renew') and not getattr(play['renew'], 'o11fo', False):
            play['renew'] = wrap_renew(play['renew'])

    def now(self):
        return (self.shed, self.renew), (len(self.shed_players), len(self.renew_players))


def loops_of(nw):
    return list(getattr(nw, '_LOOPS', {}).values())


def loop_states(nw):
    return [(id(l), bool(l.shed), bool(l.released), bool(l.wanted), l.releasing) for l in loops_of(nw)]


def wait(env, cond, limit, tick=.25, poll=None):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if cond():
            return True
        env.pump(tick)
        if poll:
            poll()
    return cond()


# ---------------------------------------------------------------------------- the children

def run_child(mode, args):
    no_core_dumps()
    faulthandler.enable(all_threads=True)
    sys.path.insert(0, str(HERE.parent))
    import o6_env
    env = o6_env.Env(__doc__, '/tmp/listening-o11fo-acceptance', extra_args={
        '--child': {'default': ''}, '--only': {'default': ''}, '--repo': {'default': ''}})
    began = time.monotonic()
    failures = [0]

    def step(text):
        print('STEP %s  @%.1fs' % (text, time.monotonic() - began), flush=True)

    def check(name, ok, detail=''):
        failures[0] += 0 if ok else 1
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:600]), flush=True)

    def info(text):
        print('INFO ' + text, flush=True)

    with env:
        env.remote = Remote(env)
        if mode == 'active':
            result = active(env, step, check, info)
        elif mode == 'victim':
            result = victim(env, step)
        elif mode == 'junk':
            result = junk(env, step, check, info)
        elif mode == 'stale':
            result = stale(env, step, check, info, args)
        elif mode in ('reuse', 'racy'):
            result = start(env, step, check, info, mode)
        else:
            result = fallback(env, step, check, info, mode)
        print('RESULT ' + json.dumps(result, default=str), flush=True)
        for window in list(env.app.get_windows()):
            window.close()
        env.pump(.3)
        env.remote.close()
        print('CHECKS-DONE %d failed' % failures[0], flush=True)
    step('exiting the process')


def active(env, step, check, info):
    import next_widgets as nw
    pump = env.pump
    # ---- no window ever opened
    step('no window yet')
    play_before = dict(getattr(nw, '_PLAY', {}))
    st0 = look()
    play_after = dict(getattr(nw, '_PLAY', {}))
    check('N1 with no window ever opened, inspect is readable (exit 0, one JSON line) and has a shedding report that does '
          'not claim shedding is active',
          st0['readable'] and st0['found'] and st0['active'] is not True,
          (st0['readable'], st0['code'], st0['err'], st0['view']))
    check('N1b inspect did not start the shedding machinery: the GStreamer calls it would load are untouched',
          play_before.keys() == play_after.keys() and all(play_before[k] is play_after[k] for k in play_before),
          (sorted(play_before), sorted(play_after)))
    rst0 = env.remote.look()
    check('X3 as a separate process (shipped roomctl.py over the real socket) with no window open, inspect is readable and '
          'has a shedding report that does not claim shedding is active',
          rst0['readable'] and rst0['found'] and rst0['active'] is not True,
          (rst0['readable'], rst0['code'], rst0['err'], rst0['view']))
    step('launch')
    window = env.launch(page='rooms')
    window.show_rooms()
    pump(2.)
    rooms = o10p.Rooms(env, window)
    catalogue = rooms.loop_cards()
    cards = catalogue[:LOOPS]
    check('A0 fixture: at least %d catalogue loop rooms with distinct loop files' % LOOPS,
          len(catalogue) >= LOOPS and len({rooms.loop_of(c) for c in cards}) == LOOPS, len(catalogue))
    truth = Truth(nw)
    checkpoints, problems, all_moved = [], [], []

    def inspect_quietly():
        try:
            look()
        except Exception as error:   # polled under load; the checkpoint calls judge the report
            problems.append(repr(error))

    def checkpoint(label, expect_shed, expect_resumed):
        """Read the report; record it with what the wrappers counted and what the workload implies (None: unknowable)."""
        st, rst = settle(env, truth)
        events, distinct = truth.now()
        checkpoints.append({'label': label, 'st': st, 'rst': rst, 'truth': (events, distinct), 'expect': (expect_shed, expect_resumed),
                            'currently': sum(bool(l.shed) for l in loops_of(nw)),
                            'shed': (events[0], distinct[0]), 'resumed': (events[1], distinct[1])})
        return st

    def show(card, first=False):
        rooms.click(card)
        if first:
            all_moved.append(rooms.moves(6.)[0])
            for _ in range(20):
                truth.arm()
                if getattr(nw, '_PLAY', {}).get('shed'):
                    break
                pump(.25)
        else:
            pump(1.)
            truth.arm()
        inspect_quietly()

    def shed_all():
        return wait(env, lambda: len(loops_of(nw)) >= LOOPS and sum(bool(l.shed) for l in loops_of(nw)) >= LOOPS,
                    SHED_WAIT_S, poll=inspect_quietly)

    step('round 1: first shows')
    for index, card in enumerate(cards):
        show(card, first=True)
        if index < LOOPS - 1:
            rooms.escape()
            pump(.5)
    st1 = checkpoint('round 1 shown once', None, 0)
    check('A1 with a loop on screen and nothing refused, inspect reports scene-loop shedding active',
          st1['readable'] and st1['found'] and st1['active'] is True, (st1['readable'], st1['view']))
    check('X1 as a separate process (shipped roomctl.py over the real socket) with a loop on screen, inspect reports '
          'scene-loop shedding active', checkpoints[-1]['rst']['readable'] and checkpoints[-1]['rst']['found']
          and checkpoints[-1]['rst']['active'] is True, (checkpoints[-1]['rst']['readable'], checkpoints[-1]['rst']['code'],
                                                         checkpoints[-1]['rst']['err'], checkpoints[-1]['rst']['view']))
    rooms.escape()
    pump(.3)
    shed_ok = [shed_all()]
    nloops = len(loops_of(nw))
    checkpoint('round 1 left, shed', LOOPS, 0)
    for rnd in range(2, ROUNDS + 1):
        step('round %d' % rnd)
        for index, card in enumerate(cards):
            show(card)
            if index < LOOPS - 1:
                rooms.escape()
                pump(.4)
        checkpoint('round %d resumed' % rnd, None, LOOPS * (rnd - 1))
        rooms.escape()
        pump(.3)
        shed_ok.append(shed_all())
        checkpoint('round %d left, shed' % rnd, LOOPS * rnd, LOOPS * (rnd - 1))
    pump(1.)

    def as_expected(c):
        (shed, resumed), (want_shed, want_resumed) = c['truth'][0], c['expect']
        return (want_shed is None or shed == want_shed) and resumed == want_resumed
    check('A8 under constant inspect polling the loops still shed once per leave and resume once per show (the probe\'s '
          'own count of real sheds and resumes matches the workload)',
          all(shed_ok) and all(all_moved) and nloops == LOOPS and all(as_expected(c) for c in checkpoints) and not problems,
          (shed_ok, all_moved, nloops, [(c['label'], c['truth'][0], c['expect']) for c in checkpoints], problems[:3]))
    info('checkpoints: ' + json.dumps([(c['label'], c['truth'], c['currently'], c['st']['active'], c['st']['shed'],
                                        c['st']['resumed']) for c in checkpoints]))
    readable = all(c['st']['readable'] for c in checkpoints)
    shed_paths, resumed_paths = tracking_paths(checkpoints, 'shed'), tracking_paths(checkpoints, 'resumed')
    check('A2 the report counts the loops that have shed, and the count matches real sheds at every checkpoint over %d '
          'rounds of %d loops' % (ROUNDS, LOOPS), bool(shed_paths) and readable,
          ([(c['label'], c['shed'], c['st']['shed']) for c in checkpoints], shed_paths))
    check('A3 the report counts the loops that have resumed, and the count matches real resumes at every checkpoint '
          '(a first show is not a resume)', bool(resumed_paths) and readable,
          ([(c['label'], c['resumed'], c['st']['resumed']) for c in checkpoints], resumed_paths))
    rshed, rresumed = tracking_paths(checkpoints, 'shed', 'rst'), tracking_paths(checkpoints, 'resumed', 'rst')
    rreadable = all(c['rst']['readable'] for c in checkpoints)
    check('X2 as a separate process, the shed and resumed counts match real sheds and resumes at every checkpoint over %d '
          'rounds of %d loops' % (ROUNDS, LOOPS), bool(rshed) and bool(rresumed) and rreadable,
          ([(c['label'], c['truth'][0], c['rst']['shed'], c['rst']['resumed'], c['rst']['err']) for c in checkpoints],
           rshed, rresumed))
    # ---- quick bounces on a loop that is shed now
    step('quick bounces')
    before = truth.now()
    for _ in range(BOUNCES):
        rooms.click(cards[0])
        pump(.2)
        truth.arm()
        rooms.escape()
        pump(.2)
        inspect_quietly()
    settled = shed_all()
    pump(1.)
    after = truth.now()
    bounce = (after[0][0] - before[0][0], after[0][1] - before[0][1])
    st, rst_b = settle(env, truth)
    cp = {'label': 'after bounces', 'st': st, 'rst': rst_b, 'shed': (after[0][0], after[1][0]), 'resumed': (after[0][1], after[1][1])}
    bounce_paths = (tracking_paths(checkpoints + [cp], 'shed'), tracking_paths(checkpoints + [cp], 'resumed'))
    check('A4 after %d quick show/leave pairs the report follows the real sheds and resumes, not the clicks' % BOUNCES,
          bool(bounce_paths[0]) and bool(bounce_paths[1]) and settled and bounce[0] < BOUNCES and bounce[1] < BOUNCES,
          (bounce, st['shed'], st['resumed'], after, bounce_paths))
    # ---- inspect changes nothing
    step('inspect hammered')
    play = getattr(nw, '_PLAY', {})

    def state():
        return (loop_states(nw), truth.now(), id(play.get('shed')), id(play.get('renew')), len(nw._LOOPS), sorted(play))
    state_before = state()
    runs = [look() for _ in range(40)]
    state_mid = state()
    pump(3.)
    state_end = state()
    check('A7 inspect never changes shedding: 40 inspects with loops shed leave every loop\'s state, the shed and resume '
          'calls and the shedding hooks as they were, and nothing wakes afterwards',
          state_before == state_mid == state_end and all(r['readable'] for r in runs) and state_before[0]
          and all(s[1] for s in state_before[0]), (state_before, state_mid, state_end))
    step('window closed')
    for w in list(env.app.get_windows()):
        w.close()
    pump(1.5)
    truth_end = truth.now()
    st_end = look()
    check('N2 with the window closed, inspect is still readable (exit 0, one JSON line) and has a shedding report whose '
          'counts, if it gives them, are not above what really happened',
          st_end['readable'] and st_end['found'] and all(v <= truth_end[0][0] for v in st_end['shed'].values())
          and all(v <= truth_end[0][1] for v in st_end['resumed'].values()),
          (st_end['readable'], st_end['view'], truth_end))
    rst_end = env.remote.look()
    check('X5 as a separate process with the window closed, inspect is still readable and has a shedding report whose '
          'counts, if it gives them, are not above what really happened',
          rst_end['readable'] and rst_end['found'] and all(v <= truth_end[0][0] for v in rst_end['shed'].values())
          and all(v <= truth_end[0][1] for v in rst_end['resumed'].values()),
          (rst_end['readable'], rst_end['code'], rst_end['err'], rst_end['view'], truth_end))
    return {'checkpoints': [(c['label'], c['truth'], c['currently'], c['st']['active'], c['st']['shed'],
                             c['st']['resumed']) for c in checkpoints],
            'shed_paths': shed_paths, 'resumed_paths': resumed_paths, 'bounce': bounce,
            'view_end': st_end['view'], 'uptime': uptime()}


def fallback(env, step, check, info, mode):
    """gate: SHED_GTK set to a GTK that is not this one. gl: libgstgl cannot be opened. refs: an extra reference on the
    first loop's video sink, so the release is refused for real."""
    import ctypes
    import next_widgets as nw
    pump = env.pump
    real_cdll = ctypes.CDLL
    if mode == 'gate':
        nw.SHED_GTK = (0, 0, 0)            # before the first loop loads the GStreamer calls (as O11-MEDIA's B-checks do)
    elif mode == 'gl':
        def cdll(name=None, *a, **k):
            if name and 'libgstgl' in str(name):
                raise OSError('%s: cannot open shared object file: No such file or directory' % name)
            return real_cdll(name, *a, **k)
        ctypes.CDLL = cdll
    step('launch')
    window = env.launch(page='rooms')
    window.show_rooms()
    pump(2.)
    rooms = o10p.Rooms(env, window)
    catalogue = rooms.loop_cards()
    truth = Truth(nw)
    moved, held, st_before = [], None, None
    step('every loop once')
    try:
        for index, card in enumerate(catalogue[:LOOPS]):
            rooms.click(card)
            moved.append(rooms.moves(6.)[0])
            truth.arm()
            if index == 0:
                if mode == 'refs':
                    scene = rooms.scene_widget()
                    _, paintable = o10p.largest_paintable(scene) if scene else (None, None)
                    if isinstance(paintable, env.Gtk.MediaFile):
                        held = o11m._hold_video_sink(nw, paintable)
                st_before = look()
            rooms.escape()
            pump(.4)
        if mode == 'refs':
            wait(env, lambda: not getattr(nw, '_PLAY', {}).get('shed'), SHED_WAIT_S, poll=truth.arm)
        else:
            pump(6.)
        o11m._quiet(env)
        st1 = look()
        step('resumes')
        resumed_moves = []
        for card in (catalogue[1], catalogue[0], catalogue[2]):
            rooms.click(card)
            resumed_moves.append(rooms.moves(6.)[0])
            truth.arm()
            rooms.escape()
            pump(1.5)
        o11m._quiet(env)
        st2 = look()
        rst = env.remote.look() if mode == 'refs' else None
    finally:
        ctypes.CDLL = real_cdll
    events, distinct = truth.now()
    word = {'gate': r'gtk', 'gl': r'gstgl', 'refs': r'ref'}[mode]
    off = st1['readable'] and st1['found'] and st1['active'] is False and bool(re.search(word, st1['text']))
    digits_off = lambda text: re.sub(r'\d+', '#', text)
    if mode == 'refs':
        check('R1 refs: before the release is refused, inspect does not say shedding is off',
              st_before is not None and st_before['readable'] and st_before['found'] and st_before['active'] is not False
              and bool(held), (bool(held), st_before and st_before['view']))
        check('R2 refs: after the refused release, inspect reports shedding not active and the reason names references',
              off, (st1['readable'], st1['active'], st1['view']))
        counts = [n for n in st1['refs'] if REFS_RANGE[0] <= n <= REFS_RANGE[1]]
        check('R3 refs: the report shows the observed reference count (a whole number from %d to %d)' % REFS_RANGE,
              off and bool(counts), (st1['refs'], st1['view']))

        def agree(st):
            top = max(st['shed'].values(), default=0)
            return (bool(st['shed']) and bool(st['resumed']) and all(v <= events[0] for v in st['shed'].values())
                    and all(v <= events[1] and v <= top for v in st['resumed'].values()))
        check('R4 refs: the counts agree with what happened (at most the loop whose release was refused shed; resumes never '
              'above sheds)', events[0] <= 1 and agree(st1) and agree(st2), (events, distinct, st1['view'], st2['view']))
        check('R5 refs: the loops still play and resume, and afterwards shedding is still reported off for the same reason',
              all(moved) and all(resumed_moves) and st2['active'] is False and bool(re.search(word, st2['text']))
              and digits_off(st2['text']) == digits_off(st1['text']), (moved, resumed_moves, st1['text'], st2['text']))
        check('R6 refs: the report is plain language', plain(st1), st1['texts'])
        rcounts = [n for n in rst['refs'] if REFS_RANGE[0] <= n <= REFS_RANGE[1]]
        check('X4 refs: as a separate process (shipped roomctl.py over the real socket), inspect reports shedding not '
              'active, the reason names references and shows the observed count (a whole number from %d to %d)' % REFS_RANGE,
              rst['readable'] and rst['found'] and rst['active'] is False and bool(re.search('ref', rst['text']))
              and bool(rcounts) and plain(rst), (rst['readable'], rst['code'], rst['err'], rst['view']))
    else:
        letter, names, thing = ('G', 'with a different GTK version', 'GTK') if mode == 'gate' else (
            'L', 'with libgstgl missing', 'libgstgl')
        zero = lambda st: bool(st['shed']) and bool(st['resumed']) and not any(st['shed'].values()) and not any(
            st['resumed'].values())
        check('%s1 %s: %s, inspect reports shedding not active and the reason names %s' % (letter, mode, names, thing),
              off, (st1['readable'], st1['active'], st1['view']))
        check('%s2 %s: the counts are there and zero (no loop shed or resumed), and the loops still play' % (letter, mode),
              zero(st1) and events == (0, 0) and all(moved) and all(resumed_moves),
              (events, moved, resumed_moves, st1['view']))
        check('%s3 %s: after loops are shown and left again the report is unchanged: not active, same reason, counts zero' % (
              letter, mode), st2['active'] is False and st2['text'] == st1['text'] and zero(st2), (st1['view'], st2['view']))
        check('%s4 %s: the report is plain language' % (letter, mode), plain(st1), st1['texts'])
    info('%s: active %s, text %r, counts shed %s resumed %s, truth %s' % (
        mode, st1['active'], st1['text'][:200], st1['shed'], st1['resumed'], (events, distinct)))
    return {'mode': mode, 'reason': st1['text'], 'active': st1['active'], 'view': st1['view'],
            'truth': (events, distinct), 'uptime': uptime()}



# ---------------------------------------------------------------------------- adversarial status files (phase 2)
# Coordinator request, phase 2: the ruled interface is a per-window file $XDG_RUNTIME_DIR/listening-hub/
# scene-loops-<pid>.json read by inspect (files whose pid is dead ignored, the newest window reported). These children
# plant and kill real windows and files. The planted files carry only pid, state, shed and resumed (distinctive 7 / 5).

def status_dir():
    path = Path(os.environ['XDG_RUNTIME_DIR']) / 'listening-hub'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def plant(name, content, pid=None, newer=True):
    path = status_dir() / name
    data = content if isinstance(content, (bytes, str)) else json.dumps(content)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    if newer:
        os.utime(path, (time.time() + 5, time.time() + 5))
    return path


def record(pid, state='active', shed=7, resumed=5):
    return {'pid': pid, 'state': state, 'gtk': None, 'refs_observed': None, 'sink_finalized': None,
            'shed': shed, 'resumed': resumed}


def dead_pid():
    process = subprocess.Popen(['true'])
    process.wait()
    return process.pid


def both(env):
    """The report as the window process reads it and as a separate roomctl process reads it."""
    return {'in': look(), 'out': env.remote.look()}


def claims_active(reports):
    return any(r['active'] is True for r in reports.values())


def counts_of(reports, key):
    return {name: sorted(set(r[key].values())) for name, r in reports.items()}


def lock_holders(path):
    """(pid, close-on-exec) for every process that has `path` open, from /proc."""
    found = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            for fd in os.listdir('/proc/%s/fd' % entry):
                try:
                    if os.readlink('/proc/%s/fd/%s' % (entry, fd)) == str(path):
                        flags = int(next(l for l in open('/proc/%s/fdinfo/%s' % (entry, fd)) if l.startswith('flags:')).split()[1], 8)
                        found.append((int(entry), bool(flags & 0o2000000)))
                except (OSError, StopIteration, ValueError):
                    continue
        except OSError:
            continue
    return found


def victim(env, step):
    """A window in its own process: two loops shown and left until shed, then idle until killed."""
    import next_widgets as nw
    step('victim window')
    window = env.launch(page='rooms')
    window.show_rooms()
    env.pump(2.)
    rooms = o10p.Rooms(env, window)
    truth = Truth(nw)
    for card in rooms.loop_cards()[:2]:
        rooms.click(card)
        rooms.moves(6.)
        for _ in range(20):
            truth.arm()
            if getattr(nw, '_PLAY', {}).get('shed'):
                break
            env.pump(.25)
        rooms.escape()
        env.pump(.5)
    wait(env, lambda: sum(bool(l.shed) for l in loops_of(nw)) >= 2, SHED_WAIT_S)
    env.pump(1.)
    # This process may read its children's /proc/PID/fd (the probe's helpers run non-dumpable, which hides them otherwise).
    import ctypes
    ctypes.CDLL(None).prctl(4, 1, 0, 0, 0)
    # Children a window may spawn and that outlive it, by the routes the product uses (Python's subprocess with its default
    # close_fds, and GLib's spawn): neither may keep the window's lock alive after the window is killed.
    from gi.repository import GLib
    kid_a = subprocess.Popen(['sleep', '300'])
    kid_b = GLib.spawn_async([shutil.which('sleep'), '300'], flags=GLib.SpawnFlags.SEARCH_PATH)[0]
    holders = lock_holders(status_dir() / ('scene-loops-%d.lock' % os.getpid()))
    print('HOLDERS ' + json.dumps(holders), flush=True)
    print('READY %d %d %d %d %d' % (truth.shed, truth.renew, os.getpid(), kid_a.pid, kid_b), flush=True)
    while True:
        env.pump(.5)


class Victim:
    def __init__(self, args, output, name):
        import select
        self.select = select
        log = open(Path(output) / ('victim-%s.log' % name), 'w')
        self.process = subprocess.Popen(
            ['dbus-run-session', '--', sys.executable, str(HERE), '--child', 'victim', '--source', args.source,
             '--output', str(Path(output) / ('victim-' + name))], stdout=subprocess.PIPE, stderr=log, text=True,
            env=dict(os.environ, PYTHONFAULTHANDLER='1'), start_new_session=True, preexec_fn=no_core_dumps)
        self.pid = self.truth = None
        self.kids, self.holders = [], None
        deadline = time.monotonic() + 420
        fd, buffer = self.process.stdout.fileno(), ''
        while time.monotonic() < deadline and self.pid is None:
            ready, _, _ = select.select([fd], [], [], 1.)
            if not ready:
                continue
            chunk = os.read(fd, 65536)       # not readline(): its buffer would hold a second line select cannot see
            if not chunk:
                break
            buffer += chunk.decode(errors='replace')
            while '\n' in buffer and self.pid is None:
                line, buffer = buffer.split('\n', 1)
                if line.startswith('HOLDERS '):
                    self.holders = json.loads(line[8:])
                elif line.startswith('READY '):
                    _, shed_, renew_, pid, *kids = line.split()
                    self.truth, self.pid, self.kids = (int(shed_), int(renew_)), int(pid), [int(k) for k in kids]

    def kill(self):
        if self.pid:
            try:
                os.kill(self.pid, 9)
            except OSError:
                pass
        try:
            self.process.wait(20)
        except subprocess.TimeoutExpired:
            pass

    def cleanup(self):
        self.kill()
        for kid in self.kids:
            try:
                os.kill(kid, 9)
            except OSError:
                pass
        try:
            os.killpg(self.process.pid, 9)
        except OSError:
            pass


def stale(env, step, check, info, args):
    import next_widgets as nw
    pump = env.pump
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    directory = status_dir()
    victims = []
    try:
        step('a window in another process')
        v1 = Victim(args, output, 'one')
        victims.append(v1)
        r1 = both(env)
        file1 = directory / ('scene-loops-%s.json' % v1.pid)
        check('K1 a window running in another process: inspect (in the window process and as a separate roomctl process) '
              'reports it active with its real counts', v1.pid is not None and not any(
                  not r['readable'] or r['active'] is not True for r in r1.values())
              and all((v1.truth or (None, None))[0] in c and (v1.truth or (None, None))[1] in counts_of(r1, 'resumed')[n]
                      for n, c in counts_of(r1, 'shed').items()),
              (v1.truth, v1.pid, {n: (r['readable'], r['active'], r['view']) for n, r in r1.items()}))
        lockfile = directory / ('scene-loops-%s.lock' % v1.pid)
        holders = v1.holders        # taken by the window itself after it spawned its children (it may read their fds)
        kids_alive = lambda: [k for k in v1.kids if Path('/proc/%d' % k).exists()]
        check('K6 the window\'s lock is held by the window process alone and its descriptor is close-on-exec, while the '
              'window has spawned children (Python subprocess and GLib spawn) that stay running', len(v1.kids) == 2
              and len(kids_alive()) == 2 and holders and {h[0] for h in holders} == {v1.pid} and all(h[1] for h in holders),
              (v1.pid, v1.kids, holders))
        step('SIGKILL it')
        v1.kill()
        left = file1.exists()
        check('K0 harness precondition: a window killed with SIGKILL leaves its status file behind', left, (v1.pid, left))
        r2 = both(env)
        # [ORIGINAL, superseded by this wording: K2 "after the window was killed, ..." without the children it spawned]
        check('K2 after the window was killed while the children it spawned still run, inspect does not claim shedding is '
              'active and reports none of the dead window\'s counts', len(kids_alive()) == 2 and all(
                  r['readable'] for r in r2.values()) and not claims_active(r2)
              and all((v1.truth or (None, None))[0] not in c for c in counts_of(r2, 'shed').values() if v1.truth and v1.truth[0] not in (0,)),
              {n: (r['readable'], r['active'], r['view']) for n, r in r2.items()})
        step('a new window starts')
        window = env.launch(page='rooms')
        window.show_rooms()
        pump(2.)
        rooms = o10p.Rooms(env, window)
        truth = Truth(nw)
        cards = rooms.loop_cards()
        rooms.click(cards[0])
        rooms.moves(6.)
        for _ in range(20):
            truth.arm()
            if getattr(nw, '_PLAY', {}).get('shed'):
                break
            pump(.25)
        rooms.escape()
        wait(env, lambda: sum(bool(l.shed) for l in loops_of(nw)) >= 1, SHED_WAIT_S)
        rooms.click(cards[0])
        pump(1.5)
        mine = truth.now()[0]       # one shed, one resume
        rooms.escape()
        pump(.2)
        r3 = both(env)
        check('K3 a new window after the kill: inspect reports the new window (active, its own real counts), not the dead '
              'one\'s', mine == (1, 1) and all(r['readable'] and r['active'] is True for r in r3.values()) and all(
                  c == [mine[0]] for c in counts_of(r3, 'shed').values()) and all(
                  c == [mine[1]] for c in counts_of(r3, 'resumed').values()),
              (mine, v1.truth, {n: (r['active'], r['view']) for n, r in r3.items()}))
        step('a second window at once')
        v2 = Victim(args, output, 'two')
        victims.append(v2)
        r4 = both(env)
        check('K4 two windows at once: inspect reports one window, the newest, with its own counts (not a sum or a mix)',
              v2.pid is not None and all(r['readable'] and r['active'] is True for r in r4.values()) and all(
                  c == [(v2.truth or (None, None))[0]] for c in counts_of(r4, 'shed').values()) and all(
                  c == [(v2.truth or (None, None))[1]] for c in counts_of(r4, 'resumed').values()),
              (v2.truth, mine, {n: (r['active'], r['view']) for n, r in r4.items()}))
        step('stale and garbage files beside live windows')
        gone = dead_pid()
        plant('scene-loops-%d.json' % gone, record(gone, shed=9, resumed=9))
        plant('scene-loops-garbage.json', '{not json')
        plant('scene-loops-1.json', b'\xff\xfe\x00binary')
        r5 = both(env)
        check('K5 files from a dead pid (newer than the live windows\') and corrupt files beside live windows do not hide or '
              'replace the newest live window\'s report', all(r['readable'] and r['active'] is True for r in r5.values())
              and all(c == [(v2.truth or (None, None))[0]] for c in counts_of(r5, 'shed').values()) and all(
                  c == [(v2.truth or (None, None))[1]] for c in counts_of(r5, 'resumed').values()),
              {n: (r['active'], r['view']) for n, r in r5.items()})
        return {'v1': v1.truth, 'mine': mine, 'v2': v2.truth, 'uptime': uptime()}
    finally:
        for v in victims:
            v.cleanup()


def junk(env, step, check, info):
    """No window: planted files only. Nothing may read as active, and inspect must stay readable."""
    directory = status_dir()
    for old in directory.glob('scene-loops-*'):
        old.unlink()
    live = subprocess.Popen(['sleep', '300'])
    try:
        gone = dead_pid()
        plant('scene-loops-%d.json' % gone, record(gone))
        r = both(env)
        check('J1 a status file whose pid is dead, alone: inspect is readable, does not claim shedding is active and reports '
              'none of its counts', all(x['readable'] for x in r.values()) and not claims_active(r) and all(
                  not ({7, 5} & set(c)) for c in counts_of(r, 'shed').values()) and all(
                  not ({7, 5} & set(c)) for c in counts_of(r, 'resumed').values()),
              {n: (x['active'], x['view']) for n, x in r.items()})
        for old in directory.glob('scene-loops-*'):
            old.unlink()
        plant('scene-loops-%d.json' % live.pid, record(live.pid))
        r = both(env)
        check('J2 a stale file whose pid was reused by an unrelated live process (not a Listening Room window): inspect is '
              'readable and makes no false active claim and reports none of its counts',
              all(x['readable'] for x in r.values()) and not claims_active(r) and all(
                  not ({7, 5} & set(c)) for c in counts_of(r, 'shed').values()) and all(
                  not ({7, 5} & set(c)) for c in counts_of(r, 'resumed').values()),
              {n: (x['active'], x['view']) for n, x in r.items()})
        for old in directory.glob('scene-loops-*'):
            old.unlink()
        own = os.getpid()
        junk_files = {
            'scene-loops-%d.json' % own: '{"pid": %d, "state": "active", "shed": ' % own,         # truncated JSON
            'scene-loops-empty.json': '',
            'scene-loops-binary.json': b'\x00\xff\xfe garbage \x80',
            'scene-loops-list.json': '[1, 2, 3]',
            'scene-loops-null.json': 'null',
            'scene-loops-string-pid.json': json.dumps({'pid': 'abc', 'state': 'active', 'shed': 7, 'resumed': 5}),
            'scene-loops-negative-pid.json': json.dumps(record(-5)),
            'scene-loops-huge-pid.json': json.dumps(record(10 ** 30)),
            'scene-loops-zero-pid.json': json.dumps(record(0)),
            'scene-loops-bool-pid.json': json.dumps(record(True)),
            'scene-loops-state-unknown.json': json.dumps(record(live.pid, state='bogus')),
            'scene-loops-state-number.json': json.dumps(record(live.pid, state=3)),
            'scene-loops-counts-wrong.json': json.dumps(dict(record(gone, state='unreported'), shed='many', resumed=[])),
            'scene-loops-big.json': ('{"pid": %d, "pad": "%s"}' % (gone, 'x' * 3_000_000)),
        }
        failed = {}
        for name, content in junk_files.items():          # one at a time, so a crash on one cannot hide the others
            for old in directory.glob('scene-loops-*'):
                if old.is_dir():
                    old.rmdir()
                else:
                    old.unlink()
            plant(name, content)
            r = both(env)
            if not all(x['readable'] for x in r.values()) or claims_active(r):
                failed[name] = {n: (x['readable'], x['code'], x['err'][-120:], x['active']) for n, x in r.items()}
        for old in directory.glob('scene-loops-*'):
            old.unlink()
        (directory / 'scene-loops-dir.json').mkdir(exist_ok=True)
        r = both(env)
        if not all(x['readable'] for x in r.values()) or claims_active(r):
            failed['scene-loops-dir.json (a directory)'] = {n: (x['readable'], x['code'], x['err'][-120:], x['active'])
                                                            for n, x in r.items()}
        (directory / 'scene-loops-dir.json').rmdir()
        check('J3 corrupt, truncated, empty, binary, wrong-typed, unknown-state, oversized, impossible-pid and odd-named '
              'status files (and a directory named like one), each alone: inspect stays readable (exit 0, one JSON line) '
              'and makes no active claim', not failed, failed)
        return {'uptime': uptime()}
    finally:
        live.kill()
        live.wait()



def start(env, step, check, info, mode):
    """A window starting in this process. reuse: leftovers of a SIGKILLed window with this very pid (a stale status file and
    an unheld lock file, the pid number reused by the new window). racy: a status reader holds the window's lock file at the
    moment the window first reports, and lets go a moment later."""
    import fcntl
    import next_widgets as nw
    pump = env.pump
    directory = status_dir()
    own = os.getpid()
    lockpath = directory / ('scene-loops-%d.lock' % own)
    held = None
    if mode == 'reuse':
        lockpath.write_text('')
        plant('scene-loops-%d.json' % own, record(own))
        r0 = both(env)
        check('K9 leftovers of a killed window whose pid number a new process now has (stale status file, unheld lock): '
              'inspect before the new window reports makes no active claim and reports none of the stale counts',
              all(r['readable'] for r in r0.values()) and not claims_active(r0) and all(
                  not ({7, 5} & set(c)) for k in ('shed', 'resumed') for c in counts_of(r0, k).values()),
              {n: (r['active'], r['view']) for n, r in r0.items()})
    else:
        held = os.open(lockpath, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_SH)         # a reader probing the lock at this very moment
    step('window')
    window = env.launch(page='rooms')
    window.show_rooms()
    pump(2.)
    rooms = o10p.Rooms(env, window)
    truth = Truth(nw)
    rooms.click(rooms.loop_cards()[0])
    moved = rooms.moves(6.)[0]
    for _ in range(20):
        truth.arm()
        if getattr(nw, '_PLAY', {}).get('shed'):
            break
        pump(.25)
    pump(1.5)
    if held is not None:
        os.close(held)                            # the reader is done; the window has had no other change to report
        pump(SETTLE_S + 2.)
    else:
        pump(2.)
    r1 = both(env)
    if mode == 'reuse':
        check('K9b ... and once the new window has reported, inspect shows the new window (active, no stale counts)',
              moved and all(r['readable'] and r['active'] is True for r in r1.values()) and all(
                  not ({7, 5} & set(c)) for k in ('shed', 'resumed') for c in counts_of(r1, k).values()),
              {n: (r['active'], r['view']) for n, r in r1.items()})
    else:
        check('K8 a status reader holding the window\'s lock file when the window first reports does not leave the window '
              'unreported: a moment after it lets go, with the loop on screen, inspect reports shedding active',
              moved and all(r['readable'] and r['active'] is True for r in r1.values()),
              {n: (r['active'], r['view']) for n, r in r1.items()})
    return {'uptime': uptime()}


# ---------------------------------------------------------------------------- parent

TREE_SCRIPT = r'''
import json, os, sys
explicit = os.environ.get('O11FO_EXPLICIT')
if explicit:
    sys.path.insert(0, explicit)       # a probe pointed at another tree (--source X) does this before importing helpers
import source_tree
source = source_tree.use_source(os.environ['O11FO_DEFAULT'])
print(json.dumps({'source': str(source), 'env': os.environ.get('LISTENING_HUB_SOURCE'), 'first': sys.path[0]}))
'''


def tree_checks(check, repo):
    """tests/source_tree.py in a subprocess per scenario, with scratch trees (each holds the marker room_session.py)."""
    tests = Path(repo) / 'tests'
    base = Path(tempfile.mkdtemp(prefix='o11fo-trees-')).resolve()
    try:
        trees = {}
        for name in ('default', 'chosen', 'other'):
            tree = base / name
            tree.mkdir()
            (tree / 'room_session.py').write_text('')
            (tree / 'listening_hub.py').write_text('')
            trees[name] = tree
        script = base / 'script' / 'run.py'
        script.parent.mkdir()
        script.write_text(TREE_SCRIPT)

        def run(pythonpath, explicit=None, hub=None):
            env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'LISTENING_HUB_SOURCE', 'O11FO_EXPLICIT')}
            env.update(PYTHONPATH=os.pathsep.join(pythonpath), O11FO_DEFAULT=str(trees['default'] / 'listening_hub.py'),
                       PYTHONDONTWRITEBYTECODE='1')
            if explicit:
                env['O11FO_EXPLICIT'] = explicit
            if hub:
                env['LISTENING_HUB_SOURCE'] = str(hub / 'listening_hub.py')
            try:
                done = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=30,
                                      cwd=str(base / 'script'))
                return json.loads(done.stdout.strip().splitlines()[-1]), done.stderr[-300:]
            except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
                return {'error': repr(error)}, ''

        d, c, o, t = trees['default'], trees['chosen'], trees['other'], str(tests)

        def wins(result, tree):
            return result.get('source') == str(tree) and result.get('first') == str(tree)

        r1 = run([str(c), t], explicit=str(c), hub=o)
        check('T1 source_tree: the --source tree is also on PYTHONPATH and LISTENING_HUB_SOURCE names another tree: the '
              '--source tree wins (and LISTENING_HUB_SOURCE follows it)',
              wins(r1[0], c) and r1[0].get('env') == str(c / 'listening_hub.py'), r1)
        r2 = run([str(c), t], explicit=str(c))
        check('T2 source_tree: the --source tree is also on PYTHONPATH and LISTENING_HUB_SOURCE is unset: the --source tree '
              'is used, not the default tree', wins(r2[0], c), r2)
        r3 = run([str(c), t], explicit=str(c) + os.sep, hub=o)
        check('T3 source_tree: the same when --source is spelled differently from the PYTHONPATH entry (trailing slash)',
              wins(r3[0], c), r3)
        r4 = run([str(d), str(c), t], explicit=str(c), hub=o)
        check('T4 source_tree: the same when PYTHONPATH lists the --source tree after the default tree',
              wins(r4[0], c), r4)
        r5 = run([str(d), t], hub=o)
        check('T5 source_tree: the dev loop\'s PYTHONPATH export alone is not a choice: LISTENING_HUB_SOURCE names another '
              'tree and that tree wins', r5[0].get('source') == str(o), r5)
        r6 = run([str(d), t])
        check('T6 source_tree: the PYTHONPATH export alone, no LISTENING_HUB_SOURCE: the default tree',
              r6[0].get('source') == str(d), r6)
        r7 = run([str(c), t], hub=o)
        check('T7 source_tree: another tree only exported on PYTHONPATH (never put first on sys.path by the caller) is not a '
              'choice either: LISTENING_HUB_SOURCE wins', r7[0].get('source') == str(o), r7)
        r8 = run([str(d), t], explicit=str(c), hub=o)
        check('T8 source_tree: control, a --source tree that is not on PYTHONPATH still wins over LISTENING_HUB_SOURCE',
              wins(r8[0], c), r8)
        return {'T1': r1, 'T2': r2, 'T3': r3, 'T4': r4, 'T5': r5, 'T6': r6, 'T7': r7, 'T8': r8}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_parent(args):
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit('terminated'))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    repo = args.repo or str(REPO)
    checks, summary = [], {'uptime_start': uptime(), 'source': str(Path(args.source).resolve()), 'children': {}}

    def check(name, ok, detail=''):
        checks.append({'criterion': name, 'pass': bool(ok), 'detail': '' if ok else str(detail)[:1500]})
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:700]), flush=True)

    def child(mode, timeout=600):
        log = output / ('%s.log' % mode)
        runtime = tempfile.mkdtemp(prefix='o11fo-child-', dir=os.environ.get('XDG_RUNTIME_DIR'))
        environment = dict(os.environ, PYTHONFAULTHANDLER='1', XDG_RUNTIME_DIR=runtime)
        command = ['dbus-run-session', '--', sys.executable, str(HERE), '--child', mode, '--source', args.source,
                   '--output', str(output / mode)]
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
        text = log.read_text(errors='replace')
        result = {}
        for line in text.splitlines():
            if line.startswith('RESULT '):
                result = json.loads(line[7:])
            elif line.startswith('INFO '):
                print(line, flush=True)
            elif line.startswith(('PASS ', 'FAIL ')):
                name, _, detail = line[5:].partition('  ')
                check(name, line.startswith('PASS '), detail)
        print('INFO %s child: exit %s in %.0f s; %s' % (mode, code, time.monotonic() - started, uptime()), flush=True)
        crashed = not isinstance(code, int) or code < 0
        check('%s: the child finished without a crash or hang' % mode, not crashed, 'exit %s\n%s' % (code, text[-1200:]))
        check('%s: the child reported all its oracles and exited 0' % mode, code == 0 and 'CHECKS-DONE' in text,
              'exit %s\n%s' % (code, text[-1200:]))
        summary['children'][mode] = result
        return result

    print('source: %s  renderer: %s' % (summary['source'], os.environ.get('GSK_RENDERER')), flush=True)
    print('uptime at start: %s' % summary['uptime_start'], flush=True)
    parts = set(filter(None, args.only.split(','))) or {'active', 'gate', 'gl', 'refs', 'junk', 'stale', 'reuse', 'racy', 'tree', 'notes'}
    results = {mode: child(mode) for mode in MODES if mode in parts}
    if {'gate', 'gl', 'refs'} <= set(results):
        reasons = [str(results[m].get('reason', '')).strip() for m in ('gate', 'gl', 'refs')]
        check('P1 the three "not active" reasons are all given and differ from each other (another GTK version, no libgstgl, '
              'refused release)', all(reasons) and len({re.sub(r'\d+', '#', r) for r in reasons}) == 3, reasons)
    if 'tree' in parts:
        summary['tree'] = tree_checks(check, repo)
    if 'notes' in parts:
        # A source-text check, on purpose: the CHANGELOG is documentation, it has no behaviour to drive.
        text = (Path(repo) / 'CHANGELOG.md').read_text()
        match = re.search(r'## \[Unreleased\](.*?)(?=\n## \[|\Z)', text, re.S)
        section = (match.group(1) if match else '').lower()
        check('C1 the CHANGELOG [Unreleased] section records the inspect addition: scene-loop shedding status',
              'inspect' in section and 'shed' in section, section[:300])
    summary['uptime_end'] = uptime()
    (output / 'o11fo-acceptance.json').write_text(json.dumps(checks, indent=1))
    (output / 'o11fo-summary.json').write_text(json.dumps(summary, indent=1, default=str))
    failed = sum(1 for c in checks if not c['pass'])
    print('%d passed, %d failed' % (len(checks) - failed, failed), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    arguments = parse()
    if arguments.child:
        run_child(arguments.child, arguments)
    else:
        if 'DISPLAY' not in os.environ or os.environ.get('LISTENING_PROBE_ISOLATED') != '1':
            reexec_isolated()
        sys.exit(run_parent(arguments))
