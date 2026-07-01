"""
MeroShare Browser-based Apply
Uses Playwright to handle the full login -> form -> PIN -> submit flow.
"""

import logging
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

import accounts

logger = logging.getLogger("meroshare")

MEROSHARE_URL = "https://meroshare.cdsc.com.np"
BROWSER_TIMEOUT_MS = 120_000  # 2 minutes max for entire operation

# Where unconfirmed-apply screenshots land. Same directory the rotating
# log file lives in, but in a `screenshots/` subdir so they don't get
# munged by RotatingFileHandler. Created on demand.
_SCREENSHOT_DIR = accounts.STATE_DIR / "logs" / "screenshots"


def _capture_unconfirmed_screenshot(page, issue_id: int) -> Path | None:
    """Save a full-page screenshot for the operator to review.

    Called from the "Could not confirm result" branch so the user has
    visual evidence of what the browser saw when the apply outcome was
    ambiguous. Returns the path on success, or None when the capture
    itself fails (we never want diagnostics to mask the real return).
    """
    try:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _SCREENSHOT_DIR / f"unconfirmed-{issue_id}-{int(time.time())}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.warning(
            "Unconfirmed apply screenshot saved to %s (compare against MeroShare manually).",
            path,
        )
        return path
    except Exception as e:
        # Diagnostic capture is best-effort; don't let a screenshot
        # error overwrite the real failure message we're about to return.
        logger.debug("Could not save unconfirmed-apply screenshot: %s", e)
        return None


def _pace(min_s=1.0, max_s=3.0):
    time.sleep(random.uniform(min_s, max_s))


# Phrases MeroShare uses (across toasts and inline messages) when the
# user has already submitted an application for the same issue. Matched
# case-insensitively against arbitrary page text, so each must be
# specific enough that an unrelated page can't produce a false positive.
#
# "have applied" alone was here historically but is dangerously short:
# it fires on innocuous descriptive text like "users who have applied
# for this issue earlier" that MeroShare's own help banners use. The
# anchored "have already applied" / "you have applied" phrases below
# pin to first-/second-person constructions used by the actual error
# toasts, not third-person descriptions.
_ALREADY_APPLIED_PHRASES = (
    "already applied",
    "have already applied",
    "you have applied",
    "already submitted",
    "form already exists",
    "duplicate application",
)


