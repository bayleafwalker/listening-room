"""Metadata CLI parsing keeps explicit human reviews distinct from observations."""
import sys
import unittest
import json
import tempfile
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import roomctl
import metadata_store


class MetadataCliTests(unittest.TestCase):
    def test_review_requires_explicit_whole_recording_acknowledgement(self):
        with self.assertRaisesRegex(ValueError, 'whole exact recording'):
            roomctl.metadata_arguments('metadata-review', '{"acknowledged": "true"}')
        action, args = roomctl.metadata_arguments('metadata-review', '{"origin":"library","item_id":"a","classification":"instrumental","reviewer":"listener","acknowledged":true}')
        self.assertEqual(action, 'metadata.review')
        self.assertIs(args['acknowledged'], True)

    def test_import_reads_export_for_controller_validation(self):
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as source:
            json.dump({'version': 1, 'records': [{'review': {'classification': 'instrumental'}}]}, source)
            source.flush()
            action, args = roomctl.metadata_arguments('metadata-import', source.name)
        self.assertEqual(action, 'metadata.import')
        self.assertEqual(len(args['records']), 1)
        self.assertEqual(roomctl.metadata_arguments('metadata-export'), ('metadata.export', {}))

    def test_round_trip_more_than_200_records_preserves_trust_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            source = metadata_store.MetadataStore(Path(directory) / 'source.sqlite')
            target = metadata_store.MetadataStore(Path(directory) / 'target.sqlite')
            for index in range(251):
                identity = f'track{index:04}'
                source.observe('library', identity, 'a' * 64, features=[{'provider':'test', 'name':'energy', 'value':0.4}])
            source.review('library', 'track0000', 'a' * 64, 'instrumental', 'Listener')
            target.observe('library', 'track0000', 'b' * 64)
            target.review('library', 'track0000', 'b' * 64, 'spoken', 'Local')
            calls = []
            def request(action, args):
                calls.append((action, len(args.get('records', []))))
                if action == 'metadata.export': return source.export_page(cursor=args.get('cursor'))
                if action == 'metadata.import': return {'imported': target.import_records(args['records'])}
                self.fail('Unexpected controller action')
            with mock.patch.object(roomctl.session_client, 'request', side_effect=request):
                document = roomctl.metadata_export()
                self.assertEqual(len(document['records']), 251)
                self.assertEqual(roomctl.metadata_import(document), {'imported': 250})
            self.assertGreater(sum(action == 'metadata.export' for action, _ in calls), 1)
            self.assertEqual([count for action, count in calls if action == 'metadata.import'], [200, 51])
            self.assertEqual(target.current('library', 'track0000')['review']['classification'], 'spoken')
            imported = target.current('library', 'track0001')
            self.assertIsNone(imported['audio_sha256'])
            self.assertIsNone(imported['review'])

    def test_import_batches_by_wire_size_and_rejects_cursor_loops(self):
        document = {'version': 1, 'records': [{'value': 'x' * 400000} for _ in range(3)]}
        sizes = []
        def request(action, args):
            sizes.append(len(json.dumps({'action': action, 'args': args}).encode()))
            return {'imported': len(args['records'])}
        with mock.patch.object(roomctl.session_client, 'request', side_effect=request):
            self.assertEqual(roomctl.metadata_import(document), {'imported': 3})
        self.assertEqual(len(sizes), 3)
        self.assertTrue(all(size < 1024 * 1024 for size in sizes))
        page = {'version': 1, 'records': [{'origin':'a','item_id':'b'}], 'next_cursor': {'origin':'a','item_id':'b'}}
        with mock.patch.object(roomctl.session_client, 'request', return_value=page):
            with self.assertRaisesRegex(ValueError, 'repeating'):
                roomctl.metadata_export()

    def test_enrich_and_export_use_controller_actions(self):
        action, args = roomctl.metadata_arguments('metadata-enrich', '{"reference":{"provider":"jellyfin","type":"Audio","id":"a"}}')
        self.assertEqual((action, args['reference']['id']), ('metadata.enrich', 'a'))
        self.assertEqual(roomctl.metadata_arguments('metadata-list'), ('metadata.list', {}))
        with self.assertRaises(ValueError):
            roomctl.metadata_arguments('metadata-enrich', '[]')

if __name__ == '__main__':
    unittest.main()
