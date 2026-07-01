"""File-backed secrets survive keychain loss (the brew-upgrade data-loss fix)."""

import stat

import accounts
import secrets_store


def test_secrets_persist_in_file_without_keychain(tmp_path, monkeypatch):
    # Point the secrets file at a temp dir and disable the keychain mirror, so we
    # exercise the file backend alone — the exact state after an ad-hoc rebuild
    # loses keychain access.
    monkeypatch.setattr(accounts, "STATE_DIR", tmp_path)
    monkeypatch.setattr(secrets_store, "_ensure_keyring", lambda: None)

    assert secrets_store.set("acct1", "password", "hunter2") is True
    assert secrets_store.get("acct1", "password") == "hunter2"

    # The file exists and is 0600.
    p = tmp_path / ".secrets.json"
    assert p.exists()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # It must not contain the value in a world-readable location — perms above
    # are the guarantee; here we just confirm it round-trips.

    # Simulate a fresh app build (no in-memory state, no keychain): the value
    # must still come back from the file. This is exactly what used to vanish.
    secrets_store._reset_cache_for_tests()
    assert secrets_store.get("acct1", "password") == "hunter2"

    # Delete removes it from the file.
    assert secrets_store.delete("acct1", "password") is True
    assert secrets_store.get("acct1", "password") is None


def test_is_available_true_with_writable_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "STATE_DIR", tmp_path)
    assert secrets_store.is_available() is True


def test_set_refuses_to_overwrite_an_unreadable_file(tmp_path, monkeypatch):
    # A present-but-corrupt secrets file must NOT be atomically overwritten by a
    # single-key write — that would wipe every other account's secrets + the AES
    # key. set() must fail closed and leave the file untouched.
    monkeypatch.setattr(accounts, "STATE_DIR", tmp_path)
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(secrets_store, "_ensure_keyring", lambda: None)

    assert secrets_store.set("a", "password", "keepme") is True
    (tmp_path / ".secrets.json").write_text("{ not valid json")  # corrupt it

    assert secrets_store.set("b", "password", "new") is False
    # The corrupt file is left exactly as-is (not replaced with {"b.password": ...}).
    assert (tmp_path / ".secrets.json").read_text() == "{ not valid json"
