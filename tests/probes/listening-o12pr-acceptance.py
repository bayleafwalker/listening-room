"""O12-PRESENTER acceptance: a calm, optional presenter between station tracks.

Written independently from the O12 brief and P1 programme plan, not an implementation.  It runs real GTK on a
private Xvfb/D-Bus through o6_env, the real controller on its session test double, fixture providers, and loopback
OpenAI-compatible chat/speech fakes.  It observes only listener-facing surfaces: station-detail and Mix labels,
listening-roomctl inspect/status, and MPRIS.  No real endpoint, desktop session, account, or audio is used.

Readings for coordinator ruling:
* "every few tracks at most" is tested as no more than one played segment across the fixture's first two music items;
  the exact adjustable numeric budget and history-window size remain the implementer's choice.  A 4 s preparation
  limit and a 6 s observation limit are test limits, not a product latency promise.
* "ready before its slot" means an unavailable, timed-out, malformed, or failed synthesis result is recorded as a
  dropped outcome with a reason, produces neither an audible/gap state nor an error card, and music keeps its clock.
* The contract does not name actions for test setup.  station.save/start are existing public controller actions used
  only to seed and tune the fixture; presenter changes are made through the labelled GTK controls.  A presenter
  switch is expected to persist as station preference/reference data, never as audio, a prepared stream, or a token.
* A primary source is represented by a saved station marked ``speech: true`` (ruling 8).  The no-metadata fixture
  is a saved station whose reference id differs from the others.  These source declarations are deliberately
  public data rather than a presenter implementation hook.
* Rulings 16-20, test setup in this probe's own process: o6_env's ``jellyfin_queue`` patch returns the same two
  tracks for every reference.  The probe replaces it with a reference-aware queue of eight tracks, each recorded
  through ``metadata_store.observe`` exactly as o6_env records the first two, and, for the no-metadata reference
  only, three items that have no captured record (the probe asserts that the music identity is one of them).
  Each scenario that crosses track ends saves and tunes its own station, so the station engine's consumed-track
  exclusion cannot run a station out of queue.  ``tune`` waits until that station is active, with its transition
  clear and music playing.  The queue is shuffled, so the positive control accepts the captured record of
  whichever recorded fixture track played (ruling 17) and requires the source to name a library tag field
  (ruling 5-R1) and the speech request to ask for ``wav`` (ruling 4).
* Outcome baseline: the snapshot's ``presenter.segments`` is not reset when the station changes, and a station
  change can withdraw the previous station's prepared record.  ``tune`` holds the fakes' requests until it has
  measured the common prefix of the lists before and after the station change; everything after that prefix is
  this scenario's.  Clip sources are unique to the clip scenario and are read from the whole list.
* Ruling 18, observing a slot: after a verified end the probe waits for the speech layer to play (or for the slot's
  record), never for the next track.  Checks that expect no speech wait for the next track instead.  P2b, P10 and
  P11 use a six-second fake segment so that muting, pausing and leaving fall inside the slot.  The slot's length
  is read from the segment record's public ``duration`` and compared with the time the music transition takes.
* Ruling 19, the Mix: a listener reaches the Mix with Open the room, then the labelled Mix button.  The Presenter
  row is the smallest ancestor of its mapped title that holds a mapped level and a Mute control and neither of
  the other rows' titles.  A label match on the window object never counts.
* A segment is listed as ``played`` only once its slot has played (P1f).  P1e-R2 separately requires that tuning
  alone never sounds the speech layer.
* P2b/P5b need the existing inspect music-layer identity and monotonic playback position as public observations.
  The probe accepts ``item``/``id``/``name`` and ``position``/``elapsed``/``clock`` spellings, but needs one of each.
* Narrow clip reading for coordinator ruling: a fixture registers clips as ``preferences.presenter_clips`` entries
  (``source``, ``audio``, ``allowed_placement``, ``expires_at``). ``audio`` is a short local WAV written under the
  isolated test directory. The corresponding public segment record exposes ``source`` and ``placement``. If clips
  instead have a labelled public editor, this setup should migrate to it.
* Maximum length reading: a result over the budget maximum may be dropped with a reason or recorded as played only
  when its public duration is no greater than that maximum. The fake supplies a valid WAV longer than the fixture
  budget, and this check reads the record's public ``duration``, never wall-clock time.
* Slot timing reading for coordinator ruling 9: tuning starts music but is not a slot.  Slots count verified
  ends since tuning (ruling 20), so a station's second slot follows ``every_tracks`` more ends than its first.  ``track_end`` below uses the
  session double's verified EOF signal (the same ``backend.media = {'eof_reached': True}`` mechanism as
  ``test_station_eof_advances_only_on_verified_eof_and_returns_history``), then waits for the queue to advance.
  The first eligible slot follows that end.  Later slots follow ``budget.every_tracks`` verified ends; P8 reads that
  public minimum and drives two slots on the same station, rather than mistaking two tunes for two slots.
* Repeat, primary-speech, no-metadata, and clip fixtures opt in through the public ``station.save`` top-level
  ``presenter`` object.  P6/P7 additionally read back the active station's enabled state while observing.
* Before an eligible end, every positive and failure case waits up to four seconds for ``presenter.ready`` or a
  preparation-time drop.  Only P5b deliberately crosses its boundary while preparation is still slow.
* P3b constructs ``session_service.Mpris`` on the probe's private D-Bus before querying it, matching a real
  MPRIS consumer without relying on a harness-owned player service.
* Ruling 11's room transport checks use the public ``listening-roomctl pause``, ``play``, and ``leave`` surfaces.
  A speech-layer slot must remain paused for a measurable interval, resume alongside music, and delay the music
  transition until its remaining segment duration is spent (not sooner than the duration minus the time it had
  already sounded, and not hung).

Run: scripts/listening-dev.sh acceptance OUTDIR o12pr
"""
import copy
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import wave
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import o6_env
import online_services

env = o6_env.Env(__doc__, '/tmp/listening-o12pr-acceptance', extra_args={'--repo': {'default': ''}})
env.__enter__()
REPO = Path(env.args.repo or Path(__file__).resolve().parents[2])
checks = []


def check(name, ok, detail=''):
    checks.append({'criterion': name, 'pass': bool(ok)})
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)[:600]), flush=True)


