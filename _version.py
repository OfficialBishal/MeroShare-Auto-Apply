"""Application version.

This file is OVERWRITTEN at build time by `scripts/build_dmg.sh` /
`scripts/build_windows.sh`, which read the active git tag (or fall
back to a date-stamped `+dev` version for local builds).

The dev sentinel `0.0.0+dev` is recognized by the updater as
"running unreleased code" and skips the periodic update prompt so
contributors aren't pestered with their own diff. Manual update
checks still work (they show "you're on a development build").
"""

__version__ = "0.0.0+dev"
