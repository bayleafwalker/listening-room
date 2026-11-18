import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('listening_hub', os.environ.get('LISTENING_HUB_SOURCE', Path(__file__).resolve().parents[1] / 'src/listening_hub.py'))
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)

class FakeSocket:
    def __init__(self, events):
        self.events = events
        self.buffer = io.BytesIO(b''.join(json.dumps(e).encode() + b'\n' for e in events))
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, timeout): pass
    def connect(self, path): pass
    def sendall(self, data): self.sent = json.loads(data)
    def recv(self, size): return self.buffer.read(size)
    def makefile(self, mode): return io.BytesIO(b''.join(json.dumps(e).encode() + b'\n' for e in self.events))

class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_patch = mock.patch.object(hub, 'STATE', Path(self.tmp.name) / 'state')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.runtime_patch = mock.patch.object(hub, 'RUNTIME', Path(self.tmp.name) / 'runtime')
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.socket_patch = mock.patch.object(hub, 'SOCKET', Path(self.tmp.name) / 'runtime' / 'radio.sock')
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def test_twitch_audio_uses_extractor_in_the_music_lane(self):
        with mock.patch.object(hub.shutil, 'which', return_value='/bin/yt-dlp'), mock.patch.object(hub, 'start_radio') as start:
            hub.start_twitch_audio('https://www.twitch.tv/lofigirl')
        self.assertEqual(start.call_args.args[0]['id'], 'twitch-channel')
        self.assertEqual(start.call_args.kwargs['ytdlp'], '/bin/yt-dlp')
        with mock.patch.object(hub, 'start_radio') as start:
            with self.assertRaises(hub.HubError):
                hub.start_twitch_audio('https://example.org/lofigirl')
        start.assert_not_called()

    def test_rofi_has_no_thinking_timeout(self):
        with mock.patch.object(hub, 'run') as run:
            run.return_value.returncode = 1
            hub.choose('Test', [('Choice', 'choice')])
        self.assertIsNone(run.call_args.kwargs['timeout'])

    def test_duplicate_station_labels_remain_selectable(self):
        with mock.patch.object(hub, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '1\n'
            chosen = hub.choose('Station', [('Same name', 'first'), ('Same name', 'second')])
        self.assertEqual(chosen, 'second')

    def test_discovery_fails_over_and_searches_country(self):
        good = io.BytesIO(b'[]')
        addrs = [(None, None, None, None, ('192.0.2.1', 443)), (None, None, None, None, ('192.0.2.2', 443))]
        with mock.patch.object(hub.socket, 'getaddrinfo', return_value=addrs), mock.patch.object(hub.socket, 'gethostbyaddr', side_effect=[('a.api.radio-browser.info', [], []), ('b.api.radio-browser.info', [], [])]), mock.patch.object(hub.random, 'shuffle'), mock.patch.object(hub.urllib.request, 'urlopen', side_effect=[OSError('down'), good]) as request:
            self.assertEqual(hub.search_radio('country:FI'), [])
        self.assertIn('countrycode=FI', request.call_args.args[0].full_url)
        self.assertEqual(request.call_count, 2)

    def test_world_search_combines_filters_and_normalizes_metadata(self):
        import urllib.parse
        payload = [{'name': 'Jazz', 'stationuuid': 'abc', 'url_resolved': 'https://example.org/live', 'country': 'Finland', 'countrycode': 'FI', 'language': 'finnish,english', 'tags': 'jazz, folk', 'homepage': 'https://example.org'}]
        with mock.patch.object(hub.socket, 'getaddrinfo', side_effect=OSError()), mock.patch.object(hub.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            result = hub.search_radio('Jazz', country='fi', language='finnish', tag='folk', cache=False)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0].full_url).query)
        self.assertEqual(params, {'name': ['Jazz'], 'countrycode': ['FI'], 'language': ['finnish'], 'tag': ['folk'], 'limit': ['50'], 'hidebroken': ['true'], 'order': ['clickcount'], 'reverse': ['true']})
        self.assertEqual(result[0]['country'], 'Finland')
        self.assertEqual(result[0]['countrycode'], 'FI')
        self.assertEqual(result[0]['language'], 'finnish,english')
        self.assertEqual(result[0]['tags'], ['jazz', 'folk'])
        self.assertEqual(result[0]['website'], 'https://example.org')
        self.assertFalse((hub.STATE / 'search-cache.json').exists())

    def test_world_search_rejects_bad_filters_before_network(self):
        for query, kwargs in [('x', {'country': 'ZZ'}), ('x', {'country': 'FIN'}), ('x', {'tag': 'x' * 241}), ('x', {'language': 123}), ('tag:', {}), ('country:US', {'country': 'FI'})]:
            with self.subTest(query=query, kwargs=kwargs), mock.patch.object(hub.socket, 'getaddrinfo') as dns:
                with self.assertRaises(hub.HubError):
                    hub.search_radio(query, **kwargs)
                dns.assert_not_called()

    def test_broken_ipc_is_not_a_running_radio(self):
        with mock.patch.object(hub, 'ipc', side_effect=OSError('disconnected')):
            self.assertIsNone(hub.radio_info())

    def test_radio_status_includes_station_and_title(self):
        hub.update_state(radio_name='Station')
        with mock.patch.object(hub, 'ipc', side_effect=[False, False, 'Song']):
            info = hub.radio_info()
        with mock.patch.object(hub, 'all_info', return_value=[info]):
            text = hub.status()['text']
        self.assertIn('Station', text)
        self.assertIn('Song', text)

    @unittest.skipUnless(os.environ.get('LISTENING_HUB_CATALOG'), 'catalogue not provided')
    def test_catalogue_contract(self):
        data = json.loads(Path(os.environ['LISTENING_HUB_CATALOG']).read_text())
        seen = set()
        self.assertTrue(data['sources'])
        for item in data['sources']:
            for key in ('id', 'name', 'group', 'kind'):
                self.assertTrue(isinstance(item.get(key), str) and item[key])
            self.assertNotIn(item['id'], seen)
            seen.add(item['id'])
            self.assertIn(item['kind'], ('radio', 'web', 'app', 'discovery', 'search'))
            if item['kind'] in ('radio', 'web', 'discovery'):
                self.assertTrue(hub.valid_url(item.get('url', '')), item['id'])
            if item['kind'] == 'app':
                self.assertTrue(item.get('command') and all(isinstance(x, str) and x for x in item['command']))
            if item['kind'] == 'search':
                self.assertTrue(item.get('query'))

    def test_volume_values_are_bounded_and_validate_input(self):
        self.assertEqual(hub.volume_value('+5', 99), 100)
        self.assertEqual(hub.volume_value('-5', 2), 0)
        for value in ('101', '-101.5', 'NaN', '50%'):
            with self.assertRaises(hub.HubError):
                hub.volume_value(value, 50)
        with self.assertRaises(hub.HubError):
            hub.volume_value('+5', None)

    def test_player_and_master_volume_are_separate(self):
        with mock.patch.object(hub, 'all_info', return_value=[{'id': 'browser', 'status': 'Playing'}]), mock.patch.object(hub, 'player_volume', return_value=98), mock.patch.object(hub, 'run') as run:
            self.assertEqual(hub.set_volume('+5'), 100)
            self.assertEqual(run.call_args.args[0], ['playerctl', '-p', 'browser', 'volume', '1.0'])
        with mock.patch.object(hub, 'system_volume', return_value={'percent': 50, 'muted': False}), mock.patch.object(hub, 'run') as run:
            hub.set_volume('-5', master=True)
            self.assertEqual(run.call_args.args[0], ['wpctl', 'set-volume', '-l', '1.0', '@DEFAULT_AUDIO_SINK@', '0.45'])

    def test_profile_baseline_write_once_and_restore_preserves_transient_state(self):
        hub.update_state(selected='browser', radio_name='Now playing', pause_others_on_radio_start=False)
        hub.save_favorite({'name': 'Favorite', 'url': 'https://example.org/live'})
        hub.ensure_baseline()
        hub.update_state(selected='radio', radio_name='Still playing', pause_others_on_radio_start=True)
        hub.ensure_baseline()
        with mock.patch.object(hub, 'player_command') as control, mock.patch.object(hub, 'play') as play:
            hub.restore_profile('default')
        self.assertFalse(hub.preferences()['pause_others_on_radio_start'])
        self.assertEqual(hub.state()['selected'], 'radio')
        self.assertEqual(hub.state()['radio_name'], 'Still playing')
        self.assertEqual(len(hub.favorites()), 1)
        control.assert_not_called()
        play.assert_not_called()
        with self.assertRaises(hub.HubError):
            hub.save_profile('default')

    def test_profile_does_not_copy_url_query_tokens(self):
        hub.save_favorite({'name': 'Protected', 'url': 'https://example.org/live?token=secret'})
        hub.save_profile('private')
        self.assertNotIn('secret', json.dumps(hub.profiles()))

    def test_profile_preserves_public_station_query_parameters(self):
        hub.save_favorite({'name': 'Station', 'url': 'https://example.org/live?quality=high#charset=utf8'})
        hub.save_profile('evening')
        hub.save_json(hub.STATE / 'favorites.json', [])
        hub.restore_profile('evening')
        self.assertEqual(hub.favorites()[0]['url'], 'https://example.org/live?quality=high#charset=utf8')

    def test_selected_player_mute_remembers_volume(self):
        with mock.patch.object(hub, 'all_info', return_value=[{'id': 'browser', 'status': 'Playing'}]), mock.patch.object(hub, 'player_volume', side_effect=[37, 0]), mock.patch.object(hub, 'run') as run:
            hub.mute()
            hub.mute()
        self.assertEqual(run.call_args_list[0].args[0][-1], '0')
        self.assertEqual(run.call_args_list[1].args[0][-1], '0.37')

    def test_save_scene_references_favorite_without_stream_url(self):
        item = {'id': 'custom', 'name': 'My station', 'url': 'https://example.org/live?token=secret', 'kind': 'radio'}
        hub.update_state(selected='radio', radio_source_id='custom')
        hub.save_favorite(item)
        info = [{'id': 'radio', 'status': 'Playing'}]
        with mock.patch.object(hub, 'all_info', return_value=info), mock.patch.object(hub, 'player_volume', return_value=42), mock.patch.object(hub, 'ambience_module', side_effect=ImportError('missing')):
            scene = hub.save_scene('Evening')
        self.assertEqual(scene['source_id'], hub.favorites()[0]['id'])
        self.assertEqual(scene['volume'], 42)
        self.assertNotIn('secret', json.dumps(hub.scenes()))
        with mock.patch('session_client.request') as dispatch, mock.patch.object(hub, 'set_volume') as volume:
            hub.play_scene(scene['id'])
        self.assertEqual(dispatch.call_args.args[0], 'enter')
        recipe = dispatch.call_args.args[1]['recipe']
        self.assertEqual(recipe['music']['source_id'], hub.favorites()[0]['id'])
        self.assertEqual(recipe['music']['volume'], 42)
        self.assertNotIn('secret', json.dumps(recipe))
        volume.assert_not_called()

    def test_explicit_player_volume_and_mute_never_fall_back(self):
        infos = [{'id': 'a', 'status': 'Playing'}, {'id': 'b', 'status': 'Playing'}]
        hub.update_state(selected='b')
        with mock.patch.object(hub, 'all_info', return_value=infos), mock.patch.object(hub, 'player_volume', return_value=25), mock.patch.object(hub, 'run') as run:
            hub.set_volume('+5', target_id='a')
            hub.mute(target_id='a')
            self.assertEqual(run.call_args_list[0].args[0], ['playerctl', '-p', 'a', 'volume', '0.3'])
            self.assertEqual(run.call_args_list[1].args[0], ['playerctl', '-p', 'a', 'volume', '0'])
            with self.assertRaisesRegex(hub.HubError, 'no longer available'):
                hub.set_volume(30, target_id='gone')
            with self.assertRaisesRegex(hub.HubError, 'no longer available'):
                hub.mute(target_id='gone')
            self.assertEqual(run.call_count, 2)

    def test_cli_first_save_default_succeeds_but_second_fails(self):
        with mock.patch.object(hub.sys, 'argv', ['listening-hub', 'profile', 'save', 'default']), mock.patch.object(hub, 'notify'):
            self.assertEqual(hub.main(), 0)
            with mock.patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(hub.main(), 1)
        self.assertIn('default', hub.profiles())

    def test_cli_video_selects_only_after_success(self):
        module = mock.Mock()
        module.presets.return_value = []
        with mock.patch.object(hub.sys, 'argv', ['listening-hub', 'video', 'start', 'https://example.org/video']), mock.patch.object(hub, 'ambience_module', return_value=module), mock.patch.object(hub, 'notify'):
            module.start.side_effect = ValueError('failed')
            with mock.patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(hub.main(), 1)
            self.assertNotEqual(hub.state().get('selected'), 'video')
            module.start.side_effect = None
            self.assertEqual(hub.main(), 0)
            self.assertEqual(hub.state()['selected'], 'video')

    def test_url_rejects_non_http_and_credentials(self):
        for url in ('file:///etc/passwd', 'javascript:alert(1)', 'https://user:pass@example.com/', 'https://', 'https://example.org/\nmalicious'):
            self.assertFalse(hub.valid_url(url), url)
        self.assertTrue(hub.valid_url('https://radio.example/stream'))

    def test_youtube_audio_requires_one_explicit_video(self):
        for url in ('https://music.youtube.com/search?q=nightcore',
                    'https://www.youtube.com/playlist?list=abc',
                    'https://youtube.com.evil.test/watch?v=abcdefghijk',
                    'https://user@youtube.com/watch?v=abcdefghijk',
                    'https://youtube.com:bad/watch?v=abcdefghijk'):
            with self.subTest(url=url), self.assertRaises(hub.HubError):
                hub.youtube_video_url(url)
        self.assertEqual(hub.youtube_video_url('https://music.youtube.com/watch?v=abcdefghijk&list=example'),
                         'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertEqual(hub.youtube_video_url('https://youtu.be/abcdefghijk?t=2'),
                         'https://www.youtube.com/watch?v=abcdefghijk')

    def test_youtube_audio_is_explicit_and_requires_extractor(self):
        with mock.patch.object(hub.shutil, 'which', return_value=None), mock.patch.object(hub, 'start_radio') as start:
            with self.assertRaisesRegex(hub.HubError, 'yt-dlp'):
                hub.start_youtube_audio('https://youtu.be/abcdefghijk')
            start.assert_not_called()
        with mock.patch.object(hub.shutil, 'which', return_value='/nix/store/bin/yt-dlp'), mock.patch.object(hub, 'start_radio') as start:
            hub.start_youtube_audio('https://youtu.be/abcdefghijk')
            self.assertEqual(start.call_args.kwargs['ytdlp'], '/nix/store/bin/yt-dlp')
            self.assertEqual(start.call_args.args[0]['url'], 'https://www.youtube.com/watch?v=abcdefghijk')

    def test_verified_stations_offer_direct_play_and_website(self):
        path = Path(os.environ.get('LISTENING_HUB_CATALOG', Path(__file__).resolve().parents[1] / 'src/catalogue.json'))
        catalogue = json.loads(path.read_text())
        sources = {item['id']: item for item in catalogue['sources']}
        for key in ('ylex', 'yle-klassinen', 'radio-dismuke'):
            self.assertEqual(sources[key]['kind'], 'radio')
            self.assertTrue(hub.valid_url(sources[key]['url']))
            self.assertTrue(hub.valid_url(sources[key]['website']))

    def test_radio_audio_survives_failed_start_and_absent_player(self):
        hub.update_state(selected='browser')
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run') as run, mock.patch.object(hub, 'ipc', side_effect=[27, True]), mock.patch.object(hub.socket, 'socket', side_effect=OSError('failed')):
            run.return_value.returncode = 0
            with self.assertRaises(hub.HubError):
                hub.start_radio({'name': 'one', 'url': 'https://example.org/one'})
            launch = next(c.args[0] for c in run.call_args_list if c.args[0][0] == 'systemd-run')
            self.assertIn('--volume=0', launch)
            self.assertIn('--mute=yes', launch)
        self.assertEqual(hub.state()['selected'], 'browser')
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run') as run, mock.patch.object(hub, 'ipc', side_effect=OSError('absent')), mock.patch.object(hub.socket, 'socket', side_effect=OSError('failed')):
            run.return_value.returncode = 0
            with self.assertRaises(hub.HubError):
                hub.start_radio({'name': 'two', 'url': 'https://example.org/two'})
            launch = next(c.args[0] for c in run.call_args_list if c.args[0][0] == 'systemd-run')
            self.assertIn('--volume=0', launch)
            self.assertIn('--mute=yes', launch)

    def test_paused_radio_controls_remember_actual_volume_and_mute(self):
        with mock.patch.object(hub, 'all_info', return_value=[{'id': 'radio', 'status': 'Paused'}]), mock.patch.object(hub, 'ipc') as ipc:
            def command(args):
                if args == ['get_property', 'volume']:
                    return 38
                if args == ['get_property', 'mute']:
                    return True
            ipc.side_effect = command
            hub.set_volume(45, target_id='radio')
            self.assertEqual(hub.state()['radio_audio'], {'volume': 45, 'muted': True})
            hub.mute(target_id='radio')
            self.assertEqual(hub.state()['radio_audio'], {'volume': 38, 'muted': False})

    def test_radio_settings_do_not_follow_browser_or_master(self):
        hub.update_state(radio_audio={'volume': 31, 'muted': True})
        with mock.patch.object(hub, 'all_info', return_value=[{'id': 'browser', 'status': 'Playing'}]), mock.patch.object(hub, 'player_volume', return_value=0), mock.patch.object(hub, 'run'), mock.patch.object(hub, 'system_volume', return_value={'percent': 40}):
            hub.set_volume(50)
            hub.set_volume(60, master=True)
            hub.mute(master=True)
        self.assertEqual(hub.state()['radio_audio'], {'volume': 31, 'muted': True})

    def test_youtube_extractor_is_isolated_from_user_configuration(self):
        sock = FakeSocket([{'event': 'file-loaded'}, {'event': 'playback-restart'}])
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run') as run, mock.patch.object(hub.socket, 'socket', return_value=sock), mock.patch.object(hub, 'ipc', return_value=None):
            run.return_value.returncode = 0
            hub.start_radio({'name': 'Video', 'url': 'https://www.youtube.com/watch?v=abcdefghijk'}, ytdlp='/mock/yt-dlp')
        command = next(c.args[0] for c in run.call_args_list if c.args[0][0] == 'systemd-run')
        self.assertIn('--no-config', command)
        self.assertIn('--no-video', command)
        self.assertIn('--ytdl-format=bestaudio/best', command)
        self.assertIn('--script-opts=ytdl_hook-ytdl_path=/mock/yt-dlp', command)
        self.assertIn('--ytdl-raw-options=ignore-config=,no-playlist=', command)

    def test_youtube_not_ready_does_not_pause_an_existing_browser(self):
        hub.update_state(selected='browser', pause_others_on_radio_start=True)
        sock = FakeSocket([{'event': 'file-loaded'}])
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run') as run, mock.patch.object(hub.socket, 'socket', return_value=sock), mock.patch.object(hub, 'all_info', return_value=[{'id':'browser','status':'Playing'}]), mock.patch.object(hub, 'player_command') as control:
            run.return_value.returncode = 0
            with self.assertRaisesRegex(hub.HubError, 'closed before stream loaded'):
                hub.start_radio({'name':'Video','url':'https://youtu.be/abcdefghijk'}, ytdlp='/mock/yt-dlp')
        control.assert_not_called()
        self.assertEqual(hub.state()['selected'], 'browser')

    def test_radio_loading_has_absolute_deadline_despite_unrelated_events(self):
        sock = FakeSocket([])
        sock.recv = mock.Mock(return_value=b'{"event":"idle"}\n')
        ticks = iter(range(0,1000,10))
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run') as run, mock.patch.object(hub.socket, 'socket', return_value=sock), mock.patch.object(hub.time, 'monotonic', side_effect=lambda:next(ticks)):
            run.return_value.returncode = 0
            with self.assertRaisesRegex(hub.HubError, 'timed out'):
                hub.start_radio({'name':'Video','url':'https://youtu.be/abcdefghijk'}, ytdlp='/mock/yt-dlp')
        self.assertLess(sock.recv.call_count, 5)

    def test_favorites_are_atomic_and_deduplicated(self):
        hub.save_favorite({'name': 'one\nstation', 'url': 'https://example.org/live'})
        hub.save_favorite({'name': 'renamed', 'url': 'https://example.org/live'})
        self.assertEqual([x['name'] for x in hub.favorites()], ['renamed'])
        with self.assertRaises(hub.HubError):
            hub.save_favorite({'url': 'file:///tmp/a'})
        self.assertEqual(len(hub.favorites()), 1)

    def test_selected_player_falls_back_deterministically(self):
        items = [{'id': 'a', 'status': 'Paused'}, {'id': 'b', 'status': 'Playing'}]
        hub.update_state(selected='gone')
        self.assertEqual(hub.target(items)['id'], 'b')
        hub.update_state(selected='a')
        self.assertEqual(hub.target(items)['id'], 'a')

    def test_status_escapes_markup(self):
        info = {'id': '<browser>', 'status': 'Playing', 'artist': '<artist>', 'title': 'A & B'}
        with mock.patch.object(hub, 'all_info', return_value=[info]):
            result = hub.status()
        self.assertIn('&lt;artist&gt;', result['text'])
        self.assertIn('A &amp; B', result['tooltip'])
        self.assertNotIn('<browser>', result['tooltip'])

    def test_live_radio_rejects_track_skip(self):
        with self.assertRaisesRegex(hub.HubError, 'no track navigation'):
            hub.player_command('radio', 'next')

    def start(self, events, enabled):
        hub.update_state(pause_others_on_radio_start=enabled)
        events = list(events)
        if any(e.get('event') == 'file-loaded' for e in events): events.append({'event':'playback-restart'})
        sock = FakeSocket(events)
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run') as run, mock.patch.object(hub, 'all_info', return_value=[{'id':'browser', 'status':'Playing'}]), mock.patch.object(hub.socket, 'socket', return_value=sock), mock.patch.object(hub, 'player_command') as command, mock.patch.object(hub, 'ipc', return_value=None):
            run.return_value.returncode = 0
            try:
                hub.start_radio({'name':'Station', 'url':'https://example.org/live'})
            finally:
                self.commands = command.call_args_list
                self.runs = run.call_args_list

    def test_overlap_is_default_and_configurable_after_load(self):
        self.start([{'event':'file-loaded'}], False)
        self.assertEqual(self.commands, [])
        self.assertIn('/mock/mpv', next(c.args[0] for c in self.runs if c.args[0][0] == 'systemd-run'))
        self.start([{'event':'file-loaded'}], True)
        self.assertEqual(self.commands, [mock.call('browser', 'pause')])
        self.assertEqual(hub.state()['selected'], 'radio')

    def test_failed_stream_never_pauses_other_player(self):
        with self.assertRaisesRegex(hub.HubError, 'could not be opened'):
            self.start([{'event':'end-file', 'reason':'error'}], True)
        self.assertEqual(self.commands, [])
        self.assertNotEqual(hub.state().get('selected'), 'radio')
        self.assertEqual(self.runs[-1].args[0][:3], ['systemctl', '--user', 'stop'])

    def test_failed_replacement_keeps_existing_radio_socket(self):
        hub.RUNTIME.mkdir(mode=0o700)
        hub.SOCKET.write_bytes(b'existing control socket')
        hub.update_state(selected='radio', radio_source_id='old')
        events = FakeSocket([{'event':'file-loaded'}, {'event':'playback-restart'}])
        def command(args, **kwargs):
            if args[:3] == ['systemctl','--user','stop'] and args[-1] == 'listening-hub-radio.service':
                return mock.Mock(returncode=1)
            if args[:3] == ['systemctl','--user','show']:
                return mock.Mock(returncode=0, stdout='loaded\n')
            return mock.Mock(returncode=0)
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run', side_effect=command), mock.patch.object(hub.socket, 'socket', return_value=events), mock.patch.object(hub, 'ipc', return_value=None):
            with self.assertRaisesRegex(hub.HubError, 'preserved'):
                hub.start_radio({'name': 'new', 'url': 'https://example.org/live'})
        self.assertEqual(hub.SOCKET.read_bytes(), b'existing control socket')
        self.assertEqual(hub.state()['radio_source_id'], 'old')

    def test_failed_candidate_does_not_stop_previous_radio(self):
        hub.update_state(selected='radio', radio_source_id='old', radio_unit='listening-hub-radio-123456abcdef.service', radio_socket='radio-123456abcdef.sock')
        previous = hub.radio_socket()
        previous.parent.mkdir(mode=0o700)
        previous.touch()
        with mock.patch.object(hub.shutil, 'which', return_value='/mock/mpv'), mock.patch.object(hub, 'run', return_value=mock.Mock(returncode=0)) as run, mock.patch.object(hub.socket, 'socket', return_value=FakeSocket([{'event':'end-file', 'reason':'error'}])), mock.patch.object(hub, 'ipc', return_value=None):
            with self.assertRaises(hub.HubError): hub.start_radio({'id':'new','name':'New','url':'https://example.org/new'})
        self.assertEqual(hub.state()['radio_source_id'], 'old')
        self.assertTrue(previous.exists())
        stops = [call.args[0][-1] for call in run.call_args_list if call.args[0][:3] == ['systemctl','--user','stop']]
        self.assertEqual(len(stops), 1)
        self.assertNotIn('listening-hub-radio-123456abcdef.service', stops)

    def test_cancelled_start_never_stops_or_launches_a_player(self):
        import threading
        event = threading.Event(); event.set()
        with mock.patch.object(hub, 'run') as run:
            with self.assertRaisesRegex(hub.HubError, 'cancelled'):
                hub.start_radio({'url':'https://example.org/live'}, cancel_event=event)
        run.assert_not_called()

    def test_playlist_redirect_loads_stream_before_pausing(self):
        self.start([{'event': 'end-file', 'reason': 'redirect'},
                    {'event': 'file-loaded'}], True)
        self.assertEqual(self.commands, [mock.call('browser', 'pause')])
        launch = next(call.args[0] for call in self.runs
                      if call.args[0][0] == 'systemd-run')
        self.assertIn('--property=PartOf=graphical-session.target', launch)

    def test_search_rejects_bogus_streams(self):
        response = io.BytesIO(json.dumps([{'name': 'Bad', 'url_resolved':'file:///tmp/a'}, {'name':'Good\nName', 'url_resolved':'https://example.org', 'stationuuid':'1'}]).encode())
        with mock.patch.object(hub.socket, 'getaddrinfo', side_effect=OSError('DNS unavailable')), mock.patch.object(hub.urllib.request, 'urlopen', return_value=response):
            results = hub.search_radio('test')
        self.assertEqual([x['name'] for x in results], ['Good Name'])
        self.assertEqual(len(hub.load_json(hub.STATE / 'search-cache.json', [])), 1)

if __name__ == '__main__':
    unittest.main()
