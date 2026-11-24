"""Whether scene-loop shedding works, for `listening-roomctl inspect`.

Shedding (next_widgets._load_shed) frees an off-screen scene loop's video sink. It runs in the window's
process, and `inspect` is answered by the room controller, so the window leaves a small status file in
the runtime directory (one per window process, removed when it exits) and `status()` reads it. There is
no socket or action behind it: nothing here changes what the window does, and without a runtime
directory nothing is written.

The reader never imports GTK, so any tool can use it. State names are stable:
  active       shedding is on: an off-screen loop gives up its video sink and takes a new one when shown
  gtk          this GTK version is not the one whose sink leaks (loops keep their sink; nothing is wrong)
  no_libgstgl  GStreamer's GL library is missing (loops keep their sink)
  refused      a sink release found other than the expected references (`refs_observed`) and shedding
               switched itself off for the rest of this process (safe; loops keep their sink from now on)
  no_player    the loops' players cannot be reached, so off-screen loops are only paused
  unreported   no running window has reported (the window is closed, or no scene loop has played yet)
"""
import json
import os
from pathlib import Path
import tempfile

FILE = 'scene-loops-%d.json'
PREFIX, SUFFIX = 'scene-loops-', '.json'

SUMMARIES = {
    'active': 'Shedding is active: an off-screen scene loop frees its video output and gets a new one when shown.',
    'gtk': 'Shedding is not used with GTK %(gtk)s; off-screen scene loops keep their video output (about 42-49 MB each).',
    'no_libgstgl': 'Shedding is off: GStreamer GL (libgstgl) is missing, so off-screen scene loops keep their video output (about 42-49 MB each).',
    'refused': ('Shedding switched itself off for this window after a release found %(refs_observed)s references '
                'where 2 were expected; off-screen scene loops keep their video output from now on (about 42-49 MB each).'),
    'no_player': 'Shedding is off: the scene loops\' players cannot be reached, so off-screen loops are only paused.',
    'unreported': 'No open window has reported scene-loop shedding yet (the window is closed, or no scene loop has played).',
}

_status = {'state': None, 'gtk': None, 'refs_observed': None, 'shed': 0, 'resumed': 0}


def _directory():
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime) / 'listening-hub' if runtime else None


def _write():
    directory = _directory()
    if directory is None:
        return
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=directory, prefix='.scene-loops-')
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(dict(_status, pid=os.getpid()), handle)
        os.replace(name, directory / (FILE % os.getpid()))
    except OSError:
        pass                      # a status file is never worth disturbing the window


def _remove():
    directory = _directory()
    if directory is not None:
        try:
            (directory / (FILE % os.getpid())).unlink()
        except OSError:
            pass


def decided(state, gtk=None):
    """The window worked out whether shedding can run (once per process)."""
    if _status['state'] is None:
        import atexit
        atexit.register(_remove)
    _status.update(state=state, gtk=gtk)
    _write()


def refused(refs_observed):
    """A sink release found `refs_observed` references on the GL context instead of the expected two."""
    _status.update(state='refused', refs_observed=refs_observed)
    _write()


def shed():
    _status['shed'] += 1
    _write()


def resumed():
    _status['resumed'] += 1
    _write()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True               # exists, not ours to signal
    return True


def status():
    """The newest running window's report, with a plain-language `summary` (always the same keys)."""
    directory = _directory()
    found = []
    for path in (directory.glob(PREFIX + '*' + SUFFIX) if directory and directory.is_dir() else ()):
        try:
            record = json.loads(path.read_text())
            if _alive(record['pid']) and record['state'] in SUMMARIES:
                found.append((path.stat().st_mtime, record))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    found.sort(key=lambda item: item[0])
    record = found[-1][1] if found else {'state': 'unreported'}
    def count(key):
        value = record.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    shed, resumed = count('shed'), count('resumed')
    refs = count('refs_observed')
    gtk = record.get('gtk') if isinstance(record.get('gtk'), str) else None
    summary = SUMMARIES[record['state']] % {'gtk': gtk or 'this version', 'refs_observed': refs if refs is not None else 'other'}
    if shed is not None:
        summary += ' %d shed and %d resumed in this window.' % (shed, resumed or 0)
    return {'state': record['state'], 'summary': summary, 'gtk': gtk, 'refs_observed': refs,
            'shed': shed, 'resumed': resumed, 'windows': len(found)}
