# Contributing

Thanks for helping. Small, focused changes are easiest to review. Please read
the [Code of Conduct](CODE_OF_CONDUCT.md) first.

## Set up

You need Nix with flakes. Everything else comes from the dev shell:

```sh
nix develop            # or: scripts/listening-dev.sh shell
```

## Tests

```sh
scripts/listening-dev.sh test                       # every suite
scripts/listening-dev.sh test test_listening_stations.py   # one or more suites
nix build                                           # the package, its checks and the scene render
```

The suites in `tests/` are listed in `nix/package.nix`; add a new one there so
the build and the dev script run it. They use isolated HOME and XDG state and
never touch your desktop session.

## GUI probes

`scripts/listening-dev.sh probe [OUTDIR]` runs the evidence probe on a private
Xvfb display and D-Bus session with portals off. The acceptance probes in
`tests/probes/` drive the real window the same way:

```sh
scripts/listening-dev.sh acceptance [OUTDIR]   # evidence probes, then the five acceptance probes
```

`acceptance` expects 131, 161, 193, 170 and 242 passing checks for O1 to O5. O4 keeps
one recorded failure, the superseded "5i [ORIGINAL]" check, so it reports 170 passed, 1 failed.

Probes write screenshots to the output directory; the acceptance probes also
check that the screenshots they need exist there. Never point a probe at your
real session.

## Showcase images

`scripts/showcase.sh` regenerates `docs/screenshots/` and `docs/rooms/` from
fixtures. Use fixtures only: no real hosts, accounts or listening history.

## Ground rules

- Browsing, searching and previewing never start playback.
- No personal data in code, fixtures, docs or images. Use `example.org` and
  `*.example.test` addresses. `gitleaks detect --no-git` should stay clean.
- Do not weaken or skip a test to make a change pass; fix the change, or explain
  in the pull request why the oracle is wrong.
- Update `CHANGELOG.md` for user-visible changes.
