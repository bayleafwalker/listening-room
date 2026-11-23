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
* A primary source is represented by a saved station marked speech.  "No metadata" removes the fixture item's
  captured provenance before it is tuned.  These source declarations are deliberately public data rather than a
  presenter implementation hook.
* The positive fixture is a saved collection station whose Jellyfin reference resolves to the metadata pipeline's
  captured ``jellyfin:fixture:track-1`` record.  Under ruling 5-R1, its source must name that record and a library
  tag field (title, artist, or album), rather than merely be non-empty.  The no-metadata fixture uses a distinct,
  otherwise identical reference with no captured record.
* P2b/P5b need the existing inspect music-layer identity and monotonic playback position as public observations.
  The probe accepts ``item``/``id``/``name`` and ``position``/``elapsed``/``clock`` spellings, but needs one of each.
* Narrow clip reading for coordinator ruling: a fixture registers clips as ``preferences.presenter_clips`` entries
  (``source``, ``audio``, ``allowed_placement``, ``expires_at``). ``audio`` is a short local WAV written under the
  isolated test directory. The corresponding public segment record exposes ``source`` and ``placement``. If clips
  instead have a labelled public editor, this setup should migrate to it.
* Maximum length reading: a result over the budget maximum may be dropped with a reason or recorded as played only
  when its public duration is no greater than that maximum. The fake supplies a valid WAV longer than the fixture
  budget, and this check reads the record's public ``duration``, never wall-clock time.
* Slot timing reading for coordinator ruling 9: tuning starts music but is not a slot.  ``track_end`` below uses the
  session double's verified EOF signal (the same ``backend.media = {'eof_reached': True}`` mechanism as
  ``test_station_eof_advances_only_on_verified_eof_and_returns_history``), then waits for the queue to advance.
  The first eligible slot follows that end.  Later slots follow ``budget.every_tracks`` verified ends; P8 reads that
  public minimum and drives two slots on the same station, rather than mistaking two tunes for two slots.
* Repeat, primary-speech, no-metadata, and clip fixtures opt in through the public ``station.save``
  ``preferences.presenter.enabled`` preference; this deliberately tests saved station feedback rather than a
  presenter-private hook.  P6/P7 additionally read back the station's enabled state while observing.
* P3b runs MPRIS only inside ``scripts/listening-dev.sh acceptance``: that harness supplies the private D-Bus session.
  A detached invocation may return ``None`` because it lacks that bus, which is an environment limitation rather
  than a product result.
* Ruling 11's room transport checks use the public ``listening-roomctl pause``, ``play``, and ``leave`` surfaces.
  A speech-layer slot must remain paused for a measurable interval, resume alongside music, and delay the music
  transition until its remaining segment duration is spent.

