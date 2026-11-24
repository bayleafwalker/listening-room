"""tests/source_tree.py: which src tree a probe's helpers import from.

An explicit --source tree wins over LISTENING_HUB_SOURCE even when it is also on PYTHONPATH; the dev
loop's ordinary PYTHONPATH export is not a choice. Each case runs in a fresh interpreter, because the
helper reads sys.path and the environment as the process started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TESTS = Path(__file__).resolve().parent
SCRIPT = '''
import json, os, sys
sys.path.insert(0, %r)
explicit = os.environ.pop('TEST_EXPLICIT', '')
if explicit:
    sys.path.insert(0, explicit)       # what a probe does with --source
import source_tree
source = source_tree.use_source(os.path.join(os.environ['TEST_DEFAULT'], 'listening_hub.py'))
print(json.dumps({'source': str(source), 'env': os.environ.get('LISTENING_HUB_SOURCE')}))
''' % str(TESTS)


class SourceTree(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.trees = {}
        for name in ('default', 'x', 'y'):
            tree = Path(directory.name, name).resolve()
            tree.mkdir()
            (tree / 'room_session.py').write_text('')
            self.trees[name] = tree

    def run_case(self, explicit=None, pythonpath=(), env=None):
        child = {key: value for key, value in os.environ.items()
                 if key not in ('PYTHONPATH', 'LISTENING_HUB_SOURCE')}
        child['TEST_DEFAULT'] = str(self.trees['default'])
        if pythonpath:
            child['PYTHONPATH'] = os.pathsep.join(str(self.trees[name]) for name in pythonpath)
        if env:
            child['LISTENING_HUB_SOURCE'] = str(self.trees[env] / 'listening_hub.py')
        if explicit:
            child['TEST_EXPLICIT'] = str(self.trees[explicit])
        result = subprocess.run([sys.executable, '-c', SCRIPT], env=child, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_explicit_source_that_is_also_on_pythonpath_beats_the_env_tree(self):
        got = self.run_case(explicit='x', pythonpath=['x'], env='y')
        self.assertEqual(got['source'], str(self.trees['x']))
        self.assertEqual(got['env'], str(self.trees['x'] / 'listening_hub.py'))

    def test_explicit_source_off_pythonpath_beats_the_env_tree(self):
        got = self.run_case(explicit='x', pythonpath=[], env='y')
        self.assertEqual(got['source'], str(self.trees['x']))

    def test_the_dev_loops_pythonpath_export_is_not_a_choice(self):
        got = self.run_case(pythonpath=['x'], env='y')
        self.assertEqual(got['source'], str(self.trees['y']))
        self.assertEqual(got['env'], str(self.trees['y'] / 'listening_hub.py'))

    def test_dev_loop_with_matching_env_and_no_source_uses_it(self):
        self.assertEqual(self.run_case(pythonpath=['x'], env='x')['source'], str(self.trees['x']))

    def test_nothing_chosen_uses_the_default_tree(self):
        self.assertEqual(self.run_case()['source'], str(self.trees['default']))


if __name__ == '__main__':
    unittest.main()
