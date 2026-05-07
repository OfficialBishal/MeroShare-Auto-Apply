"""Tests for the dependency-free parts of accounts.py."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import accounts
from tests._keystore_fake import FakeKeystoreMixin


class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(accounts.slugify("Default Account", set()), "default-account")

    def test_dedup(self):
        existing = {"default-account"}
        self.assertEqual(
            accounts.slugify("Default Account", existing),
            "default-account-2",
        )

    def test_empty_name_falls_back(self):
        self.assertEqual(accounts.slugify("???", set()), "account")

    def test_strips_special_chars(self):
        self.assertEqual(
            accounts.slugify("Wife's Account!", set()),
            "wife-s-account",
        )


class _IsolatedFs(FakeKeystoreMixin):
    """Mixin: redirect ACCOUNTS_FILE / ENV_FILE to a tmpdir AND swap in
    an in-memory keystore so no test touches the real Keychain."""

    def setUp(self):
        super().setUp()  # FakeKeystoreMixin patches secrets_store
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.accounts_file = self.tmp_path / "accounts.json"
        self.env_file = self.tmp_path / ".env"
        self._patches = [
            mock.patch.object(accounts, "ACCOUNTS_FILE", self.accounts_file),
            mock.patch.object(accounts, "ENV_FILE", self.env_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        super().tearDown()


class MigrationTests(_IsolatedFs, unittest.TestCase):
    def test_no_env_no_accounts_returns_empty(self):
        self.assertEqual(accounts.load(), [])

    def test_migrates_env_to_accounts_json(self):
        self.env_file.write_text(
            "MEROSHARE_DP_ID=10600\n"
            "MEROSHARE_USERNAME=user1\n"
            "MEROSHARE_PASSWORD=pw1\n"
            "MEROSHARE_CRN=CB123\n"
            "MEROSHARE_PIN=1234\n"
        )
        result = accounts.load()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "default")
        self.assertEqual(result[0]["name"], "Default Account")
        self.assertEqual(result[0]["dp_id"], "10600")
        self.assertTrue(self.accounts_file.exists())

    def test_migration_skipped_when_accounts_file_exists(self):
        self.env_file.write_text(
            "MEROSHARE_DP_ID=10600\nMEROSHARE_USERNAME=user1\n"
            "MEROSHARE_PASSWORD=pw1\nMEROSHARE_CRN=CB123\nMEROSHARE_PIN=1234\n"
        )
        self.accounts_file.write_text(json.dumps({"accounts": [
            {"id": "real", "name": "Real", "dp_id": "1", "username": "1",
             "password": "1", "crn": "1", "pin": "1"}
        ]}))
        result = accounts.load()
        self.assertEqual(result[0]["id"], "real")

    def test_partial_env_does_not_migrate(self):
        self.env_file.write_text("MEROSHARE_DP_ID=10600\n")
        self.assertEqual(accounts.load(), [])
        self.assertFalse(self.accounts_file.exists())

    def test_corrupt_accounts_json_returns_empty(self):
        self.accounts_file.write_text("not json {{{")
        self.assertEqual(accounts.load(), [])

    def test_unexpected_shape_returns_empty(self):
        self.accounts_file.write_text("[]")  # valid JSON, wrong shape
        self.assertEqual(accounts.load(), [])


class AppliedIssuesTests(_IsolatedFs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.applied_file = self.tmp_path / ".applied_issues.json"
        self.applied_lock = self.tmp_path / ".applied_issues.lock"
        self._applied_patch = mock.patch.object(accounts, "APPLIED_FILE", self.applied_file)
        self._lock_patch = mock.patch.object(accounts, "APPLIED_LOCK_FILE", self.applied_lock)
        self._applied_patch.start()
        self._lock_patch.start()

    def tearDown(self):
        self._lock_patch.stop()
        self._applied_patch.stop()
        super().tearDown()

    def test_legacy_flat_shape_migrates(self):
        self.applied_file.write_text(json.dumps({
            "123": {"applied_at": "2026-01-01", "company": "Acme"},
            "456": {"applied_at": "2026-01-02"},
        }))
        result = accounts.load_applied()
        self.assertEqual(result, {
            "default": {
                "123": {"applied_at": "2026-01-01", "company": "Acme"},
                "456": {"applied_at": "2026-01-02"},
            },
        })

    def test_v2_envelope_loads(self):
        self.applied_file.write_text(json.dumps({
            "_schema_version": 2,
            "accounts": {"acct1": {"123": {"applied_at": "2026-01-01"}}},
        }))
        self.assertEqual(
            accounts.load_applied(),
            {"acct1": {"123": {"applied_at": "2026-01-01"}}},
        )

    def test_empty_dict_returns_empty(self):
        self.applied_file.write_text("{}")
        self.assertEqual(accounts.load_applied(), {})

    def test_corrupt_json_returns_empty(self):
        self.applied_file.write_text("not json")
        self.assertEqual(accounts.load_applied(), {})

    def test_list_top_level_returns_empty(self):
        # Wrong shape (list instead of dict). Must not crash with AttributeError.
        self.applied_file.write_text("[]")
        self.assertEqual(accounts.load_applied(), {})

    def test_unknown_future_version_returns_empty(self):
        # Newer format we don't understand: refuse to interpret rather than
        # destroy data.
        self.applied_file.write_text(json.dumps({
            "_schema_version": 99,
            "accounts": {"x": {"1": {}}},
        }))
        self.assertEqual(accounts.load_applied(), {})

    def test_unversioned_non_legacy_returns_empty(self):
        # A dict that isn't the legacy flat shape and has no version: treat
        # as empty rather than guess.
        self.applied_file.write_text(json.dumps({"random": "junk"}))
        self.assertEqual(accounts.load_applied(), {})

    def test_save_writes_versioned_envelope(self):
        accounts.save_applied({"x": {"1": {"applied_at": "now"}}})
        raw = json.loads(self.applied_file.read_text())
        self.assertEqual(raw["_schema_version"], 2)
        self.assertEqual(raw["accounts"], {"x": {"1": {"applied_at": "now"}}})

    def test_save_then_load_roundtrip(self):
        accounts.save_applied({"x": {"1": {"applied_at": "now"}}})
        self.assertEqual(
            accounts.load_applied(),
            {"x": {"1": {"applied_at": "now"}}},
        )

    def test_save_uses_atomic_write(self):
        # After save_applied, no .tmp sibling should remain.
        accounts.save_applied({"a": {}})
        self.assertFalse(self.applied_file.with_suffix(".json.tmp").exists())

    def test_v2_envelope_strips_schema_version_from_accounts(self):
        # Manual file edit could put _schema_version at both levels.
        # The inner one must be filtered out so it doesn't appear as an
        # account-id with weird issue-records.
        self.applied_file.write_text(json.dumps({
            "_schema_version": 2,
            "accounts": {
                "real": {"123": {"applied_at": "2026-01-01"}},
                "_schema_version": {"weird": "leak"},
            },
        }))
        result = accounts.load_applied()
        self.assertIn("real", result)
        self.assertNotIn("_schema_version", result)


class CrudTests(_IsolatedFs, unittest.TestCase):
    _next_username = 1000

    def _sample(self, name="A"):
        # Each call gets a unique username so duplicate-credential
        # detection doesn't reject tests that just want unique accounts
        # by name.
        CrudTests._next_username += 1
        return {"name": name, "dp_id": "10600", "username": f"u{CrudTests._next_username}",
                "password": "p", "crn": "c", "pin": "1234"}

    def test_add_assigns_id(self):
        rec = accounts.add(self._sample("Wife"))
        self.assertEqual(rec["id"], "wife")
        self.assertEqual(rec["name"], "Wife")

    def test_add_missing_field_raises(self):
        bad = self._sample()
        del bad["pin"]
        with self.assertRaises(accounts.AccountError):
            accounts.add(bad)

    def test_update_does_not_overwrite_with_blank(self):
        accounts.add(self._sample("A"))
        accounts.update("a", {"name": "A2"})
        rec = accounts.get("a")
        self.assertEqual(rec["name"], "A2")
        self.assertEqual(rec["password"], "p")

    def test_delete_removes(self):
        accounts.add(self._sample("X"))
        accounts.delete("x")
        self.assertEqual(accounts.load(), [])

    def test_mask_hides_password_and_pin(self):
        # Fixed-width placeholder leaks neither length nor first char.
        masked = accounts.mask({"id": "a", "name": "A", "dp_id": "1",
                                "username": "u", "password": "secret",
                                "crn": "c", "pin": "1234"})
        self.assertEqual(masked["password"], accounts.MASKED_PLACEHOLDER)
        self.assertEqual(masked["pin"], accounts.MASKED_PLACEHOLDER)
        self.assertEqual(masked["username"], "u")

    def test_update_rejects_mask_value_at_source(self):
        # Even if a caller bypasses the Flask route's mask-strip, update()
        # must not stomp the real password with the masked placeholder.
        accounts.add(self._sample("Z"))
        accounts.update("z", {
            "password": accounts.MASKED_PLACEHOLDER,
            "pin": accounts.MASKED_PLACEHOLDER,
        })
        rec2 = accounts.get("z")
        self.assertEqual(rec2["password"], "p")  # untouched
        self.assertEqual(rec2["pin"], "1234")  # untouched

    def test_update_accepts_real_password_that_shares_first_char_and_length(self):
        # Regression: the previous "first char + asterisks" mask made
        # update() silently drop a legitimate password rotation when the
        # new value happened to share the old value's first char and
        # length. With a fixed-width placeholder, only the literal
        # placeholder is skipped.
        accounts.add(self._sample("Y"))
        accounts.update("y", {"password": "p9999999"})
        self.assertEqual(accounts.get("y")["password"], "p9999999")
        accounts.update("y", {"pin": "9999"})
        self.assertEqual(accounts.get("y")["pin"], "9999")

    def test_update_ignores_id_field(self):
        accounts.add(self._sample("Q"))
        accounts.update("q", {"id": "hijacked", "name": "Q renamed"})
        rec = accounts.get("q")
        self.assertEqual(rec["id"], "q")  # id stays
        self.assertEqual(rec["name"], "Q renamed")

    def test_get_nonexistent_raises(self):
        with self.assertRaises(accounts.AccountError):
            accounts.get("does-not-exist")

    def test_default_kitta_persists_when_in_bounds(self):
        sample = self._sample("Primary")
        sample["default_kitta"] = 50
        rec = accounts.add(sample)
        self.assertEqual(rec["default_kitta"], 50)
        self.assertEqual(accounts.get("primary")["default_kitta"], 50)

    def test_default_kitta_blank_means_use_global(self):
        # Blank/None must default to None on disk so auto_apply.py can
        # distinguish "use global" from a stored 0.
        sample = self._sample("NoKitta")
        sample["default_kitta"] = ""
        rec = accounts.add(sample)
        self.assertIsNone(rec["default_kitta"])

    def test_default_kitta_rejects_below_min(self):
        sample = self._sample("BadKitta")
        sample["default_kitta"] = 0
        with self.assertRaises(accounts.AccountError):
            accounts.add(sample)

    def test_default_kitta_rejects_above_max(self):
        sample = self._sample("HugeKitta")
        sample["default_kitta"] = 100_001
        with self.assertRaises(accounts.AccountError):
            accounts.add(sample)

    def test_default_kitta_rejects_non_integer(self):
        sample = self._sample("StrKitta")
        sample["default_kitta"] = "abc"
        with self.assertRaises(accounts.AccountError):
            accounts.add(sample)

    def test_update_clears_default_kitta_with_empty_string(self):
        sample = self._sample("Clear")
        sample["default_kitta"] = 30
        accounts.add(sample)
        accounts.update("clear", {"default_kitta": ""})
        self.assertIsNone(accounts.get("clear")["default_kitta"])

    def test_update_changes_default_kitta(self):
        sample = self._sample("Bump")
        sample["default_kitta"] = 10
        accounts.add(sample)
        accounts.update("bump", {"default_kitta": 75})
        self.assertEqual(accounts.get("bump")["default_kitta"], 75)

    def test_delete_nonexistent_raises(self):
        with self.assertRaises(accounts.AccountError):
            accounts.delete("does-not-exist")

    def test_rejects_duplicate_name_case_insensitive(self):
        # Name uniqueness prevents slug collision on delete-and-re-add
        # (the freed slug would re-link to any orphaned applied-issues
        # state under that id).
        accounts.add(self._sample("Wife"))
        with self.assertRaises(accounts.AccountError):
            accounts.add(self._sample("Wife"))
        with self.assertRaises(accounts.AccountError):
            accounts.add(self._sample("WIFE"))  # case-insensitive

    def test_slug_dedup_still_works_for_distinct_names(self):
        # The slug machinery still dedupes when names slugify to the
        # same string but differ in punctuation/case after stripping.
        # Two names that BOTH slugify to "wife-account" but differ in
        # display string are allowed.
        accounts.add(self._sample("Wife Account"))
        accounts.add(self._sample("Wife--Account"))
        ids = sorted(a["id"] for a in accounts.load())
        self.assertEqual(ids, ["wife-account", "wife-account-2"])

    def test_rejects_duplicate_dp_and_boid(self):
        # Same MeroShare account stored twice under different display
        # names is almost always a mistake. Refuse it.
        first = self._sample("Mine")
        accounts.add(first)
        dup = {**self._sample("Spouse"), "dp_id": first["dp_id"], "username": first["username"]}
        with self.assertRaises(accounts.AccountError) as ctx:
            accounts.add(dup)
        self.assertIn("DP", str(ctx.exception))

    def test_rejects_non_numeric_dp_id(self):
        bad = self._sample("X")
        bad["dp_id"] = "abc"
        with self.assertRaises(accounts.AccountError):
            accounts.add(bad)

    def test_rejects_short_pin(self):
        bad = self._sample("X")
        bad["pin"] = "12"
        with self.assertRaises(accounts.AccountError):
            accounts.add(bad)

    def test_rejects_blank_name(self):
        bad = self._sample("   ")
        with self.assertRaises(accounts.AccountError):
            accounts.add(bad)

    def test_slug_caps_at_max_length(self):
        # Slug cap is defense-in-depth for legacy/migrated names that
        # bypass the 60-char display-name validator. Test slugify
        # directly with a long input.
        slug = accounts.slugify("x" * 200, set())
        self.assertLessEqual(len(slug), accounts.SLUG_MAX_LEN)

    def test_rejects_name_over_60_chars(self):
        bad = self._sample("X")
        bad["name"] = "y" * 61
        with self.assertRaises(accounts.AccountError):
            accounts.add(bad)

    def test_delete_cascades_applied_state(self):
        # After deleting an account, its entry in .applied_issues.json
        # must also vanish; otherwise re-adding the same name would
        # resurrect old applies via slug collision.
        rec = accounts.add(self._sample("Casc"))
        accounts.save_applied({rec["id"]: {"123": {"applied_at": "now"}}})
        accounts.delete(rec["id"])
        self.assertNotIn(rec["id"], accounts.load_applied())

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes only")
    def test_save_writes_with_restrictive_mode(self):
        # accounts.json holds plaintext credentials; default umask gives
        # 0o644 which is world-readable. Make sure save tightens to 0o600.
        accounts.add(self._sample("M"))
        mode = self.accounts_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes only")
    def test_load_tightens_legacy_loose_permissions(self):
        # A pre-existing accounts.json written before the 0o600 enforcement
        # should be tightened on the next read, not left exposed.
        self.accounts_file.write_text(json.dumps({"accounts": []}))
        os.chmod(self.accounts_file, 0o644)
        accounts.load()
        mode = self.accounts_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_concurrent_add_does_not_lose_writes(self):
        # Without the lock around load -> append -> save_all, two
        # concurrent add() calls can both see the same baseline and the
        # later save_all clobbers the earlier one's record.
        def add_one(name):
            accounts.add({**self._sample(name), "name": name})

        threads = [
            threading.Thread(target=add_one, args=(f"User{i}",))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(accounts.load()), 8)

    def test_load_filters_malformed_entries(self):
        # Manually written file with mixed valid/invalid entries.
        self.accounts_file.write_text(json.dumps({"accounts": [
            {"id": "ok", "name": "OK", "dp_id": "1", "username": "u",
             "password": "p", "crn": "c", "pin": "1"},
            None,
            42,
            {"name": "missing-id"},
            {"id": "missing-name"},
        ]}))
        result = accounts.load()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "ok")


class V1MigrationWritebackTests(_IsolatedFs, unittest.TestCase):
    """v1 -> v2 migration must persist on first read so subsequent
    invocations see the v2 envelope and downstream tools (manual
    file edits, debugging) aren't surprised by a stale shape.
    """
    def setUp(self):
        super().setUp()
        self.applied_file = self.tmp_path / ".applied_issues.json"
        self.applied_lock = self.tmp_path / ".applied_issues.lock"
        self._applied_patch = mock.patch.object(accounts, "APPLIED_FILE", self.applied_file)
        self._lock_patch = mock.patch.object(accounts, "APPLIED_LOCK_FILE", self.applied_lock)
        self._applied_patch.start()
        self._lock_patch.start()

    def tearDown(self):
        self._lock_patch.stop()
        self._applied_patch.stop()
        super().tearDown()

    def test_v1_migration_writes_back_v2(self):
        self.applied_file.write_text(json.dumps({
            "123": {"applied_at": "2026-01-01", "company": "Acme"},
        }))
        accounts.load_applied()
        # File should now be v2 envelope.
        raw = json.loads(self.applied_file.read_text())
        self.assertEqual(raw.get("_schema_version"), 2)
        self.assertIn("default", raw["accounts"])
        self.assertIn("123", raw["accounts"]["default"])


class UpdateAppliedTests(_IsolatedFs, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.applied_file = self.tmp_path / ".applied_issues.json"
        self.applied_lock = self.tmp_path / ".applied_issues.lock"
        self._applied_patch = mock.patch.object(accounts, "APPLIED_FILE", self.applied_file)
        self._lock_patch = mock.patch.object(accounts, "APPLIED_LOCK_FILE", self.applied_lock)
        self._applied_patch.start()
        self._lock_patch.start()

    def tearDown(self):
        self._lock_patch.stop()
        self._applied_patch.stop()
        super().tearDown()

    def test_update_applied_runs_mutator_and_persists(self):
        accounts.save_applied({"acct1": {"123": {"applied_at": "old"}}})

        def add_456(state):
            state.setdefault("acct1", {})["456"] = {"applied_at": "new"}

        result = accounts.update_applied(add_456)
        self.assertEqual(set(result["acct1"].keys()), {"123", "456"})
        # Round-trip through disk.
        reread = accounts.load_applied()
        self.assertEqual(set(reread["acct1"].keys()), {"123", "456"})

    def test_update_applied_handles_missing_account(self):
        # Mutator that pops a non-existent issue should be a no-op,
        # not crash.
        def pop_unknown(state):
            bucket = state.get("does-not-exist")
            if isinstance(bucket, dict):
                bucket.pop("anything", None)

        accounts.update_applied(pop_unknown)  # should not raise
        self.assertEqual(accounts.load_applied(), {})


class MaskTests(unittest.TestCase):
    def test_mask_uses_fixed_width_placeholder(self):
        # Length and first-char must NOT leak.
        m1 = accounts.mask({"password": "a"})
        m2 = accounts.mask({"password": "averylongpassword12345"})
        self.assertEqual(m1["password"], accounts.MASKED_PLACEHOLDER)
        self.assertEqual(m2["password"], accounts.MASKED_PLACEHOLDER)
        self.assertEqual(m1["password"], m2["password"])

    def test_mask_skips_empty_values(self):
        m = accounts.mask({"password": "", "pin": None})
        self.assertEqual(m.get("password"), "")
        self.assertIsNone(m.get("pin"))


class StateDirResolutionTests(unittest.TestCase):
    """_resolve_state_dir picks where on disk state files live.

    Tests cover the three resolution branches: env-var override,
    macOS Application Support default, and the dev-mode fallback.
    """

    def test_env_var_override_takes_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "custom"
            with mock.patch.dict(os.environ, {"MEROSHARE_DATA_DIR": str(target)}):
                resolved = accounts._resolve_state_dir()
            # Compare resolved vs resolved: macOS aliases /var to
            # /private/var, which the resolution path normalises.
            self.assertEqual(resolved, target.resolve())
            self.assertTrue(target.exists())

    def test_env_var_unwritable_falls_back(self):
        # An OSError on the user-supplied path must not crash. Force
        # the OSError via a Path.mkdir patch — the previous version
        # of this test relied on a guaranteed-unwritable path string
        # which mkdir(parents=True) happily created instead of
        # rejecting.
        with mock.patch.dict(os.environ, {"MEROSHARE_DATA_DIR": "/tmp/whatever"}), \
                mock.patch.object(Path, "mkdir",
                                  side_effect=PermissionError("denied")):
            with mock.patch("accounts.logger.warning"):
                resolved = accounts._resolve_state_dir()
        # The function should have fallen through to SOURCE_DIR (or
        # Application Support on darwin which also raises in our mock).
        # Either way, NOT the env-var path.
        self.assertNotEqual(str(resolved), "/tmp/whatever")


class MigrationTests(unittest.TestCase):
    """_migrate_dev_state moves dev-mode state into Application Support
    on first resolution so dev and bundled installs share one location.
    """

    def test_moves_files_when_target_empty(self):
        with tempfile.TemporaryDirectory() as legacy_d, \
                tempfile.TemporaryDirectory() as target_d:
            legacy = Path(legacy_d)
            target = Path(target_d)
            (legacy / "accounts.json").write_text('{"accounts": []}')
            (legacy / "config.json").write_text('{}')
            moved = accounts._migrate_dev_state(legacy, target)
            self.assertTrue(moved)
            self.assertTrue((target / "accounts.json").exists())
            self.assertTrue((target / "config.json").exists())
            self.assertFalse((legacy / "accounts.json").exists())

    def test_skips_when_target_already_has_state(self):
        with tempfile.TemporaryDirectory() as legacy_d, \
                tempfile.TemporaryDirectory() as target_d:
            legacy = Path(legacy_d)
            target = Path(target_d)
            (legacy / "accounts.json").write_text('legacy')
            (target / "accounts.json").write_text('target')
            moved = accounts._migrate_dev_state(legacy, target)
            self.assertFalse(moved)
            self.assertEqual((target / "accounts.json").read_text(), "target")
            self.assertEqual((legacy / "accounts.json").read_text(), "legacy")

    def test_skips_when_legacy_empty(self):
        with tempfile.TemporaryDirectory() as legacy_d, \
                tempfile.TemporaryDirectory() as target_d:
            legacy = Path(legacy_d)
            target = Path(target_d)
            self.assertFalse(accounts._migrate_dev_state(legacy, target))

    def test_no_op_when_legacy_equals_target(self):
        # Bullet-proof against the dev-mode case where SOURCE_DIR is
        # the state dir; calling migration with src==dst would otherwise
        # try to shutil.move a file onto itself.
        with tempfile.TemporaryDirectory() as d:
            same = Path(d)
            (same / "accounts.json").write_text("x")
            self.assertFalse(accounts._migrate_dev_state(same, same))
            self.assertTrue((same / "accounts.json").exists())


if __name__ == "__main__":
    unittest.main()
