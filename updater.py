"""GitHub Releases-based update check.

Polls https://api.github.com/repos/<owner>/<repo>/releases/latest,
compares its `tag_name` to the running version, and returns release
metadata when a newer version is available.

Pure Python. Uses `requests`, already a project dependency. No
auth token needed; the GitHub API allows 60 unauthenticated requests
per hour per IP, which is plenty for a "check every 6 hours" cadence
even in a paranoid debug session.

The update flow does NOT auto-install. Without an Apple Developer
ID, replacing a running .app on disk is unreliable: macOS treats
the result as a "modified" binary and Gatekeeper-blocks subsequent
launches. The menu bar instead exposes "Update Available" which on
click opens the new .dmg in the user's browser; the user drags the
new .app onto Applications as on first install.

If you ever sign with a Developer ID + notarize, swap in Sparkle
or a similar framework for in-place updates.
"""
from __future__ import annotations

import logging
import platform
from typing import Optional, TypedDict

import requests

logger = logging.getLogger("meroshare.updater")

DEFAULT_REPO = "OfficialBishal/MeroShare-Auto-Apply"
DEFAULT_TIMEOUT_S = 8
DEV_SUFFIX = "+dev"


class ReleaseInfo(TypedDict):
    """Subset of GitHub's release JSON we care about."""
    version: str       # tag without leading 'v'
    url: str           # html_url. The human-readable release page
    notes: str         # body. The markdown release notes
    asset_url: Optional[str]  # direct .dmg download (preferred when present)


def _parse_version(s: str) -> tuple[int, ...]:
    """Parse a CalVer-ish version string into a comparable tuple.

    Accepts things like '2026.05.04', 'v2026.05.04', '2026.05.04.1',
    '0.0.0+dev'. Strips any leading 'v' and trailing '+suffix'.
    Non-numeric parts compare as 0. So '+dev' (which becomes a
    bare suffix we drop) just isn't part of the comparison key.

    Returning a tuple lets Python's tuple comparison do the work:
        _parse_version('2026.05.04') < _parse_version('2026.05.05')
    """
    # Drop a pre-release suffix ('-rc1', '-beta') like we drop '+dev'. Splitting
    # on '-' as a separator (the old behavior) made '2026.05.04-rc1' parse to
    # (2026,5,4,0), which sorts ABOVE the final '2026.05.04' — backwards.
    s = s.strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    if not s:
        return (0,)
    parts = s.split(".")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def is_dev_build(version: str) -> bool:
    """True when the running version is the unstamped dev sentinel.

    Used by the menu bar's auto-check to skip update prompts for
    contributors running from source. The comparison would always
    show "update available" otherwise.
    """
    return DEV_SUFFIX in version


def _pick_asset_url(assets: list, *, prefer_arch: bool = True) -> Optional[str]:
    """Pick the right .dmg asset from a release's asset list.

    When the release ships multiple .dmgs (e.g. per-arch), prefer the
    one matching the current host's architecture. Falls back to the
    first .dmg when there's no match. Future-proof for the day we
    cross-build both arm64 and x86_64.
    """
    dmgs = [a for a in assets if (a.get("name") or "").endswith(".dmg")]
    if not dmgs:
        return None
    if prefer_arch:
        arch = platform.machine().lower()
        # python-build-standalone uses 'aarch64' for arm64, but the
        # .dmg names tend to use 'arm64'. Match both spellings.
        arch_aliases = {"arm64", "aarch64"} if arch == "arm64" else {arch}
        for asset in dmgs:
            name = (asset.get("name") or "").lower()
            if any(a in name for a in arch_aliases):
                return asset.get("browser_download_url")
    return dmgs[0].get("browser_download_url")


def check_for_updates(
    current_version: str,
    repo: str = DEFAULT_REPO,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    session: Optional[requests.Session] = None,
) -> Optional[ReleaseInfo]:
    """Return release info if a newer release exists, else None.

    Returns None on any failure path. Network error, parse error,
    repo with no releases, current version >= latest tag. Caller
    can't distinguish "up to date" from "couldn't reach GitHub",
    which is intentional: we don't want to nag the user on a flaky
    network. Failures are logged at DEBUG so they're inspectable
    without spamming the main log.

    `session` is for tests; production callers leave it None.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        # GitHub requires a User-Agent on API calls; identifying the
        # tool here also helps when debugging rate limits.
        "User-Agent": "MeroShare-Auto-Apply-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    client = session or requests

    # Try /releases/latest first — that's the canonical endpoint and
    # cheapest. Fall back to /releases (list) on 404, which we've
    # observed on freshly-created repos where /releases/latest hasn't
    # been indexed yet (the UI shows the "Latest" badge but the API
    # endpoint returns 404 for several minutes after the first push).
    # The list endpoint always works and the first entry is the most
    # recent published release.
    data = None
    try:
        resp = client.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=timeout_s, headers=headers,
        )
    except requests.RequestException as e:
        logger.debug("Update check (latest) fetch failed: %s", e)
        return None
    if resp.status_code == 403:
        logger.debug("Update check rate-limited (HTTP 403)")
        return None
    if resp.status_code == 404:
        # Either the repo has no releases yet OR the /latest endpoint
        # is lagging behind /releases. Try the list endpoint.
        try:
            list_resp = client.get(
                f"https://api.github.com/repos/{repo}/releases",
                timeout=timeout_s, headers=headers,
            )
        except requests.RequestException as e:
            logger.debug("Update check (list fallback) fetch failed: %s", e)
            return None
        if not list_resp.ok:
            logger.debug("Update check list fallback HTTP %s", list_resp.status_code)
            return None
        try:
            releases = list_resp.json()
        except ValueError:
            return None
        if not isinstance(releases, list) or not releases:
            return None
        # First non-draft, non-prerelease entry. The list is sorted
        # newest-first, so the first match is the right one.
        for r in releases:
            if not r.get("draft") and not r.get("prerelease"):
                data = r
                break
        if data is None:
            return None
    elif not resp.ok:
        logger.debug("Update check HTTP %s", resp.status_code)
        return None
    else:
        try:
            data = resp.json()
        except ValueError:
            logger.debug("Update check returned non-JSON")
            return None

    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None

    # The dev sentinel always parses as (0,) so any released version
    # is "newer". Keep the comparison consistent. Manual check
    # callers want to know they're on dev, but the auto-check should
    # still treat dev as "ahead of any release" via is_dev_build().
    if not is_dev_build(current_version) and \
       _parse_version(tag) <= _parse_version(current_version):
        return None

    return ReleaseInfo(
        version=tag.lstrip("vV"),
        url=data.get("html_url") or f"https://github.com/{repo}/releases/latest",
        notes=data.get("body") or "",
        asset_url=_pick_asset_url(data.get("assets") or []),
    )
