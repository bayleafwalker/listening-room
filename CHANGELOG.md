# Changelog

## Unreleased

- Added an optional station presenter with a bounded budget, captured-metadata receipts,
  quiet failure status and separate Mix controls. Prepared voice is now played from an
  ephemeral speech layer; late and unavailable slots fall straight through to music.

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [2.5.0] - 2026-11-22

### Added
- Choosing an ambience now has its own header: the Rooms | Listen switcher gives way to a
  back arrow ("Back to Mix") and "Choose ambience · room name" in the room's accent, until the
  mode ends (Back, any navigation away, or choosing a sound).
- Twitch Live channels can be filtered and paged. Chips for All, Music, ASMR, any category added
  with "＋ Category" (searched on Twitch, saved in the Twitch preferences) and Your channels;
  a sort menu (Most watched, Fewest watched, Your channels first); rows show the stream title,
  category, viewers, language and the stream preview; "Show more" reads the next Twitch page.
  The row menu adds "Add to your channels" and "Hide this channel". A signed-out status line says
  that your channels still play.
- Below 560 px the Rooms | Listen switcher is a bar at the bottom of the window.
- **Watch** a YouTube video with sound, from every YouTube result, shelf row and the
  YouTube link sheet. It opens in its own player window (not as audio plus picture),
  pauses the room's Music and Ambience the way Pause does, and Now shows
  "Watching · ‹title›" with Stop watching. When the video stops, a toast offers Resume;
  the room never resumes on its own. With no room, Watch just plays.
- **Show as scene** on YouTube rows: the video becomes the room's silent scene, only the
  picture changes, and the room is marked Changed with Save as my room.
- YouTube suggestions before you type, without an account: Your saved YouTube, "For
  ‹room›" and "Live now", cached on disk (shown at once, refreshed at most hourly, kept
  for a day) and fetched off the GTK thread. Browsing them starts nothing.
- The session controller has a `video` action (`start` with a public YouTube `url` and
  `name`, queued; `stop`, at once) and a `video` snapshot field (status, name, url,
  started_at, room_paused) while the Watch window is open.
- Each Mix row (Music, Ambience) has its own Pause/Play and Stop buttons; a row paused on
  its own says "paused" in the accent colour. `listening-roomctl music|ambience
  pause|play|toggle|stop|next` exposes the same layer actions.
- A `transport` controller action (`toggle`, `pause`, `play`) is the one room Play/Pause:
  MPRIS, the media keys and `listening-roomctl toggle|pause|play` use it.
- `inspect` reports the latest start as `entry` (id, name, state, error, the room kept).

### Changed
- The window opens faster: Rooms paints its cards at once and their posters arrive
  from a worker thread, so the GTK thread no longer decodes art at startup (about
  300 ms of it before, under 1 ms now). The package build writes every poster at card
  size (`NAME.card.jpg`, 640x360) and cards show that; a poster without one (your own
  rooms) is scaled on the worker the first time it is shown.
- Scene loops cost far less memory: GStreamer's libav video decoders run one thread
  instead of one per core, which roughly halves what the eight catalogue loops hold
  once each has been shown (about 930 MB to 420-490 MB in the software-rendered
  test harness). Loops still play only while on screen and are still never torn down.
- At most 12 loop files hold a pipeline for the life of the window (the catalogue's
  and a few of your own clips); a clip beyond that shows its still.
- GStreamer loads on a worker when the window opens, so the first room preview no
  longer freezes the window while GStreamer scans its plugins (1.6 s or more after a
  first start or an upgrade); a scene shown before that shows its still, then moves.
- A room preview shows its still at once and starts its loop after the double-click
  time, so a double-click that enters, or skimming across cards, never builds a scene
  pipeline for the preview it passes.
- The dev shell has the package's GStreamer plugins, so `scripts/listening-dev.sh test`
  and acceptance play scene loops like the installed window.
- Live channels no longer stop at 60 and no longer lose ASMR: each category keeps its own list
  and cursor instead of being merged and cut. The Listen shelf shows the categories taking turns.
- World radio search puts whole-word and tag matches ahead of matches inside longer words
  ("rain" no longer lists "Ukrainian" first), then orders by clicks and votes.
- Library rows say "Play" (on demand); "Tune in" stays for live radio and Twitch.
- The mini layout's expand button moved to the bottom row inside the main window, clear of the
  window controls. Now's toasts sit above the room name and now-playing line, and controls do
  not fade while a toast with an action is showing.
- Browse rooms and Listen fit a 360 px window: room cards go one per line, the station page is
  built stacked, station cards are wider, the Listen scope chips scroll sideways instead of
  ellipsizing, and Recently played lists each station once.
- YouTube rows say Play for videos and Tune in only for live streams. Their menu puts
  Show as scene first (it never interrupts sound), then Play as ambience, Save as station
  and Open elsewhere. The YouTube link sheet offers Play, Watch, Show as scene, Play as
  ambience and Open elsewhere, in the same order and words. While choosing an ambience,
  Play as music and Watch move into the row menu.
- MPRIS Play/Pause/PlayPause and the media keys pause and resume the whole room, as the
  window's Pause does, instead of the music layer alone; MPRIS Stop and the media Stop
  key leave the room. `PlaybackStatus` is the room's (Playing while any layer plays) and
  the metadata names the room and what plays (the ambience when only it plays).
