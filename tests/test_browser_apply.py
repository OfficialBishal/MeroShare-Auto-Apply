"""Tests for browser_apply.py helpers and result-classification logic.

We don't drive a real Chromium here. Instead we exercise the pure
phrase-matching helper and the new `already_applied` flag plumbing,
plus the `apply_with_retry` retry-decision matrix (which runs entirely
on the result dict and never touches Playwright).
"""
from __future__ import annotations

import unittest
from unittest import mock

import browser_apply


class LooksAlreadyAppliedTests(unittest.TestCase):
    """The detector must catch real MeroShare wording but not normal text."""

    def test_empty_input_is_not_already_applied(self):
        self.assertFalse(browser_apply._looks_already_applied(""))
        self.assertFalse(browser_apply._looks_already_applied(None))  # type: ignore[arg-type]

    def test_already_applied_is_caught(self):
        self.assertTrue(browser_apply._looks_already_applied(
            "You have already applied for this issue."
        ))

    def test_first_person_have_applied_is_caught(self):
        self.assertTrue(browser_apply._looks_already_applied(
            "You have applied for this share already."
        ))

    def test_form_already_exists_is_caught(self):
        self.assertTrue(browser_apply._looks_already_applied(
            "Form already exists in the system."
        ))

    def test_duplicate_application_is_caught(self):
        self.assertTrue(browser_apply._looks_already_applied(
            "Duplicate application detected."
        ))

    def test_descriptive_third_person_is_not_caught(self):
        # The previous "have applied" substring would have false-fired on
        # benign descriptive text. The tightened phrases must not.
        self.assertFalse(browser_apply._looks_already_applied(
            "Users who have applied for this issue earlier will receive shares."
        ))

    def test_case_insensitive(self):
        self.assertTrue(browser_apply._looks_already_applied(
            "ALREADY APPLIED"
        ))
        self.assertTrue(browser_apply._looks_already_applied(
            "AlReAdY sUbMiTtEd"
        ))


class _PlaywrightTimeoutLikeError(Exception):
    """Stand-in for playwright.sync_api.TimeoutError so tests don't need
    a real Playwright dependency to construct one."""


class ApplyWithRetryTests(unittest.TestCase):
    """`apply_with_retry` decides retry-vs-fail purely from the result dict;
    that decision matrix is the most important non-Playwright logic in
    browser_apply.py and used to be entirely uncovered."""

    def test_first_attempt_success_is_returned_unchanged(self):
        called = {"n": 0}

        def fake_apply(_csid, **_kw):
            called["n"] += 1
            return {"success": True, "message": "ok"}

        with mock.patch.object(browser_apply, "apply_via_browser", fake_apply):
            result = browser_apply.apply_with_retry(
                42, retries=3, cooldown_s=0,
                credentials={"username": "u"}, default_kitta=10,
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "ok")
        self.assertEqual(called["n"], 1)

    def test_already_applied_flag_survives_retry_wrapper(self):
        def fake_apply(_csid, **_kw):
            return {
                "success": True,
                "already_applied": True,
                "message": "Already applied (apply page redirected)",
            }

        with mock.patch.object(browser_apply, "apply_via_browser", fake_apply):
            result = browser_apply.apply_with_retry(
                42, retries=1, cooldown_s=0,
                credentials={"username": "u"},
            )
        self.assertTrue(result["success"])
        self.assertTrue(result.get("already_applied"))

    def test_pre_submit_failure_is_retried(self):
        sequence = [
            {"success": False, "message": "Login timed out"},
            {"success": True, "message": "ok"},
        ]
        calls = []

        def fake_apply(_csid, **_kw):
            calls.append(1)
            return sequence.pop(0)

        with mock.patch.object(browser_apply, "apply_via_browser", fake_apply), \
             mock.patch.object(browser_apply.time, "sleep"):
            result = browser_apply.apply_with_retry(
                42, retries=2, cooldown_s=0,
                credentials={"username": "u"},
            )
        self.assertTrue(result["success"])
        # Annotated with retry attempt so the user sees it wasn't first try.
        self.assertIn("succeeded on retry", result["message"])
        self.assertEqual(len(calls), 2)

    def test_post_submit_ambiguity_is_not_retried(self):
        # "Could not confirm result" can fire AFTER the apply click landed
        # server-side. A retry there would risk a double-submit, so the
        # wrapper must NOT retry. This is the highest-stakes test in the
        # whole file.
        calls = []

        def fake_apply(_csid, **_kw):
            calls.append(1)
            return {
                "success": False,
                "message": "Could not confirm result - check MeroShare manually",
            }

        with mock.patch.object(browser_apply, "apply_via_browser", fake_apply):
            result = browser_apply.apply_with_retry(
                42, retries=3, cooldown_s=0,
                credentials={"username": "u"},
            )
        self.assertFalse(result["success"])
        self.assertEqual(len(calls), 1, "ambiguous post-submit failure must not retry")

    def test_ambiguous_browser_timeout_is_not_retried(self):
        # Browser timeouts can fire mid-form OR after the click — we don't
        # know which. Treated as not-safe-to-retry (better to leave the
        # user with "check manually" than risk a duplicate).
        calls = []

        def fake_apply(_csid, **_kw):
            calls.append(1)
            return {"success": False, "message": "Browser timeout: 120000ms"}

        with mock.patch.object(browser_apply, "apply_via_browser", fake_apply):
            result = browser_apply.apply_with_retry(
                42, retries=3, cooldown_s=0,
                credentials={"username": "u"},
            )
        self.assertFalse(result["success"])
        self.assertEqual(len(calls), 1)

    def test_attempt_count_is_appended_after_exhaustion(self):
        def fake_apply(_csid, **_kw):
            return {"success": False, "message": "Login timed out"}

        with mock.patch.object(browser_apply, "apply_via_browser", fake_apply), \
             mock.patch.object(browser_apply.time, "sleep"):
            result = browser_apply.apply_with_retry(
                42, retries=2, cooldown_s=0,
                credentials={"username": "u"},
            )
        self.assertIn("after 3 attempts", result["message"])


class CredentialValidationTests(unittest.TestCase):
    """apply_via_browser refuses to launch a browser without credentials."""

    def test_returns_failure_when_credentials_missing(self):
        result = browser_apply.apply_via_browser(42, credentials=None)
        self.assertFalse(result["success"])
        self.assertIn("credentials required", result["message"])

    def test_returns_failure_when_required_field_missing(self):
        # Missing pin — the validator should call out the field.
        result = browser_apply.apply_via_browser(
            42,
            credentials={
                "username": "u", "password": "p", "crn": "c",
                "dp_id": "10600",  # pin missing
            },
        )
        self.assertFalse(result["success"])
        self.assertIn("pin", result["message"].lower())


if __name__ == "__main__":
    unittest.main()
