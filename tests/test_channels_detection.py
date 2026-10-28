"""Unit tests for the shim's channel-capability detection.

Includes a Windows regression case: ``Win32_Process.CommandLine`` for a
pip-generated launcher invoked via PATH is the bare basename
(``"prodius-mcp"``, not ``"prodius-mcp.exe"``). If that launcher is not
recognised as a passthrough, the walk stops there and never reaches the
real MCP client, so channels are reported off on every Windows session.

These tests exercise the helpers directly (no subprocess / no process
tree walking) by monkey-patching ``_ancestor_command_lines``. That keeps
the suite hermetic and runnable on every platform.
"""

from __future__ import annotations

import pytest

import prodius_mcp


# ---------------------------------------------------------------------------
# _looks_like_passthrough_launcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline",
    [
        # Bare launcher names — what Win32 reports when invoked via PATH.
        # This is the case that the old `.exe`-requiring markers missed.
        "prodius-mcp",
        "prodius-mcp-reset",
        "prodius-permission-hook",
        # Full-path / explicit-extension forms.
        r"C:\Users\dev\AppData\Local\Programs\Python\Python311\Scripts\prodius-mcp.exe",
        r"C:\Python\Scripts\prodius-mcp-reset.exe --foo",
        # Case-insensitive (CIM occasionally normalises path casing).
        "PRODIUS-MCP",
        r"C:\PYTHON\SCRIPTS\PRODIUS-MCP.EXE",
        # Empty cmdline — kernel-side processes whose CommandLine is
        # unreadable should be treated as passthrough so the walk continues.
        "",
    ],
)
def test_passthrough_launcher_matches(cmdline):
    assert prodius_mcp._looks_like_passthrough_launcher(cmdline) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        # Real Claude Code invocations — must NOT be classified as passthrough,
        # otherwise the loop walks past the process that carries the channels flag.
        r'"C:\Users\dev\.local\bin\claude.exe" --dangerously-skip-permissions',
        r'"C:\Users\dev\.local\bin\claude.exe" --dangerously-load-development-channels server:prodius',
        "/usr/local/bin/claude --channels server:prodius",
        # Unrelated processes that happen to be in the chain.
        "powershell.exe -NoProfile",
        "/bin/zsh",
        "explorer.exe",
        # "prodius" alone (without "-mcp") must not match — the flag
        # `--dangerously-load-development-channels server:prodius` contains
        # the substring "prodius" but is NOT one of our launchers.
        "claude --some-flag server:prodius",
        # Regression: a process whose cmdline merely CONTAINS "prodius-mcp"
        # somewhere in an argument path must NOT be treated as a passthrough.
        # The CI harness lives under prodius-mcp/ci/, and an early
        # version of the fix substring-matched the bare name and incorrectly
        # skipped past the harness's own Python — causing the walk to climb
        # past the process that carried the channels flag.
        r'"C:\Python311\python.exe" C:\Users\dev\src\prodius-mcp\ci\test_channels_harness.py --dangerously-load-development-channels server:prodius',
        # And: VS Code / Cursor / similar IDEs spawned from a project path
        # that happens to contain "prodius-mcp".
        r'"C:\Program Files\Cursor\Cursor.exe" C:\dev\prodius-mcp-workspace',
    ],
)
def test_non_passthrough_not_matched(cmdline):
    assert prodius_mcp._looks_like_passthrough_launcher(cmdline) is False


# ---------------------------------------------------------------------------
# _cmdline_executable_basename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline,expected",
    [
        # Bare names — Windows PATH-resolved launcher reporting.
        ("prodius-mcp", "prodius-mcp"),
        ("prodius-mcp --foo", "prodius-mcp"),
        # Unquoted paths.
        (r"C:\Python\Scripts\prodius-mcp.exe", "prodius-mcp"),
        (r"C:\Python\Scripts\prodius-mcp.exe --foo bar", "prodius-mcp"),
        # Quoted paths with spaces.
        (r'"C:\Program Files\Python\python.exe" -m prodius_mcp', "python"),
        (r'"C:\Users\dev\.local\bin\claude.exe" --dangerously-skip-permissions', "claude"),
        # POSIX paths.
        ("/usr/local/bin/claude --channels server:prodius", "claude"),
        ("/bin/zsh", "zsh"),
        # Mixed separators (rare but valid).
        (r"C:\foo/bar/prodius-mcp.exe", "prodius-mcp"),
        # Empty / whitespace-only.
        ("", ""),
        ("   ", ""),
        # Unbalanced quote — treat the rest as the exe path, still extract basename.
        (r'"C:\foo\bar\thing.exe', "thing"),
    ],
)
def test_cmdline_executable_basename(cmdline, expected):
    assert prodius_mcp._cmdline_executable_basename(cmdline) == expected


