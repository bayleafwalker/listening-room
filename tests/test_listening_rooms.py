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
        overlay=mock.patch.object(rooms.audio_layers,'state',return_value=None)
        overlay.start(); self.addCleanup(overlay.stop)
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

    def test_sound_only_is_explicit_and_does_not_open_a_view(self):
        with mock.patch.object(rooms.hub, 'play') as play, mock.patch.object(rooms.ambience, 'start') as start:
            result = rooms.launch_room('midnight-library', with_view=False)
        self.assertEqual(play.call_args.args[0]['id'], 'dark-academia-piano')
        start.assert_not_called()
        self.assertFalse(result['errors'])
        self.assertEqual(rooms.hub.state()['last_room'], 'midnight-library')

    def test_room_starts_selected_sound_and_its_silent_view_on_nine(self):
        with mock.patch.object(rooms.hub, 'play'), mock.patch.object(rooms.ambience, 'start') as start:
            rooms.launch_room('midnight-library')
        self.assertEqual(start.call_args.args[0]['id'], 'moonlit-lake')
        self.assertEqual(start.call_args.kwargs, {'mode': 'ambience', 'placement': '9'})

    def test_failed_view_reports_partial_success_and_leaves_audio_alone(self):
        with mock.patch.object(rooms.hub, 'play') as play, mock.patch.object(rooms.ambience, 'start', side_effect=ValueError('display failed')), mock.patch.object(rooms.hub, 'player_command') as control:
            result = rooms.launch_room('midnight-library')
        play.assert_called_once(); control.assert_not_called()
        self.assertIn('Playing', result['message'])
        self.assertIn('view could not open', result['message'])
        self.assertEqual(result['errors'], ['display failed'])

    def test_unavailable_source_fails_before_any_effect(self):
        with mock.patch.object(rooms.hub, 'sources', return_value=[]), mock.patch.object(rooms.hub, 'play') as play:
            with self.assertRaises(rooms.hub.HubError):
                rooms.launch_room('midnight-library')
        play.assert_not_called()

    def test_browser_room_explains_manual_playback(self):
        with mock.patch.object(rooms.hub, 'play'), mock.patch.object(rooms.ambience, 'start'):
            result = rooms.launch_room('finnish-discoveries')
        self.assertIn('Start playback in your browser', result['message'])
        self.assertNotIn('Playing', result['message'])

    def test_finnish_room_reports_direct_radio_playback(self):
        next(item for item in self.sources if item['id'] == 'ylex')['kind'] = 'radio'
        with mock.patch.object(rooms.hub, 'play'), mock.patch.object(rooms.hub, 'update_state'):
            result = rooms.launch_room('finnish-discoveries', with_view=False)
        self.assertEqual(result['message'], 'Playing ylex.')

    def test_quiet_scopes_owned_players_and_explicit_app(self):
        infos = [{'id': identity, 'status': 'Playing'} for identity in ('radio', 'video', 'browser-A', 'browser-B')]
        with mock.patch.object(rooms.hub, 'all_info', return_value=infos), mock.patch.object(rooms.hub, 'player_command') as control, mock.patch.object(rooms.ambience, 'state', return_value={'status': 'Playing'}), mock.patch.object(rooms.ambience, 'control') as vista:
            result = rooms.quiet_room(target_id='browser-A')
        self.assertEqual({call.args[0] for call in control.call_args_list}, {'radio', 'video', 'browser-A'})
        self.assertNotIn('browser-B', result['paused'])
        vista.assert_called_once_with('pause', 'ambience')

    def test_stale_quiet_target_does_not_fall_back_to_another_browser(self):
        with mock.patch.object(rooms.hub, 'all_info', return_value=[{'id': 'another-browser', 'status': 'Playing'}]), mock.patch.object(rooms.hub, 'player_command') as control, mock.patch.object(rooms.ambience, 'state', return_value=None):
            result = rooms.quiet_room(target_id='gone')
        control.assert_not_called()
        self.assertTrue(result['errors'])

    def test_no_selected_app_does_not_adopt_a_new_browser(self):
        with mock.patch.object(rooms.hub, 'all_info', return_value=[{'id': 'new-browser', 'status': 'Playing'}]), mock.patch.object(rooms.hub, 'player_command') as control, mock.patch.object(rooms.ambience, 'state', return_value=None):
            result = rooms.quiet_room(target_id=None)
        control.assert_not_called()
        self.assertEqual(result['paused'], [])

    def test_quiet_includes_atmosphere_without_adopting_other_apps(self):
        with mock.patch.object(rooms.hub, 'all_info', return_value=[]), mock.patch.object(rooms.ambience, 'state', return_value=None), mock.patch.object(rooms.audio_layers, 'state', return_value={'status': 'Playing'}), mock.patch.object(rooms.audio_layers, 'control') as control:
            result = rooms.quiet_room()
        control.assert_called_once_with('pause')
        self.assertEqual(result['paused'], ['overlay'])

    def test_stop_room_only_stops_owned_units(self):
        with mock.patch.object(rooms.hub, 'radio_audio_state') as radio, mock.patch.object(rooms.ambience, '_video_audio') as video, mock.patch.object(rooms.audio_layers, 'remember_audio') as overlay, mock.patch.object(rooms.hub, 'run') as run:
            run.return_value.returncode = 0
            result = rooms.stop_room()
        radio.assert_called_once_with();video.assert_called_once_with();overlay.assert_called_once_with()
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ['listening-hub-radio.service', 'listening-hub-ambience.service', 'listening-hub-video.service', 'listening-hub-overlay.service'])
        self.assertFalse(result['errors'])

    def test_last_entered_is_a_preview_hint_not_a_playback_command(self):
        rooms.hub.update_state(last_room='northern-quiet')
        with mock.patch.object(rooms.hub, 'play') as play:
            choices = rooms.rooms()
        self.assertEqual([item['id'] for item in choices if item['last_entered']], ['northern-quiet'])
        play.assert_not_called()

    def test_still_mode_uses_artwork_and_keeps_radio_choice(self):
        artwork = Path(self.directory.name) / 'still.jpg'
        artwork.touch()
        self.views[0]['still_url'] = str(artwork)
        with mock.patch.object(rooms.hub, 'play') as play, mock.patch.object(rooms.ambience, 'start') as start:
            rooms.launch_room('midnight-library', still=True)
        self.assertEqual(play.call_args.args[0]['id'], 'dark-academia-piano')
        self.assertEqual(start.call_args.args[0]['url'], str(artwork))
        self.assertEqual(self.views[0]['url'], '/missing/moonlit-lake.mp4')

    def test_missing_still_fails_before_starting_audio(self):
        with mock.patch.object(rooms.hub, 'play') as play, mock.patch.object(rooms.ambience, 'start') as start:
            with self.assertRaisesRegex(rooms.hub.HubError, 'Still artwork is unavailable'):
                rooms.launch_room('midnight-library', still=True)
        play.assert_not_called(); start.assert_not_called()

    def test_still_choice_does_not_block_sound_only(self):
        with mock.patch.object(rooms.hub, 'play') as play, mock.patch.object(rooms.ambience, 'start') as start:
            rooms.launch_room('midnight-library', with_view=False, still=True)
        play.assert_called_once(); start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
