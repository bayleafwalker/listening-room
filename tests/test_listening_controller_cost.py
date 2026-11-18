"""The controller's steady-state cost: PipeWire queries, state writes, IPC connections, file reads.

Also covers the refresh loop surviving unexpected errors and the disconnect-pause and
call-ducking policy, driven through the real policy poll with PipeWire faked.
"""
import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_devices
import listening_hub as hub
import mpv_ipc
import room_session as sessions
import session_service
from test_listening_session import FakeBackend

STATION = {'id': 's1', 'name': 'One', 'sources': [{'kind': 'radio', 'source_id': 'piano', 'name': 'Piano'}]}
COMMUNICATION = [{'index': 7, 'corked': False, 'properties': {'media.role': 'Communication'}}]


class Environment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(Path(self.tmp.name) / 'config'),
            'XDG_STATE_HOME': str(Path(self.tmp.name) / 'state'),
            'XDG_CACHE_HOME': str(Path(self.tmp.name) / 'cache')})
        environment.start()
        self.addCleanup(environment.stop)
        state = mock.patch.object(hub, 'STATE', Path(self.tmp.name) / 'hub-state')
        state.start()
        self.addCleanup(state.stop)
        self.now = 1000.
        self.backend = FakeBackend()
        self.controller = sessions.Controller(self.backend, self.tmp.name, clock=lambda: self.now)
        self.addCleanup(self.controller.close)
        # The module-level "previous output" must not leak between tests.
        audio_devices._previous_output = None

    def pipewire(self, sinks=(), sink_inputs=(), source_outputs=()):
        """Fake pactl; returns the list of commands it was asked to run."""
        commands = []

        def run(args):
            commands.append(list(args))
            return 'sink1\n'

        def listing(kind):
            commands.append(['pactl', 'list', kind])
            return {'sinks': [{'name': name, 'index': 1, 'properties': {}} for name in sinks],
                    'sink-inputs': list(sink_inputs), 'source-outputs': list(source_outputs)}[kind]
        for patch in (mock.patch.object(audio_devices, '_run', side_effect=run),
                      mock.patch.object(audio_devices, '_list', side_effect=listing)):
            patch.start()
            self.addCleanup(patch.stop)
        return commands


