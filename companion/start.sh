#!/usr/bin/env bash
# Start the overhired companion server.
# Kills any stale process on port 7878 before starting.
set -e

PORT=7878
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Kill stale process if port is busy
STALE=$(lsof -ti:"$PORT" 2>/dev/null || true)
if [ -n "$STALE" ]; then
    echo "Killing stale process on port $PORT (PID $STALE)..."
    kill "$STALE"
    sleep 1
fi

echo "Starting companion on http://127.0.0.1:$PORT ..."
cd "$SCRIPT_DIR"
exec python -m uvicorn main:app --host 127.0.0.1 --port "$PORT" "$@"
