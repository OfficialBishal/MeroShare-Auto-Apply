"""Cross-process apply mutex: no two apply runs overlap (no double-submit)."""

import threading

import accounts
import auto_apply


def _isolate(tmp_path, monkeypatch):
    """Give this test a private lockfile + a fresh in-process lock so a lingering
    apply thread from an earlier test can't make these assertions flaky."""
    monkeypatch.setattr(accounts, "APPLY_ENGINE_LOCK_FILE", tmp_path / ".apply-engine.lock")
    monkeypatch.setattr(accounts, "_apply_engine_thread_lock", threading.Lock())


def test_apply_engine_lock_is_mutually_exclusive(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    with accounts.try_apply_engine_lock() as first:
        assert first is True
        with accounts.try_apply_engine_lock() as second:
            assert second is False  # already held
    # released — reacquirable
    with accounts.try_apply_engine_lock() as again:
        assert again is True


def test_check_and_apply_skips_when_engine_locked(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    with accounts.try_apply_engine_lock() as held:
        assert held is True
        results: dict = {}
        out = auto_apply.check_and_apply(
            {"share_types": {}, "auto_apply": {}}, results=results
        )
        assert out == []
        assert "_skipped" in results  # surfaced to the GUI, not silently dropped


def test_dry_run_bypasses_the_mutex(tmp_path, monkeypatch):
    # A dry run never submits, so a held lock must not block it.
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(accounts, "load", lambda: [])  # no accounts -> no network
    with accounts.try_apply_engine_lock() as held:
        assert held is True
        results: dict = {}
        out = auto_apply.check_and_apply(
            {"share_types": {}, "auto_apply": {}}, dry_run=True, results=results
        )
        assert out == []
        assert "_skipped" not in results  # reached the impl, wasn't blocked
