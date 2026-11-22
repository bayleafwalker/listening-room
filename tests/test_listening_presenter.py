import sys
import io
import wave
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import presenter


class PresenterTests(unittest.TestCase):
    STATION = {'presenter': {'enabled': True, 'budget': {'every_tracks': 4, 'max_seconds': 18}}}
    TRACK = {'name': 'Night Walk', 'artist': 'Example Quartet', 'album': 'Dusk'}
    RECORD = {'origin': 'library', 'item_id': 'track-1', 'identities': [
        {'provider': 'jellyfin', 'name': 'title', 'value': 'Night Walk'},
        {'provider': 'jellyfin', 'name': 'artist', 'value': 'Example Quartet'}]}

    @staticmethod
    def wav(seconds=.1):
        output = io.BytesIO()
        with wave.open(output, 'wb') as value:
            value.setparams((1, 2, 8000, int(8000 * seconds), 'NONE', ''))
            value.writeframes(b'\0\0' * int(8000 * seconds))
        return output.getvalue()

    def test_no_metadata_never_calls_a_model(self):
        value = presenter.Presenter()
        with patch.object(value, '_chat') as chat:
            self.assertIsNone(value.prepare(self.STATION, {}))
        self.assertFalse(chat.called)
        self.assertEqual(value.snapshot()['segments'][-1]['reason'], 'no captured metadata')

    def test_primary_speech_suppresses_a_slot(self):
        value = presenter.Presenter()
        self.assertIsNone(value.prepare(self.STATION, self.TRACK, self.RECORD, primary_speech=True))
        self.assertEqual(value.snapshot()['segments'][-1]['reason'], 'primary speech source')

    def test_malformed_and_voice_failure_leave_a_receipt_not_a_gap(self):
        value = presenter.Presenter()
        with patch.object(value, '_chat', return_value=''), patch.object(value, '_speech') as voice:
            self.assertIsNone(value.prepare(self.STATION, self.TRACK, self.RECORD))
            self.assertFalse(voice.called)
        self.assertEqual(value.snapshot()['segments'][0]['outcome'], 'dropped')
        with patch.object(value, '_chat', return_value='A short link.'), patch.object(value, '_speech', side_effect=ValueError('offline')):
            self.assertIsNone(value.prepare(self.STATION, self.TRACK, self.RECORD))
        self.assertIn('offline', value.snapshot()['status'])

    def test_language_failure_is_a_quiet_drop(self):
        value = presenter.Presenter()
        with patch.object(value, '_chat', side_effect=ValueError('offline')):
            self.assertIsNone(value.prepare(self.STATION, self.TRACK, self.RECORD))
        snapshot = value.snapshot()
        self.assertEqual(snapshot['segments'][0]['reason'], 'language unavailable')
        self.assertIn('offline', snapshot['status'])

    def test_segment_has_metadata_receipts_and_does_not_repeat(self):
        value = presenter.Presenter()
        with patch.dict('os.environ', {'XDG_RUNTIME_DIR': '/tmp'}, clear=False), \
                patch.object(value, '_chat', return_value='A short link.'), patch.object(value, '_speech', return_value=self.wav()):
            self.assertEqual(value.prepare(self.STATION, self.TRACK, self.RECORD)['outcome'], 'played')
            self.assertIsNone(value.prepare(self.STATION, self.TRACK, self.RECORD))
        segment = value.snapshot()['segments'][-1]
        self.assertEqual(segment['reason'], 'repeated segment')
        self.assertEqual(segment['sources'][0]['source'], 'track-1')

    def test_wav_duration_and_request_format_are_public(self):
        value = presenter.Presenter()
        with patch.dict('os.environ', {'XDG_RUNTIME_DIR': '/tmp'}, clear=False), \
                patch.object(value, '_chat', return_value='A short link.'), \
                patch.object(value, '_speech', return_value=self.wav(.25)):
            segment = value.prepare(self.STATION, self.TRACK, self.RECORD)
        self.assertAlmostEqual(segment['duration'], .25)
        with patch.object(value, '_request', return_value=self.wav()) as request:
            value._speech('Words', 3)
        self.assertEqual(request.call_args.args[2]['response_format'], 'wav')

    def test_expired_clip_is_dropped(self):
        value = presenter.Presenter(clock=lambda: 10)
        self.assertIsNone(value.prepare(self.STATION, self.TRACK, clip={'text': 'Old', 'expires_at': 9}))
        self.assertEqual(value.snapshot()['segments'][-1]['reason'], 'clip expired')


if __name__ == '__main__':
    unittest.main()