class PolicyCostTests(Environment):
    def test_idle_controller_with_policies_off_runs_no_pactl_in_a_minute(self):
        audio_devices.configure(preferred_output='sink1', pause_on_disconnect=False, duck_on_call=False)
        self.controller.settings.update(pause_on_disconnect=False, duck_on_call=False)
        commands = self.pipewire(sinks=['sink1'])
        for _ in range(60):
            self.now += 1
            self.controller.poll_policy()
        self.assertEqual(commands, [])
        self.assertIsNone(self.controller.policy_status['error'])

    def test_policy_reads_each_stream_list_at_most_once_per_poll(self):
        audio_devices.configure(preferred_output='sink1', pause_on_disconnect=True, duck_on_call=True)
        self.controller.settings.update(pause_on_disconnect=True, duck_on_call=True)
        commands = self.pipewire(sinks=['sink1'])
        self.controller.poll_policy()
        listed = [command[-1] for command in commands if command[:2] == ['pactl', 'list']]
        self.assertEqual(sorted(listed), ['sink-inputs', 'sinks', 'source-outputs'])
        self.assertNotIn(['pactl', 'get-default-sink'], commands)

    def test_a_policy_asks_only_for_what_it_needs(self):
        audio_devices.configure(preferred_output='sink1', pause_on_disconnect=True, duck_on_call=False)
        commands = self.pipewire(sinks=['sink1'])
        self.assertFalse(audio_devices.poll_policy()['disconnect'])
        self.assertEqual([c[-1] for c in commands], ['sinks'])
        audio_devices.configure(pause_on_disconnect=False, duck_on_call=True)
        del commands[:]
        audio_devices.poll_policy()
        self.assertEqual(sorted(c[-1] for c in commands), ['sink-inputs', 'source-outputs'])

    def test_the_controllers_own_duck_setting_still_detects_calls(self):
        audio_devices.configure(pause_on_disconnect=False, duck_on_call=False)
        commands = self.pipewire(source_outputs=COMMUNICATION)
        self.assertEqual(audio_devices.poll_policy()['call_active'], False)
        self.assertEqual(commands, [])
        self.assertTrue(audio_devices.poll_policy(detect_calls=True)['call_active'])

    def test_disconnect_pauses_a_playing_room_through_the_real_policy(self):
        audio_devices.configure(preferred_output='headphones', pause_on_disconnect=True)
        self.backend.state['music'] = {'status': 'Playing', 'source_id': 'x', 'volume': 50, 'muted': False}
        self.controller.refresh()
        self.pipewire(sinks=['speakers'])
        with mock.patch.object(self.controller, 'quiet') as quiet:
            self.controller.poll_policy()
        quiet.assert_called_once()
        self.assertTrue(self.controller.policy_status['disconnect'])

    def test_present_output_does_not_pause(self):
        audio_devices.configure(preferred_output='headphones', pause_on_disconnect=True)
        self.backend.state['music'] = {'status': 'Playing', 'source_id': 'x', 'volume': 50, 'muted': False}
        self.controller.refresh()
        self.pipewire(sinks=['headphones'])
        with mock.patch.object(self.controller, 'quiet') as quiet:
            self.controller.poll_policy()
        quiet.assert_not_called()

    def test_a_call_ducks_and_its_end_restores_through_the_real_policy(self):
        audio_devices.configure(duck_on_call=True, pause_on_disconnect=False)
        self.controller.settings['duck_on_call'] = True
        self.backend.state['music'] = {'status': 'Playing', 'source_id': 'x', 'volume': 50, 'muted': False}
        self.controller.refresh()
        calls = self.pipewire(source_outputs=COMMUNICATION)
        self.controller.poll_policy()
        self.assertEqual(self.backend.state['music']['volume'], 10)
        self.assertLessEqual(len([c for c in calls if c[-1] == 'sink-inputs']), 1)
        calls = self.pipewire()
        self.controller.poll_policy()
        self.assertEqual(self.backend.state['music']['volume'], 50)


class ObserveCostTests(Environment):
    def test_unchanged_radio_audio_is_not_rewritten_and_state_is_read_once(self):
        hub.update_state(radio_name='Station', radio_audio={'volume': 40, 'muted': False})
        answers = {'volume': 40, 'mute': False, 'pause': False, 'idle-active': False, 'media-title': 'Song'}
        reads, writes = [], []
        real_load, real_save = hub.load_json, hub.save_json
        with mock.patch.object(hub, 'ipc', side_effect=lambda command, **kw: answers[command[-1]]), \
                mock.patch.object(hub, 'load_json', side_effect=lambda p, d: reads.append(Path(p).name) or real_load(p, d)), \
                mock.patch.object(hub, 'save_json', side_effect=lambda p, v: writes.append(Path(p).name) or real_save(p, v)):
            backend = sessions.Backend()
            layers = backend.layers()
            self.assertEqual(layers['music']['status'], 'Playing')
            self.assertEqual(layers['music']['volume'], 40)
            self.assertEqual(reads.count('state.json'), 1)
            self.assertEqual(writes, [])
            answers['volume'] = 41
            backend.layers()
            self.assertEqual(writes, ['state.json'])
            self.assertEqual(hub.state()['radio_audio']['volume'], 41)
            backend.layers()
            self.assertEqual(writes, ['state.json'])

    def test_media_details_reuse_the_socket_the_refresh_already_read(self):
        hub.update_state(radio_name='Station', radio_socket='radio-0123456789ab.sock', radio_audio={'muted': False})
        reads = []
        real_load = hub.load_json
        sockets = []
        with mock.patch.object(hub, 'RUNTIME', Path(self.tmp.name)), mock.patch.object(hub, 'SOCKET', Path(self.tmp.name) / 'radio.sock'), \
                mock.patch.object(hub, 'ipc', side_effect=lambda command, **kw: sockets.append(kw.get('socket_path')) or False), \
                mock.patch.object(hub, 'load_json', side_effect=lambda p, d: reads.append(Path(p).name) or real_load(p, d)):
            backend = sessions.Backend()
            backend.layers()
            backend.media_details()
        self.assertEqual(reads.count('state.json'), 1)
        self.assertEqual({str(s) for s in sockets}, {str(Path(self.tmp.name) / 'radio-0123456789ab.sock')})