def superseded(original, revision, ok, detail=''):
    """Keep R0 observable without letting its weaker oracle decide the verdict."""
    print('SUPERSEDED %s %s [ORIGINAL, superseded by %s]%s' %
          ('PASS' if ok else 'FAIL', original, revision, '' if ok else '  ' + str(detail)[:300]), flush=True)


def finish():
    out = env.output
    out.mkdir(parents=True, exist_ok=True)
    (out / 'o12pr-acceptance.json').write_text(json.dumps({'results': checks}, indent=1))
    try:
        env.__exit__()
    finally:
        failed = sum(not c['pass'] for c in checks)
        print('%d passed, %d failed' % (len(checks) - failed, failed), flush=True)


def wait(predicate, timeout=6.):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        env.pump(.1)
    return bool(predicate())


def snapshot():
    env.controller.refresh()
    return env.service.execute('inspect', {})


def station(name):
    return next((item for item in env.controller.stations() if item.get('name') == name), None)


RECORDED = ['track-%d' % n for n in range(1, 9)]
UNRECORDED_ID = 'track-without-captured-provenance'


def install_fixture_queue():
    """Test setup in this probe's own process (see the docstring's ruling 17 reading).

    The shared environment returns the same two captured tracks for every reference.  This replaces that patch
    for the probe so that (a) a station has enough tracks for the boundaries its scenario crosses, each recorded
    through the normal metadata-store pipeline exactly as o6_env records track-1 and track-2, and (b) a reference
    saved with ``UNRECORDED_ID`` really plays items that have no captured record.
    """
    extra = [{'provider': 'jellyfin', 'id': 'track-%d' % n, 'type': 'Audio', 'name': 'Fixture piece %d' % n,
              'artist': 'Someone', 'album': 'Neoclassical collection'} for n in range(3, 9)]
    for track in extra:
        env.controller.metadata_store.observe('jellyfin:fixture', track['id'], hashlib.sha256(track['id'].encode()).hexdigest())
    bare = [{'provider': 'jellyfin', 'id': 'unrecorded-%d' % n, 'type': 'Audio', 'name': 'Unrecorded piece %d' % n,
             'artist': 'Nobody', 'album': 'No record'} for n in range(1, 4)]

    def queue(item, cancel_event=None):
        if (item or {}).get('id') == UNRECORDED_ID:
            return copy.deepcopy(bare)
        return copy.deepcopy(o6_env.TRACKS + extra)
    patcher = patch.object(online_services, 'jellyfin_queue', side_effect=queue)
    patcher.start()
    return patcher


def save(name, *, speech=False, metadata=True, clips=None, presenter=False):
    definition = copy.deepcopy(o6_env.RADIO_STATION)
    definition.update(name=name, preferences={})
    # The fixture provenance is created by o6_env through the normal metadata-store pipeline.
    # These are station references, never a presenter-private input or prepared audio.
    track = copy.deepcopy(o6_env.TRACKS[0])
    if not metadata:
        track['id'] = UNRECORDED_ID
        track['name'] = 'No provenance fixture'
    definition.update(sources=[{'source_id': 'jellyfin:fixture', 'reference': track}],
                      generation={'kind': 'collection'})
    if speech:
        # Reading: this is the saved, public declaration that identifies a primary speech station.
        definition['speech'] = True
    if clips is not None:
        definition['preferences']['presenter_clips'] = clips
    if presenter:
        definition['presenter'] = {'enabled': True}
    env.service.execute('station.save', {'station': definition})
    found = station(name)
    assert found, 'fixture station was not saved'
    return found


BASE = [0]
# Endpoint requests wait here while closed.  tune() closes it so that the outcomes listed before this station
# can prepare anything are counted exactly (the snapshot's list is not reset per station), then opens it.
GATE = threading.Event()
GATE.set()


def tune(item):
    """Start the saved station and wait until it, and no other, is active with its transition cleared."""
    GATE.clear()
    listed = [json.dumps(s, sort_keys=True) for s in segment_list()]
    env.service.execute('station.start', {'id': item['id']})

    def active():
        data = snapshot()
        station_now = (((data.get('active') or {}).get('music') or {}).get('station') or {})
        music = (data.get('layers') or {}).get('music') or {}
        return (station_now.get('id') == item['id'] and not data.get('transition')
                and music.get('status') == 'Playing' and music_marker(data)[1] is not None)
    try:
        found = wait(active, 8.)
        # Outcomes that were listed before this station, and survived the station change.  A prepared segment of
        # the previous station may be withdrawn, so the common prefix, not the earlier length, is the baseline.
        # Requests are held, so a segment that needs the endpoints cannot be listed yet; a preparation-time drop
        # or clip that needs none may already be, and counts as this station's.
        now = [json.dumps(s, sort_keys=True) for s in segment_list()]
        BASE[0] = next((i for i, (a, b) in enumerate(zip(listed, now)) if a != b), min(len(listed), len(now)))
    finally:
        GATE.set()
    return found


def verified_end():
    """Deliver one public, verified end-of-file to the session double; returns the monotonic time of the end."""
    env.backend.media = {'eof_reached': True}
    env.controller.refresh()
    env.controller.worker.submit(lambda: None).result(timeout=3)
    return time.monotonic()


def track_end(timeout=8.):
    """A verified end, then wait for the queue to advance.  For ends where no segment is expected to play:
    under rulings 9 and 11 a played segment holds the music transition until the slot is spent (ruling 18)."""
    _music, before, _clock = music_marker()
    verified_end()
    return wait(lambda: music_marker()[1] != before and music_marker()[1] is not None, timeout)


def slot_record(before, timeout=6.):
    """After a verified end, wait for the slot's record rather than for the next track (ruling 18)."""
    return wait(lambda: len(segment_list()) > before, timeout)


def every_tracks(data=None):
    budget = (data or presenter_data()).get('budget') or {}
    value = budget.get('every_tracks') if isinstance(budget, dict) else None
    return value if isinstance(value, int) and value >= 1 else None


def eligible_slot(count=1):
    """Cross the first or subsequent scheduler boundary via verified track endings."""
    return all(track_end() for _ in range(count))


def widgets(root):
    return list(env.widgets(root))


def label_of(widget):
    direct = getattr(widget, 'get_label', lambda: None)()
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    nested = [w.get_text() for w in widgets(widget) if isinstance(w, env.Gtk.Label) and w.get_text().strip()]
    return ' '.join(nested).strip()


