#!/bin/bash
# Start de MIDI Patchbay lokaal en open Chrome.
cd "$(dirname "$0")"
PORT=8765
python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 &
SERVER=$!
trap "kill $SERVER 2>/dev/null" EXIT
sleep 1
open -a "Google Chrome" "http://localhost:$PORT/" || open "http://localhost:$PORT/"
echo "MIDI Patchbay draait op http://localhost:$PORT/  (Ctrl+C of dit venster sluiten om te stoppen)"
wait $SERVER
