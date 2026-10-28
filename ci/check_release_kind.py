#!/usr/bin/env python3
"""Assert pyproject.toml's version is (or isn't) a PEP 440 pre-release.

Used by the publish-pypi-beta and publish-pypi-stable CI jobs to make a
misfired manual trigger fail loudly instead of corrupting the version stream.

A pre-release per PEP 440 is any version with an ``aN`` / ``bN`` / ``rcN``
suffix on the release segment (e.g. ``0.3.0b1``, ``1.0.0rc2``,
``2.1.0a3.dev1``). Development releases (``.devN``) and post-releases
(``.postN``) are NOT pre-releases — they're separately classified by PEP 440.

Stdlib only. Mirrors `packaging.version.Version.is_prerelease` for the
common cases we care about, without pulling in the ``packaging`` dependency.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# Matches the pre-release marker per PEP 440 §Pre-release segments.
# Accepts: aN / alphaN / bN / betaN / cN / rcN / preN / previewN, with the
# optional [._-] separators PEP 440 allows on either side of the marker.
# The marker attaches directly to the release segment (e.g. ``0.3.0b1``)
# so we don't require a left boundary — just the marker token. The trailing
# digit count is optional because PEP 440 allows ``1.0a`` (normalized to
# ``1.0a0``), and skipping it would let an implicit-number pre-release slip
# past `--require-prerelease`. To guard against false positives in odd
# local-version segments, the version is anchored after stripping ``+local``
# and the marker must be followed by either end-of-string, digits, or one
# of the separators PEP 440 uses before the next segment.
_PRERELEASE_RE = re.compile(
    r"(?:a|alpha|b|beta|c|rc|pre|preview)"   # marker (PEP 440 §Pre-release)
    r"[._-]?"                                 # optional separator
    r"\d*"                                    # optional pre-release number
    r"(?=\.|$|\+|-|_)",                       # followed by segment boundary
    re.IGNORECASE,
)

# Matches a PEP 440 development release suffix (.devN). A dev release is
# *not* a pre-release per PEP 440 classification, but it's also not a
# stable release — `publish-pypi-stable` should reject it.
_DEV_RELEASE_RE = re.compile(
    r"(?<=\d)"        # must follow a digit (the release segment's tail)
    r"\.?-?_?"        # optional separator
    r"dev\d*"         # the dev marker plus its optional number
    r"$",             # only the tail of the version (post-local strip)
    re.IGNORECASE,
)


def read_pyproject_version(pyproject_path: Path) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit(
            f"check_release_kind: no `version = \"...\"` line in {pyproject_path}"
        )
    return match.group(1)


def is_prerelease(version: str) -> bool:
    """Return True iff the version carries a PEP 440 pre-release marker."""
    # Strip local version (``+local``) — PEP 440 disallows pre-release tags there.
    base = version.split("+", 1)[0]
    return bool(_PRERELEASE_RE.search(base))


def is_dev_release(version: str) -> bool:
    """Return True iff the version carries a PEP 440 dev-release marker (``.devN``)."""
    base = version.split("+", 1)[0]
    return bool(_DEV_RELEASE_RE.search(base))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help="Path to pyproject.toml.",
    )
    parser.add_argument(
        "--version",
        help="Explicit version string. Overrides pyproject parsing.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--require-prerelease",
        action="store_true",
        help="Exit non-zero if the version is NOT a pre-release.",
    )
    mode.add_argument(
        "--reject-prerelease",
        action="store_true",
        help="Exit non-zero if the version IS a pre-release.",
    )
    args = parser.parse_args(argv)

    version = args.version or read_pyproject_version(args.pyproject)
    pre = is_prerelease(version)
    dev = is_dev_release(version)

    if args.require_prerelease and not pre:
        print(
            f"check_release_kind: ERROR — version '{version}' is not a "
            "pre-release. Beta publishes require an aN/bN/rcN suffix "
            "(e.g. 0.3.0b1)."
        )
        return 1

    if args.reject_prerelease and pre:
        print(
            f"check_release_kind: ERROR — version '{version}' is a "
            "pre-release. Stable publishes must drop the aN/bN/rcN suffix."
        )
        return 1

    # Stable also rejects development releases — `.devN` is not a pre-release
    # per PEP 440 classification, but it's not a final release either, and
    # shipping a `.devN` from the stable branch would silently land an
    # incomplete build at the default `pip install prodius-mcp` resolution.
    if args.reject_prerelease and dev:
        print(
            f"check_release_kind: ERROR — version '{version}' is a "
            "development release (.devN). Stable publishes must use a final "
            "release version."
        )
        return 1

    if pre:
        kind = "pre-release"
    elif dev:
        kind = "development release"
    else:
        kind = "stable"
    print(f"check_release_kind: OK — '{version}' is a {kind} version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
