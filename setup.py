#!/usr/bin/env python3
"""
MeroShare Auto-Apply - Interactive Setup
Walks you through configuring credentials and preferences.

Flags:
  --reset           Re-run credential setup even if accounts.json
                    already exists (useful after a rotation / new
                    BOID); prompts for a unique display name first.
  --list-accounts   Print configured accounts (id, name, DP, BOID,
                    preferred bank) and exit. No secrets are shown.
"""

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path

import accounts as accounts_mod

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def print_header():
    print()
    print("=" * 55)
    print("    MEROSHARE AUTO-APPLY - SETUP")
    print("    Never miss an IPO or Right Share again!")
    print("=" * 55)
    print()


def _ask_secret(prompt: str, help_text: str = "", validate=None) -> str:
    """Like ask() but uses getpass so the value isn't echoed.

    Used for password and PIN. `input()` echoes both to the terminal
    and into shell scrollback. When stdin isn't a TTY, getpass issues
    a warning and falls back to echoed input. We detect the warning
    case via warnings.catch_warnings so we can tell the user the
    input was visible (for log-aware automation).
    """
    import warnings

    if help_text:
        print(f"  {help_text}")
    while True:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", getpass.GetPassWarning)
                v = getpass.getpass(f"  {prompt}: ").strip()
            if any(isinstance(w.message, getpass.GetPassWarning) for w in caught):
                print("  (note: input was echoed because stdin is not a TTY)")
        except EOFError:
            v = input(f"  {prompt}: ").strip()
        if validate is None:
            return v
        err = validate(v)
        if err is None:
            return v
        print(f"  ! {err}")


def setup_credentials(force: bool = False):
    """Interactive credential setup. Writes accounts.json (multi-account capable).

    Returns True when an account was added or already exists; False
    when the user requested setup but it couldn't be completed (e.g.
    accounts.add raised on duplicate name).
    """

    print("-" * 55)
    print("  STEP 1: MeroShare Account")
    print("-" * 55)
    print()
    print("  Your credentials are stored locally in accounts.json.")
    print("  They are NEVER uploaded or shared anywhere.")
    print("  You can add more accounts later from Settings in the GUI.")
    print()

    existing = accounts_mod.load()
    if existing and not force:
        print(f"  Found {len(existing)} existing account(s); skipping.")
        print("  Re-run with --reset to add another, or use the GUI Settings tab.")
        print()
        # The next phase (test_login) should NOT run with stale state
        # the user didn't just confirm. Return False so the caller
        # can decide whether to test.
        return False

    def ask(prompt, help_text="", validate=None):
        """Prompt with optional validator. Re-asks on validation failure."""
        if help_text:
            print(f"  {help_text}")
        while True:
            v = input(f"  {prompt}: ").strip()
            if validate is None:
                return v
            err = validate(v)
            if err is None:
                return v
            print(f"  ! {err}")

    def _v_dp(s):
        if not s:
            return "DP ID is required"
        if not s.isdigit():
            return "DP ID must be numeric (e.g. 10600)"
        return None

    def _v_pin(s):
        if not s:
            return "PIN is required"
        if not (s.isdigit() and len(s) == 4):
            return "PIN must be exactly 4 digits"
        return None

    def _v_required(s, label):
        if not s:
            return f"{label} is required"
        return None

    def _v_unique_name(s):
        if not s:
            return "Account name is required"
        # Mirror accounts.add()'s upper bound so the user finds out
        # NOW, not after typing every credential.
        if len(s) > 60:
            return "name must be 60 characters or fewer"
        for a in accounts_mod.load():
            if (a.get("name") or "").lower() == s.lower():
                return f"another account named '{s}' already exists"
        return None

    if existing and force:
        # In --reset mode, prompt for a name so the new account
        # doesn't collide with the existing one (which would otherwise
        # rejected by accounts.add's uniqueness check).
        name = ask(
            "Account display name",
            help_text="A short label for this account (e.g. 'Mine', 'Spouse').",
            validate=_v_unique_name,
        )
    else:
        name = "Default Account"
    print()

    dp_id = ask(
        "DP ID",
        help_text="The number in parentheses next to your broker name on the login page.\n  Example: NIMB ACE CAPITAL LIMITED (10600) -> enter 10600",
        validate=_v_dp,
    )
    print()

    username = ask(
        "Username (BOID)",
        help_text="Your MeroShare username / BOID number.",
        validate=lambda s: _v_required(s, "BOID"),
    )
    print()

    # Password and PIN go through getpass so they don't end up in
    # terminal scrollback or shell history.
    password = _ask_secret(
        "Password",
        help_text="Your MeroShare login password (input hidden).",
        validate=lambda s: _v_required(s, "Password"),
    )
    print()

    crn = ask(
        "CRN",
        help_text="Customer Reference Number from your bank (shown on the apply form).",
        validate=lambda s: _v_required(s, "CRN"),
    )
    print()

    pin = _ask_secret(
        "Transaction PIN (4 digits)",
        help_text="The 4-digit PIN you use when applying for shares (input hidden).",
        validate=_v_pin,
    )
    print()

    preferred_bank = ask(
        "Preferred bank (optional)",
        help_text="Substring of bank name to auto-pick on the apply form. Leave blank to use the first available.",
    )
    print()

    preferred_bank_account = ask(
        "Preferred bank account (optional)",
        help_text=(
            "Account number substring when you have multiple accounts at "
            "the same bank. Leave blank to pick the first."
        ),
    )
    print()

    try:
        accounts_mod.add({
            "name": name,
            "dp_id": dp_id,
            "username": username,
            "password": password,
            "crn": crn,
            "pin": pin,
            "preferred_bank": preferred_bank or None,
            "preferred_bank_account": preferred_bank_account or None,
        })
    except accounts_mod.AccountError as e:
        print(f"  ! Could not save account: {e}")
        return False
    print(f"  Account '{name}' saved to accounts.json")
    print()
    return True


