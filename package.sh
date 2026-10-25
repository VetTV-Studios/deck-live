#!/bin/sh
set -e
cd "$(dirname "$0")"
OUT="$(cd .. && pwd)/deck-live.zip"
rm -rf /tmp/pkg && mkdir -p "/tmp/pkg/Deck Live/dist"
cp dist/index.js "/tmp/pkg/Deck Live/dist/"
<<<<<<< HEAD
cp main.py gst_worker.py gst_launch_worker.py plugin.json package.json README.md LICENSE "/tmp/pkg/Deck Live/"
=======
cp main.py gst_worker.py gamehelp.py plugin.json package.json README.md LICENSE "/tmp/pkg/Deck Live/"
>>>>>>> origin/feature/game-help
rm -f "$OUT" && (cd /tmp/pkg && python3 -m zipfile -c "$OUT" "Deck Live")
echo "$OUT"
