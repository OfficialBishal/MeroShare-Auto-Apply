"""Tests for menubar.py pure helpers.

The rumps event loop and Cocoa NSStatusItem are out of scope (they
require a running app + display server). What we cover here is the
plumbing that runs without rumps state: format helpers, plist
escaping, the launch-agent installer's idempotency, and the dialog
fallback dispatch.

Module-level imports of menubar pull in rumps, which is mac-only.
Skip the whole module on non-darwin so CI's Linux runners stay green.
"""
import sys
import unittest

if sys.platform != "darwin":
    raise unittest.SkipTest("menubar.py is macOS-only")


import os
import tempfile
from pathlib import Path
from unittest import mock

import accounts
import menubar


class FormatRelativeTests(unittest.TestCase):
    def test_returns_unknown_on_empty(self):
        self.assertEqual(menubar._format_relative(None), "unknown")
        self.assertEqual(menubar._format_relative(""), "unknown")

    def test_returns_just_now_for_recent(self):
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.assertEqual(menubar._format_relative(recent), "just now")

    def test_returns_minutes_ago(self):
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        self.assertEqual(menubar._format_relative(past), "5m ago")

    def test_unparseable_returns_input(self):
        # Garbage in, garbage out, but not a crash.
        self.assertEqual(menubar._format_relative("garbage"), "garbage")

    def test_future_timestamp_uses_in_prefix(self):
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
        result = menubar._format_relative(future)
        self.assertTrue(result.startswith("in "), f"got {result!r}")

    def test_naive_iso_uses_nepal_timezone(self):
        # Match the convention in auto_apply._normalize_applied_date:
        # naive ISO inputs are interpreted as Asia/Kathmandu, not as
        # the daemon machine's local TZ. Without this, "5h ago" on a
        # US/EU host would be off by the local-vs-NPT offset.
        from datetime import datetime, timedelta, timezone
        npt = timezone(timedelta(hours=5, minutes=45))
        # Build an ISO string an hour ago in NPT, strip the tzinfo.
        naive = (datetime.now(npt) - timedelta(hours=1)).replace(tzinfo=None).isoformat(timespec="seconds")
        self.assertEqual(menubar._format_relative(naive), "1h ago")


class FormatAmountTests(unittest.TestCase):
    def test_renders_thousands_separator(self):
        self.assertEqual(menubar._format_amount(100000), "Rs. 100,000")

    def test_zero_means_no_cap(self):
        self.assertEqual(menubar._format_amount(0), "no cap")

    def test_none_means_no_cap(self):
        self.assertEqual(menubar._format_amount(None), "no cap")

    def test_handles_string_input(self):
        # Hand-edited config.json could land a string here. Don't crash.
        self.assertEqual(menubar._format_amount("not a number"), "not a number")


class IntervalLabelTests(unittest.TestCase):
    def test_singular(self):
        self.assertEqual(menubar._interval_label(1), "Every hour")

    def test_plural(self):
        self.assertEqual(menubar._interval_label(6), "Every 6 hours")
        self.assertEqual(menubar._interval_label(24), "Every 24 hours")


