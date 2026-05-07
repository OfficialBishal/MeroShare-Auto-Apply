#!/usr/bin/env python3
"""
MeroShare Auto-Apply Tool
Checks for new IPO/Right Share/FPO issues and auto-applies based on config.

Usage:
    python auto_apply.py                    # One-shot: check and apply now
    python auto_apply.py --daemon           # Run continuously on schedule
    python auto_apply.py --list             # List current open issues
    python auto_apply.py --status           # Show application status
    python auto_apply.py --apply <id>       # Manually apply for a specific issue
    python auto_apply.py --dry-run          # Check but don't apply

On macOS, prefer the GUI's Background Scheduler (Settings tab) or
`scheduler.py` over `--daemon`. The launchd-based scheduler survives
reboots and runs without keeping a terminal open.
"""

import argparse
import json
import logging
import logging.handlers
import os
import platform
import threading
import subprocess
import sys
import time
from datetime import datetime

import requests as _requests

import accounts
from meroshare_client import MeroShareClient
from browser_apply import apply_via_browser, apply_with_retry

# ── Paths ───────────────────────────────────────────────────────────

# State (logs, config, applied-issues) lives in accounts.STATE_DIR,
# which the bundled .app redirects to ~/Library via MEROSHARE_DATA_DIR.
LOG_DIR = accounts.STATE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True, parents=True)
CONFIG_FILE = accounts.STATE_DIR / "config.json"

# ── Logging ─────────────────────────────────────────────────────────

# Only configure logging when this file is invoked as a script (CLI or
# launchd). When app.py imports `check_and_apply`, app.py's basicConfig
# has already configured the root logger and we should not interfere.
if __name__ == "__main__" or not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            # Rotating handler keeps logs bounded for long-running
            # launchd-driven invocations.
            logging.handlers.RotatingFileHandler(
                LOG_DIR / "meroshare.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=5,
            ),
        ],
    )
logger = logging.getLogger("meroshare")

# ── State Tracking ──────────────────────────────────────────────────


def load_applied():
    """Re-export from accounts module so tests/CLI can patch in one place."""
    return accounts.load_applied()


def save_applied(state):
    accounts.save_applied(state)


# MeroShare's API serves Nepal-local timestamps without any timezone
# annotation. A daemon running on a US/EU server interpreted those
# naive strings as its OWN local timezone, which shifted dates by up
# to a full day around midnight Nepal time. Default to Asia/Kathmandu
# explicitly; the MEROSHARE_TZ env var lets advanced users override.
def _meroshare_tz():
    from datetime import timezone, timedelta
    name = os.environ.get("MEROSHARE_TZ", "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            logger.warning(
                "MEROSHARE_TZ=%r is not a valid IANA zone; falling back to Asia/Kathmandu",
                name,
            )
    # Asia/Kathmandu is UTC+05:45 year-round (no DST). Use a fixed
    # offset rather than ZoneInfo so we don't need tzdata installed
    # on minimal Linux containers.
    return timezone(timedelta(hours=5, minutes=45), name="Asia/Kathmandu")


def _normalize_applied_date(raw) -> str:
    """Normalize MeroShare's appliedDate into our ISO format.

    Handles the formats we've seen in the wild ("2026-05-04 02:30:00",
    "2026/05/04 02:30:00", and ISO already), falling back to the
    current time when nothing parses. The GUI's `new Date(...)` is
    strict about format, so we want the on-disk cache to be uniform.

    Naive datetimes are interpreted as Asia/Kathmandu (where MeroShare
    runs). NOT the daemon machine's local timezone. Without this,
    a daemon on a non-NPT host would store timestamps shifted by up
    to 12 hours from the user's actual application time.
    """
    npt = _meroshare_tz()
    iso_now = datetime.now(npt).isoformat(timespec="seconds")
    if not raw:
        return iso_now
    s = str(raw).strip()
    if not s:
        return iso_now
    # Try ISO first (with the strict 'T' separator). Python 3.11+'s
    # fromisoformat relaxes this and accepts a space too, but we want
    # downstream consumers (the GUI's `new Date()`) to see a uniform
    # 'T'-separated form, so we always reformat through `isoformat`.
    try:
        dt = datetime.fromisoformat(s)
        # Ensure tz so JS doesn't interpret naive as UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=npt)
        return dt.isoformat(timespec="seconds")
    except ValueError:
        pass
    # MeroShare common formats. Naive, interpreted as Nepal time.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=npt)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return iso_now


