#!/bin/sh
set -e
cd "$(dirname "$0")"
# Default next to the repo (writable in CI and local clones). Override with DECK_LIVE_ZIP_OUT.
OUT="${DECK_LIVE_ZIP_OUT:-$(pwd)/deck-live.zip}"
rm -rf /tmp/pkg && mkdir -p "/tmp/pkg/Deck Live/dist" "/tmp/pkg/Deck Live/assets"
cp dist/index.js "/tmp/pkg/Deck Live/dist/"
cp main.py gst_worker.py gst_launch_worker.py gamehelp.py plugin.json package.json README.md LICENSE STORE.md "/tmp/pkg/Deck Live/"
cp assets/icon.png "/tmp/pkg/Deck Live/assets/"
rm -f "$OUT" && (cd /tmp/pkg && python3 -m zipfile -c "$OUT" "Deck Live")
echo "$OUT"