- Room Play resumes exactly what room Pause paused; a layer paused, played or stopped on
  its own leaves the resume offer and stays as left. With no offer, Play resumes every
  paused layer instead of saying there is nothing to resume. A repeated Quiet adds to the
  offer instead of erasing it. While anything is audible the room shows Pause.
- No ⋯ menu holds a single item: Mix rows always offer Change… and Stop there (with Next
  track and Try again when they apply), and a Listen row with one extra action shows it
  as a button.
- Removed 17 scene tuning `color` keys the motion engine never reads (electrical effects
  in four rooms and the second water effect in after-hours). The rendered loops are
  byte-identical. Their colours are listed for a later scene review rather than honoured.
- In an acceptance expectation, `@@` matches only a measurement (digits, sign, decimal
  point, comma, brackets, spaces), and a line that combines `!` with `@@` is refused.
- `tests/netguard.py` also guards Python child processes that suites start (a
  `sitecustomize` on `PYTHONPATH`, kept in explicit `env=` too); `tests/test_listening_netguard.py`
  covers it. Non-Python children are not covered.
- `scripts/listening-dev.sh probe` and `acceptance` run their GUI children in a low-CPU-weight
  user scope at nice 10 (`LISTENING_DEV_PRIORITY=normal` turns it off).

### Fixed
- A station whose Familiarity was never set shows "A mix" (its neutral choice), not
  "Familiar", the first button.
- `tests/test_listening_station_eof.py` no longer fails now and then with
  ConnectionRefused: the decoder creates its control socket a moment before it listens,
  and the test now waits for the connection to be accepted, not for the file to exist.
- A refresh failure (backend down) and audio-policy changes are now pushed to
  subscribers; before, they only showed on the next unrelated change, and a window
  that stops polling while the stream is up never saw them.
- A transient snapshot failure no longer fails or silently closes a `subscribe`: it
  serves the latest good revision (a subscribe before any snapshot waits for the
  first; MPRIS ignores changes until then). Subscribers wait at most 10 s for the fresh snapshot instead of for as long
  as the controller lock is held.
- The client's protocol-version probe no longer times out after 1 s: `inspect` with
  `fields: []` returns just the version without a snapshot or the controller lock,
  so a loaded host or an oversize snapshot cannot make the controller look
  disconnected. Protocol-1 controllers are still detected and never sent `subscribe`.
- `status --follow` exits as soon as Waybar's pipe is closed, even when the status
  does not change, instead of holding a subscriber slot.
- Snapshot feed listeners run only on the feed thread, as documented, and may call
  `refresh()` without deadlocking.
- Scenes loop again: a scene stopped moving at the end of its clip (about 20 s after
  entering a room), because GTK's own looping stops at the end of the stream with
  this GTK and GStreamer. A playing loop now starts over itself, on the same pipeline.
- Entering a room from idle whose music fails but whose ambience or scene starts enters the
  room, with the failure and Try again on the Music layer.
- A start where nothing can start no longer returns silently to Rooms: Now keeps the card "Couldn’t
  start ‹room›" with Try again and Back, named after the room tried. A room that kept
  playing does not show that failure as its own music failing.
- Connecting no longer shows Next track or More/Less like this, a stale Mix summary, or
  room volume 0 while the stored volume is not 0.

## [2.4.0] - 2026-11-21

### Added
- The session controller pushes changes: a `subscribe` action streams one JSON line
  per snapshot change over the private socket (heartbeat every 15 s when idle), and
  `session_client.Subscription` / `subscribe()` give clients an iterator or callback
  that reconnects with backoff and never starts playback. `inspect` reports the
  protocol `version` (2). MPRIS updates from the same feed.
- `listening-roomctl status --follow` prints a Waybar line whenever the status
  changes, so the Waybar module runs one process with `exec` and no `interval`
  (the README snippet is updated; plain `status` still works for polling).

### Changed
- A new `subscribe` stream starts from the controller's current state, so its first
  snapshot includes every command acknowledged before it connected (under load it
  could start one revision behind).
- The controller's actions are declared once in a registry (handler, whether it runs
  at once or queues, argument types) that drives both dispatch and the socket
  service. Unknown actions and wrongly typed arguments now fail at once with a
  plain error instead of being queued.
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

### Internal
- One suite list: `tests/run_suites.py` runs every `tests/test_*.py` for both the package
  check and `scripts/listening-dev.sh test`; exceptions are named with reasons in
  `tests/suites-manual.txt`. The WP1 and WP3 oracle suites now run in the package check.
- `tests/netguard.py` blocks non-loopback network access in every suite `run_suites.py` starts;
  the session evidence probe now fakes the World radio search instead of querying the live directory.
- Acceptance probe expectations live in `tests/probes/expected/` and are judged by check
  name (`acceptance_verdict.py`) instead of pass counts in the dev script.
- `nix/package.nix` has one launcher helper; the dev shell reuses the package's Python.
- CONTRIBUTING covers the asset render cost and adding a test or probe; SECURITY lists
  what the app deliberately trusts. The classic-era `listening-hub-audio`/`-mix` probes
  are reviewed: `-audio` (live network streams) is archived under `tests/probes/archive-classic/`
  with its reasons; `-mix` stays as a manual real-systemd check. `listening-session-mpris.py`
  (real Gio MPRIS on an isolated bus; run with a built package path) is kept and its stale
  expectation fixed: MPRIS Volume is the room volume, default 1.0.

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
