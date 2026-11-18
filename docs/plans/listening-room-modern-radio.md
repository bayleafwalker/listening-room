# Listening Room: personal radio

Source is authoritative for supported
behavior; tests do not establish that an online account, speaker, phone or light
is connected.

## Product intention

Listening Room is a personal radio for your surroundings. Choose a mood,
programme or familiar source; it keeps the sound and picture going with little
attention. Save listening intentions as stations, and complete surroundings as
rooms. Manual playlists remain optional ingredients.

A station selects continuous programming from approved sources. A room captures
the station or direct source, background audio, picture, levels and placement.
The internal session is shared by GTK, MPRIS, Waybar and remote commands; it is
not another navigation destination.

## Acceptance contract

- Main navigation is Listen, Room and Recent. Settings contains Connections;
  advanced sources and setup remain reachable. (The
  [experience redesign](listening-room-ux-redesign.md) plans to replace this
  navigation clause; the other clauses still apply.)
- Listen exposes native Twitch channel selection, direct radio, local sounds,
  library selections and saved stations. Native playback leads the catalogue;
  external browser/app destinations have a separate Open elsewhere section.
- Tune in is explicit, with a visible destination. Main audio and Background
  audio use common supported source adapters. Picture distinguishes audio-only,
  video with sound and silent visual routes. Unsupported combinations have an
  explanation. Browsing never starts or replaces playback.
- The current room and mix remain visible while browsing. Changing one layer
  preserves the other layers and their controls. Candidate failures preserve
  the previous working source.
- Saved stations retain references and preferences, never prepared streams or
  access tokens. Generated library stations prepare a short queue, refill ahead,
  avoid recent repeats where the pool permits, and expose upcoming tracks only
  on request. A one-off track can play next, then station programming resumes.
- Mood adjustments affect upcoming selections. Strict speech constraints exclude
  unverified content; editorial descriptions of live radio remain preferences.
  Selection reasons and bounded catalogue limits are visible.
- Reconnection is bounded. Fallbacks use only approved source pools; exhaustion
  produces quiet and an actionable layer error. End policies are explicit.
- Station history supports replay of actual captured references. Reopening the
  app offers an explicit return; it does not autoplay. Feedback applies to the
  selected station. Optional programme arcs make predictable changes.
- Provider setup uses supported provider-specific authentication with secure
  private storage and renewal. Capabilities distinguish browse, local playback,
  remote playback and external handoff. Login success alone is not playback proof.
- Jellyfin automatic selections work without an AI dependency. Existing AudioMuse
  playlists can supply stations. AudioMuse availability is separately testable;
  unavailable analysis cannot block already prepared playback.
- Music Assistant is optional. Its library/player routes and YouTube Music
  playlists are presented only with their actual capabilities and selected
  remote player. No desktop composed-mix transfer is implied.
- Earlier inside/desk modes, complete saved rooms, licensed offline atmosphere,
  Window open, fades, Quiet/return, picture placement/reduced motion, output
  routing/disconnect policy, MPRIS, Waybar, KDE Connect commands, lighting restore,
  link preview and listening history remain operational.

## Verification and delivery

Use synthetic accounts and isolated HOME/XDG state for automated checks. Exercise
real GTK and Gio on private display/session buses and force failure paths. Build
and check the Nix package, and run `nix flake check`. Independent review covers playback ownership,
credential isolation, direct-first discovery and the listening intention.

Recovery source: `listening-room-before-modern-radio-20261115` at
`fea874baff845bd533560ae930d318e62bbbce4d`. Earlier full local artifact recovery is
retained as documented in `docs/runbooks/listening-room-session.md`.