def _safe_save_applied(state, *, context: str = "") -> bool:
    """Persist applied state, logging (not raising) on failure.

    Returns True on success. Used inside the per-issue apply loop where
    a disk-full / permission error must NOT raise out of the worker
    thread. That would skip recording previously-successful submits in
    the same cycle and cause double-applies on the next run. Logging
    loudly is the best-effort fallback.
    """
    try:
        save_applied(state)
        return True
    except OSError as e:
        logger.error(
            "Could not persist applied-issues state%s: %s. "
            "This run's confirmed submissions may be re-attempted next cycle.",
            f" ({context})" if context else "", e,
        )
        return False


def _safe_update_applied(mutator, *, context: str = "") -> bool:
    """Lock-protected read-modify-write of `.applied_issues.json`.

    Equivalent to `_safe_save_applied` but uses `accounts.update_applied`,
    which holds the cross-process file lock for the WHOLE load → mutate
    → write cycle. This is what we want around per-submit / batch-seed
    state writes inside `check_and_apply`: the previous "load once at
    the top of the run, save snapshot per-submit" pattern let a parallel
    GUI apply land between our load and save and get silently clobbered.
    """
    try:
        accounts.update_applied(mutator)
        return True
    except OSError as e:
        logger.error(
            "Could not persist applied-issues state%s: %s. "
            "This run's confirmed submissions may be re-attempted next cycle.",
            f" ({context})" if context else "", e,
        )
        return False


# ── Config ──────────────────────────────────────────────────────────


class ConfigError(RuntimeError):
    """Raised when config.json is missing or unparseable.

    A library function (imported by app.py) must not call sys.exit -
    that would kill the Flask server, not just the request. The CLI
    entrypoint catches this and exits with status 1.
    """


def load_config():
    if not CONFIG_FILE.exists():
        raise ConfigError(
            f"Config file not found: {CONFIG_FILE}. "
            "Run 'python setup.py' to create one, or copy "
            "config.example.json to config.json"
        )
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {CONFIG_FILE}: {e}") from e


# ── Desktop Notification ────────────────────────────────────────────


# Recent-notifications cache: dedups identical (title, message) pairs
# fired within a short window so the user isn't spammed with the same
# "Application Failed" toast on back-to-back retries. 90s covers a
# typical multi-account cycle; longer than that and the user probably
# does want to know it's still happening.
#
# Thread-safe under the Flask threaded server: app.py's do_apply and
# do_check workers can both call notify() concurrently. The lock
# protects both the read-then-write check and the GC pass that walks
# the dict.
_NOTIFY_DEDUP_WINDOW_S = 90
_recent_notifications: dict = {}  # (title, message) -> ts
_notify_lock = threading.Lock()


