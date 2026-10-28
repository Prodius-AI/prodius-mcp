"""Channel notifications: their shape, and how a late answer is delivered."""

from unittest import mock

import pytest

import prodius_mcp
from prodius_mcp import _channel_notification, _recover_cancelled_answer


class TestChannelNotification:
    def test_plain_message(self):
        msg = _channel_notification("hello", {"type": "voice"}, [])
        assert msg == {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {"content": "hello", "meta": {"type": "voice"}},
        }

    def test_meta_values_are_always_strings(self):
        meta = _channel_notification("x", {"recovered": True, "n": 3, "s": "a"}, [])["params"]["meta"]
        assert meta == {"recovered": "true", "n": "3", "s": "a"}

    def test_screenshot_path_goes_into_content_and_meta(self):
        attachments = [
            {"kind": "screenshot", "path": "/tmp/a.jpg", "data": "base64..."},
            {"kind": "selection", "text": "foo"},
            {"kind": "screenshot"},  # no path: skipped
            "junk",
        ]
        params = _channel_notification("look", {}, attachments)["params"]
        assert params["content"].startswith("look\n\n[Screenshot at: /tmp/a.jpg]\n")
        assert "Read tool" in params["content"]
        assert params["meta"] == {"screenshot_paths": "/tmp/a.jpg"}
        assert "base64" not in params["content"]

    def test_screenshot_without_text(self):
        params = _channel_notification("", {}, [{"kind": "screenshot", "path": "/p.jpg"}])["params"]
        assert params["content"].startswith("[Screenshot at: /p.jpg]")


class TestRecoverCancelledAnswer:
    @pytest.fixture
    def sent(self, monkeypatch):
        written = []
        monkeypatch.setattr(prodius_mcp, "_write_msg", written.append)
        return written

    def _recover(self):
        _recover_cancelled_answer("tok", "http://x", "Agent", 123, "yes please", [])

    def test_dropped_when_channels_are_off(self, monkeypatch, sent):
        monkeypatch.setattr(prodius_mcp, "_channels_enabled", False)
        with mock.patch("prodius_mcp._http_request") as http:
            self._recover()
        http.assert_not_called()
        assert sent == []

    def test_queued_once_and_not_also_written(self, monkeypatch, sent):
        monkeypatch.setattr(prodius_mcp, "_channels_enabled", True)
        with mock.patch("prodius_mcp._http_request", return_value={"status": "ok", "enqueued": True}) as http:
            self._recover()
        http.assert_called_once()
        path, body = http.call_args.args[1], http.call_args.args[3]
        assert path == "/mcp/enqueue-channel"
        assert body["content"] == "yes please"
        assert body["meta"] == {"type": "voice", "recovered": "true"}
        assert sent == []

    @pytest.mark.parametrize("reply", [{"error": "No correspondent found"}, {"enqueued": False}])
    def test_written_directly_when_queueing_fails(self, monkeypatch, sent, reply):
        monkeypatch.setattr(prodius_mcp, "_channels_enabled", True)
        with mock.patch("prodius_mcp._http_request", return_value=reply):
            self._recover()
        (msg,) = sent
        assert msg["params"]["content"] == "yes please"
        assert msg["params"]["meta"]["recovered"] == "true"
