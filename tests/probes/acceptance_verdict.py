#!/usr/bin/env python3
"""Judge one acceptance probe run against the checks it is expected to produce.

    acceptance_verdict.py NAME RESULTS_DIR [--record]

RESULTS_DIR holds the probe's results file (the ``*acceptance*.json`` it writes: a list of
``{criterion, pass}`` records, or a dict holding that list). ``expected/NAME.checks`` lists one
check name per line; an entry may declare the measured part of its name with ``@@``
(for example ``... (worst gap @@ ms)``); that span matches only measurement characters (digits, sign, decimal point, comma,
square brackets and spaces, with at least one digit; units stay outside the span) and the rest of the name must match
exactly, so a span cannot swallow a changed word or a ``FAILED`` marker.
Nothing else is ignored: thresholds, scenario inputs and markers in a name are part of its identity. A line starting with ``!`` is a known failure, kept visible on purpose
(the superseded "5i [ORIGINAL]" check in O4); a line cannot combine ``!`` with ``@@`` (a failure is recorded under the exact name it failed with); comment lines start with ``# ``.

The run passes when:
- every failing check is a listed known failure and every known failure still fails;
- every listed check is still present (a check that disappears fails the run).

Checks that are new, not listed, are reported and allowed if they pass, so adding a check to
a probe needs no edit here. ``--record`` rewrites the list from the run, keeping comment lines. Every failing check is
recorded as a known failure and printed, so review the diff: a ``!`` line needs a comment saying why.
Passing entries that use ``@@`` are kept as written; a failing one is recorded under its exact name.
"""
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent


def load_results(directory):
    files = sorted(Path(directory).glob('*acceptance*.json'), key=lambda p: p.stat().st_mtime)
    if not files:
        sys.exit(f'no *acceptance*.json in {directory}: the probe did not finish')
    data = json.loads(files[-1].read_text())
    if isinstance(data, dict):
        data = data.get('results', data.get('checks'))
    results = []
    for item in data:
        if isinstance(item, dict):
            results.append((item.get('criterion', item.get('name')), bool(item.get('pass', item.get('ok')))))
        else:
            results.append((item[0], bool(item[1])))
    return results


WILDCARD = '@@'
# What a measured span may hold: digits, sign, decimal point, comma, square brackets and spaces (units stay outside the span).
_FILLER = r'[+\-.,\[\]\s]'
MEASUREMENT = _FILLER + r'*[0-9](?:[0-9]|' + _FILLER + ')*'


def pattern(entry):
    """The regex for an entry that declares measured spans with ``@@``, else None."""
    if WILDCARD not in entry:
        return None
    return re.compile(('(?:' + MEASUREMENT + ')').join(re.escape(part) for part in entry.split(WILDCARD)), re.S)


def load_expected(path):
    names, known = Counter(), Counter()
    comments = []
    if path.exists():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.startswith('# ') or line == '#':
                comments.append(line)
                continue
            entry = line[1:] if line.startswith('!') else line
            if not entry:
                continue
            if line.startswith('!') and WILDCARD in entry:
                sys.exit(f'{path.name}:{number}: a known failure ("!") cannot use "@@": a measured span would hide '
                         f'what failed. List the failure under the exact name it fails with: {entry}')
            names[entry] += 1
            if line.startswith('!'):
                known[entry] += 1
    return names, known, comments


def identify(name, names, patterns):
    """The expected entry a result belongs to: its exact name, else the first ``@@`` entry matching all of it."""
    if name in names and WILDCARD not in name:
        return name
    for entry, regex in patterns.items():
        if regex.fullmatch(name):
            return entry
    return name


def main(argv):
    record = '--record' in argv
    args = [arg for arg in argv if arg != '--record']
    if len(args) != 2:
        sys.exit(__doc__)
    name, directory = args
    expected_file = Path(os.environ.get('LISTENING_VERDICT_EXPECTED') or HERE / 'expected') / f'{name}.checks'
    results = load_results(directory)
    names, known, comments = load_expected(expected_file)
    patterns = {entry: pattern(entry) for entry in names if pattern(entry)}
    raw = [check for check, _ok in results]
    results = [(identify(check, names, patterns), ok) for check, ok in results]
    passed, failed = Counter(), Counter()
    for check, ok in results:
        (passed if ok else failed)[check] += 1
    seen = passed + failed
    problems = []
    for check, count in failed.items():
        if count > known[check]:
            problems.append(f'FAILED {check}')
    for check, count in known.items():
        if failed[check] < count:
            problems.append(f'known failure now passes (update {expected_file.name}): {check}')
    for check, count in names.items():
        if seen[check] < count:
            problems.append(f'MISSING check (disappeared): {check}')
    new = sum(max(0, seen[c] - names[c]) for c in seen)
    summary = (f'{name}: {sum(passed.values())} passed, {sum(failed.values())} failed '
               f'({sum(known.values())} known), {new} new, {sum(1 for p in problems if p.startswith("MISSING"))} missing')
    if record:
        out = [check if ok else '!' + exact for (check, ok), exact in zip(results, raw)]
        expected_file.parent.mkdir(exist_ok=True)
        expected_file.write_text('\n'.join(comments + out) + '\n')
        for problem in problems:
            if problem.startswith('FAILED'):
                print(f'  RECORDED AS A KNOWN FAILURE (add a comment line saying why): {problem[7:]}')
        print(f'{summary}: recorded {len(out)} checks in {expected_file}')
        return 0
    for problem in problems:
        print('  ' + problem)
    print(summary + (' FAIL' if problems else ' ok'))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
