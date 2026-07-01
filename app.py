#!/usr/bin/env python3
"""
MeroShare Auto-Apply - Web GUI
A clean web interface for managing your MeroShare share applications.
Run this and open http://localhost:5050 in your browser.
"""

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests as _requests
from flask import Flask, jsonify, redirect, render_template, request, url_for

from browser_apply import apply_with_retry

import accounts
import meroshare_client
import scheduler
import secrets_store

# Re-export for code paths that referenced the class directly.
MeroShareClient = meroshare_client.MeroShareClient

# State files (config, logs) live in accounts.STATE_DIR. Which honors
# MEROSHARE_DATA_DIR when the bundled .app launcher sets it.
CONFIG_FILE = accounts.STATE_DIR / "config.json"
LOG_FILE = accounts.STATE_DIR / "logs" / "meroshare.log"

LOG_FILE.parent.mkdir(exist_ok=True, parents=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        # 2 MB / 5 rotations keeps disk usage bounded for a long-running
        # daemon. Without this, logs/meroshare.log grew unbounded.
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5
        ),
    ],
)
logger = logging.getLogger("meroshare")

_CREDENTIAL_PATTERN = __import__("re").compile(
    r"\b(password|pin|crn|pwd)\s*[=:][^\s,&]+",
    flags=__import__("re").IGNORECASE,
)


def _safe_exc(e: Exception) -> str:
    """Render an exception for log without leaking request bodies.

    `requests.HTTPError`'s str() includes the URL — typically
    harmless for our endpoints (auth is via header), but a 400 with
    the submitted payload echoed back can show up in `args[0]`. Truncate
    aggressively so an oversized error string can't bloat the log,
    and scrub anything that looks like `key=value` for sensitive
    keys (defense-in-depth: even if a future endpoint URL-encodes
    credentials, they don't end up in the log).
    """
    msg = str(e)
    msg = _CREDENTIAL_PATTERN.sub(r"\1=[REDACTED]", msg)
    if len(msg) > 200:
        msg = msg[:200] + " …(truncated)"
    return f"{type(e).__name__}: {msg}"


app = Flask(__name__)


# Strip the version-leaking Server header. Werkzeug's default value
# is "Werkzeug/<version> Python/<version>", which discloses the
# Python interpreter version to anyone who can reach the localhost
# port. We don't expose this to the network, but defense-in-depth.
@app.after_request
def _strip_server_header(resp):
    resp.headers["Server"] = "MeroShare"
    return resp


# Boot timestamp used for cache-busting static asset URLs and for the
# /api/version live-reload poller. Set once per process. Werkzeug's
# reloader spawns a child for each restart, so each child gets its own.
APP_BOOT_TS = int(time.time())


@app.context_processor
def _inject_boot_ts():
    """Available as {{ boot_ts }} in templates so static asset links can
    cache-bust on every server restart. Also provides human-readable
    forms: `boot_iso` for `<time datetime="...">` and `boot_human`
    for the rendered footer build label.
    """
    boot_dt = datetime.fromtimestamp(APP_BOOT_TS).astimezone()
    return {
        "boot_ts": APP_BOOT_TS,
        "boot_iso": boot_dt.isoformat(timespec="seconds"),
        "boot_human": boot_dt.strftime("%Y-%m-%d %H:%M"),
    }


# Methods that change state on the server. POSTed/DELETED/PUT requests
# need an Origin/Referer cross-check to defend against DNS-rebinding
# attacks: an attacker can lure the user to a page whose DNS resolves
# to 127.0.0.1, and a `fetch('http://localhost:5050/api/restore', ...)`
# from there will reach this Flask process. The localhost bind alone
# does not stop that; an Origin check does.
_STATE_CHANGING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
# GET endpoints that return plaintext secrets must ALSO pass the cross-origin
# guard — otherwise a DNS-rebound attacker page can read them (they can read a
# same-origin GET response, unlike a no-cors POST).
_SENSITIVE_GET_PATHS = {"/api/backup"}
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def _is_local_origin(origin_or_referer: str | None) -> bool:
    """True if the URL's host matches the loopback set we accept."""
    if not origin_or_referer:
        return False
    try:
        parsed = urlparse(origin_or_referer)
    except ValueError:
        return False
    host = parsed.hostname or ""
    return host in _ALLOWED_HOSTS


@app.before_request
def _guard_state_changing_requests():
    sensitive_get = request.method == "GET" and request.path in _SENSITIVE_GET_PATHS
    if request.method not in _STATE_CHANGING_METHODS and not sensitive_get:
        return None
    # Threat model: a browser at evil.example does
    # `fetch('http://localhost:5050/api/factory-reset', {method: 'POST',
    # mode: 'no-cors', credentials: 'include'})`. The fetch is sent
    # but cannot read the response, yet the server still executes
    # the action. We need to identify that browser request as
    # cross-origin and reject it.
    #
    # Allow paths (any one is sufficient):
    #   - Origin or Referer header points at loopback
    #   - Sec-Fetch-Site is "same-origin" or "none" (the latter is
    #     what address-bar typed URLs and bookmarks send)
    #
    # Reject paths:
    #   - Sec-Fetch-Site is "cross-site" or "same-site". Those are
    #     the explicit cross-origin signals modern browsers send and
    #     a malicious page CANNOT spoof them (the browser sets them).
    #   - Origin/Referer points at a non-loopback host
    #   - No origin signals at all AND the request comes from
    #     something that looks like a browser (this is where DNS
    #     rebinding lands; non-browser scripts are allowed because
    #     they're trusted local processes).
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    sec_fetch_site = request.headers.get("Sec-Fetch-Site")
    user_agent = (request.headers.get("User-Agent") or "").lower()

    # Hard-reject: browser-issued explicit cross-origin signals.
    # These cannot be spoofed by JavaScript on the attacker page.
    if sec_fetch_site in ("cross-site", "same-site"):
        return jsonify({"error": "request rejected: cross-origin"}), 403

    # Allow: any verifiable same-origin signal.
    if sec_fetch_site in ("same-origin", "none"):
        return None
    if _is_local_origin(origin) or _is_local_origin(referer):
        return None

    # Allow: Origin/Referer present but neither parses as loopback -
    # already rejected by `_is_local_origin` returning False above
    # AND falling through.
    if origin is not None or referer is not None:
        return jsonify({"error": "request rejected: non-loopback origin"}), 403

    # No origin signals at all. This is curl/wget/scripts on the
    # local machine for legitimate cases, but it's also the DNS-
    # rebinding scenario. Allow only when the User-Agent doesn't
    # look like a browser. A spoofed Mozilla UA from curl is rare
    # and dodges the rest of the security filters in the wild;
    # rejecting common browser tokens is a defense-in-depth tweak.
    looks_like_browser = any(
        tok in user_agent for tok in ("mozilla", "chrome", "safari", "firefox", "edg/", "opera")
    )
    if looks_like_browser:
        return jsonify({"error": "request rejected: browser without origin signals"}), 403
    return None


# In-memory state for background tasks. `_bg_lock` guards the
# check-then-set on `_bg_status['running']` so two simultaneous POSTs
# can't both pass the gate before either flips it. Flask's dev server
# is threaded, so this race is real, not theoretical.
_bg_lock = threading.Lock()
_bg_status = {"running": False, "message": "", "results": {}}