# ---------------------------------------------------------------------------
# _has_channels_flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline",
    [
        # Development-channels variants (current production form).
        "claude --dangerously-load-development-channels server:prodius",
        r'"C:\Users\dev\.local\bin\claude.exe" --dangerously-skip-permissions --dangerously-load-development-channels server:prodius',
        # Future "graduated" form — `--channels` with an argument.
        "claude --channels server:prodius",
        # `--channels` as the last token (no trailing space, no arg).
        "claude --channels",
    ],
)
def test_has_channels_flag_positive(cmdline):
    assert prodius_mcp._has_channels_flag(cmdline) is True


@pytest.mark.parametrize(
    "cmdline",
    [
        # No flag at all.
        "claude",
        r'"C:\Users\dev\.local\bin\claude.exe" --dangerously-skip-permissions',
        # The bare launcher cmdline carries no flag.
        "prodius-mcp",
        "",
        # Substrings that look similar but aren't the flag.
        "claude --channels-something-else server:prodius",  # `--channels-` not `--channels `
        "claude --no-channels",
    ],
)
def test_has_channels_flag_negative(cmdline):
    assert prodius_mcp._has_channels_flag(cmdline) is False


# ---------------------------------------------------------------------------
# _detect_channels_enabled — end-to-end via monkey-patched ancestor walk
# ---------------------------------------------------------------------------


def test_detect_windows_launcher_then_claude_with_channels(monkeypatch):
    """The exact Windows scenario the bug fix targets.

    Process chain from the shim:
      level 0: ``prodius-mcp.exe`` launcher → cmdline ``"prodius-mcp"``
      level 1: ``claude.exe ... --dangerously-load-development-channels ...``

    Before the fix: level 0 failed the passthrough match, channels flag
    check returned False on ``"prodius-mcp"``, loop exited → False.
    After the fix: level 0 is correctly recognised as passthrough, the
    walk reaches level 1, and the flag is detected.
    """
    monkeypatch.setattr(
        prodius_mcp,
        "_ancestor_command_lines",
        lambda max_depth: [
            "prodius-mcp",
            r'"C:\Users\dev\.local\bin\claude.exe" --dangerously-skip-permissions --dangerously-load-development-channels server:prodius',
        ],
    )
    assert prodius_mcp._detect_channels_enabled() is True


def test_detect_posix_claude_direct_with_channels(monkeypatch):
    """POSIX path: no launcher hop, claude is the immediate parent."""
    monkeypatch.setattr(
        prodius_mcp,
        "_ancestor_command_lines",
        lambda max_depth: [
            "/usr/local/bin/claude --channels server:prodius",
        ],
    )
    assert prodius_mcp._detect_channels_enabled() is True


def test_detect_claude_without_channels_returns_false(monkeypatch):
    """The walk reaches a non-passthrough without the flag → False."""
    monkeypatch.setattr(
        prodius_mcp,
        "_ancestor_command_lines",
        lambda max_depth: [
            "prodius-mcp",
            r'"C:\Users\dev\.local\bin\claude.exe" --dangerously-skip-permissions',
        ],
    )
    assert prodius_mcp._detect_channels_enabled() is False


def test_detect_only_passthroughs_returns_false(monkeypatch):
    """Defensive: if every level looks like a passthrough we give up
    rather than guessing. Conservative default keeps us off when the
    chain is unreadable (e.g. CIM call failed and returned empties)."""
    monkeypatch.setattr(
        prodius_mcp,
        "_ancestor_command_lines",
        lambda max_depth: ["prodius-mcp", "", ""],
    )
    assert prodius_mcp._detect_channels_enabled() is False


def test_detect_empty_chain_returns_false(monkeypatch):
    """Process-tree lookup returned nothing — be conservative."""
    monkeypatch.setattr(
        prodius_mcp,
        "_ancestor_command_lines",
        lambda max_depth: [],
    )
    assert prodius_mcp._detect_channels_enabled() is False


def test_detect_swallows_ancestor_walk_exceptions(monkeypatch):
    """If the underlying ps/CIM call raises, return False instead of
    leaking the exception to import time."""
    def _boom(max_depth):
        raise RuntimeError("CIM unavailable")

    monkeypatch.setattr(prodius_mcp, "_ancestor_command_lines", _boom)
    assert prodius_mcp._detect_channels_enabled() is False
