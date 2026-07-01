"""Credential/secret store — file-backed, with the OS keystore as a mirror.

Secrets (each account's password/CRN/PIN and the accounts.json AES key) are
stored in a 0600 JSON file under the app's data dir. The OS keystore (macOS
Keychain / Windows Credential Manager / Linux Secret Service) is kept as a
BEST-EFFORT MIRROR only.

Why the file is the source of truth: the OS keychain ties access to the
accessing binary's code-signing identity. This app ships ad-hoc signed (no
Apple Developer ID), so every rebuilt release is a *different* identity — the
new build cannot read the previous build's keychain items, which silently wiped
all saved accounts on `brew upgrade` (the encrypted accounts.json became
undecryptable and was reset). The 0600 file lives in
~/Library/Application Support (not the bundle) and is not gated on the
signature, so it survives upgrades.

Security trade-off (deliberate): a process with filesystem access to the user's
own data dir can read these secrets — the same level as this app's legacy
plaintext-in-accounts.json fallback. It is weaker than keychain-gated storage,
which is unattainable without a stable Apple Developer ID signature.

Public API (unchanged):
    is_available()
    set(account_id, field, value) -> bool
    get(account_id, field) -> str | None
    delete(account_id, field) -> bool
    delete_all_for(account_id, fields)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

logger = logging.getLogger("meroshare")

SERVICE = "com.meroshare.autoapply"

# Keystore keys for things that aren't account credentials but still belong in
# the store — e.g. the AES-GCM key for the encrypted-at-rest accounts.json.
META_ACCOUNT = "_meroshare_meta"
META_FILE_KEY = "file_encryption_key"

_FILENAME = ".secrets.json"
_lock = threading.Lock()


def _secrets_path():
    # Lazy import: accounts imports secrets_store at load time, but by the time
    # any of these functions run, accounts.STATE_DIR is resolved. Tests that
    # exercise the real backend patch accounts.STATE_DIR (or this function).
    import accounts
    return accounts.STATE_DIR / _FILENAME


def _cross_process_lock():
    """Cross-process advisory lock guarding the whole-file read-modify-write, so
    the GUI and the launchd daemon (separate processes) can't clobber each
    other's secret writes. Reuses accounts' fcntl helper (no-op on Windows).
    Distinct lock FILE from any other so it can't self-deadlock with a nested
    lock held by the caller."""
    import accounts
    return accounts._file_lock(accounts.STATE_DIR / ".secrets.lock")


def _load_strict() -> dict:
    """Read for a read-MODIFY-write. A present-but-unreadable file RAISES so the
    caller ABORTS rather than atomically overwriting (and thereby wiping) every
    other secret. Only a genuinely absent file is treated as empty."""
    p = _secrets_path()
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))  # ValueError on corrupt
    if not isinstance(data, dict):
        raise ValueError("secrets file is not a JSON object")
    return data


def _load_or_empty() -> dict:
    """Read for a PURE read: absent OR unreadable both yield {} (safe — no write
    follows, so nothing gets clobbered)."""
    try:
        return _load_strict()
    except (OSError, ValueError):
        return {}


def _save_file(data: dict) -> bool:
    path = _secrets_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)  # 0600 from creation, no world-readable window
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except OSError as e:
        logger.error("secrets_store: could not persist secrets file: %s", type(e).__name__)
        return False


def _k(account_id: str, field: str) -> str:
    return f"{account_id}.{field}"


# ── Keychain mirror (best-effort) ──────────────────────────────────

_keyring = None
_keyring_checked = False
_keyring_available = False


def _ensure_keyring():
    global _keyring, _keyring_checked, _keyring_available
    if _keyring_checked:
        return _keyring if _keyring_available else None
    _keyring_checked = True
    try:
        import keyring as kr
        backend_module = type(kr.get_keyring()).__module__.lower()
        if "fail" in backend_module or "null" in backend_module:
            return None
        _keyring = kr
        _keyring_available = True
        return _keyring
    except Exception:
        return None


def _kr_set(account_id: str, field: str, value: str) -> None:
    kr = _ensure_keyring()
    if kr is None:
        return
    try:
        kr.set_password(SERVICE, _k(account_id, field), str(value))
    except Exception as e:
        logger.debug("secrets_store: keychain mirror set failed: %s", type(e).__name__)


def _kr_get(account_id: str, field: str):
    kr = _ensure_keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, _k(account_id, field))
    except Exception:
        return None


def _kr_delete(account_id: str, field: str) -> None:
    kr = _ensure_keyring()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, _k(account_id, field))
    except Exception:
        pass


# ── Public API ─────────────────────────────────────────────────────


def is_available() -> bool:
    """The 0600 file backend is available on any platform with a writable data
    dir. Returns True; when the data dir is genuinely unusable, set() returns
    False and callers fall back to plaintext-in-accounts.json."""
    return True


def set(account_id: str, field: str, value: str) -> bool:
    if not account_id or not field:
        raise ValueError("account_id and field are required")
    try:
        with _lock, _cross_process_lock():
            data = _load_strict()  # fail closed: never overwrite an unreadable file
            data[_k(account_id, field)] = str(value)
            ok = _save_file(data)
    except (OSError, ValueError) as e:
        logger.error(
            "secrets_store.set: refusing to overwrite unreadable secrets file "
            "(%s); caller falls back to plaintext.", type(e).__name__,
        )
        return False
    _kr_set(account_id, field, value)  # best-effort mirror
    return ok


def get(account_id: str, field: str):
    if not account_id or not field:
        raise ValueError("account_id and field are required")
    with _lock:
        v = _load_or_empty().get(_k(account_id, field))
    if v is not None:
        return v
    # Not in the file — recover from the keychain mirror (e.g. a value written by
    # an older keychain-only build under this same signature) and migrate it into
    # the file so the next upgrade keeps it. Best-effort: a read must never fail.
    kv = _kr_get(account_id, field)
    if kv is not None:
        try:
            with _lock, _cross_process_lock():
                data = _load_strict()
                data[_k(account_id, field)] = kv
                _save_file(data)
        except (OSError, ValueError):
            pass
    return kv


def delete(account_id: str, field: str) -> bool:
    if not account_id or not field:
        raise ValueError("account_id and field are required")
    existed = False
    try:
        with _lock, _cross_process_lock():
            data = _load_strict()
            existed = data.pop(_k(account_id, field), None) is not None
            if existed:
                _save_file(data)
    except (OSError, ValueError) as e:
        logger.error("secrets_store.delete: unreadable secrets file (%s).", type(e).__name__)
        existed = False
    _kr_delete(account_id, field)
    return existed


def delete_all_for(account_id: str, fields) -> None:
    for f in fields:
        delete(account_id, f)


def _reset_cache_for_tests() -> None:
    """Force re-detection of the keyring mirror on next call."""
    global _keyring, _keyring_checked, _keyring_available
    _keyring = None
    _keyring_checked = False
    _keyring_available = False