def _claim_bg(message: str) -> bool:
    """Atomically claim the background slot. Returns False if already taken."""
    with _bg_lock:
        if _bg_status.get("running"):
            return False
        _bg_status["running"] = True
        _bg_status["message"] = message
        _bg_status["results"] = {}
        return True


def _bg_set(**kwargs) -> None:
    """Mutate _bg_status under the lock. Use for any write from a worker
    thread so the read in api_apply_status doesn't see a torn state."""
    with _bg_lock:
        _bg_status.update(kwargs)


def _bg_set_result(account_id: str, payload: dict) -> None:
    """Insert one per-account result under the lock."""
    with _bg_lock:
        _bg_status["results"][account_id] = payload


def _bg_snapshot() -> dict:
    """Return a deep-enough copy of _bg_status for safe JSON serialization.
    `results` is a nested dict mutated by worker threads; iterating it
    during json.dumps without a lock can raise RuntimeError when a key
    is added mid-iteration."""
    with _bg_lock:
        return {
            "running": _bg_status["running"],
            "message": _bg_status["message"],
            "results": {k: dict(v) for k, v in _bg_status["results"].items()},
        }


# ── Helpers ─────────────────────────────────────────────────────────


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    # Mirrors setup.py's wizard default. `notifications` is included so
    # the GUI's saveConfig (which preserves _loadedConfig) doesn't drop
    # it on first save when no config.json exists yet.
    return {
        "share_types": {"ipo_ordinary": True, "right_share": True, "fpo": False, "mutual_fund": False, "debenture": False},
        "auto_apply": {"enabled": True, "default_kitta": 10, "right_share_apply_max": True, "max_amount": 100000},
        "check_interval_hours": 6,
        "notifications": {"desktop": True, "log_file": True},
    }


def save_config(config):
    # Atomic write so an interrupted save (Ctrl-C, OS crash, full disk)
    # can't leave config.json half-written. Mode 0o600 to match the
    # rest of the project's file-perm hygiene. Config.json doesn't
    # contain credentials, but a uniform restrictive set keeps `ls
    # -la` predictable and protects against future fields that might.
    accounts._atomic_write(
        CONFIG_FILE, json.dumps(config, indent=2), mode=0o600,
    )


def has_any_account():
    return len(accounts.load()) > 0


# ── Application-report cache ───────────────────────────────────────
# /api/issues marks each chip "applied" by consulting MeroShare's own
# application report per account. The authoritative source. Without
# that cross-check, a user who applied directly on the MeroShare
# website would see stale "○ pending" chips here and risk a double
# submit.
#
# That requires a login + report fetch per account on every /api/issues
# call (the dashboard auto-refreshes every 60s), so we cache the report
# per account for REPORT_TTL seconds and invalidate after a successful
# apply for that account. `?force=true` busts the cache.

REPORT_TTL_SECONDS = 300
# Kept short so a closed issue drops out of the menu bar / dashboard quickly
# after MeroShare removes it from the applicable list. The menu bar polls every
# 30s; a 120s TTL bounds the stale window to ~2 min without hammering logins.
APPLICABLE_TTL_SECONDS = 120

_report_lock = threading.Lock()
_report_cache: dict = {}        # {account_id: (fetched_at_ts, {company_share_id_str: applicantFormId})}
_applicable_lock = threading.Lock()
_applicable_cache: dict = {}    # {account_id: (fetched_at_ts, [issue_dict])}


def _report_get_cached(account_id: str):
    """Return cached set of applied company_share_ids for an account, or None."""
    with _report_lock:
        entry = _report_cache.get(account_id)
        if not entry:
            return None
        fetched_at, ids = entry
        if (time.time() - fetched_at) > REPORT_TTL_SECONDS:
            return None
        return dict(ids)  # snapshot


def _report_put(account_id: str, ids: dict) -> None:
    with _report_lock:
        _report_cache[account_id] = (time.time(), dict(ids))


def _report_invalidate(account_id: str) -> None:
    with _report_lock:
        _report_cache.pop(account_id, None)


def _applicable_get_cached(account_id: str):
    """Return the cached list of eligible issues for an account, or None.

    Right shares are reserved to existing shareholders, so the applicable-
    issues list differs per account. We cache per account and use it as
    the source of truth for which chips appear on each issue row.
    """
    with _applicable_lock:
        entry = _applicable_cache.get(account_id)
        if not entry:
            return None
        fetched_at, issues = entry
        if (time.time() - fetched_at) > APPLICABLE_TTL_SECONDS:
            return None
        return list(issues)


def _applicable_put(account_id: str, issues: list) -> None:
    with _applicable_lock:
        _applicable_cache[account_id] = (time.time(), list(issues))


def _applicable_invalidate(account_id: str) -> None:
    with _applicable_lock:
        _applicable_cache.pop(account_id, None)


