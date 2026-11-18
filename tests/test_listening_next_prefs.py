"""Wording, truthful status and inertness for the next window's Preferences and Track details."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))

FORBIDDEN = ('Main audio', 'Background audio', 'Picture', 'Retry picture', 'Refresh')

SETTINGS = {
    'jellyfin': {'endpoint': 'https://jellyfin.example.org', 'user_id': 'user-1', 'token_file': '/private/jellyfin.token'},
    'twitch': {}, 'music_assistant': {'endpoint': 'https://music.example.org'}, 'audiomuse': {'endpoint': ''},
    'listenbrainz': {'endpoint': 'https://api.listenbrainz.org', 'token_file': ''},
    'home_assistant': {'endpoint': ''},
}
CHECKS = {
    'jellyfin': {'state': 'working', 'detail': 'Library access verified.'},
    'audiomuse': {'state': 'working', 'detail': 'AudioMuse web service responded.'},
}


def setUpModule():
    global _home, _environment, prefs, details
    _home = tempfile.TemporaryDirectory(prefix='listening-prefs-tests-')
    _environment = patch.dict(os.environ, {'HOME': _home.name, 'XDG_CONFIG_HOME': _home.name + '/config',
                                           'XDG_STATE_HOME': _home.name + '/state',
                                           'XDG_DATA_HOME': _home.name + '/data',
                                           'XDG_CACHE_HOME': _home.name + '/cache'})
    _environment.start()
    os.environ.pop('ACOUSTID_CLIENT_KEY', None)
    import next_prefs
    import next_track_details
    prefs, details = next_prefs, next_track_details


def tearDownModule():
    _environment.stop()
    _home.cleanup()


def rows(settings=None, checks=None, twitch=None, environ=None):
    return {row['id']: row for row in prefs.connection_rows(SETTINGS if settings is None else settings,
                                                            CHECKS if checks is None else checks,
                                                            {'state': 'none'} if twitch is None else twitch,
                                                            {} if environ is None else environ)}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def assert_vocabulary(case, value):
    for text in strings(value):
        for word in FORBIDDEN:
            case.assertNotIn(word, text)


class ConnectionRows(unittest.TestCase):
    def test_the_six_connections_in_the_agreed_order(self):
        self.assertEqual(list(rows()), ['jellyfin', 'twitch', 'music_assistant', 'audiomuse',
                                        'listenbrainz', 'home_assistant'])

    def test_only_a_successful_check_is_connected(self):
        table = rows()
        self.assertEqual((table['jellyfin']['status'], table['jellyfin']['tone']), ('Signed in · library ready', 'ok'))
        self.assertEqual((table['audiomuse']['status'], table['audiomuse']['tone']),
                         ('Web service answers · analysis not confirmed', 'ok'))
        self.assertEqual((table['music_assistant']['status'], table['music_assistant']['tone']),
                         ('Added · not checked yet', 'warn'))
        self.assertEqual(table['home_assistant']['status'], 'Not set up')
        self.assertEqual(table['listenbrainz']['status'], 'Not set up')
        self.assertFalse(table['home_assistant']['working'])
        self.assertEqual(table['home_assistant']['action'], 'Set up')
        self.assertEqual(table['music_assistant']['action'], 'Manage')

    def test_a_configured_service_without_an_account_is_not_called_connected(self):
        table = rows({'jellyfin': {'endpoint': 'https://jellyfin.example.org'}}, checks={})
        self.assertEqual(table['jellyfin']['status'], 'Added · not checked yet')
        table = rows({'jellyfin': {'endpoint': 'https://jellyfin.example.org'}},
                     checks={'jellyfin': {'state': 'working'}})
        self.assertEqual(table['jellyfin']['status'], 'Connected · account ready')

    def test_a_failed_check_says_what_is_known(self):
        table = rows(checks={'jellyfin': {'state': 'failed', 'error': 'Jellyfin could not be reached'}})
        self.assertEqual(table['jellyfin']['status'], 'Jellyfin could not be reached')
        self.assertEqual(table['jellyfin']['tone'], 'bad')

    def test_twitch_uses_its_private_sign_in_state(self):
        self.assertEqual(rows(twitch={'state': 'saved'})['twitch']['status'], 'Saved sign-in · not checked yet')
        self.assertEqual(rows(twitch={'state': 'connected'})['twitch']['status'], 'Signed in · renews on its own')
        error = rows(twitch={'state': 'error', 'detail': 'The Twitch login file must be private'})['twitch']
        self.assertEqual((error['status'], error['tone']), ('The Twitch login file must be private', 'bad'))
        self.assertFalse(rows(twitch={'state': 'none'})['twitch']['configured'])

    def test_listenbrainz_counts_a_saved_file_or_an_environment_token(self):
        self.assertFalse(rows()['listenbrainz']['configured'])
        settings = copy.deepcopy(SETTINGS)
        settings['listenbrainz'] = {'endpoint': 'https://api.listenbrainz.org', 'token_file': '/private/lb.token'}
        self.assertTrue(rows(settings)['listenbrainz']['configured'])
        self.assertTrue(prefs.configured('listenbrainz', {'token_file': ''}, {'LISTENBRAINZ_TOKEN': 'x'}))
        self.assertFalse(prefs.configured('music_assistant', {'endpoint': ''}, {'MUSIC_ASSISTANT_TOKEN': 'x'}))

    def test_capability_chips_state_what_each_connection_can_do(self):
        table = rows()
        self.assertEqual(table['jellyfin']['chips'], ['Browse', 'Plays here', 'Open elsewhere'])
        self.assertEqual(table['music_assistant']['chips'], ['Browse', 'Plays on speakers'])
        self.assertEqual(table['audiomuse']['chips'], ['Station mixes'])
        self.assertEqual(table['listenbrainz']['chips'], ['History'])
        self.assertEqual(table['home_assistant']['chips'], ['Room lighting'])

    def test_rows_never_use_engine_words(self):
        assert_vocabulary(self, [rows(), rows(checks={}), rows(twitch={'state': 'saved'})])


class ChoiceTests(unittest.TestCase):
    OUTPUTS = [{'name': 'desk', 'description': 'Desk speakers'}, {'name': 'buds', 'description': 'Headphones'}]

    def test_output_choice_starts_with_the_system_output_and_finds_the_saved_sink(self):
        labels, names, selected = prefs.output_choices(self.OUTPUTS, 'buds')
        self.assertEqual(labels, ['System output', 'Desk speakers', 'Headphones'])
        self.assertEqual(names, [None, 'desk', 'buds'])
        self.assertEqual(selected, 2)
        self.assertEqual(prefs.output_choices(self.OUTPUTS, 'gone')[2], 0)
        self.assertEqual(prefs.output_choices([], None), (['System output'], [None], 0))

    def test_placements_are_the_agreed_words(self):
        self.assertEqual(prefs.placement_words(), ['Workspace 9', 'Here', 'On top', 'Small'])
        self.assertEqual([prefs.placement_index(value) for value in ('9', 'here', 'pin', 'pip', 'other')],
                         [0, 1, 2, 3, 0])

    def test_display_choice_offers_the_current_display_first(self):
        monitors = [{'name': 'DP-1', 'description': 'Studio'}]
        labels, names, selected = prefs.display_choices(monitors, 'DP-1')
        self.assertEqual((labels, names, selected), (['Current display', 'Studio'], [None, 'DP-1'], 1))
        self.assertEqual(prefs.display_choices([], 'DP-9'), (['Current display'], [None], 0))


class RecordingWording(unittest.TestCase):
    def test_the_environment_override_is_shown_as_such(self):
        self.assertEqual(prefs.acoustid_status(True, {}), ('Application key saved in a private file', 'ok'))
        self.assertEqual(prefs.acoustid_status(False, {})[1], 'off')
        self.assertEqual(prefs.acoustid_status(True, {'ACOUSTID_CLIENT_KEY': 'x'})[0],
                         'Environment override in use (ACOUSTID_CLIENT_KEY)')

    def test_only_jellyfin_audio_tracks_are_bulk_references(self):
        items = [{'provider': 'jellyfin', 'id': 'a', 'type': 'Audio'}, {'provider': 'jellyfin', 'id': 'b'},
                 {'provider': 'link', 'id': 'c', 'type': 'Audio'}]
        self.assertEqual([item['id'] for item in prefs.library_references(items)], ['a'])
        self.assertEqual(len(prefs.library_references([items[0]] * 30)), 20)

    def test_current_jobs_and_their_words(self):
        jobs = [{'job_id': 'a', 'kind': 'enrich', 'status': 'running'},
                {'job_id': 'b', 'kind': 'match', 'status': 'queued', 'message': 'Waiting for the network'},
                {'job_id': 'c', 'kind': 'analyze', 'status': 'completed'}]
        self.assertEqual([row['title'] for row in prefs.running_jobs(jobs)], ['Enrich', 'Match'])
        self.assertEqual(prefs.running_jobs(jobs)[1]['detail'], 'Waiting · Waiting for the network')
        self.assertEqual(prefs.jobs_summary(jobs), '2 metadata jobs in progress')
        self.assertEqual(prefs.jobs_summary(jobs[:1]), '1 metadata job in progress')
        self.assertEqual(prefs.jobs_summary([]), 'No metadata jobs.')

    def test_export_and_import_stay_bounded_and_keep_provenance(self):
        pages = [{'records': [{'origin': 'a', 'item_id': '1'}]}, {'records': [{'origin': 'a', 'item_id': '2'}]}]
        document = prefs.export_document(pages)
        self.assertEqual(document, {'version': 1, 'records': [{'origin': 'a', 'item_id': '1'},
                                                             {'origin': 'a', 'item_id': '2'}]})
        self.assertEqual(prefs.import_records('{"version": 1, "records": [{"origin": "a"}]}'), [{'origin': 'a'}])
        for bad in ('not json', '[]', '{"records": "no"}'):
            with self.assertRaises(ValueError):
                prefs.import_records(bad)
        with self.assertRaises(ValueError):
            prefs.import_records('{' + '"x": "' + 'a' * 2100000 + '"}')
        self.assertEqual([len(chunk) for chunk in prefs.chunks(list(range(405)))], [200, 200, 5])

    def test_waybar_and_launcher_status_read_local_files_only(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(prefs.waybar_status(folder)[1], 'off')
            Path(folder, 'waybar').mkdir()
            Path(folder, 'waybar', 'config.jsonc').write_text('{"custom/listening": {"exec": "listening-roomctl status"}}')
            self.assertEqual(prefs.waybar_status(folder)[1], 'ok')
            Path(folder, 'waybar', 'config.jsonc').write_text('{"custom/clock": {}}')
            self.assertEqual(prefs.waybar_status(folder)[1], 'warn')
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(prefs.launcher_status(folder, [])[1], 'off')
            Path(folder, 'applications').mkdir()
            Path(folder, 'applications', 'listening-room.desktop').write_text('[Desktop Entry]\nActions=Quiet;\n')
            self.assertEqual(prefs.launcher_status(folder, [])[0], 'Launcher entry installed · with room actions')
            Path(folder, 'applications', 'listening-room.desktop').write_text('[Desktop Entry]\nName=Listening room\n')
            self.assertEqual(prefs.launcher_status(folder, [])[0], 'Launcher entry installed · no extra actions')


def record():
    return {'origin': 'jellyfin:abc', 'item_id': 'track1', 'audio_sha256': 'a' * 64,
            'identities': [{'provider': 'Jellyfin', 'provider_id': '6f1f0a4c-0000-0000-0000-000000000001',
                            'name': 'MusicBrainzTrack', 'method': 'reported ProviderIds',
                            'date': '2026-11-17T10:00:00+00:00', 'status': 'unconfirmed'}],
            'features': [{'provider': 'Essentia MTG', 'name': 'instrumental score', 'value': .87,
                          'model': 'voice instrumental msd musicnn 1', 'version': '2',
                          'date': '2026-11-17T10:00:00+00:00'}],
            'unbound_features': [{'provider': 'AudioMuse', 'name': 'mood calm', 'value': .5}],
            'review': None, 'updated_at': '2026-11-17T10:00:00+00:00'}


class TrackDetailLines(unittest.TestCase):
    TRACK = {'provider': 'jellyfin', 'id': 'track1', 'type': 'Audio', 'name': 'Nocturne',
             'artist': 'Fixture Quartet', 'album': 'Night studies'}

    def text(self, reference=None, record_value=None, snapshot=None):
        return '\n'.join(details.record_lines(self.TRACK if reference is None else reference, record_value, snapshot))

    def test_a_track_without_metadata_says_which_actions_exist(self):
        text = self.text(record_value=None)
        self.assertIn('No metadata for this recording yet. Choose Enrich, Match or Analyze.', text)
        self.assertIn('Fixture Quartet', text)

    def test_the_scope_statements_survive_from_the_classic_panel(self):
        text = self.text(record_value=record())
        self.assertIn('Audio version: SHA256 ' + 'a' * 64, text)
        self.assertIn('not a probability or human review', text)
        self.assertIn('audio version unknown', text)
        self.assertIn('No human review for this audio version.', text)
        self.assertIn('Review validity is checked again before use.', text)
        self.assertIn('Identity: MusicBrainzTrack', text)

    def test_the_current_stations_reason_appears_only_for_that_track(self):
        snapshot = {'station': {'current': {'id': 'track1'}, 'explanation': ['Near your usual energy']}}
        self.assertIn('Why this selection: Near your usual energy', self.text(record_value=record(), snapshot=snapshot))
        other = {'station': {'current': {'id': 'other'}, 'explanation': ['Near your usual energy']}}
        self.assertNotIn('Near your usual energy', self.text(record_value=record(), snapshot=other))
        self.assertIn('while it is the station’s current track', self.text(record_value=record(), snapshot=other))

    def test_lines_never_use_engine_words(self):
        assert_vocabulary(self, self.text(record_value=record(), snapshot={'station': {}}))

    def test_candidates_and_labels_keep_the_provider(self):
        jobs = [{'job_id': 'j1', 'status': 'completed', 'result': {'candidates': [
            {'provider': 'MusicBrainz', 'provider_id': 'c1', 'title': 'Nocturne no. 2'}]}},
            {'job_id': 'j2', 'status': 'running', 'result': {'candidates': [{'provider': 'x'}]}}]
        candidates = details.match_candidates(jobs, 'j1')
        self.assertEqual(details.candidate_labels(candidates), ['Nocturne no. 2 (MusicBrainz)'])
        self.assertEqual(details.match_candidates(jobs, 'j2'), [])
        self.assertEqual(details.candidate_labels([]), ['No identity candidates'])
        self.assertEqual(details.candidate_labels([{'provider_id': 'p'}]), ['p (unknown)'])

    def test_the_review_form_asks_for_scope_and_a_classification(self):
        self.assertIsNone(details.review_problem(self.TRACK, record(), 'Ada', True, 'instrumental'))
        self.assertEqual(details.review_problem(None, record(), 'Ada', True, 'instrumental'),
                         'Open a library track first.')
        self.assertEqual(details.review_problem(self.TRACK, None, 'Ada', True, 'instrumental'),
                         'Load this recording’s details first.')
        self.assertEqual(details.review_problem(self.TRACK, record(), 'Ada', True, None),
                         'Choose what this recording is.')
        self.assertEqual(details.review_problem(self.TRACK, record(), 'Ada', False, 'vocal'),
                         'Listen to the whole recording and confirm the review scope.')
        self.assertEqual(details.review_problem(self.TRACK, record(), '  ', True, 'vocal'),
                         'Enter your name or review identifier.')

    def test_review_choices_match_the_controller_contract(self):
        self.assertEqual([key for key, _label in details.CLASSES],
                         ['instrumental', 'vocal', 'spoken', 'unknown'])
        self.assertIn('Listen to the whole exact recording yourself before reviewing.', details.REVIEW_EXPLANATION)
        self.assertIn('Reviewing does not start playback.', details.REVIEW_EXPLANATION)
        self.assertEqual(details.REVIEW_ACK, 'I listened to the entire exact recording')
        self.assertIn('Confirming an identity is separate from reviewing the audio.', details.MATCH_NOTE)

    def test_jobs_words_match_the_classic_panel(self):
        self.assertEqual(details.jobs_words([{'kind': 'enrich', 'status': 'running'},
                                             {'kind': 'match', 'status': 'queued'}]),
                         'Enrich: running (2 pending)')
        self.assertEqual(details.jobs_words([]), 'No metadata jobs.')


class KeepControlsTests(unittest.TestCase):
    def test_keep_controls_is_an_additive_setting_the_window_can_read(self):
        import room_session
        from test_listening_session import FakeBackend
        with tempfile.TemporaryDirectory() as folder:
            controller = room_session.Controller(FakeBackend(), Path(folder))
            try:
                self.assertFalse(controller.settings['keep_controls'])
                controller.apply_settings({'keep_controls': True})
                self.assertTrue(controller.snapshot()['settings']['keep_controls'])
                with self.assertRaises(room_session.SessionError):
                    controller.apply_settings({'unknown_setting': True})
            finally:
                controller.close()


class OptionsWording(unittest.TestCase):
    def test_the_dialog_offers_the_agreed_wording(self):
        labels = [prefs.SERVICE_NOTES[service] for service in prefs.ORDER]
        table = rows()
        self.assertIn('web service', ''.join(strings(prefs.WORKING)).lower())
        assert_vocabulary(self, [labels, table, prefs.SERVICE_NOTES, prefs.RECORDINGS_NOTE])
        self.assertNotIn('Picture', prefs.WORKING['audiomuse'])


class StoredChecksFollowTheConfiguration(unittest.TestCase):
    def setUp(self):
        import online_services
        self.online = online_services
        self.online._write(self.online._state_dir() / 'connection-checks.json',
                           {'audiomuse': {'id': 'audiomuse', 'state': 'working', 'checked_at': 1}})
        self.online.save_connection('audiomuse', {'endpoint': 'http://one.fixture.test'})
        self.online._write(self.online._state_dir() / 'connection-checks.json',
                           {'audiomuse': {'id': 'audiomuse', 'state': 'working', 'checked_at': 1}})

    def test_changing_the_address_or_token_drops_the_stored_success(self):
        self.online.save_connection('audiomuse', {'username': 'same-address'})
        self.assertIn('audiomuse', self.online.checks())
        self.online.save_connection('audiomuse', {'endpoint': 'http://two.fixture.test'})
        self.assertNotIn('audiomuse', self.online.checks())

    def test_a_failed_test_withdraws_the_earlier_success_and_records_nothing(self):
        with patch.object(self.online, '_request', side_effect=self.online.ConnectionError('unreachable')):
            with self.assertRaises(self.online.ConnectionError):
                self.online.test_connection('audiomuse')
        self.assertEqual(self.online.checks(), {})


if __name__ == '__main__':
    unittest.main()
