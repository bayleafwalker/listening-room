# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Internal
- One suite list: `tests/run_suites.py` runs every `tests/test_*.py` for both the package
  check and `scripts/listening-dev.sh test`; exceptions are named with reasons in
  `tests/suites-manual.txt`. The WP1 and WP3 oracle suites now run in the package check.
- Acceptance probe expectations live in `tests/probes/expected/` and are judged by check
  name (`acceptance_verdict.py`) instead of pass counts in the dev script.
- `nix/package.nix` has one launcher helper; the dev shell reuses the package's Python.
- CONTRIBUTING covers the asset render cost and adding a test or probe; SECURITY lists
  what the app deliberately trusts. The classic-era `listening-hub-audio`/`-mix` probes
  are reviewed: `-audio` (live network streams) is archived under `tests/probes/archive-classic/`
  with its reasons; `-mix` stays as a manual real-systemd check.

### Added
- Objective checks for the sounds (spectrum, event structure, distinctness, loop
  seam, level, determinism): `tests/test_listening_atmosphere_character.py`.
- Ambience in the Mix: Change… on the Ambience row opens Listen in an ambience
  mode (offline sounds, radio, live channels, YouTube, library). Each result's
  action is Play as ambience; choosing one replaces the layer and returns to the Mix.
- New station from scratch: a name, a pasted stream link or any Listen search
  result (radio, library, live channel, YouTube) as the start, and the mood
  choices. Stations can be deleted from their page, after a confirmation.
- YouTube in Listen: search through yt-dlp (no account or API key), with
  Tune in, Play as ambience and Open elsewhere. A truthful line in Listen and
  Preferences when yt-dlp is missing.
- Twitch: a "Set up Twitch to see live channels" entry that opens Preferences,
  and live channels (music and ASMR) listed once signed in.
- Starting something (entering a room, Tune in, Play as ambience, the mini player)
  opens Now at once with "Connecting to X…" and a spinner, disables the pressed
  control, and on failure shows a plain-words card with Try again and Back; the
  previous state stays intact.
- Mix: with no ambience the row reads "No ambience" with a Choose ambience
  button, and the level slider and Mute stay hidden until a source exists.
  Play as ambience shows a toast with Undo.
- `tests/probes/listening-o6-timing.py` times selection and audits GTK-thread work.
- A window smoke test in the package checks: the real window on a private Xvfb with
  fixtures shows Rooms, a preview, Listen, Now with the Mix, and Preferences. Each
  surface draws on its own; one that fails says so inline and the rest keeps working.
- A persistent "The room service isn't running" banner with Start again (which
  starts the idle service, never playback); meanwhile Now does not look live.
- Errors are plain sentences; the raw cause is behind Details (banners) or a Details
  button (toasts). Refusals and failures show at once, not behind another toast.
- Tune in with no room shows a named room with the last scene (or a default) and
  Choose a scene. Tune in over a room marks it Changed, with Save as my room.
- Browse rooms in the idle mini player and the narrow layout.
- Library appears after connecting Jellyfin, and World radio favourites on the
  Radio shelf, without a restart (the snapshot carries `config_generation`).

### Changed
- Sounds: the rain under distant thunder is now clearly audible between the
  rolls, and the six offline sounds are matched on A-weighted (perceived)
  loudness instead of raw RMS, so thunder is no longer far quieter than rain.
- The six offline sounds now sound like their names. Each is synthesized from the
  events that define it instead of a filtered noise bed: dense droplets and
  runnels for rain, a warm roar with crackles, pops and hiss for the fireplace,
  long irregular rolls over light rain for thunder, gusts that drive leaf rustle
  for forest wind, a swaying rumble with paired rail-joint clacks for the train,
  and unintelligible formant-shaped babble with cup clinks for the café. Still
  original procedural synthesis, CC0, with the same names and file names; levels
  are matched across the six. Descriptions were tightened to match.
- Scene loops look like what they show. The Midnight Library fire is a living
  flame behind the grate, with flicker, a few sparks and firelight on the
  hearth, and the grate no longer bends. Chimney smoke rises instead of
  swaying, clouds drift as one layer, and the Velvet Circuit candle and the
  Wizarding Valley lantern flicker as light.
- Scene polish. The Midnight Library fire now fills the firebox (taller, wider
  tongues, more heat) instead of being smaller than the painted flame, and the
  wall where the painted flame was removed keeps the stone's grain instead of a
  smooth smudge. Clouds in After Hours, Atomic Lounge and Wizarding Valley no
  longer tick every 10 seconds: the drift is now resampled without the step
  that appeared each time a copy passed zero shift. The unused fire `lick` key
  is gone. Scene tuning files are validated at load: an unknown or
  out-of-range effect parameter fails the asset build with a message naming the
  file, effect and parameter (`tests/test_listening_scene_tuning.py`). The four
  room videos change; the rigid-structure checks are unchanged.
- Each room names the structure that must not move: grates, frames, posts,
  rocks, figures and horizons. No effect can move it, and the build checks it.
  The room videos change as a result.
- One click on a room card previews it immediately (the double-click wait is gone);
  a quick second click, double-click or Enter still enters.
- Try again appears in a Mix row's menu only when that layer failed.
- Favourites, the World radio cache and Twitch channels are read off the GTK thread.

## [2.3.1] - 2026-11-18

First public release. The application had been developed privately; this is
its first standalone repository.

### Added
- The Listening Room app: Rooms, Listen and Now in one libadwaita window, with
  a mini player, a Waybar popover, MPRIS, and `listening-roomctl`.
- Eight rooms with reviewed art and muted scene loops, rendered at build time.
- Internet radio, Radio Browser discovery, YouTube Music links, Twitch channel
  layers, and optional Jellyfin, Music Assistant and ListenBrainz connections.
- Optional voice/instrumental estimates (MTG models, CC BY-NC-SA 4.0).
- Nix flake: `packages.default`, `overlays.default`, a dev shell, and
  `scripts/listening-dev.sh` for tests and GUI probes.

### Changed
- Source license is GPL-3.0-or-later; room art and scene inputs are CC0-1.0
  (AI-generated).
- The Jellyfin address is package configuration (`jellyfinUrl`); none is built in.
- The Twitch follows import needs a name; there is no default profile.
