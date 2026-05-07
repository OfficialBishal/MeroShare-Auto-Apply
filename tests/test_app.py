"""Tests for Flask routes that don't require live MeroShare connectivity.

These cover the routes most likely to regress: validation, the bg-status
gate, and credential mask handling. Routes that hit the network
(/api/issues, /api/status, /api/apply) are covered by checking that the
input validation around them rejects bad payloads. Not the network call
itself.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import accounts
import app as app_module
from tests._keystore_fake import FakeKeystoreMixin


class _FlaskTestCase(FakeKeystoreMixin, unittest.TestCase):
    """Redirect every on-disk artifact to a tmpdir AND swap secrets_store
    for an in-memory fake so tests can run in any order without polluting
    the user's real accounts.json, config.json, or system keychain."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        accounts_file = self.tmp_path / "accounts.json"
        applied_file = self.tmp_path / ".applied_issues.json"
        env_file = self.tmp_path / ".env"
        config_file = self.tmp_path / "config.json"

        applied_lock = self.tmp_path / ".applied_issues.lock"
        self._patches = [
            mock.patch.object(accounts, "ACCOUNTS_FILE", accounts_file),
            mock.patch.object(accounts, "APPLIED_FILE", applied_file),
            mock.patch.object(accounts, "APPLIED_LOCK_FILE", applied_lock),
            mock.patch.object(accounts, "ENV_FILE", env_file),
            mock.patch.object(app_module, "CONFIG_FILE", config_file),
        ]
        for p in self._patches:
            p.start()

        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Reset the bg-status singleton between tests so the gate isn't stuck
        # from a prior run.
        with app_module._bg_lock:
            app_module._bg_status["running"] = False
            app_module._bg_status["message"] = ""
            app_module._bg_status["results"] = {}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        super().tearDown()

    _next_username = 5000

    def _add_account(self, name="A"):
        # Unique username per call so duplicate-credential detection
        # doesn't trip when a test wants two accounts with different
        # display names.
        type(self)._next_username += 1
        return accounts.add({
            "name": name, "dp_id": "10600",
            "username": f"u{type(self)._next_username}",
            "password": "p", "crn": "c", "pin": "1234",
        })


class ConfigRouteTests(_FlaskTestCase):
    def test_get_returns_default_when_no_file(self):
        # No config.json yet. Server returns the in-memory default which
        # must include the "notifications" key; otherwise the GUI's
        # save-with-merge would drop it on first save.
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        cfg = resp.get_json()
        self.assertIn("share_types", cfg)
        self.assertIn("auto_apply", cfg)
        self.assertIn("notifications", cfg)

    def test_save_rejects_non_dict_body(self):
        # A null / list / string body used to silently write garbage that
        # crashed check_and_apply on the next load. Reject explicitly.
        for bad_body in ("null", "[]", '"not a dict"', "42"):
            with self.subTest(body=bad_body):
                resp = self.client.post(
                    "/api/config", data=bad_body,
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 400)

    def test_save_writes_atomically(self):
        # Confirm save uses _atomic_write (no .tmp left behind).
        body = {"share_types": {"ipo_ordinary": True}, "auto_apply": {}}
        resp = self.client.post("/api/config", json=body)
        self.assertEqual(resp.status_code, 200)
        cfg_file = app_module.CONFIG_FILE
        self.assertTrue(cfg_file.exists())
        self.assertFalse(cfg_file.with_suffix(".json.tmp").exists())
        self.assertEqual(json.loads(cfg_file.read_text()), body)


