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
* The positive fixture is a saved collection station whose Jellyfin reference is the metadata pipeline's captured
  ``jellyfin:fixture/track-1`` record.  A source must name that captured record (its id or title), rather than merely
  be non-empty.  The no-metadata fixture uses a distinct, otherwise identical reference with no captured record.
* P2b/P5b need the existing inspect music-layer identity and monotonic playback position as public observations.
  The probe accepts ``item``/``id``/``name`` and ``position``/``elapsed``/``clock`` spellings, but needs one of each.
* Narrow clip reading for coordinator ruling: a fixture registers clips as ``preferences.presenter_clips`` entries
  (``source``, ``allowed_placement``, ``expires_at``).  The corresponding public segment record exposes ``source``
  and ``placement``.  If clips instead have a labelled public editor, this setup should migrate to it.
* Maximum length reading: a result over the budget maximum may be dropped with a reason or recorded as played only
  when its public duration is no greater than that maximum.  The fake advertises its overlong duration in its speech
  response, so this is not a wall-clock guess.

Run: scripts/listening-dev.sh acceptance OUTDIR o12pr
"""
import copy
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
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


def save(name, *, speech=False, metadata=True, clips=None):
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
    env.service.execute('station.save', {'station': definition})
    found = station(name)
    assert found, 'fixture station was not saved'
    return found


def tune(item):
    env.service.execute('station.start', {'id': item['id']})
    return wait(lambda: bool(snapshot().get('station') or env.controller.active), 6.)


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
                    self.send_header('Content-Type', 'audio/mpeg')
                    # A deterministic duration declaration lets the public segment record be checked without audio decoding.
                    payload = (b'ID3DURATION=9999;' if outer.mode == 'overlong-voice' else b'ID3DURATION=1;') + b'\0' * 64
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
    return 'track-1' in text or 'nocturne in c' in text


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
                ('P2b muting Presenter during a segment never advances, pauses, or delays music', 'P2b-R1'),
                ('P4 endpoint failure drops the slot quietly, records a reason, and leaves music playing without an error card', 'P4-R1'),
                ('P5 prepared eligible segments use loopback LLM and speech connections and retain text plus captured sources', 'P5-R1'),
                ('P5b a segment not ready at its slot is dropped rather than creating a gap', 'P5b-R1'),
                ('P6 a primary speech station suppresses presenter segments while it plays', 'P6-R1'),
                ('P7 an item with no metadata gets no factual presenter segment', 'P7-R1'),
                ('P8 a segment does not repeat within the station history window', 'P8-R1')):
                superseded(original, revision, False, 'Presenter is absent from the station detail on this branch')
            for name in [
                'P1 station detail exposes a Presenter switch and a visible adjustable budget',
                'P1b-R1 Less talking leaves Presenter enabled and persists a strictly sparser budget',
                'P1c Music only is station feedback: it turns Presenter off without changing station',
                'P2 the Mix exposes a Presenter row with its own level and Mute',
                'P2b-R1 muting Presenter while it is playing keeps music identity and advancing clock',
                'P3 listening-roomctl inspect reports presenter enabled, budget, quiet status and recent sourced outcomes',
                'P3b MPRIS remains music playback while an optional presenter layer is enabled',
                'P4-R1 LLM timeout drops quietly with no window error card',
                'P4-R1 malformed LLM output drops quietly with no window error card',
                'P4-R1 missing speech endpoint drops quietly with no window error card',
                'P4-R1 failing speech endpoint drops quietly with no window error card',
                'P5-R1 the captured-metadata positive control plays one sourced fake segment',
                'P5b-R1 a slow slot drops, with music continuously playing and no played segment for that slot',
                'P6-R1 primary speech suppresses slots after the positive control proved a slot can play',
                'P7-R1 no captured provenance suppresses factual segments after the positive control',
                'P8-R1 identical text in two eligible slots plays exactly once within the history window',
                'P1d-R1 an over-budget fake segment is dropped or publicly trimmed to the budget maximum',
                'P9-R1 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry',
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
        music_only = labelled(window, 'Music only', (env.Gtk.Button,))
        if music_only: music_only[0].emit('clicked'); env.settle(.6)
        after_music = station(item['name']) or {}
        superseded('P1b Less talking is station feedback: it lowers/persists the budget without changing station', 'P1b-R1',
                    bool(less) and after_less.get('id') == before_id and after_less != item, after_less)
        check('P1b-R1 Less talking leaves Presenter enabled and persists a strictly sparser budget',
              bool(less) and after_less.get('id') == before_id and bool((snapshot().get('presenter') or {}).get('enabled'))
              and budget_sparser(before_budget, after_budget), (before_budget, after_budget, after_less))
        check('P1c Music only is station feedback: it turns Presenter off without changing station',
              bool(music_only) and after_music.get('id') == before_id and not bool((snapshot().get('presenter') or {}).get('enabled')), after_music)
        window.close()

        # Turn it back on and tune it through the established public station surface.
        window = open_detail(item); set_presenter(window, True); tuned = tune(station(item['name']))
        mix_labels = visible_labels(window)
        presenter_row = 'Presenter' in mix_labels or bool(labelled(window, 'Presenter'))
        check('P2 the Mix exposes a Presenter row with its own level and Mute',
              tuned and presenter_row and bool(labelled(window, 'Mute')) and any(isinstance(w, env.Gtk.Scale) and w.get_mapped() for w in widgets(window)), mix_labels)
        playing = wait(lambda: any(s.get('outcome') == 'played' for s in segment_list()), 6.)
        before_state, before_item, before_clock = music_marker()
        mute = labelled(window, 'Mute', (env.Gtk.Button, env.Gtk.Switch))
        if mute: mute[-1].emit('clicked') if isinstance(mute[-1], env.Gtk.Button) else mute[-1].set_active(True)
        env.settle(.7)
        after_state, after_item, after_clock = music_marker()
        superseded('P2b muting Presenter during a segment never advances, pauses, or delays music', 'P2b-R1',
                    bool(mute) and before_state == after_state, (before_state, after_state))
        check('P2b-R1 muting Presenter while it is playing keeps music identity and advancing clock',
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
            fake.mode = mode; fake.requests.clear(); tune(station(item['name'])); wait(lambda: bool(snapshot().get('presenter')), 6)
            p = snapshot().get('presenter') or {}; segments = p.get('segments') or []
            music = (snapshot().get('layers') or {}).get('music') or {}
            superseded('P4 %s drops the slot quietly, records a reason, and leaves music playing without an error card' % wording,
                        'P4-R1', music.get('status') == 'Playing' and bool(p.get('status'))
                        and any(s.get('outcome') == 'dropped' and s.get('reason') for s in segments), p)
            check('P4-R1 %s drops quietly with no window error card' % wording,
                  music.get('status') == 'Playing' and bool(p.get('status')) and no_error_card(window)
                  and any(s.get('outcome') == 'dropped' and s.get('reason') for s in segments), p)

        fake.mode = 'good'; fake.requests.clear(); tune(station(item['name'])); wait(lambda: any(s.get('outcome') == 'played' for s in segment_list()), 6)
        p = snapshot().get('presenter') or {}; segments = p.get('segments') or []
        positive = [s for s in segments if isinstance(s, dict) and s.get('outcome') == 'played'
                    and s.get('text') == 'Nocturne in C by Someone.' and source_names_metadata(s)]
        superseded('P5 prepared eligible segments use loopback LLM and speech connections and retain text plus captured sources', 'P5-R1',
              any(path.endswith('/v1/chat/completions') for path, _ in fake.requests) and any(path.endswith('/v1/audio/speech') for path, _ in fake.requests)
              and all({'text', 'sources', 'outcome'} <= set(s) for s in segments if isinstance(s, dict)), (fake.requests, p))
        check('P5-R1 the captured-metadata positive control plays one sourced fake segment',
              bool(positive) and any(path.endswith('/v1/chat/completions') for path, _ in fake.requests)
              and any(path.endswith('/v1/audio/speech') for path, _ in fake.requests), (fake.requests, p))

        fake.mode = 'slow-voice'; fake.requests.clear(); tune(station(item['name']))
        slow_before, slow_item, slow_clock = music_marker(); wait(lambda: any(s.get('outcome') == 'dropped' for s in segment_list()), 6.)
        slow_after, slow_item_after, slow_clock_after = music_marker(); slow = segment_list()
        superseded('P5b a segment not ready at its slot is dropped rather than creating a gap', 'P5b-R1',
                    any(s.get('outcome') == 'dropped' for s in slow) or bool(presenter_data().get('status')), slow)
        check('P5b-R1 a slow slot drops, with music continuously playing and no played segment for that slot',
              slow_after.get('status') == 'Playing' and slow_item == slow_item_after and slow_item is not None
              and isinstance(slow_clock, (int, float)) and isinstance(slow_clock_after, (int, float)) and slow_clock_after > slow_clock
              and any(s.get('outcome') == 'dropped' and s.get('reason') for s in slow)
              and not any(s.get('outcome') == 'played' for s in slow[-1:]), slow)

        fake.mode = 'good'; fake.requests.clear(); speech = save('Primary speech fixture', speech=True); tune(speech); env.pump(1.)
        speech_p = snapshot().get('presenter') or {}
        superseded('P6 a primary speech station suppresses presenter segments while it plays', 'P6-R1',
                    not any(s.get('outcome') == 'played' for s in segment_list(speech_p)), speech_p)
        check('P6-R1 primary speech suppresses slots after the positive control proved a slot can play',
              bool(positive) and not fake.requests and not any(s.get('outcome') == 'played' for s in segment_list(speech_p)), speech_p)
        # The public record is the oracle: an empty captured-source list may never become a played factual claim.
        # The fixture radio item has no metadata-pipeline provenance; an implementation must therefore leave it
        # unplayed (or omit it), rather than manufacture facts from a title or an LLM response.
        bare = save('No metadata fixture', metadata=False); fake.requests.clear(); tune(bare); env.pump(1.)
        bare_segments = segment_list()
        superseded('P7 an item with no metadata gets no factual presenter segment', 'P7-R1',
                    all(not (not s.get('sources') and s.get('outcome') == 'played') for s in bare_segments), bare_segments)
        check('P7-R1 no captured provenance suppresses factual segments after the positive control',
              bool(positive) and not any(s.get('outcome') == 'played' for s in bare_segments), bare_segments)

        fake.mode = 'good'; fake.requests.clear(); tune(station(item['name'])); wait(lambda: len(fake.requests) >= 4, 6.)
        repeats = segment_list(); played = [s for s in repeats if s.get('outcome') == 'played' and s.get('text') == 'Nocturne in C by Someone.']
        superseded('P8 a segment does not repeat within the station history window', 'P8-R1', len({s.get('text') for s in played}) == len(played), repeats)
        check('P8-R1 identical text in two eligible slots plays exactly once within the history window',
              len(fake.requests) >= 4 and len(played) == 1 and len(repeats) >= 2
              and any(s.get('outcome') == 'dropped' and s.get('reason') for s in repeats), repeats)

        fake.mode = 'overlong-voice'; tune(station(item['name'])); env.pump(1.)
        overlong = segment_list(); budget = presenter_data().get('budget') or {}
        maximum = next((v for key, v in budget.items() if isinstance(v, (int, float)) and 'max' in key.casefold()), None) if isinstance(budget, dict) else None
        check('P1d-R1 an over-budget fake segment is dropped or publicly trimmed to the budget maximum',
              maximum is not None and (any(s.get('outcome') == 'dropped' and s.get('reason') for s in overlong)
              or any(s.get('outcome') == 'played' and isinstance(s.get('duration'), (int, float)) and s['duration'] <= maximum for s in overlong)), (budget, overlong))

        now = time.time()
        clips = [{'source': 'fixture-clip', 'allowed_placement': 'between_tracks', 'expires_at': now + 60},
                 {'source': 'expired-fixture-clip', 'allowed_placement': 'between_tracks', 'expires_at': now - 1}]
        clip_station = save('Clip fixture', metadata=True, clips=clips); tune(clip_station); env.pump(1.)
        clip_segments = segment_list()
        check('P9-R1 prerecorded clips use the slot scheduler, preserve source/placement, and honour expiry',
              any(s.get('outcome') == 'played' and s.get('source') == 'fixture-clip'
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
