"""Tests for the raw JSON-RPC 2.0 protocol handling in prodius-mcp.

Uses subprocess to send NDJSON on stdin and verify stdout responses.
This tests the full protocol loop as Claude Code sees it.
"""

import json
import os
import subprocess
import sys
import threading

import prodius_mcp

# Path to the shim entry point
SHIM = [sys.executable, "-m", "prodius_mcp"]


_ENV = {
    "PRODIUS_MCP_TOKEN": "test-token-abc",
    "PRODIUS_BASE_URL": "http://127.0.0.1:1",  # unreachable
    "PRODIUS_AGENT_NAME": "Test Agent",
}


def _run_shim(messages: list[str], timeout: float = 20.0) -> list[dict]:
    """Send NDJSON messages to the shim, close stdin, return parsed responses.

    Closing stdin gives in-flight tool calls only a short grace period, so
    use _await_response() for calls that must reach the (unreachable) bridge.
    """
    stdin_data = "\n".join(messages) + "\n"
    result = subprocess.run(
        SHIM,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **_ENV},
    )
    responses = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


def _await_response(messages: list[str], want_id, timeout: float = 30.0) -> dict:
    """Send messages with stdin held open until the response to ``want_id``.

    A real client keeps stdin open while it waits. That matters on Windows,
    where even a refused loopback connection takes about two seconds.
    """
    proc = subprocess.Popen(SHIM, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env={**os.environ, **_ENV})
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        proc.stdin.write("\n".join(messages) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.strip():
                msg = json.loads(line)
                if msg.get("id") == want_id:
                    return msg
        raise AssertionError(f"no response with id {want_id}")
    finally:
        timer.cancel()
        proc.stdin.close()
        proc.wait(timeout=10)


def _msg(method: str, msg_id=None, params=None) -> str:
    """Build a JSON-RPC message string."""
    d = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        d["id"] = msg_id
    if params is not None:
        d["params"] = params
    return json.dumps(d)


class TestInitialize:
    def test_basic_handshake(self):
        responses = _run_shim([
            _msg("initialize", 1, {"protocolVersion": "2025-03-26"}),
        ])
        assert len(responses) == 1
        r = responses[0]
        assert r["id"] == 1
        assert r["result"]["protocolVersion"] == "2025-03-26"
        assert r["result"]["serverInfo"]["name"] == "prodius-voice"
        # Not a literal: the version is "0.0.0+dev" from a source checkout
        # and tracks pyproject.toml when installed (see test_version.py).
        assert r["result"]["serverInfo"]["version"] == prodius_mcp.__version__
        assert "tools" in r["result"]["capabilities"]
        assert "instructions" in r["result"]

    def test_channel_capability_declared(self):
        """Initialize response declares claude/channel experimental capability."""
        responses = _run_shim([
            _msg("initialize", 1, {"protocolVersion": "2025-03-26"}),
        ])
        caps = responses[0]["result"]["capabilities"]
        assert "experimental" in caps
        assert "claude/channel" in caps["experimental"]

    def test_version_mismatch_returns_server_version(self):
        responses = _run_shim([
            _msg("initialize", 1, {"protocolVersion": "9999-01-01"}),
        ])
        assert responses[0]["result"]["protocolVersion"] == "2025-03-26"

    def test_notification_form_gets_no_response(self):
        """initialize without id is a notification — no response."""
        responses = _run_shim([
            _msg("initialize", params={"protocolVersion": "2025-03-26"}),
            _msg("ping", 99),
        ])
        # Only ping should get a response
        assert len(responses) == 1
        assert responses[0]["id"] == 99


class TestToolsList:
    def test_lists_two_tools(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("tools/list", 2),
        ])
        tools_resp = responses[1]
        tools = tools_resp["result"]["tools"]
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert names == {"ask_user", "notify"}

    def test_tool_schemas_have_required_fields(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("tools/list", 2),
        ])
        for tool in responses[1]["result"]["tools"]:
            assert "inputSchema" in tool
            assert "required" in tool["inputSchema"]

    def test_tools_do_not_have_caller_role(self):
        """caller_role was removed — suppression is now server-side via shim_pid."""
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("tools/list", 2),
        ])
        for tool in responses[1]["result"]["tools"]:
            props = tool["inputSchema"]["properties"]
            assert "caller_role" not in props, f"{tool['name']} still has caller_role"
            assert "annotations" in tool

    def test_notification_form_ignored(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("tools/list"),  # no id
            _msg("ping", 99),
        ])
        ids = [r["id"] for r in responses]
        assert 99 in ids
        assert None not in ids  # no null-id response


