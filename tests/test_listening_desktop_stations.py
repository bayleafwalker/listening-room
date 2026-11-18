"""Station commands and bounded desktop routing, without a user session."""
import os
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import listening_hub as hub
import atmosphere_catalogue
import roomctl
import session_service


class DesktopStations(unittest.TestCase):
    def test_station_reads_and_edits_return_the_controller_result(self):
        controller = mock.Mock()
        controller.dispatch.return_value = {'stations': [{'id': 'reading', 'name': 'Reading'}]}
        service = session_service.Service(controller)
        for action in ('station.list', 'station.save', 'station.update', 'station.start', 'station.recent', 'station.replay'):
            self.assertEqual(service.execute(action, {'id': 'reading'}), controller.dispatch.return_value)
        controller.worker.submit.assert_not_called()

    def test_superseded_queued_failure_does_not_replace_current_errors(self):
        started, release = threading.Event(), threading.Event()
        def fail(_action, _args):
            started.set()
            release.wait(2)
            raise ValueError('Old source failed')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
            controller = SimpleNamespace(generation=0, cancel=threading.Event(), lock=threading.RLock(),
                                         worker=worker, dispatch=fail, errors={}, _changed=mock.Mock())
            service = session_service.Service(controller)
            self.assertTrue(service.execute('atmosphere', {})['accepted'])
            self.assertTrue(started.wait(1))
            with controller.lock:
                controller.generation += 1
                controller.cancel.set()
            release.set()
            worker.submit(lambda: None).result(timeout=2)
        self.assertEqual(controller.errors, {})
        controller._changed.assert_not_called()

    def test_phone_and_launcher_commands_use_references(self):
        self.assertEqual(roomctl.station_arguments('tune', 'reading'), ('station.start', {'id': 'reading'}))
        self.assertEqual(roomctl.station_arguments('stations'), ('station.list', {}))
        self.assertEqual(roomctl.station_arguments('replay', 'evening-1'), ('station.replay', {'id': 'evening-1'}))
        self.assertEqual(roomctl.station_arguments('play-next', '{"provider":"jellyfin","id":"abc"}'),
                         ('station.play_next', {'item': {'provider': 'jellyfin', 'id': 'abc'}}))
        for action, value in (('tune', None), ('replay', 'bad\nvalue'), ('station-save', '[]'), ('station-update', '{')):
            with self.assertRaises(ValueError):
                roomctl.station_arguments(action, value)

    def test_private_stream_parameters_cannot_enter_public_room_exports(self):
        for key in ('signature', 'password', 'credential', 'Authorization', 'secret', 'api_key'):
            self.assertTrue(hub.private_favorite({'url': 'https://example.test/audio?' + key + '=fixture'}))
        self.assertFalse(hub.private_favorite({'url': 'https://example.test/audio?format=mp3'}))

    def test_waybar_station_caption_is_escaped(self):
        snapshot = {'station': {'definition': {'name': 'Reading <quiet>'}}, 'layers': {'music': {'status': 'Playing', 'name': 'Piano & strings'}}}
        value = roomctl.status(snapshot)
        self.assertIn('Reading &lt;quiet&gt;', value['text'])
        self.assertIn('Piano &amp; strings', value['text'])

    def test_offline_main_audio_accepts_only_its_catalogue_asset(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'LISTENING_HUB_ASSETS': directory}):
            folder = Path(directory) / 'atmospheres'
            folder.mkdir()
            audio = folder / 'rain-glass.wav'
            audio.write_bytes(b'test asset')
            source = atmosphere_catalogue.source('rain-glass')
            self.assertEqual(hub.playback_url(source), str(audio))
            for item in ({'id': 'rain-glass', 'url': '/etc/passwd'},
                         {'id': 'unknown', 'url': str(audio)},
                         {'id': 'rain-glass', 'url': audio.as_uri()},
                         {'id': 'rain-glass', 'url': 'relative.wav'}):
                with self.assertRaises(hub.HubError):
                    hub.playback_url(item)
            audio.unlink()
            with self.assertRaises(hub.HubError):
                hub.playback_url(source)

    def test_station_mpris_metadata_and_next_follow_the_current_program(self):
        snapshot = {'active': {'music': {'source_id': 'jellyfin-1', 'reference': {'provider': 'jellyfin'}}},
                    'layers': {'music': {'status': 'Playing', 'title': 'Old stream title'}},
                    'station': {'current': {'id': '1', 'name': 'Current piano', 'artist': 'Pianist', 'album': 'Evening'},
                                'can_next': True}, 'media': {'seekable': True, 'duration': 100}}
        values = session_service.mpris_values(snapshot)
        self.assertEqual(values['Metadata']['xesam:title'], 'Current piano')
        self.assertEqual(values['Metadata']['xesam:artist'], ['Pianist'])
        self.assertTrue(values['CanGoNext'])
        self.assertTrue(values['CanSeek'])
        snapshot['layers']['music']['status'] = 'Stopped'
        self.assertFalse(session_service.mpris_values(snapshot)['CanGoNext'])


if __name__ == '__main__':
    unittest.main()