# Asia/Kathmandu (UTC+05:45, no DST) — MeroShare's clock. MEROSHARE_TZ can
# override for tests. Fixed offset so no tzdata is required.
def _npt_tz():
    name = os.environ.get("MEROSHARE_TZ", "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(hours=5, minutes=45), name="Asia/Kathmandu")


def _issue_is_closed(issue: dict) -> bool:
    """True only when we are CONFIDENT the issue's apply window has ended.

    Conservative on purpose: any parse ambiguity returns False (keep showing
    the issue). For a real-money tool, wrongly hiding a still-open issue (a
    missed application) is worse than briefly showing one that just closed.
    The close date is treated as END of that day in NPT, so an issue on its
    close day is never dropped early.
    """
    raw = issue.get("issueCloseDate")
    if not raw:
        return False
    try:
        npt = _npt_tz()
        dt = datetime.fromisoformat(str(raw).strip().replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=npt)
        if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt < datetime.now(npt)
    except (ValueError, TypeError):
        return False


def _lookup_share_price(issue_id: str, accounts_in_scope: list[dict]) -> float | None:
    """Find the per-share price for an issue by scanning per-account caches.

    Returns the first finite positive price found, or None when the issue
    isn't in any cached applicable list (or the field is missing/garbled).
    Used by /api/apply so browser_apply.py's max_amount cap converts to
    kitta at the real price rather than the 100 NPR fallback.
    """
    import math
    with _applicable_lock:
        for account in accounts_in_scope:
            entry = _applicable_cache.get(account["id"])
            if not entry:
                continue
            _, issues = entry
            for issue in issues:
                if str(issue.get("companyShareId", "")) != issue_id:
                    continue
                raw = issue.get("sharePerUnit")
                if raw in (None, ""):
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(val) and val > 0:
                    return val
    return None


# ── Routes ──────────────────────────────────────────────────────────


@app.route("/")
def index():
    if not has_any_account():
        return redirect(url_for("settings"))
    return render_template("index.html")


@app.route("/api/issues")
def api_issues():
    """Fetch open issues with per-account eligibility and application state.

    Per-account fetching matters because right shares are reserved to
    existing shareholders. Each account's applicable-issues list is
    different, so we can't borrow one account's view for all. The
    response includes an issue only for accounts where the issue is
    eligible (or where the local diary says we already applied for it,
    so allotment results don't disappear from the chips when an issue
    later becomes ineligible).

    Both the per-account applicable-issues list and the application
    report are cached for ~5 minutes; `?force=true` busts both.
    """
    all_accounts = accounts.load()
    if not all_accounts:
        return jsonify({"error": "No accounts configured"}), 400

    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    applied_local = accounts.load_applied()

    failed_logins: list = []
    server_applied: dict = {}     # {account_id: {company_share_id: applicantFormId}}
    eligible_ids: dict = {}       # {account_id: set(company_share_id strings)}
    issue_by_id: dict = {}        # {company_share_id: issue_dict}. First writer wins
    report_unknown: set = set()
    eligibility_unknown: set = set()

    for account in all_accounts:
        acct_id = account["id"]
        cached_report = None if force else _report_get_cached(acct_id)
        cached_issues = None if force else _applicable_get_cached(acct_id)

        if cached_report is not None and cached_issues is not None:
            server_applied[acct_id] = cached_report
            ids_set = set()
            for i in cached_issues:
                cid = str(i.get("companyShareId", ""))
                if cid:
                    ids_set.add(cid)
                    issue_by_id.setdefault(cid, i)
            eligible_ids[acct_id] = ids_set
            continue

        client = MeroShareClient(credentials=account)
        if not client.login():
            tag = client.last_login_error or "unknown"
            failed_logins.append(f"{account['name']} ({tag})")
            client.session.close()
            # Use whatever cache we have; mark unknowns for the rest.
            if cached_report is not None:
                server_applied[acct_id] = cached_report
            else:
                report_unknown.add(acct_id)
            if cached_issues is not None:
                ids_set = set()
                for i in cached_issues:
                    cid = str(i.get("companyShareId", ""))
                    if cid:
                        ids_set.add(cid)
                        issue_by_id.setdefault(cid, i)
                eligible_ids[acct_id] = ids_set
            else:
                eligibility_unknown.add(acct_id)
                eligible_ids[acct_id] = set()
            continue

        try:
            # Eligible issues for this account.
            if cached_issues is None:
                try:
                    fetched = client.get_applicable_issues()
                except _requests.HTTPError as e:
                    logger.info("applicableIssue not available for %s (%s); "
                                "falling back to currentIssue", account["name"], e)
                    fetched = client.get_current_issues()
                _applicable_put(acct_id, fetched)
            else:
                fetched = cached_issues
            ids_set = set()
            for i in fetched:
                cid = str(i.get("companyShareId", ""))
                if cid:
                    ids_set.add(cid)
                    issue_by_id.setdefault(cid, i)
            eligible_ids[acct_id] = ids_set

            # Application report for this account.
            if cached_report is None:
                try:
                    reports = client.get_application_report()
                    ids = {
                        str(r["companyShareId"]): r.get("applicantFormId")
                        for r in reports
                        if r.get("companyShareId") is not None
                    }
                    server_applied[acct_id] = ids
                    _report_put(acct_id, ids)
                except Exception as e:
                    logger.warning(
                        "Could not fetch application report for %s: %s",
                        account["name"], e,
                    )
                    report_unknown.add(acct_id)
            else:
                server_applied[acct_id] = cached_report
        finally:
            client.logout()

    # If every account failed to login *and* we have no cached eligibility,
    # there's literally nothing to show. Surface the error rather than
    # an empty list that looks like "no open issues".
    if not issue_by_id and len(failed_logins) == len(all_accounts):
        # Generic message: account names ("Wife", "Mom", etc.) are
        # personal metadata and don't belong in API error bodies -
        # the per-account breakdown stays in logs/meroshare.log for
        # local inspection.
        rate_limited = any("rate_limited" in m for m in failed_logins)
        if rate_limited:
            msg = ("Login failed for all accounts: MeroShare is throttling "
                   "logins. Wait 5–10 minutes and try again.")
        else:
            msg = (f"Login failed for all {len(all_accounts)} account(s). "
                   "Check credentials in Settings; details in logs/meroshare.log.")
        return jsonify({"error": msg}), 401

    # Pull in any locally-recorded applications for issues we don't see
    # in any account's applicable list. The issue dict won't have details
    # like shareTypeName, but the user still wants visibility.
    for _acct_id, recs in applied_local.items():
        for issue_id, rec in recs.items():
            if issue_id not in issue_by_id:
                issue_by_id[issue_id] = {
                    "companyShareId": issue_id,
                    "companyName": rec.get("company", "?"),
                    "shareTypeName": "",
                    "shareGroupName": "",
                }

    result = []
    for issue_id, issue in issue_by_id.items():
        applications = {}
        for account in all_accounts:
            acct_id = account["id"]
            is_eligible = issue_id in eligible_ids.get(acct_id, set())
            local_rec = applied_local.get(acct_id, {}).get(issue_id)
            # An account with no eligibility AND no local application is
            # simply absent from this issue. Not a "pending" chip.
            if not is_eligible and local_rec is None:
                continue
            on_server = issue_id in server_applied.get(acct_id, {})
            applications[acct_id] = {
                "accountName": account["name"],
                "applied": on_server or (local_rec is not None),
                "appliedAt": local_rec.get("applied_at") if local_rec else None,
                "stateUnknown": acct_id in report_unknown and local_rec is None,
            }
        if not applications:
            # Defensive: if the issue ended up with zero chips (no eligible
            # accounts, no local records) drop it rather than render an
            # empty row.
            continue
        # Drop an issue whose apply window has closed, UNLESS an account has an
        # application recorded for it (so allotment/history stays visible). This
        # makes a just-closed issue disappear immediately, even while the
        # applicable-issues cache is still warm.
        has_application = any(a["applied"] for a in applications.values())
        if not has_application and _issue_is_closed(issue):
            continue
        # Forward the issue lifecycle dates and share price so the GUI
        # can render a "closes in N hours" urgency badge and a
        # pre-flight cost preview in the apply modal — both purely
        # presentational, but high-impact UX. Names mirror MeroShare's
        # own keys so a future server change shows up as a missing
        # field rather than a silent rename.
        result.append({
            "id": issue_id,
            "company": issue.get("companyName", "?"),
            "shareType": issue.get("shareTypeName", "?"),
            "shareGroup": issue.get("shareGroupName", "?"),
            "reservation": issue.get("reservationTypeName", ""),
            "category": MeroShareClient.classify_issue(issue),
            "issueOpenDate": issue.get("issueOpenDate"),
            "issueCloseDate": issue.get("issueCloseDate"),
            "sharePerUnit": issue.get("sharePerUnit"),
            "applications": applications,
        })
    return jsonify(result)


@app.route("/api/status")
def api_status():
    """Fetch application reports across all accounts.

    `?per_account=N` (default 20, max 200) controls how many of each
    account's most recent reports we hydrate with detail. The previous
    hardcoded 20 was duplicated and silently capped any user with
    bigger histories.
    """
    try:
        per_account = max(1, min(int(request.args.get("per_account", 20)), 200))
    except (TypeError, ValueError):
        per_account = 20

    all_accounts = accounts.load()
    all_reports = []
    failed = []

    for account in all_accounts:
        try:
            client = MeroShareClient(credentials=account)
            if not client.login():
                logger.warning("Login failed for %s in /api/status. Skipping", account["name"])
                failed.append(account["name"])
                continue
            try:
                reports = client.get_application_report()
                trimmed = reports[:per_account]
                for r in trimmed:
                    r["accountId"] = account["id"]
                    r["accountName"] = account["name"]
                    form_id = r.get("applicantFormId")
                    if form_id:
                        try:
                            detail = client.get_application_detail(form_id)
                            r["appliedKitta"] = detail.get("appliedKitta")
                            r["amount"] = detail.get("amount")
                            r["appliedDate"] = detail.get("appliedDate")
                            r["detailStatus"] = detail.get("statusName", "")
                            r["statusDescription"] = detail.get("statusDescription")
                            r["meroshareRemark"] = detail.get("meroshareRemark")
                            r["bankName"] = detail.get("clientName")
                        except Exception as e:
                            logger.warning("Detail fetch failed for %s: %s", form_id, _safe_exc(e))
                all_reports.extend(trimmed)
            finally:
                client.logout()
        except Exception as e:
            logger.warning("Failed to fetch report for %s: %s", account["name"], _safe_exc(e))
            failed.append(account["name"])

    if not all_reports and failed and len(failed) == len(all_accounts):
        return jsonify({
            "error": f"Could not fetch history. All accounts failed: {', '.join(failed)}"
        }), 502
    # Allotment-transition notifications: fire a desktop toast exactly
    # once when an application moves from Pending → Allotted/NotAllotted.
    # State is keyed by applicantFormId so the same poll re-running
    # (the GUI auto-refreshes /api/status) doesn't re-toast. Done after
    # the response shape is final so a notification bug can't change
    # the API contract.
    try:
        _notify_allotment_changes(all_reports)
    except Exception as e:
        # Notification failures must NEVER break the status route.
        logger.warning("Allotment notification pipeline failed: %s", _safe_exc(e))
    return jsonify(all_reports)


# Lowercase substrings that indicate a final allotment outcome.
# MeroShare's spelling has historically been "ALLOTED" (single 't'), but
# we accept either to survive a future rename.
_ALLOTMENT_FINAL_PHRASES = ("allotted", "alloted", "not alloted", "not allotted",
                            "rejected", "refunded")


def _is_final_allotment_status(status: str) -> bool:
    """True when the status text indicates allotment is settled (one of
    Allotted / Not Allotted / Rejected / Refunded). Pending and lookalike
    intermediate states return False — those don't deserve a toast."""
    if not status:
        return False
    lowered = status.lower()
    return any(p in lowered for p in _ALLOTMENT_FINAL_PHRASES)


def _classify_allotment(status: str) -> str:
    """Reduce a free-form MeroShare status to one of:
       'allotted' | 'not_allotted' | 'pending'.
    Used for deciding which notification copy to use."""
    if not status:
        return "pending"
    lowered = status.lower()
    if "not allotted" in lowered or "not alloted" in lowered or "rejected" in lowered:
        return "not_allotted"
    if "allotted" in lowered or "alloted" in lowered:
        return "allotted"
    return "pending"


def _notify_allotment_changes(reports: list[dict]) -> None:
    """Persist allotment-status transitions to the state file.

    Originally also fired desktop notifications on each transition
    (allotted / not allotted / finalized). Removed by user request:
    the apply-time notification is the only one that's meaningful in
    practice — allotment outcomes can be reviewed in the dashboard
    and a toast for "not allotted" is just bad news pestering you.

    The state file is still maintained so that if notifications are
    ever re-enabled, the diff baseline is correct (otherwise turning
    them on would dump a flood of "transitions" for everything that
    happened to be settled at the moment of re-enable).
    """
    state = accounts.load_allotment_state()
    changed = False
    for r in reports:
        form_id = r.get("applicantFormId")
        if not form_id:
            continue
        current = (r.get("detailStatus") or r.get("statusName") or "").strip()
        if not current:
            continue
        prev_entry = state.get(str(form_id)) or {}
        prev_status = prev_entry.get("status", "")
        if current != prev_status:
            state[str(form_id)] = {
                "status": current,
                "company": r.get("companyName") or "",
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            changed = True
    if changed:
        try:
            accounts.save_allotment_state(state)
        except OSError as e:
            logger.warning("Could not persist allotment state: %s", _safe_exc(e))


@app.route("/api/apply/<int:issue_id>", methods=["POST"])
def api_apply(issue_id):
    """Apply for an issue across one or more accounts.

    Body: {"account_ids": ["a", "b"]}. If omitted or empty, applies for
    all accounts that haven't applied yet.
    """
    body = request.get_json(silent=True) or {}
    config = load_config()
    auto_config = config.get("auto_apply") or {}
    global_default_kitta = auto_config.get("default_kitta", 10)
    # The GUI sends `category` from the issue list it's already rendered.
    # Combined with the saved config flag, this is what flips the apply
    # form from the MeroShare-prefilled minimum to the full eligible
    # quantity for right shares. Validate the value: a wrong-cased
    # "RIGHT_SHARE" or non-string body silently bypassed apply_max for
    # right shares, leaving users applying for the prefilled minimum
    # instead of their full eligible quantity.
    raw_category = body.get("category")
    category = raw_category.lower() if isinstance(raw_category, str) else None
    KNOWN_CATEGORIES = {
        "ipo_ordinary", "right_share", "fpo",
        "mutual_fund", "debenture", "preferred_share",
    }
    if category is not None and category not in KNOWN_CATEGORIES:
        return jsonify({
            "error": f"Unknown category {raw_category!r}. "
                     f"Must be one of: {sorted(KNOWN_CATEGORIES)}",
        }), 400
    apply_max = (
        category == "right_share"
        and bool(auto_config.get("right_share_apply_max", True))
    )
    max_amount = auto_config.get("max_amount") or None

    raw_ids = body.get("account_ids")
    # Explicit account_ids only. The previous "default to every account
    # that hasn't applied" silently included accounts that weren't even
    # eligible for the issue (right shares are reserved). The GUI now
    # sends only eligible-pending IDs from the chips, so a missing list
    # means "the caller forgot". Better to fail loudly.
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "account_ids required"}), 400

    all_accounts = accounts.load()
    known_ids = {a["id"] for a in all_accounts}
    unknown = [i for i in raw_ids if i not in known_ids]
    if unknown:
        # Fail before claiming the bg slot. Easier to debug than a
        # silent "no targets" 400 below.
        return jsonify({"error": f"Unknown account_ids: {', '.join(unknown)}"}), 400
    targets = [a for a in all_accounts if a["id"] in raw_ids]

    if not targets:
        return jsonify({"error": "No accounts to apply with"}), 400

    # Look up the issue's per-share price from any account's cached
    # applicable-issues list, so the max_amount cap in browser_apply
    # uses the real number instead of the 100 NPR fallback. Without
    # this, a premium FPO at e.g. 500 NPR/share would be capped at
    # 5x the user's intended budget. The cache is populated by the
    # most recent /api/issues call; if it's stale or empty we pass
    # None and browser_apply emits a warning into the log.
    share_price = _lookup_share_price(str(issue_id), targets)
    if share_price is None:
        logger.info(
            "share_price unknown for issue %s at apply time; "
            "browser_apply will warn and assume 100 NPR for any cap.",
            issue_id,
        )

    if not _claim_bg(f"Starting apply for {len(targets)} account(s)..."):
        return jsonify({"error": "Another task is running"}), 409

    def do_apply():
        # Cross-process apply mutex: never submit while the scheduled daemon (or
        # another apply run) is mid-apply. Manual enter/exit keeps the existing
        # loop body unindented.
        _apply_lock = accounts.try_apply_engine_lock()
        try:
            _apply_ok = _apply_lock.__enter__()
            if not _apply_ok:
                _bg_set(message="Another apply run is already in progress. Try again in a moment.")
                return
            for idx, account in enumerate(targets, 1):
                _bg_set(message=f"Applying for issue {issue_id} on {account['name']} ({idx}/{len(targets)})…")
                # Per-account default_kitta beats the global config so
                # multi-account users can size each account independently.
                effective_kitta = (
                    account.get("default_kitta") or global_default_kitta
                )
                try:
                    result = apply_with_retry(
                        issue_id, retries=1, headless=True,
                        default_kitta=effective_kitta,
                        credentials=account, apply_max=apply_max,
                        max_amount=max_amount,
                        share_price=share_price,
                    )
                except Exception as e:
                    # logger.exception would dump the full Playwright
                    # traceback, which can echo CRN/PIN form values from
                    # the failing call frame into meroshare.log. Mirror
                    # auto_apply.py's safer type+message pattern, and
                    # route the GUI-visible message through _safe_exc
                    # so a 400 echo from MeroShare doesn't leak the
                    # submitted login payload back to the dashboard.
                    safe = _safe_exc(e)
                    logger.error(
                        "Apply crashed for account %s: %s",
                        account["name"], safe,
                    )
                    _bg_set_result(account["id"], {
                        "accountName": account["name"],
                        "success": False,
                        "message": f"Unexpected error: {safe}",
                    })
                    continue
                _bg_set_result(account["id"], {
                    "accountName": account["name"],
                    "success": result.get("success"),
                    "alreadyApplied": bool(result.get("already_applied")),
                    "message": result.get("message", ""),
                })
                if result.get("success"):
                    # update_applied holds the cross-process file lock
                    # for the whole load → mutate → save cycle so a
                    # concurrent scheduler write can't clobber this
                    # record (or vice versa). The `already_applied`
                    # flag is recorded on disk so a future "show me
                    # what THIS app applied" report can distinguish
                    # genuine submissions from server-synced records.
                    already = bool(result.get("already_applied"))
                    def _seed(state, _aid=account["id"], _iid=str(issue_id),
                              _msg=result["message"], _already=already):
                        state.setdefault(_aid, {})[_iid] = {
                            "applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "message": _msg,
                            "already_applied": _already,
                        }
                    accounts.update_applied(_seed)
                    # MeroShare's report and applicable-issues lists both
                    # change for this account when an apply succeeds -
                    # invalidate both so the next /api/issues sees fresh.
                    _report_invalidate(account["id"])
                    _applicable_invalidate(account["id"])
            with _bg_lock:
                successes = sum(1 for r in _bg_status["results"].values() if r.get("success"))
            _bg_set(message=f"Done. {successes}/{len(targets)} account(s) applied")
        finally:
            try:
                _apply_lock.__exit__(None, None, None)
            except Exception:
                pass
            _bg_set(running=False)

    thread = threading.Thread(target=do_apply, daemon=True)
    thread.start()
    return jsonify({"status": "started", "accounts": [a["name"] for a in targets]})


