"""Tests for the dependency-free parts of scheduler.py."""
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import scheduler
from scheduler import VALID_INTERVALS, _parse_last_run, _render_plist


class RenderPlistTests(unittest.TestCase):
    """`_render_plist` looks up `venv/bin/python3` or `../python/bin/python3`
    on disk so the launchd plist points at a real interpreter. CI runners
    don't have either (pip-install into system Python), so we substitute
    the running test interpreter — guaranteed to exist — via a patch on
    `_resolve_plist_python`.
    """

    def setUp(self):
        # Tests exercise plist rendering, not interpreter lookup. Pin the
        # path to sys.executable so the lookup never fails on CI.
        self._patch = mock.patch.object(
            scheduler, "_resolve_plist_python",
            return_value=Path(sys.executable),
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_produces_valid_xml(self):
        for hours in VALID_INTERVALS:
            with self.subTest(hours=hours):
                plist_xml = _render_plist(hours)
                ET.fromstring(plist_xml)

    def test_interval_in_seconds(self):
        plist_xml = _render_plist(6)
        self.assertIn("<integer>21600</integer>", plist_xml)
        plist_xml = _render_plist(1)
        self.assertIn("<integer>3600</integer>", plist_xml)

    def test_label_present(self):
        plist_xml = _render_plist(6)
        self.assertIn("<string>com.meroshare.autoapply</string>", plist_xml)

    def test_rejects_invalid_interval(self):
        # Range now 1..24. Anything outside must still raise.
        with self.assertRaises(ValueError):
            _render_plist(0)
        with self.assertRaises(ValueError):
            _render_plist(25)
        with self.assertRaises(ValueError):
            _render_plist(-1)

    def test_accepts_arbitrary_hour_in_range(self):
        # Previously only (1, 3, 6, 12, 24) were allowed. Now any whole
        # hour 1..24 should render valid plist XML.
        for h in (2, 4, 5, 7, 8, 9, 10, 11, 13, 14, 18, 23):
            with self.subTest(h=h):
                _render_plist(h)  # would raise on invalid


SAMPLE_LOG_WITH_APPLY = """\
2026-04-25 10:00:00 [INFO] Some unrelated startup line
2026-04-29 10:00:00 [INFO] ============================================================
2026-04-29 10:00:00 [INFO] Checking for new issues at 2026-04-29 10:00:00
2026-04-29 10:00:01 [INFO] Found 2 open issue(s).
2026-04-29 10:00:05 [INFO] Applied for: Acme Hydropower
2026-04-29 12:00:00 [INFO] Some later unrelated line
"""

SAMPLE_LOG_NO_APPLY = """\
2026-04-29 10:00:00 [INFO] Checking for new issues at 2026-04-29 10:00:00
2026-04-29 10:00:01 [INFO] No open issues found.
2026-04-29 10:00:01 [INFO] No new applications made this run.
"""

SAMPLE_LOG_NO_SUMMARY = """\
2026-04-29 10:00:00 [INFO] Checking for new issues at 2026-04-29 10:00:00
2026-04-29 10:00:01 [ERROR] Login failed. Check credentials in .env
"""

# Multi-account refactor changed the log line shape from
# "Checking for new issues at ..." to "Checking <account> at ...".
SAMPLE_LOG_MULTI_ACCOUNT = """\
2026-04-29 10:00:00 [INFO] Checking Mine at 2026-04-29 10:00:00
2026-04-29 10:00:01 [INFO] No new applications made this run.
2026-04-29 10:00:02 [INFO] Checking Wife's Account at 2026-04-29 10:00:02
2026-04-29 10:00:05 [INFO] Applied for: Acme Hydropower (Wife's Account)
"""


class ParseLastRunTests(unittest.TestCase):
    def _write(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def test_returns_none_when_log_missing(self):
        ts, summary = _parse_last_run(Path("/nonexistent/path.log"))
        self.assertIsNone(ts)
        self.assertIsNone(summary)

    def test_parses_apply_summary(self):
        log_path = self._write(SAMPLE_LOG_WITH_APPLY)
        ts, summary = _parse_last_run(log_path)
        self.assertEqual(ts, "2026-04-29 10:00:00")
        self.assertEqual(summary, "Applied for: Acme Hydropower")

    def test_parses_no_apply_summary(self):
        log_path = self._write(SAMPLE_LOG_NO_APPLY)
        ts, summary = _parse_last_run(log_path)
        self.assertEqual(ts, "2026-04-29 10:00:00")
        self.assertEqual(summary, "No new applications made this run.")

    def test_no_summary_returns_none_summary(self):
        log_path = self._write(SAMPLE_LOG_NO_SUMMARY)
        ts, summary = _parse_last_run(log_path)
        self.assertEqual(ts, "2026-04-29 10:00:00")
        self.assertIsNone(summary)

    def test_parses_multi_account_log_format(self):
        # Regression: the regex must match "Checking <name> at ..." too,
        # not only the legacy "Checking for new issues at ...".
        log_path = self._write(SAMPLE_LOG_MULTI_ACCOUNT)
        ts, summary = _parse_last_run(log_path)
        # The most-recent check is for "Wife's Account" at 10:00:02.
        self.assertEqual(ts, "2026-04-29 10:00:02")
        self.assertIn("Applied for", summary)


if __name__ == "__main__":
    unittest.main()
