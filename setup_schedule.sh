#!/bin/bash
# Setup script for MeroShare auto-checker on macOS.
# Loads a launchd agent that runs the checker every N hours (default 6).
# Same operation as the GUI's "Background Scheduler" toggle. Both
# routes go through scheduler.py.
#
# Usage:
#   ./setup_schedule.sh             # every 6 hours (default)
#   ./setup_schedule.sh 3           # every 3 hours
#   ./setup_schedule.sh 12          # every 12 hours

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo "Error: Virtual environment not found at $SCRIPT_DIR/venv"
    echo "Run ./run.sh once to bootstrap it."
    exit 1
fi

INTERVAL="${1:-6}"

# Validate before invoking Python. Clearer error than scheduler.py's
# "must be 1..24" stack trace, and avoids spawning a subshell on bad
# input.
if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || (( INTERVAL < 1 || INTERVAL > 24 )); then
    echo "Error: interval must be an integer 1..24 (got '$INTERVAL')"
    exit 1
fi

cd "$SCRIPT_DIR"
"$PYTHON" -m scheduler start "$INTERVAL"

echo ""
echo "Scheduler started (every $INTERVAL hour(s))."
echo "Manage it from the app's Settings page, or:"
echo "  Status:  $PYTHON -m scheduler status"
echo "  Stop:    $PYTHON -m scheduler stop"
echo "  Logs:    tail -f $SCRIPT_DIR/logs/meroshare.log"
