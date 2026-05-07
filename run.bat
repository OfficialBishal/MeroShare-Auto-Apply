@echo off
REM MeroShare Auto-Apply - Double-click to run
REM Everything is handled automatically.

cd /d "%~dp0"

set VENV=%~dp0venv
set PYTHON=%VENV%\Scripts\python.exe

REM First-time setup
if not exist "%PYTHON%" (
    echo.
    echo   Setting up for first time... [this only happens once]
    echo.

    where python >nul 2>nul
    if errorlevel 1 (
        echo   Python 3 is required.
        echo   Download it from: https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )

    python -m venv "%VENV%"
    "%PYTHON%" -m pip install -q --upgrade pip
    "%PYTHON%" -m pip install -q -r requirements.txt
    echo   Installing browser engine...
    "%PYTHON%" -m playwright install chromium
    echo.
    echo   Setup complete!
    echo.
)

REM No credentials yet? Run setup wizard.
REM accounts.json is the multi-account source of truth; .env is also
REM accepted so a legacy install (which migrates on first load) doesn't
REM get re-prompted before migration runs.
if not exist "%~dp0accounts.json" if not exist "%~dp0.env" (
    "%PYTHON%" setup.py
)

REM Launch the app (detached background) so closing this window doesn't
REM kill the GUI. Use the power icon in the GUI to stop everything.
if not exist "%~dp0logs" mkdir "%~dp0logs"
netstat -ano | findstr ":5050 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo   Already running at http://localhost:5050
    start "" http://localhost:5050
) else (
    echo   Starting in background...
    REM Wrap in cmd /c so the > redirect attaches to the spawned
    REM process, not to `start` itself. Without the wrapper, Python's
    REM stdout/stderr were lost on Windows and the app.log file stayed
    REM empty even when the app crashed at startup.
    start "" /B cmd /c ""%PYTHON%" app.py > "%~dp0logs\app.log" 2>&1"
)
echo.
echo   App is running at http://localhost:5050
echo   Use the power icon in the header to stop everything.
echo   This window can be closed.
echo.