class FakeMpv(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, answers):
        class Handler(socketserver.StreamRequestHandler):
            def handle(handler):
                self.connections += 1
                for line in handler.rfile:
                    message = json.loads(line)
                    handler.wfile.write(b'{"event":"property-change"}\n')
                    name = message['command'][-1]
                    reply = {'request_id': message['request_id'], 'error': 'success', 'data': answers.get(name)}
                    if name not in answers:
                        reply['error'] = 'property unavailable'
                    handler.wfile.write(json.dumps(reply).encode() + b'\n')
        self.connections = 0
        super().__init__(str(path), Handler)


class IpcConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'mpv.sock'
        self.server = FakeMpv(self.path, {'pause': False, 'volume': 33, 'mute': True})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_each_call_opens_its_own_connection_outside_a_scope(self):
        for name in ('pause', 'volume', 'mute'):
            mpv_ipc.call(self.path, ['get_property', name])
        self.assertEqual(self.server.connections, 3)

    def test_a_scope_reuses_one_connection_and_skips_events(self):
        with mpv_ipc.shared():
            values = [mpv_ipc.call(self.path, ['get_property', name]) for name in ('pause', 'volume', 'mute', 'volume')]
        self.assertEqual(values, [False, 33, True, 33])
        self.assertEqual(self.server.connections, 1)

    def test_a_rejected_property_keeps_the_connection(self):
        with mpv_ipc.shared():
            with self.assertRaises(mpv_ipc.Rejected):
                mpv_ipc.call(self.path, ['get_property', 'nothing'])
            self.assertEqual(mpv_ipc.call(self.path, ['get_property', 'volume']), 33)
        self.assertEqual(self.server.connections, 1)

    def test_a_missing_player_is_attempted_once_per_scope(self):
        missing = Path(self.tmp.name) / 'absent.sock'
        attempts = []

        class Counting(mpv_ipc._Connection):
            def __init__(self, path, timeout):
                attempts.append(path)
                super().__init__(path, timeout)
        with mock.patch.object(mpv_ipc, '_Connection', Counting), mpv_ipc.shared():
            for _ in range(3):
                with self.assertRaises(OSError):
                    mpv_ipc.call(missing, ['get_property', 'pause'])
        self.assertEqual(len(attempts), 1)

    def test_a_scope_is_not_shared_between_threads_or_kept_after_it_ends(self):
        with mpv_ipc.shared():
            mpv_ipc.call(self.path, ['get_property', 'pause'])
            worker = threading.Thread(target=mpv_ipc.call, args=(self.path, ['get_property', 'pause']))
            worker.start()
            worker.join()
        self.assertEqual(self.server.connections, 2)
        mpv_ipc.call(self.path, ['get_property', 'pause'])
        self.assertEqual(self.server.connections, 3)

    def test_a_refresh_opens_fewer_connections_than_one_per_property(self):
        with mock.patch.object(hub, 'SOCKET', self.path):
            with mpv_ipc.shared():
                for name in ('pause', 'volume', 'mute', 'volume', 'mute'):
                    hub.ipc(['get_property', name], socket_path=self.path)
        self.assertEqual(self.server.connections, 1)


