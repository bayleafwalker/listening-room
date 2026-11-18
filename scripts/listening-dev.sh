#!/usr/bin/env bash
# Listening Room development loop without rebuilding the package.
#
#   scripts/listening-dev.sh test [SUITE...]  run the package's test suites (all by default)
#   scripts/listening-dev.sh probe [OUTDIR] [PROBE]  real GTK on private Xvfb + D-Bus; writes screenshots
#                                             (PROBE is a tests/probes name, default listening-session-ui)
#   scripts/listening-dev.sh acceptance [OUTDIR]  evidence probes, then the O1-O5 acceptance probes
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

suites() {
  # The package checkPhase is the source of truth for which suites run.
  grep -o 'tests/test_listening_[a-z0-9_]*\.py' nix/package.nix | sort -u
}

case "${1:-}" in
  test)
    shift
    mapfile -t selected < <(if (($#)); then printf 'tests/%s\n' "$@"; else suites; fi)
    in_shell bash -c '
      set -e
      # Mirror the build sandbox: never read the live desktop session state.
      scratch=$(mktemp -d); trap "rm -rf \"$scratch\"" EXIT
      export HOME=$scratch XDG_STATE_HOME=$scratch/state XDG_CONFIG_HOME=$scratch/config XDG_CACHE_HOME=$scratch/cache
      unset XDG_RUNTIME_DIR WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS
      export LISTENING_TEST_MPV=$(command -v mpv)
      for suite in "$@"; do echo "== $suite"; python3 "$suite"; done
      cd "$(dirname "$LISTENING_HUB_SOURCE")" && python3 validate_vistas.py "$LISTENING_HUB_ASSETS"
    ' _ "${selected[@]}"
    ;;
  probe)
    out=${2:-"${TMPDIR:-/tmp}/listening-ui-review"}
    mkdir -p "$out"
    runtime=$(mktemp -d); trap 'rm -rf "$runtime"' EXIT; chmod 700 "$runtime"
    # No portals and a private runtime dir: D-Bus activation must not reach
    # the live compositor or mount the documents portal in the real session.
    in_shell env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS \
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
      in_shell env -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS \
        XDG_RUNTIME_DIR="$runtime" GDK_BACKEND=x11 GDK_DEBUG=no-portals \
        xvfb-run -a -s '-screen 0 1440x1000x24' dbus-run-session python3 "$@"
    }
    # O4 keeps one recorded failure: the superseded "5i [ORIGINAL]" check.
    declare -A expected=([next]='130 passed, 0 failed' [o2]='161 passed, 0 failed' [o3]='193 passed, 0 failed'
                         [o4]='170 passed, 1 failed' [o5]='242 passed, 0 failed')
    status=0
    for entry in next:listening-next-ui:listening-next-acceptance o2:listening-next-rooms:listening-o2-acceptance \
                 o3:listening-listen-ui:listening-o3-acceptance o4::listening-o4-acceptance \
                 o5:listening-session-ui:listening-o5-acceptance; do
      IFS=: read -r name evidence acceptance <<<"$entry"
      mkdir -p "$out/$name"
      if [ -n "$evidence" ]; then gui "tests/probes/$evidence.py" --output "$out/$name" >"$out/$name/evidence.log" 2>&1 || true; fi
      echo "== $name"
      gui "tests/probes/$acceptance.py" --repo "$repo" --output "$out/$name" >"$out/$name/acceptance.log" 2>&1 || true
      got=$(grep -E '^[0-9]+ passed' "$out/$name/acceptance.log" | tail -n1 || true)
      echo "$got (expected ${expected[$name]})"
      [ "$got" = "${expected[$name]}" ] || status=1
    done
    exit $status
    ;;
  shell)
    in_shell bash
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
