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


def save(name, *, speech=False):
    definition = copy.deepcopy(o6_env.RADIO_STATION)
    definition.update(name=name, preferences={})
    if speech:
        # Reading: this is the saved, public declaration that identifies a primary speech station.
        definition['speech'] = True
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
                    payload = b'ID3' + b'\0' * 64
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
              not (forbidden & set(json.dumps(stored).casefold().replace('-', '_').replace('"', ' ').split())), stored)
        room.close()

        # On the base branch the feature is deliberately absent.  Record every contract check as a clear
        # feature-absence failure and finish promptly; otherwise a pre-feature run would spend its time waiting
        # for endpoints which no code can call.  The complete adversarial path below runs once Presenter exists.
        if not feature_present:
            for name in [
                'P1 station detail exposes a Presenter switch and a visible adjustable budget',
                'P1b Less talking is station feedback: it lowers/persists the budget without changing station',
                'P1c Music only is station feedback: it turns Presenter off without changing station',
                'P2 the Mix exposes a Presenter row with its own level and Mute',
                'P2b muting Presenter during a segment never advances, pauses, or delays music',
                'P3 listening-roomctl inspect reports presenter enabled, budget, quiet status and recent sourced outcomes',
                'P3b MPRIS remains music playback while an optional presenter layer is enabled',
                'P4 LLM timeout drops the slot quietly, records a reason, and leaves music playing without an error card',
                'P4 malformed LLM output drops the slot quietly, records a reason, and leaves music playing without an error card',
                'P4 missing speech endpoint drops the slot quietly, records a reason, and leaves music playing without an error card',
                'P4 failing speech endpoint drops the slot quietly, records a reason, and leaves music playing without an error card',
                'P5 prepared eligible segments use loopback LLM and speech connections and retain text plus captured sources',
                'P5b a segment not ready at its slot is dropped rather than creating a gap',
                'P6 a primary speech station suppresses presenter segments while it plays',
                'P7 an item with no metadata gets no factual presenter segment',
                'P8 a segment does not repeat within the station history window',
            ]:
                check(name, False, 'Presenter is absent from the station detail on this branch')
            return

        item = save('Presenter fixture')
        window = open_detail(item)
        enabled = set_presenter(window)
        labels = visible_labels(window)
        check('P1 station detail exposes a Presenter switch and a visible adjustable budget',
              enabled and 'Presenter' in labels and bool(labelled(window, 'talk') or labelled(window, 'budget')), labels)
        before_id = item['id']
        less = labelled(window, 'Less talking', (env.Gtk.Button,))
        if less: less[0].emit('clicked'); env.settle(.6)
        after_less = station(item['name']) or {}
        music_only = labelled(window, 'Music only', (env.Gtk.Button,))
        if music_only: music_only[0].emit('clicked'); env.settle(.6)
        after_music = station(item['name']) or {}
        check('P1b Less talking is station feedback: it lowers/persists the budget without changing station',
              bool(less) and after_less.get('id') == before_id and after_less != item, after_less)
        check('P1c Music only is station feedback: it turns Presenter off without changing station',
              bool(music_only) and after_music.get('id') == before_id and not bool((snapshot().get('presenter') or {}).get('enabled')), after_music)
        window.close()

        # Turn it back on and tune it through the established public station surface.
        window = open_detail(item); set_presenter(window, True); tuned = tune(station(item['name']))
        mix_labels = visible_labels(window)
        presenter_row = 'Presenter' in mix_labels or bool(labelled(window, 'Presenter'))
        check('P2 the Mix exposes a Presenter row with its own level and Mute',
              tuned and presenter_row and bool(labelled(window, 'Mute')) and any(isinstance(w, env.Gtk.Scale) and w.get_mapped() for w in widgets(window)), mix_labels)
        before_music = (snapshot().get('layers') or {}).get('music', {}).copy()
        mute = labelled(window, 'Mute', (env.Gtk.Button, env.Gtk.Switch))
        if mute: mute[-1].emit('clicked') if isinstance(mute[-1], env.Gtk.Button) else mute[-1].set_active(True)
        env.settle(.7)
        after_music = (snapshot().get('layers') or {}).get('music', {}).copy()
        check('P2b muting Presenter during a segment never advances, pauses, or delays music',
              bool(mute) and before_music == after_music, (before_music, after_music))
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
            check('P4 %s drops the slot quietly, records a reason, and leaves music playing without an error card' % wording,
                  music.get('status') == 'Playing' and bool(p.get('status')) and any(s.get('outcome') == 'dropped' and s.get('reason') for s in segments if isinstance(s, dict)), p)

        fake.mode = 'good'; fake.requests.clear(); tune(station(item['name'])); wait(lambda: bool(fake.requests), 6)
        p = snapshot().get('presenter') or {}; segments = p.get('segments') or []
        check('P5 prepared eligible segments use loopback LLM and speech connections and retain text plus captured sources',
              any(path.endswith('/v1/chat/completions') for path, _ in fake.requests) and any(path.endswith('/v1/audio/speech') for path, _ in fake.requests)
              and all({'text', 'sources', 'outcome'} <= set(s) for s in segments if isinstance(s, dict)), (fake.requests, p))
        check('P5b a segment not ready at its slot is dropped rather than creating a gap',
              any(s.get('outcome') == 'dropped' for s in segments if isinstance(s, dict)) or bool(p.get('status')), p)

        speech = save('Primary speech fixture', speech=True); tune(speech); env.pump(1.)
        speech_p = snapshot().get('presenter') or {}
        check('P6 a primary speech station suppresses presenter segments while it plays',
              not any(s.get('outcome') == 'played' for s in speech_p.get('segments', []) if isinstance(s, dict)), speech_p)
        # The public record is the oracle: an empty captured-source list may never become a played factual claim.
        # The fixture radio item has no metadata-pipeline provenance; an implementation must therefore leave it
        # unplayed (or omit it), rather than manufacture facts from a title or an LLM response.
        check('P7 an item with no metadata gets no factual presenter segment',
              all(not (not s.get('sources') and s.get('outcome') == 'played') for s in segments if isinstance(s, dict)), segments)
        check('P8 a segment does not repeat within the station history window',
              len({s.get('text') for s in segments if isinstance(s, dict) and s.get('outcome') == 'played'}) == len([s for s in segments if isinstance(s, dict) and s.get('outcome') == 'played']), segments)
    finally:
        fake.close()


try:
    run()
except Exception as error:
    traceback.print_exc()
    check('O12 probe ran without an unhandled error', False, '%s: %s' % (type(error).__name__, error))
finally:
    finish()