class AccountRouteTests(_FlaskTestCase):
    def test_create_returns_masked_record(self):
        resp = self.client.post("/api/accounts", json={
            "name": "Mine", "dp_id": "10600", "username": "u",
            "password": "secret", "crn": "c", "pin": "1234",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # The route must mask password and pin before returning, so a
        # browser dev-tools peek doesn't reveal the credential. Mask
        # uses a fixed-width placeholder (no length / first-char leak).
        import accounts as _accounts
        self.assertEqual(body["password"], _accounts.MASKED_PLACEHOLDER)
        self.assertEqual(body["pin"], _accounts.MASKED_PLACEHOLDER)
        self.assertEqual(body["username"], "u")

    def test_create_rejects_missing_field(self):
        resp = self.client.post("/api/accounts", json={
            "name": "X", "dp_id": "1", "username": "u",
            # password missing
            "crn": "c", "pin": "1",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_update_strips_masked_password_at_route(self):
        # GUI sends back the masked password unchanged on edits where the
        # user didn't retype. The route must strip it so the real value
        # isn't overwritten with the mask shape.
        rec = self._add_account("Edit Me")
        masked_pwd = accounts.mask({"password": "p"})["password"]
        resp = self.client.put(f"/api/accounts/{rec['id']}", json={
            "name": "Renamed",
            "password": masked_pwd,
        })
        self.assertEqual(resp.status_code, 200)
        stored = accounts.get(rec["id"])
        self.assertEqual(stored["name"], "Renamed")
        self.assertEqual(stored["password"], "p")  # untouched

    def test_delete_unknown_returns_404(self):
        resp = self.client.delete("/api/accounts/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_get_lists_masked_accounts(self):
        self._add_account("Mine")
        resp = self.client.get("/api/accounts")
        self.assertEqual(resp.status_code, 200)
        listed = resp.get_json()
        self.assertEqual(len(listed), 1)
        # Every credential field must be masked at the API boundary so a
        # browser dev-tools peek doesn't reveal them.
        self.assertEqual(listed[0]["password"], accounts.mask({"password": "p"})["password"])
        self.assertEqual(listed[0]["pin"], accounts.mask({"pin": "1234"})["pin"])


class BgStatusGateTests(_FlaskTestCase):
    def test_apply_status_returns_snapshot_shape(self):
        resp = self.client.get("/api/apply-status")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(set(body.keys()), {"running", "message", "results"})
        self.assertFalse(body["running"])

    def test_run_check_rejects_when_no_accounts(self):
        # Guards against a confusing 500 if the user clicks Run-now before
        # adding any account.
        resp = self.client.post("/api/run-check")
        self.assertEqual(resp.status_code, 400)

    def test_apply_rejects_when_no_targets(self):
        # No accounts at all → no apply target → 400.
        resp = self.client.post("/api/apply/123", json={"account_ids": []})
        self.assertEqual(resp.status_code, 400)

    def test_run_check_blocks_concurrent(self):
        # Once the bg slot is claimed (manually), a second run-check must
        # be rejected with 409, not silently start a second worker.
        self._add_account("X")
        with app_module._bg_lock:
            app_module._bg_status["running"] = True
        try:
            resp = self.client.post("/api/run-check")
            self.assertEqual(resp.status_code, 409)
        finally:
            with app_module._bg_lock:
                app_module._bg_status["running"] = False


class IssuesMergeTests(_FlaskTestCase):
    """Per-account eligibility merge in /api/issues. Right shares are
    reserved to existing shareholders, so an issue might be eligible
    for one account but not another. The chip set per issue must
    reflect that, not paint a chip for every account on every row.
    """

    def setUp(self):
        super().setUp()
        # Two accounts: "Mine" and "Spouse".
        self._add_account("Mine")
        self._add_account("Spouse")
        # Reset module caches between tests.
        app_module._report_cache.clear()
        app_module._applicable_cache.clear()

    def _seed_caches(self, mine_eligible, spouse_eligible, applied_local=None):
        # Helper to bypass real MeroShare login. Populate the per-account
        # caches directly with what the per-account fetch would have
        # returned.
        for acct_id, eligible in [("mine", mine_eligible), ("spouse", spouse_eligible)]:
            issues = [
                {"companyShareId": str(cid), "companyName": f"Company {cid}",
                 "shareTypeName": "Right Share" if cid == 100 else "IPO"}
                for cid in eligible
            ]
            app_module._applicable_put(acct_id, issues)
            app_module._report_put(acct_id, {})  # nothing applied
        if applied_local:
            accounts.save_applied(applied_local)

    def test_right_share_eligible_for_only_one_account(self):
        # Issue 100 is a right share, only Mine is eligible. Issue 200
        # is an IPO, both eligible.
        self._seed_caches([100, 200], [200])
        resp = self.client.get("/api/issues")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        by_id = {i["id"]: i for i in body}

        # Issue 100 only has Mine's chip; no Spouse chip.
        self.assertIn("100", by_id)
        apps_100 = by_id["100"]["applications"]
        self.assertIn("mine", apps_100)
        self.assertNotIn("spouse", apps_100)

        # Issue 200 has both chips.
        apps_200 = by_id["200"]["applications"]
        self.assertIn("mine", apps_200)
        self.assertIn("spouse", apps_200)

    def test_locally_applied_for_ineligible_account_still_shows(self):
        # Mine had Issue 100 in applicable a week ago and applied for
        # it. Today MeroShare's applicable list no longer includes 100
        # (already applied / closed). The chip should still appear so
        # the user sees the historic application.
        self._seed_caches([200], [200], applied_local={
            "mine": {"100": {"applied_at": "2026-04-01T10:00:00", "company": "Old Co"}},
        })
        resp = self.client.get("/api/issues")
        self.assertEqual(resp.status_code, 200)
        by_id = {i["id"]: i for i in resp.get_json()}
        self.assertIn("100", by_id)
        self.assertTrue(by_id["100"]["applications"]["mine"]["applied"])
        # Spouse never applied for 100 and isn't eligible. No chip.
        self.assertNotIn("spouse", by_id["100"]["applications"])


class ShutdownRouteTests(_FlaskTestCase):
    def test_refuses_when_bg_task_running(self):
        # Don't tear down the server mid-apply; the user would see a
        # cancelled submission with no recovery.
        with app_module._bg_lock:
            app_module._bg_status["running"] = True
        try:
            resp = self.client.post("/api/shutdown")
            self.assertEqual(resp.status_code, 409)
            self.assertIn("error", resp.get_json())
        finally:
            with app_module._bg_lock:
                app_module._bg_status["running"] = False

    def test_returns_shutting_down_when_idle(self):
        # The route's contract: synchronously call scheduler.stop() so a
        # failure (launchctl perms, missing plist) surfaces in the
        # response, then schedule the kill on a background thread.
        with mock.patch.object(app_module.threading, "Thread") as mock_thread, \
             mock.patch.object(app_module.scheduler, "stop") as mock_stop:
            mock_thread.return_value.start.return_value = None
            mock_stop.return_value = None
            resp = self.client.post("/api/shutdown")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get("status"), "shutting down")
        # scheduler.stop must have been called BEFORE we returned -
        # not deferred to the worker thread.
        mock_stop.assert_called_once()
        # Two background threads now: one to SIGTERM the menu bar
        # (so the user's "Stop everything" feels instant rather than
        # waiting for the menu bar's 30s sentinel tick), and one to
        # SIGINT Flask itself after a 0.3s response-flush delay.
        self.assertEqual(mock_thread.call_count, 2)

    def test_signals_menu_bar_directly(self):
        # Regression: previously the menu bar lingered up to 30 seconds
        # after "Stop everything" because it polled the sentinel file
        # on its 30s tick. /api/shutdown now also pgrep's the menu bar
        # process and SIGTERMs it. Verify pgrep is invoked AND the
        # SIGTERM happens (mocked, since the menubar.py script only
        # exists at runtime in production builds).
        fake_proc = mock.MagicMock()
        fake_proc.stdout = ""  # no menu bar to signal in tests
        with mock.patch.object(app_module.subprocess, "run",
                               return_value=fake_proc) as mock_run, \
                mock.patch.object(app_module.os, "kill"), \
                mock.patch.object(app_module.scheduler, "stop"):
            resp = self.client.post("/api/shutdown")
            # The signal-menubar thread is daemon=True and runs in the
            # background. Give it a moment to fire.
            import time as _t
            _t.sleep(0.1)
        self.assertEqual(resp.status_code, 200)
        # pgrep was called with the menubar.py pattern.
        called_args = [c.args[0] for c in mock_run.call_args_list]
        self.assertTrue(
            any("pgrep" in args[0] and "menubar.py" in args
                for args in called_args),
            f"pgrep call not found in {called_args}",
        )

    def test_surfaces_scheduler_stop_failure(self):
        # When scheduler.stop raises, the response should still 200
        # but include a scheduler_warning the GUI can surface.
        with mock.patch.object(app_module.threading, "Thread") as mock_thread, \
             mock.patch.object(app_module.scheduler, "stop",
                               side_effect=RuntimeError("launchctl perms")):
            mock_thread.return_value.start.return_value = None
            resp = self.client.post("/api/shutdown")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("status"), "shutting down")
        self.assertIn("launchctl perms", body.get("scheduler_warning", ""))


class ConfigValidationTests(unittest.TestCase):
    """_validate_config blocks values that would crash check_and_apply
    on the next load."""

    def test_accepts_minimal_object(self):
        self.assertIsNone(app_module._validate_config({}))

    def test_rejects_non_dict(self):
        self.assertIn("object", app_module._validate_config(None) or "")
        self.assertIn("object", app_module._validate_config([1, 2]) or "")

    def test_rejects_bad_check_interval(self):
        self.assertIn("check_interval_hours",
                      app_module._validate_config({"check_interval_hours": 0}) or "")
        self.assertIn("check_interval_hours",
                      app_module._validate_config({"check_interval_hours": 99}) or "")
        self.assertIn("check_interval_hours",
                      app_module._validate_config({"check_interval_hours": "soon"}) or "")

    def test_rejects_negative_max_amount(self):
        self.assertIn("max_amount", app_module._validate_config({
            "auto_apply": {"max_amount": -1},
        }) or "")

    def test_rejects_non_bool_share_type(self):
        self.assertIn("share_types.fpo", app_module._validate_config({
            "share_types": {"fpo": "yes"},
        }) or "")

    def test_accepts_preferred_share_toggle(self):
        # Menubar's Preferences submenu can toggle preferred_share.
        # Validator must not reject it as an unknown share type.
        self.assertIsNone(app_module._validate_config({
            "share_types": {"preferred_share": True},
        }))

    def test_rejects_unknown_share_type(self):
        # An unknown key would never be acted on by the classifier so
        # rejecting it loudly is better than silently storing it.
        err = app_module._validate_config({
            "share_types": {"made_up_thing": True},
        })
        self.assertIsNotNone(err)
        self.assertIn("made_up_thing", err)


class TailLinesTests(unittest.TestCase):
    """_tail_lines reads from the file end without slurping the whole
    file. Verifies behaviour against the previous read-everything
    implementation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "log.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_last_n_lines(self):
        self.path.write_text("\n".join(f"line {i}" for i in range(100)))
        out = app_module._tail_lines(self.path, 5)
        self.assertEqual(out, [f"line {i}" for i in range(95, 100)])

    def test_handles_smaller_file_than_chunk(self):
        self.path.write_text("a\nb\nc\n")
        # \n at end produces empty trailing element after splitlines drops it
        self.assertEqual(app_module._tail_lines(self.path, 10), ["a", "b", "c"])

    def test_handles_missing_file(self):
        self.assertEqual(app_module._tail_lines(self.path / "nope", 10), [])


class OriginGuardTests(_FlaskTestCase):
    """The before_request guard rejects browser POSTs that don't
    prove same-origin via Origin / Referer / Sec-Fetch-Site."""

    def test_allows_loopback_origin(self):
        resp = self.client.post(
            "/api/config",
            json={},
            headers={"Origin": "http://localhost:5050"},
        )
        # Validation passes for empty dict; route returns 200.
        self.assertEqual(resp.status_code, 200)

    def test_allows_loopback_referer(self):
        resp = self.client.post(
            "/api/config",
            json={},
            headers={"Referer": "http://127.0.0.1:5050/settings"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_allows_sec_fetch_same_origin(self):
        resp = self.client.post(
            "/api/config",
            json={},
            headers={"Sec-Fetch-Site": "same-origin",
                     "User-Agent": "Mozilla/5.0"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_rejects_browser_with_no_origin_signals(self):
        # The DNS-rebinding scenario the previous early-return missed:
        # a browser request with no Origin, no Referer, no Sec-Fetch-
        # Site, and a browser User-Agent.
        resp = self.client.post(
            "/api/config",
            json={},
            headers={"User-Agent": "Mozilla/5.0 (Browser)"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_rejects_cross_origin(self):
        resp = self.client.post(
            "/api/config",
            json={},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_allows_curl_no_signals(self):
        resp = self.client.post(
            "/api/config",
            json={},
            headers={"User-Agent": "curl/8.0"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_does_not_guard_get_requests(self):
        resp = self.client.get(
            "/api/version",
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_rejects_sec_fetch_cross_site(self):
        # Browser-set Sec-Fetch-Site can't be spoofed by JS. When it
        # says cross-site we know definitively this is not same-origin.
        resp = self.client.post(
            "/api/config",
            json={},
            headers={
                "Sec-Fetch-Site": "cross-site",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_rejects_sec_fetch_same_site_subdomain(self):
        # `same-site` from a sibling localhost.attacker.example is
        # still cross-ORIGIN even though it's same-site.
        resp = self.client.post(
            "/api/config",
            json={},
            headers={
                "Sec-Fetch-Site": "same-site",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_allows_sec_fetch_none(self):
        # `none` is what address-bar / bookmark / typed URL sends.
        resp = self.client.post(
            "/api/config",
            json={},
            headers={
                "Sec-Fetch-Site": "none",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertEqual(resp.status_code, 200)


class RestoreVsMalformedAccountsTests(_FlaskTestCase):
    """save_all refuses to overwrite a malformed accounts.json with a
    single-account payload. The restore route must surface that as a
    400 with the helpful message, not a generic 500."""

    def test_restore_propagates_account_error_as_400(self):
        # Force a malformed accounts.json on disk.
        accounts.ACCOUNTS_FILE.write_text("not json {{{")
        body = {
            "schema": 1,
            "accounts": [{
                "id": "x", "name": "X", "dp_id": "10600", "username": "u",
                "password": "p", "crn": "c", "pin": "1234",
            }],
            "applied": {},
        }
        resp = self.client.post(
            "/api/restore",
            json=body,
            headers={"Origin": "http://localhost:5050"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("malformed", resp.get_json().get("error", ""))


class RestoreValidatesPerRecordTests(_FlaskTestCase):
    """A backup with malformed account records used to be accepted whole-
    sale, leaving the user with credential-empty accounts that crashed
    silently on the next scheduler run. The restore route now runs each
    record through `accounts.validate_record` before writing."""

    def _post_restore(self, body):
        return self.client.post(
            "/api/restore",
            json=body,
            headers={"Origin": "http://localhost:5050"},
        )

    def test_rejects_record_with_empty_password(self):
        body = {
            "schema": 1,
            "accounts": [{
                "id": "x", "name": "X", "dp_id": "10600", "username": "u",
                "password": "", "crn": "c", "pin": "1234",
            }],
            "applied": {},
        }
        resp = self._post_restore(body)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("password", resp.get_json()["error"].lower())

    def test_rejects_record_with_non_numeric_dp_id(self):
        body = {
            "schema": 1,
            "accounts": [{
                "id": "x", "name": "X", "dp_id": "ABCDE", "username": "u",
                "password": "p", "crn": "c", "pin": "1234",
            }],
            "applied": {},
        }
        resp = self._post_restore(body)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("dp_id", resp.get_json()["error"].lower())

    def test_rejects_duplicate_dp_username_pair_within_backup(self):
        body = {
            "schema": 1,
            "accounts": [
                {"id": "a", "name": "A", "dp_id": "10600", "username": "u",
                 "password": "p", "crn": "c", "pin": "1234"},
                {"id": "b", "name": "B", "dp_id": "10600", "username": "u",
                 "password": "p", "crn": "c", "pin": "1234"},
            ],
            "applied": {},
        }
        resp = self._post_restore(body)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("two records", resp.get_json()["error"].lower())

    def test_rejects_duplicate_name_within_backup(self):
        body = {
            "schema": 1,
            "accounts": [
                {"id": "a", "name": "Same", "dp_id": "10600", "username": "u1",
                 "password": "p", "crn": "c", "pin": "1234"},
                {"id": "b", "name": "Same", "dp_id": "10601", "username": "u2",
                 "password": "p", "crn": "c", "pin": "1234"},
            ],
            "applied": {},
        }
        resp = self._post_restore(body)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("two accounts named", resp.get_json()["error"].lower())

    def test_accepts_well_formed_backup(self):
        body = {
            "schema": 1,
            "accounts": [{
                "id": "x", "name": "X", "dp_id": "10600", "username": "u",
                "password": "p", "crn": "c", "pin": "1234",
            }],
            "applied": {},
        }
        resp = self._post_restore(body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["accounts"], 1)


class ApplyCategoryValidationTests(_FlaskTestCase):
    """The api_apply category field used to be unchecked. A wrong-cased
    value (e.g. 'RIGHT_SHARE') silently bypassed apply_max for right
    shares — leaving users applying for the prefilled minimum instead
    of their full eligible quantity. We now validate the value strictly."""

    def setUp(self):
        super().setUp()
        self._add_account("Mine")

    def test_unknown_category_returns_400(self):
        # Note: account_ids is required, so we send a real one. The
        # category check fires before any background work starts.
        ids = [a["id"] for a in accounts.load()]
        resp = self.client.post(
            "/api/apply/123",
            json={"account_ids": ids, "category": "made_up"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unknown category", resp.get_json()["error"].lower())

    def test_non_string_category_is_treated_as_none(self):
        ids = [a["id"] for a in accounts.load()]
        # `True`/`123` shouldn't 400 — the route normalizes to None and
        # leaves apply_max at its default-False. We can't observe
        # apply_max from the GUI, but we can verify the 400 doesn't fire.
        with mock.patch.object(app_module, "apply_with_retry") as patched:
            patched.return_value = {"success": False, "message": "noop"}
            resp = self.client.post(
                "/api/apply/123",
                json={"account_ids": ids, "category": True},
            )
        self.assertEqual(resp.status_code, 200)


class AllotmentNotificationTests(_FlaskTestCase):
    """The Pending → Allotted transition fires a desktop notification
    exactly once. Subsequent polls observing the same final state must
    NOT re-fire (the worst kind of bug for a notification: spam after
    the first happy moment)."""

    def setUp(self):
        super().setUp()
        self._notifications = []
        # Patch the import that _notify_allotment_changes uses lazily.
        import auto_apply
        self._notify_patch = mock.patch.object(
            auto_apply, "notify",
            side_effect=lambda title, msg: self._notifications.append((title, msg)),
        )
        self._notify_patch.start()

    def tearDown(self):
        self._notify_patch.stop()
        super().tearDown()

    def _run(self, reports):
        app_module._notify_allotment_changes(reports)

    def test_first_observation_of_final_status_does_not_notify(self):
        # Fresh install sees an already-allotted issue. We should NOT
        # toast — the user wasn't using this tool when allotment landed,
        # so claiming "shares allotted!" would mislead.
        self._run([{
            "applicantFormId": 1, "detailStatus": "ALLOTED",
            "companyName": "Test Bank", "accountName": "A",
        }])
        self.assertEqual(self._notifications, [])

    def test_pending_to_allotted_fires_one_notification(self):
        # First poll: pending. No notification (no transition).
        self._run([{
            "applicantFormId": 7, "detailStatus": "PENDING",
            "companyName": "Test Bank", "accountName": "A",
            "appliedKitta": 50,
        }])
        self.assertEqual(self._notifications, [])
        # Second poll: allotted. ONE notification.
        self._run([{
            "applicantFormId": 7, "detailStatus": "ALLOTED",
            "companyName": "Test Bank", "accountName": "A",
            "appliedKitta": 50,
        }])
        self.assertEqual(len(self._notifications), 1)
        title, msg = self._notifications[0]
        self.assertIn("allotted", title.lower())
        self.assertIn("Test Bank", msg)

    def test_allotted_status_does_not_re_fire_on_subsequent_polls(self):
        self._run([{
            "applicantFormId": 9, "detailStatus": "PENDING",
            "companyName": "Bank", "accountName": "A",
        }])
        self._run([{
            "applicantFormId": 9, "detailStatus": "ALLOTED",
            "companyName": "Bank", "accountName": "A",
        }])
        self._run([{
            "applicantFormId": 9, "detailStatus": "ALLOTED",
            "companyName": "Bank", "accountName": "A",
        }])
        self._run([{
            "applicantFormId": 9, "detailStatus": "ALLOTED",
            "companyName": "Bank", "accountName": "A",
        }])
        self.assertEqual(len(self._notifications), 1)

    def test_classifier_handles_meroshare_spelling_variants(self):
        # MeroShare has historically used "ALLOTED" (single t). Don't
        # let a spelling change silently disable allotment detection.
        for variant in ("ALLOTED", "Allotted", "allotted"):
            with self.subTest(variant=variant):
                self.assertEqual(app_module._classify_allotment(variant), "allotted")
        for variant in ("NOT ALLOTED", "Not Allotted", "rejected"):
            with self.subTest(variant=variant):
                self.assertEqual(app_module._classify_allotment(variant), "not_allotted")
        for variant in ("PENDING", "", "Some Future Status"):
            with self.subTest(variant=variant):
                self.assertEqual(app_module._classify_allotment(variant), "pending")

    def test_classifier_priority_negative_first(self):
        # Regression: 'NOT ALLOTED'.lower().includes('alloted') is true,
        # so a naive if/elif chain that tests positive-allotted first
        # mis-classifies losing applications as wins. Pin the priority:
        # negative phrasing must be checked BEFORE positive.
        self.assertEqual(app_module._classify_allotment("NOT ALLOTED"),
                         "not_allotted",
                         "regression: NOT ALLOTED counted as ALLOTTED")
        self.assertEqual(app_module._classify_allotment("Not Allotted"),
                         "not_allotted")


class LookupSharePriceTests(_FlaskTestCase):
    """The /api/apply path used to omit share_price entirely, falling back
    to browser_apply.py's 100 NPR assumption. For premium FPOs that
    silently capped applications at 5x the user's intended budget."""

    def test_pulls_price_from_applicable_cache(self):
        self._add_account("Mine")
        app_module._applicable_put("mine", [{
            "companyShareId": "777",
            "sharePerUnit": 500.0,
        }])
        price = app_module._lookup_share_price("777", accounts.load())
        self.assertEqual(price, 500.0)

    def test_returns_none_when_issue_not_in_cache(self):
        self._add_account("Mine")
        app_module._applicable_put("mine", [])
        self.assertIsNone(app_module._lookup_share_price("999", accounts.load()))

    def test_ignores_garbage_share_per_unit(self):
        self._add_account("Mine")
        for bad in ("abc", "", None, float("nan"), -100):
            with self.subTest(bad=bad):
                app_module._applicable_put("mine", [{
                    "companyShareId": "777",
                    "sharePerUnit": bad,
                }])
                self.assertIsNone(
                    app_module._lookup_share_price("777", accounts.load())
                )


class SafeExcTests(unittest.TestCase):
    def test_includes_type_name(self):
        out = app_module._safe_exc(ValueError("oops"))
        self.assertTrue(out.startswith("ValueError:"))
        self.assertIn("oops", out)

    def test_truncates_long_messages(self):
        out = app_module._safe_exc(RuntimeError("x" * 1000))
        self.assertLess(len(out), 250)
        self.assertIn("truncated", out)

    def test_redacts_credential_patterns(self):
        # Defense-in-depth: a future endpoint URL-encoding creds in a
        # query string shouldn't dump them to logs verbatim.
        out = app_module._safe_exc(ValueError("auth failed: password=hunter2 pin=1234"))
        self.assertNotIn("hunter2", out)
        self.assertNotIn("1234", out)
        self.assertIn("[REDACTED]", out)

    def test_redacts_crn(self):
        out = app_module._safe_exc(RuntimeError("body: crn=CB1234567 oops"))
        self.assertNotIn("CB1234567", out)
        self.assertIn("[REDACTED]", out)


class HealthEndpointTests(_FlaskTestCase):
    def test_returns_200_with_checks(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("uptime_seconds", body)
        self.assertIn("checks", body)
        self.assertIn("accounts", body["checks"])
        self.assertIn("applied_issues", body["checks"])

    def test_returns_503_when_accounts_unloadable(self):
        with mock.patch.object(accounts, "load",
                               side_effect=RuntimeError("disk error")):
            resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertEqual(body["status"], "degraded")
        self.assertFalse(body["checks"]["accounts"]["ok"])


class FailuresEndpointTests(_FlaskTestCase):
    def test_links_apply_to_failure_lines(self):
        # Write a synthetic log fragment matching the format the
        # auto_apply.py logger produces, then verify the endpoint
        # returns the parsed (account, company, error) triple.
        log = (
            "2026-05-04 02:29:09,045 [INFO]     -> Applying for Yambaling Hydropower on Mine (kitta=10, ...)\n"
            "2026-05-04 02:30:00,335 [ERROR]     -> FAILED: Could not confirm result\n"
            "2026-05-04 02:30:00,452 [INFO]     -> Applying for UNITED AJOD INSURANCE LIMITED on Mine (kitta=10, ...)\n"
            "2026-05-04 02:30:53,521 [ERROR]     -> FAILED: Could not confirm result\n"
        )
        log_path = self.tmp_path / "meroshare.log"
        log_path.write_text(log)
        with mock.patch.object(app_module, "LOG_FILE", log_path):
            resp = self.client.get("/api/failures")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual(len(rows), 2)
        # Newest first.
        self.assertEqual(rows[0]["account"], "Mine")
        self.assertIn("UNITED AJOD", rows[0]["company"])
        self.assertEqual(rows[1]["company"], "Yambaling Hydropower")

    def test_returns_empty_when_log_missing(self):
        bogus = self.tmp_path / "nope.log"
        with mock.patch.object(app_module, "LOG_FILE", bogus):
            resp = self.client.get("/api/failures")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])


class FactoryResetTests(_FlaskTestCase):
    def test_requires_confirm(self):
        resp = self.client.post(
            "/api/factory-reset",
            json={},
            headers={"Origin": "http://localhost:5050"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_wipes_state_when_confirmed(self):
        # Seed some state.
        self._add_account("Test")
        self.assertTrue(accounts.ACCOUNTS_FILE.exists())
        resp = self.client.post(
            "/api/factory-reset",
            json={"confirm": "WIPE"},
            headers={"Origin": "http://localhost:5050"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "reset")
        self.assertFalse(accounts.ACCOUNTS_FILE.exists())

    def test_refuses_during_bg_task(self):
        with app_module._bg_lock:
            app_module._bg_status["running"] = True
        try:
            resp = self.client.post(
                "/api/factory-reset",
                json={"confirm": "WIPE"},
                headers={"Origin": "http://localhost:5050"},
            )
            self.assertEqual(resp.status_code, 409)
        finally:
            with app_module._bg_lock:
                app_module._bg_status["running"] = False


class DeleteAppliedForAccountTests(_FlaskTestCase):
    def test_clears_all_for_account(self):
        accounts.save_applied({
            "acct1": {"123": {"applied_at": "x"}, "456": {"applied_at": "y"}},
            "acct2": {"789": {"applied_at": "z"}},
        })
        resp = self.client.delete(
            "/api/applied-issues/acct1",
            headers={"Origin": "http://localhost:5050"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["removed"], 2)
        # acct1 gone; acct2 untouched.
        state = accounts.load_applied()
        self.assertNotIn("acct1", state)
        self.assertIn("acct2", state)

    def test_unknown_account_is_zero_remove(self):
        resp = self.client.delete(
            "/api/applied-issues/ghost",
            headers={"Origin": "http://localhost:5050"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["removed"], 0)


class ShutdownSentinelTests(_FlaskTestCase):
    """/api/shutdown writes a sentinel file the menu bar polls for.

    Without this, "Stop everything" from the GUI takes down Flask but
    leaves the menu bar running, which would re-spawn Flask on the
    next "Open Dashboard" click.
    """

    def test_writes_sentinel(self):
        with mock.patch.object(app_module.threading, "Thread") as mock_thread, \
                mock.patch.object(app_module.scheduler, "stop"):
            mock_thread.return_value.start.return_value = None
            resp = self.client.post("/api/shutdown")
        self.assertEqual(resp.status_code, 200)
        sentinel = accounts.STATE_DIR / ".shutdown-requested"
        self.assertTrue(sentinel.exists())

    def test_factory_reset_clears_sentinel(self):
        sentinel = accounts.STATE_DIR / ".shutdown-requested"
        sentinel.write_text("123")
        resp = self.client.post(
            "/api/factory-reset",
            json={"confirm": "WIPE"},
            headers={"Origin": "http://localhost:5050"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(sentinel.exists())


class LooksAlreadyAppliedTests(unittest.TestCase):
    """The browser-side already-applied detection is critical for
    avoiding double-submits. Give it explicit coverage."""

    def test_recognized_phrases(self):
        from browser_apply import _looks_already_applied
        for s in [
            "You have already applied for this issue.",
            "Application form already exists.",
            "Duplicate application detected.",
            "You already submitted this form.",
        ]:
            self.assertTrue(_looks_already_applied(s), s)

    def test_negatives(self):
        from browser_apply import _looks_already_applied
        for s in ["", None, "Apply now", "All accounts"]:
            self.assertFalse(_looks_already_applied(s), s)


if __name__ == "__main__":
    unittest.main()
