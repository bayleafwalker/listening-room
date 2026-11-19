import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import room_catalogue as rooms


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        state = mock.patch.object(rooms.hub, 'STATE', Path(self.directory.name) / 'state')
        state.start(); self.addCleanup(state.stop)
        self.sources = [{'id': item['source_id'], 'name': item['source_id'], 'kind': 'web' if item['source_id'] == 'ylex' else 'radio'} for item in rooms.RECIPES]
        self.views = [{'id': identity, 'name': identity, 'url': '/missing/' + identity + '.mp4', 'available': True} for identity in ('moonlit-lake', 'northern-lake', 'norway')]
        for target, value in ((mock.patch.object(rooms.hub, 'sources', return_value=self.sources), None), (mock.patch.object(rooms.ambience, 'presets', return_value=self.views), None)):
            target.start(); self.addCleanup(target.stop)

    def test_browsing_rooms_never_starts_playback_or_changes_state(self):
        with mock.patch.object(rooms.hub, 'play') as play, mock.patch.object(rooms.ambience, 'start') as start:
            choices = rooms.rooms()
        self.assertEqual(len(choices), 8)
        self.assertEqual(len({item['id'] for item in choices}), 8)
        play.assert_not_called(); start.assert_not_called()
        self.assertFalse(rooms.hub.STATE.exists())

    def test_last_entered_is_a_preview_hint_not_a_playback_command(self):
        rooms.hub.update_state(last_room='northern-quiet')
        with mock.patch.object(rooms.hub, 'play') as play:
            choices = rooms.rooms()
        self.assertEqual([item['id'] for item in choices if item['last_entered']], ['northern-quiet'])
        play.assert_not_called()


if __name__ == '__main__':
    unittest.main()
