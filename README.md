# MeroShare Auto-Apply

**Never miss an IPO or Right Share on NEPSE again.**

Automatically checks MeroShare for new share issues and applies for you. Just set it up once with your MeroShare details, and it handles the rest.

![Dashboard](https://img.shields.io/badge/Status-Active-green)

## What it does

- Detects new IPOs, Right Shares, and FPOs as soon as they open
- Applies for you automatically (or with one click from the dashboard)
- Manages multiple MeroShare accounts from one place. Apply for the same issue across all of them
- Lets you choose what to apply for. Skip mutual funds, debentures, etc.
- Shows your full application history with allotment results
- Sends desktop notifications when new shares are available
- Tracks everything so it never applies twice for the same issue

## Getting Started

### Easiest path — download the prebuilt app

Grab the latest release from [GitHub Releases](https://github.com/OfficialBishal/MeroShare-Auto-Apply/releases/latest):

- **macOS**: `MeroShare-Auto-Apply.dmg` — open it, drag the app to Applications, launch from Launchpad. The first launch needs ~30 seconds for the bundled browser engine to download. No Python install required.
- **Windows**: `MeroShare-Auto-Apply-windows.zip` — extract anywhere, double-click `Run MeroShare Auto-Apply.bat`. The first launch installs dependencies (~2–5 minutes); subsequent launches are instant.

**On macOS Sequoia (and newer)**, double-clicking a freshly-downloaded build often shows *"MeroShare Auto-Apply.app is damaged and can't be opened"*. The `.app` isn't actually damaged — macOS refuses to run ad-hoc-signed binaries that came through the browser. One-time fix: open Terminal and paste the line below, then double-click the app normally.

```
xattr -dr com.apple.quarantine "/Applications/MeroShare Auto-Apply.app"
```

(On older macOS this may instead show "unidentified developer" — in that case right-click the .app → Open → Open does the same thing without Terminal.)

If you want to skip this step on every download, the proper fix is to ship the build with an Apple Developer ID and notarize it ($99/year for the developer program). The build script has the hooks; if/when there's a cert, releases stop tripping Gatekeeper entirely.

### From source (developer path)

If you'd rather run from source — auditing, modifying, or contributing:

1. Have Python 3.10+ installed ([python.org/downloads](https://www.python.org/downloads/), tick "Add Python to PATH" on Windows).
2. Clone the repo:
   ```
   git clone https://github.com/OfficialBishal/MeroShare-Auto-Apply.git
   cd MeroShare-Auto-Apply
   ```
3. Run it:
   - **Mac/Linux:** `./run.sh`
   - **Windows:** double-click `run.bat`

First run installs dependencies into a local `venv/`, asks for your MeroShare details, and opens the dashboard at `http://localhost:5050`.

### Where to find your login details

| What | Where to find it |
|------|-----------------|
| **DP ID** | On the MeroShare login page, open the dropdown. The number in parentheses next to your broker. Example: `NIMB ACE CAPITAL LIMITED (10600)` → enter `10600` |
| **Username** | The number you type into MeroShare's Username field — typically the last 6–7 digits of your BOID. Example: a BOID like `1301010012345678` corresponds to Username `12345678` |
| **Password** | Your MeroShare password |
| **CRN** | Your bank's Customer Reference Number. Visible on the share application form in MeroShare |
| **Transaction PIN** | The 4-digit PIN you enter when applying for shares in MeroShare |

## Dashboard

Once running, the dashboard opens at `http://localhost:5050` with four tabs:

- **Dashboard**. See open issues and apply with one click. Each issue shows per-account chips so you know exactly which of your accounts have already applied (the chips reflect MeroShare's actual records, not just what this tool did locally).
- **History**. Your past applications with allotment results (Allotted / Not Allotted / Pending), filterable and CSV-exportable
- **Settings**. Manage accounts, share-type preferences, the background scheduler, backup/restore, stop the server
- **Logs**. Recent log lines, colored by level (Error / Warning / Info), filterable

### Header quick actions (top right)

| Icon | Use |
|---|---|
| Refresh | Force-refresh all data |
| Schedule | Toggle the background scheduler. Shows time-to-next-run while active. |
| Close | Close the tab. App keeps running in the background |
| Power | Stop everything (GUI + scheduler) |

A small dot next to the icons shows sync state. Green when fresh, yellow when stale, red on errors.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `R` | Refresh all data |
| `1`–`4` | Switch tabs (Dashboard / History / Settings / Logs) |
| `?` | Show keyboard shortcut help |
| `Esc` | Close modal / cancel |
| `Enter` | Confirm modal |

Shortcuts are skipped while you're typing in a form so they don't interfere with passwords.

## Settings

You can configure which types of shares to auto-apply for:

| Setting | Default |
|---------|---------|
| IPO Ordinary Shares | On |
| Right Shares | On |
| FPO | Off |
| Mutual Funds | Off |
| Debentures | Off |
| Default kitta (for IPO) | 10 |
| Apply max eligible for Right Shares | On |
| Max amount per application | Rs. 100,000 |

Change these anytime from the Settings tab or by editing `config.json`.

## Running in the background

### Mac

Open the app (`./run.sh`), go to **Settings → Background Scheduler**, and flip
the toggle on. This installs a macOS launchd agent (`com.meroshare.autoapply`)
that runs the checker on whatever whole-hour interval you pick (1–24).

The scheduler is **independent of the GUI**:
- Closing the `run.sh` terminal does not stop it.
- It survives reboots and resumes after sleep/wake.
- When you reopen the app, the toggle reflects the live state and shows when
  the next check is due.

To turn it off, flip the toggle off in Settings. From the command line:

```
venv/bin/python3 -m scheduler stop      # turn off
venv/bin/python3 -m scheduler status    # check
venv/bin/python3 -m scheduler start 6   # turn on without GUI (interval in hours)
```

`./setup_schedule.sh [hours]` is a shortcut. `./setup_schedule.sh 3` starts
the scheduler at a 3-hour interval, default is 6.

### Windows

Open Task Scheduler and create a task that runs `run.bat` every few hours.

### Environment overrides

| Variable | Effect |
|----------|--------|
| `MEROSHARE_PORT` | Bind port for the Flask GUI (default: `5050`) |
| `MEROSHARE_DATA_DIR` | Where state files (`accounts.json`, `config.json`, `.applied_issues.json`, logs, capital cache) live. Defaults to `~/Library/Application Support/MeroShare Auto-Apply/` on macOS and the project root elsewhere. The `.dmg` launcher sets this automatically. |
| `MEROSHARE_TZ` | IANA timezone used to interpret MeroShare's naive timestamps. Defaults to Asia/Kathmandu (+05:45). Set this only if you've stood up a regional MeroShare mirror in a different zone. |

```
MEROSHARE_PORT=5060 ./run.sh                 # Mac/Linux
set MEROSHARE_PORT=5060 && run.bat           # Windows
MEROSHARE_DATA_DIR=/tmp/meroshare ./run.sh   # alternate state dir
```

## Command line (optional)

If you prefer the terminal:

```
# Activate the environment
source venv/bin/activate        # Mac/Linux
venv\Scripts\activate           # Windows

# List configured accounts (no secrets)
python setup.py --list-accounts

# List open issues for the first account
python auto_apply.py --list

# Same, for a specific account by id
python auto_apply.py --list --account <id>

# Auto-apply now
python auto_apply.py

# Dry run (check without applying)
python auto_apply.py --dry-run

# View application history
python auto_apply.py --status

# Apply for a specific issue manually
python auto_apply.py --apply <company_share_id> --account <id>

# Run continuously (foreground, with SIGTERM clean shutdown)
python auto_apply.py --daemon

# Re-run setup wizard to add another account
python setup.py --reset
```

## API endpoints

The GUI talks to the same Flask app via these JSON endpoints. Useful if you
want to script the tool.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/health` | Liveness + readiness; reports state-file health and uptime. 200 OK / 503 Degraded |
| `GET`  | `/api/version` | Boot timestamp (used by the live-reload poller) |
| `GET`  | `/api/issues` | Open MeroShare issues per account |
| `GET`  | `/api/status?per_account=N` | Application history (default 20 most recent per account) |
| `GET`  | `/api/scheduler` | launchd scheduler state |
| `POST` | `/api/scheduler/start` | Body: `{ "interval_hours": 1..24 }` |
| `POST` | `/api/scheduler/stop` | Stop the launchd agent |
| `POST` | `/api/run-check` | Run a check synchronously in the background |
| `POST` | `/api/apply/<issue_id>` | Apply for one issue across the listed accounts. Body: `{ "account_ids": ["mine", "spouse"], "category": "ipo_ordinary" }`. Category is allowlist-validated; the chosen kitta is the per-account `default_kitta` if set, else the global default. |
| `POST` | `/api/accounts/<id>/test-login` | Verify credentials for one account; returns BOID + DEMAT on success |
| `GET`  | `/api/apply-status` | Polled while a check or apply is running |
| `GET`  | `/api/applied-issues` | Local "already applied" cache |
| `DELETE` | `/api/applied-issues/<account_id>/<issue_id>` | Forget one local entry |
| `DELETE` | `/api/applied-issues/<account_id>` | Forget every local entry for an account |
| `GET`  | `/api/failures` | Recent failed apply attempts (parsed from logs) |
| `POST` | `/api/factory-reset` | Body: `{ "confirm": "WIPE" }`. Wipes accounts, applied state, config, capital cache |
| `POST` | `/api/backup` | Download a backup JSON |
| `POST` | `/api/restore` | Restore from a backup |
| `POST` | `/api/shutdown` | Stop the GUI + scheduler |

State-changing requests must come from a same-origin caller (browser
GUI) or a non-browser local script (curl, etc.). Cross-origin browser
POSTs are rejected via the `Sec-Fetch-Site` header.

## Releasing

The repo ships a GitHub Actions workflow at `.github/workflows/release.yml`
that builds both the macOS .dmg and the Windows .zip on every tag push and
attaches them to the corresponding GitHub Release.

```
# Cut a release for today.
git tag v$(date +%Y.%m.%d)
git push origin --tags
```

That's it. Within ~5 minutes the release page on GitHub has both
artifacts attached. The version embedded in the .dmg / .zip matches the
tag (with the leading `v` stripped. `v2026.05.04` → `2026.05.04`).

### Auto-update flow

The macOS menu bar app polls `https://api.github.com/repos/<your-fork>/releases/latest`
every 6 hours (the unauthenticated rate limit is 60/hr/IP, plenty
headroom). When a release is found whose tag is newer than the bundle's
embedded version, the menu bar:

1. Adds a new top-level item: **Update Available: v2026.05.10 →**
2. Fires a native macOS notification: *"MeroShare Auto-Apply update available"*

Clicking the item opens the .dmg directly in the user's browser. They
download, drag the new .app onto Applications, and replace the old -
same flow as first install. The app does NOT replace itself in place
because that requires a Developer ID code-signing cert the project
doesn't currently have ($99/year Apple Developer Program); replacing
an unsigned binary while it's running breaks Gatekeeper on next launch.

Manual checks: click **Check for Updates…** in the menu. If you're
already up to date, you'll see an alert confirming the version. Dev
builds (anything with `+dev` in the version) skip the auto-prompt
to avoid pestering contributors who are running unreleased code,
but the manual check still works.

The updater is repo-aware. Change `updater.DEFAULT_REPO` if you
fork the project, or set `MEROSHARE_UPDATE_REPO` env var (left as
a follow-up; right now the constant is hard-coded). The Windows
.zip doesn't currently surface the update prompt; a future Windows
"updater.exe" sidecar could do it.

## Building distributables

Two build scripts, both produce self-contained artifacts the recipient
can run with no Python install of their own. Both cache their downloads
in `~/.cache/meroshare-build/` so re-builds are fast.

### macOS. `dist/MeroShare-Auto-Apply.dmg`

```
./scripts/build_dmg.sh
```

Output: `dist/MeroShare-Auto-Apply.dmg` (~85 MB).

What's inside the .app bundle:
- A relocatable Python 3.12.7 from
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
- All pip dependencies (Flask, requests, schedule, Playwright, **rumps**)
- The full project source. Including `menubar.py`, the macOS menu bar app

Recipient experience:
1. Open the .dmg, drag the .app to Applications.
2. **First launch only:** Right-click the .app → Open → Open
   (Gatekeeper bypass. The build is unsigned). A Terminal window
   opens for ~30 seconds while Playwright downloads its Chromium
   browser engine (~150 MB).
3. The **menu bar item** (an outlined "M" icon, top-right of the
   screen) appears. There is no Dock icon (the app runs as
   `LSUIElement=true`). During the brief cold-start window the icon
   carries a "Starting…" label that clears once the dashboard server
   is reachable.
4. **All subsequent launches:** double-click. The menu bar item and
   the dashboard server come up together — clicking "Open Dashboard"
   opens the GUI immediately, no separate spawn step.

#### Menu bar item

Click the menu bar icon to get:

- **Status header** with version, scheduler state, next-run time, open-issue count
- **Open Dashboard** (⌘O), **Run Check Now** (⌘R)
- **Open Issues** submenu listing each open issue, deduped by company-share-id, with per-issue navigation
- **Accounts** submenu with each account's DP/BOID/bank and a one-click "Test login"
- **Background Scheduler** submenu with on/off toggle and 1/3/6/12/24-hour interval picker (checkmark shows current)
- **Preferences** submenu toggles share-type auto-apply (IPO / Right / FPO / Mutual Fund / Debenture), shows kitta and max-amount, and exposes a **Launch at login** toggle
- **Update Available** appears at the top when GitHub Releases has a newer version; click to download
- **Quit** (⌘Q) tears the whole tool down — menu bar, dashboard server, and launchd scheduler — with a confirmation prompt. Clicking **Stop everything** in Settings → Server does the same thing from the dashboard side; the menu bar exits in lockstep with the server.

The status line refreshes every 30 seconds in the background. All HTTP
calls run on worker threads so the menu bar never freezes.

#### Launch at login

The Preferences submenu has a **Launch at login** toggle that installs a
per-user LaunchAgent at `~/Library/LaunchAgents/com.meroshare.menubar.plist`
pointing at this Python and `menubar.py`. macOS starts it on every sign-in.
Toggle off to remove. No admin rights required.

#### Code signing

The build is ad-hoc-codesigned (`codesign -s -`) so the bundle is
sealed against tampering. Without an Apple Developer ID
($99/year), Gatekeeper still warns on first launch. That's
inherent to non-Developer-ID distribution. To remove the warning
entirely, you'd `codesign --deep --sign "Developer ID Application:
..."` and `xcrun notarytool submit ...`; the build script has
hooks for both if you ever get a cert.

Optional: `brew install create-dmg` for a prettier .dmg window
layout; the build falls back to plain `hdiutil` otherwise.

### Windows. `dist/MeroShare-Auto-Apply-windows.zip`

```
./scripts/build_windows.sh
```

Output: `dist/MeroShare-Auto-Apply-windows.zip` (~12 MB).

What's inside:
- Python 3.12.7 embeddable distribution from python.org
- `get-pip.py` (offline pip bootstrap)
- The full project source
- A top-level `Run MeroShare Auto-Apply.bat` that does first-launch
  setup and subsequent launches transparently
- A plain-text `README.txt` with one-paragraph instructions

Recipient experience:
1. Extract the .zip anywhere (Documents/, Desktop/, etc.).
2. Double-click `Run MeroShare Auto-Apply.bat`.
3. **First launch only:** the .bat shows three numbered steps -
   pip bootstrap, dependency install, Chromium download. ~2-5
   minutes total depending on internet speed.
4. **All subsequent launches:** the .bat detects the marker file
   and skips straight to launching the app.

The Windows .zip is cross-built on macOS (no Windows machine
required) but only ships amd64 Python. The vast majority of
Windows users. ARM64 Windows users would need to pull a different
embeddable from python.org. To uninstall, the user just deletes
the extracted folder.

## Tests

```
./venv/bin/python -m unittest discover -s tests
./venv/bin/python -m ruff check .
```

The unit suite is offline (every HTTP call mocked) and runs in
under a second. CI runs both on every push and on every tag. The
menu bar tests skip cleanly on non-darwin runners.

## Privacy & Security

Credential storage uses two independent layers:

1. **OS keystore for irrevocable secrets** (password, CRN, PIN). On macOS this is the system Keychain; on Windows the Credential Manager (DPAPI); on Linux the Secret Service (gnome-keyring / KWallet). The keystore is encrypted at rest by the platform and protected by your login password. Other processes running as you can't read it without prompting (macOS shows a "Keychain Access" prompt the first time, with an "Always Allow" cache after).
2. **AES-256-GCM file encryption** for the rest of `accounts.json` (account names, DEMAT IDs, BOIDs, bank preferences, default kitta). The 256-bit key lives in the keystore. Even with read access to the file (Time Machine backup, stolen disk image), an attacker needs the keystore — i.e. your login password — to decrypt the metadata.

Other protections:

- `accounts.json` is written with mode 0o600 (only your user can read it). This is the *floor*; layers 1+2 add real protection on top.
- The tool only talks to official MeroShare servers (`cdsc.com.np`).
- `accounts.json` is excluded from git so your details can't accidentally be shared.
- All actions are logged locally in the `logs/` folder, mode 0o600, with rotation at 2 MB / 5 backups. Error paths route through `_safe_exc()` which scrubs `password=`, `pin=`, and `crn=` patterns from any string before it lands on disk.
- Backup files (`/api/backup`) are plaintext — that's by design, since the destination machine's keystore is empty. Treat the backup file the same way you'd treat a written-down password.
- Sensitive credentials are never logged, never echoed to API responses, and never left in process memory longer than the apply call.
- The GUI binds to `127.0.0.1` only and rejects cross-origin browser requests via the `Sec-Fetch-Site` header. Override the bind port with `MEROSHARE_PORT`. Override the timezone used to interpret MeroShare's naive timestamps with `MEROSHARE_TZ` (defaults to Asia/Kathmandu).

### What protection means in practice

| Threat | Protected? |
|---|---|
| Other macOS users on the same machine read `accounts.json` | ✅ Mode 0o600 + AES envelope |
| Malware running as you reads `accounts.json` | ✅ Body is AES-encrypted; key lives in keystore (separate ACL on macOS) |
| Stolen laptop, FileVault off | ✅ AES envelope holds even with disk image access; keystore items are still encrypted |
| Stolen laptop, logged in & unlocked | ⚠️ Same threat model as your Mail / Notes — once unlocked, "as you" malware can ask the keystore |
| Time Machine backup leaked | ✅ Both keychain and accounts.json encrypted |
| User accidentally exfiltrates via `/api/backup` | ⚠️ By design plaintext — the user must store the backup file safely (encrypted disk, password manager) |

### Migration & moving machines

When you move to a new machine, **use Settings → Backup & restore** rather than copying `accounts.json` directly. The encrypted file is only readable on the machine where its key was generated; copying it without the keystore leaves you locked out. The backup endpoint exports plaintext for portability — store the backup file somewhere safe (encrypted disk, password manager).

If you build the app from source on Linux without a working Secret Service backend (headless / no DBus), the tool falls back to the legacy "plaintext in `accounts.json`, mode 0o600" path. That's still functional; it's just the protection level you'd have had before this version.

### Heads-up: macOS Keychain prompt on first scheduler run

The first time the **launchd-spawned scheduler** tries to read your credentials from the Keychain, macOS will show a prompt that looks roughly like:

> "python" wants to use your confidential information stored in "com.meroshare.autoapply" in your keychain.

**Click "Always Allow"**, not just "Allow" — single-click "Allow" works for one access only, and the scheduler reads credentials on every cycle. Without "Always Allow", the next cycle prompts again, and on a launchd-spawned process there's no GUI to click, so the read fails silently and the cycle skips that account.

The GUI (Flask process started by the menu bar app) prompts separately. Both prompts appear on first install — click "Always Allow" on each.

If you accidentally clicked "Deny": open **Keychain Access**, search for `com.meroshare.autoapply`, and either delete the entries (the next test-login re-creates them with a fresh prompt) or right-click the entry → Access Control → add the binary back to the Always Allow list.

## Run the dashboard as a real Mac app (Sonoma 14.4+)

The dashboard is a Flask web GUI by default — open it in your browser. If you'd prefer it to feel like a native macOS app (real window with traffic-light buttons, Dock icon, Cmd+Tab presence, native shortcuts), Safari has a built-in shortcut for that:

1. Make sure the menu bar app is running (so `localhost:5050` is alive). Either double-click the .app, or click the **M** in your menu bar → **Open Dashboard**.
2. In Safari, navigate to `http://localhost:5050`.
3. **File → Add to Dock…** (or Share menu → Add to Dock).
4. Give it a name and icon. Done — Safari creates a real Web App in `~/Applications/`.

From now on you can launch the dashboard from the Dock or Launchpad just like any other Mac app. It runs in its own window, has Cmd+W / Cmd+R / Cmd+Q natively, and shows up in Mission Control. The Flask backend keeps running in the menu bar; the Web App is just the UI surface.

> **Note:** the Web App needs the menu bar app running first (otherwise localhost:5050 returns "can't connect"). If you want the Web App to launch the menu bar automatically, set the menu bar app to **Settings → Preferences → Launch at login**.

## Troubleshooting

### "The scheduler stopped running"

The dashboard now shows a red banner when the configured interval has passed without a successful check. To diagnose:

1. **Check the launchd state:**
   ```
   launchctl list | grep meroshare
   ```
   No output → the agent unloaded. Settings → Background scheduler → toggle off, toggle on.

2. **Check `~/Library/Application Support/MeroShare Auto-Apply/logs/meroshare.log`:**
   ```
   tail -100 ~/Library/Application\ Support/MeroShare\ Auto-Apply/logs/meroshare.log
   ```
   Look for `[ERROR]` lines or `Login failed for all accounts`. The tail is what the **Logs tab** shows in the GUI.

3. **Verify accounts.json readability:**
   ```
   ls -la ~/Library/Application\ Support/MeroShare\ Auto-Apply/accounts.json
   ```
   Must be `-rw-------` (mode 600) and owned by you. If permissions drifted, the scheduler can't read credentials.

4. **MeroShare WAF blowback:** if you've been hitting it hard, MeroShare can throttle logins for 5–60 minutes per IP. Check the log for HTTP 503 / "rate_limited". Wait it out.

5. **Bundled Python missing or moved:** the scheduler points launchd at the venv's Python (or the bundled one inside the .app). If you moved the .app or deleted the venv, the agent points at a non-existent path and silently fails. Re-toggle the scheduler to refresh the path.

### "Apply form did not load"

MeroShare changes their HTML occasionally, and `browser_apply.py` uses Playwright selectors that match the form structure. If a release breaks the selectors:

1. Check if there's a newer release at the project's GitHub Releases page.
2. As a workaround, apply manually on MeroShare directly. The tool's local cache will pick up your manual application on the next check via the application-report sync.

### "Login failed: rate_limited"

MeroShare has been more aggressive with WAF rate-limiting since 2025. The tool already paces logins, but a multi-account run that fails mid-cycle and retries can trip the limit. Wait 5–10 minutes, then re-trigger via Run check now.

## FAQ

**Will this get my account banned?**
The tool adds natural pauses between actions and uses a real browser for applying. It only checks for new issues every few hours. Use at your own discretion.

**Does it work on Windows?**
Yes. Double-click `run.bat`.

**Can I use multiple accounts?**
Yes. Open Settings → Accounts → "+ Add Account". Each account is checked and applied for independently. The dashboard shows per-account state on every issue, and the Apply button applies for all accounts that haven't applied yet.

**How do I know it applied?**
Check the History tab, the logs, or the desktop notification.

**What if something goes wrong?**
Check the Logs tab. If the application fails, you'll see the error there. You can always apply manually on MeroShare as a fallback.

## Disclaimer

This is a personal tool. Always verify your applications on MeroShare directly. The author is not responsible for any financial decisions or missed applications. Use at your own risk.

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) — see `LICENSE` for the full text.

**TL;DR:** free for personal use, hobby projects, education, research, and any noncommercial use. You can read the source, fork it, modify it, and run your own copy on your own MeroShare account, no permission needed. **Selling, hosting as a paid service, or white-labeling requires a separate commercial license** — email <bishalmodern@gmail.com> if you want to discuss one.

This is a *source-available, noncommercial* license — not OSI-approved open source. The choice is deliberate: this tool handles real money on behalf of users, and a permissive license like MIT would let third parties package it as a paid product without any accountability to the original author. PolyForm-NC keeps the code auditable and forkable while reserving commercial deployment to the copyright holder.
