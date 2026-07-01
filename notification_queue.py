"""Cross-process notification queue.

Exists because macOS desktop notifications inherit the SENDER process's
bundle icon. The Flask process spawned by the menubar app is detached
from the .app bundle, so notifications fired via `osascript` from there
show a generic "Script Editor" icon instead of the MeroShare Auto-Apply
logo.

Architecture:
    Flask side  →  enqueue(title, message) writes a JSONL line
    Menubar side → drain() reads + truncates, returns a list

The menubar process owns the .app bundle, so when IT fires the
notification (via rumps.notification, which uses NSUserNotification),
the bundle icon appears correctly.

Falls back gracefully when the queue file is unwritable (read-only
filesystem, permissions): callers can fire osascript directly as a
last resort. That's the dev-mode (./run.sh) path where there's no
menubar to drain anyway.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("meroshare")

# Single-line JSONL: one record per line, simple to append-and-read,
# no schema versioning or seek-to-EOF gymnastics. The file is written
# 0o600 since notification messages can include account names.


def _queue_path() -> Path:
    # Late import so we don't pull accounts (and its keystore migration
    # side effect) into anyone who just wants to call enqueue().
    import accounts
    return accounts.STATE_DIR / ".notify-queue.jsonl"


def enqueue(title: str, message: str, *, max_age_s: int = 60) -> bool:
    """Append a notification request to the queue.

    `max_age_s` lets the menubar's drain step skip stale entries from
    a previous app session (e.g. a crash + restart shouldn't replay
    yesterday's "Share Applied!" toasts). Returns True on success.
    """
    path = _queue_path()
    record = {
        "title": str(title),
        "message": str(message),
        "ts": int(time.time()),
        "max_age_s": int(max_age_s),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Open with O_APPEND so concurrent writes from multiple
        # processes (Flask + scheduler) interleave atomically at the
        # line level without a separate lock.
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError as e:
        logger.debug("notification_queue.enqueue failed: %s", e)
        return False


def drain() -> list[dict]:
    """Read every queued notification, truncate the file, return them.

    Stale entries (older than each record's max_age_s) are silently
    dropped — the user shouldn't see "Share Applied!" for an
    application from yesterday because the menubar wasn't running
    at the time. The truncate happens regardless, so a stale-only
    queue gets cleared too.

    Caller (menubar) is expected to be the SOLE drainer; that's why
    we don't use locking. If two menubars race we'd lose some
    notifications, which is acceptable — the alternative (cross-
    process locking with timeouts) is more failure surface than
    duplicate menubars are worth handling.
    """
    path = _queue_path()
    if not path.exists():
        return []
    # Atomically claim the queue: rename it aside, THEN read. os.rename is
    # atomic on POSIX, so any concurrent enqueue (O_APPEND) after the rename
    # lands in a fresh queue file the next drain picks up — instead of being
    # silently lost in the read-then-truncate window.
    claimed = path.with_suffix(path.suffix + f".draining.{os.getpid()}")
    try:
        os.replace(path, claimed)
    except OSError as e:
        logger.debug("notification_queue.drain claim failed: %s", e)
        return []
    try:
        raw = claimed.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug("notification_queue.drain read failed: %s", e)
        raw = ""
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass
    now = int(time.time())
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        max_age = int(rec.get("max_age_s") or 60)
        ts = int(rec.get("ts") or 0)
        if ts and (now - ts) > max_age:
            continue  # stale, drop
        out.append({
            "title": str(rec.get("title", "")),
            "message": str(rec.get("message", "")),
        })
    return out


def menubar_alive() -> bool:
    """True if a menubar.py process is currently running on this host.

    Used by the Flask-side notify() helper to decide whether to enqueue
    (menubar will pick it up) vs. fall back to osascript directly.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "menubar.py"],
            check=False, capture_output=True, text=True, timeout=1,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False