def visible_labels(root):
    return {w.get_text().strip() for w in widgets(root) if isinstance(w, env.Gtk.Label)
            and w.get_mapped() and w.get_text().strip()}


def labelled(root, words, kinds=()):
    words = words.casefold()
    found = []
    for item in widgets(root):
        if not item.get_mapped() or (kinds and not isinstance(item, kinds)):
            continue
        if words in label_of(item).casefold() or words in (getattr(item, 'get_tooltip_text', lambda: '')() or '').casefold():
            found.append(item)
    return found


def presenter_switch(root):
    # A labelled Presenter section may own the switch rather than the switch having an accessible name.
    for item in labelled(root, 'presenter', (env.Gtk.Switch, env.Gtk.Button)):
        return item
    for label in [w for w in widgets(root) if isinstance(w, env.Gtk.Label) and 'presenter' in w.get_text().casefold()]:
        parent = label
        for _ in range(4):
            parent = parent.get_parent()
            if parent is None:
                break
            found = [w for w in widgets(parent) if isinstance(w, env.Gtk.Switch) and w.get_mapped()]
            if found:
                return found[0]
    return None


def open_detail(item):
    window = env.launch(page='listen')
    window.listen.show_station(item)
    env.pump(.8)
    return window


def set_presenter(window, enabled=True):
    switch = presenter_switch(window)
    if switch is None:
        return False
    if isinstance(switch, env.Gtk.Switch):
        switch.set_active(enabled)
    else:
        switch.emit('clicked')
    env.settle(.7)
    return True


class FakeEndpoints:
    """Loopback-only OpenAI-shaped fakes.  Modes are chosen per adversarial section."""
    def __init__(self):
        self.mode = 'good'
        self.requests = []
        self.good_wav = wav_bytes(1)
        # Deliberately exceed any normal presenter maximum without using a synthetic metadata format.
        self.overlong_wav = wav_bytes(600)
        # Long enough to pause, hold and resume inside, and within the default budget maximum.
        self.long_wav = wav_bytes(6)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                size = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(size)
                GATE.wait(8.)
                outer.requests.append((self.path, body))
                if outer.mode == 'timeout' and self.path.endswith('/chat/completions'):
                    time.sleep(5.)
                    return
                if outer.mode in ('slow-voice', 'overlong-voice') and self.path.endswith('/audio/speech'):
                    time.sleep(5. if outer.mode == 'slow-voice' else .05)
                if outer.mode == 'missing-voice' and self.path.endswith('/audio/speech'):
                    self.send_error(404, 'voice model absent')
                    return
                if outer.mode == 'voice-fails' and self.path.endswith('/audio/speech'):
                    self.send_error(503, 'voice unavailable')
                    return
                self.send_response(200)
                if self.path.endswith('/chat/completions'):
                    self.send_header('Content-Type', 'application/json')
                    payload = ('{not json' if outer.mode == 'malformed' else json.dumps(
                        {'choices': [{'message': {'content': 'Nocturne in C by Someone.'}}]})).encode()
                else:
                    self.send_header('Content-Type', 'audio/wav')
                    payload = (outer.overlong_wav if outer.mode == 'overlong-voice'
                               else outer.long_wav if outer.mode == 'long-voice' else outer.good_wav)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers(); self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return 'http://127.0.0.1:%d' % self.server.server_port

    def close(self):
        self.server.shutdown(); self.thread.join(2); self.server.server_close()


def configure(endpoints, voice=True):
    path = Path(os.environ['XDG_CONFIG_HOME']) / 'listening-hub' / 'connections.json'
    path.parent.mkdir(mode=0o700, exist_ok=True)
    values = {'presenter_llm': {'endpoint': endpoints.endpoint}}
    if voice:
        values['presenter_voice'] = {'endpoint': endpoints.endpoint}
    path.write_text(json.dumps(values))
    return path


def wav_bytes(seconds):
    """A valid PCM WAV fixture; its duration is conveyed by the standard WAV header."""
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\0\0' * int(8000 * seconds))
    return output.getvalue()


def write_clip_wav():
    path = Path(os.environ['XDG_RUNTIME_DIR']) / 'o12pr-clip.wav'
    path.write_bytes(wav_bytes(3))
    return path


_socket = {}


def roomctl(*arguments):
    if not _socket:
        import session_service
        path = Path(os.environ['XDG_RUNTIME_DIR']) / 'o12pr-session.sock'
        server, _descriptor = session_service.make_server(path, env.service)
        assert server is not None, 'private controller socket did not bind'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _socket.update(path=path, server=server)
    environment = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')}
    environment.update(LISTENING_SESSION_SOCKET=str(_socket['path']), PYTHONPATH=str(env.source), PYTHONDONTWRITEBYTECODE='1')
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(subprocess.run, [sys.executable, str(env.source / 'roomctl.py')] + list(arguments),
                             capture_output=True, text=True, timeout=10, env=environment)
        while not future.done(): env.pump(.05)
        return future.result()


_mpris = {}


def mpris_bus():
    if not _mpris:
        import session_service
        from gi.repository import Gio
        _mpris['service'] = session_service.Mpris(env.service)
        _mpris['module'] = session_service
        _mpris['pool'] = concurrent.futures.ThreadPoolExecutor(1)

        def connect():
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            return Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        future = _mpris['pool'].submit(connect)
        while not future.done():
            env.pump(.03)
        _mpris['connection'] = future.result()
    return _mpris


def _mpris_playback_status(bus):
    """One D-Bus Get, made on the pool thread while the main loop is pumped.

    The Mpris service answers on this process's GLib main loop, so a call_sync from the main thread
    would block the loop that must answer it and time out (the coordinator's merged run 2 saw None).
    """
    from gi.repository import Gio, GLib
    future = bus['pool'].submit(lambda: bus['connection'].call_sync(bus['module'].MPRIS_NAME, bus['module'].MPRIS_PATH,
        'org.freedesktop.DBus.Properties', 'Get', GLib.Variant('(ss)', (bus['module'].PLAYER_INTERFACE, 'PlaybackStatus')),
        GLib.VariantType.new('(v)'), Gio.DBusCallFlags.NONE, 3000, None))
    while not future.done():
        env.pump(.03)
    try:
        return future.result().unpack()[0]
    except GLib.Error:
        return None


