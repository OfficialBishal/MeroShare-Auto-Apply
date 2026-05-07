#!/bin/bash
# MeroShare Auto-Apply - Double-click to run
# Everything is handled automatically.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/venv"
PYTHON="$VENV/bin/python3"

# ── First-time setup (auto, only once) ──────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo ""
    echo "  Setting up for first time... (this only happens once)"
    echo ""

    if ! command -v python3 &> /dev/null; then
        echo "  Python 3 is required."
        echo "  Download it from: https://www.python.org/downloads/"
        echo ""
        read -p "  Press Enter to exit..."
        exit 1
    fi

    python3 -m venv "$VENV"
    "$PYTHON" -m pip install -q --upgrade pip
    "$PYTHON" -m pip install -q -r requirements.txt
    echo "  Installing browser engine..."
    "$PYTHON" -m playwright install chromium
    echo ""
    echo "  Setup complete!"
    echo ""
fi

# ── No credentials yet? Run setup wizard then open GUI ──────────────
# accounts.json is the multi-account source of truth. .env is also accepted
# so a legacy single-account install. Which migrates on first load() -
# doesn't get re-prompted before migration runs.
if [ ! -f "$SCRIPT_DIR/accounts.json" ] && [ ! -f "$SCRIPT_DIR/.env" ]; then
    "$PYTHON" setup.py
fi

# ── Launch the app (detached background) ────────────────────────────
# Running detached means closing this terminal won't kill the GUI.
# Use the Power icon in the GUI's header to stop everything (or run
# `lsof -ti :5050 | xargs kill` from any terminal).
PORT=5050
LOGS_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOGS_DIR"

if lsof -ti :$PORT > /dev/null 2>&1; then
    echo "  Already running at http://localhost:$PORT"
    open "http://localhost:$PORT" 2>/dev/null || true
else
    echo "  Starting in background..."
    # Roll the previous app.log if it's larger than 5 MB so it doesn't
    # grow unbounded across reboots. Best-effort; failures are silent.
    if [ -f "$LOGS_DIR/app.log" ]; then
        SIZE=$(stat -f%z "$LOGS_DIR/app.log" 2>/dev/null || stat -c%s "$LOGS_DIR/app.log" 2>/dev/null || echo 0)
        if [ "$SIZE" -gt 5242880 ]; then
            mv -f "$LOGS_DIR/app.log" "$LOGS_DIR/app.log.1" 2>/dev/null || true
        fi
    fi
    # nohup + & detaches the python process from this shell so closing
    # the terminal (Cmd+W or red X) doesn't take the GUI down with it.
    # app.py opens the browser on its own once Flask binds.
    nohup "$PYTHON" app.py > "$LOGS_DIR/app.log" 2>&1 &
    disown 2>/dev/null || true
fi

echo ""
echo "  App is running at http://localhost:$PORT"
echo "  Use the power icon in the header to stop everything."
echo "  This terminal can be closed."
echo ""
