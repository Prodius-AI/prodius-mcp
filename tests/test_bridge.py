"""The shim against a fake Voice Bridge: what it sends, and what it returns.

The JSON-RPC tests in test_jsonrpc.py point at an unreachable port, so they
only ever see "Cannot connect". These run the real shim process against
tests/_fake_bridge.py and check the HTTP side too.
"""

import json
import os
import subprocess
import sys
from unittest import mock

import pytest

import prodius_mcp

from ._fake_bridge import FakeBridge

SHIM = [sys.executable, "-m", "prodius_mcp"]


@pytest.fixture
def bridge():
    b = FakeBridge()
    yield b
    b.close()


def _env(bridge) -> dict:
    return {
        **os.environ,
        "PRODIUS_MCP_TOKEN": "test-token",
        "PRODIUS_BASE_URL": bridge.url,
        "PRODIUS_AGENT_NAME": "Test Agent",
    }


def _rpc(method, msg_id=None, params=None) -> str:
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _run(bridge, lines, timeout=15.0) -> list[dict]:
    result = subprocess.run(
        SHIM, input="\n".join(lines) + "\n", capture_output=True, text=True,
        timeout=timeout, env=_env(bridge),
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _call(name, arguments, msg_id=2) -> str:
    return _rpc("tools/call", msg_id, {"name": name, "arguments": arguments})


class TestHappyPath:
    def test_notify_forwards_identity_and_succeeds(self, bridge):
        bridge.respond("/mcp/notify", {"status": "shown"})
        responses = _run(bridge, [
            _rpc("initialize", 1),
            _call("notify", {"message": "Build passed", "level": "success", "task_name": "CI"}),
        ])
        result = next(r for r in responses if r.get("id") == 2)["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"]) == {"status": "shown"}

        (call,) = bridge.calls("/mcp/notify")
        assert call["headers"]["Authorization"] == "Bearer test-token"
        body = call["body"]
        assert body["message"] == "Build passed"
        assert body["level"] == "success"
        assert body["task_name"] == "CI"
        assert body["agent_name"] == "Test Agent"
        assert isinstance(body["shim_pid"], int) and body["shim_pid"] > 0
        assert isinstance(body["source_pid"], int) and body["source_pid"] > 0
        assert body["shim_pid"] != body["source_pid"]

    def test_ask_user_returns_the_answer(self, bridge):
        bridge.respond("/mcp/ask", {"answer": "ship it", "selected_option": 1})
        responses = _run(bridge, [
            _rpc("initialize", 1),
            _call("ask_user", {"question": "Deploy?", "options": ["Yes", "No"],
                               "detail": "**3 files**", "timeout_s": 30}),
        ])
        result = next(r for r in responses if r.get("id") == 2)["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["answer"] == "ship it"

        body = bridge.calls("/mcp/ask")[0]["body"]
        assert body["question"] == "Deploy?"
        assert body["options"] == ["Yes", "No"]
        assert body["detail"] == "**3 files**"
        assert body["timeout_s"] == 30
        assert body["client_request_id"] == 2
        assert isinstance(body["shim_pid"], int)

    def test_desktop_error_is_an_error_result(self, bridge):
        bridge.respond("/mcp/notify", {"error": "suppressed"}, status=409)
        responses = _run(bridge, [_call("notify", {"message": "x"})])
        result = responses[0]["result"]
        assert result["isError"] is True
        assert json.loads(result["content"][0]["text"])["error"] == "suppressed"

    def test_http_error_without_error_key_is_still_an_error(self, bridge):
        bridge.respond("/mcp/notify", {"message": "service unavailable"}, status=503)
        responses = _run(bridge, [_call("notify", {"message": "x"})])
        result = responses[0]["result"]
        assert result["isError"] is True
        assert "HTTP 503" in json.loads(result["content"][0]["text"])["error"]


class TestCancellation:
    def test_cancel_suppresses_response_and_cancels_desktop_session(self, bridge):
        bridge.hold("/mcp/ask")
        proc = subprocess.Popen(
            SHIM, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_env(bridge),
        )
        try:
            proc.stdin.write(_call("ask_user", {"question": "Still there?"}, msg_id=42) + "\n")
            proc.stdin.flush()
            bridge.wait_for("/mcp/ask")
            # Hold the cancel call too: the stdin loop must not wait for it.
            bridge.hold("/mcp/cancel-by-request")
            proc.stdin.write(_rpc("notifications/cancelled", params={"requestId": 42}) + "\n")
            proc.stdin.write(_rpc("ping", 7) + "\n")
            proc.stdin.flush()
            (cancel,) = bridge.wait_for("/mcp/cancel-by-request")
            assert cancel["body"] == {"client_request_id": 42}
            assert json.loads(proc.stdout.readline()) == {"jsonrpc": "2.0", "id": 7, "result": {}}
            bridge.release("/mcp/cancel-by-request")
            bridge.release("/mcp/ask")
            out, _ = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
        ids = [json.loads(line).get("id") for line in out.splitlines() if line.strip()]
        assert 42 not in ids


class TestHttpRequest:
    def test_recovers_from_a_regenerated_token(self, bridge):
        bridge.require_token = "new-token"
        bridge.respond("/mcp/notify", {"status": "shown"})
        with mock.patch("prodius_mcp.find_token", return_value="new-token"):
            result = prodius_mcp._http_request("POST", "/mcp/notify", "old-token", {}, bridge.url, timeout=5)
        assert result == {"status": "shown"}
        auth = [c["headers"]["Authorization"] for c in bridge.calls("/mcp/notify")]
        assert auth == ["Bearer old-token", "Bearer new-token"]

    def test_recovers_from_new_port_and_new_token_together(self, bridge):
        """Desktop app restarted: the old port refuses, the new one wants a new token."""
        bridge.require_token = "new-token"
        bridge.respond("/mcp/notify", {"status": "shown"})
        with mock.patch("prodius_mcp.find_base_url", return_value=bridge.url), \
             mock.patch("prodius_mcp.find_token", return_value="new-token"):
            result = prodius_mcp._http_request(
                "POST", "/mcp/notify", "old-token", {}, "http://127.0.0.1:1", timeout=5)
        assert result == {"status": "shown"}

    def test_truncated_response_is_an_error_not_an_exception(self):
        from http.client import IncompleteRead
        with mock.patch("prodius_mcp.urlopen", side_effect=IncompleteRead(b"{")):
            result = prodius_mcp._http_request("POST", "/x", "t", {}, "http://127.0.0.1:1", timeout=5)
        assert "IncompleteRead" in result["error"]

    def test_no_retry_when_token_unchanged(self, bridge):
        bridge.respond("/mcp/notify", {"error": "Invalid or missing auth token"}, status=401)
        with mock.patch("prodius_mcp.find_token", return_value="same"):
            result = prodius_mcp._http_request("POST", "/mcp/notify", "same", {}, bridge.url, timeout=5)
        assert len(bridge.calls("/mcp/notify")) == 1
        assert "error" in result

    def test_non_object_json_is_an_error(self, bridge):
        bridge.respond("/mcp/notify", [1, 2])
        result = prodius_mcp._http_request("POST", "/mcp/notify", "t", {}, bridge.url, timeout=5)
        assert "error" in result

    def test_connection_refused_mentions_the_desktop_app_once(self):
        with mock.patch("prodius_mcp.find_base_url", return_value="http://127.0.0.1:1"):
            result = prodius_mcp._http_request("POST", "/x", "t", {}, "http://127.0.0.1:1", timeout=5)
        assert result["error"].count("Is the Prodius desktop app running?") == 1