def mpris_status():
    bus = mpris_bus()
    # Wait for the name to appear on the private bus, as the O10 probe does, before reading the status.
    found = []

    def answered():
        status = _mpris_playback_status(bus)
        if status is not None:
            found.append(status)
        return status is not None
    wait(answered, 6)
    return found[-1] if found else None


def presenter_data():
    return snapshot().get('presenter') or {}


def segment_list(data=None):
    return [s for s in (data or presenter_data()).get('segments', []) if isinstance(s, dict)]


def prepared_or_dropped(before):
    """Wait for readiness or its one preparation-time outcome before an eligible end."""
    return wait(lambda: bool(presenter_data().get('ready')) or any(
        s.get('outcome') == 'dropped' and s.get('reason') for s in segment_list()[before:]), 4.)


def music_marker(data=None):
    music = ((data or snapshot()).get('layers') or {}).get('music') or {}
    identity = next((music.get(key) for key in ('item', 'id', 'name', 'source') if music.get(key) is not None), None)
    clock = next((music.get(key) for key in ('position', 'elapsed', 'clock') if isinstance(music.get(key), (int, float))), None)
    return music, identity, clock


def source_names_metadata(segment):
    """Ruling 17: the queue is shuffled, so accept the captured record of whichever recorded fixture track played."""
    text = json.dumps(segment.get('sources') or []).casefold()
    names_record = any('jellyfin:fixture:%s' % track in text for track in RECORDED)
    return names_record and 'unrecorded' not in text and any(field in text for field in ('title', 'artist', 'album'))


def speech_layer(data=None):
    layers = (data or snapshot()).get('layers') or {}
    return layers.get('speech') or layers.get('presenter') or {}


def speech_is(status, data=None):
    return speech_layer(data).get('status') == status


def no_error_card(window):
    """No error card is visible, on a window that is open and showing something (not a closed or empty one)."""
    labels = visible_labels(window)
    return (window.get_mapped() and bool(labels) and not (snapshot().get('errors') or {})
            and not any('error' in label.casefold() or 'failed' in label.casefold() for label in labels))


def open_mix(window):
    """Open the Mix the way a listener does: Open the room from Listen, then the room's labelled Mix button."""
    def mix_buttons():
        return [b for b in widgets(window) if isinstance(b, env.Gtk.Button) and b.get_mapped()
                and ((b.get_tooltip_text() or '') == 'Mix' or label_of(b).split(None, 1)[:1] == ['Mix'])]
    if not mix_buttons():
        for button in labelled(window, 'Open the room', (env.Gtk.Button,)):
            button.emit('clicked')
            env.settle(1.)
            break
    for button in mix_buttons():
        button.emit('clicked')
        env.settle(.8)
        return True
    return False


def presenter_row(window):
    """The Presenter row of the open Mix: the smallest ancestor of its mapped title holding a mapped level and a
    Mute control, and none of the other rows' titles.  A label match on the window itself never counts."""
    Gtk = env.Gtk
    titles = [w for w in widgets(window) if isinstance(w, Gtk.Label) and w.get_mapped() and w.get_text().strip() == 'Presenter']
    for title in titles:
        node = title.get_parent()
        while node is not None and node is not window:
            texts = visible_labels(node)
            if 'Music' in texts or 'Ambience' in texts:
                break
            scales = [w for w in widgets(node) if isinstance(w, Gtk.Scale) and w.get_mapped()]
            mutes = [w for w in labelled(node, 'Mute', (Gtk.Button, Gtk.Switch)) if w is not node]
            if scales and mutes:
                return {'root': node, 'level': scales[0], 'mute': mutes[0]}
            node = node.get_parent()
    return None


def set_muted(control, muted=True):
    if isinstance(control, (env.Gtk.ToggleButton, env.Gtk.Switch)):
        control.set_active(muted)
    else:
        control.emit('clicked')
    env.settle(.7)


def budget_sparser(before, after):
    """A lower maximum or fewer eligible slots is the only accepted meaning of Less talking."""
    def numbers(value, prefix=''):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from numbers(child, prefix + '.' + str(key).casefold())
        elif isinstance(value, (int, float)):
            yield prefix, value
    old, new = dict(numbers(before)), dict(numbers(after))
    changes = []
    for key in old.keys() & new.keys():
        if any(word in key for word in ('every', 'interval', 'tracks_between', 'frequency')):
            changes.append(new[key] > old[key])
        if any(word in key for word in ('max', 'length', 'seconds', 'duration')):
            changes.append(new[key] < old[key])
    return any(changes)


