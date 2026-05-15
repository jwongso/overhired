#!/usr/bin/env bash
# Start the grapply companion server.
# Kills any stale process on port 7878 before starting.
set -e

PORT=7878
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If port is already in use, assume companion is running — leave it alone
if lsof -ti:"$PORT" > /dev/null 2>&1; then
    echo "Companion already running on port $PORT — nothing to do."
    exit 0
fi

echo "Starting companion on http://127.0.0.1:$PORT ..."
cd "$SCRIPT_DIR"
exec python -m uvicorn main:app --host 127.0.0.1 --port "$PORT" "$@"
