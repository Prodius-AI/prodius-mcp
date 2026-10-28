"""Tests for prodius_mcp._config — shared configuration helpers."""

import json
import os
import sys
from pathlib import Path
from unittest import mock

from prodius_mcp._config import (
    DEFAULT_BASE_URL,
    find_base_url,
    find_token,
    read_settings,
    settings_path,
)


class TestSettingsPath:
    """Test settings_path() across platforms.

    Assertions use as_posix() to normalise separators — these tests may
    run on Windows (via PowerShell from WSL) where Path uses backslashes.
    """

    def test_linux(self):
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch("prodius_mcp._config._is_wsl", return_value=False):
                p = settings_path()
                assert p is not None
                assert ".config/Prodius/settings.json" in p.as_posix()

    def test_macos(self):
        with mock.patch.object(sys, "platform", "darwin"):
            p = settings_path()
            assert "Application Support/Prodius/settings.json" in p.as_posix()

    def test_windows(self):
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"}):
                p = settings_path()
                assert p is not None
                assert "Prodius/Prodius/settings.json" in p.as_posix()

    def test_windows_no_localappdata(self):
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.dict(os.environ, {}, clear=True):
                p = settings_path()
                assert p is None

    def test_wsl_uses_windows_path(self):
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch("prodius_mcp._config._is_wsl", return_value=True):
                with mock.patch(
                    "prodius_mcp._config._wsl_windows_localappdata",
                    return_value=Path("/mnt/c/Users/test/AppData/Local"),
                ):
                    p = settings_path()
                    assert p is not None
                    posix = p.as_posix()
                    assert "/mnt/c/Users/test/AppData/Local/Prodius/Prodius/settings.json" in posix

    def test_wsl_falls_back_to_linux_path(self):
        """When WSL detection succeeds but LOCALAPPDATA resolution fails."""
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch("prodius_mcp._config._is_wsl", return_value=True):
                with mock.patch(
                    "prodius_mcp._config._wsl_windows_localappdata",
                    return_value=None,
                ):
                    p = settings_path()
                    assert p is not None
                    assert ".config/Prodius/settings.json" in p.as_posix()


class TestReadSettings:
    def test_reads_valid_json(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"mcp_token": "abc123"}))
        with mock.patch("prodius_mcp._config.settings_path", return_value=settings_file):
            result = read_settings()
            assert result == {"mcp_token": "abc123"}

    def test_returns_empty_on_missing_file(self, tmp_path):
        with mock.patch("prodius_mcp._config.settings_path", return_value=tmp_path / "nope.json"):
            assert read_settings() == {}

    def test_returns_empty_on_invalid_json(self, tmp_path):
        bad_file = tmp_path / "settings.json"
        bad_file.write_text("not json{{{")
        with mock.patch("prodius_mcp._config.settings_path", return_value=bad_file):
            assert read_settings() == {}

    def test_returns_empty_on_none_path(self):
        with mock.patch("prodius_mcp._config.settings_path", return_value=None):
            assert read_settings() == {}

    def test_returns_empty_when_json_is_not_an_object(self, tmp_path):
        for payload in ("[]", "null", "42"):
            settings_file = tmp_path / "settings.json"
            settings_file.write_text(payload)
            with mock.patch("prodius_mcp._config.settings_path", return_value=settings_file):
                assert read_settings() == {}


class TestFindToken:
    def test_env_var_takes_precedence(self):
        with mock.patch.dict(os.environ, {"PRODIUS_MCP_TOKEN": "env-token"}):
            with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_token": "file-token"}):
                assert find_token() == "env-token"

    def test_falls_back_to_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PRODIUS_MCP_TOKEN", None)
            with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_token": "file-token"}):
                assert find_token() == "file-token"

    def test_returns_none_when_both_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PRODIUS_MCP_TOKEN", None)
            with mock.patch("prodius_mcp._config.read_settings", return_value={}):
                assert find_token() is None

    def test_ignores_non_string_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_token": 123}):
                assert find_token() is None


