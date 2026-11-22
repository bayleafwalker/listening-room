import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import presenter


class PresenterTests(unittest.TestCase):
    STATION = {'presenter': {'enabled': True, 'budget': {'every_tracks': 4, 'max_seconds': 18}}}
    TRACK = {'name': 'Night Walk', 'artist': 'Example Quartet', 'album': 'Dusk'}

    def test_no_metadata_never_calls_a_model(self):
        value = presenter.Presenter()
        with patch.object(value, '_chat') as chat:
            self.assertIsNone(value.prepare(self.STATION, {}))
        self.assertFalse(chat.called)
        self.assertEqual(value.snapshot()['segments'][0]['reason'], 'no captured metadata')

    def test_primary_speech_suppresses_a_slot(self):
        value = presenter.Presenter()
        self.assertIsNone(value.prepare(self.STATION, self.TRACK, primary_speech=True))
        self.assertEqual(value.snapshot()['segments'][0]['reason'], 'primary speech source')

    def test_malformed_and_voice_failure_leave_a_receipt_not_a_gap(self):
        value = presenter.Presenter()
        with patch.object(value, '_chat', return_value=''), patch.object(value, '_speech') as voice:
            self.assertIsNone(value.prepare(self.STATION, self.TRACK))
            self.assertFalse(voice.called)
        self.assertEqual(value.snapshot()['segments'][0]['outcome'], 'dropped')
        with patch.object(value, '_chat', return_value='A short link.'), patch.object(value, '_speech', side_effect=ValueError('offline')):
            self.assertIsNone(value.prepare(self.STATION, self.TRACK))
        self.assertIn('offline', value.snapshot()['status'])

    def test_segment_has_metadata_receipts_and_does_not_repeat(self):
        value = presenter.Presenter()
        with patch.object(value, '_chat', return_value='A short link.'), patch.object(value, '_speech'):
            self.assertEqual(value.prepare(self.STATION, self.TRACK)['outcome'], 'played')
            self.assertIsNone(value.prepare(self.STATION, self.TRACK))
        segment = value.snapshot()['segments'][0]
        self.assertEqual(segment['reason'], 'repeated segment')
        self.assertEqual(segment['sources'][0]['source'], 'captured metadata')

    def test_expired_clip_is_dropped(self):
        value = presenter.Presenter(clock=lambda: 10)
        self.assertIsNone(value.prepare(self.STATION, self.TRACK, clip={'text': 'Old', 'expires_at': 9}))
        self.assertEqual(value.snapshot()['segments'][0]['reason'], 'clip expired')


if __name__ == '__main__':
    unittest.main()
