"""Tests for prodius_mcp.reset — parent-process detection and when to reset."""

import json
import os
import subprocess
import sys
from unittest import mock

import pytest

from prodius_mcp.reset import _detect_claude_code_pid, _read_proc_info

from ._fake_bridge import FakeBridge

RESET = [sys.executable, "-m", "prodius_mcp.reset"]


def _ps_result(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestReadProcInfoDarwin:
    """macOS uses `ps -o ppid=,comm= -p <pid>` — no /proc on darwin."""

    def test_parses_ppid_and_basename(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch(
                "prodius_mcp.reset.subprocess.run",
                return_value=_ps_result(" 12345 /usr/local/bin/node\n"),
            ):
                ppid, comm = _read_proc_info(99999)
                assert ppid == 12345
                assert comm == "node"

    def test_handles_claude_cli_path(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch(
                "prodius_mcp.reset.subprocess.run",
                return_value=_ps_result("100 /opt/homebrew/bin/claude\n"),
            ):
                ppid, comm = _read_proc_info(200)
                assert ppid == 100
                assert comm == "claude"

    def test_returns_none_on_empty_output(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch(
                "prodius_mcp.reset.subprocess.run",
                return_value=_ps_result("\n"),
            ):
                assert _read_proc_info(1) == (None, None)

    def test_returns_none_on_nonzero_exit(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch(
                "prodius_mcp.reset.subprocess.run",
                return_value=_ps_result("", returncode=1),
            ):
                assert _read_proc_info(99999) == (None, None)

    def test_returns_none_on_subprocess_error(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch(
                "prodius_mcp.reset.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=2.0),
            ):
                assert _read_proc_info(1) == (None, None)

    def test_returns_none_on_malformed_ppid(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch(
                "prodius_mcp.reset.subprocess.run",
                return_value=_ps_result("notanumber /usr/local/bin/node\n"),
            ):
                assert _read_proc_info(1) == (None, None)


class TestDetectClaudeCodePidDarwin:
    """The walk-up loop should find 'node' within the first few ancestors."""

    def test_finds_node_one_level_up(self):
        # getppid -> sh -> node(claude code)
        proc_tree = {
            1000: (500, "sh"),   # our parent is sh
            500: (1, "node"),    # sh's parent is node
        }

        def fake_read(pid):
            return proc_tree.get(pid, (None, None))

        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch("prodius_mcp.reset.os.getppid", return_value=1000):
                with mock.patch("prodius_mcp.reset._read_proc_info", side_effect=fake_read):
                    assert _detect_claude_code_pid() == 500

    def test_finds_node_as_direct_parent(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch("prodius_mcp.reset.os.getppid", return_value=777):
                with mock.patch(
                    "prodius_mcp.reset._read_proc_info",
                    side_effect=lambda pid: (1, "node") if pid == 777 else (None, None),
                ):
                    assert _detect_claude_code_pid() == 777

    def test_returns_none_if_no_node_in_tree(self):
        proc_tree = {
            1000: (500, "sh"),
            500: (200, "bash"),
            200: (1, "launchd"),
        }
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch("prodius_mcp.reset.os.getppid", return_value=1000):
                with mock.patch(
                    "prodius_mcp.reset._read_proc_info",
                    side_effect=lambda pid: proc_tree.get(pid, (None, None)),
                ):
                    assert _detect_claude_code_pid() is None

    def test_stops_at_depth_limit(self):
        """Loop caps at 5 ancestors — deeper node ancestors aren't found."""
        proc_tree = {i: (i - 1, "sh") for i in range(10, 1, -1)}
        proc_tree[1] = (None, "node")  # depth 9 — too deep

        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch("prodius_mcp.reset.os.getppid", return_value=10):
                with mock.patch(
                    "prodius_mcp.reset._read_proc_info",
                    side_effect=lambda pid: proc_tree.get(pid, (None, None)),
                ):
                    assert _detect_claude_code_pid() is None

    def test_handles_ppid_loop_gracefully(self):
        """If a process reports itself as its own parent, don't infinite loop."""
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch("prodius_mcp.reset.os.getppid", return_value=42):
                with mock.patch(
                    "prodius_mcp.reset._read_proc_info",
                    side_effect=lambda pid: (42, "sh"),
                ):
                    assert _detect_claude_code_pid() is None

    def test_unsupported_platform_returns_none(self):
        with mock.patch.object(sys, "platform", "win32"):
            assert _detect_claude_code_pid() is None
            assert _read_proc_info(1) == (None, None)


class TestSessionSource:
    """SessionStart fires on resume/compact/fork too; only new conversations reset."""

    @pytest.fixture
    def bridge(self):
        b = FakeBridge()
        yield b
        b.close()

    def _run(self, bridge, stdin: str) -> None:
        env = {**os.environ, "PRODIUS_MCP_TOKEN": "t", "PRODIUS_BASE_URL": bridge.url,
               "PRODIUS_AGENT_NAME": "Claude Code"}
        proc = subprocess.run(RESET, input=stdin, capture_output=True, text=True,
                              timeout=30, env=env)
        assert proc.returncode == 0

    @pytest.mark.parametrize("source", ["startup", "clear"])
    def test_new_conversation_resets(self, bridge, source):
        self._run(bridge, json.dumps({"hook_event_name": "SessionStart", "source": source}))
        (call,) = bridge.calls("/mcp/reset-conversation")
        assert call["body"]["agent_name"] == "Claude Code"

    @pytest.mark.parametrize("source", ["resume", "compact", "fork"])
    def test_continued_conversation_is_kept(self, bridge, source):
        self._run(bridge, json.dumps({"hook_event_name": "SessionStart", "source": source}))
        assert bridge.calls("/mcp/reset-conversation") == []

    def test_no_hook_input_still_resets(self, bridge):
        self._run(bridge, "")
        assert len(bridge.calls("/mcp/reset-conversation")) == 1
