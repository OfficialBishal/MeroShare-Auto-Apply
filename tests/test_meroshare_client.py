"""Tests for the pure-function parts of meroshare_client.py."""
import unittest
from unittest import mock

import requests

import meroshare_client
from meroshare_client import MeroShareClient


class ConstructorTests(unittest.TestCase):
    def test_requires_credentials(self):
        # The multi-account refactor made `credentials` mandatory. A bare
        # MeroShareClient() used to silently fall back to env vars, which
        # would now read either nothing or a stale .env. Refuse loudly.
        with self.assertRaises(TypeError):
            MeroShareClient(None)
        with self.assertRaises(TypeError):
            MeroShareClient({})


class ClassifyIssueTests(unittest.TestCase):
    """classify_issue drives every auto-apply decision. Branches must be
    exercised explicitly because the substring matching is fragile."""

    def test_mutual_fund_via_group(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareGroupName": "Mutual Fund Scheme"}),
            "mutual_fund",
        )

    def test_mutual_fund_via_scheme_keyword(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareGroupName": "Some Scheme"}),
            "mutual_fund",
        )

    def test_debenture_via_type(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Debenture"}),
            "debenture",
        )

    def test_debenture_word_boundary(self):
        # "bond" must not match e.g. "abandoned"
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Bond"}),
            "debenture",
        )
        self.assertNotEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Abandoned IPO"}),
            "debenture",
        )

    def test_right_share_via_type(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Right Share"}),
            "right_share",
        )

    def test_right_share_via_reservation(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Reserved IPO"}),
            "right_share",
        )

    def test_company_name_with_right_substring_does_not_become_right_share(self):
        # The substring trap: "Bright Future Hydro" must NOT classify as
        # right_share. classify_issue must ignore companyName entirely.
        issue = {
            "companyName": "Bright Future Hydropower Limited",
            "shareTypeName": "Ordinary Shares",
            "shareGroupName": "Ordinary",
        }
        self.assertEqual(MeroShareClient.classify_issue(issue), "ipo_ordinary")

    def test_company_name_with_bond_substring_does_not_become_debenture(self):
        issue = {
            "companyName": "Vagabond Investments",
            "shareTypeName": "IPO",
        }
        self.assertEqual(MeroShareClient.classify_issue(issue), "ipo_ordinary")

    def test_fpo_via_type(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "FPO"}),
            "fpo",
        )

    def test_fpo_via_full_phrase(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Further Public Offering"}),
            "fpo",
        )

    def test_default_is_ipo_ordinary(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Ordinary Shares"}),
            "ipo_ordinary",
        )

    def test_handles_none_fields(self):
        # The MeroShare API occasionally returns None for missing fields.
        # The function lowercases via `(... or "")`. Verify no crash.
        # Returns "unknown" (not "ipo_ordinary") so the auto-apply loop
        # refuses to act on a category it can't identify.
        issue = {
            "companyName": None, "shareTypeName": None,
            "shareGroupName": None, "subGroup": None,
            "reservationTypeName": None,
        }
        self.assertEqual(MeroShareClient.classify_issue(issue), "unknown")

    def test_handles_missing_keys(self):
        self.assertEqual(MeroShareClient.classify_issue({}), "unknown")

    def test_preferred_share_classified(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Preference Share"}),
            "preferred_share",
        )

    def test_sukuk_classified_as_debenture(self):
        self.assertEqual(
            MeroShareClient.classify_issue({"shareTypeName": "Sukuk Bond"}),
            "debenture",
        )


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = ""

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _new_client():
    # MeroShareClient requires a credentials dict at construction.
    return MeroShareClient(credentials={
        "dp_id": "10600", "username": "u", "password": "p",
        "crn": "c", "pin": "1234",
    })


class RequestHelperTests(unittest.TestCase):
    """The HTTP wrapper must add timeouts and retry 5xx but NOT 4xx."""

    def test_retries_5xx_then_succeeds(self):
        client = _new_client()
        responses = [_FakeResponse(503), _FakeResponse(200, {"ok": True})]
        with mock.patch.object(client.session, "request",
                               side_effect=responses) as m, \
             mock.patch.object(meroshare_client.time, "sleep"):
            resp = client._request("GET", "https://x.example/y", retries=2)
        self.assertEqual(resp.status_code, 200)
        # All call kwargs must include `timeout`. The whole point of
        # the wrapper.
        for call in m.call_args_list:
            self.assertIn("timeout", call.kwargs)

    def test_does_not_retry_4xx(self):
        client = _new_client()
        responses = [_FakeResponse(400)]
        with mock.patch.object(client.session, "request",
                               side_effect=responses) as m, \
             mock.patch.object(meroshare_client.time, "sleep"):
            resp = client._request("GET", "https://x.example/y", retries=3)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(m.call_count, 1)

    def test_retries_on_connection_error_then_raises(self):
        client = _new_client()
        with mock.patch.object(
            client.session, "request",
            side_effect=requests.ConnectionError("nope"),
        ) as m, mock.patch.object(meroshare_client.time, "sleep"):
            with self.assertRaises(requests.ConnectionError):
                client._request("GET", "https://x.example/y", retries=2)
        # 1 initial + 2 retries
        self.assertEqual(m.call_count, 3)


class PagedPostTests(unittest.TestCase):
    """_paged_post must walk pages until a short page or totalNoOfRows."""

    def test_stops_on_short_page(self):
        client = _new_client()
        page1 = _FakeResponse(200, {"object": [{"id": i} for i in range(client._PAGE_SIZE)]})
        page2 = _FakeResponse(200, {"object": [{"id": 999}]})  # short
        with mock.patch.object(client, "_request",
                               side_effect=[page1, page2]) as m, \
             mock.patch.object(meroshare_client.time, "sleep"):
            results = client._paged_post("https://x.example/p", {})
        self.assertEqual(len(results), client._PAGE_SIZE + 1)
        self.assertEqual(m.call_count, 2)

    def test_stops_on_total_rows(self):
        client = _new_client()
        page1 = _FakeResponse(200, {
            "object": [{"id": i} for i in range(client._PAGE_SIZE)],
            "totalNoOfRows": client._PAGE_SIZE,
        })
        with mock.patch.object(client, "_request",
                               side_effect=[page1]) as m, \
             mock.patch.object(meroshare_client.time, "sleep"):
            results = client._paged_post("https://x.example/p", {})
        self.assertEqual(len(results), client._PAGE_SIZE)
        self.assertEqual(m.call_count, 1)

    def test_caps_at_max_pages_with_warning(self):
        client = _new_client()
        # Always return a full page so the loop never short-circuits;
        # we should stop at _MAX_PAGES with a warning instead of looping
        # forever.
        full_page = _FakeResponse(200, {
            "object": [{"id": i} for i in range(client._PAGE_SIZE)],
        })
        with mock.patch.object(client, "_request",
                               return_value=full_page) as m, \
             mock.patch.object(meroshare_client.time, "sleep"), \
             self.assertLogs("meroshare", level="WARNING") as logs:
            results = client._paged_post("https://x.example/p", {})
        self.assertEqual(m.call_count, client._MAX_PAGES)
        self.assertEqual(len(results), client._MAX_PAGES * client._PAGE_SIZE)
        self.assertTrue(
            any("Pagination cap" in msg for msg in logs.output),
            "expected a 'Pagination cap' warning",
        )


if __name__ == "__main__":
    unittest.main()