class SnapshotCacheTests(Environment):
    def reads(self):
        names = []
        real = hub.load_json
        patch = mock.patch.object(hub, 'load_json', side_effect=lambda p, d: names.append(Path(p).name) or real(p, d))
        patch.start()
        self.addCleanup(patch.stop)
        return names

    def settled(self, name, content):
        path = Path(self.tmp.name) / name
        hub.save_json(path, content)
        os.utime(path, ns=(1, 1))   # long settled: the cache may trust it

    def test_unchanged_station_and_variant_files_are_not_re_read(self):
        self.settled('stations.json', [STATION])
        self.settled('room-variants.json', [])
        names = self.reads()
        first = self.controller.snapshot()
        for _ in range(20):
            self.controller.snapshot()
        self.assertEqual(names.count('stations.json'), 1)
        self.assertEqual(names.count('room-variants.json'), 1)
        self.assertEqual(self.controller.snapshot()['stations'], first['stations'])

    def test_a_changed_file_is_noticed_even_with_the_same_size(self):
        station = STATION
        self.settled('stations.json', [station])
        before = self.controller.snapshot()['stations']
        self.settled('stations.json', [dict(station, name='Two')])
        after = self.controller.snapshot()['stations']
        self.assertNotEqual(before, after)
        self.assertEqual(after[0]['name'], 'Two')

    def test_a_just_written_file_is_re_read_until_it_settles(self):
        hub.save_json(Path(self.tmp.name) / 'stations.json', [])
        names = self.reads()
        self.controller.snapshot()
        self.controller.snapshot()
        self.assertEqual(names.count('stations.json'), 2)

    def test_a_missing_file_is_cheap_and_appears_when_created(self):
        names = self.reads()
        self.assertEqual(self.controller.snapshot()['stations'], [])
        self.controller.snapshot()
        self.assertEqual(names.count('stations.json'), 1)
        self.settled('stations.json', [STATION])
        self.assertEqual(len(self.controller.snapshot()['stations']), 1)

    def test_snapshots_are_copies(self):
        self.settled('stations.json', [STATION])
        self.controller.snapshot()['stations'][0]['name'] = 'changed'
        self.assertEqual(self.controller.snapshot()['stations'][0]['name'], 'One')


class FlakyBackend(FakeBackend):
    def __init__(self, failures):
        super().__init__()
        self.failures = list(failures)

    def layers(self):
        if self.failures:
            raise self.failures.pop(0)
        return super().layers()


class RefreshLoopTests(Environment):
    def run_loop(self, controller, until):
        stopping = threading.Event()
        thread = threading.Thread(target=session_service.refresh_loop, args=(controller, stopping, .01), daemon=True)
        thread.start()
        try:
            deadline = threading.Event()
            for _ in range(300):
                if until():
                    break
                deadline.wait(.01)
            alive = thread.is_alive()
        finally:
            stopping.set()
            thread.join(2)
        return alive

    def test_the_loop_survives_unexpected_errors_logs_each_once_and_recovers(self):
        backend = FlakyBackend([AttributeError('x'), AttributeError('x'), KeyError('k'), TypeError('t')])
        controller = sessions.Controller(backend, self.tmp.name, clock=lambda: self.now)
        self.addCleanup(controller.close)
        seen = []
        with self.assertLogs(session_service.LOG, 'ERROR') as logged:
            alive = self.run_loop(controller, lambda: (seen.append(controller.errors.get('refresh')), not backend.failures
                                                       and 'refresh' not in controller.errors)[1])
        self.assertTrue(alive)
        self.assertIn("AttributeError: x", seen)
        self.assertIn("TypeError: t", seen)
        self.assertNotIn('refresh', controller.errors)
        messages = [record.getMessage() for record in logged.records]
        self.assertEqual(len([m for m in messages if 'AttributeError: x' in m]), 1)
        self.assertEqual(len([m for m in messages if 'KeyError' in m]), 1)
        self.assertEqual(len([m for m in messages if 'TypeError' in m]), 1)

    def test_a_failing_policy_poll_does_not_end_the_loop(self):
        controller = sessions.Controller(FakeBackend(), self.tmp.name, clock=lambda: self.now)
        self.addCleanup(controller.close)
        polls = []

        def poll():
            polls.append(1)
            raise RuntimeError('policy')
        controller.poll_policy = poll
        with self.assertLogs(session_service.LOG, 'ERROR'):
            alive = self.run_loop(controller, lambda: len(polls) >= 3)
        self.assertTrue(alive)
        self.assertGreaterEqual(len(polls), 3)
        self.assertEqual(controller.policy_status, {'error': 'policy'})

    def test_the_loop_stops_when_asked(self):
        controller = sessions.Controller(FakeBackend(), self.tmp.name, clock=lambda: self.now)
        self.addCleanup(controller.close)
        stopping = threading.Event()
        stopping.set()
        session_service.refresh_loop(controller, stopping, .01)


if __name__ == '__main__':
    unittest.main()
