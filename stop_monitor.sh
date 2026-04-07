#!/usr/bin/env bash
# Stop the IMAX monitor background process.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/monitor.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "No PID file found. Is the monitor running?"
    exit 1
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm "$PID_FILE"
    echo "Monitor stopped (PID $PID)."
else
    echo "Process $PID not found — already stopped?"
    rm "$PID_FILE"
fi
