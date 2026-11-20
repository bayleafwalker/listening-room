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

**Adding a test.** Create `tests/test_listening_<topic>.py`, a plain `unittest` file
with a `__main__` guard. That is all: `tests/run_suites.py` runs every `tests/test_*.py`,
and both the package build and `scripts/listening-dev.sh test` use it. A file that
must not run by default (it needs built assets or minutes of CPU) gets a line with its
reason in `tests/suites-manual.txt`; run it by name or with `test --all`. A test file not
named `test_listening_*.py` fails the run, so none can be forgotten. Suites use isolated
HOME and XDG state and never touch your desktop session.

**The asset render is expensive.** `nix build` renders every room's scene loop and sound
bed (`nix/assets.nix`), which takes many minutes. Only `src/artwork`, `src/scene_tuning`
and the renderer sources invalidate it, so UI, provider and metadata changes reuse the
reviewed render. Touch those inputs on purpose, and expect the dev loop to rebuild the
assets the first time after you do.

## GUI probes

`scripts/listening-dev.sh probe [OUTDIR]` runs the evidence probe on a private
Xvfb display and D-Bus session with portals off. The acceptance probes in
`tests/probes/` drive the real window the same way:

```sh
scripts/listening-dev.sh acceptance [OUTDIR]   # evidence probes, then the acceptance probes
```

**Adding a probe.** Add `tests/probes/listening-<name>.py` (and, for an acceptance probe,
a line in `tests/probes/acceptance.manifest`). An acceptance probe prints `PASS`/`FAIL`
lines and writes its `*acceptance*.json` results into `--output`.
`tests/probes/acceptance_verdict.py` judges a run by check names, not counts: any
failure that is not a known one fails, and so does a check that disappeared. New passing
checks need no edit. After a deliberate change, `scripts/listening-dev.sh acceptance OUTDIR --record`
rewrites `tests/probes/expected/<name>.checks`. A known failure is a line starting with
`!` with a comment line above it saying why. Today O4 keeps the superseded "5i [ORIGINAL]" check, and O3 keeps
its "hub test list registers the O2 and O3 suites" check, which looked for suite names in `nix/package.nix` and awaits a
replacement.

`tests/probes/listening-hub-mix.py` is a manual real-systemd/mpv check for a development
machine, not part of the loop. Retired probes live in `tests/probes/archive-classic/`.

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
