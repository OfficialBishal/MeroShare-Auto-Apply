"""Tests for the secrets_store + accounts.py keystore integration.

Uses the in-memory FakeKeystore so no test touches the real OS keychain.
Also covers the legacy plaintext-fallback path (KEYSTORE_AVAILABLE=False)
since that's the install-time degradation we promise still works.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import accounts
import secrets_store
from tests._keystore_fake import FakeKeystoreMixin


class _AccountsFs(FakeKeystoreMixin):
    """Mixin: tmpdir for accounts.json + the in-memory keystore fake."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(accounts, "ACCOUNTS_FILE", self.tmp_path / "accounts.json"),
            mock.patch.object(accounts, "ENV_FILE", self.tmp_path / ".env"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        super().tearDown()


def _sample(name="A", username="u1"):
    return {
        "name": name, "dp_id": "10600", "username": username,
        "password": "p", "crn": "c", "pin": "1234",
    }


class KeystoreOnAddTests(_AccountsFs, unittest.TestCase):
    """When the keystore is available, sensitive fields must NOT land
    in the on-disk accounts.json."""

    def test_sensitive_fields_absent_from_disk(self):
        accounts.add(_sample("A"))
        # File is encrypted at-rest now too; decrypt to look inside.
        # _decrypt_payload_from_disk reads the keystore for the file
        # key, which the fake provides during the test.
        raw = (self.tmp_path / "accounts.json").read_text()
        decoded = accounts._decrypt_payload_from_disk(raw)
        rec = decoded["accounts"][0]
        for k in ("password", "crn", "pin"):
            self.assertNotIn(k, rec, f"sensitive field {k} leaked into JSON")
        self.assertTrue(rec[accounts._SECRETS_FLAG])

    def test_sensitive_fields_resolve_on_load(self):
        accounts.add(_sample("A"))
        loaded = accounts.load()
        self.assertEqual(loaded[0]["password"], "p")
        self.assertEqual(loaded[0]["pin"], "1234")
        self.assertEqual(loaded[0]["crn"], "c")

    def test_keystore_sees_each_secret_under_account_id(self):
        accounts.add(_sample("Bishal"))
        # FakeKeystore stores under (account_id, field).
        self.assertEqual(self.fake_keystore.get("bishal", "password"), "p")
        self.assertEqual(self.fake_keystore.get("bishal", "pin"), "1234")
        self.assertEqual(self.fake_keystore.get("bishal", "crn"), "c")

    def test_delete_cascades_to_keystore(self):
        accounts.add(_sample("Z"))
        self.assertIsNotNone(self.fake_keystore.get("z", "password"))
        accounts.delete("z")
        for k in ("password", "crn", "pin"):
            self.assertIsNone(
                self.fake_keystore.get("z", k),
                f"{k} stranded in keystore after delete",
            )

    def test_deleting_last_account_purges_file_encryption_key(self):
        # Regression: smoke test against the real macOS Keychain caught
        # a stale `_meroshare_meta.file_encryption_key` entry left
        # behind after the only account was deleted via the API.
        # Surfaces as an unexplained "com.meroshare.autoapply" item in
        # Keychain Access that the user has no UI to clean up.
        accounts.add(_sample("Solo"))
        self.assertIsNotNone(
            self.fake_keystore.get(
                secrets_store.META_ACCOUNT, secrets_store.META_FILE_KEY,
            ),
            "file-encryption key should exist after add",
        )
        accounts.delete("solo")
        self.assertIsNone(
            self.fake_keystore.get(
                secrets_store.META_ACCOUNT, secrets_store.META_FILE_KEY,
            ),
            "file-encryption key stranded in keystore after last account "
            "was deleted",
        )

    def test_deleting_one_of_many_keeps_file_encryption_key(self):
        # The flip side: deleting one of several accounts must NOT
        # purge the file-encryption key, or the next load() can't
        # decrypt the file holding the surviving accounts.
        accounts.add(_sample("Keep", username="k"))
        accounts.add(_sample("Drop", username="d"))
        accounts.delete("drop")
        self.assertIsNotNone(
            self.fake_keystore.get(
                secrets_store.META_ACCOUNT, secrets_store.META_FILE_KEY,
            ),
            "file-encryption key dropped while other accounts still need it",
        )
        # And the surviving account is still readable.
        loaded = accounts.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "Keep")

    def test_update_writes_new_password_to_keystore(self):
        accounts.add(_sample("X"))
        accounts.update("x", {"password": "newpw9"})
        self.assertEqual(self.fake_keystore.get("x", "password"), "newpw9")
        # And the file still doesn't contain the new password.
        raw = (self.tmp_path / "accounts.json").read_text()
        self.assertNotIn("newpw9", raw)


