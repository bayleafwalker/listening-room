#!/usr/bin/env bash
# Regenerate the README showcase from fixtures only (no accounts, no network):
#
#   scripts/showcase.sh [rooms|preview|screenshots|all]
#
#   rooms         docs/rooms/*.jpg      the eight room plates, 1280 px wide
#   preview       docs/rooms/preview.webp  a short muted loop of one room
#   screenshots   docs/screenshots/*.png  the app on a private Xvfb + D-Bus
#                 (tests/probes/listening-session-ui.py and listening-listen-ui.py)
#
# Everything runs in the dev shell with throwaway HOME/XDG state and a private
# display; nothing touches the desktop session.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
what=${1:-all}

rooms() {
  mkdir -p docs/rooms
  nix develop "$repo" -c python3 - <<'PY'
from pathlib import Path
from PIL import Image
for source in sorted(Path('src/artwork').glob('*.[jp][pn]g')):
    image = Image.open(source).convert('RGB')
    image = image.resize((1280, round(image.height * 1280 / image.width)), Image.LANCZOS)
    image.save(Path('docs/rooms') / (source.stem + '.jpg'), quality=82, optimize=True, progressive=True)
PY
}

preview() {
  assets=$(nix build --no-link --print-out-paths .#listening-hub.assets)
  nix develop "$repo" -c ffmpeg -v error -y -ss 2 -t 6 -i "$assets/room-midnight-library.mp4" \
    -vf 'fps=10,scale=640:-1' -c:v libwebp -q:v 50 -loop 0 docs/rooms/preview.webp
}

screenshots() {
  raw=$(mktemp -d); trap 'rm -rf "$raw"' EXIT
  scripts/listening-dev.sh probe "$raw/session" listening-session-ui
  scripts/listening-dev.sh probe "$raw/listen" listening-listen-ui
  nix develop "$repo" -c python3 scripts/curate-screenshots.py "$raw" docs/screenshots
}

case "$what" in
  rooms) rooms ;;
  preview) preview ;;
  screenshots) screenshots ;;
  all) rooms; preview; screenshots ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
