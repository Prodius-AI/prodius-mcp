"""Shared configuration helpers for prodius-mcp.

Reads the MCP auth token and server URL from environment variables or
the Prodius desktop app's ``settings.json``.  Stdlib only — no external
dependencies.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional


DEFAULT_PORT = 5555
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}"


def _is_wsl() -> bool:
    """Detect if running inside Windows Subsystem for Linux."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def _wsl_windows_localappdata() -> Optional[Path]:
    """Resolve Windows LOCALAPPDATA as a WSL mount path.

    Runs ``cmd.exe /C echo %LOCALAPPDATA%`` and converts the result
    with ``wslpath -u``.  Returns None on any failure.
    """
    try:
        raw = subprocess.run(
            ["cmd.exe", "/C", "echo", "%LOCALAPPDATA%"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5.0,
        )
        win_path = raw.stdout.strip()
        if not win_path or win_path == "%LOCALAPPDATA%":
            return None
        converted = subprocess.run(
            ["wslpath", "-u", win_path],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=3.0,
        )
        linux_path = converted.stdout.strip()
        return Path(linux_path) if linux_path else None
    except Exception:
        return None


def settings_path() -> Optional[Path]:
    """Return the platform-specific path to Prodius settings.json.

    Must match the path used by the desktop app's persistence module
    (``appdirs.user_config_dir("Prodius")``).  On Windows that resolves
    to ``LOCALAPPDATA/Prodius/Prodius`` (author=appname when unset).
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Prodius" / "settings.json"
    elif sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if not local_app_data:
            return None
        return Path(local_app_data) / "Prodius" / "Prodius" / "settings.json"
    else:
        # On WSL, read from the Windows settings path via /mnt/c mount
        if _is_wsl():
            local_app_data = _wsl_windows_localappdata()
            if local_app_data:
                return local_app_data / "Prodius" / "Prodius" / "settings.json"
        return Path.home() / ".config" / "Prodius" / "settings.json"


def read_settings() -> dict:
    """Read Prodius settings.json, returning {} on any failure."""
    path = settings_path()
    if path and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(data, dict):
            return data
    return {}


def find_token() -> Optional[str]:
    """Find the MCP auth token from environment or Prodius settings."""
    env_token = os.environ.get("PRODIUS_MCP_TOKEN")
    if env_token:
        return env_token
    token = read_settings().get("mcp_token")
    return token if isinstance(token, str) and token else None


def _wsl_host_ip() -> Optional[str]:
    """Return the Windows host IP reachable from WSL2.

    The default gateway on the WSL virtual NIC is the Windows host's IP
    on the Hyper-V virtual switch.
    """
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=3.0,
        )
        # Output: "default via 172.18.32.1 dev eth0"
        parts = result.stdout.strip().split()
        if len(parts) >= 3 and parts[0] == "default" and parts[1] == "via":
            return parts[2]
    except Exception:
        pass
    return None


def find_base_url() -> str:
    """Discover the Prodius Flask server URL from environment or settings."""
    env_url = os.environ.get("PRODIUS_BASE_URL")
    if env_url:
        return env_url
    port = read_settings().get("mcp_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        port = DEFAULT_PORT
    host = "127.0.0.1"
    # WSL2 in NAT mode reaches Windows through the default gateway. In
    # mirrored mode the gateway is the real LAN router and Windows is on
    # loopback, so the gateway must never be used there: the Bearer token
    # would go to the router. When the mode can't be read (WSL older than
    # 2.0, which also predates mirrored mode), use the gateway only if
    # nothing listens on loopback.
    if _is_wsl():
        mode = _wsl_networking_mode()
        if mode == "nat" or (mode is None and not _port_open(host, port)):
            wsl_ip = _wsl_host_ip()
            if wsl_ip:
                host = wsl_ip
    return f"http://{host}:{port}"


def _wsl_networking_mode() -> Optional[str]:
    """``"nat"``, ``"mirrored"`` etc. from ``wslinfo``, or None if unknown."""
    try:
        result = subprocess.run(
            ["wslinfo", "--networking-mode"],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    mode = result.stdout.strip().lower()
    return mode if result.returncode == 0 and mode else None


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
