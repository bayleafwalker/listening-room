# Archived classic-era probes

`listening-hub-audio.py` is archived, not deleted.

It decoded three catalogue radio streams (`ylex`, `yle-klassinen`, `radio-dismuke`)
through the classic `listening_hub` module on the live network, with a user systemd unit
and mpv on a null audio output (and one public YouTube video with `CHECK_YOUTUBE=1`).

Retired because:

- it needs live third-party streams, so it fails when a station is down and proves
  nothing about this code; the development rules forbid real network calls in checks;
- it starts a user systemd unit on the operator's own manager and expects a built package
  path as its argument, which no loop here provides;
- what it protected in our code (start a radio, report decoded audio, no video track) is
  covered by the fake-mpv and fake-`systemd-run` suites (`test_listening_hub.py`,
  `test_listening_layers.py`, `test_listening_wp3_acceptance.py`);
- whether a catalogue stream is still alive is a catalogue health question, not a
  regression test.

Its sibling `tests/probes/listening-hub-mix.py` stays: it is the one check that drives
real systemd and mpv with local HTTP fixtures (see the runbook).
