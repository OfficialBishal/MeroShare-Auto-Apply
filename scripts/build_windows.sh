#!/bin/bash
# Build a portable Windows .zip distribution.
#
# Output: dist/MeroShare-Auto-Apply-windows.zip (~15-20 MB)
#
# Runs on macOS or Linux. Produces a .zip the user extracts on
# Windows and double-clicks `Run MeroShare Auto-Apply.bat` (or the
# included shortcut). The .zip ships:
#
#   1. Python 3.12.7 embeddable distribution (~10 MB, from python.org)
#   2. The full MeroShare Auto-Apply source tree
#   3. A robust run.bat that does first-launch dependency install and
#      then transparently launches the app on subsequent runs.
#
# Why the .zip is small: we DON'T pre-bundle pip dependencies (that
# would require a Windows-host build, since pip wheels are
# platform-specific). Instead the bundled Python pulls deps from PyPI
# on first launch. Slow once (~2 minutes), instant after.
#
# Why we DON'T ship Playwright Chromium: same reason as the macOS
# build. Chromium is 150MB, gets stale fast, and Playwright handles
# the download cleanly on first launch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build/windows"
DIST_DIR="$PROJECT_ROOT/dist"
ZIP_NAME="MeroShare-Auto-Apply-windows.zip"
ZIP_PATH="$DIST_DIR/$ZIP_NAME"
STAGE_NAME="MeroShare-Auto-Apply"
STAGE_DIR="$BUILD_DIR/$STAGE_NAME"

CACHE_DIR="$HOME/.cache/meroshare-build"
mkdir -p "$CACHE_DIR" "$DIST_DIR"

# Python embeddable for Windows. Pin a specific version for
# reproducibility. amd64 covers the vast majority of Windows users;
# add an arm64 variant in a follow-up if needed.
PY_VERSION="3.12.7"
PY_EMBED_NAME="python-${PY_VERSION}-embed-amd64.zip"
PY_EMBED_URL="https://www.python.org/ftp/python/${PY_VERSION}/${PY_EMBED_NAME}"
PY_EMBED_CACHE="$CACHE_DIR/$PY_EMBED_NAME"

GET_PIP_URL="https://bootstrap.pypa.io/get-pip.py"
GET_PIP_CACHE="$CACHE_DIR/get-pip.py"

cd "$PROJECT_ROOT"

# Version resolution. Same logic as scripts/build_dmg.sh so a single
# tag push produces matched-version artifacts on both platforms.
if [ -n "${GITHUB_REF_NAME:-}" ]; then
    VERSION="${GITHUB_REF_NAME#v}"
elif TAG="$(git -C "$PROJECT_ROOT" describe --tags --exact-match 2>/dev/null)"; then
    VERSION="${TAG#v}"
else
    VERSION="$(date +%Y.%m.%d)+dev"
fi

# ── Clean ────────────────────────────────────────────────────────────
echo "→ Cleaning build/windows/"
rm -rf "$BUILD_DIR"
mkdir -p "$STAGE_DIR"

# ── Download Python embeddable + get-pip.py (cached) ─────────────────
if [ ! -f "$PY_EMBED_CACHE" ]; then
    echo "→ Downloading Python $PY_VERSION embeddable (Windows amd64)"
    curl -fL --progress-bar -o "$PY_EMBED_CACHE.tmp" "$PY_EMBED_URL"
    mv "$PY_EMBED_CACHE.tmp" "$PY_EMBED_CACHE"
else
    echo "→ Using cached Python embeddable: $PY_EMBED_CACHE"
fi
if [ ! -f "$GET_PIP_CACHE" ]; then
    echo "→ Downloading get-pip.py"
    curl -fL --progress-bar -o "$GET_PIP_CACHE.tmp" "$GET_PIP_URL"
    mv "$GET_PIP_CACHE.tmp" "$GET_PIP_CACHE"
fi

