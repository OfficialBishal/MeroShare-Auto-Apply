# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [calendar versioning](https://calver.org/) (`YYYY.MM.DD`).

When you cut a release, move the "Unreleased" section under a new
`## [YYYY.MM.DD]` heading dated for the tag push. Compare links at
the bottom keep the diff between versions one click away.

## [Unreleased]

## [2026.05.07]

### Added — Security

- **OS keystore for irrevocable credentials.** Password, CRN, and
  Transaction PIN now live in the platform-native encrypted store:
  macOS Keychain, Windows Credential Manager (DPAPI), or Linux Secret
  Service. The plaintext copy is removed from `accounts.json`. Falls
  back to legacy mode-0o600 plaintext when no keystore is reachable
  (headless Linux without DBus).
- **AES-256-GCM file encryption** for `accounts.json` body. Even
  metadata (account names, DEMAT IDs, BOIDs, bank preferences,
  `default_kitta`) is encrypted. Per-install random key stored in the
  keystore — both the file and the keystore must be compromised
  together to read. Defense-in-depth on top of the keystore layer.
- Lazy plaintext-→-keystore migration on first load after upgrade —
  existing installs move secrets into the keystore transparently.
- Cascade-delete of keystore entries on `accounts.delete()` and
  `/api/factory-reset` so credentials don't strand in the OS store.
- New `secrets_store.py` module with isolated unit tests; tests use
  an in-memory `FakeKeystoreMixin` so the real OS keychain is never
  touched in CI.

### Added — Product

- **Pre-flight cost preview** in the Apply confirm modal: "≈ Rs.
  1,50,000 across 3 account(s) at Rs. 500/share." Uses Indian/Nepali
  lakh-style grouping to match what users see on MeroShare itself.
- **Closing-soon urgency badge** on the Open Issues list. Red +
  pulsing under 24h, yellow under 3 days, blue beyond. Issues sort
  most-urgent first.
- **Per-account `default_kitta` override.** Lets multi-account users
  size their primary account at 50 kitta and a relative's at 10
  without flipping the global setting between cycles.
- **Lifetime stats card** on the dashboard: Applied / Allotted / Not
  allotted / Pending counts pulled from `/api/status`.
- **CRN / password expiry warnings.** `expiredDate` and
  `passwordExpiryDate` from MeroShare are persisted in localStorage
  (14-day TTL) when the user runs Test login; dashboard shows a red
  banner ≤7 days, yellow ≤30 days. Per-account expiry line in the
  Settings → Accounts list.
- **Allotment-status notifications.** Desktop toast fires exactly
  once when an application transitions Pending → Allotted / Not
  Allotted. State persisted in `.allotment_status.json`. First
  observation of an already-final state on a fresh install does NOT
  toast (avoids "Shares allotted!" for something allotted weeks ago).
- **Scheduler health banner** on the dashboard. When the scheduler
  is enabled but its last successful run is overdue (≥1.5× the
  configured interval) the dashboard shows a yellow / red banner
  with diagnosis hints. Closes the silent-dead-scheduler bug class.
- **Inline help text** under each credential input on the account
  form (DP ID, BOID, Password, PIN, CRN). No more alt-tabbing to
  the README to remember what a CRN is.
- **Apply button shows "Applying… (spinner)"** immediately on
  confirm. Closes the visual gap between click and the post-apply
  issues refresh.
- Support-development section in Settings: GitHub Sponsors link +
  "email me for paid setup help" link. Tip-jar, not paywall.

### Added — Other

- macOS menu bar app (`menubar.py`) with status header, open-issues
  submenu, accounts submenu with test-login shortcut, scheduler
  interval picker, preferences submenu including Launch-at-login.
- Bundled-Python macOS `.dmg` build (`scripts/build_dmg.sh`) using
  `python-build-standalone`; recipient does not need Python installed.
- Portable Windows `.zip` build (`scripts/build_windows.sh`) with
  Python embeddable + first-launch pip bootstrap.
- GitHub Releases-based auto-update channel (`updater.py`); menu
  bar polls every 6 hours and notifies on new versions.
- Release pipeline (`.github/workflows/release.yml`): tag push
  triggers macOS .dmg + Windows .zip builds and uploads as release
  assets.
- Health (`/api/health`), failures (`/api/failures`), factory-reset
  (`/api/factory-reset`), per-account applied-cache delete, and
  test-login endpoints.
- Cross-process file lock around `.applied_issues.json` writes.
- State migration: dev-mode (project root) state moves automatically
  to `~/Library/Application Support/MeroShare Auto-Apply/` so dev
  and bundled installs share one location.
- Shutdown sentinel: GUI's "Stop everything" now also exits the
  menu bar (previously the menu bar would re-spawn Flask on next
  Open Dashboard click).
- `MEROSHARE_PORT` and `MEROSHARE_TZ` environment overrides.
- Origin / `Sec-Fetch-Site` CSRF guard on state-changing endpoints.
- Ad-hoc codesign of the .app bundle so macOS can detect tampering.
- Diagnostic full-page screenshot on "Could not confirm result"
  apply outcomes (saved to `logs/screenshots/`); gives the user
  visual evidence to disambiguate slow-submit from rejected.

