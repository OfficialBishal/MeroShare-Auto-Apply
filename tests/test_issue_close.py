"""_issue_is_closed: conservative close-date filtering for /api/issues."""

import app


def test_clearly_past_close_is_closed():
    assert app._issue_is_closed({"issueCloseDate": "2020-01-01T15:00:00"}) is True


def test_clearly_future_close_is_open():
    assert app._issue_is_closed({"issueCloseDate": "2099-01-01T15:00:00"}) is False


def test_missing_close_date_is_open():
    assert app._issue_is_closed({}) is False
    assert app._issue_is_closed({"issueCloseDate": None}) is False
    assert app._issue_is_closed({"issueCloseDate": ""}) is False


def test_unparseable_close_date_keeps_issue_open():
    # Never hide an issue we can't confidently classify as closed.
    assert app._issue_is_closed({"issueCloseDate": "sometime soon"}) is False


def test_date_only_close_treated_as_end_of_day():
    assert app._issue_is_closed({"issueCloseDate": "2020-06-30"}) is True
    assert app._issue_is_closed({"issueCloseDate": "2099-06-30"}) is False


def test_space_separated_close_date_parses():
    assert app._issue_is_closed({"issueCloseDate": "2020-06-30 14:30:00"}) is True