# ── Extract Python embeddable into stage ─────────────────────────────
PY_DIR="$STAGE_DIR/python"
mkdir -p "$PY_DIR"
echo "→ Extracting Python into $PY_DIR"
unzip -q "$PY_EMBED_CACHE" -d "$PY_DIR"

# Enable site-packages in the embeddable distribution. The shipped
# `python312._pth` has `import site` commented out, which prevents
# pip from finding installed packages. Uncommenting it (or adding
# the line) restores normal Python import semantics. Also add the
# Lib/site-packages path so packages installed via get-pip.py land
# somewhere Python looks.
PTH_FILE="$PY_DIR/python312._pth"
if [ -f "$PTH_FILE" ]; then
    # Add Lib/site-packages to the search path and uncomment site.
    {
        cat "$PTH_FILE"
        echo "Lib/site-packages"
    } > "$PTH_FILE.new"
    sed -i.bak 's/^#import site/import site/' "$PTH_FILE.new"
    mv "$PTH_FILE.new" "$PTH_FILE"
    rm -f "$PTH_FILE.bak"
    echo "  patched python312._pth (enabled site)"
else
    echo "  WARNING: $PTH_FILE not found. Embeddable layout changed?"
fi

# Stage get-pip.py inside the bundle so first-launch can bootstrap
# pip without internet for THAT step (still needs internet for the
# pip install of our requirements, but at least pip itself is local).
cp "$GET_PIP_CACHE" "$PY_DIR/get-pip.py"

# ── Stage source files ───────────────────────────────────────────────
echo "→ Staging source"
mkdir -p "$STAGE_DIR/app"
rsync -a \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='build' \
    --exclude='dist' \
    --exclude='*.dmg' \
    --exclude='logs' \
    --exclude='.applied_issues*' \
    --exclude='.capital_cache.json' \
    --exclude='accounts.json' \
    --exclude='config.json' \
    --exclude='.env' \
    --exclude='.DS_Store' \
    --exclude='node_modules' \
    --exclude='.playwright-mcp' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='docs' \
    "$PROJECT_ROOT/" "$STAGE_DIR/app/"

# Drop the macOS-specific scheduler script. Windows Task Scheduler
# is a different model. Also drop run.bat (we'll write a smarter one
# at the top level that points at the bundled Python).
rm -f "$STAGE_DIR/app/setup_schedule.sh" "$STAGE_DIR/app/run.bat"

# Stamp the version. Windows doesn't currently have a menu-bar-style
# auto-update prompt, but stamping it keeps the artifacts consistent
# and lets future tooling (e.g. a Windows updater) read the same
# value the macOS build uses.
echo "→ Stamping version: $VERSION"
cat > "$STAGE_DIR/app/_version.py" <<PY_EOF
"""Application version. Generated by scripts/build_windows.sh."""

__version__ = "$VERSION"
PY_EOF

# ── Write a Windows-aware run.bat at the top level ───────────────────
echo "→ Writing top-level run.bat"
# Write the .bat content to a temp file with LF line endings, then
# convert to CRLF (Windows cmd.exe can be picky about line endings).
# Doing it this way avoids the quoting hell of embedding a multi-line
# .bat. With start/echo/findstr/etc.. Inside a bash heredoc.
RUN_BAT_PATH="$STAGE_DIR/Run MeroShare Auto-Apply.bat"
RUN_BAT_TMP="$BUILD_DIR/run.bat.tmp"
cat > "$RUN_BAT_TMP" <<'BAT_EOF'
@echo off
REM MeroShare Auto-Apply -- Windows portable launcher.
REM Bundles Python; pulls Python deps and Chromium on first launch.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set PYTHON=%~dp0python\python.exe
set APP_DIR=%~dp0app
set LOGS_DIR=%APP_DIR%\logs
set MARKER=%APP_DIR%\.first-run-done
set PORT=5050

if not exist "%PYTHON%" (
    echo.
    echo   ERROR: bundled Python is missing.
    echo   Re-extract the .zip and try again.
    echo.
    pause
    exit /b 1
)