class LaunchAgentPlistTests(unittest.TestCase):
    """The plist generator builds XML by string interpolation. Verify
    that user-controllable fields (paths) get XML-escaped so a path
    containing < / > / & doesn't produce an unparseable plist."""

    def test_escapes_xml_special_chars_in_path(self):
        # Run with a fake state dir containing an `&` character;
        # the resulting plist must still parse.
        from plistlib import loads as plist_loads

        with tempfile.TemporaryDirectory() as d:
            fake_state = Path(d) / "Foo & Bar"
            fake_state.mkdir()
            with mock.patch.object(accounts, "STATE_DIR", fake_state), \
                    mock.patch.object(menubar, "LAUNCH_AGENT_PATH",
                                      Path(d) / "agent.plist"), \
                    mock.patch.object(menubar.subprocess, "run") as mock_run, \
                    mock.patch.object(menubar.rumps, "notification"):
                mock_run.return_value = mock.Mock(returncode=0)
                menubar._launch_at_login_enable()
                content = (Path(d) / "agent.plist").read_bytes()
            # plistlib raises ExpatError if the XML is malformed.
            parsed = plist_loads(content)
            # The escaped & survives parsing as a literal in the value.
            env = parsed.get("EnvironmentVariables") or {}
            self.assertIn("&", env.get("MEROSHARE_DATA_DIR", ""))

    def test_idempotent_unloads_existing_before_install(self):
        # Second call to _enable should unload, then load.
        with tempfile.TemporaryDirectory() as d:
            agent_path = Path(d) / "agent.plist"
            with mock.patch.object(menubar, "LAUNCH_AGENT_PATH", agent_path), \
                    mock.patch.object(menubar.subprocess, "run") as mock_run, \
                    mock.patch.object(menubar.rumps, "notification"):
                mock_run.return_value = mock.Mock(returncode=0)
                menubar._launch_at_login_enable()
                self.assertTrue(agent_path.exists())
                # Second invocation should detect the existing plist
                # and call `launchctl unload` before `load`.
                mock_run.reset_mock()
                menubar._launch_at_login_enable()
                # First call should have been an unload.
                first_args = mock_run.call_args_list[0].args[0]
                self.assertEqual(first_args[1], "unload")


class ShowDialogFallbackTests(unittest.TestCase):
    """_show_dialog primary path is osascript on darwin (rumps.alert
    silently rendered NSAlert behind the foreground app for LSUIElement
    builds; the menu would freeze with an invisible dialog). rumps
    remains a fallback when osascript itself fails (rare, but possible
    if AppleScript permissions get revoked)."""

    def test_uses_osascript_first_on_darwin(self):
        # Regression for the user-reported "About menu freezes" bug.
        # Even when rumps.alert is mocked to "succeed" silently, we
        # must NOT take that path on darwin — it's the path that
        # produces invisible dialogs in LSUIElement-true bundles.
        with mock.patch.object(menubar.threading, "current_thread",
                               return_value=menubar.threading.main_thread()), \
                mock.patch.object(menubar, "_activate_foreground"), \
                mock.patch.object(menubar.sys, "platform", "darwin"), \
                mock.patch.object(menubar.rumps, "alert") as mock_alert, \
                mock.patch.object(menubar.subprocess, "run") as mock_run:
            menubar._show_dialog("Title", "Message")
        # osascript should have been called…
        self.assertTrue(mock_run.called)
        args = mock_run.call_args.args[0]
        self.assertEqual(args[0], "osascript")
        # …and rumps.alert NOT called (its silent invisibility was
        # the original bug).
        self.assertFalse(mock_alert.called,
                         "rumps.alert called as primary path on darwin — "
                         "regression of About-menu-freezes bug")

    def test_falls_back_to_rumps_when_osascript_raises_on_darwin(self):
        # osascript can fail (binary missing in some sandboxes,
        # AppleScript permissions revoked). When that happens, rumps
        # is the fallback so the user still sees something.
        with mock.patch.object(menubar.threading, "current_thread",
                               return_value=menubar.threading.main_thread()), \
                mock.patch.object(menubar, "_activate_foreground"), \
                mock.patch.object(menubar.sys, "platform", "darwin"), \
                mock.patch.object(menubar, "_osascript_dialog",
                                  side_effect=RuntimeError("permissions")), \
                mock.patch.object(menubar.rumps, "alert") as mock_alert, \
                mock.patch.object(menubar.logger, "warning"):
            menubar._show_dialog("Title", "Message")
        self.assertTrue(mock_alert.called)

    def test_uses_rumps_directly_on_non_darwin(self):
        # On Linux / Windows, osascript doesn't exist; rumps.alert
        # is the primary path (and the only one). The osascript
        # short-circuit must not fire.
        with mock.patch.object(menubar.threading, "current_thread",
                               return_value=menubar.threading.main_thread()), \
                mock.patch.object(menubar, "_activate_foreground"), \
                mock.patch.object(menubar.sys, "platform", "linux"), \
                mock.patch.object(menubar.rumps, "alert") as mock_alert, \
                mock.patch.object(menubar.subprocess, "run") as mock_run:
            menubar._show_dialog("Title", "Message")
        self.assertTrue(mock_alert.called)
        self.assertFalse(mock_run.called)

    def test_osascript_escapes_quotes_and_backslashes(self):
        # A title or message containing " or \\ would otherwise break
        # AppleScript's string-literal parsing.
        with mock.patch.object(menubar.subprocess, "run") as mock_run:
            menubar._osascript_dialog('Has "quotes"', 'Has \\backslash')
        args = mock_run.call_args.args[0]
        script = args[2]
        # The literal " in our input must NOT appear unescaped.
        self.assertNotIn('"quotes"', script)
        self.assertIn('\\"quotes\\"', script)


