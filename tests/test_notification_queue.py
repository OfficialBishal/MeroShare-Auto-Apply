"""Tests for the cross-process notification queue.

The queue exists so the Flask process (detached from the .app bundle)
can still produce notifications with the proper app icon: it appends
to a JSONL file, the menubar process drains and fires rumps.notification
where the bundle context produces the right icon.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import accounts
import notification_queue


class NotificationQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patch = mock.patch.object(
            accounts, "STATE_DIR", self.tmp_path,
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_enqueue_then_drain_returns_entries_in_order(self):
        notification_queue.enqueue("First title", "First body")
        notification_queue.enqueue("Second title", "Second body")
        out = notification_queue.drain()
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "First title")
        self.assertEqual(out[1]["title"], "Second title")

    def test_drain_truncates_so_subsequent_drain_is_empty(self):
        notification_queue.enqueue("Once", "Body")
        notification_queue.drain()  # consumes
        out = notification_queue.drain()
        self.assertEqual(out, [])

    def test_drain_drops_stale_entries(self):
        # Stale = older than max_age_s. The use case: app crashed
        # yesterday, queue file survived; we shouldn't replay
        # yesterday's "Share Applied!" toasts on restart.
        path = self.tmp_path / ".notify-queue.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hand-write a stale entry with a known-old timestamp.
        old_ts = int(time.time()) - 3600  # 1 hour ago
        path.write_text(
            '{"title":"Old","message":"Stale","ts":' + str(old_ts) + ',"max_age_s":60}\n',
            encoding="utf-8",
        )
        notification_queue.enqueue("Fresh", "New")
        out = notification_queue.drain()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Fresh")

    def test_drain_handles_corrupt_lines_gracefully(self):
        # A malformed JSONL line (e.g. partial write from a crashed
        # writer) must not stop the drain.
        path = self.tmp_path / ".notify-queue.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'not valid json\n'
            '{"title":"Good","message":"OK","ts":' + str(int(time.time())) + '}\n'
            '{partial line\n',
            encoding="utf-8",
        )
        out = notification_queue.drain()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Good")

    def test_drain_returns_empty_when_no_file(self):
        # Fresh install, no queue file ever written.
        out = notification_queue.drain()
        self.assertEqual(out, [])

    def test_enqueue_creates_file_with_restrictive_perms(self):
        # Notification messages can include account names — same
        # 0o600 protection as accounts.json / applied_issues.json.
        notification_queue.enqueue("X", "Y")
        path = self.tmp_path / ".notify-queue.jsonl"
        self.assertTrue(path.exists())
        # mode & 0o777 isolates the perms bits from the file-type bits
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600,
                         f"queue file mode is {oct(mode)}, expected 0o600")

    def test_menubar_alive_returns_bool(self):
        # We don't try to assert true/false (depends on host state);
        # just that the helper returns a bool without raising.
        result = notification_queue.menubar_alive()
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
