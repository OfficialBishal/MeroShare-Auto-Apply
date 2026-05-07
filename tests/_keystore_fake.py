"""In-memory fake for secrets_store, used by every test that touches accounts.

Critical safety net: without this, a test that calls `accounts.add({...})`
would write to the real macOS Keychain / Linux Secret Service / Windows
Credential Manager, leaving stale entries the user has to clean up by
hand. The fake plugs in via mock.patch.object on secrets_store and
behaves identically (same return values, same is_available story) but
keeps every byte in a per-test dict that vanishes at tearDown.

Usage in a test class:

    from tests._keystore_fake import patch_keystore

    def setUp(self):
        self.keystore_patch = patch_keystore(self)
        self.keystore_patch.start()

    def tearDown(self):
        self.keystore_patch.stop()

Or via the _IsolatedFs mixin which already wires it.
"""
from __future__ import annotations

import contextlib
from unittest import mock

import secrets_store


class FakeKeystore:
    """Dict-backed stand-in for the OS keystore."""

    def __init__(self, available: bool = True):
        self.available = available
        self.store: dict[tuple[str, str], str] = {}

    def is_available(self):
        return self.available

    def set(self, account_id: str, field: str, value: str) -> bool:
        if not self.available:
            return False
        self.store[(account_id, field)] = str(value)
        return True

    def get(self, account_id: str, field: str):
        if not self.available:
            return None
        return self.store.get((account_id, field))

    def delete(self, account_id: str, field: str) -> bool:
        if not self.available:
            return False
        return self.store.pop((account_id, field), None) is not None

    def delete_all_for(self, account_id: str, fields):
        for f in fields:
            self.delete(account_id, f)


@contextlib.contextmanager
def keystore_context(available: bool = True) -> "FakeKeystore":
    """Yield a fresh FakeKeystore patched onto secrets_store for the
    duration of the with-block. Use in standalone test functions; the
    class-based mixin below handles setUp/tearDown."""
    fake = FakeKeystore(available=available)
    patches = [
        mock.patch.object(secrets_store, "is_available", fake.is_available),
        mock.patch.object(secrets_store, "set", fake.set),
        mock.patch.object(secrets_store, "get", fake.get),
        mock.patch.object(secrets_store, "delete", fake.delete),
        mock.patch.object(secrets_store, "delete_all_for", fake.delete_all_for),
    ]
    for p in patches:
        p.start()
    try:
        yield fake
    finally:
        for p in patches:
            p.stop()


class FakeKeystoreMixin:
    """Mixin for unittest.TestCase. Patches secrets_store with a fresh
    FakeKeystore in setUp; restores in tearDown. Every test in the
    class gets a clean store so cross-test contamination is impossible.

    Subclass-set `KEYSTORE_AVAILABLE = False` to test the legacy
    plaintext-fallback paths.
    """
    KEYSTORE_AVAILABLE = True

    def setUp(self):  # noqa: N802 (unittest convention)
        super().setUp()
        self.fake_keystore = FakeKeystore(available=self.KEYSTORE_AVAILABLE)
        self._keystore_patches = [
            mock.patch.object(secrets_store, "is_available", self.fake_keystore.is_available),
            mock.patch.object(secrets_store, "set", self.fake_keystore.set),
            mock.patch.object(secrets_store, "get", self.fake_keystore.get),
            mock.patch.object(secrets_store, "delete", self.fake_keystore.delete),
            mock.patch.object(secrets_store, "delete_all_for", self.fake_keystore.delete_all_for),
        ]
        for p in self._keystore_patches:
            p.start()

    def tearDown(self):  # noqa: N802
        for p in self._keystore_patches:
            p.stop()
        super().tearDown()