class AutoStartFlaskTests(unittest.TestCase):
    """Auto-start runs once on launch and silently spawns Flask. Unlike
    `_ensure_flask` (the user-click path), it must NEVER show a modal
    dialog on failure — that would block login.
    """

    def _make_app(self):
        # Build an app instance without going through __init__ (which
        # would start timers, build the menu, etc.). We only need
        # `_flask_proc` and the bound methods.
        app = menubar.MeroShareMenuBar.__new__(menubar.MeroShareMenuBar)
        app._flask_proc = None
        return app

    def test_skips_when_flask_already_alive(self):
        app = self._make_app()
        with mock.patch.object(app, "_flask_alive", return_value=True), \
                mock.patch.object(menubar.subprocess, "Popen") as mock_popen, \
                mock.patch.object(menubar.time, "sleep"):
            app._auto_start_flask()
        self.assertFalse(mock_popen.called,
                         "auto-start spawned Flask while another was already running")

    def test_does_not_show_dialog_on_app_py_missing(self):
        # Login UX rule: auto-start must never block the user with a
        # modal at sign-in. Failures degrade to log-and-continue.
        app = self._make_app()
        bogus = Path("/var/empty/__definitely_does_not_exist__.py")
        with mock.patch.object(app, "_flask_alive", return_value=False), \
                mock.patch.object(menubar, "BASE_DIR", bogus.parent), \
                mock.patch.object(menubar, "_show_dialog") as mock_dialog, \
                mock.patch.object(menubar.subprocess, "Popen") as mock_popen, \
                mock.patch.object(menubar.time, "sleep"):
            app._auto_start_flask()
        self.assertFalse(mock_dialog.called,
                         "auto-start showed a blocking modal at app launch")
        self.assertFalse(mock_popen.called)

    def test_does_not_show_dialog_when_flask_doesnt_come_up(self):
        # Flask is spawned but never reaches /api/health. The retry
        # cap (15s in production, mocked here) should fall through
        # silently — the next status tick paints "stopped".
        app = self._make_app()
        with mock.patch.object(app, "_flask_alive", return_value=False), \
                mock.patch.object(menubar.Path, "exists", return_value=True), \
                mock.patch.object(menubar.subprocess, "Popen",
                                  return_value=mock.MagicMock(pid=999)), \
                mock.patch.object(menubar, "_show_dialog") as mock_dialog, \
                mock.patch.object(menubar.time, "sleep"):
            app._auto_start_flask()
        self.assertFalse(mock_dialog.called,
                         "auto-start blocked login with a modal after Flask failed to bind")


class ShowDialogDispatchTests(unittest.TestCase):
    """_show_dialog must dispatch to the main thread when called from
    a worker. NSAlert.runModal is not thread-safe and can corrupt the
    AppKit runloop if invoked off-main."""

    def test_dispatches_when_called_from_background_thread(self):
        # Simulate being on a background thread by patching the
        # main-thread check, and verify rumps.alert is NOT called
        # synchronously (it gets queued via rumps.Timer instead).
        with mock.patch.object(menubar.threading, "main_thread",
                               return_value=mock.sentinel.main), \
                mock.patch.object(menubar.threading, "current_thread",
                                  return_value=mock.sentinel.background), \
                mock.patch.object(menubar.rumps, "alert") as mock_alert, \
                mock.patch.object(menubar.rumps, "Timer") as mock_timer:
            menubar._show_dialog("title", "message")
            # Synchronous rumps.alert must not have been called from
            # the background-thread path.
            mock_alert.assert_not_called()
            # A Timer should have been scheduled to run on main.
            mock_timer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
