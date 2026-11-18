"""Trust and recording binding checks without network or player processes."""
import sys
import json
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import metadata_store
import station_engine

A = 'a' * 64
B = 'b' * 64


def track(item):
    return {'provider': 'jellyfin', 'type': 'Audio', 'id': item, 'speech_verified': True}


def station():
    return {'name': 'Strict', 'sources': [{'reference': track('1')}],
            'preferences': {'speech': 'Strict instrumental'}, 'end_policy': 'repeat'}


class MetadataTrust(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = metadata_store.MetadataStore(Path(self.temp.name) / 'records.db')

    def test_hash_and_library_bind_reviews_and_unknown_invalidates(self):
        self.store.observe('server-a', '1', A, features=[{'provider': 'test', 'name': 'speech_verified', 'value': 'true'}])
        self.assertIsNone(self.store.inspect('server-a', '1', A)['review'])
        self.store.review('server-a', '1', A, 'instrumental', 'Alice')
        self.assertIsNone(self.store.inspect('server-b', '1', A))
        self.assertIsNone(self.store.inspect('server-a', '1', B))
        self.store.observe('server-a', '1', None)
        self.assertIsNone(self.store.inspect('server-a', '1', None)['review'])
        self.store.observe('server-a', '1', A)
        self.assertIsNone(self.store.inspect('server-a', '1', A)['review'])
        with self.assertRaises(ValueError):
            self.store.review('server-a', '1', B, 'instrumental', 'Alice')

    def test_untrusted_flags_never_authorize_and_revocation_filters_all_paths(self):
        self.store.observe('server-a', '1', A)
        resolver = lambda item: self.store.current('server-a', item['id'])
        program = station_engine.Program(station(), [track('1')], trusted_resolver=resolver)
        self.assertIsNone(program.next())
        with self.assertRaises(ValueError):
            program.play_next(track('1'))
        self.store.review('server-a', '1', A, 'instrumental', 'Alice')
        program.retune()
        self.assertEqual(program.next()['id'], '1')
        program.play_next(track('1'))
        program.replay = [track('1')]
        self.store.revoke('server-a', '1', A)
        self.assertFalse(program.can_next())
        self.assertIsNone(program.next())
        with self.assertRaises(ValueError):
            program.play_next(track('1'))

    def test_bound_instrumental_estimates_rank_softly_and_invalidate(self):
        definition = station()
        definition['preferences'] = {'speech': 'Likely instrumental'}
        def feature(value):
            return {'provider': 'Essentia MTG', 'name': 'instrumental score',
                    'method': 'whole original audio mean patch estimate', 'value': value}
        self.store.observe('server-a', '1', A, features=[feature(.95)], revision={'etag': 'v1'})
        self.store.observe('server-a', '2', B, features=[feature(.05)], revision={'etag': 'v1'})
        self.store.observe('server-a', '3', None, features=[feature(1)])
        resolver = lambda item: self.store.current('server-a', item['id'])
        program = station_engine.Program(definition, [track('1'), track('2'), track('3')], seed=1, trusted_resolver=resolver)
        self.assertEqual([program.next()['id'] for _ in range(3)], ['1', '3', '2'])
        self.assertIn('uncalibrated', ' '.join(program.explain(track('1'))))
        self.store.observe('server-a', '1', None, revision={'etag': 'v1'})
        self.assertIsNotNone(station_engine.instrumental_score(self.store.current('server-a', '1')))
        self.store.observe('server-a', '1', None, revision={'etag': 'v2'})
        self.assertIsNone(station_engine.instrumental_score(self.store.current('server-a', '1')))

    def test_provider_output_survives_lightweight_refresh_and_guides_ranking(self):
        import metadata_pipeline as pipeline
        import online_services as online
        from test_listening_metadata_pipeline import CONFIG, TRACK, ITEM
        packets = lambda *a, **k: ((k, v) for k, v in [(b'H', b'{"status":200,"length":"3"}'), (b'D', b'abc'), (b'E', b'')])
        with mock.patch.object(online, 'config', return_value=CONFIG), \
             mock.patch.object(online, '_token', return_value='fixture-token'), \
             mock.patch.object(pipeline.metadata_http, 'exchange', side_effect=packets), \
             mock.patch.object(pipeline, '_track', side_effect=[TRACK, TRACK, TRACK]), \
             mock.patch.object(pipeline.os.path, 'isfile', return_value=True), \
             mock.patch.object(pipeline.importlib.util, 'find_spec', return_value=True), \
             mock.patch.object(pipeline, '_classify_file', return_value=[.93, .07]):
            actual = pipeline.classify_jellyfin(ITEM)
        self.store.observe(actual['origin'], ITEM['id'], actual['audio_sha256'], features=actual['features'], revision=actual['revision'])
        self.assertAlmostEqual(station_engine.instrumental_score(self.store.current(actual['origin'], ITEM['id'])), .93)
        self.store.observe(actual['origin'], ITEM['id'], None, revision=actual['revision'])
        self.assertAlmostEqual(station_engine.instrumental_score(self.store.current(actual['origin'], ITEM['id'])), .93)
        self.assertEqual(self.store.current(actual['origin'], ITEM['id'])['features'][0]['method'], actual['features'][0]['method'])

    def test_import_cannot_forge_review_or_overwrite_local(self):
        self.store.observe('server-a', '1', A)
        self.store.review('server-a', '1', A, 'instrumental', 'Alice')
        data = [{'origin': 'server-a', 'item_id': '1', 'audio_sha256': B,
                 'review': {'classification': 'instrumental', 'reviewer': 'Mallory'}, 'identities': [], 'features': []},
                {'origin': 'server-b', 'item_id': '1', 'audio_sha256': A,
                 'review': {'classification': 'instrumental'},
                 'identities': [{'provider': 'MusicBrainz', 'provider_id': 'example', 'status': 'confirmed'}], 'features': []}]
        self.assertEqual(self.store.import_records(data), 1)
        self.assertEqual(self.store.inspect('server-a', '1', A)['review']['reviewer'], 'Alice')
        imported = self.store.current('server-b', '1')
        self.assertIsNone(imported['audio_sha256'])
        self.assertIsNone(imported['review'])
        self.assertEqual(imported['identities'][0]['status'], 'candidate')

    def test_paged_export_roundtrips_more_than_200_records(self):
        for number in range(255):
            self.store.observe('server-a', f'{number:04}', A,
                               features=[{'provider': 'provider', 'name': 'energy', 'value': number / 255}])
        other = metadata_store.MetadataStore(Path(self.temp.name) / 'other.db')
        cursor = None
        pages, exported = 0, []
        while True:
            page = self.store.export_page(cursor=cursor, limit=37)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=True).encode('ascii')), metadata_store.MAX_PAGE_BYTES)
            pages += 1
            exported.extend(page['records'])
            other.import_records(page['records'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertGreater(pages, 1)
        self.assertEqual(len(exported), 255)
        self.assertEqual(len({(x['origin'], x['item_id']) for x in exported}), 255)
        self.assertEqual(len(other.list_records(limit=500)), 255)
        self.assertIsNone(other.current('server-a', '0001')['audio_sha256'])

    def test_page_byte_budget_and_legacy_oversized_record_fails_safely(self):
        long_text = 'ä' * 190
        observations = [{'provider': 'test', 'name': 'energy', 'model': long_text, 'value': index} for index in range(60)]
        for number in range(15):
            self.store.observe('server-a', f'{number:04}', A, features=observations)
        first = self.store.export_page(limit=100)
        self.assertIsNotNone(first['next_cursor'])
        self.assertLessEqual(len(json.dumps(first, ensure_ascii=True).encode('ascii')), metadata_store.MAX_PAGE_BYTES)
        with contextlib.closing(sqlite3.connect(self.store.path)) as db:
            db.execute('UPDATE recordings SET features=? WHERE item_id=?', (json.dumps([{'provider': 'x', 'name': 'huge', 'value': 'x' * 600000}]), '0000'))
            db.commit()
        with self.assertRaisesRegex(ValueError, 'size limit'):
            self.store.export_page()
        with self.assertRaises(ValueError):
            self.store.export_page(cursor={'origin': 'bad/url', 'item_id': 'x'})

    def test_sensitive_or_unbounded_observations_rejected(self):
        for provider in ('https://example.test/token', '/private/file', 'x' * 201):
            with self.assertRaises(ValueError):
                self.store.observe('server-a', '1', A, identities=[{'provider': provider}])
        with self.assertRaises(ValueError):
            self.store.observe('server-a', '1', A, features=[{'provider': 'x', 'name': 'energy'}] * 101)

if __name__ == '__main__':
    unittest.main()
