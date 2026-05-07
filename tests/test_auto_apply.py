"""Tests for auto_apply.py CLI helpers and the disk-save safety net.

Network-dependent flows (check_and_apply, list_issues, show_status)
are exercised at the integration boundary instead of mocked here -
the goal of this file is the small pure helpers that previous
versions covered with no tests at all.
"""
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import accounts
import auto_apply
from tests._keystore_fake import FakeKeystoreMixin


class _IsolatedFs(FakeKeystoreMixin):
    """Same shape as tests/test_accounts.py. Keep CRUD off the real
    user's accounts.json AND off the system keychain."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(accounts, "ACCOUNTS_FILE", self.tmp_path / "accounts.json"),
            mock.patch.object(accounts, "APPLIED_FILE", self.tmp_path / ".applied_issues.json"),
            mock.patch.object(accounts, "APPLIED_LOCK_FILE", self.tmp_path / ".applied_issues.lock"),
            mock.patch.object(accounts, "ENV_FILE", self.tmp_path / ".env"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        super().tearDown()


def _sample_account(name="A", username="u1"):
    return {
        "name": name, "dp_id": "10600", "username": username,
        "password": "p", "crn": "c", "pin": "1234",
    }


class ResolveAccountTests(_IsolatedFs, unittest.TestCase):
    def test_returns_first_when_no_id_given(self):
        accounts.add(_sample_account("First", "u1"))
        accounts.add(_sample_account("Second", "u2"))
        rec = auto_apply._resolve_account(None)
        self.assertEqual(rec["name"], "First")

    def test_returns_specific_account_by_id(self):
        accounts.add(_sample_account("First", "u1"))
        accounts.add(_sample_account("Second", "u2"))
        rec = auto_apply._resolve_account("second")
        self.assertEqual(rec["name"], "Second")

    def test_returns_none_when_id_unknown(self):
        accounts.add(_sample_account("First", "u1"))
        with self.assertLogs("meroshare", level="ERROR"):
            rec = auto_apply._resolve_account("ghost")
        self.assertIsNone(rec)

    def test_returns_none_when_no_accounts(self):
        with self.assertLogs("meroshare", level="ERROR"):
            rec = auto_apply._resolve_account(None)
        self.assertIsNone(rec)


class SafeSaveAppliedTests(_IsolatedFs, unittest.TestCase):
    def test_returns_true_on_success(self):
        ok = auto_apply._safe_save_applied({"acct": {"1": {"applied_at": "now"}}})
        self.assertTrue(ok)
        # And it was actually persisted.
        self.assertEqual(
            accounts.load_applied(),
            {"acct": {"1": {"applied_at": "now"}}},
        )

    def test_returns_false_and_logs_on_oserror(self):
        # Patch save_applied to raise. Covers the disk-full path
        # without needing a real readonly fs.
        with mock.patch.object(auto_apply, "save_applied",
                               side_effect=OSError("disk full")):
            with self.assertLogs("meroshare", level="ERROR"):
                ok = auto_apply._safe_save_applied({"x": {}}, context="test")
        self.assertFalse(ok)


class NotifyDedupTests(unittest.TestCase):
    """notify() dedups identical (title, message) pairs within
    _NOTIFY_DEDUP_WINDOW_S seconds. Verify the lock keeps it
    thread-safe under concurrent callers."""

    def setUp(self):
        # Flush dedup state between tests.
        with auto_apply._notify_lock:
            auto_apply._recent_notifications.clear()

    def _disable_macos_subprocess(self):
        # notify() shells out to osascript on macOS; mock it so the
        # test doesn't actually pop a system notification.
        return mock.patch.object(auto_apply.subprocess, "run")

    def test_first_call_records(self):
        with self._disable_macos_subprocess():
            auto_apply.notify("T", "M")
        self.assertIn(("T", "M"), auto_apply._recent_notifications)

    def test_second_within_window_is_skipped(self):
        with self._disable_macos_subprocess() as run_mock:
            auto_apply.notify("T", "M")
            first_calls = run_mock.call_count
            auto_apply.notify("T", "M")  # within window
            self.assertEqual(run_mock.call_count, first_calls)

    def test_concurrent_callers_dont_corrupt_state(self):
        # 50 threads firing identical notify() concurrently. The
        # dict-mutation-during-iteration crash this lock prevents would
        # show up here under load.
        import threading
        with self._disable_macos_subprocess():
            errors = []

            def worker(i):
                try:
                    auto_apply.notify(f"T-{i % 5}", f"M-{i % 5}")
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])


class NormalizeAppliedDateTests(unittest.TestCase):
    def test_passes_through_iso(self):
        s = "2026-05-04T02:30:00+05:45"
        self.assertEqual(auto_apply._normalize_applied_date(s), s)

    def test_converts_meroshare_space_format(self):
        out = auto_apply._normalize_applied_date("2026-05-04 02:30:00")
        # Round-trip parseable as ISO.
        from datetime import datetime as _dt
        _dt.fromisoformat(out)
        self.assertIn("2026-05-04T02:30:00", out)

    def test_naive_input_uses_nepal_timezone(self):
        # MeroShare returns Nepal-local timestamps without TZ. Verify
        # we tag them as +05:45 regardless of where the daemon runs.
        out = auto_apply._normalize_applied_date("2026-05-04 02:30:00")
        self.assertIn("+05:45", out)

    def test_converts_slash_format(self):
        out = auto_apply._normalize_applied_date("2026/05/04 02:30:00")
        self.assertIn("2026-05-04T02:30:00", out)
        self.assertIn("+05:45", out)

    def test_falls_back_to_now_on_garbage(self):
        out = auto_apply._normalize_applied_date("not a date")
        # Falls through to ISO now. Must be parseable.
        from datetime import datetime as _dt
        _dt.fromisoformat(out)

    def test_handles_none_and_empty(self):
        from datetime import datetime as _dt
        for v in (None, "", "   "):
            out = auto_apply._normalize_applied_date(v)
            _dt.fromisoformat(out)

    def test_meroshare_tz_env_var(self):
        # The override exists for advanced users who run MeroShare's
        # mirror in a different region.
        with mock.patch.dict(os.environ, {"MEROSHARE_TZ": "UTC"}):
            out = auto_apply._normalize_applied_date("2026-05-04 02:30:00")
        # UTC tag is "+00:00" in isoformat output.
        self.assertIn("+00:00", out)


class ConfigErrorTests(unittest.TestCase):
    """load_config() must raise ConfigError instead of sys.exit so the
    Flask server doesn't get killed when app.py imports auto_apply."""

    def test_raises_configerror_on_missing_file(self):
        with mock.patch.object(auto_apply, "CONFIG_FILE",
                               Path("/no/such/file.json")):
            with self.assertRaises(auto_apply.ConfigError):
                auto_apply.load_config()

    def test_raises_configerror_on_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text("not json {{")
            with mock.patch.object(auto_apply, "CONFIG_FILE", cfg):
                with self.assertRaises(auto_apply.ConfigError):
                    auto_apply.load_config()

    def test_cli_main_exits_cleanly_on_configerror(self):
        # The CLI's main() catches ConfigError, logs it, and exits 1
        #. Without dumping a traceback. Verifies the error path stays
        # tidy for users who run `python auto_apply.py` with no config.
        with mock.patch.object(auto_apply, "load_config",
                               side_effect=auto_apply.ConfigError("missing")):
            old_argv = sys.argv
            sys.argv = ["auto_apply.py"]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    with self.assertLogs("meroshare", level="ERROR"):
                        auto_apply.main()
                self.assertEqual(ctx.exception.code, 1)
            finally:
                sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