def notify(title, message):
    """Send a desktop notification (macOS/Linux).

    Respects the config flag notifications.desktop. Defaulting to True
    when config is missing so existing installs keep their pop-ups.
    Dedups identical (title, message) pairs within
    _NOTIFY_DEDUP_WINDOW_S seconds.
    """
    logger.info("NOTIFICATION: %s - %s", title, message)
    # Late-load config so flipping the toggle in the GUI takes effect
    # without restarting the daemon. Treat any falsy desktop value
    # (False, 0, "", null) as an opt-out so a hand-edited config.json
    # with a non-bool there still respects user intent.
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f) or {}
            notif = cfg.get("notifications") or {}
            if not notif.get("desktop", True):
                return  # user opted out of desktop pop-ups
        except (OSError, json.JSONDecodeError):
            pass
    # Dedup: same (title, message) within window → don't re-fire.
    # All access to _recent_notifications goes through the lock so
    # the GC pass can't race with concurrent notify() calls and
    # raise "dictionary changed size during iteration".
    now = time.time()
    key = (title, message)
    with _notify_lock:
        last = _recent_notifications.get(key)
        if last is not None and (now - last) < _NOTIFY_DEDUP_WINDOW_S:
            return
        _recent_notifications[key] = now
        # GC stale entries when the table is large enough to matter.
        # 50 fresh entries is plenty for the dedup window. Beyond
        # that we're holding stale records.
        if len(_recent_notifications) > 50:
            cutoff = now - _NOTIFY_DEDUP_WINDOW_S
            stale = [k for k, v in list(_recent_notifications.items()) if v < cutoff]
            for k in stale:
                _recent_notifications.pop(k, None)
    if platform.system() == "Darwin":
        # The bundled .app's menubar process polls a notification queue
        # file and fires rumps.notification — that path produces
        # toasts with the proper MeroShare Auto-Apply icon, because
        # NSUserNotification inherits the calling process's bundle.
        # Falling back to osascript directly produces toasts with a
        # generic "Script Editor" icon (no .app context for this Flask
        # subprocess).
        #
        # If the menubar isn't alive (dev mode `./run.sh`), enqueue
        # would just leave records to rot — fall straight to osascript
        # so the user still sees something.
        import notification_queue
        if notification_queue.menubar_alive() and notification_queue.enqueue(title, message):
            return
        # Dev-mode fallback. AppleScript string literals are delimited
        # by double quotes; backslashes, double quotes, newlines and
        # carriage returns all need escaping (an unescaped \n breaks
        # osascript with a syntax error). The message and title come
        # from API data (company names) which we don't trust.
        def _esc(s):
            return (
                s.replace("\\", "\\\\")
                 .replace('"', '\\"')
                 .replace("\r", " ")
                 .replace("\n", " ")
            )
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{_esc(message)}" with title "{_esc(title)}"'],
                check=False, capture_output=True,
            )
        except Exception as e:
            logger.debug("Desktop notification failed: %s", e)
    elif platform.system() == "Linux":
        try:
            subprocess.run(["notify-send", title, message], check=False, capture_output=True)
        except Exception as e:
            logger.debug("Desktop notification failed: %s", e)


# ── Core Logic ──────────────────────────────────────────────────────