### Changed

- `accounts.STATE_DIR` resolution now prefers `~/Library/Application
  Support/MeroShare Auto-Apply/` on macOS regardless of launch path.
- `MeroShareClient` retries 5xx with backoff and times out per call.
- Issue classifier returns `"unknown"` for unrecognized share types
  rather than silently treating them as ordinary IPOs.
- Browser apply pre-flight checks `applicantForm/active/search` so
  already-submitted issues don't trigger re-apply attempts.
- Browser apply success matcher accepts more wording variants
  (`applied`/`submitted` successfully). Post-apply confirmation
  timeout raised 10s → 20s + dropped a redundant pre-check sleep
  to absorb peak-load latency without false "Could not confirm".
- Plist generator XML-escapes interpolated paths.
- Build .dmg includes a custom .icns generated from the favicon SVG.
- Menu bar item is now icon-only (no "MS" text fallback). Source
  tree ships `static/menubar-icon.png` so `./run.sh` mode also has
  the icon (previously only the bundled `.dmg` had one).
- `check_and_apply` writes go through `accounts.update_applied`
  (lock-protected read-modify-write) instead of snapshot-save —
  eliminates the lost-update race where a parallel GUI apply got
  silently clobbered.
- `_ALREADY_APPLIED_PHRASES` tightened to anchor on first/second
  person constructions ("you have applied", "have already applied")
  so descriptive third-person help-banner text can't false-fire.

### Fixed

- **Financial correctness:** `api_apply` now plumbs `share_price`
  from the cached applicable-issues so `browser_apply.py`'s
  `max_amount` cap converts to kitta at the *real* price. Without
  this a Rs. 500/share FPO got capped at 5× the user's intended
  budget. Also: `category` field is allowlist-validated; a wrong-
  cased "RIGHT_SHARE" used to silently bypass `apply_max` for right
  shares.
- **BOID mismatch in apply form** now aborts before submit instead
  of warning-and-continuing — prevents applying with a mismatched
  session.
- **Server-side "already applied" detections** are no longer
  reported as fresh applications. Auto-apply distinguishes the two
  via a new `already_applied` flag and suppresses the misleading
  "Share Applied!" toast when the result was actually a duplicate
  rejection from MeroShare.
- `UnboundLocalError` in `check_and_apply` on non-HTTP exceptions.
- Settings disappearing when switching between dev (`./run.sh`) and
  installed-app launch paths.
- "Stop everything" from the GUI not actually stopping the menu bar.
- macOS menu bar dialogs hiding behind other windows (LSUIElement
  apps lose foreground state); `_show_dialog` now calls
  `NSApp.activateIgnoringOtherApps_` and falls back to osascript.
- Em-dash overuse across the codebase (mechanical sweep + manual
  follow-up cleanup).
- Various race conditions in `_bg_status` / sentinel handling.

### Security

- `/api/factory-reset` requires `{"confirm": "WIPE"}` body AND now
  purges keystore entries for every deleted account.
- Generic 401 message on login-all-failed (no per-account name leak).
- `_safe_exc` redacts password/pin/crn patterns from logged
  exceptions; round-10 audit fixed two remaining paths
  (`api_test_account_login` and `do_apply`'s exception handler) that
  bypassed it and would have echoed Playwright traceback frames or
  MeroShare's 400-response payload back to the client / log file.
- `api_restore` validates each backup record through
  `accounts.validate_record` before writing — empty-credentialed
  backups no longer silently land on disk.
- Per-record `default_kitta` validation: bounded to 1–100,000.

### Removed

- `MeroShareClient.get_portfolio()`. The Portfolio dashboard tab was
  pulled in an earlier release as too aggressive on MeroShare's WAF;
  the underlying API method had no callers since then.

### License

- **Relicensed from MIT to PolyForm Noncommercial 1.0.0.** The tool
  handles real-money credentials and applies for shares on the user's
  behalf; a permissive license like MIT would let third parties
  package and sell it without accountability to the original author.
  PolyForm-NC keeps the code source-available, auditable, and
  forkable for personal use while reserving commercial deployment
  (sale, paid hosting, white-labeling) to the copyright holder. The
  switch is opt-in for new installs; copies obtained under MIT keep
  their MIT rights for that snapshot.

### Planned (not yet shipped)

- **Native macOS dashboard window** (replace localhost-in-browser
  with a real `NSWindow` + WKWebView). Initial brainstorm assumed a
  trivial PyWebView wrap; the reality is `rumps.App().run()` and
  `webview.start()` both want to own the main `NSApplication` run
  loop, so the right fix is either a subprocess-based dashboard
  process or a full PyObjC NSStatusItem + NSWindow rewrite. Tracked
  as a focused follow-up. Until then, macOS Sonoma 14.4+ users can
  Safari → File → Add to Dock to get a near-native window experience
  with zero code change (see README "Run the dashboard as a real Mac
  app").

[Unreleased]: https://github.com/OfficialBishal/MeroShare-Auto-Apply/compare/v2026.05.07...HEAD
[2026.05.07]: https://github.com/OfficialBishal/MeroShare-Auto-Apply/releases/tag/v2026.05.07
