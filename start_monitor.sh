#!/usr/bin/env bash
# Start the IMAX monitor as a background process.
# Usage: ./start_monitor.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/monitor.pid"
LOG_FILE="$SCRIPT_DIR/imax_monitor.log"

if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Monitor is already running (PID $PID). Use stop_monitor.sh to stop it."
        exit 1
    else
        rm "$PID_FILE"
    fi
fi

nohup python3 "$SCRIPT_DIR/monitor.py" >/dev/null 2>&1 &
PID=$!
echo $PID > "$PID_FILE"
echo "Monitor started (PID $PID). Tailing log — Ctrl+C to stop tailing (monitor keeps running)."
echo "Log: $LOG_FILE"
sleep 1
tail -f "$LOG_FILE"
