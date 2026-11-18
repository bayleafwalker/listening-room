import hashlib
import io
import sys
import threading
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import metadata_pipeline as pipeline
import online_services as online


ITEM = {'provider': 'jellyfin', 'type': 'Audio', 'id': 'abc'}
CONFIG = {'jellyfin': {'endpoint': 'https://music.example', 'user_id': 'user', 'token_file': '', 'username': ''}, 'audiomuse': {'endpoint': 'https://analysis.example', 'user_id': '', 'token_file': '', 'username': ''}}
TRACK = {'Id': 'abc', 'Type': 'Audio', 'ProviderIds': {'MusicBrainzTrack': '72b4228e-0b1c-4994-a763-9488f04de8a0', 'MusicBrainzAlbum': 'not-an-id'},
         'MediaSources': [{'Id': 'source', 'Size': 3, 'ETag': 'v1'}]}


class MetadataPipelineTests(TestCase):
    def setUp(self):
        patcher = mock.patch.dict('os.environ', {'JELLYFIN_TOKEN': 'secret'}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_collect_drops_forged_id_and_unbounded_values(self):
        score = {'item_id': 'abc', 'tempo': 122, 'energy': float('nan'), 'mood_vector': 'happy:0.8,sad:nan,evil:4'}
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(online, '_request', side_effect=[TRACK, score]):
            result = pipeline.collect_jellyfin(ITEM)
        self.assertEqual(len(result['identities']), 1)
        self.assertEqual([(f['name'], f['value']) for f in result['features']], [('tempo', 122), ('mood happy', .8)])
        self.assertNotIn('review', result)

    def test_reject_unmatched_provider_response(self):
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(online, '_request', side_effect=[TRACK, {'item_id': 'other', 'tempo': 120}]):
            self.assertEqual(pipeline.collect_jellyfin(ITEM)['features'], [])

    def test_hash_complete_bytes_and_reject_stale_revision(self):
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(b'abc')
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(online, '_token', return_value='secret'), mock.patch.object(pipeline.metadata_http, 'exchange', side_effect=lambda *args, **kwargs: ((k, v) for k, v in [(b'H', b'{"status":200,"length":"3"}'), (b'D', b'abc'), (b'E', b'')])), mock.patch.object(pipeline, '_track', side_effect=[TRACK, TRACK]):
            self.assertEqual(pipeline.fingerprint_jellyfin(ITEM), hashlib.sha256(b'abc').hexdigest())
        changed = dict(TRACK, MediaSources=[{'Id': 'source', 'Size': 3, 'ETag': 'v2'}])
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(online, '_token', return_value='secret'), mock.patch.object(pipeline.metadata_http, 'exchange', side_effect=lambda *args, **kwargs: ((k, v) for k, v in [(b'H', b'{"status":200,"length":"3"}'), (b'D', b'abc'), (b'E', b'')])), mock.patch.object(pipeline, '_track', side_effect=[TRACK, changed]):
            with self.assertRaises(online.ConnectionError):
                pipeline.fingerprint_jellyfin(ITEM)

    def test_cancellation_and_reject_unsafe_ids(self):
        event = threading.Event(); event.set()
        with self.assertRaises(online.ConnectionError):
            online._cancel(event)
        with mock.patch.object(online, 'config', return_value=CONFIG):
            with self.assertRaises(online.ConnectionError):
                pipeline._track(dict(ITEM, id='../../elsewhere'))

    def test_public_lookup_is_explicit_bounded_and_cancelled(self):
        with self.assertRaises(online.ConnectionError):
            pipeline.musicbrainz_lookup(['bad'])
        with self.assertRaises(online.ConnectionError):
            pipeline.musicbrainz_lookup(['72b4228e-0b1c-4994-a763-9488f04de8a0'] * 21)
        with self.assertRaises(online.ConnectionError):
            pipeline.acoustid_lookup('abc', 100, '')
        event = threading.Event(); event.set()
        with self.assertRaisesRegex(online.ConnectionError, 'cancelled'):
            pipeline._public_request('musicbrainz.org', '/ws/2/recording', {}, event)
        with self.assertRaises(online.ConnectionError):
            pipeline._public_request('localhost', '/anything', {})

    def test_no_unversioned_media_source(self):
        track = dict(TRACK, MediaSources=[{'Id': 'source', 'Size': 3}])
        with self.assertRaises(online.ConnectionError):
            pipeline._source(track)

    def test_search_returns_candidates_and_not_reviews(self):
        data = dict(TRACK, Name='Track', Artists=['Artist'])
        response = {'recordings': [{'id': '72b4228e-0b1c-4994-a763-9488f04de8a0', 'title': 'Track'}, {'id': 'forged'}]}
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(pipeline, '_track', return_value=data), mock.patch.object(pipeline, '_public_request', return_value=response) as request:
            rows = pipeline.musicbrainz_search(ITEM)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'candidate')
        self.assertNotIn('review', rows[0])
        self.assertIn('recording:', request.call_args.args[2]['query'])

    def test_classifier_rejects_untrusted_weights_before_network(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as model:
            model.write(b'bad weights'); model.flush()
            with mock.patch.dict('os.environ', {'LISTENING_VOICE_MODEL': model.name}), mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(pipeline, '_track', return_value=TRACK), mock.patch.object(online, '_request') as request:
                with self.assertRaisesRegex(online.ConnectionError, 'weights'):
                    pipeline.classify_stored_embedding(ITEM)
                request.assert_not_called()

    def test_direct_classification_is_bound_to_actual_audio(self):
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(b'abc')
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(online, '_token', return_value='secret'), mock.patch.object(pipeline.metadata_http, 'exchange', side_effect=lambda *args, **kwargs: ((k, v) for k, v in [(b'H', b'{"status":200,"length":"3"}'), (b'D', b'abc'), (b'E', b'')])), mock.patch.object(pipeline, '_track', side_effect=[TRACK, TRACK, TRACK]), mock.patch.object(pipeline, '_classify_file', return_value=[.1, .9]):
            result = pipeline._audio_snapshot(ITEM, classify=True)
        self.assertEqual(result['audio_sha256'], hashlib.sha256(b'abc').hexdigest())
        self.assertEqual(result['revision']['etag'], 'v1')
        self.assertEqual([x['value'] for x in result['features']], [.1, .9])
        self.assertTrue(all('confidence' not in x for x in result['features']))

    def test_classification_rejects_revision_change_during_inference(self):
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(b'abc')
        changed = dict(TRACK, MediaSources=[{'Id': 'source', 'Size': 3, 'ETag': 'v2'}])
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(online, '_token', return_value='secret'), mock.patch.object(pipeline.metadata_http, 'exchange', side_effect=lambda *args, **kwargs: ((k, v) for k, v in [(b'H', b'{"status":200,"length":"3"}'), (b'D', b'abc'), (b'E', b'')])), mock.patch.object(pipeline, '_track', side_effect=[TRACK, TRACK, changed]), mock.patch.object(pipeline, '_classify_file', return_value=[.1, .9]):
            with self.assertRaisesRegex(online.ConnectionError, 'changed'):
                pipeline._audio_snapshot(ITEM, classify=True)

    def test_config_change_with_colliding_ids_aborts_collection(self):
        current = dict(CONFIG)
        second = dict(CONFIG['jellyfin'], endpoint='https://other.example')
        def request(service, *args, **kwargs):
            if service == 'jellyfin':
                current['jellyfin'] = second
                return TRACK
            return {'item_id': 'abc', 'tempo': 100}
        with mock.patch.object(online, 'config', side_effect=lambda: current), mock.patch.object(online, '_request', side_effect=request):
            with self.assertRaisesRegex(online.ConnectionError, 'changed'):
                pipeline.collect_jellyfin(ITEM)

    def test_credential_is_paired_with_captured_server_settings(self):
        initial = CONFIG['jellyfin']
        changed = dict(initial, endpoint='https://other.example', token_file='/other/token')
        with mock.patch.object(online, 'config', side_effect=[CONFIG, dict(CONFIG, jellyfin=changed)]), mock.patch.object(online, '_token', return_value='secret') as token, mock.patch.object(online.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(online.ConnectionError, 'changed'):
                online._request('jellyfin', '/Items')
        self.assertEqual(token.call_args.kwargs['settings'], initial)
        opener.assert_not_called()

    def test_slow_stream_observes_cancel_between_reads(self):
        event = threading.Event()
        class SlowResponse:
            def read(self, size):
                raise AssertionError('read1 should be used')
            def read1(self, size):
                event.set()
                return b'x'
        with self.assertRaisesRegex(online.ConnectionError, 'cancelled'):
            online._read_bounded(SlowResponse(), 100, event)

    def test_verified_snapshot_keeps_exact_bytes_open(self):
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(b'abc')
        with mock.patch.object(online, 'config', return_value=CONFIG), mock.patch.object(pipeline.metadata_http, 'exchange', side_effect=lambda *args, **kwargs: ((k, v) for k, v in [(b'H', b'{"status":200,"length":"3"}'), (b'D', b'abc'), (b'E', b'')])), mock.patch.object(pipeline, '_track', side_effect=[TRACK, TRACK]):
            result = pipeline.acquire_verified_snapshot(ITEM)
        try:
            self.assertEqual(result['file'].read(), b'abc')
            self.assertEqual(result['audio_sha256'], hashlib.sha256(b'abc').hexdigest())
            self.assertEqual(result['origin'], pipeline.library_origin(CONFIG['jellyfin']))
        finally:
            result['file'].close()

    def test_classifier_child_thread_limits_do_not_modify_parent(self):
        import os
        import tempfile
        parent_value = os.environ.get('OMP_NUM_THREADS')
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (b'[0.1, 0.9]', b'')
        process.poll.return_value = 0
        with tempfile.TemporaryFile() as audio, mock.patch.object(pipeline.subprocess, 'Popen', return_value=process) as launch:
            self.assertEqual(pipeline._classify_file(audio), [.1, .9])
        child_env = launch.call_args.kwargs['env']
        self.assertEqual([child_env[k] for k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMBA_NUM_THREADS')], ['2', '2', '2'])
        self.assertEqual(os.environ.get('OMP_NUM_THREADS'), parent_value)

    def test_acoustid_key_private_persistence_and_env_override(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'hub'
            with mock.patch.object(online, '_config_dir', return_value=directory), mock.patch.dict(os.environ, {'ACOUSTID_CLIENT_KEY': ''}):
                self.assertEqual(pipeline.acoustid_client_key(), '')
                self.assertEqual(pipeline.configure_acoustid_client_key('application_key-123'), {'configured': True})
                path = directory / 'acoustid-client-key'
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(pipeline.acoustid_client_key(), 'application_key-123')
                with mock.patch.dict(os.environ, {'ACOUSTID_CLIENT_KEY': 'env-key'}):
                    self.assertEqual(pipeline.acoustid_client_key(), 'env-key')
                self.assertEqual(pipeline.configure_acoustid_client_key(''), {'configured': False})
                self.assertFalse(path.exists())

    def test_acoustid_key_rejects_insecure_or_symlinked_file(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'hub'; directory.mkdir(mode=0o700)
            key = directory / 'acoustid-client-key'
            key.write_text('test-key'); key.chmod(0o644)
            with mock.patch.object(online, '_config_dir', return_value=directory), mock.patch.dict(os.environ, {'ACOUSTID_CLIENT_KEY': ''}):
                with self.assertRaises(online.ConnectionError):
                    pipeline.acoustid_client_key()
                key.unlink()
                key.symlink_to(Path(root) / 'elsewhere')
                with self.assertRaises(online.ConnectionError):
                    pipeline.acoustid_client_key()
                with self.assertRaises(online.ConnectionError):
                    pipeline.configure_acoustid_client_key('invalid key')
