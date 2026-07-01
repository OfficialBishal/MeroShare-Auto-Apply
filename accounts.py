"""Multi-account credential storage for MeroShare.

Single source of truth for account records. accounts.json holds
non-credential fields (id, name, dp_id, username, preferences) and the
irrevocable secrets (password, CRN, PIN) live in the OS keystore via
secrets_store. The JSON is mode 0o600; the keystore is encrypted at
rest by macOS / Windows / Linux's Secret Service. Falls back to
plaintext-in-JSON when no keystore is reachable so headless / unusual
setups still work.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl  # POSIX only
except ImportError:  # Windows: no fcntl. Cross-process locks degrade to no-op;
    fcntl = None     # _atomic_write + the in-process locks still protect callers.

import secrets_store

logger = logging.getLogger("meroshare")

# Where the .py files live. Fixed at import time. Callers that need
# to point at the source tree (e.g. the launchd plist's script_path)
# use this; everything else uses STATE_DIR.
SOURCE_DIR = Path(__file__).resolve().parent


_STATE_FILES_FOR_MIGRATION = (
    "accounts.json",
    "config.json",
    ".applied_issues.json",
    ".capital_cache.json",
    # The lock file isn't strictly state, but leaving it behind in
    # the legacy dir keeps it from doing anything useful so move it
    # too. The shutdown sentinel is short-lived: only present
    # while a Stop-everything is in flight.
    ".applied_issues.lock",
)


def _migrate_dev_state(legacy: Path, target: Path) -> bool:
    """If the legacy (project-root) install has state files but `target`
    doesn't, move them to `target` so both run modes share one location.

    Returns True if anything was migrated. Idempotent: a second call
    finds nothing to move and returns False.
    """
    if legacy == target:
        return False
    legacy_has = any((legacy / f).exists() for f in _STATE_FILES_FOR_MIGRATION)
    target_has = any((target / f).exists() for f in _STATE_FILES_FOR_MIGRATION)
    if not legacy_has or target_has:
        return False
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in _STATE_FILES_FOR_MIGRATION:
        src = legacy / name
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(target / name))
            moved.append(name)
        except OSError as e:
            logger.warning("Could not migrate %s: %s", name, e)
    if moved:
        logger.info("Migrated state files to %s: %s", target, ", ".join(moved))
        return True
    return False


def _resolve_state_dir() -> Path:
    """Pick the directory where read/write state files live.

    Resolution order:
      1. MEROSHARE_DATA_DIR env var, when set, takes precedence.
         This is what the bundled .app launcher uses to redirect
         state into ~/Library/Application Support, but a power user
         can also point it at any writable directory.
      2. On macOS, always use ~/Library/Application Support/MeroShare
         Auto-Apply. Apple convention; means the dev `./run.sh` flow
         and the installed .app share one state location, so settings
         don't disappear when switching launch paths. Migrates any
         existing state from the project root the first time.
      3. On Windows / Linux, fall back to the project root (SOURCE_DIR)
         since there's no portable equivalent and the Windows .zip
         build runs portably from wherever it's extracted.
    """
    override = os.environ.get("MEROSHARE_DATA_DIR")
    if override:
        try:
            p = Path(override).expanduser().resolve()
            p.mkdir(parents=True, exist_ok=True)
            _migrate_dev_state(SOURCE_DIR, p)
            return p
        except OSError as e:
            logger.warning(
                "MEROSHARE_DATA_DIR=%r is not usable (%s); "
                "falling back to source dir.", override, e,
            )

    if sys.platform == "darwin":
        try:
            p = Path.home() / "Library" / "Application Support" / "MeroShare Auto-Apply"
            p.mkdir(parents=True, exist_ok=True)
            _migrate_dev_state(SOURCE_DIR, p)
            return p
        except OSError as e:
            logger.warning(
                "Could not use ~/Library/Application Support (%s); "
                "falling back to source dir.", e,
            )

    return SOURCE_DIR


STATE_DIR = _resolve_state_dir()

ACCOUNTS_FILE = STATE_DIR / "accounts.json"
ENV_FILE = STATE_DIR / ".env"
APPLIED_FILE = STATE_DIR / ".applied_issues.json"
APPLIED_LOCK_FILE = STATE_DIR / ".applied_issues.lock"
# Serializes the whole real-money apply engine across processes (the launchd
# daemon, a one-shot CLI run, the GUI's run-check worker, and the GUI's
# single-issue /api/apply). Distinct from APPLIED_LOCK_FILE, which guards only
# the applied-state file write — this one spans decide → submit → record so two
# processes can't both pass the "already applied?" check and double-submit.
APPLY_ENGINE_LOCK_FILE = STATE_DIR / ".apply-engine.lock"
# Tracks the last-seen `statusName` per applicantFormId so the GUI/daemon
# can fire desktop notifications exactly once on status transitions
# (Pending → Allotted / Not Allotted), not every poll. Map shape:
#   { "<applicantFormId>": {"status": "ALLOTED", "company": "...", "ts": <ISO>} }
ALLOTMENT_STATE_FILE = STATE_DIR / ".allotment_status.json"

CREDENTIAL_KEYS = ("dp_id", "username", "password", "crn", "pin")

# The subset of CREDENTIAL_KEYS that are "irrevocable secrets": leaking
# them lets an attacker move money in the user's name. These fields are
# stored in the OS keystore (macOS Keychain / Win Cred Mgr / Linux
# Secret Service) when one is available, and only fall back to plaintext
# in accounts.json when the keystore isn't reachable. dp_id and username
# stay in the JSON either way — they're identifiers, not credentials.
SENSITIVE_KEYS = ("password", "crn", "pin")
NON_SENSITIVE_CREDENTIAL_KEYS = tuple(k for k in CREDENTIAL_KEYS if k not in SENSITIVE_KEYS)

# Persisted records use this flag to signal that sensitive fields live
# in the keystore and are absent from the JSON file. Read paths know to
# resolve from the store; write paths know to strip before persisting.
_SECRETS_FLAG = "_secrets_in_store"

# Optional non-credential per-account fields. Stored alongside
# credentials so multi-account setups can have per-account preferences
# without a separate config file. `preferred_bank_account` lets users
# pick a specific bank account when multiple are linked at the same
# bank (otherwise the browser flow always picks index 1, which can
# fail at submit if CRN doesn't match).
OPTIONAL_KEYS = ("preferred_bank", "preferred_bank_account")

# Optional integer per-account fields. Currently just default_kitta:
# overrides the global config.auto_apply.default_kitta for *this* account
# when applying for IPOs. Lets users size their primary account at e.g.
# 50 kitta and a relative's smaller account at 10 without juggling
# global config between cycles. None means "use the global default".
OPTIONAL_INT_KEYS = ("default_kitta",)
_DEFAULT_KITTA_BOUNDS = (1, 100_000)

# Fixed-width mask: leaks neither password length nor first character.
# Used for any value displayed back to the GUI.
MASKED_PLACEHOLDER = "********"


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout: float = 10.0):
    """Cross-process advisory lock on `path`.

    Used around load -> mutate -> save_applied so the launchd-spawned
    auto_apply.py and the GUI's Run-check worker can't both load the
    same baseline and clobber each other's writes (multi-process race
    that the threading.RLock in this module doesn't guard against).

    Best-effort: on platforms without fcntl (Windows) this becomes a
    no-op. The threading lock plus _atomic_write still protects
    intra-process callers.
    """
    if fcntl is None:  # Windows / no fcntl: no cross-process lock available.
        yield
        return
    try:
        path.touch(exist_ok=True)
        fd = os.open(path, os.O_RDWR)
    except OSError as e:
        logger.debug("Could not open lock file %s: %s. Proceeding without lock", path, e)
        yield
        return
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    logger.debug("flock failed (%s). Proceeding without lock", e)
                    break
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Timed out (%ss) waiting for lock on %s. "
                        "proceeding anyway; concurrent writers may race.",
                        timeout, path,
                    )
                    break
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

# Serializes accounts.json read-modify-write across threads so two
# concurrent CRUD requests from the GUI can't race load() -> append ->
# save_all() and lose one of the writes.
_accounts_lock = threading.RLock()

# In-process half of the apply-engine mutex (guards threads within one Flask
# process; the fcntl lock below adds the cross-process half). Non-reentrant.
_apply_engine_thread_lock = threading.Lock()


@contextlib.contextmanager
def try_apply_engine_lock():
    """Non-blocking mutex for the real-money apply engine.

    Yields True if the caller may proceed to apply, or False if another
    thread/process is already applying (caller should skip/reject rather than
    risk a duplicate submission). Never blocks. On a filesystem where the
    lockfile can't be opened it degrades to the in-process lock only.
    """
    if not _apply_engine_thread_lock.acquire(blocking=False):
        yield False
        return
    fd = None
    try:
        if fcntl is None:  # Windows / no fcntl: in-process (thread) lock only.
            yield True
            return
        try:
            APPLY_ENGINE_LOCK_FILE.touch(exist_ok=True)
            fd = os.open(APPLY_ENGINE_LOCK_FILE, os.O_RDWR)
        except OSError as e:
            logger.debug("apply-engine lockfile unavailable (%s); thread lock only", e)
            yield True
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            fd = None
            yield False
            return
        yield True
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        _apply_engine_thread_lock.release()


class AccountError(Exception):
    """Raised when account operations fail."""


SLUG_MAX_LEN = 40


def slugify(name: str, existing_ids: set[str]) -> str:
    """Turn a name into a unique short id. Pure function.

    Caps at SLUG_MAX_LEN so a pathological 200-char account name
    doesn't produce a 200-char id that breaks UI layout or path
    handling downstream.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "account"
    if len(base) > SLUG_MAX_LEN:
        base = base[:SLUG_MAX_LEN].rstrip("-") or "account"
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}-{n}" in existing_ids:
        n += 1
    return f"{base}-{n}"