@app.route("/api/apply-status")
def api_apply_status():
    """Check background apply status."""
    return jsonify(_bg_snapshot())


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


def _validate_config(config: dict) -> str | None:
    """Schema-check the config payload before persisting.

    Returns an error string (caller surfaces as 400) or None when OK.
    Validation is deliberately tight: anything that auto_apply.py /
    browser_apply.py reads must be the right type and within sensible
    bounds, otherwise the next scheduled run crashes from a bad
    persisted value (e.g. max_amount=-1 → negative kitta cap).
    """
    if not isinstance(config, dict):
        return "config must be a JSON object"
    interval = config.get("check_interval_hours")
    if interval is not None and (not isinstance(interval, int) or not 1 <= interval <= 24):
        return "check_interval_hours must be an integer 1..24"
    # share_types: explicit allowlist of recognized keys, plus a
    # bool-only value rule. The classifier in meroshare_client knows
    # ipo_ordinary, right_share, fpo, mutual_fund, debenture, and
    # preferred_share. An unknown key would never be acted on, so
    # rejecting it loudly is better than silently storing dead config.
    KNOWN_SHARE_TYPES = {
        "ipo_ordinary", "right_share", "fpo",
        "mutual_fund", "debenture", "preferred_share",
    }
    share_types = config.get("share_types")
    if share_types is not None:
        if not isinstance(share_types, dict):
            return "share_types must be an object"
        for k, v in share_types.items():
            if k not in KNOWN_SHARE_TYPES:
                return f"share_types.{k} is not a recognized share type"
            if not isinstance(v, bool):
                return f"share_types.{k} must be a boolean"
    auto = config.get("auto_apply")
    if auto is not None:
        if not isinstance(auto, dict):
            return "auto_apply must be an object"
        if "enabled" in auto and not isinstance(auto["enabled"], bool):
            return "auto_apply.enabled must be a boolean"
        kitta = auto.get("default_kitta")
        if kitta is not None and (not isinstance(kitta, int) or not 1 <= kitta <= 100_000):
            return "auto_apply.default_kitta must be an integer 1..100000"
        max_amount = auto.get("max_amount")
        if max_amount not in (None, "", 0) and (
            not isinstance(max_amount, (int, float)) or max_amount < 0
        ):
            return "auto_apply.max_amount must be a non-negative number or null"
        if "right_share_apply_max" in auto and not isinstance(
            auto["right_share_apply_max"], bool
        ):
            return "auto_apply.right_share_apply_max must be a boolean"
    notif = config.get("notifications")
    if notif is not None:
        if not isinstance(notif, dict):
            return "notifications must be an object"
        if "desktop" in notif and not isinstance(notif["desktop"], bool):
            return "notifications.desktop must be a boolean"
    return None


