"""prodius-mcp-reset — resets conversation state on /clear or startup.

Called by a Claude Code SessionStart hook. Reads the MCP token from
environment or settings.json and POSTs to /mcp/reset-conversation.
SessionStart also fires on resume, compaction and fork, where the
conversation continues; those are skipped, so the hook can be registered
with any matcher.

Uses only stdlib (urllib) — no dependency on the mcp package.

Exit codes:
    0 — always. A SessionStart hook must never block Claude Code, so every
        failure mode (no token, connection refused, HTTP error, JSON parse
        failure) is logged to stderr and swallowed. If you need to debug,
        watch stderr — the Claude Code launcher captures it.
"""

import json
import os
import subprocess
import sys
from urllib.request import Request, urlopen
from urllib.error import URLError

from prodius_mcp._config import find_token, find_base_url


def _read_proc_info(pid):
    """Return (ppid, comm) for a pid, or (None, None) on failure.

    Linux: reads /proc/<pid>/comm and /proc/<pid>/status.
    macOS: shells out to `ps -o ppid=,comm= -p <pid>` (no /proc).
    """
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        return int(line.split()[1]), comm
        except (OSError, ValueError):
            pass
        return None, None

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode != 0:
                return None, None
            line = result.stdout.strip()
            if not line:
                return None, None
            parts = line.split(None, 1)
            if len(parts) != 2:
                return None, None
            ppid = int(parts[0])
            # ps returns the full executable path on macOS — take basename
            comm = os.path.basename(parts[1])
            return ppid, comm
        except (subprocess.SubprocessError, ValueError, OSError):
            return None, None

    return None, None


def _detect_claude_code_pid():
    """Try to find the Claude Code (Node.js) PID from our process tree.

    Hook execution: Claude Code -> /bin/sh -> prodius-mcp-reset
    The shim sends os.getppid() which is Claude Code's PID.
    We need the same PID so the reset targets the right instance.

    Strategy: walk up from our parent, looking for a 'node' or 'claude'
    process. Falls back to None if detection fails (broadcast reset).
    """
    if sys.platform not in ("linux", "darwin"):
        return None

    try:
        current = os.getppid()
        for _ in range(5):
            if current <= 1:
                break
            ppid, comm = _read_proc_info(current)
            if comm is None:
                break
            if comm.lower() in ("node", "claude"):
                return current
            if ppid is None or ppid == current:
                break
            current = ppid
    except Exception:
        pass
    return None


# SessionStart sources that begin a new conversation. resume, compact and
# fork continue an existing one, whose Prodius context must survive.
_RESET_SOURCES = ("startup", "clear")


def _session_source():
    """The SessionStart ``source`` from the hook input on stdin, if any."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        hook_input = json.loads(sys.stdin.read() or "null")
    except (OSError, ValueError):
        return None
    if isinstance(hook_input, dict) and isinstance(hook_input.get("source"), str):
        return hook_input["source"]
    return None


def main():
    source = _session_source()
    if source is not None and source not in _RESET_SOURCES:
        print(f"prodius-mcp-reset: session {source}, keeping the conversation", file=sys.stderr)
        return

    token = find_token()
    if not token:
        # No token configured — Prodius MCP not set up, exit silently
        print("prodius-mcp-reset: no token found, skipping", file=sys.stderr)
        sys.exit(0)

    agent_name = os.environ.get("PRODIUS_AGENT_NAME", "Unknown Agent")
    base_url = find_base_url()
    url = f"{base_url}/mcp/reset-conversation"

    payload = {"agent_name": agent_name}
    source_pid = _detect_claude_code_pid()
    if source_pid:
        payload["source_pid"] = source_pid

    print(
        f"prodius-mcp-reset: agent={agent_name!r} pid={source_pid} url={url}",
        file=sys.stderr,
    )

    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=5.0) as resp:
            data = resp.read()
            print(f"prodius-mcp-reset: OK — {data.decode()}", file=sys.stderr)
    except URLError as e:
        print(f"prodius-mcp-reset: connection failed — {e}", file=sys.stderr)
    except Exception as e:
        print(f"prodius-mcp-reset: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