def _read_env_file() -> dict:
    """Read .env into a dict. Used only during one-time migration.

    `utf-8-sig` strips a UTF-8 BOM if present. A Notepad-saved .env
    would otherwise put `\\ufeffMEROSHARE_DP_ID` as the first key and
    silently skip migration.
    """
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


def _migrate_from_env() -> dict | None:
    """Build a single 'Default Account' record if .env has all five keys.

    Returns the new accounts payload, or None if migration not applicable.
    """
    env = _read_env_file()
    keys = ("MEROSHARE_DP_ID", "MEROSHARE_USERNAME", "MEROSHARE_PASSWORD",
            "MEROSHARE_CRN", "MEROSHARE_PIN")
    if not all(env.get(k) for k in keys):
        return None
    return {
        "accounts": [{
            "id": "default",
            "name": "Default Account",
            "dp_id": env["MEROSHARE_DP_ID"],
            "username": env["MEROSHARE_USERNAME"],
            "password": env["MEROSHARE_PASSWORD"],
            "crn": env["MEROSHARE_CRN"],
            "pin": env["MEROSHARE_PIN"],
        }]
    }


def _atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    """Write to a sibling .tmp then atomically rename. Prevents torn writes
    if the process is killed mid-write or two processes race on the file.

    `mode`, if given, is applied to the .tmp before the rename so the
    final file lands with restrictive permissions in one step (avoids a
    race window where another process could open the file between rename
    and chmod). Pass 0o600 for credential files.

    On failure (e.g. disk full) the .tmp is removed so it doesn't get
    overwritten silently next call.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        # When a restrictive mode is requested (credential files), create the
        # temp file with that mode from the START. The previous open()+chmod
        # left a window where the plaintext-secrets temp existed world-readable
        # under the default umask before the chmod ran.
        if mode is not None:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            try:
                os.fchmod(fd, mode)  # force even if a stale tmp pre-existed looser
            except OSError:
                pass
            f = os.fdopen(fd, "w")
        else:
            f = open(tmp, "w")
        # fsync the temp file before rename so a power-cut between
        # write and rename can't leave an empty/torn file. Without this,
        # POSIX is allowed to defer the data write past the rename.
        with f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # not all filesystems support fsync (e.g. tmpfs)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ── Encrypted-at-rest envelope for accounts.json ──────────────────────
# The JSON file holds metadata (account names, DEMAT IDs, BOIDs, preferred
# bank) — not credentials (those go through secrets_store), but still
# per-user financial info. AES-256-GCM with a per-install random key keeps
# it non-obvious on disk.
# NOTE: since the file-backed secrets_store (see its module docstring), the
# AES key now co-resides at 0600 in .secrets.json in this same directory, so
# the envelope no longer defends against an attacker who can read the data
# dir (Time Machine / stolen disk image) — it now only keeps casual `cat`
# output non-obvious. This is the deliberate trade-off that makes account
# data survive ad-hoc-signed upgrades; strong at-rest protection needs a
# stable Apple Developer ID signature (keychain-gated key).
_ENC_VERSION = 1


def _get_or_create_file_key() -> bytes | None:
    """Fetch the AES-256-GCM key for accounts.json from the keystore,
    generating a fresh 32-byte key on first call. Returns None when
    the keystore is unavailable — caller falls back to plaintext."""
    if not secrets_store.is_available():
        return None

    def _decode(raw):
        if not raw:
            return None
        try:
            k = base64.b64decode(raw)
            return k if len(k) == 32 else None
        except Exception:
            return None

    key = _decode(secrets_store.get(secrets_store.META_ACCOUNT, secrets_store.META_FILE_KEY))
    if key:
        return key
    # Generate the key atomically across processes. Without this lock, two
    # concurrent first-runs (e.g. a launchd tick coinciding with GUI startup)
    # could each mint a DIFFERENT key and clobber the other's — leaving one
    # process's accounts.json encrypted under a key that's no longer on disk,
    # i.e. undecryptable (the data loss this whole change set out to prevent).
    with _file_lock(ACCOUNTS_FILE.parent / ".secrets-key.lock"):
        key = _decode(secrets_store.get(secrets_store.META_ACCOUNT, secrets_store.META_FILE_KEY))
        if key:  # another process created it while we waited for the lock
            return key
        key = os.urandom(32)
        if not secrets_store.set(
            secrets_store.META_ACCOUNT,
            secrets_store.META_FILE_KEY,
            base64.b64encode(key).decode("ascii"),
        ):
            logger.warning(
                "accounts: could not store file-encryption key; "
                "falling back to plaintext accounts.json.",
            )
            return None
        return key


def _encrypt_payload_for_disk(payload: dict) -> str:
    """Wrap `payload` in an AES-GCM envelope, returning the on-disk text.

    Falls through to plaintext JSON when the keystore is unavailable —
    that's the legacy install path (still 0o600, still keystore-less)
    and we don't want to break it. The wrap format is itself JSON so
    the file remains debuggable with `cat`: an attacker can see THAT
    it's encrypted, just not the contents.
    """
    plaintext_bytes = json.dumps(payload, indent=2).encode("utf-8")
    key = _get_or_create_file_key()
    if key is None:
        return plaintext_bytes.decode("utf-8")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        # cryptography is in requirements.txt but a custom build with
        # it missing should still degrade gracefully rather than wedge.
        logger.warning(
            "accounts: cryptography library unavailable (%s); "
            "writing plaintext accounts.json.", e,
        )
        return plaintext_bytes.decode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext_bytes, associated_data=None)
    envelope = {
        "_encryption": {
            "version": _ENC_VERSION,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
        },
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, indent=2)


def _decrypt_payload_from_disk(raw: str) -> dict:
    """Decode whatever the file holds. Three accepted shapes:
        1. Encrypted envelope (`_encryption` key present) — decrypt.
        2. Legacy plaintext (`accounts` key) — pass through.
        3. Anything else — caller's JSON parsing already raised.
    """
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or "_encryption" not in parsed:
        return parsed  # legacy plaintext
    enc = parsed["_encryption"]
    if not isinstance(enc, dict) or enc.get("version") != _ENC_VERSION:
        raise ValueError(
            f"accounts.json uses unsupported encryption version "
            f"{enc.get('version')!r}; this build expects {_ENC_VERSION}."
        )
    if "nonce" not in enc or "ciphertext" not in parsed:
        raise ValueError("accounts.json envelope missing nonce or ciphertext")
    key = _get_or_create_file_key()
    if key is None:
        # File is encrypted but we can't get the key — most likely the
        # user moved accounts.json to a new machine without copying the
        # keystore. The right answer is "restore from backup".
        raise ValueError(
            "accounts.json is encrypted but the OS keystore key isn't "
            "available. If you moved this file from another machine, "
            "use a JSON backup instead."
        )
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ValueError(
            "accounts.json is encrypted but the cryptography library "
            "isn't installed. `pip install cryptography` and retry."
        ) from None
    nonce = base64.b64decode(enc["nonce"])
    ciphertext = base64.b64decode(parsed["ciphertext"])
    plaintext_bytes = AESGCM(key).decrypt(nonce, ciphertext, associated_data=None)
    return json.loads(plaintext_bytes.decode("utf-8"))


def _save_payload(payload: dict) -> None:
    # 0o600 keeps the file out of reach of OTHER local users. We also encrypt
    # the body, but the AES key now co-resides at 0600 in .secrets.json here
    # (file-backed secrets_store), so this protects against other users of the
    # machine, NOT against someone who can read this user's data dir. See the
    # _ENC_VERSION note above for the rationale.
    _atomic_write(ACCOUNTS_FILE, _encrypt_payload_for_disk(payload), mode=0o600)


def _to_persisted(record: dict) -> dict:
    """Strip sensitive fields from a resolved record before persisting.

    When the keystore is the source of truth, the JSON file must NOT
    also contain the secrets — that would defeat the entire purpose
    (file leaks would re-expose what the keystore was protecting).
    """
    out = dict(record)
    if out.get(_SECRETS_FLAG):
        for k in SENSITIVE_KEYS:
            out.pop(k, None)
    return out


def _resolve_secrets(record: dict) -> dict:
    """Inflate a persisted record with secrets fetched from the keystore.

    No-op for legacy plaintext records (where _SECRETS_FLAG is unset).
    Returns a new dict — never mutates the input — because the same
    persisted record may also be in the in-memory cache used by save_all.
    """
    if not record.get(_SECRETS_FLAG):
        return record
    out = dict(record)
    for k in SENSITIVE_KEYS:
        # Empty string when missing so callers that propagate the dict
        # to MeroShareClient see a missing-credential failure rather
        # than a Python KeyError stack trace.
        out[k] = secrets_store.get(record["id"], k) or ""
    return out


def _migrate_plaintext_to_store_locked(records: list[dict]) -> bool:
    """If any record stores secrets in plaintext AND the OS keystore is
    available, move them. Returns True when any record was changed
    (caller should persist).

    Caller must hold `_accounts_lock`. We don't acquire here because
    load() invokes this from inside its own lock-held section.

    Idempotent: a second call after a successful migration finds every
    record already stamped with _SECRETS_FLAG and exits cheaply.
    """
    if not secrets_store.is_available():
        return False
    changed = False
    for rec in records:
        if rec.get(_SECRETS_FLAG):
            continue
        # Skip records missing one or more secrets — they're broken,
        # don't half-migrate them. The user needs to re-enter via Edit.
        if not all(rec.get(k) for k in SENSITIVE_KEYS):
            continue
        ok = True
        for k in SENSITIVE_KEYS:
            if not secrets_store.set(rec["id"], k, rec[k]):
                ok = False
                break
        if not ok:
            # Roll back the partial keystore writes for this record so
            # we don't end up with a half-migrated state.
            for k in SENSITIVE_KEYS:
                secrets_store.delete(rec["id"], k)
            logger.warning(
                "Could not migrate account '%s' to keystore; staying plaintext.",
                rec.get("name", rec.get("id")),
            )
            continue
        for k in SENSITIVE_KEYS:
            rec.pop(k, None)
        rec[_SECRETS_FLAG] = True
        changed = True
    if changed:
        logger.info(
            "Migrated %d account(s) into the OS keystore.",
            sum(1 for r in records if r.get(_SECRETS_FLAG)),
        )
    return changed


def load() -> list[dict]:
    """Return all accounts. Migrates from .env on first call if needed.

    Filters out malformed entries. Anything that's not a dict with at
    least an `id` and `name`. Logs a warning if any are dropped so the
    user can recover by editing accounts.json.

    Sensitive fields (password/CRN/PIN) are resolved from the OS
    keystore for any record stamped with _secrets_in_store. Legacy
    plaintext records are migrated transparently the first time we
    see them with a working keystore.
    """
    with _accounts_lock:
        if ACCOUNTS_FILE.exists():
            # Tighten permissions on every read so a hand-edited or
            # restored-from-backup file lands at 0o600. Best-effort:
            # a chmod failure (read-only fs, foreign owner) doesn't
            # block reading.
            try:
                if (ACCOUNTS_FILE.stat().st_mode & 0o777) != 0o600:
                    os.chmod(ACCOUNTS_FILE, 0o600)
            except OSError:
                pass
            try:
                raw_text = ACCOUNTS_FILE.read_text()
                payload = _decrypt_payload_from_disk(raw_text)
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(
                    "accounts.json is corrupt and could not be parsed (%s). "
                    "Fix or delete %s to recover.", e, ACCOUNTS_FILE,
                )
                return []
            except Exception as e:
                # cryptography.InvalidTag (AES-GCM tag mismatch) and
                # any other unexpected decrypt failure end up here.
                # Most common cause: an encrypted accounts.json from a
                # prior install whose keystore key is no longer
                # available (user uninstalled+reinstalled, copied the
                # file from another machine, keychain was reset). We
                # MUST NOT propagate this to a 500 in the GUI — that
                # blocks the user from even reaching Settings to
                # reset things. Move the unreadable file aside so a
                # subsequent load() doesn't keep hitting it, log a
                # clear recovery path, and return empty so the GUI
                # lands on Settings.
                quarantined = ACCOUNTS_FILE.with_suffix(
                    f".unreadable.{int(time.time())}.json"
                )
                try:
                    ACCOUNTS_FILE.rename(quarantined)
                except OSError as rename_err:
                    logger.error(
                        "accounts.json could not be decrypted (%s) and the "
                        "file could not be moved out of the way (%s). "
                        "Manually move or remove %s to recover.",
                        e, rename_err, ACCOUNTS_FILE,
                    )
                    return []
                # Drop the now-orphaned file-encryption key so the
                # next add() starts with a clean key+file pair.
                try:
                    secrets_store.delete(
                        secrets_store.META_ACCOUNT,
                        secrets_store.META_FILE_KEY,
                    )
                except Exception:
                    pass
                logger.error(
                    "accounts.json could not be decrypted (%s — the AES key "
                    "in the OS keystore doesn't match this file). The file "
                    "has been moved to %s. To recover: re-add accounts via "
                    "Settings, or use Settings → Restore if you have a "
                    "backup file.",
                    type(e).__name__, quarantined,
                )
                return []
            if not isinstance(payload, dict):
                logger.error("accounts.json has unexpected shape (%s)", type(payload).__name__)
                return []
            raw_list = payload.get("accounts", [])
            if not isinstance(raw_list, list):
                logger.error("accounts.json 'accounts' field is not a list; ignoring")
                return []
            clean = [a for a in raw_list if isinstance(a, dict) and a.get("id") and a.get("name")]
            if len(clean) != len(raw_list):
                logger.warning(
                    "Dropped %d malformed account entry/entries from accounts.json",
                    len(raw_list) - len(clean),
                )
            # Two-phase migration on read:
            #   1. Plaintext credentials → keystore (per-record).
            #   2. Plaintext file body → AES-GCM envelope (whole-file).
            # Either triggers a save; both happen at most once per
            # install. After that, steady state is "keystore + encrypted
            # file" and reads short-circuit through this block.
            file_was_plaintext_envelope = '"_encryption"' not in raw_text
            need_save = _migrate_plaintext_to_store_locked(clean)
            if file_was_plaintext_envelope and secrets_store.is_available():
                need_save = True  # force re-write under the AES envelope
            if need_save:
                _save_payload({"accounts": clean})
            return [_resolve_secrets(a) for a in clean]

        migrated = _migrate_from_env()
        if migrated:
            # Run a keystore migration on the .env-imported records so
            # a fresh install with a legacy .env never lands plaintext
            # secrets on disk in the first place.
            recs = list(migrated["accounts"])
            _migrate_plaintext_to_store_locked(recs)
            _save_payload({"accounts": recs})
            return [_resolve_secrets(a) for a in recs]
        return []


def get(account_id: str) -> dict:
    for a in load():
        if a["id"] == account_id:
            return a
    raise AccountError(f"account not found: {account_id}")


def save_all(accounts: list[dict]) -> None:
    """Persist the accounts list. Refuses if the on-disk file looks
    malformed AND we're about to write a strict-subset of the loaded
    state. That pattern indicates we just nuked the user's other
    accounts because load() returned [] on a corrupt file. Better to
    surface the corruption than silently overwrite credentials.

    Sensitive fields are stripped from records carrying _SECRETS_FLAG
    before they hit disk: those secrets live in the keystore now and
    must not be re-leaked into the JSON. Records without the flag are
    legacy plaintext and pass through untouched.

    When the resulting list is EMPTY, the file is deleted rather than
    written as `{"accounts": []}`. Together with the file-encryption
    key being purged on last-account-delete, this leaves no stranded
    encrypted file that nothing can decrypt — caller's next add()
    starts cleanly with a fresh key and a fresh file.
    """
    with _accounts_lock:
        if ACCOUNTS_FILE.exists():
            try:
                raw = ACCOUNTS_FILE.read_text()
                json.loads(raw)
            except (OSError, ValueError) as e:
                # The file is on disk but unparseable. If the caller is
                # about to write a small list (e.g. one new account),
                # they're likely working from an empty load() result.
                # Refuse so a typo / failed migration doesn't trash
                # the user's real credentials.
                if len(accounts) <= 1:
                    raise AccountError(
                        f"refusing to overwrite malformed accounts.json "
                        f"({type(e).__name__}: {e}). Fix or remove the "
                        f"file at {ACCOUNTS_FILE} first."
                    ) from e
        if not accounts:
            try:
                ACCOUNTS_FILE.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Could not remove empty accounts.json: %s", e)
            return
        _save_payload({"accounts": [_to_persisted(a) for a in accounts]})


def _looks_masked(value: str) -> bool:
    """True if `value` is the fixed-width placeholder we send to the GUI."""
    return value == MASKED_PLACEHOLDER


def validate_record(account: dict) -> dict:
    """Validate and normalize one account record.

    Returns a cleaned dict (name + CREDENTIAL_KEYS) or raises AccountError
    on the first problem found. Pulled out of `add()` so other entry
    points (notably api_restore in app.py, which used to write raw
    backup records straight to disk) get the same guarantees: no empty
    credentials, numeric DP ID, 4-digit PIN, no masked-placeholder
    leakage. Cross-record checks (duplicate name, duplicate
    (dp_id, username)) stay in `add()` since they need the existing
    list — restore handles uniqueness in its own pass.
    """
    if not isinstance(account, dict):
        raise AccountError("account must be a JSON object")
    name = (account.get("name") or "").strip()
    if not name:
        raise AccountError("name is required")
    # Reasonable upper bound. Prevents pathological inputs that would
    # produce 200-char ids, blow up UI rows, etc.
    if len(name) > 60:
        raise AccountError("name must be 60 characters or fewer")

    cleaned = {"name": name}
    for k in CREDENTIAL_KEYS:
        v = (account.get(k) or "").strip()
        if not v:
            raise AccountError(f"{k} is required (account '{name}')")
        if _looks_masked(v):
            raise AccountError(
                f"{k} looks like the masked placeholder (account '{name}'). "
                "Did you forget to type the real value?"
            )
        cleaned[k] = v

    # MeroShare DP IDs are numeric ("10600" etc.). PINs are 4 digits.
    # Validate at this boundary so a CLI/API caller bypassing the GUI's
    # HTML5 validation can't slip through nonsense.
    if not cleaned["dp_id"].isdigit():
        raise AccountError(f"dp_id must be numeric (account '{name}')")
    if not (cleaned["pin"].isdigit() and len(cleaned["pin"]) == 4):
        raise AccountError(f"pin must be exactly 4 digits (account '{name}')")
    # Optional integer fields: only validate range when present. Missing
    # / None / "" means "fall through to global default" — that's the
    # whole point of these being optional.
    raw_kitta = account.get("default_kitta")
    if raw_kitta not in (None, ""):
        try:
            kitta_val = int(raw_kitta)
        except (TypeError, ValueError):
            raise AccountError(
                f"default_kitta must be an integer (account '{name}')"
            ) from None
        lo, hi = _DEFAULT_KITTA_BOUNDS
        if not (lo <= kitta_val <= hi):
            raise AccountError(
                f"default_kitta must be between {lo} and {hi} (account '{name}')"
            )
        cleaned["default_kitta"] = kitta_val
    return cleaned


def add(account: dict) -> dict:
    """Add a new account. Generates id from name. Returns saved record.

    The whole load -> mutate -> save_all sequence runs under
    `_accounts_lock` so two concurrent CRUD requests from Flask's
    threaded server can't both load the same baseline and clobber each
    other's writes.

    Validates and normalizes inputs at this boundary. Strips whitespace,
    rejects all-whitespace names, ensures DP ID is numeric, ensures PIN
    is 4 digits, refuses to create a duplicate (dp_id, username) pair,
    and refuses to reuse an existing account *name* (case-insensitive).
    The name uniqueness check prevents slug collisions on delete+re-add
    (the same name would re-pick a freed slug and resurrect any leftover
    .applied_issues.json state under that id).
    """
    cleaned = validate_record(account)

    with _accounts_lock:
        existing = load()
        # Reject duplicate (dp_id, username). That's the same MeroShare
        # account, just stored twice locally. Almost always a mistake.
        for a in existing:
            if a.get("dp_id") == cleaned["dp_id"] and a.get("username") == cleaned["username"]:
                raise AccountError(
                    f"another account already uses DP {cleaned['dp_id']} + BOID {cleaned['username']}"
                )
        # Reject duplicate name (case-insensitive). Without this, a
        # delete-then-re-add of the same name would re-pick the freed
        # slug and any orphaned .applied_issues.json entries under
        # that id would attach to the new account.
        lowered = cleaned["name"].lower()
        for a in existing:
            if (a.get("name") or "").lower() == lowered:
                raise AccountError(
                    f"another account named '{a.get('name')}' already exists; "
                    "pick a unique name"
                )

        existing_ids = {a["id"] for a in existing}
        new_id = slugify(cleaned["name"], existing_ids)
        # Try to write secrets to the OS keystore first. If any of
        # the writes fail or the keystore isn't available, fall through
        # to the legacy plaintext-in-JSON path so account creation
        # still succeeds — at the cost of weaker storage.
        store_ok = secrets_store.is_available()
        if store_ok:
            for k in SENSITIVE_KEYS:
                if not secrets_store.set(new_id, k, cleaned[k]):
                    # Roll back partial writes for this account so we
                    # don't leave half a credential in the store.
                    for kk in SENSITIVE_KEYS:
                        secrets_store.delete(new_id, kk)
                    store_ok = False
                    break
        new = {
            "id": new_id,
            "name": cleaned["name"],
        }
        # Non-sensitive credential fields (dp_id, username) always live
        # in the JSON — they're identifiers, not secrets.
        for k in NON_SENSITIVE_CREDENTIAL_KEYS:
            new[k] = cleaned[k]
        if store_ok:
            new[_SECRETS_FLAG] = True
        else:
            # Legacy fallback: persist the secrets in the JSON file too.
            # 0o600 is still the on-disk floor.
            for k in SENSITIVE_KEYS:
                new[k] = cleaned[k]
        # Optional fields go through too, defaulting to None so they
        # show up in the JSON for the GUI to render. Coerce to string
        # before stripping so a JSON `null`, integer, or boolean from
        # a misbehaving caller can't slip past the truthy normalization.
        for k in OPTIONAL_KEYS:
            raw = account.get(k)
            if raw is None or raw == "":
                new[k] = None
                continue
            try:
                v = str(raw).strip()
            except Exception:
                v = ""
            new[k] = v if v else None
        # Optional int fields: validate_record already coerced/bounded
        # them into `cleaned` when present; otherwise default to None
        # so the GUI can distinguish "unset → use global" from "0".
        for k in OPTIONAL_INT_KEYS:
            new[k] = cleaned.get(k)
        existing.append(new)
        save_all(existing)
        # Return the resolved view so the route can mask + return.
        return _resolve_secrets(new)


def update(account_id: str, fields: dict) -> dict:
    """Update fields of an existing account.

    `id` is immutable and silently ignored if present in `fields`.
    Empty / None values are skipped so the GUI can submit a partial
    payload without clearing fields. The fixed-width MASKED_PLACEHOLDER
    is also skipped. That's what the GUI displays for password/pin
    in the edit form, and we must not write the placeholder back to
    disk as if it were the new credential.

    Re-validates name uniqueness and PIN/DP shape on update too, so a
    PATCH can't smuggle invalid data past the add() validators.
    """
    # Case-insensitive filter so a frontend that sends "Id" or "ID"
    # can't smuggle in a mutation of the immutable identifier.
    fields = {k: v for k, v in fields.items() if k.lower() != "id"}
    with _accounts_lock:
        accts = load()
        for a in accts:
            if a["id"] != account_id:
                continue
            # Validate every field BEFORE any mutation: a validation
            # failure must leave the in-memory dict identical to the
            # on-disk state, so a downstream save_all() can't persist
            # half-applied changes.
            new_name = fields.get("name")
            stripped_name = None
            if new_name:
                stripped = new_name.strip()
                if not stripped:
                    raise AccountError("name cannot be blank")
                if len(stripped) > 60:
                    raise AccountError("name must be 60 characters or fewer")
                lowered = stripped.lower()
                for other in accts:
                    if other is a:
                        continue
                    if (other.get("name") or "").lower() == lowered:
                        raise AccountError(
                            f"another account named '{other.get('name')}' already exists"
                        )
                stripped_name = stripped
            cleaned_creds: dict = {}
            for k in CREDENTIAL_KEYS:
                v = fields.get(k)
                if not v:
                    continue
                if _looks_masked(v):
                    continue
                if k == "dp_id" and not str(v).isdigit():
                    raise AccountError("dp_id must be numeric")
                if k == "pin" and not (str(v).isdigit() and len(str(v)) == 4):
                    raise AccountError("pin must be exactly 4 digits")
                cleaned_creds[k] = v
            # Validate optional int fields BEFORE the mutation phase so
            # a bad value rolls back cleanly. Same coercion rules as
            # validate_record: empty string clears, missing leaves
            # untouched, present-and-bad raises.
            cleaned_int: dict = {}
            for k in OPTIONAL_INT_KEYS:
                if k not in fields:
                    continue
                raw = fields[k]
                if raw in (None, ""):
                    cleaned_int[k] = None  # clear
                    continue
                try:
                    val = int(raw)
                except (TypeError, ValueError):
                    raise AccountError(f"{k} must be an integer or empty") from None
                if k == "default_kitta":
                    lo, hi = _DEFAULT_KITTA_BOUNDS
                    if not (lo <= val <= hi):
                        raise AccountError(
                            f"default_kitta must be between {lo} and {hi}"
                        )
                cleaned_int[k] = val
            # All validators passed. Now mutate.
            if stripped_name is not None:
                a["name"] = stripped_name
            # Sensitive fields go to the keystore when available; the
            # JSON copy is removed so save_all() can never re-persist
            # the old value. Non-sensitive credentials (dp_id, username)
            # stay in the JSON.
            for k, v in cleaned_creds.items():
                if k in SENSITIVE_KEYS and a.get(_SECRETS_FLAG):
                    if not secrets_store.set(a["id"], k, v):
                        # Keystore set failed mid-update: keep the old
                        # secret rather than blanking it. The user sees
                        # a 500 from the route, can retry, and the
                        # account remains usable in the meantime.
                        raise AccountError(
                            f"could not write {k} to keystore "
                            f"({type(secrets_store).__name__})"
                        )
                    a.pop(k, None)
                else:
                    a[k] = v
            # Optional fields are clearable: an empty string explicitly
            # removes the preference (sentinel). Truthy values overwrite.
            for k in OPTIONAL_KEYS:
                if k in fields:
                    a[k] = fields[k] if fields[k] else None
            for k, v in cleaned_int.items():
                a[k] = v
            save_all(accts)
            return _resolve_secrets(a)
        raise AccountError(f"account not found: {account_id}")


def delete(account_id: str) -> None:
    """Delete an account and cascade-remove its applied-issues entry.

    Without the cascade, deleting then re-adding an account with the
    same name would slug-collide back onto the freed id, and the
    orphaned applied-issues records would re-attach to the new
    account. Secrets in the keystore are also purged: leaving them
    behind would surface as a stale Keychain entry the user can't
    explain (and on Linux Secret Service, accumulate over time).

    When this is the LAST account, the file-encryption key (in the
    META namespace) is also purged. Otherwise it'd survive in
    Keychain Access as an unexplained "com.meroshare.autoapply" entry
    the user has no UI to clean up. The next add() regenerates a
    fresh key.
    """
    with _accounts_lock:
        accounts = load()
        new = [a for a in accounts if a["id"] != account_id]
        if len(new) == len(accounts):
            raise AccountError(f"account not found: {account_id}")
        save_all(new)
        # Cascade: drop applied state for this account too.
        applied = load_applied()
        if account_id in applied:
            del applied[account_id]
            save_applied(applied)
        # Cascade: purge keystore entries for this account. Idempotent
        # — no-op if the entry was already gone or no keystore is
        # available. Done AFTER save_all so a keystore hiccup can't
        # leave us with the JSON record gone but secrets stranded.
        secrets_store.delete_all_for(account_id, SENSITIVE_KEYS)
        if not new:
            secrets_store.delete(
                secrets_store.META_ACCOUNT,
                secrets_store.META_FILE_KEY,
            )


def mask(account: dict) -> dict:
    """Return a copy with password and pin replaced by a fixed placeholder.

    Uses a fixed-width string instead of "first char + asterisks" so we
    don't leak the password length or its first character to anyone
    looking at the GUI (e.g. a screenshare). The placeholder is also
    detectable by `update()` so the GUI can submit it without the
    credential being overwritten.
    """
    out = dict(account)
    for k in ("password", "pin"):
        if out.get(k):
            out[k] = MASKED_PLACEHOLDER
    return out


# ── applied-issues state (per-account) ─────────────────────────────────

# File-format versions for `.applied_issues.json`:
#   v1 (legacy, pre-multi-account):
#       {"<issue_id>": {"applied_at": "...", ...}, ...}
#   v2 (current, multi-account):
#       {"_schema_version": 2,
#        "accounts": {"<account_id>": {"<issue_id>": {...}, ...}}}
#
# Files without "_schema_version" are detected as v1 and migrated to v2 on
# read. Files where "_schema_version" exists but is unknown to this code
# are loaded empty and a warning is logged. Better than silently treating
# a future format as legacy.

APPLIED_SCHEMA_VERSION = 2


def _looks_like_v1(state: dict) -> bool:
    """Detect legacy flat shape: every value is a record dict.

    Inspects every value, not just the first, so partially-corrupt files
    (mixed shapes) fail the check. Such files are then logged as
    'unversioned and not in legacy shape' by load_applied() and treated
    as empty. They are NOT silently reinterpreted under the v2 path.
    """
    if not state:
        return False
    return all(
        isinstance(v, dict) and "applied_at" in v
        for v in state.values()
    )


def load_applied() -> dict:
    """Load v2 {account_id: {issue_id: record}} state.

    Migrates v1 (flat) on read. Returns the inner accounts mapping; the
    schema-version envelope is stripped.
    """
    if not APPLIED_FILE.exists():
        return {}
    try:
        raw = json.loads(APPLIED_FILE.read_text())
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not read applied_issues.json: %s", e)
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "applied_issues.json has unexpected shape (%s), ignoring",
            type(raw).__name__,
        )
        return {}

    version = raw.get("_schema_version")
    if version == APPLIED_SCHEMA_VERSION:
        accts = raw.get("accounts", {})
        if not isinstance(accts, dict):
            return {}
        # Defensive: drop any reserved sentinel that snuck into the
        # account-id keyspace via manual file edit.
        accts.pop("_schema_version", None)
        return accts
    if version is None:
        # Legacy file or empty {}. Try v1 detection
        if not raw:
            return {}
        if _looks_like_v1(raw):
            # Wrap legacy flat-shape under the 'default' account.
            migrated = {"default": raw}
            # Persist the migrated shape eagerly. Without this, a
            # read-only path (--status, GUI History tab) would leave
            # the file in v1 forever, so every subsequent read pays the
            # migration cost and any downstream code reading the file
            # directly sees an inconsistent shape.
            try:
                save_applied(migrated)
                logger.info("Migrated .applied_issues.json from v1 to v2.")
            except OSError as e:
                logger.warning(
                    "Could not write migrated v2 .applied_issues.json: %s "
                    "(file stays in v1; will retry next save)", e,
                )
            return migrated
        # Unversioned file that doesn't look like v1 either; treat as empty
        # rather than risk corrupting it.
        logger.warning(
            "applied_issues.json is unversioned and not in legacy shape; ignoring"
        )
        return {}
    logger.warning(
        "applied_issues.json has unknown schema_version=%r (this code knows %d); "
        "ignoring to avoid corrupting newer-format data",
        version, APPLIED_SCHEMA_VERSION,
    )
    return {}


def save_applied(state: dict) -> None:
    """Write the v2 envelope: {_schema_version, accounts: state}.

    Mode 0o600 because this file embeds account names alongside applied
    company IDs. Not credentials, but still per-user metadata that
    other local users on a shared box have no business reading.

    Holds the cross-process file lock for the write so the launchd
    auto_apply.py and a concurrent GUI Run-check can't both load the
    same baseline and clobber each other's records.
    """
    payload = {"_schema_version": APPLIED_SCHEMA_VERSION, "accounts": state}
    with _file_lock(APPLIED_LOCK_FILE):
        _atomic_write(APPLIED_FILE, json.dumps(payload, indent=2), mode=0o600)


def update_applied(mutator) -> dict:
    """Read-modify-write `.applied_issues.json` atomically across processes.

    `mutator(state)` is called with the loaded dict and may mutate it
    in place; the modified state is written back under the file lock.
    Returns the new state. Use this for any code that needs to
    increment/append to the applied-issues file from outside
    `check_and_apply` (the GUI's per-issue delete endpoint, for example).
    """
    with _file_lock(APPLIED_LOCK_FILE):
        state = load_applied()
        mutator(state)
        payload = {"_schema_version": APPLIED_SCHEMA_VERSION, "accounts": state}
        _atomic_write(APPLIED_FILE, json.dumps(payload, indent=2), mode=0o600)
        return state


# ── Allotment-status tracking (for one-shot transition notifications) ──


def load_allotment_state() -> dict:
    """Last-seen `statusName` per applicantFormId.

    Empty dict on first run / unreadable file / corrupt JSON. Used by
    `/api/status` to fire a desktop notification exactly once when an
    application transitions from PENDING to ALLOTTED / NOT ALLOTTED.
    Without this state, every poll would re-fire the notification.
    """
    if not ALLOTMENT_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(ALLOTMENT_STATE_FILE.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        # Don't crash the status route over a corrupt notification
        # state file. Worst case the user re-sees one transition toast.
        return {}
    return data if isinstance(data, dict) else {}


def save_allotment_state(state: dict) -> None:
    """Persist allotment state. 0o600 like every other state file."""
    _atomic_write(
        ALLOTMENT_STATE_FILE,
        json.dumps(state, indent=2),
        mode=0o600,
    )