class TestPing:
    def test_ping_returns_empty_result(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("ping", 2),
        ])
        assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}

    def test_ping_notification_ignored(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("ping"),  # no id
        ])
        assert len(responses) == 1  # only initialize


class TestUnknownMethod:
    def test_unknown_request_returns_error(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("unknown/method", 2),
        ])
        err = responses[1]
        assert err["id"] == 2
        assert err["error"]["code"] == -32601
        assert "Method not found" in err["error"]["message"]

    def test_unknown_notification_silently_ignored(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("unknown/notification"),  # no id
            _msg("ping", 99),
        ])
        ids = [r["id"] for r in responses]
        assert ids == [1, 99]


class TestMalformedInput:
    def test_invalid_json(self):
        responses = _run_shim(["not json at all", _msg("ping", 99)])
        # Should get a parse error for the bad line, then a ping response
        assert any(r.get("error", {}).get("code") == -32700 for r in responses)
        assert any(r.get("id") == 99 for r in responses)

    def test_json_array(self):
        responses = _run_shim(["[1, 2, 3]", _msg("ping", 99)])
        assert any(r.get("error", {}).get("code") == -32600 for r in responses)

    def test_json_string(self):
        responses = _run_shim(['"just a string"', _msg("ping", 99)])
        assert any(r.get("error", {}).get("code") == -32600 for r in responses)

    def test_json_number(self):
        responses = _run_shim(["42", _msg("ping", 99)])
        assert any(r.get("error", {}).get("code") == -32600 for r in responses)

    def test_empty_lines_ignored(self):
        responses = _run_shim(["", "  ", _msg("ping", 1)])
        assert len(responses) == 1
        assert responses[0]["id"] == 1


class TestToolCall:
    def test_unknown_tool_returns_protocol_error(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("tools/call", 2, {"name": "nonexistent", "arguments": {}}),
        ])
        err = responses[1]
        assert err["id"] == 2
        assert err["error"]["code"] == -32602
        assert "Unknown tool" in err["error"]["message"]

    def test_invalid_params_type(self):
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("tools/call", 2, "not-a-dict"),
        ])
        err = [r for r in responses if r.get("id") == 2][0]
        assert err["error"]["code"] == -32602
        assert "Invalid params" in err["error"]["message"]

    def test_notify_with_unreachable_server(self):
        """notify to unreachable server returns connection error in tool result."""
        tool_resp = _await_response([
            _msg("initialize", 1),
            _msg("tools/call", 2, {"name": "notify", "arguments": {"message": "hello"}}),
        ], want_id=2)
        result = tool_resp["result"]
        assert result["isError"] is True
        content_text = result["content"][0]["text"]
        error_data = json.loads(content_text)
        assert "Cannot connect" in error_data["error"]
        assert "Is the Prodius desktop app running?" in error_data["error"]

    def test_ask_user_with_unreachable_server(self):
        """ask_user to unreachable server returns connection error."""
        tool_resp = _await_response([
            _msg("initialize", 1),
            _msg("tools/call", 2, {
                "name": "ask_user",
                "arguments": {"question": "test?", "task_name": "Test"},
            }),
        ], want_id=2)
        content_text = tool_resp["result"]["content"][0]["text"]
        error_data = json.loads(content_text)
        assert "Cannot connect" in error_data["error"]

    def test_notification_form_toolcall_ignored(self):
        """tools/call without id should not produce a response."""
        responses = _run_shim([
            _msg("initialize", 1),
            _msg("tools/call", params={"name": "notify", "arguments": {"message": "x"}}),
            _msg("ping", 99),
        ])
        ids = [r["id"] for r in responses]
        assert None not in ids