def _looks_already_applied(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(p in lowered for p in _ALREADY_APPLIED_PHRASES)


def apply_via_browser(
    company_share_id: int,
    headless: bool = True,
    default_kitta: int = 10,
    credentials: dict | None = None,
    apply_max: bool = False,
    max_amount: int | None = None,
    share_price: float | None = None,
) -> dict:
    """
    Apply for a share issue using browser automation.

    Args:
        company_share_id: The MeroShare issue ID to apply for.
        headless: Run browser without visible window.
        default_kitta: Fallback kitta for IPOs if not pre-filled by MeroShare.
        credentials: Per-account credentials dict. Required.
        apply_max: When True (typically right shares with the
            right_share_apply_max config flag on), read the kitta input's
            max attribute and apply that quantity instead of leaving the
            MeroShare-prefilled minimum.
        max_amount: When set (NPR), caps the application total. Uses
            `share_price` (or 100 NPR if unknown) to convert to a kitta
            cap. Useful as a guardrail on apply_max for accounts with
            very large right-share entitlements.
        share_price: Per-share price in NPR. When omitted, falls back
            to 100 (the typical IPO/right-share price); for premium
            FPOs this assumption massively under-caps, so callers
            should pass the real `sharePerUnit` from the issue details
            whenever possible.

    Returns:
        dict with 'success' (bool) and 'message' (str).
    """
    if not credentials:
        return {"success": False, "message": "credentials required"}
    username = credentials.get("username")
    password = credentials.get("password")
    crn = credentials.get("crn")
    pin = credentials.get("pin")
    dp_id = credentials.get("dp_id")
    preferred_bank = (credentials.get("preferred_bank") or "").strip().lower()
    # Lowercase for the same case-insensitive substring match the bank
    # field uses. Account numbers are typically all-digit, but bank
    # account *labels* shown in MeroShare's dropdown often include the
    # bank name and branch in mixed case.
    preferred_bank_account = (credentials.get("preferred_bank_account") or "").strip().lower()

    if not all([username, password, crn, pin, dp_id]):
        missing = [k for k, v in {
            "username": username, "password": password,
            "crn": crn, "pin": pin, "dp_id": dp_id,
        }.items() if not v]
        return {"success": False, "message": f"Missing credentials: {', '.join(missing)}"}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.set_default_timeout(BROWSER_TIMEOUT_MS)

        try:
            # ── Step 1: Login ───────────────────────────────────────
            logger.info("Navigating to MeroShare login...")
            # Angular SPA. Networkidle is unreliable here because the
            # framework keeps long-poll connections open, so the event
            # may never fire (or fire prematurely). domcontentloaded
            # gets us in the door; the explicit selector waits below
            # handle the rest.
            page.goto(f"{MEROSHARE_URL}/#/login", wait_until="domcontentloaded")
            _pace(2, 4)

            # Open DP dropdown and find matching option by DP ID number
            dp_dropdown = page.locator(".select2-selection__rendered").first
            dp_dropdown.click()
            _pace(0.5, 1)

            # Click the DP option matching the user's DP ID
            dp_option = page.get_by_role("treeitem").filter(has_text=f"({dp_id})")
            if dp_option.count() == 0:
                return {"success": False, "message": f"DP with ID ({dp_id}) not found in dropdown"}
            dp_option.first.click()
            _pace(0.5, 1)

            # Fill credentials
            page.get_by_role("textbox", name="Username").fill(username)
            _pace(0.3, 0.8)
            page.get_by_role("textbox", name="Password").fill(password)
            _pace(0.5, 1)

            # Login
            page.get_by_role("button", name="Login").click()

            try:
                page.wait_for_url("**/dashboard**", timeout=15000)
            except PlaywrightTimeout:
                # Check for error message on the login page
                error_el = page.query_selector(".toast-error, .alert-danger")
                error_text = error_el.inner_text() if error_el else "Login timed out"
                return {"success": False, "message": f"Login failed: {error_text}"}

            logger.info("Login successful.")
            _pace(2, 4)

            # ── Step 2: Navigate to Apply Page ──────────────────────
            logger.info("Navigating to apply page for issue %s...", company_share_id)
            page.goto(
                f"{MEROSHARE_URL}/#/asba/apply/{company_share_id}",
                wait_until="domcontentloaded",
            )
            _pace(2, 4)

            # Check for error toasts (issue might be closed/not available)
            error_el = page.query_selector(".toast-error")
            if error_el:
                toast_text = error_el.inner_text() or ""
                if _looks_already_applied(toast_text):
                    return {
                        "success": True,
                        "already_applied": True,
                        "message": f"Already applied: {toast_text.strip()}",
                    }
                return {"success": False, "message": f"Cannot apply: {toast_text}"}

            # Wait for the form OR a redirect-away. Whichever happens
            # first. Without this race the URL check below would fire
            # before the SPA navigation completed (domcontentloaded
            # returns essentially synchronously on a hashbang route)
            # and a real apply form would be misclassified as
            # "already applied (apply page redirected)".
            try:
                page.wait_for_function(
                    f"() => !location.hash.includes('/apply/{company_share_id}')"
                    " || !!document.querySelector('input, select, button')",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                pass

            # Already-applied page: MeroShare often redirects away from
            # /apply/ or shows the form in read-only mode with an Edit
            # button instead of Apply. Either way we should not refill
            # and resubmit. That's how we end up paging the user with
            # "Application Failed" for forms already in the system.
            if "/apply/" not in page.url:
                # A redirect to the login page means the session dropped — that is
                # NOT an "already applied" success. Only treat other redirects
                # (e.g. back to the ASBA list, MeroShare's already-applied path)
                # as the benign already-applied case.
                if "login" in page.url.lower():
                    return {
                        "success": False,
                        "message": "Session expired (redirected to login); not applied.",
                    }
                return {
                    "success": True,
                    "already_applied": True,
                    "message": "Already applied (apply page redirected)",
                }
            try:
                body_text = page.locator("body").inner_text(timeout=2000) or ""
            except Exception:
                body_text = ""
            if _looks_already_applied(body_text):
                return {
                    "success": True,
                    "already_applied": True,
                    "message": "Already applied (detected on apply page)",
                }

            # Wait for the form
            try:
                page.wait_for_selector("text=BOID", timeout=10000)
            except PlaywrightTimeout:
                return {"success": False, "message": "Apply form did not load"}

            # Safety: confirm the form's BOID matches the credentials we
            # were asked to apply with. A mismatch would mean the login
            # silently authenticated a different account (very rare, but
            # catastrophic — would apply for the wrong person's BOID).
            # We abort rather than continue: the previous "warn and
            # proceed" behaviour was the opposite of the stated intent.
            # If we can't read the form text at all (e.g. selector
            # times out on a slow render) we let the apply proceed —
            # that's not a confirmed mismatch, just a confirmation
            # gap, and BROWSER_TIMEOUT_MS will catch a truly hung page.
            try:
                form_text = page.locator("body").inner_text()
            except Exception:
                form_text = None
            if form_text is not None and username and username not in form_text:
                logger.error(
                    "BOID %s not found in apply form — aborting to "
                    "prevent applying with a mismatched session.",
                    username,
                )
                return {
                    "success": False,
                    "message": (
                        f"BOID {username} not found in apply form — "
                        "session mismatch, aborted before submit."
                    ),
                }

            # ── Step 3: Fill Application Form ───────────────────────
            # `Bank` (exact) avoids matching neighboring labels like
            # "Bank Branch" or "Bank Account Number" that share the
            # substring. If the exact match is ambiguous (multiple
            # selects), Playwright will raise. Far better than
            # silently picking the wrong control.
            bank_select = page.get_by_label("Bank", exact=True)
            bank_options = bank_select.locator("option").all()
            if len(bank_options) < 2:
                return {"success": False, "message": "No bank available in dropdown"}

            # Pick by user preference (substring match, case-insensitive)
            # if set; otherwise fall back to first non-placeholder option.
            # First non-placeholder option by default; preferred_bank
            # below overrides when set.
            chosen_idx = 1
            if preferred_bank:
                matched_label = None
                for i, opt in enumerate(bank_options):
                    if i == 0:
                        continue  # skip placeholder
                    label = (opt.inner_text() or "").strip()
                    if preferred_bank in label.lower():
                        chosen_idx = i
                        matched_label = label
                        break
                if matched_label is None:
                    logger.warning(
                        "preferred_bank '%s' not found among %d options; "
                        "falling back to first available.",
                        credentials.get("preferred_bank"), len(bank_options) - 1,
                    )
                else:
                    logger.info("Selected bank '%s' (index %d)", matched_label, chosen_idx)
            bank_select.select_option(index=chosen_idx)
            _pace(1, 2)

            # Wait for account number dropdown to populate
            try:
                page.wait_for_selector("text=Account Number", timeout=5000)
                _pace(1, 2)
            except PlaywrightTimeout:
                return {"success": False, "message": "Account number dropdown did not load"}

            acct_select = page.get_by_label("Account Number")
            acct_options = acct_select.locator("option").all()
            if len(acct_options) < 2:
                return {"success": False, "message": "No bank account available"}
            # Pick by user preference (substring match) when set, so a
            # user with multiple accounts at the same bank can target
            # the one whose CRN matches. Defaults to the first
            # non-placeholder option otherwise.
            chosen_acct_idx = 1
            if preferred_bank_account:
                matched_acct = None
                for i, opt in enumerate(acct_options):
                    if i == 0:
                        continue  # skip placeholder
                    label = (opt.inner_text() or "").strip()
                    if preferred_bank_account in label.lower():
                        chosen_acct_idx = i
                        matched_acct = label
                        break
                if matched_acct is None:
                    logger.warning(
                        "preferred_bank_account '%s' not found among %d "
                        "options; falling back to first available.",
                        preferred_bank_account, len(acct_options) - 1,
                    )
                else:
                    logger.info("Selected bank account '%s' (index %d)",
                                matched_acct, chosen_acct_idx)
            acct_select.select_option(index=chosen_acct_idx)
            _pace(1, 2)

            # Wait for branch to auto-fill
            try:
                page.wait_for_function(
                    "document.querySelector('input[placeholder=\"Enter Branch Name\"]')?.value?.length > 0",
                    timeout=5000,
                )
            except PlaywrightTimeout:
                logger.warning("Branch did not auto-fill, continuing anyway")

            # Handle Applied Kitta:
            #   apply_max=True  -> read the input's max attribute and use
            #                      that (right shares with the
            #                      right_share_apply_max flag on); fall
            #                      back to the pre-filled value or
            #                      default_kitta if max isn't readable.
            #   apply_max=False -> keep the MeroShare-prefilled value, or
            #                      fill default_kitta when empty/zero
            #                      (typical IPO path).
            kitta_input = page.get_by_role("textbox", name="Applied Kitta")
            kitta_value = kitta_input.input_value()
            # Sanity bound on max. A stray "max=999999" attribute could
            # otherwise produce a multi-million-rupee application form.
            # 100k kitta covers any realistic right-share allotment.
            MAX_KITTA_LIMIT = 100_000
            if apply_max:
                max_attr = kitta_input.get_attribute("max")
                if max_attr and max_attr.isdigit() and 0 < int(max_attr) <= MAX_KITTA_LIMIT:
                    kitta_input.fill(max_attr)
                    logger.info("Filled max eligible kitta: %s", max_attr)
                elif max_attr and max_attr.isdigit() and int(max_attr) > MAX_KITTA_LIMIT:
                    logger.warning(
                        "Form max kitta %s exceeds sanity limit %s; using default %s",
                        max_attr, MAX_KITTA_LIMIT, default_kitta,
                    )
                    kitta_input.fill(str(default_kitta))
                else:
                    # apply_max requested but the form's 'max' attribute is
                    # missing/unreadable. Warn so the degraded outcome is visible
                    # instead of silently applying the prefilled minimum.
                    logger.warning(
                        "apply_max requested but form 'max' attribute unreadable "
                        "(got %r); falling back to prefilled/default kitta.", max_attr,
                    )
                    if not kitta_value or kitta_value == "0":
                        kitta_input.fill(str(default_kitta))
            elif not kitta_value or kitta_value == "0":
                kitta_input.fill(str(default_kitta))

            # max_amount cap: compute estimated total at the real
            # share price (when caller passed it) and clip kitta if
            # the order would exceed the budget. Falls back to 100 NPR
            # when share_price isn't a finite positive number, with
            # a warning so the assumption is visible in logs.
            if max_amount and max_amount > 0:
                # math.isfinite catches NaN and Inf, which would
                # otherwise fail the `> 0` check silently and fall
                # through to the 100 NPR assumption.
                import math as _math
                share_price_ok = (
                    isinstance(share_price, (int, float))
                    and _math.isfinite(share_price)
                    and share_price > 0
                )
                price = share_price if share_price_ok else 100
                if not share_price_ok:
                    logger.warning(
                        "max_amount cap using assumed price 100 NPR/share. "
                        "caller did not provide a finite positive share_price "
                        "(got %r); premium issues may be capped incorrectly.",
                        share_price,
                    )
                raw_kitta = kitta_input.input_value() or "0"
                try:
                    current = int(raw_kitta)
                except (ValueError, TypeError):
                    # Don't silently skip the cap. Surface this as a
                    # warning so the audit trail records that we tried
                    # to apply the budget but couldn't read the form.
                    logger.warning(
                        "max_amount cap skipped: kitta input value %r is not "
                        "an integer (form may have unexpected shape).",
                        raw_kitta,
                    )
                    current = 0
                # Respect the form's minimum and step so the cap doesn't fill a
                # below-minimum or non-lot-aligned value the form would reject
                # (a false "apply failed"). MeroShare renders min/step on the input.
                def _pos_int_attr(name, fallback):
                    v = kitta_input.get_attribute(name)
                    return int(v) if (v and v.isdigit() and int(v) > 0) else fallback
                min_unit = _pos_int_attr("min", 1)
                step = _pos_int_attr("step", 1)
                if current > 0 and current * price > max_amount:
                    affordable = int(max_amount // price)
                    if affordable < min_unit:
                        # Even the minimum lot exceeds the budget. Respect the cap:
                        # don't overspend and don't submit a doomed below-min form.
                        msg = (
                            f"Not applied: max_amount {max_amount:,} NPR is below the "
                            f"minimum {min_unit} kitta (~{int(min_unit * price):,} NPR "
                            f"at {price} NPR/share)."
                        )
                        logger.warning(msg)
                        return {"success": False, "budget_exceeded": True, "message": msg}
                    # Snap down to a valid lot. HTML's step base is `min`, so a
                    # valid value satisfies (value - min) % step == 0 — anchor at
                    # min_unit, not 0, so odd min/step pairings aren't rejected.
                    capped = min_unit + ((affordable - min_unit) // step) * step
                    if capped < current:
                        kitta_input.fill(str(capped))
                        logger.info(
                            "Capped kitta from %d to %d (price=%s NPR, min=%d, step=%d) "
                            "to fit max_amount=%d NPR",
                            current, capped, price, min_unit, step, max_amount,
                        )
            _pace(0.5, 1)

            # Fill CRN
            crn_input = page.get_by_role("textbox", name="Enter CRN")
            crn_input.fill(crn)
            _pace(0.5, 1)

            # Check disclaimer. Prefer the stable id, fall back to a
            # text-match against "agree"/"disclaimer" so a MeroShare
            # rename of the input doesn't manifest as the cryptic
            # "Proceed button disabled" downstream.
            disclaimer = page.locator("#disclaimer")
            if disclaimer.count() == 0:
                # Match any unchecked checkbox whose accessible name or
                # nearby label mentions "agree"/"disclaimer".
                disclaimer = page.get_by_role("checkbox").filter(
                    has_text=re.compile(r"(agree|disclaimer)", re.I),
                )
            if disclaimer.count() and not disclaimer.first.is_checked():
                disclaimer.first.check()
            _pace(0.5, 1)

            # Click Proceed
            proceed_btn = page.get_by_role("button", name="Proceed")
            if proceed_btn.is_disabled():
                return {"success": False, "message": "Proceed button disabled - form validation failed. Check if all fields are filled."}
            proceed_btn.click()
            _pace(2, 4)

            # ── Step 4: Enter Transaction PIN ───────────────────────
            try:
                page.wait_for_selector("#transactionPIN", timeout=10000)
            except PlaywrightTimeout:
                # Maybe there was an error on form submission
                error_el = page.query_selector(".toast-error")
                if error_el:
                    return {"success": False, "message": f"Form error: {error_el.inner_text()}"}
                return {"success": False, "message": "Transaction PIN page did not appear"}

            logger.info("Entering transaction PIN...")
            page.locator("#transactionPIN").fill(pin)
            _pace(1, 2)

            # Click final Apply
            apply_btn = page.get_by_role("button", name="Apply")
            if apply_btn.is_disabled():
                return {"success": False, "message": "Apply button disabled after PIN entry"}
            apply_btn.click()
            # Removed the previous _pace(2, 4) here: it shrunk the
            # effective wait_for_selector window below and turned slow
            # MeroShare responses (common during IPO open peaks) into
            # false "Could not confirm" outcomes — the most dangerous
            # outcome, since the user may retry and risk a double-submit.
            # 20s is comfortably above observed peak-load latency.

            # ── Step 5: Check Result ────────────────────────────────
            # MeroShare wording varies across releases. "applied
            # successfully", "submitted successfully", "Application
            # submitted". Match a broader regex so a wording change
            # doesn't make every successful submit look like a
            # failure-to-confirm.
            try:
                page.wait_for_selector(
                    "text=/(applied|submitted)\\s+successfully/i",
                    timeout=20000,
                )
                logger.info("Application submitted successfully!")
                return {"success": True, "message": "Share has been applied successfully."}
            except PlaywrightTimeout:
                pass

            # Check for error toast
            error_el = page.query_selector(".toast-error")
            if error_el:
                return {"success": False, "message": error_el.inner_text()}

            # Check if redirected to ASBA list with Edit button (success indicator)
            if "/asba" in page.url and "apply" not in page.url:
                edit_btn = page.query_selector("text=Edit")
                if edit_btn:
                    return {"success": True, "message": "Share applied (Edit button visible)."}

            # Last-resort scan: a duplicate-rejected submit shows server
            # text like "already applied" / "form already exists" but
            # not always inside `.toast-error`. Treat it as idempotent
            # success so the scheduler doesn't keep re-firing.
            try:
                tail_text = page.locator("body").inner_text(timeout=2000) or ""
            except Exception:
                tail_text = ""
            if _looks_already_applied(tail_text):
                return {
                    "success": True,
                    "already_applied": True,
                    "message": "Already applied (server rejected duplicate)",
                }

            # Last-resort: capture a screenshot before the page is torn
            # down so the user can see exactly what the browser saw at
            # the moment we gave up. Without this artifact, the user has
            # no way to disambiguate "submitted but slow" from "rejected
            # silently" — both look identical in the log.
            screenshot_path = _capture_unconfirmed_screenshot(page, company_share_id)
            msg = "Could not confirm result - check MeroShare manually"
            if screenshot_path is not None:
                msg += f" (screenshot: {screenshot_path.name})"
            return {"success": False, "message": msg}

        except PlaywrightTimeout as e:
            logger.error("Browser timeout: %s", e)
            return {"success": False, "message": f"Browser timeout: {e}"}
        except Exception as e:
            # Don't include exc_info: the Playwright traceback can echo
            # form values (kitta, CRN) into the log file. Type + str
            # is enough for triage.
            logger.error(
                "Browser automation error (%s): %s",
                type(e).__name__, e,
            )
            return {"success": False, "message": str(e)}
        finally:
            browser.close()


# ── Retry wrapper ───────────────────────────────────────────────────
# Errors that are safe to retry. They happened DEMONSTRABLY before any
# form submit, so re-running can't create a duplicate application.
# Anything else (including ambiguous "did not load" / "Browser timeout"
# that could fire AFTER the apply click landed server-side) is treated
# as not safe to retry. Better to leave the user with an ambiguous
# "check MeroShare manually" message than risk a double-submit.
_RETRYABLE_PHRASES = (
    "Login timed out",
    "Login failed: ",
    "DP with ID",
    "Account number dropdown did not load",
    "Apply form did not load",
    "No bank available in dropdown",
    "No bank account available",
)


def apply_with_retry(
    company_share_id: int,
    *,
    retries: int = 1,
    cooldown_s: float = 5.0,
    **kwargs,
) -> dict:
    """Call apply_via_browser; on a clearly pre-submit failure, retry.

    `cooldown_s` is the sleep between attempts. Gives MeroShare's WAF
    a moment to forget any rate-limit blowback from the failed attempt.
    """
    last = None
    for attempt in range(1 + max(0, retries)):
        result = apply_via_browser(company_share_id, **kwargs)
        if result.get("success"):
            # If we succeeded after a retry, annotate so the user knows
            # the apply went through but wasn't first-try clean.
            if attempt > 0:
                result["message"] = (
                    f"{result.get('message', '')} (succeeded on retry {attempt})"
                ).strip()
            return result
        msg = str(result.get("message", ""))
        last = result
        if not any(p in msg for p in _RETRYABLE_PHRASES):
            return result  # not safe to retry
        if attempt < retries:
            logger.warning(
                "Pre-submit failure (%s); retrying apply for %s in %.1fs (%d/%d)",
                msg, company_share_id, cooldown_s, attempt + 1, retries,
            )
            time.sleep(cooldown_s)
    if last and last.get("message"):
        last["message"] = f"{last['message']} (after {retries + 1} attempts)"
    return last


if __name__ == "__main__":
    import sys

    import accounts

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python browser_apply.py <company_share_id> [--visible]")
        sys.exit(1)

    share_id = int(sys.argv[1])
    visible = "--visible" in sys.argv

    accts = accounts.load()
    if not accts:
        print("No accounts configured. Add one in the GUI Settings tab.")
        sys.exit(1)

    result = apply_via_browser(share_id, headless=not visible, credentials=accts[0])
    print(f"\nResult: {'SUCCESS' if result['success'] else 'FAILED'}")
    print(f"Message: {result['message']}")