@app.route("/api/config", methods=["POST"])
def api_save_config():
    config = request.get_json(silent=True)
    err = _validate_config(config)
    if err:
        return jsonify({"error": err}), 400
    save_config(config)
    return jsonify({"status": "saved"})


@app.route("/api/accounts", methods=["GET"])
def api_get_accounts():
    return jsonify([accounts.mask(a) for a in accounts.load()])


@app.route("/api/accounts", methods=["POST"])
def api_create_account():
    body = request.get_json() or {}
    try:
        record = accounts.add(body)
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(accounts.mask(record))


@app.route("/api/accounts/<account_id>", methods=["PUT"])
def api_update_account(account_id):
    body = request.get_json() or {}
    # accounts.update() handles the masked-placeholder skip itself
    # (single source of truth for the mask shape), so the route just
    # passes the payload through. Validation errors from update()
    # propagate as 400. They include "name must be...", "pin must
    # be 4 digits", "duplicate name", etc.
    try:
        record = accounts.update(account_id, body)
    except accounts.AccountError as e:
        # Distinguish "not found" from "validation": the former returns
        # the same suffix the get() helper uses.
        msg = str(e)
        status = 404 if "not found" in msg else 400
        return jsonify({"error": msg}), status
    return jsonify(accounts.mask(record))


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    try:
        accounts.delete(account_id)
    except accounts.AccountError as e:
        return jsonify({"error": str(e)}), 404
    # Drop the deleted account's cached report/eligibility so a same-named
    # re-add can't be served another account's stale data, and to not leak
    # per-account memory on churn (mirrors the api_apply success path).
    _report_invalidate(account_id)
    _applicable_invalidate(account_id)
    return jsonify({"status": "deleted"})