def _default_config() -> dict:
    return {
        "share_types": {
            "ipo_ordinary": True,
            "right_share": True,
            "fpo": False,
            "mutual_fund": False,
            "debenture": False,
        },
        "auto_apply": {
            "enabled": True,
            "default_kitta": 10,
            "right_share_apply_max": True,
            "max_amount": 100000,
        },
        "check_interval_hours": 6,
        "notifications": {"desktop": True, "log_file": True},
    }


def _merge_with_defaults(user_cfg: dict) -> dict:
    """Shallow-merge user config onto defaults so missing nested keys
    don't KeyError later. Old setups that wrote a partial config used
    to crash setup_config() at the first `config["auto_apply"][...]`
    access.
    """
    out = _default_config()
    if not isinstance(user_cfg, dict):
        return out
    for k, v in user_cfg.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def setup_config():
    """Interactive config setup."""
    print("-" * 55)
    print("  STEP 2: Share Type Preferences")
    print("-" * 55)
    print()

    config = _default_config()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                loaded = json.load(f)
            config = _merge_with_defaults(loaded)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  Warning: could not read existing config.json ({e}).")
            keep = input("  Overwrite the broken file with defaults? [y/N]: ").strip().lower()
            if keep not in ("y", "yes"):
                print("  Leaving the existing file alone. Edit it manually, then re-run setup.")
                print()
                return

    share_types = config.get("share_types", {})

    def ask_yn(prompt, default=True):
        d = "Y/n" if default else "y/N"
        val = input(f"  {prompt} [{d}]: ").strip().lower()
        if not val:
            return default
        return val in ("y", "yes")

    share_types["ipo_ordinary"] = ask_yn("Auto-apply for IPO Ordinary Shares?", share_types.get("ipo_ordinary", True))
    share_types["right_share"] = ask_yn("Auto-apply for Right Shares?", share_types.get("right_share", True))
    share_types["fpo"] = ask_yn("Auto-apply for FPO (Further Public Offering)?", share_types.get("fpo", False))
    share_types["mutual_fund"] = ask_yn("Auto-apply for Mutual Funds?", share_types.get("mutual_fund", False))
    share_types["debenture"] = ask_yn("Auto-apply for Debentures?", share_types.get("debenture", False))
    print()

    config["share_types"] = share_types

    # Kitta settings
    print("-" * 55)
    print("  STEP 3: Application Settings")
    print("-" * 55)
    print()

    auto_cfg = config.setdefault("auto_apply", {})
    default_kitta = auto_cfg.get("default_kitta", 10)
    val = input(f"  Default kitta for IPO applications [{default_kitta}]: ").strip()
    if val.isdigit() and 1 <= int(val) <= 100_000:
        auto_cfg["default_kitta"] = int(val)
    elif val:
        print(f"  ! Ignoring '{val}'. Must be a positive integer 1..100000. Keeping {default_kitta}.")

    apply_max = auto_cfg.get("right_share_apply_max", True)
    auto_cfg["right_share_apply_max"] = ask_yn(
        "Apply for maximum eligible right shares?", apply_max
    )

    max_amount = auto_cfg.get("max_amount", 100000)
    val = input(
        f"  Maximum amount per application (Rs.). 0 means no cap [{max_amount}]: "
    ).strip()
    if val.isdigit():
        # 0 is allowed as the explicit "no cap" sentinel; downstream
        # browser_apply.py treats `not max_amount` as no cap. We
        # surface the meaning here so the user isn't surprised.
        auto_cfg["max_amount"] = int(val)
        if int(val) == 0:
            print("  (set to 0. No per-application cap)")
    elif val:
        print(f"  ! Ignoring '{val}'. Must be a non-negative integer. Keeping {max_amount}.")

    auto_cfg["enabled"] = True
    print()

    # Check interval
    interval = config.get("check_interval_hours", 6)
    val = input(f"  Check for new issues every N hours [{interval}]: ").strip()
    if val.isdigit() and 1 <= int(val) <= 24:
        config["check_interval_hours"] = int(val)
    elif val:
        print(f"  ! Ignoring '{val}'. Must be 1..24. Keeping {interval}.")

    print()

    # Save with 0o600 to match the rest of the project's file-perm
    # hygiene (consistent with accounts.json / app.py:save_config).
    import os as _os
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
        f.flush()
        try:
            _os.fsync(f.fileno())
        except OSError:
            pass
    try:
        _os.chmod(tmp, 0o600)
    except OSError:
        pass
    _os.replace(tmp, CONFIG_FILE)
    print("  Configuration saved to config.json")
    print()


