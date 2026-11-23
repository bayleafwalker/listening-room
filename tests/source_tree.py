"""Which src/ tree a test helper imports from.

A probe that is pointed at another tree (--source X, or --repo X) puts X at the front of
sys.path before it imports the helpers. A helper must then keep X, not put the default tree
back in front of it: that silently turned every negative control into a run against the
current source. Entries that only come from PYTHONPATH (the dev loop exports the default
src there) are not a choice and do not count.
"""
import os
from pathlib import Path
import sys

MARKER = 'room_session.py'


def chosen_source():
    """The src directory a caller put on sys.path itself, or None."""
    inherited = {str(Path(part).resolve()) for part in os.environ.get('PYTHONPATH', '').split(os.pathsep) if part}
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry).resolve()
        if (path / MARKER).exists():
            return None if str(path) in inherited else path
    return None


def use_source(default_hub_file):
    """Put the right src tree first on sys.path and return it.

    Precedence: a tree the caller already chose, then LISTENING_HUB_SOURCE, then the repo src.
    """
    chosen = chosen_source()
    if chosen is not None:
        source = chosen
        os.environ['LISTENING_HUB_SOURCE'] = str(source / 'listening_hub.py')
    else:
        source = Path(os.environ.get('LISTENING_HUB_SOURCE', default_hub_file)).resolve().parent
    if str(source) in sys.path:
        sys.path.remove(str(source))
    sys.path.insert(0, str(source))
    return source
