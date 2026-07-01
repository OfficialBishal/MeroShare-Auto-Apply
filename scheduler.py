"""launchd-based background scheduler for MeroShare auto-apply.

This module is the single source of truth for the background scheduler. It is
deliberately Flask-free so it can be called from both the Flask GUI and the
`setup_schedule.sh` shell shim.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import accounts

PLIST_LABEL = "com.meroshare.autoapply"

# SOURCE_DIR is where this module's siblings (auto_apply.py) live -
# the launchd plist needs to point at that, not at the user's state
# dir. STATE_DIR is where launchd writes logs and where auto_apply.py
# reads/writes config + accounts + applied-issues.
SOURCE_DIR = Path(__file__).resolve().parent
STATE_DIR = accounts.STATE_DIR

PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{PLIST_LABEL}.plist"
LOG_FILE = STATE_DIR / "logs" / "meroshare.log"

# Any whole-hour interval from 1 to 24. Was previously a fixed list of
# (1, 3, 6, 12, 24); the GUI now offers a number input so users can pick
# any interval that suits them (e.g. 4h or 8h).
VALID_INTERVALS = tuple(range(1, 25))


class SchedulerError(RuntimeError):
    """Raised when launchctl or plist operations fail."""


def _resolve_plist_python() -> Path:
    """Pick the Python interpreter the launchd plist will invoke.

    Two install shapes are supported:
      1. Dev: ./venv/bin/python3 sits next to the source.
      2. Bundled .app: a relocatable Python lives at
         Contents/Resources/python/, parallel to the app source at
         Contents/Resources/app/.

    Raises SchedulerError if neither candidate is on disk. Pulled
    out of `_render_plist` so tests can mock the resolver — CI
    runners don't have a venv (they pip-install into the system
    Python) and we shouldn't need a real venv on disk to exercise
    plist rendering.
    """
    python_candidates = [
        SOURCE_DIR / "venv" / "bin" / "python3",
        SOURCE_DIR.parent / "python" / "bin" / "python3",
    ]
    python_path = next(
        (p for p in python_candidates if p.exists()), None,
    )
    if python_path is None:
        raise SchedulerError(
            f"could not find Python interpreter in any of: {python_candidates}"
        )
    return python_path


def _render_plist(interval_hours: int) -> str:
    """Render the launchd plist for the given interval.

    Accepts any whole-hour value in 1..24. Raises ValueError outside
    that range so we never write a misconfigured plist.
    """
    if interval_hours not in VALID_INTERVALS:
        raise ValueError(
            f"interval_hours must be 1..24, got {interval_hours}"
        )

    python_path = _resolve_plist_python()
    script_path = SOURCE_DIR / "auto_apply.py"
    # launchd's WorkingDirectory + log paths live in the state dir -
    # which is writable, unlike the bundle's Resources/app/. Without
    # this, the launchd-spawned auto_apply.py would crash trying to
    # open logs/meroshare.log on a read-only Resources/.
    log_dir = STATE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    interval_seconds = interval_hours * 3600

    # XML-escape every interpolated path. A data dir under a home folder
    # containing &, < or > (e.g. "Tom & Jerry") would otherwise produce a
    # malformed plist that launchctl refuses to load.
    py = _xml_escape(str(python_path))
    script = _xml_escape(str(script_path))
    state = _xml_escape(str(STATE_DIR))
    logs = _xml_escape(str(log_dir))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>{script}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{state}</string>

    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>

    <key>StandardOutPath</key>
    <string>{logs}/launchd_stdout.log</string>

    <key>StandardErrorPath</key>
    <string>{logs}/launchd_stderr.log</string>

    <key>RunAtLoad</key>
    <true/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <!-- Pass through MEROSHARE_DATA_DIR so the launchd-spawned
             auto_apply.py finds the same state dir as the GUI. Without
             this, the bundled-app scheduler would write accounts and
             logs back to the bundle's Resources/, breaking on read-only
             installs. -->
        <key>MEROSHARE_DATA_DIR</key>
        <string>{state}</string>
    </dict>
</dict>
</plist>
"""