def test_login():
    """Test if credentials work."""
    print("-" * 55)
    print("  STEP 4: Testing Login")
    print("-" * 55)
    print()
    print("  Connecting to MeroShare...")

    try:
        from meroshare_client import MeroShareClient

        accts = accounts_mod.load()
        if not accts:
            print("  No accounts to test (skipping).")
            return False
        primary = accts[0]
        client = MeroShareClient(credentials=primary)
        if client.login():
            own = client.get_own_details()
            print("  Login successful!")
            print(f"  Account: {own.get('name', '?')}")
            print(f"  DEMAT: {own.get('demat', '?')}")
            client.logout()
            return True
        else:
            tag = client.last_login_error or "unknown"
            if tag == "rate_limited":
                print("  Login throttled by MeroShare. Wait 5–10 minutes and retry.")
            elif tag == "bad_credentials":
                print("  Login FAILED: credentials likely wrong or password expired.")
            else:
                print(f"  Login FAILED ({tag}). See logs/meroshare.log for details.")
            return False
    except Exception as e:
        print(f"  Error: {type(e).__name__}: {e}")
        return False


def install_dependencies():
    """Check and install Python dependencies.

    Surfaces pip's stderr on failure so an unreachable PyPI / SSL
    error is visible to the user instead of a bare CalledProcessError
    traceback.
    """
    print("-" * 55)
    print("  Checking Dependencies")
    print("-" * 55)
    print()

    venv_dir = BASE_DIR / "venv"
    python = venv_dir / "bin" / "python3"
    if sys.platform == "win32":
        python = venv_dir / "Scripts" / "python.exe"

    if not venv_dir.exists():
        print("  Creating virtual environment...")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"  ! venv creation failed (exit {e.returncode}).")
            print("  Make sure 'python3 -m venv --help' works in your shell.")
            sys.exit(1)

    def _run_capture(cmd, label):
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print(f"  ! {label} failed (exit {e.returncode}).")
            if e.stdout:
                print(f"    stdout: {e.stdout.strip()[-500:]}")
            if e.stderr:
                print(f"    stderr: {e.stderr.strip()[-500:]}")
            sys.exit(1)

    print("  Installing Python packages...")
    _run_capture(
        [str(python), "-m", "pip", "install", "-q", "-r",
         str(BASE_DIR / "requirements.txt")],
        "pip install",
    )

    print("  Installing browser for automation...")
    _run_capture(
        [str(python), "-m", "playwright", "install", "chromium"],
        "playwright install chromium",
    )

    print("  All dependencies installed!")
    print()