@app.route("/api/accounts/<account_id>/test-login", methods=["POST"])
def api_test_account_login(account_id):
    try:
        account = accounts.get(account_id)
    except accounts.AccountError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    client = MeroShareClient(credentials=account)
    try:
        if client.login():
            try:
                own = client.get_own_details()
            finally:
                client.logout()
            # Return the rich subset of own-details the GUI can display.
            # Other fields (clientCode, contact, etc.) are also available
            # but not displayed to keep the row compact.
            return jsonify({
                "success": True,
                "name": own.get("name", "?"),
                "demat": own.get("demat", "?"),
                "boid": own.get("boid"),
                "email": own.get("email"),
                "branchCode": own.get("clientCode"),
                "expiredDate": own.get("expiredDate"),
                "passwordExpiryDate": own.get("passwordExpiryDate"),
            })
        client.session.close()
        return jsonify({"success": False, "error": "Invalid credentials"})
    except Exception as e:
        # _safe_exc scrubs password/pin/crn= patterns and truncates;
        # without it a requests.HTTPError that echoes the submitted
        # login payload (clientId/username/password JSON) lands in the
        # browser-visible error toast.
        client.session.close()
        return jsonify({"success": False, "error": _safe_exc(e)})


def _tail_lines(path: Path, n: int, *, chunk: int = 8192) -> list[str]:
    """Return the last `n` lines of `path` without loading the whole file.

    Reads from the end in 8KB chunks until we have enough newlines -
    bounded memory regardless of log size, important because the
    Logs-tab polls this every 10s.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            offset = file_size
            buf = b""
            while offset > 0 and buf.count(b"\n") <= n:
                read_size = min(chunk, offset)
                offset -= read_size
                f.seek(offset)
                buf = f.read(read_size) + buf
            decoded = buf.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return decoded[-n:]


@app.route("/api/logs")
def api_logs():
    """Return the last N log lines (default 200; tail by query string)."""
    if not LOG_FILE.exists():
        return jsonify([])
    try:
        n = max(1, min(int(request.args.get("tail", 200)), 2000))
    except (TypeError, ValueError):
        n = 200
    return jsonify(_tail_lines(LOG_FILE, n))


@app.route("/api/failures", methods=["GET"])
def api_failures():
    """Recent FAILED apply attempts, parsed from the rotating log.

    Greps the tail for `-> FAILED:` lines and returns the per-issue
    most-recent failure. Useful when a single issue keeps failing
    across cycles and the user wants to know without trawling logs.
    Best-effort. Log parsing is fragile by design (we'd rather miss
    an entry than dump uncaught exceptions to a status endpoint).
    """
    if not LOG_FILE.exists():
        return jsonify([])
    try:
        lines = _tail_lines(LOG_FILE, 1500)
    except Exception:
        lines = []
    # Lines look like:
    #   2026-05-04 02:30:00,335 [ERROR]     -> FAILED: Could not confirm result - check MeroShare manually
    # preceded by:
    #   2026-05-04 02:29:09,045 [INFO]     -> Applying for Yambaling Hydropower  Limited on Mine ...
    import re as _re
    fail_re = _re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*FAILED:\s*(.+?)$"
    )
    apply_re = _re.compile(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.*Applying for (.+?) on (.+?) \("
    )
    last_apply: dict = {"company": None, "account": None}
    by_key: dict = {}
    for line in lines:
        m_apply = apply_re.match(line)
        if m_apply:
            last_apply = {"company": m_apply.group(1).strip(), "account": m_apply.group(2).strip()}
            continue
        m_fail = fail_re.match(line)
        if m_fail and last_apply["company"]:
            key = f"{last_apply['account']}::{last_apply['company']}"
            by_key[key] = {
                "timestamp": m_fail.group(1),
                "account": last_apply["account"],
                "company": last_apply["company"],
                "error": m_fail.group(2).strip(),
            }
    # Sort newest first.
    out = sorted(by_key.values(), key=lambda r: r["timestamp"], reverse=True)
    return jsonify(out)


@app.route("/api/factory-reset", methods=["POST"])
def api_factory_reset():
    """Wipe accounts + applied state + config.

    Provided so a user can hand off the machine without leaking
    credentials, or recover from a bad state without manually
    `rm`-ing files. Confirmation must come from the client (the GUI
    requires a typed confirmation in the modal). Refuses while a
    background task is running so an in-flight apply doesn't get
    silently cancelled.
    """
    with _bg_lock:
        if _bg_status.get("running"):
            return jsonify({"error": "A background task is running. Wait."}), 409
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "WIPE":
        return jsonify({
            "error": "factory reset requires {\"confirm\": \"WIPE\"} in body",
        }), 400
    # Purge keystore entries for every account BEFORE removing the
    # accounts.json file. Without this, the OS keystore would keep
    # the user's password/CRN/PIN long after factory-reset claimed
    # to "wipe credentials" — the worst kind of silent leak.
    # Also purge the file-encryption key in the META namespace so
    # nothing related to this app remains in Keychain Access after
    # the user has clicked "factory reset" and walked away.
    try:
        for rec in accounts.load():
            secrets_store.delete_all_for(rec["id"], accounts.SENSITIVE_KEYS)
        secrets_store.delete(
            secrets_store.META_ACCOUNT,
            secrets_store.META_FILE_KEY,
        )
    except Exception as e:
        logger.warning("Could not purge keystore on factory reset: %s", _safe_exc(e))
    removed = []
    paths = [
        accounts.ACCOUNTS_FILE, accounts.APPLIED_FILE,
        accounts.APPLIED_LOCK_FILE, CONFIG_FILE,
        accounts.ALLOTMENT_STATE_FILE,
        meroshare_client._CAPITAL_CACHE_FILE,
        # An orphaned shutdown sentinel from a previous Stop-everything
        # would make the next menu bar launch immediately quit. Wipe
        # it as part of the reset.
        accounts.STATE_DIR / ".shutdown-requested",
    ]
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
            removed.append(str(path))
        except OSError as e:
            logger.warning("Could not remove %s: %s", path, _safe_exc(e))
    # Bust in-memory caches so the next login doesn't reuse the DP
    # list / report data we just told the user was wiped.
    with _report_lock:
        _report_cache.clear()
    with _applicable_lock:
        _applicable_cache.clear()
    meroshare_client._capital_cache["data"] = None
    meroshare_client._capital_cache["fetched_at"] = 0.0
    return jsonify({"status": "reset", "removed": removed})


@app.route("/api/applied-issues", methods=["GET"])
def api_get_applied_issues():
    """Return the local `.applied_issues.json` state.

    Useful for the GUI's history tab when MeroShare's report is stale,
    and the inverse of the DELETE below.
    """
    return jsonify(accounts.load_applied())


@app.route("/api/applied-issues/<account_id>/<issue_id>", methods=["DELETE"])
def api_delete_applied_issue(account_id, issue_id):
    """Forget that we applied for `issue_id` under `account_id`.

    Lets the user fix the local cache when MeroShare's records and our
    cache disagree (e.g. the user got a refund, or we marked something
    as applied incorrectly). Uses the cross-process file lock so a
    concurrent scheduler write can't clobber the deletion.
    """
    def _mutator(state: dict) -> None:
        bucket = state.get(account_id)
        if isinstance(bucket, dict):
            bucket.pop(str(issue_id), None)

    accounts.update_applied(_mutator)
    return jsonify({"status": "deleted", "account_id": account_id, "issue_id": issue_id})


@app.route("/api/applied-issues/<account_id>", methods=["DELETE"])
def api_delete_applied_for_account(account_id):
    """Forget every locally-cached "already applied" entry for one account.

    Useful after a user's MeroShare records were wiped by a broker
    side issue, or to recover an orphaned bucket left behind by a
    deleted account.
    """
    removed = {"count": 0}

    def _mutator(state: dict) -> None:
        bucket = state.pop(account_id, None)
        if isinstance(bucket, dict):
            removed["count"] = len(bucket)

    accounts.update_applied(_mutator)
    return jsonify({"status": "deleted", "account_id": account_id, "removed": removed["count"]})


@app.route("/api/scheduler", methods=["GET"])
def api_scheduler_status():
    try:
        return jsonify(scheduler.status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    body = request.get_json(silent=True) or {}
    interval = body.get("interval_hours", 6)
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        return jsonify({"error": "interval_hours must be an integer"}), 400
    # Defensive clamp at the boundary even though scheduler.start also
    # validates. An HTML5 input lets users paste arbitrary numbers, so
    # we shouldn't rely on the GUI to enforce the range.
    if interval < 1 or interval > 24:
        return jsonify({"error": "interval_hours must be between 1 and 24"}), 400
    try:
        return jsonify(scheduler.start(interval))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except scheduler.SchedulerError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    try:
        return jsonify(scheduler.stop())
    except scheduler.SchedulerError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/run-check", methods=["POST"])
def api_run_check():
    """Trigger a one-shot check across all accounts in a background thread."""
    if not accounts.load():
        return jsonify({"error": "No accounts configured"}), 400

    config = load_config()
    if not _claim_bg("Running check across all accounts..."):
        return jsonify({"error": "Another task is running"}), 409

    def do_check():
        try:
            from auto_apply import check_and_apply
            # check_and_apply writes into a local results dict; we copy
            # into _bg_status['results'] under the lock so api_apply_status
            # never sees a torn snapshot mid-write.
            local_results: dict = {}
            applied_list = check_and_apply(
                config, dry_run=False, results=local_results,
            )
            with _bg_lock:
                _bg_status["results"] = local_results
            n = len(applied_list)
            _bg_set(message=(
                f"Check complete. Applied for {n} issue(s)" if n
                else "Check complete. No new issues"
            ))
        except Exception as e:
            logger.exception("Run-check task crashed")
            _bg_set(message=f"Check failed: {e}")
        finally:
            _bg_set(running=False)

    threading.Thread(target=do_check, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/backup")
def api_backup():
    """Bundle accounts + applied state into a single JSON download.

    The backup is plaintext by design — it's the user's escape hatch
    for migrating to a new machine, where the keystore is empty. The
    user is responsible for storing the backup somewhere safe
    (encrypted disk, password manager).

    `accounts.load()` already resolves secrets from the keystore, so
    the exported records contain plaintext password/CRN/PIN. We strip
    `_secrets_in_store` from the export because the destination's
    keystore won't yet have them; the restore route on the new machine
    will migrate them in fresh.
    """
    exported_accounts = []
    for rec in accounts.load():
        clean = {k: v for k, v in rec.items() if k != accounts._SECRETS_FLAG}
        exported_accounts.append(clean)
    payload = {
        "schema": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "accounts": exported_accounts,
        "applied": accounts.load_applied(),
    }
    body = json.dumps(payload, indent=2)
    fname = f"meroshare-backup-{int(time.time())}.json"
    from flask import Response
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.route("/api/restore", methods=["POST"])
def api_restore():
    """Replace accounts + applied state from a backup payload.

    Refuses if any background task is running (could clobber an in-flight
    apply's state write). Validates schema before touching state.
    """
    with _bg_lock:
        if _bg_status.get("running"):
            return jsonify({"error": "A background task is running. Wait."}), 409
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Backup body must be a JSON object"}), 400
    schema = body.get("schema")
    # Accept any backup whose schema we know how to migrate. Today
    # there's only one schema (1); when we bump to 2, add a migration
    # branch here and bump SUPPORTED_SCHEMAS rather than hard-failing.
    SUPPORTED_SCHEMAS = (1,)
    if schema not in SUPPORTED_SCHEMAS:
        return jsonify({
            "error": f"backup schema={schema} is not supported by this build "
                     f"(supported: {SUPPORTED_SCHEMAS}). "
                     "Upgrade the app or hand-edit the backup to match."
        }), 400
    accts = body.get("accounts")
    applied = body.get("applied")
    if not isinstance(accts, list) or not isinstance(applied, dict):
        return jsonify({"error": "Backup is missing accounts or applied state"}), 400
    # Run each backup record through the same validator add() uses so a
    # hand-edited or schema-skewed backup can't write empty-credentialed
    # accounts that would crash silently on the next scheduler run.
    # We keep the original record (id, optional fields, etc.) but require
    # it to *also* satisfy the validator — so we stay forward-compatible
    # with future fields without dropping them on restore.
    for idx, rec in enumerate(accts):
        try:
            accounts.validate_record(rec)
        except accounts.AccountError as e:
            return jsonify({
                "error": f"Backup record #{idx + 1} failed validation: {e}",
            }), 400
    # Reject duplicate (dp_id, username) and duplicate names within the
    # backup itself. save_all doesn't enforce these (they're add()'s job)
    # so an attacker-supplied backup could otherwise inject two records
    # for the same MeroShare login or two with conflicting slugs.
    seen_pairs: set = set()
    seen_names: set = set()
    for rec in accts:
        pair = (rec.get("dp_id", ""), rec.get("username", ""))
        if pair in seen_pairs:
            return jsonify({
                "error": f"Backup contains two records for DP {pair[0]} + BOID {pair[1]}",
            }), 400
        seen_pairs.add(pair)
        nm = (rec.get("name") or "").strip().lower()
        if nm in seen_names:
            return jsonify({
                "error": f"Backup contains two accounts named '{rec.get('name')}'",
            }), 400
        seen_names.add(nm)
    try:
        # Strip _secrets_in_store from incoming records: the source
        # machine's keystore was the source of truth, but on this
        # destination, the keystore is empty. By landing the records as
        # "legacy plaintext" first, then explicitly migrating to the
        # local keystore, the secrets end up in the right place AND the
        # destination's accounts.json never persists them in plaintext
        # (the migration call clears them before the next save).
        for r in accts:
            r.pop(accounts._SECRETS_FLAG, None)
        accounts.save_all(accts)
        accounts.save_applied(applied)
        # Trigger keystore migration so the just-restored plaintext
        # secrets land in the OS keystore immediately, not on the
        # next load(). Without this, accounts.json would contain
        # plaintext credentials until something happened to call load().
        accounts.load()  # side-effect: runs the plaintext-→-keystore migration
    except accounts.AccountError as e:
        # save_all refuses to clobber a malformed accounts.json. Surface
        # the helpful message instead of generic 500.
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"Could not write backup: {e}"}), 500
    # Bust caches: the restored accounts may have different
    # eligibility than the ones we cached for the previous set.
    with _report_lock:
        _report_cache.clear()
    with _applicable_lock:
        _applicable_cache.clear()
    return jsonify({"status": "restored", "accounts": len(accts)})


@app.route("/api/version")
def api_version():
    """Boot timestamp used by the live-reload poller to detect server
    restarts. Returning anything else here would expose more than we
    need to a localhost-only endpoint."""
    return jsonify({"boot_ts": APP_BOOT_TS})


@app.route("/api/health")
def api_health():
    """Liveness + readiness probe.

    Reports whether the process can read its core state files. Used
    by external monitors and by the GUI's "is the server actually
    healthy?" check. Returns 200 with details when everything works,
    503 when a critical state file (accounts.json) is unreadable.
    """
    checks = {}
    overall = True
    # Accounts loadable?
    try:
        accs = accounts.load()
        checks["accounts"] = {"ok": True, "count": len(accs)}
    except Exception as e:
        checks["accounts"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        overall = False
    # Applied-issues loadable?
    try:
        applied = accounts.load_applied()
        n_entries = sum(len(v or {}) for v in applied.values())
        checks["applied_issues"] = {"ok": True, "entries": n_entries}
    except Exception as e:
        checks["applied_issues"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        overall = False
    # Scheduler reachable?
    try:
        sched = scheduler.status()
        checks["scheduler"] = {
            "ok": True,
            "enabled": bool(sched.get("enabled")),
            "interval_hours": sched.get("interval_hours"),
        }
    except Exception as e:
        checks["scheduler"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # Scheduler issues don't fail the health check. The GUI is
        # still useful without launchd.

    payload = {
        "status": "ok" if overall else "degraded",
        "boot_ts": APP_BOOT_TS,
        "uptime_seconds": int(time.time() - APP_BOOT_TS),
        "checks": checks,
    }
    return jsonify(payload), 200 if overall else 503


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Stop the GUI server *and* the background scheduler.

    "Power off" semantics: bring the whole tool down. The launchd
    scheduler is independent of the Flask process, so we explicitly
    unload it before exiting; otherwise the user clicks Power expecting
    a clean shutdown and the scheduler keeps firing IPO checks.

    Refuses (409) while a foreground apply/check is running so we don't
    tear down mid-submission. The actual signal is sent from a short-
    delay thread so the HTTP response can flush before the process dies.
    """
    with _bg_lock:
        if _bg_status.get("running"):
            return jsonify({
                "error": "A background task is running. Wait for it to finish."
            }), 409

    # Stop the scheduler synchronously so a real error (launchctl
    # unload failed for perms, etc.) makes it into the response -
    # otherwise "Power off" lies, the GUI exits, and the launchd
    # job keeps firing checks.
    scheduler_warning = None
    try:
        scheduler.stop()
    except Exception as e:
        scheduler_warning = str(e)
        logger.warning("Could not stop scheduler during shutdown: %s", e)

    # Drop a sentinel that the macOS menu bar polls for. The sentinel
    # is the fallback path: a menu bar instance with a stale signal
    # handler (older build, weird launchd-spawn) still picks it up on
    # its next 30s tick. The fast path is the SIGTERM below — we want
    # the menu bar to die in lockstep with Flask, not 30 seconds later.
    try:
        sentinel = accounts.STATE_DIR / ".shutdown-requested"
        sentinel.write_text(str(int(time.time())))
    except OSError as e:
        logger.warning("Could not write shutdown sentinel: %s", e)

    # Direct-signal the menu bar process so the user's "Stop everything"
    # click feels instant. Without this, Flask would die immediately
    # but the menu bar status item would linger up to 30 seconds until
    # its next sentinel-check tick. pgrep is cheap and the SIGTERM is
    # idempotent — multiple "Stop everything" clicks (or this running
    # on a system with no menu bar at all, e.g. ./run.sh dev mode) are
    # harmless.
    def _signal_menubar() -> None:
        try:
            r = subprocess.run(
                ["pgrep", "-f", "menubar.py"],
                check=False, capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("pgrep menubar.py failed: %s", e)
            return
        for pid_str in r.stdout.split():
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if pid == os.getpid():
                continue  # never SIGTERM ourselves through this path
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info("Signalled menu bar (pid=%s) SIGTERM", pid)
            except (ProcessLookupError, PermissionError) as e:
                logger.debug("Could not signal menu bar pid=%s: %s", pid, e)
    threading.Thread(target=_signal_menubar, daemon=True).start()

    def _shutdown():
        time.sleep(0.3)

        if sys.platform == "win32":
            # os.kill on Windows only handles a couple of signals and
            # CTRL_C_EVENT requires special process group setup. A hard
            # exit is simpler and equivalent for a single-user dev server.
            os._exit(0)
        else:
            # If we're the reloader's child, signaling self alone leaves
            # the parent watcher alive. It would respawn a new child.
            # WERKZEUG_RUN_MAIN is only set in the reloader child.
            if os.environ.get("WERKZEUG_RUN_MAIN"):
                ppid = os.getppid()
                # Cheap protection against a parent-PID race (the
                # parent could have already exited and a recycled PID
                # could now belong to another process). Verify ppid is
                # still our parent before signaling. Kill(0) is a
                # zero-cost permission check.
                try:
                    os.kill(ppid, 0)
                    if ppid != 1:
                        os.kill(ppid, signal.SIGINT)
                except OSError:
                    pass
            # SIGINT mimics Ctrl+C; werkzeug's dev server handles it as
            # a clean shutdown via KeyboardInterrupt.
            os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=_shutdown, daemon=True).start()
    payload = {"status": "shutting down"}
    if scheduler_warning:
        payload["scheduler_warning"] = scheduler_warning
    return jsonify(payload)


@app.route("/settings")
def settings():
    return render_template("index.html")


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import glob
    # Allow PORT env override (handy when 5050 is taken on the user's
    # machine, e.g. by another local service). Falls back to 5050 to
    # match run.sh / run.bat / README.
    try:
        port = int(os.environ.get("MEROSHARE_PORT") or 5050)
    except ValueError:
        port = 5050
    if not 1024 <= port <= 65535:
        port = 5050
    print()
    print("  MeroShare Auto-Apply")
    print(f"  Open http://localhost:{port} in your browser")
    print("  Press Ctrl+C to stop")
    print()
    # Only the parent reloader process opens the browser, not every
    # respawned child; otherwise saving a file pops a new tab each time.
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        webbrowser.open(f"http://localhost:{port}")
    # Live reload of templates and static files: Werkzeug's stat reloader
    # only watches imported Python modules by default. extra_files makes
    # it also restart on HTML/CSS/JS changes so the browser picks them
    # up via the version poller. debug=False keeps the dev error pages
    # off (don't want stack traces leaking local paths).
    extra_files = (
        glob.glob("templates/**/*.html", recursive=True)
        + glob.glob("static/**/*.css", recursive=True)
        + glob.glob("static/**/*.js", recursive=True)
    )
    # Static files: Cache-Control max-age 0 so browser never caches them
    # between manual refreshes. Combined with the boot_ts query string,
    # this makes static changes appear without ⌘⇧R.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.run(
        host="127.0.0.1", port=port, debug=False,
        use_reloader=True, extra_files=extra_files,
    )
