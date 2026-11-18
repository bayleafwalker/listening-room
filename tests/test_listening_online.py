"""Fixture HTTP integration tests for the room's optional online adapters."""
import contextlib
import http.server
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py')).parent))
import online_services as online


class Fixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        from urllib.parse import parse_qs, urlsplit
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        payload = json.loads(data) if data else None
        self.server.requests.append({'method': self.command, 'path': parsed.path, 'query': query,
                                     'authorization': self.headers.get('Authorization'), 'data': payload,
                                     'media_token': self.headers.get('X-Emby-Token'), 'range': self.headers.get('Range')})
        if self.server.failure:
            self.send_response(self.server.failure)
            if self.server.failure == 302:
                self.send_header('Location', self.server.base + '/stolen')
            self.end_headers()
            self.wfile.write(b'private-token-should-never-be-shown')
            return
        if parsed.path == '/Users/Me':
            result = {'Id': 'user1', 'Name': 'Test listener'}
        elif parsed.path == '/Items':
            kind = query.get('IncludeItemTypes', ['MusicAlbum'])[0]
            result = {'Items': [{'Id': {'MusicAlbum': 'album1', 'Playlist': 'playlist1', 'Audio': 'track1'}[kind],
                                 'Name': 'A quiet album' if kind == 'MusicAlbum' else 'A song', 'Type': kind,
                                 'Artists': ['An artist'], 'RunTimeTicks': 3000000000}]}
        elif parsed.path == '/Playlists/playlist1/Items':
            result = {'Items': [{'Id': 'track1', 'Name': 'First song', 'Type': 'Audio'},
                                {'Id': 'track2', 'Name': 'Second song', 'Type': 'Audio'}]}
        elif parsed.path.startswith('/Audio/'):
            if self.server.redirect_stream:
                self.send_response(302)
                self.send_header('Location', self.server.redirect_stream)
                self.end_headers()
                return
            self.send_response(206 if self.headers.get('Range') else 200)
            self.send_header('Content-Type', 'audio/flac')
            self.send_header('Content-Length', '13')
            self.end_headers()
            self.wfile.write(b'fixture-audio')
            return
        elif parsed.path.endswith('/Images/Primary'):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'\xff\xd8fixture-jpeg')
            return
        elif parsed.path == '/api':
            command = payload['command']
            if command == 'players/all':
                result = {'message_id': payload['message_id'], 'result': [
                    {'player_id': 'desk', 'display_name': 'Desk speakers', 'available': True, 'state': 'playing'},
                    {'player_id': 'kitchen', 'name': 'Kitchen', 'available': False}]}
            elif self.server.ma_error:
                result = {'error_code': 6, 'details': 'private-token-should-never-be-shown'}
            elif command == 'player_queues/get_active_queue':
                queue_id = self.server.queue_ids.get(payload['args']['player_id'])
                result = {'result': {'queue_id': queue_id} if queue_id else None}
            else:
                result = {'message_id': payload['message_id'], 'result': None}
        elif parsed.path == '/api/states':
            result = [{'entity_id': 'scene.reading', 'attributes': {'friendly_name': 'Reading lights', 'entity_id': ['light.desk']}},
                      {'entity_id': 'light.desk', 'attributes': {}}]
        elif parsed.path.startswith('/api/services/scene/'):
            result = []
        elif parsed.path == '/1/validate-token':
            result = {'valid': True, 'user_name': 'listener'}
        elif parsed.path == '/1/user/listener/listens':
            result = {'payload': {'listens': [{'listened_at': 1, 'track_metadata': {'artist_name': 'An artist', 'track_name': 'A song'}}]}}
        elif parsed.path == '/1/cf/recommendation/user/listener/recording':
            result = {'payload': {'mbids': [{'recording_mbid': 'd23f4719-9212-49f0-ad08-ddbfbfc50d6f', 'score': 1.2}]}}
        elif parsed.path == '/1/submit-listens':
            result = {'status': 'ok'}
        else:
            result = {'path': parsed.path}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class OnlineAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.server.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.server.failure = None
        cls.server.ma_error = False
        cls.server.requests = []
        cls.server.redirect_stream = None
        cls.server.queue_ids = {}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'runtime').mkdir(mode=0o700)
        self.env = patch.dict(os.environ, {'HOME': str(root), 'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_STATE_HOME': str(root / 'state'), 'XDG_CACHE_HOME': str(root / 'cache'),
                                          'XDG_RUNTIME_DIR': str(root / 'runtime'), **{spec['env']: '' for spec in online.SERVICES.values()}})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.rate = patch.object(online, '_lb_wait')
        self.rate.start()
        self.addCleanup(self.rate.stop)
        self.server.requests.clear()
        self.server.failure = None
        self.server.ma_error = False
        self.server.redirect_stream = None
        self.server.queue_ids = {}
        self.addCleanup(online.close_playback_relay)
        for service in online.SERVICES:
            online.save_connection(service, {'endpoint': self.server.base})
            online.save_token(service, 'test-private-token')

    def test_public_configuration_never_contains_tokens(self):
        rendered = json.dumps(online.config())
        self.assertNotIn('test-private-token', rendered)
        self.assertNotIn('test-private-token', json.dumps(online.capabilities()))
        for item in online.config().values():
            self.assertEqual(stat.S_IMODE(Path(item['token_file']).stat().st_mode), 0o600)
        with self.assertRaises(online.ConnectionError):
            online.save_connection('jellyfin', {'token': 'must-not-go-here'})

    def test_rejects_nonprivate_and_symlinked_token_files(self):
        filename = Path(online.config()['jellyfin']['token_file'])
        filename.chmod(0o644)
        with self.assertRaisesRegex(online.ConnectionError, 'private'):
            online.test_connection('jellyfin')
        filename.chmod(0o600)
        link = filename.with_name('symlink')
        link.symlink_to(filename)
        online.save_connection('jellyfin', {'token_file': str(link)})
        with self.assertRaises(online.ConnectionError):
            online.test_connection('jellyfin')
        self.assertEqual(self.server.requests, [])

    def test_jellyfin_test_checks_identity_then_library_without_playing(self):
        result = online.test_connection('jellyfin')
        self.assertEqual(result['capabilities']['library'], 'working')
        self.assertEqual(result['capabilities']['playback'], 'not tested')
        self.assertEqual(online.config()['jellyfin']['user_id'], 'user1')
        self.assertEqual([request['path'] for request in self.server.requests], ['/Users/Me', '/Items'])
        self.assertIn('Token="test-private-token"', self.server.requests[0]['authorization'])
        self.assertNotIn('test-private-token', json.dumps(online.checks()))

    def test_library_search_and_recent_sort_are_encoded_and_bounded(self):
        online.save_connection('jellyfin', {'user_id': 'user1'})
        result = online.browse_jellyfin('piano & rain', 'recent')
        self.assertEqual(result[0]['provider'], 'jellyfin')
        query = self.server.requests[-1]['query']
        self.assertEqual(query['SearchTerm'], ['piano & rain'])
        self.assertEqual(query['Limit'], ['60'])
        self.assertEqual(query['SortBy'], ['DateCreated'])
        self.assertEqual(query['IncludeItemTypes'], ['MusicAlbum'])

    def test_playlist_preparation_uses_local_relay_not_token_urls(self):
        online.save_connection('jellyfin', {'user_id': 'user1'})
        item = {'provider': 'jellyfin', 'id': 'playlist1', 'type': 'Playlist', 'name': 'Reading'}
        source = online.prepare_playback(item)
        self.assertEqual([row['id'] for row in source['queue']], ['track1', 'track2'])
        self.assertEqual(source['reference'], item)
        self.assertNotIn('test-private-token', json.dumps(source))
        self.assertNotIn('test-private-token', json.dumps(online.library_queue()))
        self.assertTrue(source['url'].startswith('http://127.0.0.1:'))
        self.assertNotIn('Token', source['url'])
        request = urllib.request.Request(source['url'], headers={'Range': 'bytes=0-'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b'fixture-audio')
        self.assertEqual(self.server.requests[-1]['path'], '/Audio/track1/stream')
        self.assertEqual(self.server.requests[-1]['media_token'], 'test-private-token')
        self.assertEqual(self.server.requests[-1]['range'], 'bytes=0-')

    def test_authenticated_stream_redirect_never_reaches_another_origin(self):
        target = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        target.requests = []
        target.failure = None
        target.ma_error = False
        target.redirect_stream = None
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        try:
            self.server.redirect_stream = 'http://127.0.0.1:' + str(target.server_port) + '/stolen'
            source = online.prepare_playback({'provider': 'jellyfin', 'id': 'track1', 'type': 'Audio', 'name': 'A song'})
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(source['url'])
            result.exception.close()
            self.assertEqual(result.exception.code, 502)
            self.assertEqual(target.requests, [])
            self.assertEqual(self.server.requests[-1]['media_token'], 'test-private-token')
        finally:
            target.shutdown()
            target.server_close()
            thread.join()

    def test_artwork_is_cached_locally_without_authentication_in_path(self):
        item = {'provider': 'jellyfin', 'id': 'album1'}
        first = online.artwork(item)
        count = len(self.server.requests)
        self.assertEqual(online.artwork(item), first)
        self.assertEqual(len(self.server.requests), count)
        self.assertTrue(Path(first).read_bytes().startswith(b'\xff\xd8'))
        self.assertNotIn('test-private-token', first)

    def test_failed_authentication_is_recoverable_and_does_not_echo_response(self):
        self.server.failure = 401
        with self.assertRaises(online.ConnectionError) as result:
            online.test_connection('jellyfin')
        self.assertIn('rejected access', str(result.exception))
        self.assertNotIn('private-token', str(result.exception))
        self.server.failure = None
        self.assertEqual(online.test_connection('jellyfin')['state'], 'working')

    def test_redirects_never_forward_authorization(self):
        self.server.failure = 302
        with self.assertRaisesRegex(online.ConnectionError, 'redirected'):
            online.test_connection('jellyfin')
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]['path'], '/Users/Me')

    def test_cancellation_prevents_http_request(self):
        event = threading.Event()
        event.set()
        with self.assertRaisesRegex(online.ConnectionError, 'cancelled'):
            online.test_connection('home_assistant', event)
        self.assertEqual(self.server.requests, [])

    def test_music_assistant_uses_verified_message_commands_and_target_ids(self):
        players = online.ma_players()
        self.assertEqual(players[0]['id'], 'desk')
        self.assertFalse(players[1]['available'])
        online.ma_play('library://playlist/1', 'desk')
        self.assertEqual(self.server.requests[-1]['data']['command'], 'player_queues/play_media')
        self.assertEqual(self.server.requests[-1]['data']['args'], {'queue_id': 'desk', 'media': 'library://playlist/1', 'option': 'replace'})
        online.ma_transfer('desk', 'kitchen')
        self.assertEqual(self.server.requests[-1]['data']['args']['target_queue_id'], 'kitchen')
        self.assertEqual(self.server.requests[-1]['authorization'], 'Bearer test-private-token')
        self.server.ma_error = True
        with self.assertRaises(online.ConnectionError) as result:
            online.ma_play('library://playlist/1', 'desk')
        self.assertNotIn('private-token', str(result.exception))
        with self.assertRaises(online.ConnectionError):
            online.ma_command('arbitrary/service')

    def test_grouped_speakers_resolve_actual_queue_ids(self):
        self.server.queue_ids = {'desk': 'group-queue', 'kitchen': 'kitchen-queue'}
        online.ma_play('library://playlist/1', 'desk')
        self.assertEqual(self.server.requests[-1]['data']['args']['queue_id'], 'group-queue')
        online.ma_transfer('desk', 'kitchen')
        self.assertEqual(self.server.requests[-1]['data']['args']['source_queue_id'], 'group-queue')
        self.assertEqual(self.server.requests[-1]['data']['args']['target_queue_id'], 'kitchen-queue')
        self.server.queue_ids['kitchen'] = 'group-queue'
        with self.assertRaisesRegex(online.ConnectionError, 'same queue'):
            online.ma_transfer('desk', 'kitchen')

    def test_home_assistant_browsing_and_mapping_do_not_change_lights(self):
        scenes = online.ha_scenes()
        self.assertEqual(scenes, [{'id': 'scene.reading', 'name': 'Reading lights', 'entities': ['light.desk']}])
        online.set_room_scene('midnight-library', scenes[0]['id'], scenes[0]['entities'])
        self.assertEqual([request['method'] for request in self.server.requests], ['GET'])
        online.activate_room_scene('midnight-library')
        self.assertEqual(self.server.requests[-2]['path'], '/api/services/scene/create')
        snapshot = self.server.requests[-2]['data']['scene_id']
        self.assertEqual(self.server.requests[-1]['data'], {'entity_id': 'scene.reading'})
        online.restore_room_scene()
        self.assertEqual(self.server.requests[-1]['data'], {'entity_id': 'scene.' + snapshot})
        count = len(self.server.requests)
        self.assertFalse(online.restore_room_scene()['changed'])
        self.assertFalse(online.activate_room_scene('northern-quiet')['changed'])
        self.assertEqual(len(self.server.requests), count)

    def test_changing_rooms_snapshots_each_new_light_and_restores_all(self):
        online.set_room_scene('midnight-library', 'scene.reading', ['light.desk'])
        online.set_room_scene('northern-quiet', 'scene.northern', ['light.desk', 'light.ceiling'])
        online.activate_room_scene('midnight-library')
        online.activate_room_scene('northern-quiet')
        captures = [request['data'] for request in self.server.requests if request['path'] == '/api/services/scene/create']
        self.assertEqual([item['snapshot_entities'] for item in captures], [['light.desk'], ['light.ceiling']])
        online.restore_room_scene()
        restored = [request['data']['entity_id'] for request in self.server.requests if request['path'] == '/api/services/scene/turn_on']
        self.assertEqual(restored[-2:], ['scene.' + captures[1]['scene_id'], 'scene.' + captures[0]['scene_id']])

    def test_cancelled_lighting_activation_is_restored_before_stop_completes(self):
        online.set_room_scene('midnight-library', 'scene.reading', ['light.desk'])
        started, release, cancel, stopped = (threading.Event() for _ in range(4))
        original_request = online._request
        errors = []
        def delayed(service, path, *args, **kwargs):
            if path == '/api/services/scene/turn_on' and kwargs.get('data', {}).get('entity_id') == 'scene.reading':
                started.set()
                if not release.wait(3):
                    raise RuntimeError('fixture release timed out')
            return original_request(service, path, *args, **kwargs)
        def activate():
            try:
                online.activate_room_scene('midnight-library', cancel)
            except online.ConnectionError as error:
                errors.append(str(error))
        def stop():
            online.restore_room_scene()
            stopped.set()
        with patch.object(online, '_request', side_effect=delayed):
            enter_thread = threading.Thread(target=activate)
            enter_thread.start()
            self.assertTrue(started.wait(3))
            cancel.set()
            stop_thread = threading.Thread(target=stop)
            stop_thread.start()
            self.assertFalse(stopped.wait(.05))
            release.set()
            enter_thread.join(3)
            stop_thread.join(3)
        self.assertTrue(stopped.is_set())
        self.assertEqual(errors, ['Request cancelled'])
        operations = [request['data']['entity_id'] for request in self.server.requests if request['path'] == '/api/services/scene/turn_on']
        self.assertEqual(operations[0], 'scene.reading')
        self.assertTrue(operations[-1].startswith('scene.listening_room_'))
        self.assertFalse(online.restore_room_scene()['changed'])

    def test_bookmark_preserves_uncertainty_and_removes_private_query_values(self):
        result = online.bookmark({'selected': {'title': 'Station show', 'url': 'https://example.org/live?token=private&format=opus'},
                                  'active': {'name': 'Rainy reading'}}, 'A good moment')
        self.assertEqual(result['artist'], '')
        self.assertEqual(result['metadata_status'], 'as reported by player')
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['url'], 'https://example.org/live?format=opus')
        self.assertEqual(online.remembered()[0]['note'], 'A good moment')
        self.assertEqual(self.server.requests, [])

    def test_radio_bookmark_keeps_station_separate_from_unverified_track_artist(self):
        result = online.bookmark({'active': {'name': 'Night drive'}, 'layers': {'music': {
            'id': 'radio', 'name': 'TruckersFM', 'artist': 'TruckersFM', 'title': 'A reported show'}}})
        self.assertEqual(result['station'], 'TruckersFM')
        self.assertEqual(result['artist'], '')
        self.assertEqual(result['title'], 'A reported show')
        self.assertIsInstance(result['remembered_at'], int)

    def test_listenbrainz_history_recommendations_and_listen_threshold(self):
        online.test_connection('listenbrainz')
        self.assertEqual(online.listenbrainz_history()[0]['track_metadata']['track_name'], 'A song')
        self.assertEqual(online.listenbrainz_recommendations()[0]['score'], 1.2)
        item = {'artist': 'An artist', 'title': 'A song', 'remembered_at': 12345}
        count = len(self.server.requests)
        with self.assertRaisesRegex(online.ConnectionError, 'threshold'):
            online.submit_listen(item, 10, 300)
        for invalid in (float('nan'), float('inf'), -1):
            with self.assertRaises(online.ConnectionError):
                online.submit_listen(item, invalid, 300)
        self.assertEqual(len(self.server.requests), count)
        online.submit_listen(item, 150, 300)
        request = self.server.requests[-1]
        self.assertEqual(request['path'], '/1/submit-listens')
        self.assertEqual(request['authorization'], 'Token test-private-token')
        self.assertEqual(request['data']['payload'][0]['track_metadata']['track_name'], 'A song')

    def test_link_preview_never_fetches_or_plays_and_rejects_private_urls(self):
        self.assertEqual(online.preview_link('https://youtu.be/dQw4w9WgXcQ')['kind'], 'youtube')
        self.assertEqual(online.preview_link('https://www.twitch.tv/lofigirl')['actions'], ['music', 'atmosphere', 'original'])
        self.assertEqual(online.preview_link('https://music.youtube.com/search?q=piano')['actions'], ['original'])
        self.assertEqual(online.preview_link('https://example.org/live.mp3')['actions'], ['music', 'atmosphere', 'original'])
        for value in ('file:///tmp/private', 'https://user:password@example.org/a', 'https://example.org/a?token=secret', 'https://example.org:bad/a'):
            with self.subTest(value=value), self.assertRaises(online.ConnectionError):
                online.preview_link(value)
        self.assertEqual(self.server.requests, [])


class PhoneCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.paired = patch.object(online, 'paired_devices', return_value=[{'id': 'phone1', 'name': 'My phone'}])
        self.paired.start()
        self.addCleanup(self.paired.stop)
        self.executable = patch.object(online.shutil, 'which', return_value='/some path/listening-roomctl')
        self.executable.start()
        self.addCleanup(self.executable.stop)
        self.notify = patch.object(online, '_notify_phone_config', return_value=True)
        self.notify.start()
        self.addCleanup(self.notify.stop)

    def test_explicit_install_preserves_existing_commands_and_other_settings(self):
        directory = self.directory / 'kdeconnect/phone1/kdeconnect_runcommand'
        directory.mkdir(parents=True)
        (self.directory / 'kdeconnect').chmod(0o700)
        original = {'other-command': {'name': 'Other', 'command': 'echo "hello"'}}
        encoded = json.dumps(json.dumps(original))
        config = directory / 'config'
        config.write_text('[General]\ncommands=' + encoded + '\nother-key=true\n\n[Other]\nkeep=1\n')
        result = online.install_remote_commands('phone1')
        self.assertTrue(result['notified'])
        contents = config.read_text()
        commands = online._qsettings_commands(next(line.partition('=')[2] for line in contents.splitlines() if line.startswith('commands=')))
        self.assertEqual(commands['other-command'], original['other-command'])
        self.assertEqual(commands['listening-room-quiet']['command'], "'/some path/listening-roomctl' quiet")
        self.assertIn('other-key=true\n\n[Other]\nkeep=1', contents)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        online.install_remote_commands('phone1')
        self.assertEqual(contents, config.read_text())

    def test_unknown_device_and_symlinked_paths_are_rejected(self):
        with self.assertRaises(online.ConnectionError):
            online.install_remote_commands('unpaired')
        with self.assertRaises(online.ConnectionError):
            online.install_remote_commands('../elsewhere')
        (self.directory / 'kdeconnect').symlink_to(self.directory)
        with self.assertRaises(online.ConnectionError):
            online.install_remote_commands('phone1')

    def test_malformed_existing_commands_are_preserved_for_repair(self):
        directory = self.directory / 'kdeconnect/phone1/kdeconnect_runcommand'
        directory.mkdir(parents=True)
        (self.directory / 'kdeconnect').chmod(0o700)
        path = directory / 'config'
        path.write_text('[General]\ncommands=broken\n')
        with self.assertRaises(online.ConnectionError):
            online.install_remote_commands('phone1')
        self.assertEqual(path.read_text(), '[General]\ncommands=broken\n')


if __name__ == '__main__':
    unittest.main()