def list_accounts():
    """Print configured accounts (id + name + DP/BOID, no secrets)."""
    accts = accounts_mod.load()
    if not accts:
        print("  No accounts configured. Run `python setup.py` to add one.")
        return
    print()
    print(f"  {len(accts)} account(s):")
    print()
    for a in accts:
        bank = a.get("preferred_bank") or "(any)"
        print(f"  - id={a['id']!s:<25} name={a.get('name', '?')!s:<30}")
        print(f"    dp={a.get('dp_id', '?')!s:<8} boid={a.get('username', '?')!s:<25} bank={bank}")
    print()


def main():
    parser = argparse.ArgumentParser(description="MeroShare Auto-Apply setup wizard")
    parser.add_argument(
        "--reset", action="store_true",
        help="Re-run credential setup even if accounts.json already exists.",
    )
    parser.add_argument(
        "--list-accounts", action="store_true",
        help="Print configured accounts (no secrets) and exit.",
    )
    args = parser.parse_args()

    if args.list_accounts:
        list_accounts()
        return

    print_header()

    # Check if Python packages are available
    try:
        import requests  # noqa: F401
        import flask  # noqa: F401
    except ImportError:
        print("  Missing dependencies. Installing...")
        install_dependencies()
        print("  Please run this script again using: ./venv/bin/python setup.py")
        return

    creds_added = setup_credentials(force=args.reset)
    setup_config()

    print()
    if creds_added:
        # Only test login when we just collected fresh credentials -
        # otherwise stale on-disk credentials would produce a
        # confusing failure on every re-run of the wizard.
        test_login()

    print()
    print("=" * 55)
    print("  SETUP COMPLETE!")
    print("=" * 55)
    print()
    print("  Quick start commands:")
    print()
    print("    # List configured accounts")
    print("    python setup.py --list-accounts")
    print()
    print("    # List current open issues (first account)")
    print("    python auto_apply.py --list")
    print()
    print("    # Same, for a specific account by id")
    print("    python auto_apply.py --list --account <id>")
    print()
    print("    # Check and auto-apply now")
    print("    python auto_apply.py")
    print()
    print("    # Run in background (checks every few hours)")
    print("    python auto_apply.py --daemon")
    print()
    print("    # Set up automatic scheduling (macOS)")
    print("    ./setup_schedule.sh [interval_hours]")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Don't dump a traceback when the user hits Ctrl+C. They know
        # what they did. Whatever was already saved (accounts, config)
        # stays on disk.
        print("\n  Setup interrupted. Run again any time.\n")
        sys.exit(130)
