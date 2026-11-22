"""Independent acceptance checks for O11-HYGIENE criterion 1 (`--source` is honoured).

Owned by the acceptance author, not the implementer. Behavioural: the probe helpers are run as child processes
against copies of the source tree and the oracle is where the window's modules were really imported from.

    python3 tests/probes/listening-o11h-acceptance.py --repo WORKTREE --output OUTDIR

The trap. A scratch copy of the repo keeps the real tests/ but its src/ is POISONED: every module there records its
name and raises ImportError when imported. That poisoned tree is what each helper falls back to by default
(``parents[2]/src``), and in the "dev loop" scenario it is also what LISTENING_HUB_SOURCE, PYTHONPATH and
LISTENING_AMBIENCE_SOURCE name (scripts/listening-dev.sh exports exactly those for the repo's own src/). A helper run
with ``--source GOOD`` (an intact copy of src/) must then import every module from GOOD: a single poisoned import,
caught or not, means the flag was not honoured. The poison log is the oracle, not the exit status, because the
helpers catch ImportError for optional modules.

The negative control the brief asks for. A real acceptance probe (listening-o10h-acceptance, which runs through
o6_env) is run from the real worktree, in the dev-loop environment, with ``--source BROKEN``: a copy of src/ whose
next_window.py raises on import. It must fail. On main it passes, because o6_env imports next_window after
tests/test_listening_session.py has put the default src/ first on sys.path.

Every check runs with LISTENING_HUB_SOURCE unset and again with the dev-loop variables set, so a fix that only works
when the environment variable happens to agree with --source fails one of the two.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
parser.add_argument('--repo', default=None)
parser.add_argument('--output', default=str(Path(tempfile.gettempdir()) / 'listening-o11h-acceptance'))
ARGS = parser.parse_args()
REPO = Path(ARGS.repo).resolve() if ARGS.repo else HERE.parents[1]
OUT = Path(ARGS.output)
OUT.mkdir(parents=True, exist_ok=True)
RESULTS = []
# Self-chosen (not from the brief): a hung child must not hang the acceptance run. The slowest helper below takes
# well under a minute on an idle host; 300 s leaves room for a loaded one.
CHILD_TIMEOUT = 300

POISON = '''import os as _o11h_os
_o11h_log = _o11h_os.environ.get('O11H_POISON_LOG')
if _o11h_log:
    with open(_o11h_log, 'a') as _o11h_f:
        _o11h_f.write(__name__ + '\\n')
raise ImportError('O11H poison: %s was imported from the default src/, not from --source' % __name__)
'''

BROKEN = '''raise ImportError('O11H deliberately broken copy: next_window must not import')
'''

DRIVER = '''"""O11H driver: open the real window through o6_env and report where each listening module came from."""
import json, os, sys
sys.path.insert(0, os.environ['O11H_PROBES'])
import o6_env
env = o6_env.Env(__doc__, default_output=os.environ['O11H_DRIVER_OUT'])
with env:
    window = env.launch(page='listen')
    env.pump(.5)
    names = set(json.loads(os.environ['O11H_MODULES']))
    report = {name: getattr(module, '__file__', None) for name, module in list(sys.modules.items()) if name in names}
    with open(os.environ['O11H_REPORT'], 'w') as handle:
        json.dump(report, handle)
'''


def check(name, ok, detail=''):
    RESULTS.append({'criterion': name, 'pass': bool(ok), 'detail': str(detail)[:900]})
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else ': ' + str(detail)[:900]), flush=True)


def info(text):
    print('INFO ' + text, flush=True)


IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '.auditctl', '_artifacts')


def build_trees(work):
    """default repo copy (real tests/, poisoned src/), GOOD (intact src/), BROKEN (next_window raises)."""
    default = work / 'repo'
    shutil.copytree(REPO / 'tests', default / 'tests', ignore=IGNORE)
    shutil.copytree(REPO / 'src', default / 'src', ignore=IGNORE)
    for path in (default / 'src').rglob('*.py'):
        path.write_text(POISON)
    good = work / 'good' / 'src'
    shutil.copytree(REPO / 'src', good, ignore=IGNORE)
    broken = work / 'broken' / 'src'
    shutil.copytree(REPO / 'src', broken, ignore=IGNORE)
    (broken / 'next_window.py').write_text(BROKEN + (REPO / 'src' / 'next_window.py').read_text())
    return default, good, broken


def environment(scenario, default_src, tests):
    """The child environment: either no source variables at all, or the dev loop's, naming the default tree."""
    env = {k: v for k, v in os.environ.items()
           if k not in ('LISTENING_HUB_SOURCE', 'LISTENING_AMBIENCE_SOURCE', 'PYTHONPATH', 'WAYLAND_DISPLAY',
                        'HYPRLAND_INSTANCE_SIGNATURE', 'LISTENING_PROBE_ISOLATED')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    if scenario == 'dev loop':
        env.update(LISTENING_HUB_SOURCE=str(default_src / 'listening_hub.py'),
                   LISTENING_AMBIENCE_SOURCE=str(default_src / 'ambience.py'),
                   PYTHONPATH='%s:%s' % (default_src, tests))
    return env


def run(command, env, log, cwd):
    started = time.monotonic()
    try:
        with open(log, 'w') as handle:
            code = subprocess.call(command, env=env, stdout=handle, stderr=subprocess.STDOUT, cwd=cwd,
                                   timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        code = 'timeout'
    return code, time.monotonic() - started


def tail(log, lines=12):
    try:
        return ' | '.join(Path(log).read_text(errors='replace').splitlines()[-lines:])
    except OSError:
        return ''


def poisoned(log):
    try:
        return sorted(set(Path(log).read_text().split()))
    except OSError:
        return []


SCENARIOS = (('env unset', 'with LISTENING_HUB_SOURCE unset'),
             ('dev loop', 'with LISTENING_HUB_SOURCE and PYTHONPATH naming the default src/'))

# Helpers that take a source tree and can run without a built package. listening-metadata-model and
# listening-session-mpris also take --source but need a built package (--package), so they are not run here.
HELPERS = ('listening-listen-ui', 'listening-session-ui', 'listening-next-ui', 'listening-next-rooms',
           'listening-next-prefs')


def main():
    work = Path(tempfile.mkdtemp(prefix='o11h-trees-'))
    logs = OUT / 'logs'
    logs.mkdir(exist_ok=True)
    try:
        default, good, broken = build_trees(work)
        default_src, default_tests = default / 'src', default / 'tests'
        modules = sorted(p.stem for p in (REPO / 'src').glob('*.py'))
        driver = work / 'driver.py'
        driver.write_text(DRIVER)

        def drive(scenario, source, tag):
            env = environment(scenario, default_src, default_tests)
            report = work / ('report-%s.json' % tag)
            log_poison = work / ('poison-%s.log' % tag)
            env.update(O11H_PROBES=str(default_tests / 'probes'), O11H_DRIVER_OUT=str(work / ('out-' + tag)),
                       O11H_MODULES=json.dumps(modules), O11H_REPORT=str(report), O11H_POISON_LOG=str(log_poison))
            command = [sys.executable, str(driver)] + (['--source', str(source)] if source else [])
            code, took = run(command, env, logs / ('driver-%s.log' % tag), work)
            data = json.loads(report.read_text()) if report.exists() else None
            return code, took, data, poisoned(log_poison), logs / ('driver-%s.log' % tag)

        # Control: the trap works. Without --source the helper falls back to the poisoned default and records it.
        code, took, data, hits, log = drive('env unset', None, 'control')
        check('H0 control: o6_env without --source falls back to the poisoned default src/ and the trap records it',
              bool(hits), 'exit %s, poisoned %s; %s' % (code, hits, tail(log)))

        for scenario, words in SCENARIOS:
            tag = scenario.replace(' ', '-')
            code, took, data, hits, log = drive(scenario, good, 'good-' + tag)
            info('o6_env driver %s: exit %s in %.1f s' % (scenario, code, took))
            check('H1 o6_env --source GOOD opens the window and finishes (%s)' % words,
                  code == 0 and data is not None, 'exit %s; %s' % (code, tail(log)))
            check('H2 o6_env --source GOOD imports nothing from the default src/ (%s)' % words,
                  not hits, 'poisoned imports: %s' % hits)
            wrong = {name: path for name, path in (data or {}).items()
                     if not (path and Path(path).resolve().is_relative_to(good.resolve()))}
            needed = {'listening_hub', 'room_session', 'next_window', 'next_listen'}
            check('H3 every listening module the o6_env window loaded came from --source GOOD, next_window included (%s)'
                  % words, data is not None and not wrong and needed <= set(data),
                  'from elsewhere: %s; missing: %s' % (wrong, sorted(needed - set(data or {}))))

            for helper in HELPERS:
                env = environment(scenario, default_src, default_tests)
                log_poison = work / ('poison-%s-%s.log' % (helper, tag))
                env['O11H_POISON_LOG'] = str(log_poison)
                out = work / ('out-%s-%s' % (helper, tag))
                command = [sys.executable, str(default_tests / 'probes' / (helper + '.py')),
                           '--source', str(good), '--output', str(out)]
                code, took = run(command, env, logs / ('%s-%s.log' % (helper, tag)), work)
                hits = poisoned(log_poison)
                info('%s %s: exit %s in %.1f s' % (helper, scenario, code, took))
                check('H4 %s --source GOOD imports nothing from the default src/ (%s)' % (helper, words),
                      not hits, 'exit %s; poisoned imports: %s; %s' % (code, hits, tail(logs / ('%s-%s.log' % (helper, tag)))))

        # The brief's negative control: a real o6_env acceptance probe, from the real worktree, pointed at a broken copy.
        probe = REPO / 'tests' / 'probes' / 'listening-o10h-acceptance.py'
        for scenario, words in SCENARIOS:
            tag = scenario.replace(' ', '-')
            env = environment(scenario, REPO / 'src', REPO / 'tests')
            outcomes = {}
            for label, source in (('good', good), ('broken', broken)):
                out = work / ('o10h-%s-%s' % (label, tag))
                code, took = run([sys.executable, str(probe), '--repo', str(REPO), '--source', str(source),
                                  '--output', str(out)], env, logs / ('o10h-%s-%s.log' % (label, tag)), REPO)
                found = sorted(out.glob('*acceptance*.json')) if out.exists() else []
                results = json.loads(found[-1].read_text())['results'] if found else []
                passed = code == 0 and results and all(r['pass'] for r in results)
                outcomes[label] = (code, len(results), sum(not r['pass'] for r in results), passed)
                info('o10h --source %s %s: exit %s, %d checks, %d failed, %.1f s'
                     % (label.upper(), scenario, code, len(results), outcomes[label][2], took))
            check('N0 control: listening-o10h-acceptance --source GOOD (an intact copy) passes (%s)' % words,
                  outcomes['good'][3], 'exit/checks/failed %s; %s' % (outcomes['good'][:3], tail(logs / ('o10h-good-%s.log' % tag))))
            check('N1 negative control: listening-o10h-acceptance --source BROKEN (next_window raises) fails (%s)' % words,
                  not outcomes['broken'][3],
                  'it passed: exit/checks/failed %s, so it never imported next_window from --source' % (outcomes['broken'][:3],))
    except Exception:
        check('the probe ran to the end', False, traceback.format_exc())
    finally:
        shutil.rmtree(work, ignore_errors=True)
        (OUT / 'acceptance-o11h.json').write_text(json.dumps({'results': RESULTS}, indent=2))
        failed = [r for r in RESULTS if not r['pass']]
        print(f'{len(RESULTS) - len(failed)} passed, {len(failed)} failed', flush=True)


main()
