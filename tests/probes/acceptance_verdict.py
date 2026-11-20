#!/usr/bin/env python3
"""Judge one acceptance probe run against the checks it is expected to produce.

    acceptance_verdict.py NAME RESULTS_DIR [--record]

RESULTS_DIR holds the probe's results file (the ``*acceptance*.json`` it writes: a list of
``{criterion, pass}`` records, or a dict holding that list). ``expected/NAME.checks`` lists one
check name per line. A line starting with ``!`` is a known failure, kept visible on purpose
(the superseded "5i [ORIGINAL]" check in O4); comment lines start with ``# ``.

The run passes when:
- every failing check is a listed known failure and every known failure still fails;
- every listed check is still present (a check that disappears fails the run).

Checks that are new, not listed, are reported and allowed if they pass, so adding a check to
a probe needs no edit here. ``--record`` rewrites the list from the run, keeping comment lines. Every failing check is
recorded as a known failure and printed, so review the diff: a ``!`` line needs a comment saying why.
"""
from collections import Counter
import json
from pathlib import Path
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


def load_expected(path):
    names, known = Counter(), Counter()
    comments = []
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith('# ') or line == '#':
                comments.append(line)
            elif line.startswith('!'):
                known[line[1:]] += 1
                names[line[1:]] += 1
            elif line:
                names[line] += 1
    return names, known, comments


def main(argv):
    record = '--record' in argv
    args = [arg for arg in argv if arg != '--record']
    if len(args) != 2:
        sys.exit(__doc__)
    name, directory = args
    expected_file = HERE / 'expected' / f'{name}.checks'
    results = load_results(directory)
    names, known, comments = load_expected(expected_file)
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
        out = [('' if ok else '!') + check for check, ok in results]
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
