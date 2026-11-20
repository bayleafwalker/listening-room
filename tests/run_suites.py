#!/usr/bin/env python3
"""Run the unit and oracle suites: the one place that decides which test files run.

Every ``tests/test_*.py`` runs automatically, so adding a suite means adding the file and
nothing else. A suite that must not run by default (it needs built assets, a baseline build
or minutes of CPU) is named with its reason in ``tests/suites-manual.txt``; it runs when you
name it or pass ``--all``.

Used by the package checkPhase (nix/package.nix) and ``scripts/listening-dev.sh test``.

    run_suites.py                    every automatic suite
    run_suites.py --all              automatic and manual suites
    run_suites.py NAME [NAME...]     those suites (``tests/`` and ``.py`` are optional)
    run_suites.py --except NAME ...  every automatic suite but these (the build runs two of
                                     them in a wrapper of their own)
    run_suites.py --list             what would run, one per line

A test file that does not match ``test_listening_*.py``, a manual entry without a reason or
for a file that does not exist, and an ``--except`` name that is not a suite are errors, so
a suite can neither be forgotten nor silently dropped. Every suite runs with ``netguard`` installed:
a connection to a non-loopback address fails the test. All selected suites run; the exit
status is 1 if any failed.
"""
from pathlib import Path
import subprocess
import sys

TESTS = Path(__file__).resolve().parent
MANUAL_FILE = TESTS / 'suites-manual.txt'


# Run a suite as __main__ with the network guard installed (tests/netguard.py).
GUARDED = ('import runpy, sys; sys.path.insert(0, %r); import netguard; netguard.install(); '
           'sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")' % str(TESTS))


def manual_suites():
    manual = {}
    for number, line in enumerate(MANUAL_FILE.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, _, reason = line.partition(' ')
        if not reason.strip():
            sys.exit(f'{MANUAL_FILE.name}:{number}: {name} needs a reason after the file name')
        manual[name] = reason.strip()
    return manual


def discover():
    found = sorted(path.name for path in TESTS.glob('test_*.py'))
    stray = [name for name in found if not (name.startswith('test_listening_') and name.endswith('.py'))]
    if stray:
        sys.exit('test files must be named test_listening_*.py (helpers must not start with test_): ' + ', '.join(stray))
    manual = manual_suites()
    missing = [name for name in manual if name not in found]
    if missing:
        sys.exit(f'{MANUAL_FILE.name} lists files that do not exist: ' + ', '.join(missing))
    return found, manual


def normalise(name):
    name = name.removeprefix('tests/')
    return name if name.endswith('.py') else name + '.py'


def main(argv):
    found, manual = discover()
    everything, listing, skipping, skip, names = False, False, False, [], []
    args = iter(argv)
    for arg in args:
        if arg == '--all':
            everything, skipping = True, False
        elif arg == '--list':
            listing, skipping = True, False
        elif arg == '--except':
            skipping = True
        elif skipping and not arg.startswith('--'):
            skip.append(normalise(arg))
        else:
            names.append(normalise(arg))
    unknown = [name for name in names + skip if name not in found]
    if unknown:
        sys.exit('not a suite in tests/: ' + ', '.join(unknown))
    if names:
        selected = names
    else:
        selected = [name for name in found if everything or name not in manual]
    selected = [name for name in selected if name not in skip]
    if listing:
        print('\n'.join(selected))
        return 0
    failed = []
    for name in selected:
        print('==', name, flush=True)
        if subprocess.run([sys.executable, '-c', GUARDED, str(TESTS / name)]).returncode:
            failed.append(name)
    if failed:
        print('FAILED suites: ' + ', '.join(failed), file=sys.stderr)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
