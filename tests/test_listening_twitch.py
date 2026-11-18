import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import random
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import twitch_channels as twitch


class TwitchTests(unittest.TestCase):
    def setUp(self):
        stored = patch.object(twitch.local_credentials, 'load', return_value=twitch.local_credentials.Credentials())
        stored.start(); self.addCleanup(stored.stop)

    def test_unknown_curated_is_not_offline(self):
        with patch.dict(os.environ, {}, clear=True):
            result = twitch.discover('music', curated=['Example'])
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['channels'][0]['live'])
        self.assertEqual(result['channels'][0]['url'], 'https://www.twitch.tv/example')

    def test_live_discovery_uses_category_id_and_deduplicates(self):
        stream = {'user_login': 'example', 'user_name': 'Example', 'title': 'hello', 'game_id': '42'}
        with patch.object(twitch, '_get', side_effect=[[{'id': '42'}], [stream], [stream]]) as get:
            result = twitch.discover('asmr', ['example'], 'client', 'token')
        self.assertEqual(get.call_args_list[1].args[1]['game_id'], '42')
        self.assertEqual(len(result['channels']), 1)
        self.assertTrue(result['channels'][0]['live'])
        self.assertEqual(result['status'], 'live')

    def test_empty_and_api_failure_are_distinct(self):
        with patch.object(twitch, '_get', side_effect=[[{'id': '42'}], []]):
            self.assertEqual(twitch.discover('music', [], 'c', 't')['status'], 'offline')
        with patch.object(twitch, '_get', side_effect=OSError('failed')):
            self.assertEqual(twitch.discover('music', ['x'], 'c', 't')['status'], 'unknown')

    def test_saved_expired_login_is_renewed_once_and_discovery_retried(self):
        stale = urllib.error.HTTPError('https://api.twitch.tv/helix/games', 401, 'Unauthorized', {}, None)
        stream = {'user_login': 'example', 'user_name': 'Example', 'game_id': '42'}
        with patch.object(twitch.local_credentials, 'load', return_value=twitch.local_credentials.Credentials('client', 'old', 'Twitch CLI')), \
             patch.object(twitch.local_credentials, 'refresh', return_value=twitch.local_credentials.Credentials('client', 'new', 'Twitch CLI')) as renew, \
             patch.object(twitch, '_get', side_effect=[stale, [{'id':'42'}], [stream]]) as get:
            result=twitch.discover('music', [])
        self.assertEqual(result['status'], 'live')
        renew.assert_called_once_with('old')
        self.assertEqual(get.call_args.args[-1], 'new')

    def test_second_401_and_explicit_credentials_never_refresh_in_a_loop(self):
        stale = urllib.error.HTTPError('https://api.twitch.tv/helix/games', 401, 'Unauthorized', {}, None)
        with patch.object(twitch, '_get', side_effect=stale), patch.object(twitch.local_credentials, 'refresh') as renew:
            result=twitch.discover('music', ['saved'], 'client', 'old')
        renew.assert_not_called()
        self.assertEqual(result['reason'], 'authentication')
        self.assertEqual(result['http_status'], 401)
        self.assertIsNone(result['channels'][0]['live'])
        with patch.object(twitch.local_credentials, 'load', return_value=twitch.local_credentials.Credentials('client','old','Twitch CLI')), \
             patch.object(twitch.local_credentials, 'refresh', return_value=twitch.local_credentials.Credentials('client','new','Twitch CLI')) as renew, \
             patch.object(twitch, '_get', side_effect=stale):
            result=twitch.discover('asmr', [])
        renew.assert_called_once()
        self.assertEqual(result['reason'], 'authentication')

    def test_renewal_error_remains_unknown_and_explains_reconnection(self):
        stale = urllib.error.HTTPError('https://api.twitch.tv/helix/games', 401, 'Unauthorized', {}, None)
        with patch.object(twitch.local_credentials, 'load', return_value=twitch.local_credentials.Credentials('client','old','Twitch CLI')), \
             patch.object(twitch.local_credentials, 'refresh', side_effect=twitch.local_credentials.CredentialError('Reconnect Twitch login')), \
             patch.object(twitch, '_get', side_effect=stale):
            result=twitch.discover('music', ['saved'])
        self.assertIn('Reconnect', result['message'])
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['curated_status'], {'saved':'unknown'})

    def test_permission_rate_limit_and_connection_failures_are_distinct(self):
        for code,reason in ((403,'permission'), (429,'rate_limit'), (503,'service')):
            with self.subTest(code=code), patch.object(twitch, '_get', side_effect=urllib.error.HTTPError('https://api.twitch.tv/helix/games',code,'error',{},None)):
                result=twitch.discover('music', [], 'client','token')
            self.assertEqual(result['status'],'unknown')
            self.assertEqual(result['reason'],reason)
        with patch.object(twitch, '_get', side_effect=OSError('No route')):
            self.assertEqual(twitch.discover('music', [], 'client','token')['reason'],'connection')

    def test_follows_refresh_preserves_profile_scope_checks_and_is_bounded(self):
        stale=urllib.error.HTTPError('https://id.twitch.tv/oauth2/validate',401,'Unauthorized',{},None)
        validation={'client_id':'client','login':'listener','user_id':'123','scopes':['user:read:follows']}
        with patch.object(twitch.local_credentials,'load',return_value=twitch.local_credentials.Credentials('client','old','Twitch CLI')), \
             patch.object(twitch.local_credentials,'refresh',return_value=twitch.local_credentials.Credentials('client','new','Twitch CLI')) as renew, \
             patch.object(twitch,'_validate_token',side_effect=[stale,validation]), \
             patch.object(twitch,'_follow_page',return_value={'data':[],'pagination':{}}):
            result=twitch.import_following('listener')
        self.assertEqual(result['status'],'follows');renew.assert_called_once_with('old')
        with patch.object(twitch,'_validate_token',side_effect=stale), patch.object(twitch.local_credentials,'refresh') as renew:
            result=twitch.import_following('listener', client_id='client',access_token='token')
        renew.assert_not_called();self.assertEqual(result['status'],'needs_connection')

    def test_authenticated_requests_never_follow_redirects(self):
        seen = []
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(True)
                self.send_response(200);self.end_headers();self.wfile.write(b'{"data": []}')
            def log_message(self, *_): pass
        target=ThreadingHTTPServer(('127.0.0.1',0),Destination)
        destination='http://127.0.0.1:'+str(target.server_port)+'/data'
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302);self.send_header('Location',destination);self.end_headers()
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        threads=[threading.Thread(target=x.serve_forever,kwargs={'poll_interval':.01},daemon=True) for x in (target,server)]
        for thread in threads:thread.start()
        try:
            request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/redirect',headers={'Authorization':'Bearer synthetic-token'})
            with self.assertRaises(urllib.error.HTTPError) as error:twitch._json_get(request)
            self.assertEqual(error.exception.code,302)
            error.exception.close()
            self.assertEqual(seen,[])
        finally:
            for item in (server,target):item.shutdown();item.server_close()
            for thread in threads:thread.join(1)

    def test_oversized_api_response_is_rejected(self):
        with patch.object(twitch.urllib.request,'build_opener') as builder:
            builder.return_value.open.return_value.__enter__.return_value.read.return_value=b' '*(2*1024*1024+1)
            with self.assertRaisesRegex(ValueError,'oversized'):
                twitch._json_get(urllib.request.Request('https://api.twitch.tv/helix/games'))

    def test_curated_live_other_category_is_not_eligible(self):
        wrong = {'user_login': 'example', 'game_id': '99'}
        with patch.object(twitch, '_get', side_effect=[[{'id': '42'}], [], [wrong]]):
            result = twitch.discover('music', ['example', 'offline'], 'c', 't')
        self.assertEqual(result['channels'], [])
        self.assertEqual(result['curated_status'], {'example': 'style_mismatch', 'offline': 'offline'})
        with patch.object(twitch, '_get', side_effect=OSError()):
            self.assertEqual(twitch.discover('music', ['example'], 'c', 't')['curated_status']['example'], 'unknown')

    def test_follow_import_checks_scope_and_identity_before_fetch(self):
        token = {'client_id': 'c', 'login': 'listener', 'user_id': '123', 'scopes': ['user:read:follows']}
        for change in ({'login': 'someoneelse'}, {'client_id': 'different'}, {'scopes': []}, {'user_id': None}):
            with patch.object(twitch, '_validate_token', return_value=dict(token, **change)), \
                 patch.object(twitch, '_follow_page') as fetch:
                self.assertEqual(twitch.import_following('listener', client_id='c', access_token='t')['status'], 'needs_connection')
                fetch.assert_not_called()

    def test_follow_import_paginates_and_never_writes_preferences(self):
        token = {'client_id': 'c', 'login': 'listener', 'user_id': '123', 'scopes': ['user:read:follows']}
        pages = [{'data': [{'broadcaster_login': 'first', 'broadcaster_name': 'First'}], 'pagination': {'cursor': 'next'}},
                 {'data': [{'broadcaster_login': 'second'}], 'pagination': {}}]
        with patch.object(twitch, '_validate_token', return_value=token), \
             patch.object(twitch, '_follow_page', side_effect=pages) as fetch, \
             patch.object(twitch, 'save_preferences') as save:
            result = twitch.import_following('listener', client_id='c', access_token='t')
        self.assertEqual([item['login'] for item in result['channels']], ['first', 'second'])
        self.assertEqual(fetch.call_args_list[1].args[0]['after'], 'next')
        self.assertTrue(result['complete'])
        save.assert_not_called()
        with patch.object(twitch, '_validate_token', return_value=token), \
             patch.object(twitch, '_follow_page', return_value=pages[0]):
            self.assertFalse(twitch.import_following('listener', client_id='c', access_token='t', max_pages=1)['complete'])

    def test_follow_import_missing_or_failed_is_not_empty_follow_list(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(twitch.import_following('listener')['status'], 'needs_connection')
        with patch.object(twitch, '_validate_token', side_effect=OSError()):
            self.assertEqual(twitch.import_following('listener', client_id='c', access_token='t')['status'], 'error')

    def test_shuffle_excludes_and_avoids_immediate_repeat(self):
        result = {'channels': [{'login': x} for x in ['a', 'b', 'c']]}
        self.assertEqual(twitch.choose(result, previous='a', excluded=['c'], rng=random.Random(1))['login'], 'b')
        self.assertIsNone(twitch.choose(result, excluded=['a', 'b', 'c']))
        self.assertIsNone(twitch.choose({'channels': [{'login': 'a'}]}, previous='a'))

    def test_private_preferences_preserve_other_style_and_exclusions(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_CONFIG_HOME': folder}):
            twitch.save_preferences('music', ['Alpha', 'alpha'], ['Ignored'])
            twitch.save_preferences('asmr', ['Beta'], ['Ignored'])
            self.assertEqual(twitch.curated_channels('music'), ['alpha'])
            self.assertEqual(twitch.curated_channels('asmr'), ['beta'])
            self.assertEqual(twitch.excluded_channels(), ['ignored'])
            self.assertEqual(twitch.preferences_path().stat().st_mode & 0o777, 0o600)

    def test_bad_preferences_are_rejected(self):
        with self.assertRaises(ValueError):
            twitch.discover('music', ['../bad'])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_CONFIG_HOME': folder}):
            self.assertEqual(twitch.curated_channels('music'), [])

    def test_channel_names_accept_names_and_public_urls_and_deduplicate(self):
        self.assertEqual(twitch.parse_names('LofiGirl, https://www.twitch.tv/lofigirl/; other_name'), ['lofigirl', 'other_name'])

    def test_channel_names_reject_credentials_foreign_hosts_and_nonchannels(self):
        for value in ['https://twitch.tv/name?token=secret', 'https://other.org/name', 'https://user@twitch.tv/name',
                      'https://twitch.tv/name/clip', 'https://twitch.tv:bad/name']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                twitch.parse_names(value)


if __name__ == '__main__':
    unittest.main()
