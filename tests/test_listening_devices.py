"""Audio policy must never move or quiet an unrelated application."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import audio_devices as devices


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.environment=patch.dict(os.environ,{'XDG_CONFIG_HOME':self.directory.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    @staticmethod
    def stream(index,name='ListeningRoomMusic',pid='123',binary='mpv'):
        return {'index':index,'sink':1,'properties':{'application.name':name,
                'application.process.id':pid,'application.process.binary':binary}}

    def test_routes_only_confirmed_owned_streams_and_never_default(self):
        streams=[self.stream(1),self.stream(2,'Firefox'),self.stream(3,binary='other'),
                 self.stream(4,pid='999'),self.stream(5,'ListeningRoomAtmosphere')]
        with patch.object(devices,'outputs',return_value=[{'name':'headphones'}]), \
                patch.object(devices,'_list',return_value=streams), \
                patch.object(devices,'_pid_owned',side_effect=lambda pid,_:pid=='123'), \
                patch.object(devices,'_run',return_value='') as run:
            result=devices.route('headphones')
        self.assertEqual(result['moved'],[1,5])
        self.assertEqual([call.args[0] for call in run.call_args_list],[
            ['pactl','move-sink-input','1','headphones'],['pactl','move-sink-input','5','headphones']])
        self.assertEqual(devices.settings()['preferred_output'],'headphones')

    def test_rejects_absent_sink_before_moving_or_saving(self):
        with patch.object(devices,'outputs',return_value=[]), patch.object(devices,'_run') as run:
            with self.assertRaisesRegex(devices.DeviceError,'unavailable'):
                devices.route('missing; rm -rf /')
        run.assert_not_called()
        self.assertIsNone(devices.settings()['preferred_output'])

    def test_malformed_device_responses_fail_closed(self):
        for response in ('{}','null','[1]','bad'):
            with patch.object(devices,'_run',return_value=response), self.assertRaises(devices.DeviceError):
                devices.outputs()

    def test_outputs_use_stable_names(self):
        with patch.object(devices,'_run',side_effect=[json.dumps([
                {'index':77,'name':'alsa_output.usb_abc','description':'Headphones'},
                {'index':78,'name':'invalid\nname'}]),'alsa_output.usb_abc\n']):
            result=devices.outputs()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['id'],'alsa_output.usb_abc')
        self.assertTrue(result[0]['default'])

    def test_player_options_avoid_default_speaker_leak(self):
        devices.configure(preferred_output='headphones',pause_on_disconnect=True)
        with patch.object(devices,'outputs',return_value=[{'name':'headphones'}]):
            self.assertEqual(devices.player_options(),['--audio-device=pulse/headphones'])
        with patch.object(devices,'outputs',return_value=[]), self.assertRaisesRegex(devices.DeviceError,'disconnected'):
            devices.player_options()
        devices.configure(pause_on_disconnect=False)
        with patch.object(devices,'outputs',return_value=[]):
            self.assertEqual(devices.player_options(),[])

    def test_missing_preferred_output_service_fails_closed_for_pause_policy(self):
        devices.configure(preferred_output='headphones',pause_on_disconnect=True)
        with patch.object(devices,'outputs',side_effect=devices.DeviceError('unavailable')), \
                self.assertRaisesRegex(devices.DeviceError,'confirm'):
            devices.player_options()

    def test_call_detection_requires_explicit_active_communication_role(self):
        self.assertFalse(devices._call_active([{'properties':{'media.role':'Music'}}]))
        self.assertFalse(devices._call_active([{'corked':True,'properties':{'media.role':'Communication'}}]))
        self.assertFalse(devices._call_active([{'properties':{'application.name':'Zoom'}}]))
        self.assertTrue(devices._call_active([{'corked':False,'properties':{'media.role':'Communication'}}]))

    def test_policy_reports_disconnect_and_call_without_mutation(self):
        devices.configure(preferred_output='headphones',pause_on_disconnect=True,duck_on_call=True)
        with patch.object(devices,'outputs',return_value=[]), \
                patch.object(devices,'_list',side_effect=lambda kind:[{'properties':{'media.role':'Communication'}}]
                             if kind=='source-outputs' else []), \
                patch.object(devices,'_run') as run:
            result=devices.poll_policy()
        self.assertTrue(result['disconnect'])
        self.assertTrue(result['duck'])
        run.assert_not_called()

    def test_failed_route_does_not_replace_preference(self):
        devices.configure(preferred_output='old')
        with patch.object(devices,'outputs',return_value=[{'name':'new'}]), \
                patch.object(devices,'_list',return_value=[self.stream(1)]), \
                patch.object(devices,'_pid_owned',return_value=True), \
                patch.object(devices,'_run',side_effect=devices.DeviceError('failed')):
            with self.assertRaises(devices.DeviceError):
                devices.route('new')
        self.assertEqual(devices.settings()['preferred_output'],'old')

    def test_test_tone_is_explicit_quiet_and_routes_only_itself(self):
        with patch.object(devices,'outputs',return_value=[{'name':'headphones','default':True}]), \
                patch.object(devices.shutil,'which',return_value='/bin/mpv'), \
                patch.object(devices,'_run',return_value='') as run:
            devices.test_sound('headphones')
        command=run.call_args.args[0]
        self.assertIn('--audio-device=pulse/headphones',command)
        self.assertIn('--volume=25',command)
        self.assertFalse(any('set-default' in arg or 'set-volume' in arg for arg in command))


if __name__ == '__main__':
    unittest.main()
