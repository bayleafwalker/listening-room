#!/usr/bin/env python3
"""Independent acceptance oracles for WP4-HYGIENE (suite list, probe counts, packaging, docs, classic probes).

Owned by the acceptance author, not the implementer. Host-side: needs nix and runs builds, the dev loop and (for
`judge record`) the full GUI acceptance run, always on a throwaway copy of the repository, never the live session.
Every check prints PASS/FAIL; the exit status is non-zero when any check fails.

    wp4-oracles.py entrypoints snapshot --package STORE_PATH --out FILE   record a build's entry points and runtime env
    wp4-oracles.py entrypoints compare --repo DIR --baseline FILE         build DIR and compare with the baseline
    wp4-oracles.py suites --repo DIR [--baseline tests/fixtures/wp4-main-suites.json]
                                          the dev loop and the Nix checkPhase run the same suites (traced, not parsed),
                                          and none that main ran has disappeared
    wp4-oracles.py canary --repo DIR      an unregistered test file is caught (a run fails naming it) or runs in both
    wp4-oracles.py judge record --repo DIR --store DIR --out DIR    one real acceptance run, recorded per probe
    wp4-oracles.py judge controls --repo DIR --store DIR            replay it: clean passes; regressed, vanished,
                                                                    crashed or extra-failing checks fail the run
    wp4-oracles.py docs --repo DIR        CONTRIBUTING and SECURITY.md content, and the facts they state
    wp4-oracles.py classic --repo DIR [--base REV]   the classic probes are kept or archived, never deleted
    wp4-oracles.py structure --repo DIR   the Nix and script shape the brief names (one wrap list, no counts)

How the dev loop and checkPhase are observed: a copy of the repository gets a tests/sitecustomize.py (tests/ is on
PYTHONPATH in both runners). Every Python process then reports the script it runs (and test_listening_* modules a
non-suite process imports) on stderr as "WP4TRACE ..." lines, which reach the dev loop's output and the Nix build log.
The same hook records or replays GUI probe processes for `judge`: stdout, stderr, exit status and the files a probe
writes to its --output directory, so judging can be exercised in seconds against real results.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

FAILED = []

HOOK = r'''
# Written by tests/probes/wp4-oracles.py into a throwaway copy only. Never commit.
import os, sys
_argv0 = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ''
# A runner that starts a suite as `python -c "...runpy.run_path(argv[0])..." FILE` (run_suites.py's
# network guard) runs FILE as __main__: attribute the run to FILE only when the -c code runs a path.
_orig = getattr(sys, 'orig_argv', [])
if _argv0 == '-c' and len(sys.argv) > 1 and '-c' in _orig and 'run_path' in _orig[_orig.index('-c') + 1]:
    _argv0 = os.path.basename(sys.argv[1])


def _chain():
    here = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        candidate = os.path.join(entry or '.', 'sitecustomize.py')
        if os.path.abspath(entry or '.') != here and os.path.isfile(candidate):
            with open(candidate) as handle:
                exec(compile(handle.read(), candidate, 'exec'), {'__name__': 'sitecustomize', '__file__': candidate})
            return


try:
    _chain()
except Exception as error:   # the interpreter's own hook must not be lost silently
    sys.stderr.write('WP4TRACE CHAIN-ERROR %r\n' % (error,))

if _argv0.startswith('test_listening_') and _argv0.endswith('.py'):
    sys.stderr.write('WP4TRACE RUN %s\n' % _argv0)
    sys.stderr.flush()
elif _argv0 and os.environ.get('WP4_MODE') not in ('record', 'replay'):
    class _Finder:
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name.startswith('test_listening_'):
                sys.stderr.write('WP4TRACE IMPORT %s by %s\n' % (name, _argv0))
                sys.stderr.flush()
            return None
    sys.meta_path.insert(0, _Finder)

_mode, _store = os.environ.get('WP4_MODE'), os.environ.get('WP4_STORE')
_probe = (os.sep + 'tests' + os.sep + 'probes' + os.sep) in os.path.abspath(sys.argv[0] or '') and _argv0.endswith('.py')
if _mode in ('record', 'replay') and _store and _probe:
    import json, shutil, subprocess, atexit

    def _output_dir():
        args = sys.argv[1:]
        for index, value in enumerate(args):
            if value == '--output' and index + 1 < len(args):
                return args[index + 1]
            if value.startswith('--output='):
                return value.split('=', 1)[1]
        return None

    def _slot():
        counter = os.path.join(_store, '.count-' + _argv0)
        number = int(open(counter).read()) if os.path.exists(counter) else 0
        with open(counter, 'w') as handle:
            handle.write(str(number + 1))
        return os.path.join(_store, '%s.%d' % (_argv0, number))

    def _files(root):
        found = {}
        if root and os.path.isdir(root):
            for base, _dirs, names in os.walk(root):
                for name in names:
                    path = os.path.join(base, name)
                    try:
                        stat = os.stat(path)
                        found[os.path.relpath(path, root)] = (stat.st_mtime_ns, stat.st_size)
                    except OSError:
                        pass
        return found

    if _mode == 'replay':
        slot = _slot()
        with open(os.path.join(slot, 'meta.json')) as handle:
            meta = json.load(handle)
        out = _output_dir()
        # The runner's own log may sit in --output (it was recorded as a changed file); never overwrite the file
        # this process's stdout or stderr already writes to.
        streams = set()
        for fd in (1, 2):
            try:
                st = os.fstat(fd)
                streams.add((st.st_dev, st.st_ino))
            except OSError:
                pass
        if out:
            os.makedirs(out, exist_ok=True)
            files = os.path.join(slot, 'files')
            for base, _dirs, names in os.walk(files):
                for name in names:
                    source = os.path.join(base, name)
                    target = os.path.join(out, os.path.relpath(source, files))
                    try:
                        st = os.stat(target)
                        if (st.st_dev, st.st_ino) in streams:
                            continue
                    except OSError:
                        pass
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copyfile(source, target)
        for name, fd in (('stdout', 1), ('stderr', 2)):
            with open(os.path.join(slot, name), 'rb') as handle:
                os.write(fd, handle.read())
        os._exit(int(meta['rc']))

    slot = _slot()
    os.makedirs(os.path.join(slot, 'files'), exist_ok=True)
    out, before = _output_dir(), None
    before = _files(out)
    tees = []
    for name, fd in (('stdout', 1), ('stderr', 2)):
        read, write = os.pipe()
        tee = subprocess.Popen(['tee', os.path.join(slot, name)], stdin=read, stdout=os.dup(fd), close_fds=True)
        os.close(read)
        os.dup2(write, fd)
        os.close(write)
        tees.append(tee)
    state = {'rc': 0, 'done': False}

    def _finish(rc):
        if state['done']:
            return
        state['done'] = True
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        after = _files(out)
        for rel, mark in after.items():
            if before.get(rel) != mark:
                target = os.path.join(slot, 'files', rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    shutil.copyfile(os.path.join(out, rel), target)
                except OSError:
                    pass
        with open(os.path.join(slot, 'meta.json'), 'w') as handle:
            json.dump({'rc': rc, 'argv': sys.argv}, handle)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        for tee in tees:
            try:
                tee.wait(timeout=20)   # a child that kept the pipe open must not hang the run
            except Exception:
                tee.kill()

    _real_exit, _real_sys_exit, _real_hook = os._exit, sys.exit, sys.excepthook

    def _exit(code=0):
        _finish(code)
        _real_exit(code)

    def _sys_exit(code=0):
        state['rc'] = code if isinstance(code, int) else (0 if code is None else 1)
        _real_sys_exit(code)

    def _hook(*info):
        state['rc'] = 1
        _real_hook(*info)
    os._exit, sys.exit, sys.excepthook = _exit, _sys_exit, _hook
    atexit.register(lambda: _finish(state['rc']))
'''

CANARY = '''"""WP4 acceptance canary: a test file nobody registered. Written into a throwaway copy only."""
import sys
import unittest


class Canary(unittest.TestCase):
    def test_canary(self):
        sys.stderr.write('WP4-CANARY-RAN\\n')


if __name__ == '__main__':
    unittest.main()
'''


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (': ' + str(detail)[:1500] if detail and not ok else ''), flush=True)
    if not ok:
        FAILED.append(name)


def note(text):
    print('NOTE ' + text, flush=True)


def run(argv, cwd=None, env=None, timeout=3600):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def copy_repo(repo, extra=()):
    """A throwaway copy of the repository's files (tracked and untracked, ignored and agent audit files excluded)."""
    repo = Path(repo).resolve()
    target = Path(tempfile.mkdtemp(prefix='wp4-copy-')) / 'repo'
    listed = run(['git', '-C', str(repo), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
    if listed.returncode == 0:
        for rel in filter(None, listed.stdout.split('\0')):
            if rel.split('/')[0] in ('.auditctl', '_artifacts') or not (repo / rel).is_file():
                continue
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / rel, target / rel)
    else:
        shutil.copytree(repo, target, ignore=shutil.ignore_patterns('.git', '.auditctl', '_artifacts'))
    (target / 'tests' / 'sitecustomize.py').write_text(HOOK)
    for rel, text in extra:
        (target / rel).write_text(text)
    # Its own git repository: the dev script's `nix build .#...` and `nix develop "$repo"` must resolve to this copy,
    # never to a repository above the temporary directory, and the hook and canary must be part of the flake source.
    for argv in (['git', 'init', '-q'], ['git', 'add', '-A'],
                 ['git', '-c', 'user.name=wp4-oracles', '-c', 'user.email=wp4@example.invalid', 'commit', '-qm', 'throwaway copy']):
        run(argv, cwd=target)
    return target


def isolated_env(**extra):
    home = tempfile.mkdtemp(prefix='wp4-home-')
    env = {k: v for k, v in os.environ.items() if k not in ('WAYLAND_DISPLAY', 'DISPLAY', 'DBUS_SESSION_BUS_ADDRESS',
                                                             'HYPRLAND_INSTANCE_SIGNATURE', 'XDG_RUNTIME_DIR')}
    env.update(HOME=home, XDG_STATE_HOME=home + '/state', XDG_CONFIG_HOME=home + '/config', XDG_CACHE_HOME=home + '/cache')
    env.update(extra)
    return env


def traced(text):
    ran = set(re.findall(r'WP4TRACE RUN (test_listening_[A-Za-z0-9_]+\.py)', text))
    imported = {m + '.py' for m in re.findall(r'WP4TRACE IMPORT (test_listening_[A-Za-z0-9_]+) by (?!test_listening_)', text)}
    return ran | imported


def dev_tests(copy):
    result = run(['bash', 'scripts/listening-dev.sh', 'test'], cwd=copy, env=isolated_env(), timeout=5400)
    return result.returncode, result.stdout + result.stderr


def nix_build(copy):
    result = run(['nix', 'build', '-L', '--no-link', '--print-out-paths', 'path:%s#default' % copy], timeout=7200)
    return result.returncode, result.stdout + result.stderr


# ------------------------------------------------------------------ suites / canary
def cmd_suites(a):
    copy = copy_repo(a.repo)
    files = sorted(p.name for p in (copy / 'tests').glob('test_listening_*.py'))
    dev_rc, dev_log = dev_tests(copy)
    nix_rc, nix_log = nix_build(copy)
    Path(a.logs).mkdir(parents=True, exist_ok=True)
    (Path(a.logs) / 'suites-dev.log').write_text(dev_log)
    (Path(a.logs) / 'suites-nix.log').write_text(nix_log)
    dev, nix = traced(dev_log), traced(nix_log)
    check('S1 the dev loop (scripts/listening-dev.sh test) succeeds on the tree', dev_rc == 0, dev_log[-1500:])
    check('S2 the package build with its checkPhase succeeds on the tree', nix_rc == 0, nix_log[-1500:])
    check('S3 the trace saw suites in both runners (the observation itself works)', dev and nix, (sorted(dev), sorted(nix)))
    check('S4 the dev loop and the Nix checkPhase run the same suites', dev == nix,
          'dev only %r; nix only %r' % (sorted(dev - nix), sorted(nix - dev)))
    both = dev & nix
    check('S5 the O2 and O3 suites (superseded o3 "integration: the hub test list registers ...") run in both runners',
          {'test_listening_rooms_oracles.py', 'test_listening_listen.py'} <= both, sorted(both))
    if a.baseline:
        baseline = set(json.loads(Path(a.baseline).read_text()))
        check('S6 no suite the baseline ran has silently disappeared from either runner', baseline <= both, sorted(baseline - both))
    if a.write_baseline:
        Path(a.write_baseline).write_text(json.dumps(sorted(both), indent=1) + '\n')
        note('wrote the traced suite set to %s' % a.write_baseline)
    neither = sorted(set(files) - dev - nix)
    note('suites run by both: %d of %d test_listening_*.py files' % (len(dev & nix), len(files)))
    note('test files run by neither runner (each needs a stated, registered reason): %r' % neither)
    shutil.rmtree(copy.parent, ignore_errors=True)


def cmd_canary(a):
    name = 'test_listening_zz_wp4_canary.py'
    copy = copy_repo(a.repo, extra=[('tests/' + name, CANARY)])
    dev_rc, dev_log = dev_tests(copy)
    nix_rc, nix_log = nix_build(copy)
    Path(a.logs).mkdir(parents=True, exist_ok=True)
    (Path(a.logs) / 'canary-dev.log').write_text(dev_log)
    (Path(a.logs) / 'canary-nix.log').write_text(nix_log)
    ran = {'dev': name in traced(dev_log) and 'WP4-CANARY-RAN' in dev_log, 'nix': name in traced(nix_log) and 'WP4-CANARY-RAN' in nix_log}
    caught = {'dev': dev_rc != 0 and 'zz_wp4_canary' in dev_log, 'nix': nix_rc != 0 and 'zz_wp4_canary' in nix_log}
    auto = ran['dev'] and ran['nix'] and dev_rc == 0 and nix_rc == 0
    note('canary: ran=%r caught=%r dev_rc=%d nix_rc=%d' % (ran, caught, dev_rc, nix_rc))
    check('C1 an unregistered test file is picked up by both runners, or a run fails naming it',
          auto or caught['dev'] or caught['nix'], 'ran=%r caught=%r rc=(%d, %d)' % (ran, caught, dev_rc, nix_rc))
    check('C2 no runner silently ignores it while succeeding', not ((dev_rc == 0 and not ran['dev']) and (nix_rc == 0 and not ran['nix'])),
          'ran=%r rc=(%d, %d)' % (ran, dev_rc, nix_rc))
    if caught['dev'] != caught['nix'] and not auto:
        note('only the %s run catches the canary' % ('dev' if caught['dev'] else 'nix'))
    shutil.rmtree(copy.parent, ignore_errors=True)


# ------------------------------------------------------------------ judge
def cmd_judge(a):
    store = Path(a.store).resolve()
    if a.action == 'record':
        if store.exists():
            sys.exit('store exists: ' + str(store))
        store.mkdir(parents=True)
        copy = copy_repo(a.repo)
        out = Path(a.out).resolve()
        result = run(['bash', 'scripts/listening-dev.sh', 'acceptance', str(out)], cwd=copy,
                     env=isolated_env(WP4_MODE='record', WP4_STORE=str(store)), timeout=7200)
        (store / 'runner.stdout').write_text(result.stdout)
        (store / 'runner.stderr').write_text(result.stderr)
        (store / 'runner.rc').write_text(str(result.returncode))
        for counter in store.glob('.count-*'):
            counter.unlink()
        print(result.stdout)
        check('J0 the recorded acceptance run passes its own judging', result.returncode == 0, result.stdout[-2000:])
        shutil.rmtree(copy.parent, ignore_errors=True)
        return
    slots = sorted(p for p in store.iterdir() if p.is_dir())
    acceptance = [s for s in slots if 'acceptance' in s.name and (s / 'meta.json').exists()]
    check('J0 the store holds recorded acceptance probes', bool(acceptance), [s.name for s in slots])
    copy = copy_repo(a.repo)

    def replay(label, mutate=None):
        work = Path(tempfile.mkdtemp(prefix='wp4-replay-'))
        shutil.copytree(store, work / 'store', ignore=shutil.ignore_patterns('.count-*'))
        detail = mutate(work / 'store') if mutate else ''
        out = work / 'out'
        result = run(['bash', 'scripts/listening-dev.sh', 'acceptance', str(out)], cwd=copy,
                     env=isolated_env(WP4_MODE='replay', WP4_STORE=str(work / 'store')), timeout=3600)
        text = result.stdout + result.stderr
        logs = '\n'.join(p.read_text(errors='ignore') for p in out.rglob('*') if p.is_file() and p.suffix in ('.log', '.txt', '.json'))
        note('%s: runner exit %d; %s; last lines: %s' % (label, result.returncode, detail, ' | '.join(result.stdout.strip().splitlines()[-8:])))
        shutil.rmtree(work, ignore_errors=True)
        return result.returncode, text, logs

    def first_pass(store_dir, prefer):
        for slot in sorted(store_dir.iterdir()):
            if slot.is_dir() and prefer in slot.name and (slot / 'stdout').exists():
                lines = (slot / 'stdout').read_text(errors='ignore').splitlines()
                criteria = []
                for path in (slot / 'files').rglob('*.json'):
                    try:
                        data = json.loads(path.read_text())
                    except ValueError:
                        continue
                    if isinstance(data, list):
                        criteria += [r['criterion'] for r in data if isinstance(r, dict) and r.get('pass') and 'criterion' in r]
                for name in criteria:   # the full name as the probe recorded it (names may contain ': ')
                    if any(line == 'PASS ' + name or line.startswith('PASS ' + name + ': ') for line in lines):
                        return slot, name
                for line in lines:
                    if line.startswith('PASS '):
                        return slot, line[5:].split(': ', 1)[0]
        raise RuntimeError('no PASS line in a %s probe' % prefer)

    def edit_results(slot, name, flip):
        """Flip a check to failing (flip=True) or remove it (flip=False) everywhere the probe reported it."""
        stdout = slot / 'stdout'
        lines, kept, done = stdout.read_text(errors='ignore').splitlines(keepends=True), [], False
        for line in lines:
            body = line[5:].rstrip('\n')
            if not done and line.startswith('PASS ') and (body == name or body.startswith(name + ': ')):
                done = True
                if flip:
                    kept.append('FAIL ' + line[5:])
                continue
            match = re.match(r'^(\d+) passed, (\d+) failed', line)
            if match and done:
                passed, failed = int(match.group(1)) - 1, int(match.group(2)) + (1 if flip else 0)
                line = '%d passed, %d failed%s' % (passed, failed, line[match.end():])
            kept.append(line)
        stdout.write_text(''.join(kept))
        for path in (slot / 'files').rglob('*.json'):
            try:
                data = json.loads(path.read_text())
            except ValueError:
                continue

            def walk(value):
                if isinstance(value, list):
                    out = []
                    for item in value:
                        if isinstance(item, dict) and name in (item.get('criterion'), item.get('name')):
                            if flip:
                                out.append(dict(item, **{'pass': False}))
                            continue
                        if isinstance(item, list) and len(item) == 2 and item[0] == name and isinstance(item[1], bool):
                            if flip:
                                out.append([name, False])
                            continue
                        out.append(walk(item))
                    return out
                if isinstance(value, dict):
                    return {k: walk(v) for k, v in value.items()}
                return value
            path.write_text(json.dumps(walk(data), indent=1))
        meta = json.loads((slot / 'meta.json').read_text())
        if flip:
            meta['rc'] = 1
        (slot / 'meta.json').write_text(json.dumps(meta))
        return 'check %r in %s %s' % (name, slot.name, 'flipped to FAIL' if flip else 'removed')

    rc, text, logs = replay('clean replay')
    check('J1 a clean replay of the recorded run passes judging', rc == 0, text[-1500:])
    beside = [p for folder in ('tests', 'scripts') for p in (copy / folder).rglob('*') if p.is_file()
              and p.name not in ('listening-o4-acceptance.py', 'wp4-oracles.py', 'sitecustomize.py') and p.suffix not in ('.png', '.jpg')]
    in_data = [str(p.relative_to(copy)) for p in beside if re.search(r'5i\b.*ORIGINAL|ORIGINAL.*\b5i\b', p.read_text(errors='ignore'))]
    check('J2 the superseded o4 "5i ORIGINAL" is visible as an expected failure (named in the run output or in expected results beside the probes)',
          rc == 0 and ('5i' in text or bool(in_data)), 'run mentions 5i: %s; expected-results files naming it: %r' % ('5i' in text, in_data))
    for label, prefer, flip in (('J3 a regressed check (one PASS turned FAIL) fails judging', 'o6-acceptance', True),
                                ('J4 a silently vanished check (one PASS line and result removed, probe still exits 0) fails judging', 'o6-acceptance', False),
                                ('J5 a regressed check in another probe fails judging', 'next-acceptance', True),
                                ('J6 a new failure beside the expected o4 5i failure fails judging', 'o4-acceptance', True)):
        rc, text, _ = replay(label, lambda s, prefer=prefer, flip=flip: edit_results(*first_pass(s, prefer), flip))
        check(label, rc != 0, text[-800:])

    def crash(store_dir):
        slot = next(s for s in sorted(store_dir.iterdir()) if s.is_dir() and 'o5-acceptance' in s.name)
        (slot / 'stdout').write_text('Traceback (most recent call last):\nRuntimeError: replayed crash\n')
        shutil.rmtree(slot / 'files')
        (slot / 'files').mkdir()
        (slot / 'meta.json').write_text(json.dumps({'rc': 1}))
        return 'o5 acceptance replaced by a crash with no results'
    rc, text, _ = replay('J7', crash)
    check('J7 a probe that crashes before reporting fails judging', rc != 0, text[-800:])

    def rename(store_dir, prefer, old, new):
        """Rewrite one check name everywhere a recorded probe reported it (stdout and JSON results)."""
        slot = next(s for s in sorted(store_dir.iterdir()) if s.is_dir() and prefer in s.name)
        hits = 0
        for path in [slot / 'stdout'] + list((slot / 'files').rglob('*.json')):
            text = path.read_text(errors='ignore')
            if path.suffix == '.json':
                old_j, new_j = json.dumps(old)[1:-1], json.dumps(new)[1:-1]
                hits += text.count(old_j)
                text = text.replace(old_j, new_j)
            else:
                hits += text.count(old)
                text = text.replace(old, new)
            path.write_text(text)
        if not hits:
            raise RuntimeError('%r not found in %s' % (old, slot.name))
        return '%r -> %r in %s (%d places)' % (old, new, slot.name, hits)

    def retime(store_dir):
        slot = next(s for s in sorted(store_dir.iterdir()) if s.is_dir() and 'o6-acceptance' in s.name)
        done = []
        for path in [slot / 'stdout'] + list((slot / 'files').rglob('*.json')):
            text = path.read_text(errors='ignore')
            new = re.sub(r'(within 150 ms[^\n"]*?\()(\d+)( ms\))', lambda m: m.group(1) + str(int(m.group(2)) + 37) + m.group(3), text)
            new = re.sub(r'(\(measured \[)([^\]]*)', lambda m: m.group(1) + ', '.join('%.1f' % (float(x) + 3.3) for x in m.group(2).split(', ')), new)
            if new != text:
                done.append(path.name)
            path.write_text(new)
        return 'o6 measured values perturbed in ' + ', '.join(done)
    rc, text, _ = replay('J8a', retime)
    check('J8a measured values that differ from the recording (o6 timings shifted) still pass judging', rc == 0, text[-800:])
    for label, prefer, old, new in (
            ('J8b a check whose stated threshold changed (o2 4j ">= 480 px" -> ">= 300 px") fails judging',
             'o2-acceptance', '4j preview art area is large (>= 480 px wide at 1200)', '4j preview art area is large (>= 300 px wide at 1200)'),
            ('J8c a check whose stated scenario changed (o3 delays [0.9, 0] -> [0, 0]) fails judging',
             'o3-acceptance', '7 saves under injected delays [0.9, 0] end', '7 saves under injected delays [0, 0] end'),
            ('J8d dropping the "[ORIGINAL, superseded by R1]" marker from o4 5i fails judging',
             'o4-acceptance', '5i [ORIGINAL, superseded by R1] the Recordings', '5i the Recordings')):
        rc, text, _ = replay(label, lambda s, prefer=prefer, old=old, new=new: rename(s, prefer, old, new))
        check(label, rc != 0, text[-800:])

    def flip_o4_original(store_dir):
        slot = next(s for s in sorted(store_dir.iterdir()) if s.is_dir() and 'o4-acceptance' in s.name)
        text = (slot / 'stdout').read_text(errors='ignore')
        (slot / 'stdout').write_text(re.sub(r'^FAIL (5i \[ORIGINAL)', r'PASS \1', text, flags=re.M).replace('170 passed, 1 failed', '171 passed, 0 failed'))
        return 'o4 5i ORIGINAL turned PASS'
    rc, text, _ = replay('NOTE o4 original passes', flip_o4_original)
    note('when the superseded 5i ORIGINAL starts passing, judging exits %d (%s)' % (rc, 'flagged' if rc else 'accepted'))
    shutil.rmtree(copy.parent, ignore_errors=True)


# ------------------------------------------------------------------ entry points
def snapshot(package):
    package = Path(package).resolve()
    hook = Path(tempfile.mkdtemp(prefix='wp4-dump-'))
    (hook / 'sitecustomize.py').write_text(
        "import os, sys, json\n"
        "if os.environ.get('WP4_ENV_DUMP'):\n"
        "    print('WP4ENV ' + json.dumps({'argv': sys.argv, 'executable': sys.executable, 'env': dict(os.environ)}))\n"
        "    sys.stdout.flush(); os._exit(0)\n")
    out = str(package)
    norm = lambda s: s.replace(out, '$out')
    result = {}
    for path in sorted((package / 'bin').iterdir()):
        if path.name.startswith('.'):
            continue
        runtime = tempfile.mkdtemp(prefix='wp4-rt-')
        env = {'HOME': runtime, 'PATH': '/wp4-base', 'PYTHONPATH': str(hook), 'WP4_ENV_DUMP': '1', 'XDG_RUNTIME_DIR': runtime}
        try:
            text = run([str(path), '--wp4-env-dump-invalid'], env=env, timeout=30).stdout
        except subprocess.TimeoutExpired:
            text = ''
        dump = next((json.loads(line[7:]) for line in text.splitlines() if line.startswith('WP4ENV ')), None)
        if dump:
            env_out = {k: norm(v) for k, v in dump['env'].items() if k not in ('HOME', 'PYTHONPATH', 'WP4_ENV_DUMP', 'XDG_RUNTIME_DIR')}
            result[path.name] = {'kind': 'python', 'argv': [norm(x) for x in dump['argv']], 'executable': dump['executable'], 'env': env_out}
        else:
            raw = path.read_bytes().decode('utf-8', errors='ignore')
            commands = re.findall(r"makeCWrapper '[^\n]*(?:\n    [^\n]*)*", raw)
            if not commands and raw.startswith('#!'):
                commands = [raw]
            inner = path.with_name('.%s-wrapped' % path.name)
            if inner.exists():
                commands += re.findall(r"makeCWrapper '[^\n]*(?:\n    [^\n]*)*", inner.read_bytes().decode('utf-8', errors='ignore'))
            result[path.name] = {'kind': 'wrapper-text', 'commands': [norm(c) for c in commands]}
        shutil.rmtree(runtime, ignore_errors=True)
    wrapped = '\n'.join(p.read_bytes().decode('utf-8', errors='ignore') for p in (package / 'bin').iterdir() if p.name.startswith('.listening-hub'))
    result['_classic_probe_bins'] = sorted(set(re.findall(r'/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._-]+/bin', wrapped)) - {out + '/bin'})
    shutil.rmtree(hook, ignore_errors=True)
    return result


def cmd_entrypoints(a):
    if a.action == 'snapshot':
        data = snapshot(a.package)
        Path(a.out).write_text(json.dumps(data, indent=1, sort_keys=True) + '\n')
        print('wrote %s: %s' % (a.out, ', '.join(sorted(data))))
        return
    base = json.loads(Path(a.baseline).read_text())
    package = a.package
    if not package:
        built = run(['nix', 'build', '--no-link', '--print-out-paths', 'path:%s#default' % Path(a.repo).resolve()], timeout=7200)
        check('E0 nix build .#default succeeds', built.returncode == 0, built.stderr[-1500:])
        package = built.stdout.strip().splitlines()[-1] if built.returncode == 0 else None
    if not package:
        return
    now = snapshot(package)
    names = lambda d: sorted(k for k in d if not k.startswith('_'))
    check('E1 the same entry points (bin/ names)', names(now) == names(base), (names(base), names(now)))
    for name in names(base):
        if name not in now:
            continue
        old, new = base[name], now[name]
        if old['kind'] == 'python':
            check('E2 %s: same interpreter and arguments' % name, (old['argv'], old['executable']) == (new.get('argv'), new.get('executable')),
                  (old['argv'], old['executable'], new.get('argv'), new.get('executable')))
            old_path, new_path = old['env'].get('PATH', '').split(':'), (new.get('env') or {}).get('PATH', '').split(':')
            check('E3 %s: same runtime PATH, entry by entry' % name, old_path == new_path,
                  'removed %r added %r' % (sorted(set(old_path) - set(new_path)), sorted(set(new_path) - set(old_path))) if set(old_path) != set(new_path) else 'order differs')
            others = lambda env: {k: v for k, v in (env or {}).items() if k != 'PATH'}
            diff = sorted(k for k in set(others(old['env'])) | set(others(new.get('env'))) if others(old['env']).get(k) != others(new.get('env')).get(k))
            check('E4 %s: same runtime environment otherwise' % name, not diff,
                  {k: (old['env'].get(k), (new.get('env') or {}).get(k)) for k in diff})
        else:
            check('E5 %s: same wrapper' % name, old == new, (old, new))
    check('E6 the classic audio probe still finds the same runtime bin directories in the wrappers',
          base.get('_classic_probe_bins') == now.get('_classic_probe_bins'), (base.get('_classic_probe_bins'), now.get('_classic_probe_bins')))
    if a.repo:
        shell = run(['nix', 'develop', 'path:%s' % Path(a.repo).resolve(), '-c', 'python3', '-c', 'import sys; print(sys.executable)'], timeout=3600)
        executables = {v['executable'] for v in now.values() if isinstance(v, dict) and v.get('kind') == 'python'}
        check('E7 the dev shell uses the package Python environment', shell.returncode == 0 and executables == {shell.stdout.strip().splitlines()[-1]},
              (shell.stdout[-300:], shell.stderr[-300:], sorted(executables)))
        apps = run(['nix', 'eval', '--json', 'path:%s#apps.x86_64-linux' % Path(a.repo).resolve()], timeout=600)
        programs = {k: os.path.basename(v['program']) for k, v in json.loads(apps.stdout or '{}').items()}
        check('E8 the flake apps still open listening-room and listening-hub', programs == {'default': 'listening-room', 'listening-hub': 'listening-hub'}, programs)


# ------------------------------------------------------------------ docs, classic probes, structure
def cmd_docs(a):
    repo = Path(a.repo).resolve()
    contributing = (repo / 'CONTRIBUTING.md').read_text()
    security = (repo / 'SECURITY.md').read_text()
    paras = lambda text: [p for p in re.split(r'\n\s*\n', text)]
    lower = contributing.lower()
    check('D1 CONTRIBUTING notes the cost of the asset (scene) render',
          any(re.search(r'render', p, re.I) and re.search(r'asset|scene|loop|video', p, re.I)
              and re.search(r'minute|slow|cost|expensive|long|hour|cpu|time', p, re.I) for p in paras(contributing)))
    check('D2 CONTRIBUTING says how to add a test', any(re.search(r'\badd(ing)?\b|\bnew\b|\bregister', p, re.I) and re.search(r'\btests?\b|suite', p, re.I)
                                                        for p in paras(contributing)))
    check('D3 CONTRIBUTING says how to add a probe', any(re.search(r'\badd(ing)?\b|\bnew\b|\bregister', p, re.I) and re.search(r'\bprobe', p, re.I)
                                                         for p in paras(contributing)))
    named = sorted(set(re.findall(r'`((?:tests|scripts|nix|src)/[^`\s]+)`', contributing)))
    missing = [n for n in named if not any(repo.glob(re.sub(r'<[^>]+>', '*', n))) and not (repo / n).exists()]   # <name> placeholders match any file
    check('D4 every repository path CONTRIBUTING names exists', not missing, missing)
    stale = re.findall(r'\b1[0-9]{2} passed\b|\b(?:1[0-9]{2}|2[0-9]{2})(?:, | and )(?:1[0-9]{2}|2[0-9]{2})', contributing)
    note('CONTRIBUTING count-like phrases (should not duplicate expected results): %r' % stale)
    check('D5 SECURITY.md: valid_url allows loopback and LAN streams',
          any(re.search(r'valid_url|stream|url', p, re.I) and re.search(r'loopback|localhost|127\.0\.0\.1', p, re.I)
              and re.search(r'\bLAN\b|local[ -]network|private (network|address)', p, re.I) for p in paras(security)))
    check('D6 SECURITY.md: catalogue app entries run command lists',
          any(re.search(r'catalog', p, re.I) and re.search(r'\bapp\b', p, re.I) and re.search(r'command', p, re.I) for p in paras(security)))
    check('D7 SECURITY.md: the catalogue path can be overridden from the environment',
          any(re.search(r'LISTENING_HUB_CATALOG', p) or (re.search(r'catalog', p, re.I) and re.search(r'environment|env var', p, re.I)) for p in paras(security)))
    probe = run([sys.executable, '-c', '''
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
folder = tempfile.mkdtemp(); path = os.path.join(folder, 'c.json')
json.dump({"radio": []}, open(path, "w")); os.environ["LISTENING_HUB_CATALOG"] = path
os.environ["HOME"] = folder
import player_core as core
print(json.dumps({"loopback": core.valid_url("http://127.0.0.1:8000/a"), "lan": core.valid_url("http://192.168.1.20/a"),
                  "catalog": core.CATALOG == path}))
''', str(repo / 'src')], timeout=120)
    facts = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.returncode == 0 else {}
    check('D8 the stated facts hold: valid_url accepts loopback and LAN, LISTENING_HUB_CATALOG picks the catalogue',
          facts == {'loopback': True, 'lan': True, 'catalog': True}, (facts, probe.stderr[-500:]))
    leaks = run(['gitleaks', 'detect', '--no-git', '--no-banner', '--redact', '-s', str(repo)], timeout=600)
    check('D9 gitleaks detect --no-git is clean', leaks.returncode == 0, (leaks.stdout + leaks.stderr)[-800:])


def cmd_classic(a):
    repo = Path(a.repo).resolve()
    for name in ('listening-hub-audio.py', 'listening-hub-mix.py'):
        original = run(['git', '-C', str(repo), 'show', '%s:tests/probes/%s' % (a.base, name)])
        digest = hashlib.sha256(original.stdout.encode()).hexdigest() if original.returncode == 0 else None
        candidates = [p for p in repo.rglob(name) if '.git' not in p.parts]
        same = [p for p in candidates if hashlib.sha256(p.read_bytes()).hexdigest() == digest]
        check('K1 %s still exists in the tree (kept or archived, never deleted)' % name, bool(candidates), 'no file named %s' % name)
        place = candidates[0].relative_to(repo) if candidates else None
        if place and str(place) == 'tests/probes/' + name:
            text = candidates[0].read_text()
            doc = re.match(r'\s*(?:#![^\n]*\n)?\s*("""|\'\'\')(.*?)\1', text, re.S)
            note('%s kept in place; docstring: %s' % (name, ' '.join((doc.group(2) if doc else '').split())[:300]))
            check('K2 %s kept in place states what it still covers (docstring)' % name, bool(doc and len(doc.group(2).split()) >= 8))
        elif place:
            folder = candidates[0].parent
            notes = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.md', '.txt', '') and name in p.read_text(errors='ignore')]
            check('K3 %s archived under a clearly named directory' % name, re.search(r'archiv|retired|classic|legacy', str(place.parent), re.I) is not None, str(place))
            check('K4 %s archive has a note naming it with the reason' % name, bool(notes), [p.name for p in folder.iterdir()])
            check('K5 %s archived unchanged' % name, bool(same), [str(p.relative_to(repo)) for p in candidates])


def cmd_structure(a):
    repo = Path(a.repo).resolve()
    script = (repo / 'scripts' / 'listening-dev.sh').read_text()
    check('T1 scripts/listening-dev.sh hard-codes no "N passed, M failed" counts', not re.search(r'\d+ passed, \d+ failed', script),
          re.findall(r'.*\d+ passed, \d+ failed.*', script))
    nix = '\n'.join(p.read_text() for p in (repo / 'nix').glob('*.nix'))
    counts = {tool: len(re.findall(r'(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])' % tool, re.sub(r'#[^\n]*', '', nix)))
              for tool in ('playerctl', 'rofi', 'libnotify', 'xdg-utils', 'flatpak', 'wireplumber', 'hyprland', 'yt-dlp', 'chromaprint')}
    check('T2 the runtime PATH tools are listed once in the wrap helper (each named at most twice: argument and list)',
          all(n <= 2 for n in counts.values()), counts)
    flake = (repo / 'flake.nix').read_text()
    check('T3 the devShell no longer writes out its own python3.withPackages list', 'withPackages' not in flake, '')
    # Measured spans ("@@") in expected check lists are pinned here, outside the implementer's files:
    # a span matches any text, so a new or widened one (an all-"@@" entry, a "!" known failure with a
    # span, a span over a threshold) must be an acceptance decision, not an edit to an expected list.
    allowed = {
        'o6': {'1b single click -> preview visible within 150 ms for every card (measured @@ ms)',
               '1c the GTK thread was never held for more than 150 ms after the first (cold) preview (gaps @@ ms), and never more than 400 ms',
               '1l every selection (scope chips, Mix, station, mood) updates within 100 ms: @@',
               '1m the GTK thread was never held for more than 120 ms during those selections (worst @@ ms)',
               '6g the GTK thread was not blocked by the search subprocess (worst gap @@ ms)',
               '7a pressing Enter room shows "Connecting to <room>\u2026" on Now within 150 ms (@@ ms)'}}
    spans = {}
    for path in sorted((repo / 'tests' / 'probes' / 'expected').glob('*.checks')):
        for line in path.read_text().splitlines():
            if '@@' in line and not line.startswith('#'):
                spans.setdefault(path.stem, set()).add(line)
    extra = {name: sorted(lines - allowed.get(name, set())) for name, lines in spans.items() if lines - allowed.get(name, set())}
    check('T4 measured "@@" spans in expected check lists are exactly the pinned o6 timing entries (none known-failure, none new or widened)',
          not extra, extra)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('entrypoints')
    p.add_argument('action', choices=['snapshot', 'compare'])
    p.add_argument('--package')
    p.add_argument('--out')
    p.add_argument('--baseline')
    p.add_argument('--repo')
    for name in ('suites', 'canary'):
        p = sub.add_parser(name)
        p.add_argument('--repo', required=True)
        p.add_argument('--logs', default=tempfile.mkdtemp(prefix='wp4-logs-'))
        if name == 'suites':
            p.add_argument('--baseline', help='JSON list of suites that must still run (tests/fixtures/wp4-main-suites.json)')
            p.add_argument('--write-baseline', help='write the traced set here')
    p = sub.add_parser('judge')
    p.add_argument('action', choices=['record', 'controls'])
    p.add_argument('--repo', required=True)
    p.add_argument('--store', required=True)
    p.add_argument('--out', default=tempfile.mkdtemp(prefix='wp4-acceptance-'))
    for name in ('docs', 'structure'):
        sub.add_parser(name).add_argument('--repo', required=True)
    p = sub.add_parser('classic')
    p.add_argument('--repo', required=True)
    p.add_argument('--base', default='25cda4a')
    a = parser.parse_args()
    {'entrypoints': cmd_entrypoints, 'suites': cmd_suites, 'canary': cmd_canary, 'judge': cmd_judge,
     'docs': cmd_docs, 'classic': cmd_classic, 'structure': cmd_structure}[a.command](a)
    print('%d failed' % len(FAILED) + (': ' + '; '.join(FAILED) if FAILED else ''))
    sys.exit(1 if FAILED else 0)


if __name__ == '__main__':
    main()
