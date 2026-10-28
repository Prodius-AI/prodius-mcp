#!/usr/bin/env python3
"""Fail fast if the pyproject.toml version is already on PyPI.

PyPI rejects re-uploads of any version that has ever existed for a project,
including yanked versions. Catching that locally (and in CI before twine
upload) is the only way to avoid a confusing twine failure mid-release.

Stdlib only — runs on Python 3.10+ without tomllib (Python 3.10 doesn't have
it). Uses a regex against pyproject.toml, matching the existing
``tests/test_version.py`` design.

Usage::

    # Read pyproject and check against PyPI
    python ci/version_guard.py [--package prodius-mcp] [--pyproject pyproject.toml]

    # Or check an explicit version string
    python ci/version_guard.py --version 0.3.0 [--package prodius-mcp]

Exit codes:
    0 — version is unpublished, safe to upload.
    1 — version is already on PyPI; bump pyproject before publishing.
    2 — unexpected error (network, parse, etc.). Treated as failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_PACKAGE = "prodius-mcp"
DEFAULT_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
PYPI_JSON_TEMPLATE = "https://pypi.org/pypi/{package}/json"


def read_pyproject_version(pyproject_path: Path) -> str:
    """Extract the ``version = "..."`` value from pyproject.toml.

    Uses a regex rather than tomllib to stay 3.10-compatible (matches the
    package's ``requires-python = ">=3.10"``).
    """
    text = pyproject_path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit(
            f"version_guard: no `version = \"...\"` line in {pyproject_path}"
        )
    return match.group(1)


def fetch_published_versions(package: str, timeout: float = 10.0) -> set[str]:
    """Return the set of versions PyPI has ever held for ``package``.

    A 404 means the project name is unclaimed (returns an empty set).
    Any other failure raises — we'd rather fail noisily than silently allow
    an upload that might conflict.
    """
    url = PYPI_JSON_TEMPLATE.format(package=package)
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return set()
        raise SystemExit(
            f"version_guard: PyPI returned HTTP {exc.code} for {url}"
        ) from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"version_guard: failed to query PyPI: {exc}"
        ) from exc
    releases = payload.get("releases") or {}
    return set(releases.keys())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help=f"PyPI package name (default: {DEFAULT_PACKAGE}).",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help="Path to pyproject.toml (default: alongside this script's package).",
    )
    parser.add_argument(
        "--version",
        help="Explicit version string. Overrides pyproject.toml parsing.",
    )
    args = parser.parse_args(argv)

    version = args.version or read_pyproject_version(args.pyproject)
    print(f"version_guard: checking {args.package}=={version} against PyPI")

    published = fetch_published_versions(args.package)
    if not published:
        print(
            f"version_guard: PyPI has no releases of {args.package} yet — "
            f"version {version} is uploadable."
        )
        return 0

    if version in published:
        print(
            f"version_guard: ERROR — {args.package}=={version} already exists "
            f"on PyPI (even yanked versions are reserved). "
            f"Bump the version in {args.pyproject} before publishing."
        )
        return 1

    # Don't try to label one of these "latest" — sorting version strings
    # lexically misorders `0.10.0` vs `0.9.0`, and we don't want to pull in
    # `packaging` just for the log line.
    print(
        f"version_guard: OK — {args.package}=={version} is not on PyPI "
        f"({len(published)} prior release(s) on record)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
