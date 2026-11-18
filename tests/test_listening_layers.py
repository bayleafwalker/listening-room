import os
import threading
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import audio_layers as layers


class LayersTests(unittest.TestCase):
    def test_cancel_at_readiness_stops_own_player(self):
        import io,json
        cancel=threading.Event()
        events=[{'event':'file-loaded'},{'event':'playback-restart'}]
        connection=unittest.mock.MagicMock()
        connection.__enter__.return_value=connection
        def receive(_):
            cancel.set()
            return b''.join(json.dumps(x).encode()+b'\n' for x in events)
        connection.recv.side_effect=receive
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_RUNTIME_DIR': folder, 'XDG_STATE_HOME': folder}), patch.object(layers.shutil,'which',return_value='/bin/test'), patch.object(layers,'state',return_value=None), patch.object(layers.socket,'socket',return_value=connection), patch.object(layers,'_run',return_value=SimpleNamespace(returncode=0,stdout='')) as run:
            with self.assertRaisesRegex(layers.LayerError,'cancelled'):
                layers.start_layer('https://twitch.tv/name',cancel_event=cancel)
        launch = next(call.args[0] for call in run.call_args_list if call.args[0][0] == 'systemd-run')
        candidate = next(arg.split('=', 1)[1] for arg in launch if arg.startswith('--unit='))
        self.assertNotEqual(candidate, layers.UNIT)
        self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'stop', candidate])

    def test_cancelled_start_has_no_effects(self):
        cancel=threading.Event();cancel.set()
        with patch.object(layers, '_run') as run, patch.object(layers, '_save_settings') as save:
            with self.assertRaisesRegex(layers.LayerError, 'cancelled'):
                layers.start_layer('https://twitch.tv/name', cancel_event=cancel)
        run.assert_not_called();save.assert_not_called()

    def test_rejects_unsafe_urls_and_bad_volume_before_side_effects(self):
        for url in ['https://twitch.tv/user?token=abc', 'https://evil.example/user',
                    'https://user:pass@twitch.tv/name', 'https://twitch.tv/a/b']:
            with patch.object(layers, '_run') as run, self.assertRaises(layers.LayerError):
                layers.start_layer(url)
            run.assert_not_called()
        with patch.object(layers, '_run') as run, self.assertRaises(layers.LayerError):
            layers.start_layer('https://www.twitch.tv/name', float('nan'))
        run.assert_not_called()

    def test_controls_target_only_overlay(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_STATE_HOME': folder}), \
             patch.object(layers, 'state', return_value={'volume': 20, 'muted': False}), \
             patch.object(layers, '_ipc') as ipc, patch.object(layers, '_run') as run:
            run.return_value.returncode = 0
            layers.control('volume', 25)
            ipc.assert_called_once_with(['set_property', 'volume', 25.0])
            layers.control('stop')
            run.assert_called_once_with(['systemctl', '--user', 'stop', layers.UNIT], check=False)

    def test_missing_player_does_not_stop_anything(self):
        with patch.object(layers.shutil, 'which', return_value=None), patch.object(layers, '_run') as run:
            with self.assertRaises(layers.LayerError):
                layers.start_layer('https://www.twitch.tv/name')
            run.assert_not_called()

    def test_start_failure_stops_only_own_unit(self):
        ok = SimpleNamespace(returncode=0, stdout='')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_RUNTIME_DIR': folder, 'XDG_STATE_HOME': folder}), \
             patch.object(layers.shutil, 'which', return_value='/bin/test'), \
             patch.object(layers, '_run', return_value=ok) as run:
            with self.assertRaises(layers.LayerError):
                layers.start_layer('https://twitch.tv/name', timeout=0)
        stops = [call.args[0] for call in run.call_args_list if call.args[0][0] == 'systemctl']
        launch = next(call.args[0] for call in run.call_args_list if call.args[0][0] == 'systemd-run')
        candidate = next(arg.split('=', 1)[1] for arg in launch if arg.startswith('--unit='))
        self.assertEqual(stops, [['systemctl', '--user', 'stop', candidate]])
        self.assertNotEqual(candidate, layers.UNIT, 'A failed candidate must preserve the previous player')

    def test_remembers_settings_while_stopped(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_STATE_HOME': folder}), \
             patch.object(layers, 'state', return_value=None), patch.object(layers, '_ipc') as ipc:
            layers.control('volume', 37)
            layers.control('mute')
            self.assertEqual(layers._settings(), {'volume': 37, 'muted': True})
            ipc.assert_not_called()

    def test_replacement_uses_active_layer_settings_before_stopping(self):
        ok = SimpleNamespace(returncode=0, stdout='')
        connection = unittest.mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.return_value = b'{"event":"file-loaded"}\n{"event":"playback-restart"}\n'
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {'XDG_RUNTIME_DIR': folder, 'XDG_STATE_HOME': folder}), \
             patch.object(layers, 'state', return_value={'volume': 43, 'muted': True}), \
             patch.object(layers.socket, 'socket', return_value=connection), \
             patch.object(layers, '_ipc') as ipc, \
             patch.object(layers.shutil, 'which', return_value='/bin/test'), \
             patch.object(layers, '_run', return_value=ok) as run:
            layers.start_layer('https://twitch.tv/name')
            args = next(call.args[0] for call in run.call_args_list if call.args[0][0] == 'systemd-run')
            self.assertIn('--volume=0', args)
            self.assertIn('--mute=yes', args)
            self.assertIn(['set_property', 'volume', 43], [call.args[0] for call in ipc.call_args_list])
            self.assertEqual(layers._settings(), {'volume': 43, 'muted': True})

    def test_explicit_volume_preserves_mute(self):
        ok = SimpleNamespace(returncode=0, stdout='')
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {'XDG_RUNTIME_DIR': folder, 'XDG_STATE_HOME': folder}), \
             patch.object(layers, 'state', return_value={'volume': 43, 'muted': True}), \
             patch.object(layers.shutil, 'which', return_value='/bin/test'), \
             patch.object(layers, '_run', return_value=ok) as run:
            with self.assertRaises(layers.LayerError):
                layers.start_layer('https://twitch.tv/name', volume=12, timeout=0)
            args = next(call.args[0] for call in run.call_args_list if call.args[0][0] == 'systemd-run')
            self.assertIn('--volume=0', args)
            self.assertIn('--mute=yes', args)

    def test_dead_socket_has_no_fallback(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_RUNTIME_DIR': folder}):
            self.assertIsNone(layers.state())
            with self.assertRaises(layers.LayerError):
                layers.control('pause')

    def test_private_runtime_required(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_RUNTIME_DIR': folder}):
            path = Path(folder) / 'listening-hub'
            path.mkdir(mode=0o755)
            with self.assertRaises(layers.LayerError):
                layers._runtime()


if __name__ == '__main__':
    unittest.main()
