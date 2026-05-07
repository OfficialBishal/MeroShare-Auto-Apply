"""
MeroShare API Client
Handles authentication, fetching issues, and checking eligibility.
"""

import json
import logging
import os
import random
import time

import requests

import accounts

logger = logging.getLogger("meroshare")

# backend.cdsc.com.np works without WAF restrictions
# (webbackend.cdsc.com.np has WAF that blocks non-browser requests)
API_BASE = "https://backend.cdsc.com.np/api"
MEROSHARE = f"{API_BASE}/meroShare"

# (connect, read) seconds. MeroShare can pause for 30+ seconds under
# load; 45s read covers that without letting a wedged endpoint hang
# the daemon thread indefinitely.
HTTP_TIMEOUT = (10, 45)


def _pace(min_s=2, max_s=7):
    """Random delay between requests to avoid rate limiting."""
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


# Disk-backed cache for the master capital (DP) list. The list is the
# same for every user and changes only when MeroShare adds or removes
# a DP (rare). Persisting to disk means the launchd-spawned process
# also gets the cache benefit between runs. Without it, every cycle
# refetches /capital/ which doubles the pre-auth HTTP call count and
# adds WAF pressure.
_CAPITAL_TTL_S = 24 * 3600
_capital_cache: dict = {"data": None, "fetched_at": 0.0}
# Disk-backed cache file. accounts.STATE_DIR resolves to
# ~/Library/Application Support/... in the bundled .app build, or
# the project root for dev installs.
_CAPITAL_CACHE_FILE = accounts.STATE_DIR / ".capital_cache.json"


def _load_capital_cache_from_disk() -> None:
    """Hydrate the in-memory cache from disk on first use."""
    if _capital_cache.get("data") is not None:
        return
    if not _CAPITAL_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(_CAPITAL_CACHE_FILE.read_text())
        if isinstance(raw, dict) and isinstance(raw.get("data"), list):
            _capital_cache["data"] = raw["data"]
            _capital_cache["fetched_at"] = float(raw.get("fetched_at", 0))
    except (OSError, ValueError, TypeError) as e:
        logger.debug("Could not hydrate capital cache: %s", e)


def _save_capital_cache_to_disk() -> None:
    """Persist the cache so subsequent process invocations skip the fetch.

    Atomic write with fsync before rename so a power loss / kill-9
    mid-write can't leave a torn JSON file that would fail to load on
    next boot. Mode 0o600 to match the rest of the project's
    file-perm hygiene.
    """
    try:
        tmp = _CAPITAL_CACHE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps({
                "data": _capital_cache.get("data"),
                "fetched_at": _capital_cache.get("fetched_at", 0),
            }))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # not all filesystems support fsync
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, _CAPITAL_CACHE_FILE)
    except OSError as e:
        logger.debug("Could not persist capital cache: %s", e)


def _browser_headers():
    """Standard browser request headers."""
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "Origin": "https://meroshare.cdsc.com.np",
        "Pragma": "no-cache",
        "Referer": "https://meroshare.cdsc.com.np/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }


class MeroShareClient:
    def __init__(self, credentials: dict):
        # Multi-account refactor made `credentials` mandatory: every live
        # caller passes one. Refusing here means a future caller that
        # forgets to pass credentials fails loudly instead of silently
        # using stale env vars from a long-deleted .env.
        if not credentials:
            raise TypeError("MeroShareClient requires a credentials dict")
        self.dp_id = credentials.get("dp_id")
        self.username = credentials.get("username")
        self.password = credentials.get("password")
        self.crn = credentials.get("crn")
        self.pin = credentials.get("pin")
        self.token = None
        self.last_login_error = None  # set by login() on failure
        self.session = requests.Session()
        self.session.headers.update(_browser_headers())
        self._client_id = None
        self._own_details = None
        self._banks = None

    # ── HTTP wrapper ────────────────────────────────────────────────

    def _request(self, method: str, url: str, *, retries: int = 2, **kwargs):
        """Issue an HTTP request with timeout and bounded 5xx retry.

        Every direct `session.get/post` call must go through this so we
        never send a request without a timeout (a wedged endpoint used
        to hang the launchd run forever). Retries 5xx responses with
        jittered backoff; never retries 4xx (those are auth/validation
        errors we want to surface immediately).

        SAFETY NOTE on POST retries: this client never POSTs anything
        non-idempotent. The POST endpoints we use (auth/, search/,
        validate/, capital list) are read-only or cleanly idempotent.
        We explicitly do NOT POST to the apply endpoint
        from this client (that's `browser_apply.py` via Playwright,
        which has its own no-retry-after-submit policy in
        `apply_with_retry`). Any future POST endpoint added here that
        isn't safe to retry must call `_request(..., retries=0)`.
        """
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        last_exc = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt >= retries:
                    raise
                backoff = 1.5 ** attempt + random.uniform(0, 0.8)
                logger.warning(
                    "Network error on %s %s (%s); retrying in %.1fs (%d/%d)",
                    method, url, e, backoff, attempt + 1, retries,
                )
                time.sleep(backoff)
                continue
            if 500 <= resp.status_code < 600 and attempt < retries:
                backoff = 1.5 ** attempt + random.uniform(0, 0.8)
                logger.warning(
                    "HTTP %s on %s %s; retrying in %.1fs (%d/%d)",
                    resp.status_code, method, url, backoff, attempt + 1, retries,
                )
                time.sleep(backoff)
                continue
            return resp
        # Loop exits via the `return resp` above on the final attempt
        # for both 5xx and success cases. Reaching here means we hit
        # a network exception on every attempt. The last one re-raised
        # via the `raise` in the except clause above. The fallback raise
        # is here only to satisfy the type-checker.
        raise last_exc or RuntimeError("unreachable")  # pragma: no cover

    # ── Authentication ──────────────────────────────────────────────

    def _resolve_client_id(self, *, _allow_refetch: bool = True):
        """Look up the numeric client ID for our DP from the capital list.

        Reuses the cache (in-memory or disk) when fresh. Saves an HTTP
        call on every login and reduces pressure on MeroShare's WAF,
        including across separate launchd-spawned processes.

        `_allow_refetch` exists so the stale-DP-recovery path below
        can recurse exactly once with the cache busted, without risk
        of an infinite loop if the freshly-fetched list also lacks
        the DP.
        """
        if self._client_id:
            return self._client_id

        _load_capital_cache_from_disk()
        now = time.time()
        cached = _capital_cache.get("data")
        from_cache = bool(
            cached and (now - _capital_cache.get("fetched_at", 0)) < _CAPITAL_TTL_S
        )
        if from_cache:
            capitals = cached
        else:
            resp = self._request("GET", f"{MEROSHARE}/capital/")
            resp.raise_for_status()
            capitals = resp.json()
            _capital_cache["data"] = capitals
            _capital_cache["fetched_at"] = now
            _save_capital_cache_to_disk()

        for cap in capitals:
            cap_code = str(cap.get("code", ""))
            cap_id = str(cap.get("id", ""))
            cap_name = cap.get("name", "")
            if self.dp_id in (cap_code, cap_id):
                self._client_id = cap["id"]
                logger.info("Resolved DP '%s' -> clientId %s (%s)", self.dp_id, self._client_id, cap_name)
                return self._client_id

        # If the cache is the source of the miss, bust it and retry
        # exactly once with `_allow_refetch=False`. Without the flag a
        # DP that genuinely doesn't exist would loop forever once the
        # refetched list also lacks it.
        if from_cache and _allow_refetch:
            logger.warning(
                "DP '%s' not in cached capital list (cache age %.0fs); "
                "busting cache and retrying once.",
                self.dp_id, now - _capital_cache.get("fetched_at", 0),
            )
            _capital_cache["data"] = None
            _capital_cache["fetched_at"] = 0
            try:
                _CAPITAL_CACHE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            return self._resolve_client_id(_allow_refetch=False)

        raise ValueError(
            f"Could not find DP with code/id '{self.dp_id}' in capital list. "
            "Check the DP ID on the account record. "
            f"(Cache file: {_CAPITAL_CACHE_FILE}. Delete it to force a refetch.)"
        )

    def login(self):
        """Authenticate and store the authorization token.

        Distinguishes between auth failures (bad creds) and throttling
        responses (429 / 403) so callers and the GUI can show a useful
        message. Sets `self.last_login_error` to a short tag the caller
        can branch on.
        """
        client_id = self._resolve_client_id()
        _pace(1, 3)

        payload = {
            "clientId": client_id,
            "username": self.username,
            "password": self.password,
        }

        # Don't retry login. A 5xx between request and response could
        # have established a server-side session before failing the
        # connection. Retrying would create a second one and add WAF
        # pressure for no benefit. The user-facing failure path
        # already surfaces "wait and retry" cleanly via the GUI.
        resp = self._request("POST", f"{MEROSHARE}/auth/", json=payload, retries=0)

        if resp.status_code == 200:
            self.token = resp.headers.get("Authorization")
            self.session.headers["Authorization"] = self.token
            self.last_login_error = None
            logger.info("Login successful.")
            return True

        # Don't log resp.text on 4xx. Auth endpoints can echo the
        # submitted payload (which contains the password) in
        # validation responses. For 5xx and unknown statuses we DO
        # want a snippet of the body to debug new WAF responses, but
        # truncate aggressively and refuse to print anything that
        # looks like it contains the password we sent.
        if resp.status_code in (429, 403):
            self.last_login_error = "rate_limited"
            logger.warning(
                "Login throttled by MeroShare (HTTP %s). Wait a few minutes "
                "before retrying. The WAF blocks aggressive logins.",
                resp.status_code,
            )
        elif resp.status_code in (401, 400):
            self.last_login_error = "bad_credentials"
            logger.error("Login failed: HTTP %s. Credentials likely wrong "
                         "or password expired.", resp.status_code)
        else:
            self.last_login_error = "unknown"
            snippet = (resp.text or "")[:200].replace("\n", " ")
            if self.password and self.password in snippet:
                snippet = "[response body redacted: contained submitted password]"
            logger.error("Login failed: HTTP %s. Body: %s",
                         resp.status_code, snippet)
        return False

    def logout(self):
        """Clear the session token (and notify the server when possible).

        MeroShare keeps server-side sessions alive for a while after the
        client drops the token; without an explicit logout call, frequent
        re-logins (every scheduled cycle) accumulate dangling sessions
        and contribute to WAF throttling. Best-effort: a failed logout
        request must not propagate, but we narrow to RequestException so
        a bug-shaped exception (AssertionError, ValueError) still
        surfaces in development.
        """
        if self.token:
            try:
                self._request("POST", f"{MEROSHARE}/auth/logout/", retries=0)
            except requests.RequestException as e:
                logger.debug("Server-side logout failed (non-fatal): %s", e)
        self.token = None
        self.session.headers.pop("Authorization", None)
        self._own_details = None
        self._banks = None
        logger.info("Logged out.")

    # ── Account Details ─────────────────────────────────────────────

    def get_own_details(self):
        """Fetch the logged-in user's account details (BOID, name, etc.)."""
        if self._own_details:
            return self._own_details
        _pace(1, 3)
        resp = self._request("GET", f"{MEROSHARE}/ownDetail/")
        resp.raise_for_status()
        self._own_details = resp.json()
        return self._own_details

    def get_bank_list(self):
        """Fetch the user's linked bank list (bank name + id)."""
        if self._banks:
            return self._banks
        _pace(1, 2)
        resp = self._request("GET", f"{MEROSHARE}/bank/")
        resp.raise_for_status()
        self._banks = resp.json()
        return self._banks

    def get_bank_accounts(self, bank_id):
        """Get bank account details (account number, branch) for a bank."""
        _pace(1, 2)
        resp = self._request("GET", f"{MEROSHARE}/bank/{bank_id}")
        resp.raise_for_status()
        return resp.json()

    # ── Issues ──────────────────────────────────────────────────────

    # Maximum pages we'll walk per paginated endpoint. With a 200-row
    # page size this caps a runaway endpoint at 2000 rows. Far above
    # any realistic count of open MeroShare issues, but still bounded
    # so a misbehaving server can't loop us forever.
    _MAX_PAGES = 10
    _PAGE_SIZE = 200

    def _paged_post(self, url: str, payload_template: dict) -> list:
        """POST a search payload and walk pages until exhausted.

        The MeroShare search endpoints take `page` (1-indexed) and
        `size`. We stop when a page returns fewer rows than `size`, when
        the totalNoOfRows is satisfied, or at `_MAX_PAGES` (with a
        warning so the user knows the tail was clipped).
        """
        results: list = []
        for page in range(1, self._MAX_PAGES + 1):
            payload = dict(payload_template, page=page, size=self._PAGE_SIZE)
            resp = self._request("POST", url, json=payload)
            resp.raise_for_status()
            data = resp.json() or {}
            chunk = data.get("object", []) or []
            results.extend(chunk)
            if len(chunk) < self._PAGE_SIZE:
                return results
            total = data.get("totalNoOfRows")
            if isinstance(total, int) and len(results) >= total:
                return results
            _pace(1, 2)  # be polite between pages
        logger.warning(
            "Pagination cap (%d pages × %d) hit on %s; tail may be truncated",
            self._MAX_PAGES, self._PAGE_SIZE, url,
        )
        return results

    def get_current_issues(self):
        """Fetch all currently open share issues (across pages)."""
        _pace(2, 5)
        payload = {
            "filterFieldParams": [
                {"key": "companyIssue.companyISIN.script", "alias": "Scrip"},
                {"key": "companyIssue.companyISIN.company.name", "alias": "Company Name"},
                {"key": "companyIssue.assignedToClient.name", "value": "", "alias": "Issue Manager"},
            ],
            "searchRoleViewConstants": "VIEW_OPEN_SHARE",
            "filterDateParams": [
                {"key": "minIssueOpenDate", "condition": "", "alias": "", "value": ""},
                {"key": "maxIssueOpenDate", "condition": "", "alias": "", "value": ""},
            ],
        }
        return self._paged_post(f"{MEROSHARE}/companyShare/currentIssue", payload)

    def get_applicable_issues(self):
        """Fetch issues the user can apply for (across pages)."""
        _pace(2, 5)
        payload = {
            "filterFieldParams": [
                {"key": "companyShare.companyIssue.companyISIN.script", "alias": "Scrip"},
                {"key": "companyShare.companyIssue.companyISIN.company.name", "alias": "Company Name"},
                {"key": "companyShare.companyIssue.assignedToClient.name", "value": "", "alias": "Issue Manager"},
            ],
            "searchRoleViewConstants": "VIEW_APPLICABLE_SHARE",
            "filterDateParams": [
                {"key": "minIssueOpenDate", "condition": "", "alias": "", "value": ""},
                {"key": "maxIssueOpenDate", "condition": "", "alias": "", "value": ""},
            ],
        }
        return self._paged_post(f"{MEROSHARE}/companyShare/applicableIssue/", payload)

    def get_issue_details(self, company_share_id):
        """Get detailed info about a specific issue."""
        _pace(1, 3)
        resp = self._request("GET", f"{MEROSHARE}/active/{company_share_id}")
        resp.raise_for_status()
        return resp.json()

    # ── Right Share Specific ────────────────────────────────────────

    def get_share_criteria(self, demat, company_share_id):
        """Get right share eligibility for a DEMAT account.
        Returns the shareCriteria including reservedQuantity and shareCriteriaId.
        Endpoint: GET /api/shareCriteria/boid/{demat}/{companyShareId}
        """
        _pace(1, 3)
        resp = self._request("GET", f"{API_BASE}/shareCriteria/boid/{demat}/{company_share_id}")
        resp.raise_for_status()
        return resp.json()

    def validate_boid_for_right_share(self, company_share_id, demat):
        """Validate BOID eligibility for right share.
        Endpoint: POST /api/shareCriteria/validate/{companyShareId}
        """
        _pace(1, 2)
        payload = {"boid": demat}
        resp = self._request("POST", f"{API_BASE}/shareCriteria/validate/{company_share_id}", json=payload)
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}

    def check_customer_type_can_apply(self, company_share_id, demat):
        """Check if the customer type is allowed to apply.
        Endpoint: GET /api/meroShare/applicantForm/customerType/{companyShareId}/{demat}
        """
        _pace(1, 2)
        resp = self._request("GET", f"{MEROSHARE}/applicantForm/customerType/{company_share_id}/{demat}")
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}

    # ── Application Report ──────────────────────────────────────────

    def get_application_report(self):
        """Fetch the current application report (across pages)."""
        _pace(1, 3)
        payload = {
            "filterFieldParams": [
                {"key": "companyShare.companyIssue.companyISIN.script", "alias": "Scrip"},
                {"key": "companyShare.companyIssue.companyISIN.company.name", "alias": "Company Name"},
            ],
            "searchRoleViewConstants": "VIEW_APPLICANT_FORM_COMPLETE",
            "filterDateParams": [
                {"key": "appliedDate", "condition": "", "alias": "", "value": ""},
                {"key": "appliedDate", "condition": "", "alias": "", "value": ""},
            ],
        }
        return self._paged_post(f"{MEROSHARE}/applicantForm/active/search/", payload)

    # ── Classify Shares ─────────────────────────────────────────────

    @staticmethod
    def classify_issue(issue):
        """Determine the type of a share issue based on its properties.

        Classification is driven primarily by `shareTypeName` and
        `shareGroupName`, NOT by `companyName`, because company names can
        contain misleading substrings (e.g. "Bright Future Hydro" must not
        match the right-share rule). Word-boundary regex is used for the
        single-word triggers (right, fpo, bond) so we don't get burned by
        substrings like "copyright" / "fpoc" / "bonded".

        Returns "unknown" for issue types we don't recognize so the
        auto-apply loop can refuse to act rather than silently treating
        a novel category (preferred share, sukuk, etc.) as an ordinary
        IPO.
        """
        import re as _re

        share_type = (issue.get("shareTypeName", "") or "").lower()
        group_name = (issue.get("shareGroupName", "") or "").lower()
        sub_group = (issue.get("subGroup", "") or "").lower()
        reservation = (issue.get("reservationTypeName", "") or "").lower()
        # Type/group fields drive classification. Company name is excluded.
        type_fields = f"{share_type} {group_name} {sub_group} {reservation}"

        if "mutual fund" in type_fields or "scheme" in type_fields:
            return "mutual_fund"
        if "debenture" in type_fields or _re.search(r"\bbond\b", type_fields)\
                or "sukuk" in type_fields:
            return "debenture"
        if _re.search(r"\bright\b", type_fields) or "reserved" in share_type:
            return "right_share"
        if _re.search(r"\bfpo\b", type_fields) or "further public" in type_fields:
            return "fpo"
        if "preference" in type_fields or "preferred" in type_fields:
            return "preferred_share"
        if "ordinary" in type_fields or "ipo" in type_fields\
                or "initial public" in type_fields:
            return "ipo_ordinary"
        # Unknown category. Caller should NOT auto-apply without
        # opt-in, since it could be a new instrument type.
        return "unknown"

    def get_application_detail(self, applicant_form_id):
        """Fetch detailed info for a specific application (kitta, amount, status)."""
        _pace(0.3, 0.8)
        resp = self._request("GET", f"{MEROSHARE}/applicantForm/report/detail/{applicant_form_id}")
        resp.raise_for_status()
        return resp.json()