class KeystoreUnavailableFallbackTests(_AccountsFs, unittest.TestCase):
    """With KEYSTORE_AVAILABLE=False, legacy plaintext-in-JSON path must
    still work — that's the headless / no-DBus install we don't want to
    break."""
    KEYSTORE_AVAILABLE = False

    def test_plaintext_path_still_writes_credentials(self):
        accounts.add(_sample("Legacy"))
        loaded = accounts.load()
        self.assertEqual(loaded[0]["password"], "p")
        # File is plaintext (no encryption envelope) and contains the
        # credentials — this is the deliberate degraded mode.
        raw = (self.tmp_path / "accounts.json").read_text()
        self.assertIn('"password": "p"', raw)
        self.assertNotIn("_encryption", raw)


class PlaintextMigrationTests(_AccountsFs, unittest.TestCase):
    """A user upgrading from a pre-keystore install must have their
    plaintext accounts.json migrated transparently on first load —
    secrets move into the keystore, file gets encrypted."""

    def test_upgrade_migrates_plaintext_record(self):
        # Hand-write a legacy plaintext accounts.json to simulate an
        # upgrade from a pre-secrets_store version.
        legacy = {
            "accounts": [{
                "id": "legacy", "name": "Legacy",
                "dp_id": "10600", "username": "u1",
                "password": "old_pw", "crn": "OLDCRN", "pin": "9999",
                "preferred_bank": None, "preferred_bank_account": None,
                "default_kitta": None,
            }],
        }
        (self.tmp_path / "accounts.json").write_text(json.dumps(legacy))
        # First load triggers migration: secrets to keystore, file
        # rewritten under encryption envelope.
        loaded = accounts.load()
        self.assertEqual(loaded[0]["password"], "old_pw")
        self.assertEqual(self.fake_keystore.get("legacy", "password"), "old_pw")
        # File body is now encrypted (envelope present, no plaintext leaks).
        raw = (self.tmp_path / "accounts.json").read_text()
        self.assertIn("_encryption", raw)
        for needle in ("old_pw", "OLDCRN", "9999"):
            self.assertNotIn(needle, raw, f"{needle!r} survived migration in plaintext")

    def test_migration_idempotent(self):
        accounts.add(_sample("Stable"))
        before = (self.tmp_path / "accounts.json").read_text()
        accounts.load()
        accounts.load()
        after = (self.tmp_path / "accounts.json").read_text()
        # Re-loads should not rewrite the file (no plaintext to migrate
        # and the envelope is already there). We compare the parsed
        # decrypted contents to dodge nonce-rotation false-positives:
        # two encrypts of the same plaintext use different nonces.
        self.assertEqual(
            accounts._decrypt_payload_from_disk(before),
            accounts._decrypt_payload_from_disk(after),
        )