class TestFindBaseUrl:
    def test_env_var_takes_precedence(self):
        with mock.patch.dict(os.environ, {"PRODIUS_BASE_URL": "http://custom:9999"}):
            assert find_base_url() == "http://custom:9999"

    def test_uses_mcp_port_from_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PRODIUS_BASE_URL", None)
            with mock.patch("prodius_mcp._config._is_wsl", return_value=False):
                with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_port": 7777}):
                    assert find_base_url() == "http://127.0.0.1:7777"

    def test_default_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PRODIUS_BASE_URL", None)
            with mock.patch("prodius_mcp._config._is_wsl", return_value=False):
                with mock.patch("prodius_mcp._config.read_settings", return_value={}):
                    assert find_base_url() == DEFAULT_BASE_URL

    def test_invalid_port_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("prodius_mcp._config._is_wsl", return_value=False):
                for port in ("5555", True, 0, 70000):
                    with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_port": port}):
                        assert find_base_url() == DEFAULT_BASE_URL

    def test_wsl_nat_mode_uses_host_ip(self):
        """NAT mode: Windows is behind the gateway, whatever listens on WSL's loopback."""
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PRODIUS_BASE_URL", None)
            with mock.patch("prodius_mcp._config._is_wsl", return_value=True), \
                 mock.patch("prodius_mcp._config._wsl_networking_mode", return_value="nat"), \
                 mock.patch("prodius_mcp._config._port_open", return_value=True):
                with mock.patch("prodius_mcp._config._wsl_host_ip", return_value="172.18.32.1"):
                    with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_port": 5555}):
                        assert find_base_url() == "http://172.18.32.1:5555"

    def test_wsl_mirrored_mode_never_uses_the_gateway(self):
        """Mirrored mode: the gateway is the LAN router; never send the token there,
        not even while Prodius is stopped and loopback has no listener."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("prodius_mcp._config._is_wsl", return_value=True), \
                 mock.patch("prodius_mcp._config._wsl_networking_mode", return_value="mirrored"), \
                 mock.patch("prodius_mcp._config._port_open", return_value=False), \
                 mock.patch("prodius_mcp._config._wsl_host_ip", return_value="192.168.1.1") as gw:
                with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_port": 5555}):
                    assert find_base_url() == "http://127.0.0.1:5555"
                    gw.assert_not_called()

    def test_wsl_unknown_mode_uses_loopback_when_something_listens(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("prodius_mcp._config._is_wsl", return_value=True), \
                 mock.patch("prodius_mcp._config._wsl_networking_mode", return_value=None), \
                 mock.patch("prodius_mcp._config._port_open", return_value=True), \
                 mock.patch("prodius_mcp._config._wsl_host_ip", return_value="172.18.32.1"):
                with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_port": 5555}):
                    assert find_base_url() == "http://127.0.0.1:5555"

    def test_wsl_unknown_mode_uses_gateway_when_loopback_is_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("prodius_mcp._config._is_wsl", return_value=True), \
                 mock.patch("prodius_mcp._config._wsl_networking_mode", return_value=None), \
                 mock.patch("prodius_mcp._config._port_open", return_value=False), \
                 mock.patch("prodius_mcp._config._wsl_host_ip", return_value="172.18.32.1"):
                with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_port": 5555}):
                    assert find_base_url() == "http://172.18.32.1:5555"

    def test_wsl_falls_back_to_localhost(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PRODIUS_BASE_URL", None)
            with mock.patch("prodius_mcp._config._is_wsl", return_value=True), \
                 mock.patch("prodius_mcp._config._wsl_networking_mode", return_value="nat"):
                with mock.patch("prodius_mcp._config._wsl_host_ip", return_value=None):
                    with mock.patch("prodius_mcp._config.read_settings", return_value={"mcp_port": 5555}):
                        assert find_base_url() == "http://127.0.0.1:5555"