# ── launchctl integration ──────────────────────────────────────────────


def _run_launchctl(*args: str) -> tuple[int, str, str]:
    """Run `launchctl` with the given args. Returns (returncode, stdout, stderr).

    No exception on non-zero exit. Callers decide what's tolerable.
    """
    import subprocess

    proc = subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _is_loaded() -> bool:
    """Check whether the launchd agent is currently loaded.

    Uses `launchctl list` and looks for the label as a whole word in the
    output. Avoids shell pipes for cleaner error handling.
    """
    rc, stdout, _ = _run_launchctl("list")
    if rc != 0:
        return False
    for line in stdout.splitlines():
        parts = line.split()
        if parts and parts[-1] == PLIST_LABEL:
            return True
    return False


def _write_plist(interval_hours: int) -> None:
    """Render and write the plist file. Creates ~/Library/LaunchAgents if needed."""
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(_render_plist(interval_hours))


def start(interval_hours: int) -> dict:
    """Write the plist and load it. Idempotent: if already loaded, reload.

    Short-circuits when the agent is already loaded with the same
    interval. Reloading would reset the next-run timer (annoying for
    the user) and add unnecessary launchctl churn.
    """
    if interval_hours not in VALID_INTERVALS:
        raise ValueError(
            f"interval_hours must be one of {VALID_INTERVALS}, got {interval_hours}"
        )

    already_loaded = _is_loaded()
    current = _read_loaded_interval() if already_loaded else None
    if already_loaded and current == interval_hours:
        # No-op: already running on the requested schedule.
        return status()

    if already_loaded:
        rc, _, err = _run_launchctl("unload", str(PLIST_PATH))
        if rc != 0:
            raise SchedulerError(f"launchctl unload failed: {err.strip()}")

    try:
        _write_plist(interval_hours)
    except OSError as e:
        raise SchedulerError(f"could not write plist: {e}") from e

    rc, _, err = _run_launchctl("load", str(PLIST_PATH))
    if rc != 0:
        raise SchedulerError(f"launchctl load failed: {err.strip()}")

    return status()


def stop() -> dict:
    """Unload the plist if loaded. Leaves the plist file in place."""
    if not _is_loaded():
        return status()

    rc, _, err = _run_launchctl("unload", str(PLIST_PATH))
    if rc != 0:
        raise SchedulerError(f"launchctl unload failed: {err.strip()}")

    return status()


# ── status reporting ──────────────────────────────────────────────────


# auto_apply.py emits one of these on each check, depending on era:
#   legacy single-account: "Checking for new issues at 2026-..."
#   multi-account:         "Checking <account name> at 2026-..."
# Both share the prefix "Checking " and the suffix " at <YYYY-MM-DD>",
# so we anchor on those rather than the literal middle text. The
# leading timestamp regex tolerates either a "," (logging default) or
# "." (alt millisecond separator) so a small format change in
# auto_apply.py's logging.basicConfig doesn't silently break the GUI.
_CHECK_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.,]?\d*\s.*Checking\b.*\bat\s\d{4}-\d{2}-\d{2}"
)
_APPLIED_RE = re.compile(r"Applied for: .+")
_NO_APPLY_RE = re.compile(r"No new applications made this run\.?")


def _parse_last_run(log_path: Path = LOG_FILE) -> tuple[str | None, str | None]:
    """Return (timestamp, summary) for the most recent check.

    Scans the current log's tail; if no check line is found (e.g. right after
    a RotatingFileHandler rollover the latest "Checking..." line sits in
    meroshare.log.1 while the fresh file has none yet), falls back to the .1
    backup so a healthy scheduler isn't briefly reported as "no runs yet".
    """
    ts, summary = _scan_log_for_check(log_path)
    if ts is not None:
        return ts, summary
    backup = log_path.with_name(log_path.name + ".1")
    if backup.exists():
        return _scan_log_for_check(backup)
    return None, None


