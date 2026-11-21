"""Start-up hook that puts the network guard into a Python child of a guarded suite.

``netguard.install()`` puts this directory first on ``PYTHONPATH`` and sets ``LISTENING_NETGUARD``;
a child Python (``sys.executable``, a helper script, a fixture server) imports this module at start-up,
installs the same guard, then runs the interpreter's own ``sitecustomize`` (the Nix Python environment has one).
Without the variable it does nothing but chain.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if os.environ.get('LISTENING_NETGUARD'):
    spec = importlib.util.spec_from_file_location('netguard', os.path.join(os.path.dirname(HERE), 'netguard.py'))
    guard = importlib.util.module_from_spec(spec)
    sys.modules.setdefault('netguard', guard)
    spec.loader.exec_module(guard)
    guard.install()

# Chain to the next sitecustomize on the path, as the interpreter would have run without this one.
for entry in sys.path:
    if os.path.abspath(entry or '.') == HERE:
        continue
    candidate = os.path.join(entry or '.', 'sitecustomize.py')
    if os.path.isfile(candidate):
        chained = importlib.util.spec_from_file_location('_chained_sitecustomize', candidate)
        chained.loader.exec_module(importlib.util.module_from_spec(chained))
        break
