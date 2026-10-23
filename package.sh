#!/bin/sh
set -e
cd "$(dirname "$0")"
OUT="$(cd .. && pwd)/deck-live.zip"
rm -rf /tmp/pkg && mkdir -p "/tmp/pkg/Deck Live/dist"
cp dist/index.js "/tmp/pkg/Deck Live/dist/"
cp main.py gst_worker.py gst_launch_worker.py plugin.json package.json README.md LICENSE "/tmp/pkg/Deck Live/"
rm -f "$OUT" && (cd /tmp/pkg && python3 -m zipfile -c "$OUT" "Deck Live")
echo "$OUT"