def _scan_log_for_check(log_path: Path) -> tuple[str | None, str | None]:
    """Scan one log file's ~64KB tail for the most recent check line.

    Walks backwards to find the most recent `Checking ... at ...` line (either
    the single-account legacy form or the per-account multi-account form), then
    scans forward for an `Applied for: ...` / `No new applications` summary.
    Returns (None, None) if no check line is found.
    """
    if not log_path.exists():
        return None, None

    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            offset = max(0, size - 64 * 1024)
            f.seek(offset)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
    except OSError:
        return None, None

    lines = text.splitlines()
    tail = lines[-200:]

    check_idx = None
    timestamp = None
    for i in range(len(tail) - 1, -1, -1):
        m = _CHECK_RE.match(tail[i])
        if m:
            check_idx = i
            timestamp = m.group(1)
            break

    if check_idx is None:
        return None, None

    for line in tail[check_idx + 1:]:
        if _APPLIED_RE.search(line):
            return timestamp, _APPLIED_RE.search(line).group(0)
        if _NO_APPLY_RE.search(line):
            return timestamp, "No new applications made this run."

    return timestamp, None


def _read_loaded_interval() -> int | None:
    """Read the current StartInterval from the loaded plist file.

    Returns None if the file doesn't exist or can't be parsed.
    """
    if not PLIST_PATH.exists():
        return None
    try:
        text = PLIST_PATH.read_text()
    except OSError:
        return None
    m = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", text)
    if not m:
        return None
    seconds = int(m.group(1))
    hours = seconds // 3600
    return hours if hours in VALID_INTERVALS else None


def status() -> dict:
    """Return current scheduler state.

    Keys:
      enabled        bool       . Is the launchd agent currently loaded?
      interval_hours int | None . Interval the loaded agent is using
      last_run       str | None . ISO-ish timestamp of last check
      last_result    str | None . Short summary of last run
      next_run       str | None . ISO-ish timestamp of next expected run
    """
    from datetime import datetime, timedelta

    enabled = _is_loaded()
    interval_hours = _read_loaded_interval() if enabled else None
    last_run, last_result = _parse_last_run()

    def _localize(s):
        # Log timestamps are the machine's LOCAL clock, written naive. Attach the
        # local offset so consumers (menu bar _format_relative, the web GUI's
        # new Date()) don't misread them as UTC or Nepal time on a non-NPT host.
        if not s:
            return s
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").astimezone().isoformat(timespec="seconds")
        except ValueError:
            return s

    next_run: str | None = None
    if enabled and last_run and interval_hours:
        try:
            dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S")
            nxt = dt + timedelta(hours=interval_hours)
            # If the machine slept past a tick, last_run + interval can already
            # be in the past. Advance by whole intervals so we never present a
            # "next run" that has already elapsed (which rendered as "next Xh ago").
            now = datetime.now()
            if nxt <= now:
                elapsed = (now - dt).total_seconds()
                periods = int(elapsed // (interval_hours * 3600)) + 1
                nxt = dt + timedelta(hours=interval_hours * periods)
            next_run = _localize(nxt.strftime("%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass

    last_run = _localize(last_run)

    return {
        "enabled": enabled,
        "interval_hours": interval_hours,
        "last_run": last_run,
        "last_result": last_result,
        "next_run": next_run,
    }


# ── CLI entry point ────────────────────────────────────────────────────


def _cli(argv: list[str]) -> int:
    if len(argv) < 1:
        print("Usage: python -m scheduler {start <hours>|stop|status}", file=sys.stderr)
        return 2

    cmd = argv[0]
    try:
        if cmd == "start":
            if len(argv) < 2:
                print("start requires <hours>", file=sys.stderr)
                return 2
            result = start(int(argv[1]))
        elif cmd == "stop":
            result = stop()
        elif cmd == "status":
            result = status()
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
    except (SchedulerError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    import json
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
