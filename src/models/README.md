# Optional voice/instrumental estimates

The packaged models are authored by Pablo Alonso and the Music Technology Group
at Universitat Pompeu Fabra. Model weights use **CC BY-NC-SA 4.0**, independently
of the Listening Room application's source license. See the
[model catalogue](https://essentia.upf.edu/models.html) and
[license](https://creativecommons.org/licenses/by-nc-sa/4.0/).

- Feature extractor: `msd-musicnn-1` (model version 1).
  SHA-256 `49668ffec47e52e94b96f45930bb46a28a1368d4bdfb5c05378fa834aca616e1`.
- Classification head: `voice_instrumental-msd-musicnn-1` (metadata version 2).
  SHA-256 `dd054410fa4f9fcb0086b4223a9a5c66dd098c5d4cef485c03af0b0500443b73`.
- Output classes: instrumental, voice. Voice does not distinguish speech from
  singing, vocal samples, or wordless vocals.

Listening Room records estimates and model/method provenance. Scores are not
calibrated guarantees for a complete recording and never create a human review.
The published model evaluation does not establish accuracy for this library or
for this application's preprocessing and aggregation method.

MusicNN citation: Alonso-Jiménez et al., *TensorFlow Audio Models in Essentia*,
ICASSP 2020.
