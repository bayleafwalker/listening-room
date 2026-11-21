#!/usr/bin/env bash
# Listening Room development loop without rebuilding the package.
#
#   scripts/listening-dev.sh test [--all|SUITE...]  run the test suites: every tests/test_*.py not listed in
#                                             tests/suites-manual.txt, or those named (--all adds the manual ones)
#   scripts/listening-dev.sh probe [OUTDIR] [PROBE]  real GTK on private Xvfb + D-Bus; writes screenshots
#                                             (PROBE is a tests/probes name, default listening-session-ui)
#   scripts/listening-dev.sh acceptance [OUTDIR] [NAME...] [--record]  the probes in tests/probes/acceptance.manifest, judged
#                                             against tests/probes/expected/ (--record rewrites those lists)
#   scripts/listening-dev.sh shell            interactive shell with the same environment
#
# State is isolated by the suites and the probe themselves. The probe never
# touches the desktop session: Wayland is unset and GTK is forced onto Xvfb.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"

assets=$(nix build --no-link --print-out-paths .#listening-hub.assets)
src="$repo/src"
export LISTENING_HUB_SOURCE="$src/listening_hub.py"
export LISTENING_HUB_CATALOG="$src/catalogue.json"
export LISTENING_AMBIENCE_SOURCE="$src/ambience.py"
export LISTENING_HUB_ASSETS="$assets"
export PYTHONPATH="$src:$repo/tests"
export PYTHONDONTWRITEBYTECODE=1

in_shell() { nix develop "$repo" -c "$@"; }

case "${1:-}" in
  test)
    shift
    in_shell bash -c '
      set -e
      # Mirror the build sandbox: never read the live desktop session state.
      scratch=$(mktemp -d); trap "rm -rf \"$scratch\"" EXIT
      export HOME=$scratch XDG_STATE_HOME=$scratch/state XDG_CONFIG_HOME=$scratch/config XDG_CACHE_HOME=$scratch/cache
      unset XDG_RUNTIME_DIR WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS HYPRLAND_INSTANCE_SIGNATURE
      export LISTENING_TEST_MPV=$(command -v mpv)
      python3 tests/run_suites.py "$@"
      cd "$(dirname "$LISTENING_HUB_SOURCE")" && python3 validate_vistas.py "$LISTENING_HUB_ASSETS"
    ' _ "$@"
    ;;
  probe)
    out=${2:-"${TMPDIR:-/tmp}/listening-ui-review"}
    mkdir -p "$out"
    runtime=$(mktemp -d); trap 'rm -rf "$runtime"' EXIT; chmod 700 "$runtime"
    # No portals and a private runtime dir: D-Bus activation must not reach
    # the live compositor or mount the documents portal in the real session.
    in_shell env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u HYPRLAND_INSTANCE_SIGNATURE \
      XDG_RUNTIME_DIR="$runtime" GDK_BACKEND=x11 GDK_DEBUG=no-portals \
      xvfb-run -a -s '-screen 0 1440x1000x24' \
      dbus-run-session python3 "tests/probes/${3:-listening-session-ui}.py" --output "$out"
    echo "Screenshots: $out"
    ;;
  acceptance)
    # Each acceptance probe checks the screenshots its evidence probe wrote, so
    # the two run back to back into OUTDIR/<outcome>. Prints one summary line each; logs stay in OUTDIR/<outcome>.
    out=${2:-"${TMPDIR:-/tmp}/listening-acceptance"}
    runtime=$(mktemp -d); trap 'rm -rf "$runtime"' EXIT; chmod 700 "$runtime"
    gui() {
      in_shell env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u HYPRLAND_INSTANCE_SIGNATURE \
        XDG_RUNTIME_DIR="$runtime" GDK_BACKEND=x11 GDK_DEBUG=no-portals \
        xvfb-run -a -s '-screen 0 1440x1000x24' dbus-run-session python3 "$@"
    }
    record=; names=()
    for arg in "${@:3}"; do if [ "$arg" = --record ]; then record=--record; else names+=("$arg"); fi; done
    [ "${out#--}" = "$out" ] || { echo "OUTDIR first: acceptance OUTDIR [NAME...] [--record]" >&2; exit 2; }
    status=0
    while read -r name evidence acceptance; do
      case "$name" in ''|'#'*) continue ;; esac
      if ((${#names[@]})) && [[ " ${names[*]} " != *" $name "* ]]; then continue; fi
      mkdir -p "$out/$name"
      if [ "$evidence" != - ]; then gui "tests/probes/$evidence.py" --output "$out/$name" >"$out/$name/evidence.log" 2>&1 || true; fi
      echo "== $name"
      rm -f "$out/$name"/*acceptance*.json
      gui "tests/probes/$acceptance.py" --repo "$repo" --output "$out/$name" >"$out/$name/acceptance.log" 2>&1 || true
      # The verdict compares check names, not counts: failures, known failures and vanished checks.
      in_shell python3 tests/probes/acceptance_verdict.py $record "$name" "$out/$name" || status=1
    done < tests/probes/acceptance.manifest
    exit $status
    ;;
  shell)
    in_shell bash
    ;;
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
