import os
import fcntl
import threading
import time
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import online_services as online
import provider_auth
import twitch_credentials


class Providers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HOME': self.tmp.name, 'XDG_CONFIG_HOME': self.tmp.name,
                                           'XDG_STATE_HOME': self.tmp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_twitch_device_approval_persists_and_reuses_private_token(self):
        login = provider_auth.TwitchDeviceLogin('client123')
        begin = {'verification_uri': 'https://www.twitch.tv/activate?public=true&device-code=ABCD',
                 'user_code': 'ABCD', 'device_code': 'device123', 'expires_in': 300, 'interval': 1}
        token = {'access_token': 'access123', 'refresh_token': 'refresh123', 'token_type': 'bearer'}
        with patch.object(provider_auth, '_post', side_effect=[begin, {'pending': 'authorization_pending'}, token]), \
             patch.object(provider_auth.time, 'monotonic', side_effect=[0, 0, 2, 2, 2, 4, 4, 4]):
            self.assertEqual(login.begin()['code'], 'ABCD')
            self.assertEqual(login.finish(), {'pending': True})
            self.assertEqual(login.finish(), {'connected': True})
        path = twitch_credentials._paths()[0]
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(twitch_credentials.load().access_token, 'access123')
        self.assertEqual(twitch_credentials.load().source, 'Listening Room')
        self.assertNotIn('access123', repr(twitch_credentials.load()))

    def test_device_login_refresh_rotates_saved_tokens(self):
        login = provider_auth.TwitchDeviceLogin('client123')
        login._save('old_access', 'old_refresh')
        with patch.object(twitch_credentials, '_renew', return_value=('new_access', 'new_refresh')):
            renewed = twitch_credentials.refresh('old_access')
            self.assertEqual(renewed.access_token, 'new_access')
            self.assertEqual(renewed.source, 'Listening Room')
        self.assertEqual(twitch_credentials.load().access_token, 'new_access')

    def test_device_login_rejects_malformed_token(self):
        login = provider_auth.TwitchDeviceLogin('client123')
        login.device_code, login.deadline, login.next_poll = 'device', 20, 0
        with patch.object(provider_auth.time, 'monotonic', return_value=1), \
             patch.object(provider_auth, '_post', return_value={'access_token': 'secret', 'token_type': 'bearer'}):
            with self.assertRaises(provider_auth.AuthError):
                login.finish()
        self.assertFalse(twitch_credentials._paths()[0].exists())

    def test_cancelled_device_finish_does_not_save_after_response(self):
        login = provider_auth.TwitchDeviceLogin('client123')
        login.device_code, login.deadline, login.next_poll = 'device', 20, 0
        def token(*_args):
            login.cancel()
            return {'access_token': 'secret', 'refresh_token': 'refresh', 'token_type': 'bearer'}
        with patch.object(provider_auth.time, 'monotonic', return_value=1), patch.object(provider_auth, '_post', side_effect=token):
            with self.assertRaises(provider_auth.AuthError):
                login.finish()
        self.assertFalse(twitch_credentials._paths()[0].exists())

    def test_cancel_during_shared_refresh_lock_never_replaces_login(self):
        login = provider_auth.TwitchDeviceLogin('client123')
        login._save('old_access', 'old_refresh')
        path = twitch_credentials._paths()[0]
        fd = os.open(path.parent / '.listening-room-refresh.lock', os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        errors = []
        def update():
            try:
                login._save('new_access', 'new_refresh')
            except provider_auth.AuthError as error:
                errors.append(str(error))
        worker = threading.Thread(target=update)
        worker.start()
        time.sleep(.1)
        login.cancel()
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)
        self.assertEqual(twitch_credentials.load().access_token, 'old_access')

    def test_twitch_validation_refreshes_rejected_login_once(self):
        import urllib.error
        import twitch_channels
        login = provider_auth.TwitchDeviceLogin('client123')
        login._save('old_access', 'old_refresh')
        error = urllib.error.HTTPError('https://id.twitch.tv/oauth2/validate', 401, '', {}, None)
        validated = {'client_id': 'client123', 'expires_in': 100, 'scopes': ['user:read:follows']}
        with patch.object(twitch_channels, '_validate_token', side_effect=[error, validated]), \
             patch.object(twitch_credentials, '_renew', return_value=('new_access', 'new_refresh')):
            self.assertEqual(provider_auth.validate_saved_twitch(), {'state': 'connected', 'follows': True})
        self.assertEqual(twitch_credentials.load().access_token, 'new_access')

    def test_validation_does_not_refresh_environment_credentials(self):
        import urllib.error
        import twitch_channels
        error = urllib.error.HTTPError('https://id.twitch.tv/oauth2/validate', 401, '', {}, None)
        with patch.dict(os.environ, {'TWITCH_CLIENT_ID': 'client123', 'TWITCH_ACCESS_TOKEN': 'envtoken'}), \
             patch.object(twitch_channels, '_validate_token', side_effect=error), \
             patch.object(twitch_credentials, 'refresh') as refresh:
            with self.assertRaises(provider_auth.AuthError):
                provider_auth.validate_saved_twitch()
            refresh.assert_not_called()
            self.assertEqual(os.environ['TWITCH_ACCESS_TOKEN'], 'envtoken')

    def test_twitch_rejects_invalid_verification_origin(self):
        login = provider_auth.TwitchDeviceLogin('client123')
        with patch.object(provider_auth, '_post', return_value={'verification_uri': 'https://evil.invalid/activate',
                'user_code': 'ABCD', 'device_code': 'device', 'expires_in': 100, 'interval': 5}):
            with self.assertRaises(provider_auth.AuthError):
                login.begin()
        self.assertFalse(twitch_credentials._paths()[0].exists())

    def test_music_assistant_library_and_provider_search_are_bounded(self):
        item = {'item_id': '1', 'name': 'My list', 'uri': 'library://playlist/1', 'media_type': 'playlist'}
        with patch.object(online, 'ma_command', return_value=[item]) as command:
            self.assertEqual(online.browse_music_assistant(provider='youtube_music')[0]['name'], 'My list')
            command.assert_called_once_with('music/playlists/library_items', {'limit': 60, 'provider': 'youtube_music'}, None)
        with patch.object(online, 'ma_command', return_value={'tracks': [item]}) as command:
            online.browse_music_assistant('piano', 'tracks', provider='youtube_music', search_all=True)
            self.assertEqual(command.call_args.args[1]['providers'], ['youtube_music'])
        self.assertIsNone(online._ma_item(dict(item, uri='https://example.com/music?token=secret')))

    def test_audiomuse_mix_requires_verified_jellyfin_ids(self):
        online.save_connection('jellyfin', {'endpoint': 'https://library.example', 'user_id': 'user'})
        seed = {'provider': 'jellyfin', 'id': 'seed', 'type': 'Audio'}
        generated = [{'item_id': 'one'}, {'item_id': 'missing'}, {'item_id': 'two'}]
        library = {'Items': [{'Id': 'two', 'Name': 'Second', 'Type': 'Audio'},
                             {'Id': 'one', 'Name': 'First', 'Type': 'Audio'}]}
        with patch.object(online, '_request', side_effect=[generated, library]):
            rows = online.audiomuse_similar(seed)
        self.assertEqual([row['id'] for row in rows], ['one', 'two'])
        with patch.object(online, '_request', side_effect=[generated, {'Items': []}]):
            with self.assertRaises(online.ConnectionError):
                online.audiomuse_similar(seed)

    def test_jellyfin_real_metadata_is_preserved_without_inventing_features(self):
        ref = online._jf_item({'Id': 'track', 'Name': 'Title', 'Type': 'Audio',
            'Genres': ['Jazz', 123], 'Tags': ['Ambient', 42], 'UserData': {'IsFavorite': True, 'PlayCount': 7, 'LastPlayedDate': '2026-01-01T01:02:03Z'}})
        self.assertEqual(ref['genres'], ['Jazz'])
        self.assertEqual(ref['tags'], ['Ambient'])
        self.assertEqual(ref['last_played'], '2026-01-01T01:02:03Z')
        self.assertTrue(ref['favorite'])
        self.assertEqual(ref['play_count'], 7)
        self.assertNotIn('energy', ref)
        self.assertNotIn('speech', ref)

    def test_audiomuse_legacy_config_checks_web_not_worker(self):
        with patch.object(online, '_request', return_value={'num_recent_albums': 5, 'mood_labels': []}) as request:
            result = online.test_connection('audiomuse')
        self.assertEqual(request.call_args.args[:2], ('audiomuse', '/api/config'))
        self.assertEqual(result['capabilities']['sonic_mix'], 'not tested')
        self.assertIn('Worker', result['detail'])

    def test_expired_device_code_never_exchanges(self):
        login = provider_auth.TwitchDeviceLogin('client123')
        login.device_code, login.deadline = 'device', 1
        with patch.object(provider_auth.time, 'monotonic', return_value=2), patch.object(provider_auth, '_post') as post:
            with self.assertRaises(provider_auth.AuthError):
                login.finish()
        post.assert_not_called()

    def test_music_assistant_radio_mode_is_explicit(self):
        with patch.object(online, '_ma_queue_id', return_value='queue'), patch.object(online, 'ma_command') as command:
            online.ma_play('library://track/1', 'speaker', radio_mode=True)
            self.assertTrue(command.call_args.args[1]['radio_mode'])

    def test_jellyfin_sign_in_saves_private_token_without_password(self):
        online.save_connection('jellyfin', {'endpoint': 'https://jellyfin.example'})
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"AccessToken":"fixturetoken","User":{"Id":"user1","Name":"user"}}'
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            result = online.jellyfin_login('user', 'fixturepassword')
        self.assertTrue(result['signed_in'])
        self.assertNotIn('fixturepassword', str(online.config()))
        self.assertNotIn('fixturetoken', str(online.config()))
        self.assertEqual(online._token('jellyfin'), 'fixturetoken')

    def test_redirects_are_not_followed_with_authentication(self):
        handler = online._NoRedirect()
        response = MagicMock()
        with self.assertRaises(online.ConnectionError):
            handler.redirect_request(None, response, 302, '', {}, 'https://other.example/')
        response.close.assert_called_once()
        self.assertIsNone(provider_auth._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example/'))

    def test_jellyfin_password_not_sent_over_remote_http(self):
        online.save_connection('jellyfin', {'endpoint': 'http://example.com'})
        with patch('urllib.request.build_opener') as opener, self.assertRaises(online.ConnectionError):
            online.jellyfin_login('user', 'password')
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