class TestNoToken:
    def test_missing_token_returns_error(self):
        stdin_data = _msg("tools/call", 1, {
            "name": "notify", "arguments": {"message": "hi"},
        }) + "\n"
        env = {k: v for k, v in os.environ.items() if k != "PRODIUS_MCP_TOKEN"}
        env["PRODIUS_AGENT_NAME"] = "Test"
        # Force settings to not have a token either
        env["HOME"] = env["USERPROFILE"] = "/nonexistent"
        if "LOCALAPPDATA" in env:
            del env["LOCALAPPDATA"]
        result = subprocess.run(
            SHIM, input=stdin_data, capture_output=True, text=True,
            timeout=20.0, env=env,
        )
        responses = [json.loads(l) for l in result.stdout.strip().split("\n") if l.strip()]
        tool_resp = responses[0]
        content_text = tool_resp["result"]["content"][0]["text"]
        error_data = json.loads(content_text)
        assert "No Prodius MCP token" in error_data["error"]


class TestCancellation:
    def test_cancel_nonexistent_id_is_noop(self):
        """Cancelling an ID that was never started should not crash."""
        responses = _run_shim([
            _msg("initialize", 1),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                         "params": {"requestId": 999}}),
            _msg("ping", 2),
        ])
        assert len(responses) == 2
        assert responses[0]["id"] == 1
        assert responses[1]["id"] == 2


class TestMalformedMessages:
    """One bad message gets an error reply; the server keeps running."""

    def _then_ping(self, *bad: str) -> list[dict]:
        return _run_shim([*bad, _msg("ping", 99)])

    def test_unhashable_id(self):
        responses = self._then_ping(json.dumps(
            {"jsonrpc": "2.0", "id": [], "method": "tools/call",
             "params": {"name": "notify", "arguments": {"message": "x"}}}))
        assert responses[0]["error"]["code"] == -32600
        assert responses[0]["id"] is None
        assert responses[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}

    def test_object_id(self):
        responses = self._then_ping(json.dumps({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}))
        assert responses[0]["error"]["code"] == -32600
        assert responses[-1]["id"] == 99

    def test_missing_jsonrpc_version(self):
        responses = self._then_ping(json.dumps({"id": 5, "method": "ping"}))
        assert responses[0] == {"jsonrpc": "2.0", "id": 5,
                                "error": {"code": -32600, "message": responses[0]["error"]["message"]}}
        assert responses[-1]["id"] == 99

    def test_list_params_on_initialize(self):
        responses = self._then_ping(_msg("initialize", 1, [1]))
        assert responses[0]["id"] == 1
        assert responses[0]["error"]["code"] == -32602
        assert responses[-1]["id"] == 99

    def test_list_params_on_cancel_notification(self):
        responses = self._then_ping(_msg("notifications/cancelled", params=[1]))
        assert responses == [{"jsonrpc": "2.0", "id": 99, "result": {}}]

    def test_non_object_arguments(self):
        responses = self._then_ping(_msg("tools/call", 3, {"name": "notify", "arguments": "hi"}))
        assert responses[0]["id"] == 3
        assert responses[0]["error"]["code"] == -32602
        assert responses[-1]["id"] == 99

    def test_non_string_method(self):
        responses = self._then_ping(json.dumps({"jsonrpc": "2.0", "id": 4, "method": 12}))
        assert responses[0]["error"]["code"] == -32600
        assert responses[-1]["id"] == 99

    def test_absurdly_nested_json(self):
        responses = self._then_ping("[" * 100_000 + "]" * 100_000)
        # Older Pythons fail to parse it; newer ones parse it into a list.
        assert responses[0]["error"]["code"] in (-32700, -32600)
        assert responses[-1]["id"] == 99

    def test_huge_integer(self):
        responses = self._then_ping('{"jsonrpc": "2.0", "id": ' + "9" * 5000 + ', "method": "ping"}')
        assert responses[0]["error"]["code"] == -32700
        assert responses[-1]["id"] == 99

    def test_response_message_is_ignored(self):
        responses = self._then_ping(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))
        assert responses == [{"jsonrpc": "2.0", "id": 99, "result": {}}]
