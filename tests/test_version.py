"""Guards the single-source-of-truth version design for prodius-mcp.

The version lives in exactly one place: ``pyproject.toml``. The package
exposes it as ``prodius_mcp.__version__`` by reading installed metadata at
import time (see ``src/prodius_mcp/__init__.py``), with a dev sentinel
fallback for source-only runs. ``__version__`` is broadcast to the desktop
app in the MCP ``initialize`` handshake, so a hardcoded literal that drifted
from ``pyproject.toml`` would ship a wheel that lies about its own version —
these tests make that impossible to do silently.
"""

import importlib.metadata
import re
from pathlib import Path

import prodius_mcp

_DEV_SENTINEL = "0.0.0+dev"


def _pyproject_version() -> str:
    """The single source of truth, read straight from pyproject.toml.

    Uses a regex rather than tomllib so the test runs on Python 3.10
    (tomllib is 3.11+), matching the package's ``requires-python``.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    assert match, "no version found in pyproject.toml"
    return match.group(1)


def test_version_resolves_consistently():
    """__version__ equals the installed metadata, or the dev sentinel in source runs."""
    try:
        installed = importlib.metadata.version("prodius-mcp")
    except importlib.metadata.PackageNotFoundError:
        # Source/editable run without an install (e.g. CI puts src/ on the
        # path via conftest) — the sentinel is expected.
        assert prodius_mcp.__version__ == _DEV_SENTINEL
        return
    # Installed: metadata, the exposed version, and pyproject must all agree.
    assert prodius_mcp.__version__ == installed
    assert installed == _pyproject_version()
