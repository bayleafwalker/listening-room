"""Which src/ tree a test helper imports from.

A probe that is pointed at another tree (--source X, or --repo X) puts X at the front of
sys.path before it imports the helpers. A helper must then keep X, not put the default tree
back in front of it: that silently turned every negative control into a run against the
current source. Entries that only come from PYTHONPATH (the dev loop exports the default
src there) are not a choice and do not count. A tree that is on PYTHONPATH too still counts when the
caller put it on sys.path itself (an explicit --source X with X exported): it then appears twice,
once from the insert and once from PYTHONPATH.
"""
import os
from pathlib import Path
import sys

MARKER = 'room_session.py'


def chosen_source():
    """The src directory a caller put on sys.path itself, or None."""
    inherited = {str(Path(part).resolve()) for part in os.environ.get('PYTHONPATH', '').split(os.pathsep) if part}
    resolved = [Path(entry).resolve() for entry in sys.path if entry]
    for path in resolved:
        if (path / MARKER).exists():
            # PYTHONPATH puts an entry on sys.path once; a second copy is the caller's own insert.
            return None if str(path) in inherited and resolved.count(path) == 1 else path
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
