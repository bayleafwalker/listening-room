"""Background lane handover regressions using fake mpv/systemd and private state."""
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import audio_devices
import audio_layers as layers


class OverlaySwapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        runtime = root / 'runtime'
        runtime.mkdir(mode=0o700)
        env = patch.dict(os.environ, {'HOME': str(root), 'XDG_CONFIG_HOME': str(root/'config'),
            'XDG_STATE_HOME': str(root/'state'), 'XDG_CACHE_HOME': str(root/'cache'),
            'XDG_RUNTIME_DIR': str(runtime)})
        env.start()
        self.addCleanup(env.stop)
        self.old = {'unit': 'listening-hub-overlay-aaaaaaaaaaaa.service', 'socket': 'overlay-aaaaaaaaaaaa.sock'}
        self.states = {self.old['socket']: {'pause': False, 'mute': False, 'volume': 32.0, 'alive': True}}
        self.events = []
        self.overlap = False
        self.ready = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failure = None
        layers._save_player(self.old)
        layers._save_source({'kind': 'twitch', 'url': 'https://twitch.tv/old'})
        layers._save_settings({'volume': 32, 'muted': False})
        self.original_save = layers._save_source
        for target, name, replacement in ((layers.shutil, 'which', lambda _: '/fake/player'),
                (audio_devices, 'player_options', lambda: []), (layers, '_run', self.run_service),
                (layers, '_ipc', self.ipc), (layers, 'state', self.state),
                (layers.socket, 'socket', lambda *_: self.Socket(self))):
            stub = patch.object(target, name, side_effect=replacement)
            stub.start()
            self.addCleanup(stub.stop)

    class Socket:
        def __init__(self, fixture):
            self.fixture = fixture
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def connect(self, _path): pass
        def settimeout(self, _timeout): pass
        def sendall(self, _payload): pass
        def recv(self, _size):
            self.fixture.ready.set()
            if not self.fixture.release.wait(3):
                raise AssertionError('fixture did not release candidate')
            if self.fixture.failure == 'load':
                return b'{"request_id":1,"error":"load failed"}\n'
            return b'{"event":"file-loaded"}\n{"event":"playback-restart"}\n'

    def run_service(self, args, check=True):
        self.events.append(tuple(args))
        if args[0] == 'systemd-run':
            unit = next(x.split('=', 1)[1] for x in args if x.startswith('--unit='))
            ident = unit.removeprefix('listening-hub-overlay-').removesuffix('.service')
            self.states['overlay-' + ident + '.sock'] = {'pause': False, 'mute': True, 'volume': 0, 'alive': True}
        elif args[0] == 'systemctl' and 'stop' in args:
            unit = args[-1]
            if unit == self.old['unit'] and self.failure == 'retire':
                raise layers.LayerError('fixture old service stop failed')
            for name, state in self.states.items():
                if name in ('overlay.sock', unit.replace('listening-hub-overlay-', 'overlay-').replace('.service', '.sock')):
                    state['alive'] = False
        return SimpleNamespace(returncode=0, stdout='')

    def ipc(self, command, socket_path=None):
        name = Path(socket_path).name if socket_path else layers._player()['socket']
        player = self.states.get(name)
        if not player or not player['alive']:
            raise layers.LayerError('fixture player unavailable')
        self.events.append(('ipc', name, *command))
        if self.failure == 'preflight' and name != self.old['socket']:
            raise layers.LayerError('fixture preflight failed')
        if command[0] == 'set_property':
            if name != self.old['socket'] and command[1] == 'mute' and command[2] is False:
                old = self.states[self.old['socket']]
                self.overlap |= bool(old['alive'] and not old['pause'] and not old['mute'])
            player[command[1]] = command[2]
        return None

    def state(self):
        name = layers._player()['socket']
        value = self.states.get(name)
        if not value or not value['alive']:
            return None
        return {'status': 'Paused' if value['pause'] else 'Playing', 'volume': value['volume'], 'muted': value['mute']}

    def launch(self):
        errors = []
        def target():
            try:
                layers.start_layer('https://twitch.tv/new')
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=target)
        thread.start()
        self.assertTrue(self.ready.wait(2))
        return thread, errors

    def assert_old_preserved(self):
        self.assertEqual(layers._player()['unit'], self.old['unit'])
        self.assertEqual(layers._player()['socket'], self.old['socket'])
        self.assertEqual(layers._source()['source_id'], 'old')
        self.assertEqual(layers._settings(), {'volume': 32, 'muted': False})
        self.assertTrue(self.states[self.old['socket']]['alive'])
        self.assertFalse(self.states[self.old['socket']]['pause'])

    def test_successful_handover_never_unmutes_both_players(self):
        layers.start_layer('https://twitch.tv/new')
        self.assertFalse(self.overlap)
        self.assertEqual(layers._source()['source_id'], 'new')
        self.assertNotEqual(layers._player()['unit'], self.old['unit'])
        self.assertTrue(self.state())

    def test_failed_load_and_preflight_leave_old_playing(self):
        for phase in ('load', 'preflight'):
            with self.subTest(phase=phase):
                self.failure = phase
                with self.assertRaises(layers.LayerError):
                    layers.start_layer('https://twitch.tv/new')
                self.assert_old_preserved()

    def test_failed_source_persistence_rolls_back_previous(self):
        calls = []
        def save(value):
            calls.append(value)
            if value.get('url') == 'https://twitch.tv/new':
                raise OSError('fixture persistence failure')
            return self.original_save(value)
        with patch.object(layers, '_save_source', side_effect=save):
            with self.assertRaises(layers.LayerError):
                layers.start_layer('https://twitch.tv/new')
        self.assertTrue(calls)
        self.assert_old_preserved()

    def test_failed_settings_and_pointer_persistence_restore_old(self):
        for name in ('_save_settings', '_save_player'):
            with self.subTest(operation=name):
                original = getattr(layers, name)
                failed = False
                def fail_once(value):
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise OSError('fixture persistence failure')
                    return original(value)
                with patch.object(layers, name, side_effect=fail_once):
                    with self.assertRaises(layers.LayerError):
                        layers.start_layer('https://twitch.tv/new')
                self.assert_old_preserved()

    def test_quiet_during_load_does_not_restart_background(self):
        self.release.clear()
        worker, errors = self.launch()
        layers.control('pause')
        self.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        self.assertEqual(layers._player()['unit'], self.old['unit'])
        self.assertTrue(self.states[self.old['socket']]['pause'])
        self.assertNotEqual(layers._source().get('source_id'), 'new')

    def test_stop_during_load_cancels_candidate(self):
        self.release.clear()
        worker, errors = self.launch()
        layers.control('stop')
        self.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        self.assertFalse(self.states[self.old['socket']]['alive'])
        self.assertNotEqual(layers._source().get('source_id'), 'new')

    def test_stop_from_another_process_cancels_pending_candidate(self):
        self.release.clear()
        worker, errors = self.launch()
        child = multiprocessing.get_context('fork').Process(target=layers.control, args=('stop',))
        child.start()
        child.join(3)
        self.assertEqual(child.exitcode, 0)
        self.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors, 'a stop in another process must invalidate the loading candidate')
        self.assertNotEqual(layers._source().get('source_id'), 'new')

    def test_volume_change_during_load_applies_to_new_candidate(self):
        self.release.clear()
        worker, errors = self.launch()
        layers.control('volume', 57)
        self.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(layers._settings()['volume'], 57)
        self.assertEqual(self.state()['volume'], 57)

    def test_retirement_failure_keeps_new_candidate_and_old_paused(self):
        self.failure = 'retire'
        layers.start_layer('https://twitch.tv/new')
        self.assertNotEqual(layers._player()['unit'], self.old['unit'])
        self.assertEqual(layers._source()['source_id'], 'new')
        self.assertTrue(self.state())
        self.assertTrue(self.states[self.old['socket']]['pause'])
        self.assertIn(self.old['unit'], [x['unit'] for x in layers._player()['retiring']])
        self.failure = None
        layers.control('stop')
        self.assertFalse(self.states[self.old['socket']]['alive'])

    def test_retired_socket_cleanup_failure_keeps_new_player_running(self):
        unlink = Path.unlink
        def fail_old_socket(path, *args, **kwargs):
            if path.name == self.old['socket']:
                raise OSError('fixture old socket cleanup failed')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=fail_old_socket):
            layers.start_layer('https://twitch.tv/new')
        self.assertNotEqual(layers._player()['unit'], self.old['unit'])
        self.assertTrue(self.state())
        self.assertEqual(layers._source()['source_id'], 'new')

    def test_direct_signed_urls_rejected_before_launch_or_persistence(self):
        with self.assertRaises(layers.LayerError):
            layers.start_source({'kind': 'radio', 'url': 'https://radio.example/audio?signature=fixture-secret'})
        self.assertFalse(any(event[0] == 'systemd-run' for event in self.events))
        self.assertNotIn('fixture-secret', str(layers.audio_settings()))
        self.assert_old_preserved()


if __name__ == '__main__':
    unittest.main()
