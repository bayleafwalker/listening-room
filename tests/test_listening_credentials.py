"""Credential resolution boundaries use synthetic files and no network."""
import os
import http.client
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import twitch_credentials as credentials

class CredentialTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        env=patch.dict(os.environ,{'HOME':str(self.root),'XDG_CONFIG_HOME':str(self.root/'config')},clear=True)
        env.start();self.addCleanup(env.stop)
        home=patch.object(Path,'home',return_value=self.root)
        home.start();self.addCleanup(home.stop)
        self.file=self.root/'config/twitch-cli/.twitch-cli.env'
        self.file.parent.mkdir(parents=True)

    def write(self,token='synthetic-token'):
        self.file.write_text('CLIENTID="synthetic-client"\nACCESSTOKEN='+token+'\nCLIENTSECRET=unused\nREFRESHTOKEN=unused\n')
        self.file.chmod(0o600)

    def test_reads_private_cli_pair_without_rewriting_or_retaining_other_fields(self):
        self.write();before=self.file.read_bytes();mtime=self.file.stat().st_mtime_ns
        result=credentials.load()
        self.assertEqual((result.client_id,result.access_token,result.source),('synthetic-client','synthetic-token','Twitch CLI'))
        self.assertEqual(self.file.read_bytes(),before)
        self.assertEqual(self.file.stat().st_mtime_ns,mtime)
        self.assertNotIn('synthetic-token',repr(result));self.assertNotIn('synthetic-client',repr(result))
        self.assertFalse(hasattr(result,'client_secret'))
        self.write('replacement-token')
        self.assertEqual(credentials.load().access_token,'replacement-token')

    def test_environment_pair_wins_and_partial_pairs_never_mix_with_cli(self):
        self.write()
        with patch.dict(os.environ,{'TWITCH_CLIENT_ID':'environment-client','TWITCH_ACCESS_TOKEN':'environment-token'}):
            self.assertEqual(credentials.load().source,'environment')
        with patch.dict(os.environ,{'TWITCH_CLIENT_ID':'environment-client'}):
            with self.assertRaises(credentials.CredentialError):credentials.load()

    def test_rejects_public_permissions_symlinks_and_wrong_owner(self):
        self.write();self.file.chmod(0o644)
        with self.assertRaises(credentials.CredentialError):credentials.load()
        self.file.chmod(0o600)
        with patch.object(credentials.os,'getuid',return_value=os.getuid()+1):
            with self.assertRaises(credentials.CredentialError):credentials.load()
        target=self.file.with_suffix('.target');self.file.rename(target);self.file.symlink_to(target)
        with self.assertRaises(credentials.CredentialError):credentials.load()

    def test_malformed_file_has_value_free_error(self):
        self.write('"unclosed-sensitive-sentinel')
        with self.assertRaises(credentials.CredentialError) as caught:credentials.load()
        self.assertNotIn('sensitive-sentinel',str(caught.exception))

    def test_missing_login_has_no_secret_values(self):
        self.assertFalse(credentials.load().access_token)
        self.assertEqual(credentials.load().source,'none')

    def response(self, value=None, raw=None):
        class Response(io.BytesIO):
            status=200
            def geturl(self):return credentials.TOKEN_ENDPOINT
        return Response(raw if raw is not None else json.dumps(value or {
            'access_token':'rotated-access','refresh_token':'rotated-refresh','token_type':'bearer'}).encode())

    def opener(self, response=None, error=None):
        opener=Mock()
        opener.open.side_effect=error
        if error is None:opener.open.return_value=response or self.response()
        return patch.object(credentials.urllib.request,'build_opener',return_value=opener),opener

    def test_refresh_rotates_pair_atomically_and_preserves_unknown_lines(self):
        raw=(b'# preserve this comment\r\nCLIENTID="synthetic-client"\r\nACCESSTOKEN=old-access\r\n'
             b'CLIENTSECRET=synthetic-secret\r\nREFRESHTOKEN=old-refresh\r\nOTHER_SETTING="keep all of this"\r\n')
        self.file.write_bytes(raw);self.file.chmod(0o600);inode=self.file.stat().st_ino
        mocked,opener=self.opener()
        with mocked:result=credentials.refresh('old-access')
        self.assertEqual(result.access_token,'rotated-access')
        self.assertEqual(credentials.load().access_token,'rotated-access')
        self.assertEqual(credentials._read_cli(self.file)['REFRESHTOKEN'],'rotated-refresh')
        self.assertEqual(self.file.stat().st_mode&0o777,0o600)
        self.assertNotEqual(self.file.stat().st_ino,inode)
        self.assertIn(b'# preserve this comment\r\n',self.file.read_bytes())
        self.assertIn(b'OTHER_SETTING="keep all of this"\r\n',self.file.read_bytes())
        self.assertIn(b'CLIENTSECRET=synthetic-secret\r\n',self.file.read_bytes())
        lock=self.file.parent/'.listening-room-refresh.lock'
        self.assertEqual(lock.stat().st_mode&0o777,0o600)
        self.assertFalse(list(self.file.parent.glob('.listening-room-token-*')))
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,credentials.TOKEN_ENDPOINT)
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(urllib.parse.parse_qs(request.data.decode()),{
            'grant_type':['refresh_token'],'client_id':['synthetic-client'],
            'client_secret':['synthetic-secret'],'refresh_token':['old-refresh']})
        self.assertNotIn('rotated-access',repr(result))

    def test_special_refresh_characters_roundtrip_form_and_dotenv(self):
        old='synthetic+/percent%and&equals=hash#quote\''
        new='new+/percent%and&equals=hash#quote\''
        self.write('old-access')
        self.file.write_text(self.file.read_text().replace('REFRESHTOKEN=unused','REFRESHTOKEN='+shlex.quote(old)))
        mocked,opener=self.opener(self.response({'access_token':'new-access','refresh_token':new}))
        with mocked:credentials.refresh('old-access')
        posted=urllib.parse.parse_qs(opener.open.call_args.args[0].data.decode())
        self.assertEqual(posted['refresh_token'],[old])
        self.assertEqual(credentials._read_cli(self.file)['REFRESHTOKEN'],new)

    def test_explicit_and_partial_environment_overrides_are_never_changed_or_renewed(self):
        self.write();before=self.file.read_bytes()
        for values in ({'TWITCH_CLIENT_ID':'environment-client','TWITCH_ACCESS_TOKEN':'environment-access'},
                       {'TWITCH_CLIENT_ID':'partial-client'},{'TWITCH_ACCESS_TOKEN':'partial-access'}):
            with patch.dict(os.environ,values),patch.object(credentials,'_renew') as renew:
                snapshot=dict(os.environ)
                with self.assertRaises(credentials.CredentialError) as error:credentials.refresh('synthetic-token')
                self.assertEqual(error.exception.kind,'reconnect')
                self.assertEqual(dict(os.environ),snapshot)
                renew.assert_not_called()
        self.assertEqual(self.file.read_bytes(),before)

    def test_latest_saved_pair_wins_without_another_refresh(self):
        self.write('already-rotated')
        with patch.object(credentials,'_renew') as renew:result=credentials.refresh('failed-old-token')
        self.assertEqual((result.client_id,result.access_token),('synthetic-client','already-rotated'))
        renew.assert_not_called()

    def test_empty_optional_fields_allow_load_but_require_reconnect_on_refresh(self):
        self.write('old-access')
        self.file.write_text(self.file.read_text().replace('CLIENTSECRET=unused',"CLIENTSECRET='' ").replace('REFRESHTOKEN=unused','REFRESHTOKEN='))
        self.assertEqual(credentials.load().access_token,'old-access')
        before=self.file.read_bytes()
        with patch.object(credentials,'_renew') as renew:
            with self.assertRaises(credentials.CredentialError) as error:credentials.refresh('old-access')
        self.assertEqual(error.exception.kind,'reconnect');renew.assert_not_called()
        self.assertEqual(before,self.file.read_bytes())

    def test_rejected_refresh_keeps_original_file_and_sanitizes_errors(self):
        self.write('old-access');before=self.file.read_bytes()
        for code in (400,401,403):
            secret='sensitive-response-and-url-sentinel'
            error=urllib.error.HTTPError('https://example.test/'+secret,code,secret,{},io.BytesIO(secret.encode()))
            mocked,_=self.opener(error=error)
            with mocked,self.assertRaises(credentials.CredentialError) as caught:credentials.refresh('old-access')
            self.assertEqual(caught.exception.kind,'reconnect')
            self.assertNotIn(secret,str(caught.exception))
            self.assertEqual(before,self.file.read_bytes())

    def test_network_failure_and_partial_response_are_retryable_and_value_free(self):
        self.write('old-access');before=self.file.read_bytes()
        for error in (urllib.error.URLError('sensitive-network-sentinel'),
                      TimeoutError('sensitive-network-sentinel'),
                      http.client.IncompleteRead(b'sensitive-network-sentinel',123)):
            mocked,_=self.opener(error=error)
            with mocked,self.assertRaises(credentials.CredentialError) as caught:credentials.refresh('old-access')
            self.assertEqual(caught.exception.kind,'transient')
            self.assertNotIn('sensitive-network-sentinel',str(caught.exception))
            self.assertEqual(before,self.file.read_bytes())

    def test_malformed_and_oversized_remote_response_preserve_old_file(self):
        self.write('old-access');before=self.file.read_bytes()
        for raw in (b'not-json-sensitive-sentinel',b'x'*(credentials.MAX_RESPONSE+1),
                    json.dumps({'access_token':'sensitive-sentinel','refresh_token':None}).encode()):
            mocked,_=self.opener(self.response(raw=raw))
            with mocked,self.assertRaises(credentials.CredentialError) as caught:credentials.refresh('old-access')
            self.assertNotIn('sensitive-sentinel',str(caught.exception))
            self.assertEqual(before,self.file.read_bytes())

    @unittest.skip('ORIGINAL, superseded by test_token_post_redirects_are_refused_on_the_wire (R-src1, acceptance author, WP4): '
                   'it called the private _NoRedirect class and mocked build_opener to raise, so it never showed that the real '
                   'renewal request refuses a redirect')
    def test_redirect_handler_refuses_token_post_redirects(self):
        handler=credentials._NoRedirect()
        self.assertIsNone(handler.redirect_request(None,None,302,'redirect',{},'https://example.test/steal'))
        self.write('old-access');before=self.file.read_bytes()
        error=urllib.error.HTTPError(credentials.TOKEN_ENDPOINT,302,'sensitive-redirect',{},io.BytesIO(b'sensitive-redirect'))
        mocked,_=self.opener(error=error)
        with mocked,self.assertRaises(credentials.CredentialError) as caught:credentials.refresh('old-access')
        self.assertNotIn('sensitive-redirect',str(caught.exception))
        self.assertEqual(before,self.file.read_bytes())

    def test_token_post_redirects_are_refused_on_the_wire(self):
        # R-src1: a real loopback token endpoint answers the renewal with a redirect to a second server. The renewal
        # must fail as a value-free retryable error, leave the login file alone, and never reach the second server.
        import http.server
        stolen = []

        class Thief(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                stolen.append((self.command, self.path)); self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            do_POST = do_GET

            def log_message(self, *args):
                pass
        thief = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Thief)
        target = 'http://127.0.0.1:%d/steal?sensitive-redirect-sentinel' % thief.server_port

        class Endpoint(http.server.BaseHTTPRequestHandler):
            code = 302

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                self.send_response(type(self).code); self.send_header('Location', target); self.send_header('Content-Length', '0'); self.end_headers()

            def log_message(self, *args):
                pass
        endpoint = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        for server in (thief, endpoint):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.write('old-access')
        self.file.write_text(self.file.read_text().replace('REFRESHTOKEN=unused', 'REFRESHTOKEN=old-refresh'))
        before = self.file.read_bytes()
        url = 'http://127.0.0.1:%d/oauth2/token' % endpoint.server_port
        for code in (301, 302, 303, 307, 308):
            Endpoint.code = code
            with self.subTest(code=code), patch.object(credentials, 'TOKEN_ENDPOINT', url):
                with self.assertRaises(credentials.CredentialError) as caught:
                    credentials.refresh('old-access')
                self.assertEqual(caught.exception.kind, 'transient')
                self.assertNotIn('sensitive-redirect-sentinel', str(caught.exception))
                self.assertNotIn('old-refresh', str(caught.exception))
                self.assertEqual(before, self.file.read_bytes())
        self.assertEqual(stolen, [])

    def test_parent_0755_allowed_but_writable_parent_rejected(self):
        self.write('old-access');self.file.parent.chmod(0o755)
        mocked,_=self.opener()
        with mocked:self.assertEqual(credentials.refresh('old-access').access_token,'rotated-access')
        self.file.parent.chmod(0o777)
        with patch.object(credentials,'_renew') as renew:
            with self.assertRaises(credentials.CredentialError):credentials.refresh('rotated-access')
        renew.assert_not_called()

    def test_symlink_and_nonregular_locks_are_rejected_without_network(self):
        self.write('old-access');lock=self.file.parent/'.listening-room-refresh.lock'
        target=self.file.parent/'target';target.write_text('preserve');target.chmod(0o600)
        lock.symlink_to(target)
        with patch.object(credentials,'_renew') as renew:
            with self.assertRaises(credentials.CredentialError):credentials.refresh('old-access')
        renew.assert_not_called();self.assertEqual(target.read_text(),'preserve')
        lock.unlink();os.mkfifo(lock,0o600)
        with patch.object(credentials,'_renew') as renew:
            with self.assertRaises(credentials.CredentialError):credentials.refresh('old-access')
        renew.assert_not_called()

    def test_nonregular_login_and_oversize_login_are_rejected(self):
        os.mkfifo(self.file,0o600)
        with self.assertRaises(credentials.CredentialError):credentials.refresh('old-access')
        self.file.unlink();self.write('old-access')
        self.file.write_bytes(self.file.read_bytes()+b'x'*(credentials.MAX_LOGIN+1))
        with self.assertRaises(credentials.CredentialError):credentials.refresh('old-access')

    def test_external_cli_replacement_wins_over_our_new_pair(self):
        self.write('old-access')
        def outside(_values):
            replacement=self.file.with_name('outside-new.env')
            replacement.write_text('CLIENTID=other-client\nACCESSTOKEN=outside-access\nCLIENTSECRET=other-secret\nREFRESHTOKEN=outside-refresh\n# outside\n')
            replacement.chmod(0o600);os.replace(replacement,self.file)
            return 'our-access','our-refresh'
        with patch.object(credentials,'_renew',side_effect=outside):result=credentials.refresh('old-access')
        self.assertEqual((result.client_id,result.access_token),('other-client','outside-access'))
        self.assertIn('# outside',self.file.read_text());self.assertNotIn('our-access',self.file.read_text())

    def test_external_edit_or_incomplete_replacement_is_never_overwritten(self):
        for incomplete in (False,True):
            self.write('old-access')
            def outside(_values):
                text=self.file.read_text()+'# concurrent CLI edit\n'
                if incomplete:text=text.replace('ACCESSTOKEN=old-access\n','')
                self.file.write_text(text)
                return 'our-access','our-refresh'
            with patch.object(credentials,'_renew',side_effect=outside):
                with self.assertRaises(credentials.CredentialError):credentials.refresh('old-access')
            self.assertIn('# concurrent CLI edit',self.file.read_text())
            self.assertNotIn('our-access',self.file.read_text())

    def test_concurrent_callers_share_one_rotated_pair(self):
        self.write('old-access');entered=threading.Event();release=threading.Event();results=[];errors=[]
        def renew(_values):
            entered.set();release.wait(2)
            return 'one-new-access','one-new-refresh'
        def caller():
            try:results.append(credentials.refresh('old-access'))
            except Exception as error:errors.append(type(error).__name__)
        with patch.object(credentials,'_renew',side_effect=renew) as network:
            first=threading.Thread(target=caller);second=threading.Thread(target=caller)
            first.start()
            try:
                self.assertTrue(entered.wait(1));second.start();release.set()
                first.join(3);second.join(3)
            finally:release.set()
            self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
            self.assertEqual(errors,[]);self.assertEqual(len(results),2)
            self.assertEqual({value.access_token for value in results},{'one-new-access'})
            network.assert_called_once()

if __name__=='__main__':unittest.main()
