"""Signal quality and owned local-atmosphere lifecycle checks."""
from array import array
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import audio_layers as layers
import atmosphere_catalogue as catalogue
import render_atmospheres as render
import audio_devices


class AtmosphereTests(unittest.TestCase):
    def test_deterministic_signal_energy_peak_and_loop_seam(self):
        for item in catalogue.SOUNDS:
            with self.subTest(sound=item['id']):
                samples = render.render(item['id'], rate=8000, seconds=4)
                self.assertEqual(samples, render.render(item['id'], rate=8000, seconds=4))
                self.assertEqual(len(samples), 32000)
                rms = math.sqrt(sum(x * x for x in samples) / len(samples)) / 32767
                self.assertGreater(rms, .05)
                self.assertLess(rms, .12)
                self.assertLess(max(abs(x) for x in samples), 32767 * .66)
                self.assertLess(abs(sum(samples) / len(samples)), 1)
                differences = [samples[i] - samples[i-1] for i in range(1, len(samples))]
                derivative_rms = math.sqrt(sum(x*x for x in differences) / len(differences))
                self.assertLess(abs(samples[0] - samples[-1]), max(100, 6 * derivative_rms))

    def test_thunder_is_softer_in_high_frequencies_than_rain(self):
        def roughness(identity):
            samples = render.render(identity, rate=12000, seconds=4)
            return sum((a-b)**2 for a,b in zip(samples,samples[1:])) / sum(x*x for x in samples)
        self.assertLess(roughness('distant-thunder'), roughness('rain-glass') * .5)

    def test_wav_provenance_and_missing_asset_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LISTENING_HUB_ASSETS':folder}):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                catalogue.source('rain-glass')
            manifest = render.write_assets(folder, rate=8000, seconds=2)
            self.assertEqual(len(manifest['sounds']), 6)
            self.assertEqual(manifest['license'], 'CC0-1.0')
            for item in catalogue.presets():
                self.assertTrue(item['available'])
                with wave.open(item['url']) as source:
                    self.assertEqual(source.getparams()[:4], (1, 2, 8000, 16000))
            with self.assertRaisesRegex(ValueError, 'Choose'):
                catalogue.source('../../etc/passwd')

    def test_local_start_needs_no_resolver_and_confirms_playback(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.return_value = b'{"event":"file-loaded"}\n{"event":"playback-restart"}\n'
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'LISTENING_HUB_ASSETS':folder,'XDG_RUNTIME_DIR':folder,'XDG_STATE_HOME':folder}), \
                patch.object(layers.shutil,'which',return_value='/bin/mpv') as which, \
                patch.object(layers,'state',return_value=None), \
                patch.object(layers,'_ipc') as ipc, \
                patch.object(layers.socket,'socket',return_value=connection), \
                patch.object(layers,'_run',return_value=SimpleNamespace(returncode=0,stdout='')) as run:
            render.write_assets(folder, rate=8000, seconds=2)
            layers.start_local('rain-glass', volume=30, window_open=0)
            which.assert_called_once_with('mpv')
            launch = next(call.args[0] for call in run.call_args_list if call.args[0][0] == 'systemd-run')
            self.assertIn('--loop-file=inf', launch)
            self.assertIn('--ytdl=no', launch)
            self.assertIn('--audio-client-name=ListeningRoomAtmosphere', launch)
            self.assertIn('--af=@room-window:lavfi=[lowpass=f=900]', launch)
            self.assertIn('--volume=0', launch)
            levels = [call.args[0][2] for call in ipc.call_args_list if call.args[0][:2] == ['set_property', 'volume']]
            self.assertAlmostEqual(levels[-1], 5.4)
            self.assertEqual(layers.audio_settings()['source_id'], 'rain-glass')
            self.assertEqual(layers.audio_settings()['volume'], 30)

    def test_window_control_preserves_base_volume_and_is_outdoor_only(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_STATE_HOME':folder,'XDG_RUNTIME_DIR':folder}), \
                patch.object(layers,'state',return_value={'volume':40,'muted':False}), \
                patch.object(layers,'_ipc') as ipc, patch.object(layers.time,'sleep'):
            layers._save_settings({'volume':40,'muted':False})
            layers._save_source({'kind':'local','source_id':'rain-glass','window_open':100})
            layers.control('window', 0)
            self.assertEqual(layers.audio_settings()['volume'],40)
            self.assertEqual(layers.audio_settings()['window_open'],0)
            self.assertAlmostEqual(ipc.call_args.args[0][2], 7.2)
            layers.control('volume',20)
            self.assertAlmostEqual(ipc.call_args.args[0][2],3.6)
            layers._save_source({'kind':'local','source_id':'fireplace'})
            with self.assertRaisesRegex(layers.LayerError,'outdoor'):
                layers.control('window',40)

    def test_local_snapshot_reports_both_actual_and_base_volume(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_STATE_HOME':folder}), \
                patch.object(layers,'_ipc',side_effect=[False,False,'file.wav',7.2,False]):
            layers._save_settings({'volume':40,'muted':False})
            layers._save_source({'kind':'local','source_id':'rain-glass','window_open':0})
            result=layers.state()
            self.assertEqual(result['volume'],40)
            self.assertEqual(result['effective_volume'],7.2)
            self.assertEqual(result['title'],'Rain on glass')

    def test_bad_window_and_missing_local_sound_have_no_playback_effect(self):
        with patch.object(layers,'_run') as run, patch.object(layers,'_ipc') as ipc:
            for bad in (-1,101,float('nan')):
                with self.assertRaises(layers.LayerError):
                    layers.control('window',bad)
            with self.assertRaises(layers.LayerError):
                layers.start_local('unknown')
            run.assert_not_called()
            ipc.assert_not_called()

    def test_stop_invalidates_loading_even_without_external_cancel_token(self):
        ok=SimpleNamespace(returncode=0,stdout='')
        def service(args,check=True):
            if args[0]=='systemd-run':
                layers.control('stop')
            return ok
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{
                'XDG_RUNTIME_DIR':folder,'XDG_STATE_HOME':folder}), \
                patch.object(layers,'state',return_value=None), \
                patch.object(layers.shutil,'which',return_value='/bin/mpv'), \
                patch.object(layers,'_run',side_effect=service), \
                patch.object(layers,'_save_source') as source:
            with self.assertRaisesRegex(layers.LayerError,'cancelled'):
                layers.start_layer('https://twitch.tv/first')
        source.assert_not_called()

    def test_missing_preferred_headphones_preserves_existing_overlay(self):
        with patch.object(audio_devices,'player_options',side_effect=audio_devices.DeviceError('disconnected')), \
                patch.object(layers.shutil,'which',return_value='/bin/mpv'), \
                patch.object(layers,'_run') as run:
            with self.assertRaisesRegex(layers.LayerError,'disconnected'):
                layers.start_layer('https://twitch.tv/music')
        run.assert_not_called()

    def test_replacement_waits_for_abandoned_load_cleanup(self):
        first_started=threading.Event();release_first=threading.Event()
        history=[];errors=[]
        connection=MagicMock()
        connection.__enter__.return_value=connection
        connection.recv.return_value=b'{"event":"file-loaded"}\n{"event":"playback-restart"}\n'
        def service(args,check=True):
            history.append(args[0])
            if args[0]=='systemd-run' and history.count('systemd-run')==1:
                first_started.set()
                if not release_first.wait(2):raise AssertionError('Replacement never requested')
            return SimpleNamespace(returncode=0,stdout='')
        def start(name):
            try:layers.start_layer('https://twitch.tv/'+name)
            except layers.LayerError as error:errors.append(str(error))
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{
                'XDG_RUNTIME_DIR':folder,'XDG_STATE_HOME':folder}), \
                patch.object(layers,'state',return_value=None), \
                patch.object(layers.shutil,'which',return_value='/bin/mpv'), \
                patch.object(layers.socket,'socket',return_value=connection), \
                patch.object(layers,'_ipc'), \
                patch.object(layers,'_run',side_effect=service):
            first=threading.Thread(target=start,args=('first',));first.start()
            self.assertTrue(first_started.wait(2))
            generation=layers._play_generation
            second=threading.Thread(target=start,args=('second',));second.start()
            deadline=time.monotonic()+2
            while layers._play_generation==generation and time.monotonic()<deadline:time.sleep(.005)
            release_first.set()
            first.join(3);second.join(3)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(layers.audio_settings()['source_id'],'second')
        self.assertEqual(len(errors),1)
        self.assertIn('cancelled',errors[0])
        self.assertEqual(history,['systemd-run','systemctl','systemd-run','systemctl'])


if __name__ == '__main__':
    unittest.main()