class FileEncryptionTests(_AccountsFs, unittest.TestCase):
    """The AES-GCM envelope must hide every plaintext byte AND must
    fail loud rather than silently dropping data when the key isn't
    available."""

    def test_metadata_is_hidden_in_envelope(self):
        accounts.add({
            "name": "Bishal Modern", "dp_id": "10600", "username": "boid42",
            "password": "p", "crn": "c", "pin": "1234",
            "preferred_bank": "NIC ASIA",
        })
        raw = (self.tmp_path / "accounts.json").read_text()
        # Even non-credential metadata (account name, BOID, DEMAT,
        # bank preference) must not appear in the on-disk text.
        for needle in ("Bishal Modern", "boid42", "10600", "NIC ASIA"):
            self.assertNotIn(needle, raw, f"metadata leak: {needle!r}")

    def test_decrypt_rejects_bad_version(self):
        bad = json.dumps({
            "_encryption": {"version": 999, "algorithm": "AES-256-GCM", "nonce": "AAAA"},
            "ciphertext": "AAAA",
        })
        with self.assertRaises(ValueError):
            accounts._decrypt_payload_from_disk(bad)

    def test_load_quarantines_undecryptable_file_and_returns_empty(self):
        # Regression for the user-reported "Internal Server Error" on
        # the GUI: an encrypted accounts.json whose key is no longer
        # in the keystore used to raise cryptography.InvalidTag out
        # of accounts.load(), which propagated as a 500 from Flask's
        # `/` route. The fix must catch the decrypt failure, move the
        # bad file aside, and return [] so the GUI lands on Settings.
        accounts.add(_sample("Stranded"))
        # Drop the AES key from the keystore so the next decrypt has
        # nothing valid to work with — simulates a fresh install
        # against an inherited accounts.json (or a sequence of dist
        # builds where the keychain got reset between them).
        self.fake_keystore.store.pop(
            (secrets_store.META_ACCOUNT, secrets_store.META_FILE_KEY), None,
        )
        # load() must NOT raise; it must surface [] and quarantine
        # the file.
        try:
            result = accounts.load()
        except Exception as e:
            self.fail(f"load() raised {type(e).__name__}: {e}")
        self.assertEqual(result, [])
        self.assertFalse(accounts.ACCOUNTS_FILE.exists(),
                         "unreadable accounts.json was not moved aside; "
                         "load() will keep failing on every retry")
        # The .unreadable.* sibling MUST exist so the user can
        # diagnose / recover manually if they want to.
        siblings = list(accounts.ACCOUNTS_FILE.parent.glob("*.unreadable.*.json"))
        self.assertEqual(len(siblings), 1,
                         f"expected exactly one quarantined file, found {siblings}")

    def test_decrypt_loud_failure_when_keystore_unavailable(self):
        accounts.add(_sample("Hidden"))
        # Drop the file-encryption key. Decrypt must raise rather than
        # silently return an empty dict — losing accounts is the worst
        # possible outcome.
        self.fake_keystore.store.pop(
            (secrets_store.META_ACCOUNT, secrets_store.META_FILE_KEY), None,
        )
        # Force re-fetch — _get_or_create_file_key would otherwise
        # generate a new key, which is right for new files but wrong
        # for an existing encrypted one (it'd nuke decryption silently).
        # The current implementation does generate a new key; that's
        # the correct behavior because the file body decrypt will then
        # fail on tag verification, raising InvalidTag (a cryptography
        # library exception that subclasses Exception, not ValueError).
        # Catch the broad cryptography error class explicitly.
        from cryptography.exceptions import InvalidTag
        raw = (self.tmp_path / "accounts.json").read_text()
        with self.assertRaises(InvalidTag):
            accounts._decrypt_payload_from_disk(raw)


class SecretsStoreSmokeTests(unittest.TestCase):
    """The shape of the public API. Doesn't require a real keystore."""

    def test_module_constants(self):
        self.assertEqual(secrets_store.SERVICE, "com.meroshare.autoapply")
        self.assertTrue(callable(secrets_store.is_available))
        self.assertTrue(callable(secrets_store.set))
        self.assertTrue(callable(secrets_store.get))
        self.assertTrue(callable(secrets_store.delete))
        self.assertTrue(callable(secrets_store.delete_all_for))


if __name__ == "__main__":
    unittest.main()