Run: scripts/listening-dev.sh acceptance OUTDIR o12pr
"""
import copy
import concurrent.futures
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import o6_env

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


def save(name, *, speech=False, metadata=True, clips=None, presenter=False):
    definition = copy.deepcopy(o6_env.RADIO_STATION)
    definition.update(name=name, preferences={})
    # The fixture provenance is created by o6_env through the normal metadata-store pipeline.
    # These are station references, never a presenter-private input or prepared audio.
    track = copy.deepcopy(o6_env.TRACKS[0])
    if not metadata:
        track['id'] = 'track-without-captured-provenance'
        track['name'] = 'No provenance fixture'
    definition.update(sources=[{'source_id': 'jellyfin:fixture', 'reference': track}],
                      generation={'kind': 'collection'})
    if speech:
        # Reading: this is the saved, public declaration that identifies a primary speech station.
        definition['speech'] = True
    if clips is not None:
        definition['preferences']['presenter_clips'] = clips
    if presenter:
        definition['preferences']['presenter'] = {'enabled': True}
    env.service.execute('station.save', {'station': definition})
    found = station(name)
    assert found, 'fixture station was not saved'
    return found


def tune(item):
    env.service.execute('station.start', {'id': item['id']})
    return wait(lambda: bool(snapshot().get('station') or env.controller.active), 6.)


def track_end():
    """Cause one public, verified music EOF and wait for the station queue to advance."""
    _music, before, _clock = music_marker()
    env.backend.media = {'eof_reached': True}
    env.controller.refresh()
    env.controller.worker.submit(lambda: None).result(timeout=3)
    return wait(lambda: music_marker()[1] != before and music_marker()[1] is not None, 4.)


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
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                size = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(size)
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
                    payload = outer.overlong_wav if outer.mode == 'overlong-voice' else outer.good_wav
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
    path.write_bytes(wav_bytes(1))
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


def mpris_status():
    import session_service
    from gi.repository import Gio, GLib
    address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
        Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    try:
        reply = connection.call_sync(session_service.MPRIS_NAME, session_service.MPRIS_PATH,
            'org.freedesktop.DBus.Properties', 'Get', GLib.Variant('(ss)', (session_service.PLAYER_INTERFACE, 'PlaybackStatus')),
            GLib.VariantType.new('(v)'), Gio.DBusCallFlags.NONE, 3000, None)
        return reply.unpack()[0]
    except GLib.Error:
        return None


def presenter_data():
    return snapshot().get('presenter') or {}


def segment_list(data=None):
    return [s for s in (data or presenter_data()).get('segments', []) if isinstance(s, dict)]


def music_marker(data=None):
    music = ((data or snapshot()).get('layers') or {}).get('music') or {}
    identity = next((music.get(key) for key in ('item', 'id', 'name', 'source') if music.get(key) is not None), None)
    clock = next((music.get(key) for key in ('position', 'elapsed', 'clock') if isinstance(music.get(key), (int, float))), None)
    return music, identity, clock


def source_names_metadata(segment):
    text = json.dumps(segment.get('sources') or []).casefold()
    names_record = ('jellyfin:fixture:track-1' in text or
                    ('jellyfin:fixture' in text and 'track-1' in text))
    return names_record and any(field in text for field in ('title', 'artist', 'album'))


def speech_layer(data=None):
    layers = (data or snapshot()).get('layers') or {}
    return layers.get('speech') or layers.get('presenter') or {}


def speech_is(status, data=None):
    return speech_layer(data).get('status') == status


def no_error_card(window):
    return not any('error' in label.casefold() or 'failed' in label.casefold() for label in visible_labels(window))


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
                'P1b-R1 Less talking leaves Presenter enabled and persists a strictly sparser budget',
                'P1c Music only is station feedback: it turns Presenter off without changing station',
                'P2 the Mix exposes a Presenter row with its own level and Mute',
                'P2b-R2 muting Presenter while it is playing keeps music identity and advancing clock',
                'P3 listening-roomctl inspect reports presenter enabled, budget, quiet status and recent sourced outcomes',
                'P3b MPRIS remains music playback while an optional presenter layer is enabled',
                'P4-R2 LLM timeout drops quietly with no window error card',
                'P4-R2 malformed LLM output drops quietly with no window error card',
                'P4-R2 missing speech endpoint drops quietly with no window error card',
                'P4-R2 failing speech endpoint drops quietly with no window error card',
                'P5-R2 the captured-metadata positive control plays one sourced fake segment',
                'P5-R3 the captured-metadata positive control cites the captured record and a library tag field',
                'P5b-R2 a slow slot drops, with music continuously playing and no played segment for that slot',
                'P6-R2 primary speech suppresses slots after the positive control proved a slot can play',
                'P7-R2 no captured provenance suppresses factual segments after the positive control',
                'P8-R3 identical text in two eligible slots plays exactly once within the history window',
                'P1d-R2 an over-budget fake segment is dropped or publicly trimmed to the budget maximum',
                'P9-R2 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry',
                'P10 room Pause and Play pause and resume a presenter slot with its remaining duration',
                'P11 Leave during a presenter slot stops speech immediately',
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
        before_budget = (snapshot().get('presenter') or {}).get('budget')
        less = labelled(window, 'Less talking', (env.Gtk.Button,))
        if less: less[0].emit('clicked'); env.settle(.6)
        after_less = station(item['name']) or {}
        after_budget = (snapshot().get('presenter') or {}).get('budget')
        after_less_enabled = bool((snapshot().get('presenter') or {}).get('enabled'))
        music_only = labelled(window, 'Music only', (env.Gtk.Button,))
        if music_only: music_only[0].emit('clicked'); env.settle(.6)
        after_music = station(item['name']) or {}
        superseded('P1b Less talking is station feedback: it lowers/persists the budget without changing station', 'P1b-R1',
                    bool(less) and after_less.get('id') == before_id and after_less != item, after_less)
        check('P1b-R1 Less talking leaves Presenter enabled and persists a strictly sparser budget',
              bool(less) and after_less.get('id') == before_id and after_less_enabled
              and budget_sparser(before_budget, after_budget), (before_budget, after_budget, after_less))
        check('P1c Music only is station feedback: it turns Presenter off without changing station',
              bool(music_only) and after_music.get('id') == before_id and not bool((snapshot().get('presenter') or {}).get('enabled')), after_music)
        window.close()

        # Turn it back on and tune it through the established public station surface.
        window = open_detail(item); set_presenter(window, True); tuned = tune(station(item['name']))
        tune_segments = len(segment_list())
        tune_music, _tune_item, _tune_clock = music_marker()
        env.pump(.5)
        check('P1e-R1 tuning starts music at once but does not create an opening presenter slot',
              tuned and tune_music.get('status') == 'Playing' and len(segment_list()) == tune_segments,
              (tune_music, segment_list()))
        mix_labels = visible_labels(window)
        presenter_row = 'Presenter' in mix_labels or bool(labelled(window, 'Presenter'))
        check('P2 the Mix exposes a Presenter row with its own level and Mute',
              tuned and presenter_row and bool(labelled(window, 'Mute')) and any(isinstance(w, env.Gtk.Scale) and w.get_mapped() for w in widgets(window)), mix_labels)
        first_slot = eligible_slot()
        playing = first_slot and wait(lambda: any(s.get('outcome') == 'played' for s in segment_list()), 6.)
        before_state, before_item, before_clock = music_marker()
        mute = labelled(window, 'Mute', (env.Gtk.Button, env.Gtk.Switch))
        if mute: mute[-1].emit('clicked') if isinstance(mute[-1], env.Gtk.Button) else mute[-1].set_active(True)
        env.settle(.7)
        after_state, after_item, after_clock = music_marker()
        superseded('P2b-R1 muting Presenter during a segment never advances, pauses, or delays music', 'P2b-R2',
                    bool(mute) and before_state == after_state, (before_state, after_state))
        check('P2b-R2 muting Presenter while it is playing keeps music identity and advancing clock',
              playing and bool(mute) and before_state.get('status') == after_state.get('status') == 'Playing'
              and before_item == after_item and before_item is not None and isinstance(before_clock, (int, float))
              and isinstance(after_clock, (int, float)) and .15 <= after_clock - before_clock <= 2.,
              (before_state, after_state))
        window.close()

        inspected = roomctl('inspect')
        try: inspected_json = json.loads(inspected.stdout)
        except ValueError: inspected_json = {}
        presenter = inspected_json.get('presenter') or snapshot().get('presenter') or {}
        check('P3 listening-roomctl inspect reports presenter enabled, budget, quiet status and recent sourced outcomes',
              {'enabled', 'budget', 'segments'} <= set(presenter) and 'status' in presenter, presenter)
        check('P3b MPRIS remains music playback while an optional presenter layer is enabled',
              mpris_status() in ('Playing', 'Paused'), mpris_status())

        for mode, wording in [('timeout', 'LLM timeout'), ('malformed', 'malformed LLM output'),
                              ('missing-voice', 'missing speech endpoint'), ('voice-fails', 'failing speech endpoint')]:
            fake.mode = mode; fake.requests.clear(); tune(station(item['name']))
            before = len(segment_list()); boundary = eligible_slot()
            wait(lambda: len(segment_list()) > before, 6)
            p = snapshot().get('presenter') or {}; segments = (p.get('segments') or [])[before:]
            music = (snapshot().get('layers') or {}).get('music') or {}
            superseded('P4-R1 %s drops the slot quietly, records a reason, and leaves music playing without an error card' % wording,
                        'P4-R2', boundary and music.get('status') == 'Playing' and bool(p.get('status'))
                        and any(s.get('outcome') == 'dropped' and s.get('reason') for s in segments), p)
            check('P4-R2 %s drops quietly with no window error card' % wording,
                  boundary and music.get('status') == 'Playing' and bool(p.get('status')) and no_error_card(window)
                  and any(s.get('outcome') == 'dropped' and s.get('reason') for s in segments), p)

        fake.mode = 'good'; fake.requests.clear(); tune(station(item['name']))
        before = len(segment_list()); boundary = eligible_slot(); wait(lambda: len(segment_list()) > before, 6)
        p = snapshot().get('presenter') or {}; segments = (p.get('segments') or [])[before:]
        positive = [s for s in segments if isinstance(s, dict) and s.get('outcome') == 'played'
                    and s.get('text') == 'Nocturne in C by Someone.' and source_names_metadata(s)]
        superseded('P5-R1 prepared eligible segments use loopback LLM and speech connections and retain text plus captured sources', 'P5-R2',
              any(path.endswith('/v1/chat/completions') for path, _ in fake.requests) and any(path.endswith('/v1/audio/speech') for path, _ in fake.requests)
              and all({'text', 'sources', 'outcome'} <= set(s) for s in segments if isinstance(s, dict)), (fake.requests, p))
        superseded('P5-R2 the captured-metadata positive control plays one sourced fake segment', 'P5-R3',
                    boundary and bool(positive), (fake.requests, p))
        check('P5-R3 the captured-metadata positive control cites the captured record and a library tag field',
              boundary and bool(positive) and any(path.endswith('/v1/chat/completions') for path, _ in fake.requests)
              and any(path.endswith('/v1/audio/speech') for path, _ in fake.requests), (fake.requests, p))

        # O11's public room transport controls the audible speech layer too.  The frozen music clock during
        # a measured pause is the observable remaining-duration guard: a slot cannot be spent while paused.
        transport_station = save('Room transport fixture', metadata=True, presenter=True); tune(transport_station)
        before = len(segment_list()); transport_boundary = eligible_slot()
        speaking = transport_boundary and wait(lambda: speech_is(status='Playing'), 6.)
        playing_music, playing_item, playing_clock = music_marker()
        played_segment = next((s for s in segment_list()[before:] if s.get('outcome') == 'played'), {})
        paused_command = roomctl('pause')
        paused = wait(lambda: speech_is(status='Paused') and music_marker()[0].get('status') == 'Paused', 3.)
        paused_clock = music_marker()[2]; env.pump(.5); held_clock = music_marker()[2]
        played_command = roomctl('play')
        resumed = wait(lambda: speech_is(status='Playing') and music_marker()[0].get('status') == 'Playing', 3.)
        resumed_music, resumed_item, resumed_clock = music_marker()
        check('P10 room Pause and Play pause and resume a presenter slot with its remaining duration',
              speaking and bool(played_segment.get('duration')) and paused_command.returncode == 0
              and paused and isinstance(paused_clock, (int, float)) and isinstance(held_clock, (int, float))
              and abs(held_clock - paused_clock) <= .05 and played_command.returncode == 0 and resumed
              and playing_item == resumed_item and isinstance(resumed_clock, (int, float))
              and resumed_clock >= held_clock, (playing_music, speech_layer(), paused_clock, held_clock, resumed_music))

        leave_station = save('Leave transport fixture', metadata=True, presenter=True); tune(leave_station)
        before = len(segment_list()); leave_boundary = eligible_slot()
        leave_speaking = leave_boundary and wait(lambda: speech_is(status='Playing'), 6.)
        leave_command = roomctl('leave')
        left = wait(lambda: speech_is(status='Stopped'), 2.)
        check('P11 Leave during a presenter slot stops speech immediately',
              leave_speaking and leave_command.returncode == 0 and left, speech_layer())

        fake.mode = 'slow-voice'; fake.requests.clear(); tune(station(item['name']))
        before = len(segment_list()); boundary = eligible_slot(); slow_before, slow_item, slow_clock = music_marker()
        wait(lambda: len(segment_list()) > before, 6.)
        slow_after, slow_item_after, slow_clock_after = music_marker(); slow = segment_list()
        slow_slot = slow[before:]
        superseded('P5b-R1 a segment not ready at its slot is dropped rather than creating a gap', 'P5b-R2',
                    any(s.get('outcome') == 'dropped' for s in slow_slot) or bool(presenter_data().get('status')), slow_slot)
        check('P5b-R2 a slow slot drops, with music continuously playing and no played segment for that slot',
              boundary and slow_after.get('status') == 'Playing' and slow_item == slow_item_after and slow_item is not None
              and isinstance(slow_clock, (int, float)) and isinstance(slow_clock_after, (int, float)) and slow_clock_after > slow_clock
              and any(s.get('outcome') == 'dropped' and s.get('reason') for s in slow_slot)
              and not any(s.get('outcome') == 'played' for s in slow_slot), slow_slot)

        # Two explicit eligible boundaries on the same station; repeat suppression may occur before speech, so
        # endpoint request count is not an oracle.
        fake.mode = 'good'; repeat_station = save('Repeat fixture', metadata=True, presenter=True); tune(repeat_station)
        minimum = every_tracks(); before = len(segment_list()); first_boundary = eligible_slot()
        wait(lambda: len(segment_list()) > before, 6); second_boundary = bool(minimum) and eligible_slot(minimum)
        wait(lambda: len(segment_list()) > before + 1, 6); repeated = segment_list()[before:]
        played = [s for s in repeated if s.get('outcome') == 'played']
        first_played = next((index for index, segment in enumerate(repeated) if segment.get('outcome') == 'played'), None)
        later_drop = (first_played is not None and any(segment.get('outcome') == 'dropped' and segment.get('reason')
                      for segment in repeated[first_played + 1:]))
        superseded('P8-R2 identical text in two eligible slots plays exactly once within the history window', 'P8-R3',
                    len(played) == 1 and later_drop, repeated)
        check('P8-R3 identical text in two eligible slots plays exactly once within the history window',
              first_boundary and second_boundary and len(repeated) >= 2 and len(played) == 1 and later_drop, repeated)

        fake.mode = 'good'; fake.requests.clear(); speech = save('Primary speech fixture', speech=True, presenter=True); tune(speech)
        before = len(segment_list()); boundary = eligible_slot(); env.pump(1.)
        speech_p = snapshot().get('presenter') or {}
        speech_segments = segment_list(speech_p)[before:]
        superseded('P6-R1 a primary speech station suppresses presenter segments while it plays', 'P6-R2',
                    not any(s.get('outcome') == 'played' for s in speech_segments), speech_p)
        check('P6-R2 primary speech suppresses slots after the positive control proved a slot can play',
              boundary and bool(positive) and bool(speech_p.get('enabled')) and not fake.requests
              and not any(s.get('outcome') == 'played' for s in speech_segments), speech_p)
        # The public record is the oracle: an empty captured-source list may never become a played factual claim.
        # The fixture radio item has no metadata-pipeline provenance; an implementation must therefore leave it
        # unplayed (or omit it), rather than manufacture facts from a title or an LLM response.
        bare = save('No metadata fixture', metadata=False, presenter=True); fake.requests.clear(); tune(bare)
        before = len(segment_list()); boundary = eligible_slot(); env.pump(1.)
        bare_segments = segment_list()[before:]
        superseded('P7-R1 an item with no metadata gets no factual presenter segment', 'P7-R2',
                    all(not (not s.get('sources') and s.get('outcome') == 'played') for s in bare_segments), bare_segments)
        check('P7-R2 no captured provenance suppresses factual segments after the positive control',
              boundary and bool(positive) and bool((snapshot().get('presenter') or {}).get('enabled'))
              and not any(s.get('outcome') == 'played' for s in bare_segments), bare_segments)

        fake.mode = 'overlong-voice'; tune(station(item['name']))
        before = len(segment_list()); boundary = eligible_slot(); wait(lambda: len(segment_list()) > before, 6)
        overlong = segment_list()[before:]; budget = presenter_data().get('budget') or {}
        maximum = next((v for key, v in budget.items() if isinstance(v, (int, float)) and 'max' in key.casefold()), None) if isinstance(budget, dict) else None
        superseded('P1d-R1 an over-budget fake segment is dropped or publicly trimmed to the budget maximum', 'P1d-R2',
                    maximum is not None and any(s.get('outcome') == 'dropped' and s.get('reason') for s in overlong),
                    (budget, overlong))
        check('P1d-R2 an over-budget fake segment is dropped or publicly trimmed to the budget maximum',
              boundary and maximum is not None and (any(s.get('outcome') == 'dropped' and s.get('reason') for s in overlong)
              or any(s.get('outcome') == 'played' and isinstance(s.get('duration'), (int, float)) and s['duration'] <= maximum for s in overlong)), (budget, overlong))

        now = time.time()
        clip_audio = str(write_clip_wav())
        clips = [{'source': 'fixture-clip', 'audio': clip_audio, 'allowed_placement': 'between_tracks', 'expires_at': now + 60},
                 {'source': 'expired-fixture-clip', 'audio': clip_audio, 'allowed_placement': 'between_tracks', 'expires_at': now - 1}]
        clip_station = save('Clip fixture', metadata=True, clips=clips, presenter=True); tune(clip_station)
        before = len(segment_list()); boundary = eligible_slot(); wait(lambda: len(segment_list()) > before, 6)
        clip_segments = segment_list()[before:]
        superseded('P9-R1 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry', 'P9-R2',
                    any(s.get('outcome') == 'played' and s.get('source') == 'fixture-clip' for s in clip_segments),
                    clip_segments)
        check('P9-R2 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry',
              boundary and any(s.get('outcome') == 'played' and s.get('source') == 'fixture-clip'
                  and s.get('placement') == 'between_tracks' for s in clip_segments)
              and not any(s.get('outcome') == 'played' and s.get('source') == 'expired-fixture-clip' for s in clip_segments), clip_segments)
    finally:
        fake.close()


try:
    run()
except Exception as error:
    traceback.print_exc()
    check('O12 probe ran without an unhandled error', False, '%s: %s' % (type(error).__name__, error))
finally:
    finish()