REM Already running on the same port? Just open the existing tab.
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   Already running at http://localhost:%PORT%
    start "" http://localhost:%PORT%
    exit /b 0
)

REM First-run setup: install pip, requirements, then Playwright Chromium.
if not exist "%MARKER%" (
    echo.
    echo   First-time setup -- 2 to 5 minutes.
    echo   This window will close automatically when done.
    echo.

    echo   [1/3] Installing pip into the bundled Python...
    "%PYTHON%" "%~dp0python\get-pip.py" --no-warn-script-location
    if errorlevel 1 (
        echo   pip bootstrap failed. Check your internet connection and retry.
        pause
        exit /b 1
    )

    echo   [2/3] Installing Python dependencies -- Flask, Playwright, etc.
    "%PYTHON%" -m pip install --no-warn-script-location -r "%APP_DIR%\requirements.txt"
    if errorlevel 1 (
        echo   pip install failed. Check your internet connection and retry.
        pause
        exit /b 1
    )

    echo   [3/3] Installing Chromium browser engine -- about 150 MB.
    "%PYTHON%" -m playwright install chromium
    if errorlevel 1 (
        echo   playwright install failed. Delete %MARKER% and retry.
        pause
        exit /b 1
    )

    echo. > "%MARKER%"
    echo.
    echo   Setup complete. Launching the app...
    echo.
)

if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%"

REM Launch the app detached so closing this window doesn't kill it.
REM `start /B` runs in the same console; we wrap in cmd /c so the
REM redirect attaches to python.exe and not the start command.
start "MeroShare Auto-Apply" /B cmd /c ""%PYTHON%" "%APP_DIR%\app.py" > "%LOGS_DIR%\app.log" 2>&1"

REM Give Flask a moment to bind, then open the browser.
timeout /t 2 /nobreak >nul
start "" http://localhost:%PORT%

echo.
echo   App is running at http://localhost:%PORT%
echo   This window can be closed.
echo.
BAT_EOF

# LF -> CRLF for Windows cmd.exe.
python3 -c "
import sys
src = open(sys.argv[1], 'rb').read().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
open(sys.argv[2], 'wb').write(src)
" "$RUN_BAT_TMP" "$RUN_BAT_PATH"
rm -f "$RUN_BAT_TMP"

# ── Top-level README so the recipient knows what to do ───────────────
cat > "$STAGE_DIR/README.txt" <<'TXT_EOF'
MeroShare Auto-Apply. Portable Windows Edition

How to run:
  1. Extract this .zip somewhere (your Documents folder works fine).
  2. Double-click "Run MeroShare Auto-Apply.bat".
  3. First run: a setup window opens for ~2-5 minutes while Python
     dependencies and the Chromium browser engine download.
  4. The app opens in your default browser at http://localhost:5050.

To use a custom port, set the MEROSHARE_PORT environment variable
before running the .bat (Settings -> System -> Environment Variables).

To uninstall: just delete the extracted folder.

Your accounts and config are stored inside the extracted folder
under app/. Back those up before deleting the folder if you want
to keep them.

Source: https://github.com/OfficialBishal/MeroShare-Auto-Apply
TXT_EOF

# ── Build the .zip ───────────────────────────────────────────────────
echo "→ Building $ZIP_NAME"
rm -f "$ZIP_PATH"
# Zip from inside BUILD_DIR so the archive root is the stage dir.
# -r recursive, -q quiet, -X don't include extra macOS attrs.
( cd "$BUILD_DIR" && zip -qrX "$ZIP_PATH" "$STAGE_NAME" )

ZIP_SIZE=$(du -h "$ZIP_PATH" | cut -f1)
echo ""
echo "Done."
echo "  → $ZIP_PATH ($ZIP_SIZE)"
echo ""
echo "Distribute by sharing the .zip. The recipient extracts and"
echo "double-clicks 'Run MeroShare Auto-Apply.bat'. First launch"
echo "needs internet for pip + Chromium (~2-5 minutes); subsequent"
echo "launches are instant."
