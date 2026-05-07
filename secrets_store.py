"""OS-native credential keystore abstraction.

Backs the password/CRN/PIN fields of accounts with the platform's
encrypted store: macOS Keychain, Windows Credential Manager, or Linux
Secret Service. Falls back cleanly to plaintext-in-accounts.json (mode
0o600) when no keyring is available — that's the legacy behavior we
preserve so headless Linux / CI / unusual setups still work.

API surface intentionally narrow:
    is_available()       — True if the active backend is real (not Null/Fail).
    set(account_id, field, value)
    get(account_id, field) -> str | None
    delete(account_id, field)
    delete_all_for(account_id)

The (account_id, field) tuple is encoded as keyring's (service, username):
    service  = "com.meroshare.autoapply"
    username = f"{account_id}.{field}"

Why a separate module: keeps the keyring import lazy and isolated, makes
mocking trivial in tests, and lets accounts.py stay focused on schema
validation rather than platform plumbing.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("meroshare")

SERVICE = "com.meroshare.autoapply"

# Keystore keys for things that aren't account credentials but still
# belong in the keystore — e.g. the AES-GCM key used by the
# encrypted-at-rest layer over accounts.json.
META_ACCOUNT = "_meroshare_meta"  # synthetic "account_id" for non-account secrets
META_FILE_KEY = "file_encryption_key"


_keyring = None
_keyring_checked = False
_keyring_available = False


def _ensure_keyring():
    """Lazy-import keyring and detect whether the platform has a real
    backend. Caches the result so repeated calls are cheap.

    A "real backend" is anything that isn't keyring's Null or Fail
    backend, both of which keyring picks when no keystore is reachable
    (headless Linux without DBus, locked-down sandboxes, etc.). Their
    set_password() either no-ops or raises, neither of which we want
    to call repeatedly.
    """
    global _keyring, _keyring_checked, _keyring_available
    if _keyring_checked:
        return _keyring if _keyring_available else None
    _keyring_checked = True
    try:
        import keyring as kr  # noqa: I001
        backend = kr.get_keyring()
        # Keyring stamps the no-op backends with a `priority` of <=0
        # and they live in the `keyring.backends.{fail,null}` modules.
        # The conservative test: if the module path of the backend
        # contains 'fail' or 'null', treat as unavailable.
        backend_module = type(backend).__module__.lower()
        if "fail" in backend_module or "null" in backend_module:
            logger.info(
                "secrets_store: no real keyring backend available "
                "(%s); falling back to plaintext accounts.json (0o600).",
                backend_module,
            )
            return None
        _keyring = kr
        _keyring_available = True
        logger.info(
            "secrets_store: using %s keyring backend.",
            type(backend).__name__,
        )
        return _keyring
    except Exception as e:
        # Any import failure (missing optional deps, broken DBus, etc.)
        # falls through to the plaintext path. Log once at INFO so the
        # operator knows why their accounts.json still has plaintext
        # fields, but don't spam the apply loop.
        logger.info(
            "secrets_store: keyring import/init failed (%s); "
            "falling back to plaintext accounts.json.", e,
        )
        return None


def is_available() -> bool:
    """True when set/get will actually use a platform keystore."""
    return _ensure_keyring() is not None


def _username(account_id: str, field: str) -> str:
    return f"{account_id}.{field}"


def set(account_id: str, field: str, value: str) -> bool:
    """Store a secret. Returns True on success, False when no keyring is
    available (caller should fall back to plaintext-in-JSON).

    Raises only on programmer error (empty account_id/field). Underlying
    keyring failures are logged and surface as False, so a transient
    backend hiccup doesn't crash account creation.
    """
    if not account_id or not field:
        raise ValueError("account_id and field are required")
    kr = _ensure_keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE, _username(account_id, field), str(value))
        return True
    except Exception as e:
        # Don't log the value — `value` is the secret we're trying to
        # protect. Just type + which field.
        logger.error(
            "secrets_store.set failed for account=%s field=%s: %s",
            account_id, field, type(e).__name__,
        )
        return False


def get(account_id: str, field: str) -> str | None:
    """Fetch a secret. Returns None when not stored OR when no keyring
    is available (caller should look at the plaintext fallback)."""
    if not account_id or not field:
        raise ValueError("account_id and field are required")
    kr = _ensure_keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, _username(account_id, field))
    except Exception as e:
        logger.error(
            "secrets_store.get failed for account=%s field=%s: %s",
            account_id, field, type(e).__name__,
        )
        return None


def delete(account_id: str, field: str) -> bool:
    """Delete a secret. Returns True if deleted, False on no-op
    (already absent or no keyring)."""
    if not account_id or not field:
        raise ValueError("account_id and field are required")
    kr = _ensure_keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE, _username(account_id, field))
        return True
    except Exception:
        # keyring raises PasswordDeleteError when the entry didn't exist.
        # That's a no-op success from our caller's perspective. Don't
        # log — delete is idempotent and the absence isn't an error.
        return False


def delete_all_for(account_id: str, fields: tuple[str, ...]) -> None:
    """Cascade-delete every known secret for one account. Used by
    accounts.delete() and the factory-reset path. Caller passes the
    field tuple so secrets_store doesn't need to know the schema."""
    for f in fields:
        delete(account_id, f)


# ── Reset helpers (used by factory-reset and tests) ────────────────


def _reset_cache_for_tests() -> None:
    """Force re-detection of the keyring backend on next call.

    Tests swap the backend via `keyring.set_keyring(...)` and need our
    cache to forget the previous detection. NOT for production code.
    """
    global _keyring, _keyring_checked, _keyring_available
    _keyring = None
    _keyring_checked = False
    _keyring_available = False
