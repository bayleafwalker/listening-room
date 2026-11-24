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
  refused      a sink release found other than the expected state (`refs_observed` GL context references,
               2 expected; `sink_finalized` false when the sink was still held) and shedding switched
               itself off for the rest of this process (safe; loops keep their sink from now on)
  no_player    the loops' players cannot be reached, so off-screen loops are only paused
  unreported   no running window has reported (the window is closed, or no scene loop has played yet)
"""
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

FILE = 'scene-loops-%d.json'
LOCK = 'scene-loops-%d.lock'
NAME = re.compile(r'scene-loops-([0-9]{1,9})\.json')
# A window is live only while it holds an exclusive flock on its lock file: the kernel drops the lock when
# the process ends, so a stale file is never mistaken for a window, whatever pid the system reuses.

SUMMARIES = {
    'active': 'Shedding is active: an off-screen scene loop frees its video output and gets a new one when shown.',
    'gtk': 'Shedding is not used with GTK %(gtk)s; off-screen scene loops keep their video output (about 42-49 MB each).',
    'no_libgstgl': 'Shedding is off: GStreamer GL (libgstgl) is missing, so off-screen scene loops keep their video output (about 42-49 MB each).',
    'refused': ('Shedding switched itself off for this window after a release found %(why)s; off-screen scene '
                'loops keep their video output from now on (about 42-49 MB each).'),
    'no_player': 'Shedding is off: the scene loops\' players cannot be reached, so off-screen loops are only paused.',
    'unreported': 'No open window has reported scene-loop shedding yet (the window is closed, or no scene loop has played).',
}

_status = {'state': None, 'gtk': None, 'refs_observed': None, 'sink_finalized': None, 'shed': 0, 'resumed': 0}


def _directory():
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime) / 'listening-hub' if runtime else None


_lock = threading.Condition()
_asked = _done = 0           # writes asked for and written (the window never waits for the disk)
_worker = None
RETRY_S = .2                   # between tries while a reader holds the window's lock file
_held = None                 # the lock file's descriptor, open for the life of the process


def _write():
    """Ask for the status to be saved; a worker thread does it, so the GTK thread never touches a file."""
    global _asked, _worker
    with _lock:
        _asked += 1
        if _worker is None:
            _worker = threading.Thread(target=_run, name='scene-shedding-status', daemon=True)
            _worker.start()
        _lock.notify_all()


def flush(timeout=5.):
    """Wait until every status change asked for so far is saved (tests; the window never calls it)."""
    with _lock:
        _lock.wait_for(lambda: _done >= _asked, timeout)


def _run():
    global _done
    while True:
        with _lock:
            _lock.wait_for(lambda: _done < _asked)
            target, snapshot = _asked, dict(_status, pid=os.getpid())
        if not _save(snapshot):
            time.sleep(RETRY_S)       # the lock is busy (a status reader holds it for an instant): try again
            continue
        with _lock:
            _done = target
            _lock.notify_all()


def _save(snapshot):
    """Write the status; False only when the window's own lock could not be taken yet (to be retried)."""
    global _held
    directory = _directory()
    if directory is None:
        return True
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _held is None:
            descriptor = os.open(directory / (LOCK % os.getpid()), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(descriptor)
                return False
            _held = descriptor
        descriptor, name = tempfile.mkstemp(dir=directory, prefix='.scene-loops-')
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(snapshot, handle)
        os.replace(name, directory / (FILE % os.getpid()))
    except OSError:
        pass                      # a status file is never worth disturbing the window
    return True


def _remove():
    flush(1.)
    directory = _directory()
    if directory is not None:
        try:
            (directory / (FILE % os.getpid())).unlink()
            (directory / (LOCK % os.getpid())).unlink()
        except OSError:
            pass


def decided(state, gtk=None):
    """The window worked out whether shedding can run (once per process)."""
    if _status['state'] is None:
        import atexit
        atexit.register(_remove)
    _status.update(state=state, gtk=gtk)
    _write()


def refused(refs_observed, sink_finalized):
    """A sink release was refused: the GL context had `refs_observed` references (2 expected) and the
    video sink was (`sink_finalized`) or was not gone."""
    _status.update(state='refused', refs_observed=refs_observed, sink_finalized=bool(sink_finalized))
    _write()


def shed():
    _status['shed'] += 1
    _write()


def resumed():
    _status['resumed'] += 1
    _write()


def _live(directory, path):
    """The status record in `path` when a running window holds its lock, else None. Never raises."""
    try:
        match = NAME.fullmatch(path.name)
        record = json.loads(path.read_text())
        pid = record['pid']
        if (not match or isinstance(pid, bool) or not isinstance(pid, int) or pid != int(match.group(1))
                or not isinstance(record['state'], str) or record['state'] not in SUMMARIES):
            return None
        descriptor = os.open(directory / (LOCK % pid), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return record                      # the window holds it exclusively: it is running
        finally:
            os.close(descriptor)               # (also drops a shared lock we just got: nobody holds it)
        return None
    except Exception:
        return None


def status():
    """The newest running window's report, with a plain-language `summary` (always the same keys)."""
    directory = _directory()
    found = []
    try:
        paths = sorted(directory.glob('scene-loops-*.json')) if directory and directory.is_dir() else []
    except OSError:
        paths = []
    for path in paths:
        record = _live(directory, path)
        if record is not None:
            try:
                found.append((path.stat().st_mtime, record))
            except OSError:
                continue
    found.sort(key=lambda item: item[0])
    record = found[-1][1] if found else {'state': 'unreported'}
    def count(key):
        value = record.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    shed, resumed = count('shed'), count('resumed')
    refs = count('refs_observed')
    gtk = record.get('gtk') if isinstance(record.get('gtk'), str) else None
    finalized = record.get('sink_finalized') if isinstance(record.get('sink_finalized'), bool) else None
    why = ('the video sink still held by something else'
           if finalized is False else '%s GL context references where 2 were expected' % (refs if refs is not None else 'other'))
    summary = SUMMARIES[record['state']] % {'gtk': gtk or 'this version', 'why': why}
    if shed is not None:
        summary += ' %d shed and %d resumed in this window.' % (shed, resumed or 0)
    return {'state': record['state'], 'summary': summary, 'gtk': gtk, 'refs_observed': refs, 'sink_finalized': finalized,
            'shed': shed, 'resumed': resumed, 'windows': len(found)}
