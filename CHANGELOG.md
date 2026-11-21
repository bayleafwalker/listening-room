# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [2.3.2] - 2026-11-21

### Fixed
- Opening a room preview, switching rooms quickly, entering or leaving a room, or
  closing the window could crash the window (a segfault on GStreamer's `GstPlay`
  thread). Clearing a scene loop while GStreamer was still busy with it let that
  thread free its own player and then keep using it. Each scene loop is now
  created once, shared by the preview and Now, and only ever played or paused,
  never torn down, and loops alive at exit are left alone.
- Every room preview with a moving scene used to stay in memory with its video
  pipeline (threads, open files, a GL context, about 120 MB on a 16-core machine), so the window grew
  heavier with each preview. Closed previews are now freed, and the scene video
  costs at most one pipeline per room loop (about 120 MB each on 16 cores, mostly
  decoder threads), however often rooms are previewed.
  `tests/probes/listening-scene-media-stress.py` repeats previews, entering,
  leaving and closing with real scene loops; with `--flat` it checks that
  threads, open files and memory stay flat once every loop has been shown.


### Added
- The session controller pushes changes: a `subscribe` action streams one JSON line
  per snapshot change over the private socket (heartbeat every 15 s when idle), and
  `session_client.Subscription` / `subscribe()` give clients an iterator or callback
  that reconnects with backoff and never starts playback. `inspect` reports the
  protocol `version` (2). MPRIS updates from the same feed.
- `listening-roomctl status --follow` prints a Waybar line whenever the status
  changes, so the Waybar module runs one process with `exec` and no `interval`
  (the README snippet is updated; plain `status` still works for polling).
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
- The controller's actions are declared once in a registry (handler, whether it runs
  at once or queues, argument types) that drives both dispatch and the socket
  service. Unknown actions and wrongly typed arguments now fail at once with a
  plain error instead of being queued.
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
- The window does work in proportion to what changed: each surface draws only when
  something it shows changed, Rooms no longer builds a key every poll, and Listen
  rebuilds only the shelf whose contents changed. A poll that changes nothing draws
  nothing (`tests/probes/wp2w-rebuild-probe.py` counts it). Renaming or saving a room
  that is not playing leaves Now alone, and redrawing Now (a volume change) creates
  no widgets: the Mix's Change… buttons are relabelled only when their words change.
- The window and the mini player take snapshots from the controller's stream when it
  has one; polling is the fallback (every 5 s while a stream that worked is down;
  a controller without a stream is polled as before). Polls have a worker of their own,
  so a slow command never delays them, and all thread handoffs go through one helper.
- One stylesheet helper instead of four copies; the art cache keeps at most 64 images.
- Now says a room's name once: a room named after its station shows the track
  under the name, not the station again, and the Mix button, the mini players and
  Listen's now-playing bar say "Music" or the track instead of repeating it. Now's
  cards never cover the room's name and chip: in a short window they step aside
  while the card shows, and a failed start puts the room that is still playing back
  at once, with no "Connecting" chip left under "Couldn't start…".
- Leaving Listen by any route (a tab, Back, Now, a shortcut or the mini layout) ends
  ambience choosing, so Music › Change… and the Listen tab offer music again instead
  of "Choose an ambience".
- Undo of a default placement change in Preferences no longer counts as choosing a
  placement.
- The Waybar popover stays open when it loses focus (with focus-follows-mouse it closed
  as soon as the pointer left it). Esc, its close control and Open the room window close
  it, and a second Waybar click closes it too.

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