def contains_forbidden_saved_data(value, forbidden):
    if isinstance(value, dict):
        return any(str(key).casefold().replace('-', '_') in forbidden or contains_forbidden_saved_data(child, forbidden)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(contains_forbidden_saved_data(child, forbidden) for child in value)
    return False


def run():
    fake = FakeEndpoints()
    queue_patch = install_fixture_queue()
    try:
        configure(fake)
        legacy = save('Existing silent station')
        room = open_detail(legacy)
        initial = presenter_switch(room)
        feature_present = initial is not None
        stored = station(legacy['name']) or {}
        check('P0 existing saved stations are Presenter-off by default and their detail visibly says Presenter',
              initial is not None and not getattr(initial, 'get_active', lambda: False)(), visible_labels(room))
        check('P0b an existing room stays silent between tracks: no presenter object is enabled before opt-in',
              not bool((snapshot().get('presenter') or {}).get('enabled')), snapshot().get('presenter'))
        forbidden = {'audio', 'prepared_stream', 'stream', 'token', 'access_token'}
        check('P0c an untouched saved station contains references/preferences only, never audio, prepared stream or token',
              not contains_forbidden_saved_data(stored, forbidden), stored)
        room.close()

        # On the base branch the feature is deliberately absent.  Record every contract check as a clear
        # feature-absence failure and finish promptly; otherwise a pre-feature run would spend its time waiting
        # for endpoints which no code can call.  The complete adversarial path below runs once Presenter exists.
        if not feature_present:
            for original, revision in (
                ('P1b Less talking is station feedback: it lowers/persists the budget without changing station', 'P1b-R1'),
                ('P1b-R1 Less talking leaves Presenter enabled and persists a strictly sparser budget', 'P1b-R2'),
                ('P2 the Mix exposes a Presenter row with its own level and Mute', 'P2-R1'),
                ('P2b-R1 muting Presenter during a segment never advances, pauses, or delays music', 'P2b-R2'),
                ('P4-R1 endpoint failure drops the slot quietly, records a reason, and leaves music playing without an error card', 'P4-R2'),
                ('P5-R1 prepared eligible segments use loopback LLM and speech connections and retain text plus captured sources', 'P5-R2'),
                ('P5-R2 the captured-metadata positive control plays one sourced fake segment', 'P5-R3'),
                ('P5b-R1 a segment not ready at its slot is dropped rather than creating a gap', 'P5b-R2'),
                ('P6-R1 a primary speech station suppresses presenter segments while it plays', 'P6-R2'),
                ('P7-R1 an item with no metadata gets no factual presenter segment', 'P7-R2'),
                ('P8-R2 identical text in two eligible slots plays exactly once within the history window', 'P8-R3'),
                ('P1d-R1 an over-budget fake segment is dropped or publicly trimmed to the budget maximum', 'P1d-R2'),
                ('P9-R1 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry', 'P9-R2')):
                superseded(original, revision, False, 'Presenter is absent from the station detail on this branch')
            for name in [
                'P1 station detail exposes a Presenter switch and a visible adjustable budget',
                'P1e-R1 tuning starts music at once but does not create an opening presenter slot',
                'P1b-R2 Less talking leaves Presenter enabled and persists a strictly sparser saved-station budget',
                'P1c Music only is station feedback: it turns Presenter off without changing station',
                'P2-R1 the open Mix exposes a Presenter row with its own mapped level and Mute',
                'P2b-R3 muting Presenter in the open Mix leaves the music state alone and the slot as long as its segment',
                'P3 listening-roomctl inspect reports presenter enabled, budget, quiet status and recent sourced outcomes',
                'P3b-R1 MPRIS remains music playback while an optional presenter layer is enabled on the private bus',
                'P4-R3 LLM timeout drops quietly with no window error card',
                'P4-R3 malformed LLM output drops quietly with no window error card',
                'P4-R3 missing speech endpoint drops quietly with no window error card',
                'P4-R3 failing speech endpoint drops quietly with no window error card',
                'P5-R4 the captured-metadata positive control cites a captured record of the played fixture and a library tag field',
                'P5b-R3 a slow slot drops, with the music moving on at once and no played segment for that slot',
                'P6-R3 primary speech suppresses slots after the positive control proved a slot can play',
                'P7-R3 no captured provenance suppresses factual segments after the positive control',
                'P8-R4 identical text in two eligible slots plays exactly once within the history window',
                'P1d-R3 an over-budget fake segment is dropped or publicly trimmed to the budget maximum',
                'P9-R3 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry',
                'P10-R1 room Pause and Play pause and resume a presenter slot with its remaining duration',
                'P11-R1 Leave during a presenter slot stops speech immediately and discards prepared work',
            ]:
                check(name, False, 'Presenter is absent from the station detail on this branch')
            return

        item = save('Presenter fixture', metadata=True)
        window = open_detail(item)
        enabled = set_presenter(window)
        labels = visible_labels(window)
        check('P1 station detail exposes a Presenter switch and a visible adjustable budget',
              enabled and 'Presenter' in labels and bool(labelled(window, 'talk') or labelled(window, 'budget')), labels)
        before_id = item['id']
        before_budget = ((station(item['name']) or {}).get('presenter') or {}).get('budget')
        less = labelled(window, 'Less talking', (env.Gtk.Button,))
        if less: less[0].emit('clicked'); env.settle(.6)
        after_less = station(item['name']) or {}
        after_budget = (after_less.get('presenter') or {}).get('budget')
        after_less_enabled = bool((after_less.get('presenter') or {}).get('enabled'))
        music_only = labelled(window, 'Music only', (env.Gtk.Button,))
        if music_only: music_only[0].emit('clicked'); env.settle(.6)
        after_music = station(item['name']) or {}
        superseded('P1b Less talking is station feedback: it lowers/persists the budget without changing station', 'P1b-R1',
                    bool(less) and after_less.get('id') == before_id and after_less != item, after_less)
        superseded('P1b-R1 Less talking leaves Presenter enabled and persists a strictly sparser budget', 'P1b-R2',
                    bool(less) and after_less.get('id') == before_id and after_less_enabled
                    and budget_sparser(before_budget, after_budget), (before_budget, after_budget, after_less))
        check('P1b-R2 Less talking leaves Presenter enabled and persists a strictly sparser saved-station budget',
              bool(less) and after_less.get('id') == before_id and after_less_enabled
              and budget_sparser(before_budget, after_budget), (before_budget, after_budget, after_less))
        check('P1c Music only is station feedback: it turns Presenter off without changing station',
              bool(music_only) and after_music.get('id') == before_id
              and not bool((after_music.get('presenter') or {}).get('enabled')), after_music)
        window.close()

        # Turn it back on and tune it through the established public station surface.
        window = open_detail(item); set_presenter(window, True); tuned = tune(station(item['name']))
        tune_segments = len(segment_list())
        tune_music, _tune_item, _tune_clock = music_marker()
        env.pump(.5)
        superseded('P1e-R1 tuning starts music at once but does not create an opening presenter slot', 'P1e-R2',
                   tuned and tune_music.get('status') == 'Playing' and len(segment_list()) == tune_segments,
                   (tune_music, segment_list()))
        # Preparation may finish at once, so give it its 4 s and then look.  With no verified end there has been
        # no slot: the speech layer must never sound, and no outcome may say that a segment played.
        sounded = []
        wait(lambda: sounded.append(speech_is('Playing')) or bool(presenter_data().get('ready'))
             or len(segment_list()) > BASE[0], 4.)
        env.pump(.5); sounded.append(speech_is('Playing'))
        tune_after = segment_list()[BASE[0]:]
        check('P1e-R2 tuning starts music at once and creates no opening slot: the speech layer never sounds',
              tuned and music_marker()[0].get('status') == 'Playing' and not any(sounded) and not speech_is('Playing'),
              (tune_music, tune_after, speech_layer()))
        check('P1f a prepared segment is not reported as played before its slot',
              tuned and not any(s.get('outcome') == 'played' for s in tune_after), (tune_after, speech_layer()))
        mix_open = open_mix(window)
        row = presenter_row(window) if mix_open else None
        superseded('P2 the Mix exposes a Presenter row with its own level and Mute', 'P2-R1',
                   tuned and ('Presenter' in visible_labels(window) or bool(labelled(window, 'Presenter')))
                   and bool(labelled(window, 'Mute')) and any(isinstance(w, env.Gtk.Scale) and w.get_mapped() for w in widgets(window)),
                   visible_labels(window))
        check('P2-R1 the open Mix exposes a Presenter row with its own mapped level and Mute',
              tuned and mix_open and row is not None, (mix_open, sorted(visible_labels(window))))
        window.close()

        # A slot's speech is audible for the segment's duration whether or not it is muted (ruling 9), so muting
        # is observed against the music's identity and state around the slot and the time its transition is due.
        fake.mode = 'long-voice'; fake.requests.clear()
        mute_station = save('Mix mute fixture', metadata=True, presenter=True)
        window = env.launch(page='listen'); tuned_mute = tune(mute_station)
        mix_open = open_mix(window); row = presenter_row(window) if mix_open else None
        before = BASE[0]; prepared = prepared_or_dropped(before)
        _m, slot_item, _c = music_marker()
        slot_end = verified_end()
        playing = prepared and wait(lambda: speech_is('Playing'), 4.)
        before_state, before_item, before_clock = music_marker()
        if row is not None:
            set_muted(row['mute'], True)
        after_state, after_item, after_clock = music_marker()
        toggle_held = row is not None and bool(getattr(row['mute'], 'get_active', lambda: False)())
        advanced = wait(lambda: music_marker()[1] not in (None, slot_item), 14.); advanced_at = time.monotonic()
        mute_segments = [s for s in segment_list()[before:] if s.get('outcome') == 'played']
        slot_seconds = next((s.get('duration') for s in mute_segments if isinstance(s.get('duration'), (int, float))), None)
        resumed_state, resumed_item, resumed_clock = music_marker(); env.pump(.6); later_clock = music_marker()[2]
        superseded('P2b-R2 muting Presenter while it is playing keeps music identity and advancing clock', 'P2b-R3',
                   bool(playing) and before_state.get('status') == after_state.get('status') == 'Playing'
                   and before_item == after_item and before_item is not None and isinstance(before_clock, (int, float))
                   and isinstance(after_clock, (int, float)) and .15 <= after_clock - before_clock <= 2.,
                   (before_state, after_state))
        check('P2b-R3 muting Presenter in the open Mix leaves the music state alone and the slot as long as its segment',
              row is not None and bool(playing) and toggle_held
              and before_state.get('status') == after_state.get('status') and before_item == after_item == slot_item
              and slot_seconds is not None and advanced and slot_seconds - 1.5 <= advanced_at - slot_end <= slot_seconds + 4.
              and resumed_state.get('status') == 'Playing' and isinstance(resumed_clock, (int, float))
              and isinstance(later_clock, (int, float)) and later_clock > resumed_clock,
              (row is not None, playing, toggle_held, before_state, after_state, slot_seconds, advanced_at - slot_end,
               resumed_state, mute_segments))
        if row is not None:
            set_muted(row['mute'], False)   # the Mix level and mute are shared by every station; leave it as found
        window.close()

        inspected = roomctl('inspect')
        try: inspected_json = json.loads(inspected.stdout)
        except ValueError: inspected_json = {}
        presenter = inspected_json.get('presenter') or snapshot().get('presenter') or {}
        check('P3 listening-roomctl inspect reports presenter enabled, budget, quiet status and recent sourced outcomes',
              {'enabled', 'budget', 'segments'} <= set(presenter) and 'status' in presenter, presenter)
        mpris_playback = mpris_status()
        superseded('P3b MPRIS remains music playback while an optional presenter layer is enabled', 'P3b-R1',
                    mpris_playback in ('Playing', 'Paused'), mpris_playback)
        check('P3b-R1 MPRIS remains music playback while an optional presenter layer is enabled on the private bus',
              mpris_playback in ('Playing', 'Paused'), mpris_playback)

        # Ruling 17: every scenario that crosses track ends has its own saved station.
        def scenario(name, mode, metadata=True, **options):
            fake.mode = mode; fake.requests.clear()
            saved = save(name, metadata=metadata, presenter=True, **options)
            win = env.launch(page='listen')
            return saved, win, tune(saved)

        for mode, wording in [('timeout', 'LLM timeout'), ('malformed', 'malformed LLM output'),
                              ('missing-voice', 'missing speech endpoint'), ('voice-fails', 'failing speech endpoint')]:
            failing, failing_window, tuned_failing = scenario('Failure fixture ' + wording, mode)
            before = BASE[0]; prepared = prepared_or_dropped(before); boundary = prepared and track_end()
            wait(lambda: len(segment_list()) > before, 6)
            p = snapshot().get('presenter') or {}; segments = (p.get('segments') or [])[before:]
            music = (snapshot().get('layers') or {}).get('music') or {}
            superseded('P4-R2 %s drops quietly with no window error card' % wording, 'P4-R3',
                       boundary and music.get('status') == 'Playing' and bool(p.get('status')) and no_error_card(failing_window)
                       and any(s.get('outcome') == 'dropped' and s.get('reason') for s in segments), p)
            check('P4-R3 %s drops quietly with no window error card' % wording,
                  tuned_failing and boundary and music.get('status') == 'Playing' and bool(p.get('status'))
                  and no_error_card(failing_window)
                  and any(s.get('outcome') == 'dropped' and s.get('reason') for s in segments)
                  and not any(s.get('outcome') == 'played' for s in segments), p)
            failing_window.close()

        positive_station, positive_window, tuned_positive = scenario('Positive control fixture', 'long-voice')
        before = BASE[0]; prepared = prepared_or_dropped(before)
        verified_end(); boundary = prepared and slot_record(before)
        audible = boundary and wait(lambda: speech_is('Playing'), 4.)
        p = snapshot().get('presenter') or {}; segments = (p.get('segments') or [])[before:]
        positive = [s for s in segments if isinstance(s, dict) and s.get('outcome') == 'played'
                    and s.get('text') == 'Nocturne in C by Someone.' and source_names_metadata(s)]
        speech_bodies = [body for path, body in fake.requests if path.endswith('/v1/audio/speech')]
        wav_requested = bool(speech_bodies) and all(b'"wav"' in body for body in speech_bodies)
        superseded('P5-R1 prepared eligible segments use loopback LLM and speech connections and retain text plus captured sources', 'P5-R2',
              any(path.endswith('/v1/chat/completions') for path, _ in fake.requests) and any(path.endswith('/v1/audio/speech') for path, _ in fake.requests)
              and all({'text', 'sources', 'outcome'} <= set(s) for s in segments if isinstance(s, dict)), (fake.requests, p))
        superseded('P5-R2 the captured-metadata positive control plays one sourced fake segment', 'P5-R3',
                    boundary and bool(positive), (fake.requests, p))
        superseded('P5-R3 the captured-metadata positive control cites the captured record and a library tag field', 'P5-R4',
              boundary and bool(positive) and any(path.endswith('/v1/chat/completions') for path, _ in fake.requests)
              and any(path.endswith('/v1/audio/speech') for path, _ in fake.requests), (fake.requests, p))
        check('P5-R4 the captured-metadata positive control cites a captured record of the played fixture and a library tag field',
              tuned_positive and boundary and audible and len(positive) == 1 and any(path.endswith('/v1/chat/completions') for path, _ in fake.requests)
              and wav_requested, (fake.requests, p))
        positive_window.close()

        # O11's public room transport controls the audible speech layer too (ruling 11).  The slot is observed
        # after the verified end while its speech plays, not after the next track starts (ruling 18).
        def audible_slot(name):
            saved, win, tuned_now = scenario(name, 'long-voice')
            start = BASE[0]; prepared_now = prepared_or_dropped(start)
            _m, ending_item, _c = music_marker()
            ended = verified_end()
            speaking_now = prepared_now and wait(lambda: speech_is('Playing'), 4.)
            return win, start, ending_item, ended, speaking_now

        win, before, slot_item, slot_end, speaking = audible_slot('Room transport fixture')
        playing_state, playing_item, _playing_clock = music_marker()
        pause_at = time.monotonic()
        paused_command = roomctl('pause')
        paused = wait(lambda: speech_is('Paused'), 3.)
        music_paused = (playing_state.get('status') != 'Playing') or music_marker()[0].get('status') == 'Paused'
        paused_clock = music_marker()[2]; env.pump(1.5); held_clock = music_marker()[2]
        held = speech_is('Paused') and music_marker()[1] == slot_item
        played_command = roomctl('play'); resume_at = time.monotonic()
        resumed = wait(lambda: speech_is('Playing'), 3.)
        resumed_music = music_marker()[0]
        advanced = wait(lambda: music_marker()[1] not in (None, slot_item), 14.); advanced_at = time.monotonic()
        played_segment = next((s for s in segment_list()[before:] if s.get('outcome') == 'played'), {})
        seconds = played_segment.get('duration')
        remaining_floor = (seconds - (pause_at - slot_end)) if isinstance(seconds, (int, float)) else None
        superseded('P10 room Pause and Play pause and resume a presenter slot with its remaining duration', 'P10-R1',
                   False, 'the original waited for the next track, after the slot had finished')
        check('P10-R1 room Pause and Play pause and resume a presenter slot with its remaining duration',
              speaking and bool(seconds) and paused_command.returncode == 0 and paused and music_paused and held
              and (paused_clock is None or held_clock is None or abs(held_clock - paused_clock) <= .05)
              and played_command.returncode == 0 and resumed and resumed_music.get('status') == 'Playing'
              and advanced and remaining_floor is not None
              and remaining_floor - .7 <= advanced_at - resume_at <= seconds + 3.,
              (speaking, seconds, paused, music_paused, held, resumed, resumed_music, advanced, advanced_at - resume_at, remaining_floor))
        win.close()

        win, before, slot_item, slot_end, leave_speaking = audible_slot('Leave transport fixture')
        leave_command = roomctl('leave')
        left = wait(lambda: speech_is('Stopped'), 2.)
        env.pump(1.5)
        superseded('P11 Leave during a presenter slot stops speech immediately', 'P11-R1', False,
                   'the original waited for the next track, after the slot had finished')
        check('P11-R1 Leave during a presenter slot stops speech immediately and discards prepared work',
              leave_speaking and leave_command.returncode == 0 and left and speech_is('Stopped')
              and not presenter_data().get('ready'), speech_layer())
        win.close()

        slow_station, slow_window, tuned_slow = scenario('Slow slot fixture', 'slow-voice')
        before = BASE[0]; slow_item = music_marker()[1]
        slow_end = verified_end()
        boundary = wait(lambda: music_marker()[1] not in (None, slow_item), 8.); advanced_in = time.monotonic() - slow_end
        slow_before, slow_item_now, slow_clock = music_marker()
        wait(lambda: len(segment_list()) > before, 6.); env.pump(.6)
        slow_after, slow_item_after, slow_clock_after = music_marker(); slow_slot = segment_list()[before:]
        superseded('P5b-R2 a slow slot drops, with music continuously playing and no played segment for that slot', 'P5b-R3',
                   boundary and slow_after.get('status') == 'Playing' and slow_item_now == slow_item_after and slow_item_now is not None
                   and any(s.get('outcome') == 'dropped' and s.get('reason') for s in slow_slot), slow_slot)
        check('P5b-R3 a slow slot drops, with the music moving on at once and no played segment for that slot',
              tuned_slow and boundary and advanced_in <= 3.5 and slow_after.get('status') == 'Playing'
              and slow_item_now == slow_item_after and slow_item_now is not None
              and isinstance(slow_clock, (int, float)) and isinstance(slow_clock_after, (int, float)) and slow_clock_after > slow_clock
              and any(s.get('outcome') == 'dropped' and s.get('reason') for s in slow_slot)
              and not any(s.get('outcome') == 'played' for s in slow_slot), (advanced_in, slow_slot))
        slow_window.close()

        # Ruling 20 counts verified ends on the active station; the fixture queue has eight tracks, enough for
        # the first slot and the one due after `every_tracks` more ends.  Endpoint request counts are not an
        # oracle, because repeat suppression may happen before speech.
        repeat_station, repeat_window, tuned_repeat = scenario('Repeat fixture', 'long-voice')
        minimum = every_tracks(); before = BASE[0]; prepared = prepared_or_dropped(before)
        repeat_first_item = music_marker()[1]
        verified_end(); first_boundary = prepared and slot_record(before) and wait(lambda: speech_is('Playing'), 4.)
        after_first = len(segment_list())
        second_boundary = False
        if minimum and first_boundary:
            # The first slot holds the music transition until its speech is spent (rulings 9 and 11).
            second_boundary = wait(lambda: music_marker()[1] not in (None, repeat_first_item), 10.)
            for _ in range(minimum - 1):
                second_boundary = second_boundary and track_end()
            if second_boundary:
                prepared_or_dropped(after_first)
                verified_end(); second_boundary = slot_record(after_first)
        repeated = segment_list()[before:]
        played = [s for s in repeated if s.get('outcome') == 'played']
        first_played = next((index for index, segment in enumerate(repeated) if segment.get('outcome') == 'played'), None)
        later_drop = (first_played is not None and any(segment.get('outcome') == 'dropped' and segment.get('reason')
                      for segment in repeated[first_played + 1:]))
        superseded('P8-R3 identical text in two eligible slots plays exactly once within the history window', 'P8-R4',
                    len(played) == 1 and later_drop, repeated)
        check('P8-R4 identical text in two eligible slots plays exactly once within the history window',
              tuned_repeat and first_boundary and second_boundary and len(repeated) >= 2 and len(played) == 1 and later_drop, repeated)
        repeat_window.close()

        speech, speech_window, tuned_speech = scenario('Primary speech fixture', 'good', speech=True)
        before = BASE[0]; prepared = prepared_or_dropped(before); boundary = prepared and track_end(); env.pump(1.)
        speech_p = snapshot().get('presenter') or {}
        speech_segments = segment_list(speech_p)[before:]
        superseded('P6-R2 primary speech suppresses slots after the positive control proved a slot can play', 'P6-R3',
                   boundary and bool(positive) and bool(speech_p.get('enabled')) and not fake.requests
                   and not any(s.get('outcome') == 'played' for s in speech_segments), speech_p)
        check('P6-R3 primary speech suppresses slots after the positive control proved a slot can play',
              tuned_speech and station(speech['name']).get('speech') is True and boundary and len(positive) == 1
              and bool(speech_p.get('enabled')) and not fake.requests
              and not any(s.get('outcome') == 'played' for s in speech_segments), (station(speech['name']), speech_p))
        speech_window.close()

        # The public record is the oracle: an empty captured-source list may never become a played factual claim.
        # The unrecorded reference plays items that have no captured record (the probe's own queue, above).
        bare, bare_window, tuned_bare = scenario('No metadata fixture', 'good', metadata=False)
        playing_unrecorded = str(music_marker()[1] or '').startswith('unrecorded-')
        before = BASE[0]; prepared = prepared_or_dropped(before); boundary = prepared and track_end(); env.pump(1.)
        bare_segments = segment_list()[before:]
        superseded('P7-R2 no captured provenance suppresses factual segments after the positive control', 'P7-R3',
                   boundary and bool(positive) and bool((snapshot().get('presenter') or {}).get('enabled'))
                   and not any(s.get('outcome') == 'played' for s in bare_segments), bare_segments)
        check('P7-R3 no captured provenance suppresses factual segments after the positive control',
              tuned_bare and playing_unrecorded and boundary and len(positive) == 1
              and bool((snapshot().get('presenter') or {}).get('enabled'))
              and not any(s.get('outcome') == 'played' for s in bare_segments), (music_marker()[1], bare_segments))
        bare_window.close()

        over, over_window, tuned_over = scenario('Over budget fixture', 'overlong-voice')
        before = BASE[0]; prepared = prepared_or_dropped(before)
        verified_end(); boundary = prepared and slot_record(before)
        overlong = segment_list()[before:]; budget = presenter_data().get('budget') or {}
        maximum = next((v for key, v in budget.items() if isinstance(v, (int, float)) and 'max' in key.casefold()), None) if isinstance(budget, dict) else None
        superseded('P1d-R2 an over-budget fake segment is dropped or publicly trimmed to the budget maximum', 'P1d-R3',
                   boundary and maximum is not None and (any(s.get('outcome') == 'dropped' and s.get('reason') for s in overlong)
                   or any(s.get('outcome') == 'played' and isinstance(s.get('duration'), (int, float)) and s['duration'] <= maximum for s in overlong)),
                   (budget, overlong))
        check('P1d-R3 an over-budget fake segment is dropped or publicly trimmed to the budget maximum',
              tuned_over and boundary and maximum is not None and len(overlong) == 1
              and (any(s.get('outcome') == 'dropped' and s.get('reason') for s in overlong)
                   or any(s.get('outcome') == 'played' and isinstance(s.get('duration'), (int, float)) and s['duration'] <= maximum for s in overlong)),
              (budget, overlong))
        over_window.close()

        now = time.time()
        clip_audio = str(write_clip_wav())
        clips = [{'source': 'fixture-clip', 'audio': clip_audio, 'allowed_placement': 'between_tracks', 'expires_at': now + 60},
                 {'source': 'expired-fixture-clip', 'audio': clip_audio, 'allowed_placement': 'between_tracks', 'expires_at': now - 1}]
        clip_station = save('Clip fixture', metadata=True, clips=clips, presenter=True); tuned_clip = tune(clip_station)
        before = BASE[0]; prepared = prepared_or_dropped(before)
        verified_end(); boundary = prepared and slot_record(before) and wait(lambda: speech_is('Playing'), 4.)
        clip_segments = segment_list()   # whole list: the clip sources are unique to this scenario
        superseded('P9-R2 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry', 'P9-R3',
                    any(s.get('outcome') == 'played' and s.get('source') == 'fixture-clip' and s.get('placement') == 'between_tracks'
                        for s in clip_segments) and not any(s.get('source') == 'expired-fixture-clip' and s.get('outcome') == 'played'
                        for s in clip_segments), clip_segments)
        check('P9-R3 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry',
              tuned_clip and boundary and any(s.get('outcome') == 'played' and s.get('source') == 'fixture-clip'
                  and s.get('placement') == 'between_tracks' for s in clip_segments)
              and not any(s.get('outcome') == 'played' and s.get('source') == 'expired-fixture-clip' for s in clip_segments), clip_segments)
    finally:
        queue_patch.stop()
        fake.close()


try:
    run()
except Exception as error:
    traceback.print_exc()
    check('O12 probe ran without an unhandled error', False, '%s: %s' % (type(error).__name__, error))
finally:
    finish()
