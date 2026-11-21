"""The acceptance verdict: what an expected-file ``@@`` span may match, and the refusal of ``!`` with ``@@``."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROBES = Path(__file__).resolve().parent / 'probes'
VERDICT = PROBES / 'acceptance_verdict.py'


def verdict(expected, results):
    """Run the verdict on a fake outcome whose expected file holds `expected` lines; returns (status, output)."""
    name = 'zz-verdict-test'
    with tempfile.TemporaryDirectory() as directory:
        expected_file = Path(directory) / (name + '.checks')
        (Path(directory) / 'x-acceptance.json').write_text(json.dumps([{'criterion': n, 'pass': ok} for n, ok in results]))
        expected_file.write_text('\n'.join(expected) + '\n')
        run = subprocess.run([sys.executable, str(VERDICT), name, directory], capture_output=True, text=True,
                             env=dict(os.environ, LISTENING_VERDICT_EXPECTED=directory))
    return run.returncode, run.stdout + run.stderr


class MeasuredSpans(unittest.TestCase):
    def test_numbers_with_units_match(self):
        for measured in ('12', '-3.5', '+0.25', '[12.3, 4.5, 6]', '1200.5'):
            status, output = verdict(['gap (%s ms) ok' % '@@'], [('gap (%s ms) ok' % measured, True)])
            self.assertEqual(status, 0, (measured, output))

    def test_words_and_markers_do_not_match(self):
        for swallowed in ('FAILED', 'NaN', 'none', '12 FAILED', '[]', '', 'seconds slower'):
            status, output = verdict(['gap (@@ ms) ok'], [('gap (%s ms) ok' % swallowed, True)])
            self.assertEqual(status, 1, (swallowed, output))
            self.assertIn('MISSING', output)

    def test_a_digit_elsewhere_does_not_license_a_wordy_span(self):
        status, _ = verdict(['gap (@@ ms) of 5'], [('gap (broken ms) of 5', True)])
        self.assertEqual(status, 1)

    def test_a_failing_measured_check_still_fails(self):
        status, output = verdict(['gap (@@ ms) ok'], [('gap (12 ms) ok', False)])
        self.assertEqual(status, 1)
        self.assertIn('FAILED', output)

    def test_a_trailing_span_is_numbers_only_too(self):
        status, _ = verdict(['updates: @@'], [("updates: {'scope:all': 4.2}", True)])
        self.assertEqual(status, 1)
        status, _ = verdict(['updates: @@'], [('updates: 4.2', True)])
        self.assertEqual(status, 0)


class KnownFailuresStayExact(unittest.TestCase):
    def test_bang_with_wildcard_is_refused_with_a_message(self):
        status, output = verdict(['!gap (@@ ms) ok'], [('gap (12 ms) ok', False)])
        self.assertNotEqual(status, 0)
        self.assertIn('cannot use "@@"', output)
        self.assertIn('zz-verdict-test.checks:1', output)

    def test_an_exact_known_failure_still_works(self):
        status, _ = verdict(['!gap (12 ms) ok'], [('gap (12 ms) ok', False)])
        self.assertEqual(status, 0)


if __name__ == '__main__':
    unittest.main()
