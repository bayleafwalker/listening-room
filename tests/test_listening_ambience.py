import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

source = os.environ.get('LISTENING_AMBIENCE_SOURCE', Path(__file__).resolve().parents[1] / 'src/ambience.py')
spec = importlib.util.spec_from_file_location('ambience', source)
ambience = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ambience)


class AmbienceTests(unittest.TestCase):
    def test_silent_mode_disables_audio_and_idle_inhibition(self):
        command = ambience.command_line({'name': 'Vista'}, 'ambience', '/mpv', '/private/socket')
        self.assertIn('--no-audio', command)
        self.assertIn('--aid=no', command)
        self.assertIn('--stop-screensaver=no', command)
        self.assertIn('--loop-file=inf', command)
        for option in ('--osc=no', '--osd-level=0', '--osd-bar=no', '--border=no', '--cursor-autohide=always'):
            self.assertIn(option, command)
        self.assertTrue(any('ListeningAmbience' in value for value in command))

    def test_video_mode_has_independent_identity_and_audio(self):
        command = ambience.command_line({'name': 'Video'}, 'video', '/mpv', '/private/socket')
        self.assertNotIn('--no-audio', command)
        self.assertNotIn('--osc=no', command)
        self.assertNotIn('--border=no', command)
        self.assertIn('--stop-screensaver=yes', command)
        self.assertTrue(any('ListeningVideo' in value for value in command))

    def test_resolver_is_absolute_for_systemd_environment(self):
        with mock.patch.object(ambience.shutil, 'which', return_value='/packaged/bin/yt-dlp'):
            command = ambience.command_line({'name': 'Video'}, 'video', '/mpv', '/private/socket')
        self.assertIn('--script-opts=ytdl_hook-ytdl_path=/packaged/bin/yt-dlp', command)

    def test_custom_url_validation(self):
        for url in ('file:///etc/passwd', 'javascript:alert(1)', '--input-conf=evil', 'https://user:pass@example.com/', 'https://example.com/?access_token=secret', 'https://example.com/\nmore'):
            with self.subTest(url=url), self.assertRaises(ambience.AmbienceError):
                ambience.source(url)
        self.assertEqual(ambience.source('https://www.youtube.com/watch?v=VaLgmb5aGjo')['id'], 'custom')

    def test_missing_local_file_is_actionable(self):
        with self.assertRaisesRegex(ambience.AmbienceError, 'unavailable'):
            ambience.source('/missing/view.mp4')

    def test_video_volume_is_bounded_and_silent_volume_is_rejected(self):
        for volume in (-1, 101, float('nan'), float('inf')):
            with self.subTest(volume=volume), self.assertRaises(ambience.AmbienceError):
                ambience.control('volume', 'video', volume)
        with self.assertRaises(ambience.AmbienceError):
            ambience.control('volume', 'ambience', 50)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ambience, 'STATE_HOME', Path(directory)), mock.patch.object(ambience, '_ipc') as ipc:
            ambience.control('volume', 'video', 35)
            ipc.assert_any_call(['set_property', 'volume', 35.0], 'video')
            self.assertEqual(ambience._saved()['video']['audio']['volume'], 35)

    def test_video_audio_survives_failed_start_and_absent_player(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ambience, 'STATE_HOME', Path(directory)), mock.patch.object(ambience, '_runtime', return_value=Path(directory)), mock.patch.object(ambience.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(ambience, '_run') as run, mock.patch.object(ambience, '_ipc') as ipc:
            run.return_value.returncode = 0
            ipc.side_effect = [23, True]
            with mock.patch.object(ambience.socket, 'socket', side_effect=OSError('failed')):
                with self.assertRaises(ambience.AmbienceError):
                    ambience.start('https://example.org/video', 'video')
            launch = run.call_args_list[1].args[0]
            self.assertIn('--volume=23', launch)
            self.assertIn('--mute=yes', launch)
            ipc.side_effect = OSError('absent')
            run.reset_mock()
            with mock.patch.object(ambience.socket, 'socket', side_effect=OSError('failed')):
                with self.assertRaises(ambience.AmbienceError):
                    ambience.start('https://example.org/other', 'video')
            launch = run.call_args_list[1].args[0]
            self.assertIn('--volume=23', launch)
            self.assertIn('--mute=yes', launch)

    def test_video_controls_remember_volume_and_mute(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ambience, 'STATE_HOME', Path(directory)), mock.patch.object(ambience, '_ipc') as ipc:
            def command(args, mode):
                if args == ['get_property', 'volume']:
                    return 24
                if args == ['get_property', 'mute']:
                    return True
            ipc.side_effect = command
            ambience.control('volume', 'video', 42)
            self.assertEqual(ambience._saved()['video']['audio'], {'volume': 42, 'muted': True})
            ambience.control('mute', 'video')
            self.assertEqual(ambience._saved()['video']['audio'], {'volume': 24, 'muted': False})

    def test_stopped_socket_is_not_reported_as_running(self):
        with mock.patch.object(ambience, '_ipc', side_effect=OSError('not running')):
            self.assertIsNone(ambience.state('video'))

    def test_failed_replacement_keeps_existing_window_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / 'ambience.sock'
            socket_path.write_bytes(b'existing control socket')
            with mock.patch.object(ambience, '_runtime', return_value=Path(directory)), mock.patch.object(ambience.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(ambience, '_run') as run:
                run.side_effect = [mock.Mock(returncode=1), mock.Mock(returncode=0, stdout='loaded\n')]
                with self.assertRaisesRegex(ambience.AmbienceError, 'preserved'):
                    ambience.start('https://example.org/video')
            self.assertEqual(socket_path.read_bytes(), b'existing control socket')

    def test_bring_here_moves_only_the_owned_window_without_workspace_switch(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ambience, 'STATE_HOME', Path(directory)), mock.patch.object(ambience, '_window', return_value={'address': '0x123', 'pinned': False}), mock.patch.object(ambience, '_run') as run:
            run.return_value.stdout = json.dumps({'id': 4})
            ambience.place('here', 'ambience')
            self.assertEqual(run.call_args.args[0], ['hyprctl', 'dispatch', 'hl.dsp.window.move({workspace=4,follow=false,window="address:0x123"})'])
            self.assertEqual(ambience._saved()['ambience']['placement'], 'here')

    def test_pip_uses_current_workspace_small_float_and_pin(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ambience, 'STATE_HOME', Path(directory)), mock.patch.object(ambience, '_window', return_value={'address': '0x321', 'floating': False}), mock.patch.object(ambience, '_run') as run:
            run.return_value.stdout = json.dumps({'id': 4})
            ambience.place('pip', 'video')
            commands=[call.args[0][-1] for call in run.call_args_list]
            self.assertIn('hl.dsp.window.move({workspace=4,follow=false,window="address:0x321"})', commands)
            self.assertIn('hl.dsp.window.resize({x=640,y=360,relative=false,window="address:0x321"})', commands)
            self.assertEqual(commands[-1], 'hl.dsp.window.pin({window="address:0x321"})')
            self.assertEqual(ambience._saved()['video']['placement'], 'pip')

    def test_pip_keeps_an_already_floating_window_floating(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ambience, 'STATE_HOME', Path(directory)), mock.patch.object(ambience, '_window', return_value={'address': '0x321', 'floating': True}), mock.patch.object(ambience, '_run') as run:
            run.return_value.stdout = json.dumps({'id': 4})
            ambience.place('pip', 'ambience')
            self.assertFalse(any('window.float' in call.args[0][-1] for call in run.call_args_list))

    def test_pin_makes_only_the_owned_window_floating(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ambience, 'STATE_HOME', Path(directory)), mock.patch.object(ambience, '_window', return_value={'address': '0x321', 'floating': False}), mock.patch.object(ambience, '_run') as run:
            ambience.place('pin', 'video')
            self.assertEqual(run.call_args_list[0].args[0], ['hyprctl', 'dispatch', 'hl.dsp.window.float({action="set",window="address:0x321"})'])
            self.assertEqual(run.call_args_list[1].args[0], ['hyprctl', 'dispatch', 'hl.dsp.window.pin({window="address:0x321"})'])


if __name__ == '__main__':
    unittest.main()
