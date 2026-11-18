# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