def check_and_apply(config: dict, dry_run=False, results: dict | None = None):
    """Log in per account, check for matching issues, and apply via browser.

    `results`, if given, is mutated in place with per-account outcomes in
    the same shape /api/apply uses:
        {<account_id>: {"accountName", "success", "message", "applied": [...]}}
    The GUI's run-check uses this so per-account login/apply failures
    surface as toasts instead of disappearing into the log.
    """
    # Defensive .get(): a hand-edited config.json missing one of these
    # keys falls through to "nothing enabled", which the per-issue
    # loop surfaces as "Skipped (type ... disabled)". Clearer than
    # a KeyError stack trace.
    share_prefs = config.get("share_types") or {}
    auto_config = config.get("auto_apply") or {}
    applied_state = load_applied()
    all_accounts = accounts.load()

    if not all_accounts:
        logger.error("No accounts configured. Add one in Settings.")
        return []

    applied_list = []

    def _record(account, *, success, message, applied=None):
        if results is None:
            return
        results[account["id"]] = {
            "accountName": account["name"],
            "success": success,
            "message": message,
            "applied": list(applied or []),
        }

    for account in all_accounts:
        account_id = account["id"]
        account_name = account["name"]
        logger.info("=" * 60)
        logger.info("Checking %s at %s", account_name,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        client = MeroShareClient(credentials=account)
        if not client.login():
            logger.error("Login failed for %s. Skipping.", account_name)
            client.session.close()
            _record(account, success=False, message="Login failed")
            continue

        # Always-bound defaults: the per-issue loop below reads these
        # unconditionally, so a ConnectionError or JSONDecodeError on
        # the issue/report fetch must not leave them undefined.
        issues: list = []
        submitted_reports: list = []
        ineligible_issue_ids: set = set()
        try:
            try:
                issues = client.get_applicable_issues()
            except _requests.HTTPError as e:
                logger.info("applicableIssue not available (%s), trying currentIssue", e)
                try:
                    issues = client.get_current_issues()
                except (_requests.RequestException, ValueError) as e2:
                    logger.error(
                        "Both applicableIssue and currentIssue failed for %s: %s",
                        account_name, e2,
                    )
                    issues = []
            except (_requests.RequestException, ValueError) as e:
                logger.error(
                    "Could not fetch issues for %s: %s. Skipping.",
                    account_name, e,
                )
                issues = []
            # Authoritative server-side list of submitted forms. Narrow
            # the catch to RequestException + ValueError (the JSON path)
            # so a real bug in our pagination logic doesn't get hidden
            # under a "rely on local cache" warning.
            try:
                submitted_reports = client.get_application_report()
            except (_requests.RequestException, ValueError) as e:
                logger.warning(
                    "Could not fetch application report for %s (%s); "
                    "will rely on local cache only this run.",
                    account_name, e,
                )
                submitted_reports = []
            # Eligibility pre-flight for right-share issues: fetch the
            # user's BOID once, then ask the server whether each
            # right-share issue actually has a non-zero reserved
            # quantity for this account. Skipping ineligible issues
            # here avoids spinning up a Playwright browser only to fail
            # at submit. We still close the API session before the
            # browser apply (which logs in independently).
            try:
                own = client.get_own_details() or {}
                demat = own.get("demat") or ""
            except (_requests.RequestException, ValueError) as e:
                logger.warning(
                    "Could not fetch own details for %s (%s); skipping "
                    "right-share eligibility pre-flight.", account_name, e,
                )
                demat = ""
            if demat:
                for issue in issues:
                    if MeroShareClient.classify_issue(issue) != "right_share":
                        continue
                    iid = issue.get("companyShareId")
                    if not iid:
                        continue
                    try:
                        crit = client.get_share_criteria(demat, iid) or {}
                    except (_requests.RequestException, ValueError) as e:
                        # Network error / server hiccup. Leave the
                        # issue eligible and let the browser flow
                        # surface any rejection. Don't fail the run.
                        logger.debug(
                            "Eligibility check fetch failed for issue %s (%s)",
                            iid, e,
                        )
                        continue
                    reserved = crit.get("reservedQuantity")
                    if reserved in (None, ""):
                        # Server didn't report quantity. Assume
                        # eligible (default-permissive). Don't add to
                        # ineligible set.
                        continue
                    try:
                        reserved_f = float(reserved)
                    except (TypeError, ValueError):
                        logger.warning(
                            "Issue %s reservedQuantity=%r is not a number; "
                            "treating as eligible.", iid, reserved,
                        )
                        continue
                    if reserved_f <= 0:
                        ineligible_issue_ids.add(str(iid))
        finally:
            client.logout()

        if not issues:
            logger.info("No open issues for %s.", account_name)
            _record(account, success=True, message="No open issues")
            continue

        logger.info("Found %d open issue(s) for %s.", len(issues), account_name)

        submitted_share_ids = {
            str(r.get("companyShareId", "")) for r in submitted_reports
            if r.get("companyShareId")
        }
        account_applied = applied_state.setdefault(account_id, {})
        applied_for_account: list[str] = []
        failures_for_account: list[str] = []
        # Batch "seed from server report" writes into a single save
        # at the end of the loop. Calling _safe_update_applied for
        # every seeded issue would mean an O(N×M) full-file rewrite
        # for multi-account runs. Per-submit saves still fire one at
        # a time: each represents a real money-on-the-line submission
        # we must not lose to a mid-loop crash.
        pending_seeds: dict[str, dict] = {}

        for issue in issues:
            issue_id = str(issue.get("companyShareId", issue.get("id", "")))
            company_name = issue.get("companyName", "Unknown")
            issue_type = MeroShareClient.classify_issue(issue)

            logger.info("  [%s] %s | %s", issue_type.upper(), company_name,
                        issue.get("shareGroupName", ""))

            if not share_prefs.get(issue_type, False):
                logger.info("    -> Skipped (type '%s' disabled)", issue_type)
                continue

            # Right-share pre-flight: server says reservedQuantity is 0
            # for this BOID. Skip without launching the browser.
            if issue_id in ineligible_issue_ids:
                logger.info("    -> Skipped (no reserved quantity for %s)",
                            account_name)
                continue

            # Server says we've already submitted this. Seed the local
            # cache (so future runs short-circuit even if the report
            # fetch fails) and skip without touching the browser flow.
            #
            # The `not in account_applied` guard means this branch only
            # mutates state on the first time we see a server-reported
            # apply. So `seeded_any=True` correctly stays in sync with
            # whether we actually changed anything. Without the guard
            # we'd flip it (and trigger a save) every cycle even when
            # the cache already had the entry.
            if issue_id in submitted_share_ids and issue_id not in account_applied:
                report = next(
                    (r for r in submitted_reports
                     if str(r.get("companyShareId", "")) == issue_id),
                    {},
                )
                seed_entry = {
                    "company": company_name,
                    "type": issue_type,
                    # Normalize the server's appliedDate to an ISO
                    # string so the GUI's `new Date(...)` parses it
                    # consistently. MeroShare uses several formats
                    # ("2026-05-04 02:30:00", "2026/05/04 ..."); fall
                    # back to "now" when nothing parseable is given.
                    "applied_at": _normalize_applied_date(report.get("appliedDate")),
                    "message": "Seeded from MeroShare application report",
                }
                account_applied[issue_id] = seed_entry  # in-memory skip cache
                pending_seeds[issue_id] = seed_entry  # flushed once per account

            if issue_id in account_applied:
                logger.info("    -> Already applied on %s",
                            account_applied[issue_id].get("applied_at", "?"))
                continue

            if not auto_config.get("enabled", False):
                logger.info("    -> Auto-apply disabled.")
                # Notification removed by request — surfacing every new
                # IPO/right share was too chatty. The dashboard's Open
                # Issues tab already shows them at a glance.
                continue

            if dry_run:
                logger.info("    -> [DRY RUN] Would apply for %s on %s",
                            company_name, account_name)
                continue

            try:
                # Per-account override beats the global config: lets users
                # size their primary account at e.g. 50 kitta while
                # keeping spouse/relative accounts at 10 without flipping
                # the global setting between cycles. Falls back to the
                # global default when the per-account field is unset.
                default_kitta = (
                    account.get("default_kitta")
                    or auto_config.get("default_kitta", 10)
                )
                apply_max = (
                    issue_type == "right_share"
                    and auto_config.get("right_share_apply_max", True)
                )
                max_amount = auto_config.get("max_amount") or None
                # Use the issue's actual share price for max_amount
                # capping. Falls back to None (=> browser_apply uses
                # 100 NPR + warning) if the field isn't present.
                try:
                    share_price = float(issue.get("sharePerUnit") or 0) or None
                except (TypeError, ValueError):
                    share_price = None
                logger.info(
                    "    -> Applying for %s on %s (kitta=%d, apply_max=%s, "
                    "max_amount=%s, share_price=%s)...",
                    company_name, account_name, default_kitta, apply_max,
                    max_amount, share_price,
                )

                result = apply_with_retry(
                    int(issue_id),
                    retries=1,
                    headless=True,
                    default_kitta=default_kitta,
                    credentials=account,
                    apply_max=apply_max,
                    max_amount=max_amount,
                    share_price=share_price,
                )

                if result["success"]:
                    # Distinguish a fresh submission from a server-side
                    # "you've already applied" detection. Both are
                    # legitimate `success=True` outcomes (we shouldn't
                    # retry either), but firing "Share Applied!" for
                    # the latter misleads the user into thinking the
                    # current cycle just submitted — which can mask a
                    # real failure earlier in their day.
                    already = bool(result.get("already_applied"))
                    submit_entry = {
                        "company": company_name,
                        "type": issue_type,
                        "applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "message": result.get("message", ""),
                        "already_applied": already,
                    }
                    account_applied[issue_id] = submit_entry  # in-memory cache
                    # Lock-protected RMW so a parallel GUI apply on a
                    # *different* issue for the same account doesn't get
                    # clobbered when we write back. The previous
                    # snapshot-save pattern lost concurrent writes.
                    def _commit(state, _aid=account_id, _iid=issue_id, _entry=submit_entry):
                        state.setdefault(_aid, {})[_iid] = _entry
                    _safe_update_applied(_commit, context=f"submit {issue_id}")
                    if already:
                        logger.info(
                            "    -> ALREADY APPLIED (synced from MeroShare): %s",
                            result["message"],
                        )
                    else:
                        applied_list.append(f"{company_name} ({account_name})")
                        applied_for_account.append(company_name)
                        notify("Share Applied!", f"{account_name}: {company_name}")
                        logger.info("    -> SUCCESS: %s", result["message"])
                else:
                    msg = result.get("message", "Unknown")
                    failures_for_account.append(f"{company_name}: {msg}")
                    logger.error("    -> FAILED: %s", msg)
                    notify("Application Failed", f"{account_name}/{company_name}")
            except Exception as e:
                failures_for_account.append(f"{company_name}: {e}")
                # Don't pass exc_info=True: the chained exception may
                # carry the request body (CRN, kitta, etc.) into the
                # log file, and meroshare.log isn't gitignored as
                # carefully as accounts.json.
                logger.error("    -> Error applying for %s on %s: %s (%s)",
                             company_name, account_name, type(e).__name__, e)

        # Flush the batched seeds (if any) once per account, again
        # via the lock-protected RMW so a concurrent writer's records
        # are preserved. We replay only the seeds added during THIS
        # cycle into whatever the on-disk state currently looks like.
        if pending_seeds:
            seeds_snapshot = dict(pending_seeds)
            def _flush_seeds(state, _aid=account_id, _seeds=seeds_snapshot):
                bucket = state.setdefault(_aid, {})
                for iid, entry in _seeds.items():
                    bucket.setdefault(iid, entry)  # never overwrite an existing record
            _safe_update_applied(_flush_seeds, context=f"seed batch {account_id}")

        if failures_for_account:
            summary = f"{len(applied_for_account)} applied, {len(failures_for_account)} failed: " \
                      + "; ".join(failures_for_account)
            _record(account, success=False, message=summary,
                    applied=applied_for_account)
        elif applied_for_account:
            _record(account, success=True,
                    message=f"Applied for {', '.join(applied_for_account)}",
                    applied=applied_for_account)
        else:
            _record(account, success=True, message="No new issues to apply for")

    if applied_list:
        logger.info("Applied for: %s", ", ".join(applied_list))
    else:
        logger.info("No new applications made this run.")

    return applied_list


# ── CLI Commands ────────────────────────────────────────────────────


def _first_account_or_none():
    accts = accounts.load()
    return accts[0] if accts else None


def _resolve_account(account_id: str | None) -> dict | None:
    """Pick an account by id, falling back to the first one.

    Returns None (and logs) if no accounts are configured or the
    requested id doesn't exist. Used by the CLI subcommands so a user
    with multiple accounts can target a specific one.
    """
    accts = accounts.load()
    if not accts:
        logger.error("No accounts configured. Add one in the GUI Settings tab.")
        return None
    if account_id:
        for a in accts:
            if a["id"] == account_id:
                return a
        logger.error(
            "Account id '%s' not found. Known ids: %s",
            account_id, ", ".join(a["id"] for a in accts),
        )
        return None
    return accts[0]


def list_issues(account_id: str | None = None):
    """Display all current open issues for an account.

    With no `account_id` uses the first account; otherwise looks up the
    given id. Accepts the same `--account` flag as the other CLI subs.
    """
    primary = _resolve_account(account_id)
    if not primary:
        return
    client = MeroShareClient(credentials=primary)
    if not client.login():
        logger.error("Login failed.")
        client.session.close()
        return

    try:
        try:
            issues = client.get_applicable_issues()
        except _requests.HTTPError as e:
            logger.info("applicableIssue not available (%s), trying currentIssue", e)
            issues = client.get_current_issues()
    finally:
        client.logout()

    if not issues:
        print("\nNo open issues found.\n")
        return

    applied_state = load_applied()
    primary_id = primary["id"] if primary else "default"
    applied_for_account = applied_state.get(primary_id, {})

    print(f"\n{'='*80}")
    print(f"  CURRENT OPEN ISSUES ({len(issues)} found)")
    print(f"{'='*80}")

    for issue in issues:
        issue_id = str(issue.get("companyShareId", issue.get("id", "")))
        company = issue.get("companyName", "?")
        share_type = issue.get("shareTypeName", "?")
        share_group = issue.get("shareGroupName", "?")
        issue_type = MeroShareClient.classify_issue(issue)
        status = "APPLIED" if issue_id in applied_for_account else "OPEN"

        print(f"\n  ID: {issue_id}")
        print(f"  Company: {company}")
        print(f"  Type: {share_type} | Group: {share_group}")
        print(f"  Category: {issue_type}")
        print(f"  Status: {status}")
        print(f"  {'-'*40}")

    print()


def show_status(account_id: str | None = None):
    """Show current application report across all accounts (or one).

    With `account_id` set, only that account's applications are listed;
    otherwise every configured account is queried in turn.
    """
    all_accounts = accounts.load()
    if not all_accounts:
        logger.error("No accounts configured. Add one in the GUI Settings tab.")
        return
    if account_id:
        all_accounts = [a for a in all_accounts if a["id"] == account_id]
        if not all_accounts:
            logger.error("Account id '%s' not found.", account_id)
            return
    all_reports = []
    for account in all_accounts:
        client = MeroShareClient(credentials=account)
        if not client.login():
            label = account["name"] if account else "default"
            logger.error("Login failed for %s.", label)
            client.session.close()
            continue
        try:
            reports = client.get_application_report()
            label = account["name"] if account else "default"
            for r in reports:
                r["_account"] = label
            all_reports.extend(reports)
        finally:
            client.logout()
    reports = all_reports

    if not reports:
        print("\nNo applications found.\n")
        return

    print(f"\n{'='*80}")
    print(f"  APPLICATION REPORT ({len(reports)} applications)")
    print(f"{'='*80}")

    for report in reports:
        acct = report.get("_account", "")
        prefix = f"[{acct}] " if acct else ""
        print(f"\n  {prefix}Company: {report.get('companyName', '?')}")
        print(f"  Share Type: {report.get('shareTypeName', '?')}")
        print(f"  Applied Kitta: {report.get('appliedKitta', '?')}")
        print(f"  Amount: Rs. {report.get('amount', '?')}")
        print(f"  Status: {report.get('statusName', report.get('status', '?'))}")
        print(f"  {'-'*40}")

    print()


def apply_single(issue_id: str, config: dict, account_id: str | None = None):
    """Manually apply for a specific issue by ID.

    `account_id` lets multi-account users target a specific account;
    defaults to the first configured account.
    """
    primary = _resolve_account(account_id)
    if not primary:
        return
    client = MeroShareClient(credentials=primary)
    if not client.login():
        logger.error("Login failed.")
        client.session.close()
        return

    try:
        details = client.get_issue_details(issue_id)
    finally:
        client.logout()

    company_name = details.get("companyName", "Unknown")
    issue_type = MeroShareClient.classify_issue(details)
    min_kitta = int(details.get("minUnit", 1))
    max_kitta = int(details.get("maxUnit", 10))
    share_price = float(details.get("sharePerUnit", 100))

    print(f"\n  Company: {company_name}")
    print(f"  Type: {issue_type}")
    print(f"  Min/Max Kitta: {min_kitta}/{max_kitta}")
    print(f"  Price per share: Rs. {share_price}")

    confirm = input(f"\n  Apply for {company_name}? (y/N): ")
    if confirm.lower() != "y":
        print("  Cancelled.")
        return

    auto_config = config.get("auto_apply") or {}
    default_kitta = auto_config.get("default_kitta", 10)
    apply_max = (
        issue_type == "right_share"
        and auto_config.get("right_share_apply_max", True)
    )
    max_amount = auto_config.get("max_amount") or None
    print("  Applying via browser...")
    result = apply_via_browser(
        int(issue_id), headless=False,
        default_kitta=default_kitta,
        credentials=primary,
        apply_max=apply_max,
        max_amount=max_amount,
        share_price=share_price,
    )

    if result["success"]:
        print(f"\n  SUCCESS: {result['message']}")
        applied_state = load_applied()
        primary_id = primary["id"] if primary else "default"
        applied_state.setdefault(primary_id, {})[issue_id] = {
            "company": company_name,
            "type": issue_type,
            "applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if not _safe_save_applied(applied_state, context=f"manual {issue_id}"):
            print("  WARN: applied state could not be persisted. See logs.")
    else:
        print(f"\n  FAILED: {result.get('message', 'Unknown error')}")


def run_daemon(config: dict, dry_run=False):
    """Run the checker on a schedule with fresh login each cycle.

    Handles SIGTERM (sent by launchd / systemd / docker stop) the same
    way as Ctrl-C so a stop signal doesn't leave Playwright browsers
    orphaned in the background.
    """
    import schedule
    import signal

    stop_requested = {"flag": False}

    def _request_stop(signum, _frame):
        logger.info("Received signal %s; stopping daemon after current run.", signum)
        stop_requested["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    interval = config.get("check_interval_hours", 6)

    logger.info("Starting daemon mode. Checking every %d hours.", interval)
    # Boot-ping notification removed by request. The menu bar status
    # line already says "scheduler on" once it's running, which serves
    # the same affordance without firing a toast on every restart.

    # Run immediately on start. The signal handlers are installed
    # above, so a SIGTERM during this initial cycle sets the flag,
    # but check_and_apply itself can run for minutes per account so
    # we only react at the next tick. That's acceptable: stopping
    # mid-submit would be worse than a few extra minutes to a clean
    # boundary.
    check_and_apply(config, dry_run=dry_run)
    if stop_requested["flag"]:
        logger.info("Stop requested during initial run; exiting daemon.")
        return

    # Pass dry_run by keyword so a future signature reorder of
    # check_and_apply (config, dry_run, results) can't silently send
    # `dry_run` into the wrong slot.
    schedule.every(interval).hours.do(
        check_and_apply, config=config, dry_run=dry_run,
    )

    while not stop_requested["flag"]:
        schedule.run_pending()
        # Sleep in 1-second slices so SIGTERM is observed promptly
        # rather than after a full minute (matters when launchd is
        # waiting to reap us).
        for _ in range(60):
            if stop_requested["flag"]:
                break
            time.sleep(1)
    logger.info("Daemon stopped.")


# ── Main ────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="MeroShare Auto-Apply Tool - Never miss an IPO or Right Share",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--daemon", action="store_true", help="Run continuously on schedule")
    parser.add_argument("--list", action="store_true", help="List current open issues")
    parser.add_argument("--status", action="store_true", help="Show application report")
    parser.add_argument("--apply", metavar="ID", help="Apply for a specific issue by ID")
    parser.add_argument(
        "--account", metavar="ID",
        help="Operate on a specific account id (for --apply/--list/--status). "
             "Defaults to first account when omitted.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Check but don't apply")

    args = parser.parse_args()
    try:
        config = load_config()
    except ConfigError as e:
        logger.error("%s", e)
        sys.exit(1)

    if args.list:
        list_issues(account_id=args.account)
    elif args.status:
        show_status(account_id=args.account)
    elif args.apply:
        apply_single(args.apply, config, account_id=args.account)
    elif args.daemon:
        run_daemon(config, args.dry_run)
    else:
        check_and_apply(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
