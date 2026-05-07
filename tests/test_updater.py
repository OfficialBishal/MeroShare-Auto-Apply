"""Tests for updater.py.

Covers version comparison, asset selection, and the network paths
(HTTP error, 404 no-releases, rate-limit, malformed JSON). All
mocked via unittest.mock so the suite stays offline.
"""
import unittest
from unittest import mock

import requests

import updater


class ParseVersionTests(unittest.TestCase):
    def test_calver(self):
        self.assertEqual(updater._parse_version("2026.05.04"), (2026, 5, 4))

    def test_strips_leading_v(self):
        self.assertEqual(updater._parse_version("v2026.05.04"), (2026, 5, 4))
        self.assertEqual(updater._parse_version("V2026.05.04"), (2026, 5, 4))

    def test_strips_plus_suffix(self):
        # `+dev` is the development sentinel; the comparison key drops it.
        self.assertEqual(updater._parse_version("0.0.0+dev"), (0, 0, 0))
        self.assertEqual(
            updater._parse_version("2026.05.04+meta"), (2026, 5, 4),
        )

    def test_handles_extra_components(self):
        self.assertEqual(updater._parse_version("2026.05.04.1"), (2026, 5, 4, 1))

    def test_non_numeric_compares_as_zero(self):
        # Garbage in shouldn't crash; out comes a tuple that compares.
        self.assertEqual(updater._parse_version("abc.def"), (0, 0))

    def test_ordering(self):
        # The whole point: tuple comparison must give the right order.
        self.assertLess(
            updater._parse_version("2026.05.04"),
            updater._parse_version("2026.05.05"),
        )
        self.assertLess(
            updater._parse_version("2025.12.31"),
            updater._parse_version("2026.01.01"),
        )
        self.assertEqual(
            updater._parse_version("2026.05.04"),
            updater._parse_version("v2026.05.04"),
        )


class IsDevBuildTests(unittest.TestCase):
    def test_recognizes_dev_sentinel(self):
        self.assertTrue(updater.is_dev_build("0.0.0+dev"))
        self.assertTrue(updater.is_dev_build("2026.05.04+dev"))

    def test_released_versions_are_not_dev(self):
        self.assertFalse(updater.is_dev_build("2026.05.04"))
        self.assertFalse(updater.is_dev_build("v2026.05.04"))


class PickAssetUrlTests(unittest.TestCase):
    def test_returns_none_with_no_dmgs(self):
        assets = [{"name": "src.zip", "browser_download_url": "x"}]
        self.assertIsNone(updater._pick_asset_url(assets))

    def test_picks_only_dmg_when_one(self):
        assets = [
            {"name": "MeroShare-Auto-Apply.dmg", "browser_download_url": "the-url"},
            {"name": "windows.zip", "browser_download_url": "z"},
        ]
        self.assertEqual(updater._pick_asset_url(assets), "the-url")

    def test_prefers_arch_match(self):
        # Multiple .dmgs. Pick the one matching the host arch.
        assets = [
            {"name": "MeroShare-x86_64.dmg", "browser_download_url": "x86"},
            {"name": "MeroShare-arm64.dmg", "browser_download_url": "arm"},
        ]
        with mock.patch("updater.platform.machine", return_value="arm64"):
            self.assertEqual(updater._pick_asset_url(assets), "arm")
        with mock.patch("updater.platform.machine", return_value="x86_64"):
            self.assertEqual(updater._pick_asset_url(assets), "x86")

    def test_falls_back_to_first_dmg_on_no_match(self):
        assets = [
            {"name": "MeroShare-x86_64.dmg", "browser_download_url": "x86"},
        ]
        with mock.patch("updater.platform.machine", return_value="arm64"):
            self.assertEqual(updater._pick_asset_url(assets), "x86")


class _FakeResp:
    def __init__(self, status_code=200, json_body=None, raise_on_json=False):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = json_body
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._body or {}


class CheckForUpdatesTests(unittest.TestCase):
    def _release(self, tag="v2026.05.10", assets=None, body="notes"):
        return _FakeResp(200, {
            "tag_name": tag,
            "html_url": f"https://github.com/x/y/releases/tag/{tag}",
            "body": body,
            "assets": assets or [],
        })

    def test_returns_release_when_newer(self):
        sess = mock.MagicMock()
        sess.get.return_value = self._release(
            tag="v2026.05.10",
            assets=[{"name": "MeroShare-Auto-Apply.dmg", "browser_download_url": "the-url"}],
            body="something new",
        )
        info = updater.check_for_updates("2026.05.04", session=sess)
        self.assertIsNotNone(info)
        self.assertEqual(info["version"], "2026.05.10")
        self.assertEqual(info["asset_url"], "the-url")
        self.assertIn("something new", info["notes"])

    def test_returns_none_when_up_to_date(self):
        sess = mock.MagicMock()
        sess.get.return_value = self._release(tag="v2026.05.04")
        self.assertIsNone(
            updater.check_for_updates("2026.05.04", session=sess),
        )

    def test_returns_none_when_older(self):
        sess = mock.MagicMock()
        sess.get.return_value = self._release(tag="v2026.05.04")
        self.assertIsNone(
            updater.check_for_updates("2026.05.10", session=sess),
        )

    def test_dev_build_treats_release_as_newer(self):
        # The dev sentinel is "behind" any release. A manual check
        # from a dev build should still return release info so the
        # user knows what's out there.
        sess = mock.MagicMock()
        sess.get.return_value = self._release(tag="v2026.05.04")
        info = updater.check_for_updates("0.0.0+dev", session=sess)
        self.assertIsNotNone(info)
        self.assertEqual(info["version"], "2026.05.04")

    def test_returns_none_on_404_no_releases_yet(self):
        sess = mock.MagicMock()
        sess.get.return_value = _FakeResp(404, {})
        self.assertIsNone(
            updater.check_for_updates("2026.05.04", session=sess),
        )

    def test_returns_none_on_403_rate_limited(self):
        sess = mock.MagicMock()
        sess.get.return_value = _FakeResp(403, {})
        self.assertIsNone(
            updater.check_for_updates("2026.05.04", session=sess),
        )

    def test_returns_none_on_malformed_json(self):
        sess = mock.MagicMock()
        sess.get.return_value = _FakeResp(200, raise_on_json=True)
        self.assertIsNone(
            updater.check_for_updates("2026.05.04", session=sess),
        )

    def test_returns_none_on_connection_error(self):
        sess = mock.MagicMock()
        sess.get.side_effect = requests.ConnectionError("nope")
        self.assertIsNone(
            updater.check_for_updates("2026.05.04", session=sess),
        )

    def test_returns_none_when_tag_name_missing(self):
        sess = mock.MagicMock()
        sess.get.return_value = _FakeResp(200, {"html_url": "x"})
        self.assertIsNone(
            updater.check_for_updates("2026.05.04", session=sess),
        )

    def test_passes_user_agent_and_accept_header(self):
        # GitHub's API expects a User-Agent and accepts the json+v3
        # MIME type. Verify we send both. Easy regression spot if
        # someone refactors the headers dict.
        sess = mock.MagicMock()
        sess.get.return_value = self._release(tag="v2026.05.10")
        updater.check_for_updates("2026.05.04", session=sess)
        kwargs = sess.get.call_args.kwargs
        headers = kwargs.get("headers") or {}
        self.assertIn("User-Agent", headers)
        self.assertIn("github", headers.get("Accept", "").lower())


if __name__ == "__main__":
    unittest.main()
