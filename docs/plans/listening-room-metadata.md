# Listening Room recording metadata

The product goal is hands-off radio from a user's library, with understandable
reasons for selections. Identity, estimates and human reviews are separate kinds
of evidence. None of these operations starts playback or creates playlists.

## Collection and identity

Library stations automatically refresh a bounded set of missing or stale
observations from the connected Jellyfin account and existing AudioMuse index.
Provider IDs are reported identities. MusicBrainz text matches and AcoustID
fingerprint matches are candidates until the user confirms one. Confirmation
does not classify the audio as instrumental.

Public matching is explicit and rate limited. AcoustID requires an application
client key. Chromaprint fingerprints and recording details are sent only when
their corresponding matching action is selected. Credentials remain separate
from exported metadata and never appear in media player arguments.

## Estimates and provenance

AudioMuse's stored tempo, raw RMS energy and mood values are estimates. Existing
rows do not record their original analysis date or exact model version. Record
the retrieval time and retain those unknowns; do not substitute the retrieval
date for the analysis date or claim current weights generated historical rows.

Direct voice/instrumental analysis reads the complete original audio, records
its SHA-256 and runs checksum-pinned MusicNN and voice/instrumental ONNX models.
It covers the waveform tail and records its preprocessing, aggregation and
model identities. Its scores are uncalibrated estimates, not probabilities that
a whole recording has no vocals, and never generate human reviews. Model
weights and their separate CC BY-NC-SA 4.0 attribution ship with the app.

“Likely instrumental” is a soft preference. It can schedule bounded background
analysis and rank future selections from the resulting scores. Unknowns remain
unknown; strongly vocal estimates should rank below neutral unknowns. Existing
station preferences are preserved. Ordinary stations keep playing while
metadata jobs run.

## Reviewed instrumental playback

A human review requires the listener to attest to reviewing the entire exact
recording. Instrumental excludes speaking, singing and vocal samples. Store the
reviewer, date, classification, library scope and audio SHA-256. Public tags,
identity confirmation, imported records and model estimates cannot grant this
status.

Strict instrumental requires the trusted local review and current original
bytes to match. The player consumes the same verified anonymous snapshot,
avoiding a second server stream after the hash check. A changed/unknown recording,
review replacement or revocation cancels pending authorization or stops an
active strict track. Queue overrides, replay, repeats, reconnect and fallbacks
must preserve that rule.

## Operations and acceptance

Metadata jobs are bounded and cancellable independently of playback controls.
HTTP work runs in a killable process so the deadline covers DNS, TLS, response
headers and body reads. Each job captures a matching server/account/credential
context and aborts on reconfiguration. Original media is limited to 512 MiB;
classification additionally bounds decoded duration and execution time.

The native Metadata panel exposes observations, matching, analysis, review,
revocation and progress. Batch enrichment and CLI import/export support large
libraries without requiring manual playlist construction. Imports do not trust
reviews or audio hashes and do not overwrite existing local evidence.

Acceptance includes adversarial station and metadata tests, a real GTK probe
with fake providers and no audio, a bounded live provider/classifier test,
package and full flake checks and independent review.
Room videos must remain byte-identical to the separately approved 2.2 scenes.
