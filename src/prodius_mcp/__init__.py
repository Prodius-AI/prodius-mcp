"""Prodius MCP Server — raw JSON-RPC 2.0 over stdio.

Bridges the MCP protocol to the Prodius desktop app without the ``mcp``
Python SDK.  Each line on stdin is one JSON-RPC 2.0 message (NDJSON);
responses are written to stdout the same way.

Why raw JSON-RPC instead of the SDK?
  1. Removes the heavy ``mcp`` dependency (pydantic, httpx, starlette…)
  2. Allows Claude Code channel notifications, which the SDK cannot send
  3. Faster startup — stdlib only

Usage:
    Configure in your MCP client (e.g. Claude Code .claude/settings.json):

    {
        "mcpServers": {
            "prodius": {
                "command": "prodius-mcp",
                "args": []
            }
        }
    }
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.client import HTTPException
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from prodius_mcp._config import find_token, find_base_url, DEFAULT_BASE_URL

# Single source of truth for the version is pyproject.toml. When installed
# (the normal case — run as the ``prodius-mcp`` console script) read it from
# package metadata. When run from a source checkout without an install (e.g.
# the test suite, which puts ``src/`` on sys.path via conftest) fall back to a
# dev sentinel. This keeps the version defined in exactly one place so it can't
# drift against the value broadcast in the MCP ``initialize`` handshake below.
try:
    __version__ = _pkg_version("prodius-mcp")
except PackageNotFoundError:  # pragma: no cover - source/editable runs
    __version__ = "0.0.0+dev"


PROTOCOL_VERSION = "2025-03-26"


# ---------------------------------------------------------------------------
# Channel capability detection
# ---------------------------------------------------------------------------

# Walking deeper than the immediate non-passthrough ancestor risks false
# positives: a developer running prodius-mcp from inside a Claude Code session
# that ITSELF has --channels enabled would inherit that flag transitively. We
# only ever want the direct invoker (the MCP client), so the "stop at the
# first non-passthrough ancestor" rule (in `_detect_channels_enabled`) is the
# real protection. The depth here is just an upper bound on how many
# passthrough hops we tolerate before giving up — pip's console-script
# launcher is one, and uv/conda/venv shims occasionally add another, so a
# small cushion is cheap insurance against silent False results.
_ANCESTOR_SCAN_DEPTH = 6


def _detect_channels_enabled() -> bool:
    """Detect if the MCP client launched the shim with channel support.

    The shim's direct parent is the pip-generated console-script launcher
    (``prodius-mcp.exe`` on Windows). We skip that one passthrough hop and
    check the FIRST real ancestor's command line for the channels flag —
    that's the MCP client (Claude Code, Cursor, etc.) that spawned us.
    Stopping at the first non-passthrough is critical: continuing further
    up the tree finds the developer's outer shell / Claude session and
    produces false positives.

    Returns False when detection fails (conservative default).
    """
    try:
        for args in _ancestor_command_lines(_ANCESTOR_SCAN_DEPTH):
            if _looks_like_passthrough_launcher(args):
                continue
            return _has_channels_flag(args)
    except Exception:
        pass
    return False


_PASSTHROUGH_LAUNCHER_BASENAMES = frozenset({
    "prodius-mcp",
    "prodius-mcp-reset",
    "prodius-permission-hook",
})


def _cmdline_executable_basename(args: str) -> str:
    """Return the lowercased basename of the executable portion of a
    Windows/POSIX-style command line, with any ``.exe`` stripped.

    The executable is the first token: everything between the opening
    ``"`` and the matching closing ``"`` if the cmdline is quoted, or
    everything up to the first whitespace otherwise. Path separators
    (both ``/`` and ``\\``) are recognised so the same parser handles
    both platforms.

    Returns the empty string if no executable can be extracted.
    """
    s = args.lstrip()
    if not s:
        return ""
    if s.startswith('"'):
        end = s.find('"', 1)
        exe = s[1:end] if end > 0 else s[1:]
    else:
        ws = s.find(' ')
        exe = s if ws < 0 else s[:ws]
    exe = exe.replace('\\', '/').rsplit('/', 1)[-1]
    if exe.lower().endswith('.exe'):
        exe = exe[:-4]
    return exe.lower()


def _looks_like_passthrough_launcher(args: str) -> bool:
    """True if ``args`` is the pip console-script launcher that sits between
    Claude Code and the Python shim on Windows (and contributes no useful
    argv of its own). Empty command lines are also treated as passthroughs —
    Win32 has a small number of kernel-side processes whose CommandLine is
    unreadable, and there's nothing useful we can extract from them.

    Matches on the EXECUTABLE BASENAME, not a free-text substring:
    ``Win32_Process.CommandLine`` for a launcher found via PATH is the bare
    name (``prodius-mcp``, no extension or path), while a substring match
    on ``prodius-mcp`` would also hit any process that merely has the
    package directory in an argument (a test harness, dev scripts).

    POSIX has no separate launcher process — pip generates ``#!`` shebang
    scripts and the kernel ``exec``s straight into Python — so the shim's
    ``os.getppid()`` jumps directly to the MCP client. The basename check
    still works there and stays as defensive cover for future wrappers
    (uv, pipx run, conda activate shims) that might insert an extra hop.
    """
    if not args:
        return True
    return _cmdline_executable_basename(args) in _PASSTHROUGH_LAUNCHER_BASENAMES


def _has_channels_flag(args: str) -> bool:
    return (
        "load-development-channels" in args
        or "--channels " in args
        or args.endswith("--channels")
    )


def _ancestor_command_lines(max_depth: int) -> list[str]:
    """Return command lines for this process's parent, grandparent, etc.

    Walks up at most ``max_depth`` levels. Stops at PID 1 (init) or 0/4
    (Windows System Idle / System) — neither carries useful argv. On Windows,
    issues a single PowerShell call that traverses the chain in-process; one
    fork is cheap, N forks for N levels would balloon startup latency.
    """
    if sys.platform == "win32":
        return _windows_ancestor_command_lines(os.getppid(), max_depth)
    return _posix_ancestor_command_lines(os.getppid(), max_depth)


def _windows_ancestor_command_lines(start_pid: int, max_depth: int) -> list[str]:
    """One PowerShell call walks up to ``max_depth`` ancestors via CIM.

    Each level's command line is printed on its own line so a missing/empty
    command line (some kernel-side processes have one) shows up as a blank
    we can skip without misaligning the depth count.
    """
    # $PID is PowerShell's read-only automatic variable (its own process),
    # so the loop variable must have a different name.
    script = (
        f"$tpid = {int(start_pid)}; "
        f"for ($i = 0; $i -lt {int(max_depth)}; $i++) {{"
        "  if ($tpid -le 4) { break } "
        "  $p = Get-CimInstance Win32_Process -Filter \"ProcessId=$tpid\" -ErrorAction SilentlyContinue; "
        "  if (-not $p) { break } "
        "  Write-Output ('CMD:' + $p.CommandLine); "
        "  $tpid = [int]$p.ParentProcessId"
        "}"
    )
    try:
        raw = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
        )
    except Exception:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        if line.startswith("CMD:"):
            out.append(line[4:].strip())
    return out


def _posix_ancestor_command_lines(start_pid: int, max_depth: int) -> list[str]:
    """N successive ``ps`` calls walk up the POSIX process tree. Fast enough
    (each call ~5–15 ms) that we don't optimise into a single shell pipeline.
    """
    out: list[str] = []
    pid = start_pid
    for _ in range(max_depth):
        if pid <= 1:
            break
        try:
            row = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "ppid=,args="],
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            ).strip()
        except Exception:
            break
        # Two columns: PPID, then everything else is the command line. ``ps``
        # left-pads ppid, so split on the first whitespace run.
        parts = row.split(None, 1)
        if len(parts) < 2:
            break
        try:
            next_pid = int(parts[0])
        except ValueError:
            break
        out.append(parts[1])
        pid = next_pid
    return out


# Set once by main() before any worker thread starts. Not computed at import
# time: the hook entry points import this package too, and walking the process
# tree on every hook invocation would cost a PowerShell or ps call each time.
_channels_enabled: bool = False


# ---------------------------------------------------------------------------
# HTTP bridge to Prodius desktop app
# ---------------------------------------------------------------------------

def _http_request(method: str, path: str, token: str,
                  body: Optional[dict] = None,
                  base_url: str = DEFAULT_BASE_URL,
                  timeout: float = 120.0) -> dict:
    """Make an HTTP request to the Prodius local endpoint.

    Retries once when the first attempt fails in a way a restarted desktop
    app explains: connection refused (it may be on a different port now) or
    401 (it may have generated a new token). Both are re-read from
    settings.json, so the shim never needs a restart.

    Always returns a dict; every failure carries an ``"error"`` key.
    """
    headers = {"Content-Type": "application/json"} if body is not None else {}
    data = json.dumps(body).encode("utf-8") if body is not None else None

    retried_port = retried_token = False
    while True:
        headers["Authorization"] = f"Bearer {token}"
        req = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if not isinstance(result, dict):
                return {"error": "Unexpected response from Prodius", "response": result}
            return result
        except HTTPError as e:
            if e.code == 401 and not retried_token:
                retried_token = True
                fresh_token = find_token()
                if fresh_token and fresh_token != token:
                    _log("token rejected, retrying with the token from settings")
                    token = fresh_token
                    continue
            try:
                error_body = json.loads(e.read().decode("utf-8"))
            except (OSError, ValueError):
                error_body = None
            if isinstance(error_body, dict) and "error" in error_body:
                return error_body
            return {"error": f"HTTP {e.code}: {e.reason}", "response": error_body}
        except URLError as e:
            if not retried_port:
                retried_port = True
                # Port may have changed — re-read from settings and retry
                fresh_url = find_base_url()
                if fresh_url != base_url:
                    _log(f"Connection refused on {base_url}, retrying on {fresh_url}")
                    base_url = fresh_url
                    continue
            return {"error": f"Cannot connect to Prodius: {e.reason} -- Is the Prodius desktop app running?"}
        except (OSError, ValueError, HTTPException) as e:
            # Includes timeouts and truncated responses (IncompleteRead).
            return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Source window capture (for click-to-focus)
# ---------------------------------------------------------------------------

def _capture_source_window() -> dict:
    """Capture the active window handle at startup for click-to-focus.

    Snapshots the foreground window with the platform-native API. Called
    once at shim startup, when the terminal that launched the agent is
    normally the active window, so the overlay can bring it back to front.

    Returns dict with 'window_handle' (int) and 'platform' (str), or
    empty dict on failure.
    """
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to unix id of first process whose frontmost is true'],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=2.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                return {"window_handle": int(result.stdout.strip()), "platform": "macos"}
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                return {"window_handle": int(hwnd), "platform": "windows"}
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["xdotool", "getactivewindow"],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=1.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                return {"window_handle": int(result.stdout.strip()), "platform": "linux"}
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 output
#
# Tool-call threads, the channel poller and the main loop all write to
# stdout. Every message goes through one SimpleQueue drained by a single
# writer thread, so lines never interleave and no thread ever waits on a
# lock with a timeout: on Python 3.14 we saw timed lock waits hang the
# channel poller, and this design has no such waits at all.
# ---------------------------------------------------------------------------

_stdout_q: queue.SimpleQueue = queue.SimpleQueue()


def _stdout_writer() -> None:
    """Drain _stdout_q and write to stdout.  Runs as a daemon thread."""
    while True:
        line = _stdout_q.get()  # blocks until a message arrives
        if line is None:
            break  # poison pill for clean shutdown
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass  # stdout pipe broken — nothing we can do


# Started on first use by _ensure_writer().
_writer_thread: Optional[threading.Thread] = None
_writer_lock = threading.Lock()


def _ensure_writer() -> None:
    """Start the stdout writer thread exactly once."""
    global _writer_thread
    if _writer_thread is not None:
        return
    with _writer_lock:
        if _writer_thread is None:
            t = threading.Thread(target=_stdout_writer, daemon=True, name="StdoutWriter")
            t.start()
            _writer_thread = t


def _log(msg: str) -> None:
    """Log to stderr (never stdout — stdout is the MCP protocol channel)."""
    print(f"prodius-mcp: {msg}", file=sys.stderr, flush=True)


# MCP clients keep server stderr, so the user's spoken words are only logged
# when PRODIUS_MCP_DEBUG is set; otherwise just their length.
_DEBUG = os.environ.get("PRODIUS_MCP_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _preview(text: str) -> str:
    """Loggable stand-in for user content."""
    return repr(text[:60]) if _DEBUG else f"<{len(text)} chars>"


def _write_msg(msg: dict) -> None:
    """Queue one JSON-RPC message for stdout (safe from any thread)."""
    _ensure_writer()
    _stdout_q.put(json.dumps(msg, separators=(",", ":")) + "\n")


def _respond(msg_id, result: dict) -> None:
    """Send a JSON-RPC success response."""
    _write_msg({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _respond_error(msg_id, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    _write_msg({"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}})


# ---------------------------------------------------------------------------
# Tool-call response builder
# ---------------------------------------------------------------------------


def _redact_attachments(result: dict) -> dict:
    """Return a shallow copy of `result` with `attachments[*].data`
    stripped — those base64 blobs ride in their own `image` content
    blocks below, so leaving them inside the JSON-stringified text
    block too would double the payload and clutter Claude Code's
    transcript view (a 700 KB unreadable string with no usable signal).

    Selection metadata (`kind`, length-trimmed `text`) survives in
    the text dump; only screenshot binary payloads are removed.
    """
    if not isinstance(result, dict):
        return result
    attachments = result.get("attachments")
    if not attachments:
        return result
    redacted = []
    for att in attachments:
        if isinstance(att, dict) and "data" in att:
            redacted.append({k: v for k, v in att.items() if k != "data"})
        else:
            redacted.append(att)
    out = dict(result)
    out["attachments"] = redacted
    return out


def _build_tool_response(result: dict) -> dict:
    """Convert the desktop-side `/mcp/ask` JSON into MCP `content` blocks.

    Layout:
      * Block 0: `text` block carrying the redacted JSON dump (full
        result minus binary screenshot payloads).
      * Blocks 1..N: one `image` block per `attachments[*]` entry of
        `kind=="screenshot"` — base64 in `data`, MIME in `mimeType`
        (camelCase per MCP spec, mapped from the desktop-side
        snake_case `mime_type`). Defaults to `image/jpeg` when the
        upstream payload omits the type.

      All non-screenshot attachments (e.g. `selection`) are
      represented only in the redacted text block — they do not have
      a binary form and thus no separate content block.
    """
    content_blocks = [
        {"type": "text", "text": json.dumps(_redact_attachments(result), indent=2)},
    ]
    for att in result.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        if att.get("kind") != "screenshot":
            continue
        data = att.get("data")
        if not data:
            continue
        content_blocks.append({
            "type": "image",
            "data": data,
            "mimeType": att.get("mime_type") or "image/jpeg",
        })
    return {
        "content": content_blocks,
        "isError": "error" in result,
    }


# ---------------------------------------------------------------------------
# MCP tool definitions (plain dicts — no SDK types)
# ---------------------------------------------------------------------------

INSTRUCTIONS = (
    "Prodius shows a desktop popup and captures the user's spoken reply, so "
    "an agent can reach the user without them switching to the terminal.\n\n"
    "RULES:\n"
    "1. SUB-AGENTS / DELEGATED TASKS: Do NOT call any tools from this "
    "server. Return your results to the parent agent. Only the top-level "
    "agent communicates with the user through Prodius.\n"
    "2. HOW OFTEN to reach the user is governed by the user's own "
    "instructions (e.g. a 'Prodius' section in CLAUDE.md / AGENTS.md / "
    ".cursorrules), NOT by this server. Follow those if present. If the "
    "user has given no such instruction, reach out only when you actually "
    "need them \u2014 a permission/approval or a decision-blocking question "
    "\u2014 rather than at the end of every response.\n"
    "3. ask_user poses a question and blocks until the user answers (by "
    "voice via a desktop overlay); notify is fire-and-forget.\n"
    "4. CHANNEL MESSAGES: Voice messages from the user may arrive as "
    "<channel source=\"prodius-voice\"> tags. Treat them as if the user "
    "typed the message directly and respond naturally."
)

TOOLS = [
    {
        "name": "ask_user",
        "description": (
            "Ask the user a question via voice overlay. "
            "Sub-agents must NOT call this — return results to your "
            "parent agent. Plain text, one sentence, ~80 chars "
            "(100 max)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "The question to ask (~80 chars, 100 max). Plain text, one sentence.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 4,
                    "description": (
                        "Optional button labels (max 4). Rendered as clickable "
                        "buttons in 360px overlay. Total chars across all labels: "
                        "2 buttons max 44, 3 max 41, 4 max 38. "
                        "Keep labels short. User can always answer by voice instead."
                    ),
                },
                "detail": {
                    "type": "string",
                    "description": "Optional Markdown content. User can expand overlay to read before answering.",
                },
                "task_name": {
                    "type": "string",
                    "description": (
                        "Label for this session (e.g., 'Fix auth bug'). Shown in modal header. "
                        "Always set this on your first call \u2014 helps the user distinguish "
                        "concurrent sessions. Set once, reuse on subsequent calls."
                    ),
                },
                "timeout_s": {
                    "type": "integer",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 600,
                    "description": "Seconds before timeout. Default 120 (2 minutes). Max 600.",
                },
            },
            "required": ["question"],
        },
        "annotations": {
            "title": "Ask User by Voice",
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "notify",
        "description": (
            "Show the user a short message in a desktop overlay, without "
            "waiting for a reply. Use it as often as the user's own "
            "instructions say; with no such instructions, only for results "
            "they need to see while away from the terminal. Sub-agents "
            "must NOT call this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "One-sentence summary shown in desktop popup (aim for ~80 chars, 100 max).",
                },
                "detail": {
                    "type": "string",
                    "description": (
                        "Optional Markdown content. User can expand overlay to see full details "
                        "(files changed, decisions, context)."
                    ),
                },
                "level": {
                    "type": "string",
                    "enum": ["info", "success", "error"],
                    "default": "info",
                },
                "task_name": {
                    "type": "string",
                    "description": "Short label for this session. Same as ask_user's task_name.",
                },
            },
            "required": ["message"],
        },
        "annotations": {
            "title": "Notify User",
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    },
]


# ---------------------------------------------------------------------------
# Tool call handler
# ---------------------------------------------------------------------------

# In-flight tool calls, keyed by msg_id.  Used by notifications/cancelled
# to suppress responses for requests the client no longer cares about.
_inflight: dict = {}
_inflight_lock = threading.Lock()


def _handle_tool_call(msg_id, name: str, arguments: dict, *,
                      token: str, base_url: str,
                      agent_name: str, source_window: dict,
                      source_pid: int) -> None:
    """Process a tool call and write the JSON-RPC response.

    Runs in a thread so the main stdin loop stays responsive during
    long-poll calls (ask_user blocks up to 300 s).
    """
    try:
        # Unknown tool → JSON-RPC protocol error (not a tool-level isError)
        if name not in ("ask_user", "notify"):
            _respond_error(msg_id, -32602, f"Unknown tool: {name}")
            return

        # Lazy token re-read: if token was missing at startup but Prodius
        # was configured later, pick it up now.
        effective_token = token or find_token()
        if not effective_token:
            result = {
                "error": "No Prodius MCP token found. "
                         "Enable MCP in Prodius settings or set PRODIUS_MCP_TOKEN."
            }
        elif name == "ask_user":
            body = {
                "question": arguments.get("question", ""),
                "agent_name": agent_name,
                "source_pid": source_pid,
                "shim_pid": os.getpid(),
                "channel_capable": _channels_enabled,
                "client_request_id": msg_id,
            }
            if "options" in arguments:
                body["options"] = arguments["options"]
            if arguments.get("detail"):
                body["detail"] = arguments["detail"]
            if arguments.get("task_name"):
                body["task_name"] = arguments["task_name"]
            if arguments.get("timeout_s"):
                body["timeout_s"] = arguments["timeout_s"]
            if source_window:
                body["source_window"] = source_window
            # The desktop app holds this request open until the user answers
            # or its own long-poll deadline passes; wait a bit longer than it.
            result = _http_request("POST", "/mcp/ask", effective_token, body,
                                   base_url, timeout=300.0)
        else:  # notify
            body = {
                "message": arguments.get("message", ""),
                "level": arguments.get("level", "info"),
                "agent_name": agent_name,
                "source_pid": source_pid,
                "shim_pid": os.getpid(),
                "channel_capable": _channels_enabled,
            }
            if source_window:
                body["source_window"] = source_window
            if arguments.get("detail"):
                body["detail"] = arguments["detail"]
            if arguments.get("task_name"):
                body["task_name"] = arguments["task_name"]
            result = _http_request("POST", "/mcp/notify", effective_token,
                                   body, base_url)

        # If the request was cancelled while we were blocking, don't send
        # a response — the client already moved on.  However, if the user
        # actually spoke a reply, re-route it through the Channel so it
        # still reaches Claude instead of being silently lost.
        with _inflight_lock:
            was_cancelled = msg_id not in _inflight
            if not was_cancelled:
                del _inflight[msg_id]

        _log(f"tool result for {msg_id}: cancelled={was_cancelled} "
             f"has_answer={'answer' in result} name={name} "
             f"result_keys={list(result.keys())}")

        if was_cancelled:
            answer = result.get("answer")
            if answer and name == "ask_user":
                _recover_cancelled_answer(
                    token, base_url, agent_name, source_pid,
                    answer, result.get("attachments") or [],
                )
            else:
                _log(f"response suppressed for cancelled request {msg_id}")
            return

        _respond(msg_id, _build_tool_response(result))
    except Exception as e:
        _log(f"tool call error: {e}")
        with _inflight_lock:
            _inflight.pop(msg_id, None)
        _respond_error(msg_id, -32603, str(e))


def _channel_notification(content: str, meta: dict, attachments: list) -> dict:
    """Build a ``notifications/claude/channel`` message.

    Claude Code requires ``params.content`` to be a string and every
    ``meta`` value to be a string; anything else makes it drop the stdio
    connection. So screenshots can't travel as image blocks here. Instead
    the on-disk path goes into the text, and a local agent can open it with
    its (multimodal) Read tool. Agents without access to this machine's
    filesystem see only the path.
    See https://code.claude.com/docs/en/channels-reference
    """
    paths = [
        att["path"] for att in attachments
        if isinstance(att, dict) and att.get("kind") == "screenshot"
        and isinstance(att.get("path"), str) and att["path"]
    ]
    params_meta = {
        str(k): v if isinstance(v, str) else json.dumps(v)
        for k, v in (meta or {}).items()
    }
    if paths:
        path_lines = "\n".join(f"[Screenshot at: {p}]" for p in paths)
        hint = ("(Use the Read tool on the path above to view what the "
                "user was looking at when they spoke.)")
        content = f"{content}\n\n{path_lines}\n{hint}" if content else f"{path_lines}\n{hint}"
        params_meta["screenshot_paths"] = ",".join(paths)
    return {
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content, "meta": params_meta},
    }


def _recover_cancelled_answer(token: str, base_url: str, agent_name: str,
                              source_pid: int, answer: str,
                              attachments: list) -> None:
    """Deliver an answer the user gave after the client cancelled ask_user.

    The client no longer waits for the tool result, so the answer can only
    reach the agent as a channel message, which needs channels enabled.
    It is enqueued on the desktop app's outbound queue, where this process's
    channel poller picks it up like any other voice message. Only if that
    fails is it written to stdout directly. If the enqueue went through but
    its reply was lost, the agent gets the answer twice; that beats losing
    what the user said.
    """
    if not _channels_enabled:
        _log("answer arrived after cancellation, but channels are off; "
             "it cannot be delivered")
        return
    meta = {"type": "voice", "recovered": "true"}
    effective_token = token or find_token()
    if effective_token:
        body = {
            "agent_name": agent_name,
            "source_pid": source_pid,
            "content": answer,
            "meta": meta,
        }
        if attachments:
            body["attachments"] = attachments
        result = _http_request("POST", "/mcp/enqueue-channel", effective_token,
                               body, base_url, timeout=5.0)
        if "error" not in result and result.get("enqueued", True):
            _log(f"late answer queued for the channel poller: {_preview(answer)}")
            return
        _log(f"could not queue late answer ({result}); writing it directly")
    _write_msg(_channel_notification(answer, meta, attachments))


def _cancel_session_by_request(token: str, base_url: str,
                               client_request_id) -> None:
    """Best-effort POST to /mcp/cancel-by-request to cancel the server session.

    Called when notifications/cancelled arrives from Claude Code.  Cancelling
    the session server-side ensures it becomes terminal, so the next mic click
    on a pinned modal uses the outbound push path (reliable) instead of the
    shim recovery path (which Claude Code can drop).
    """
    effective_token = token or find_token()
    if not effective_token:
        return
    try:
        body = {"client_request_id": client_request_id}
        result = _http_request("POST", "/mcp/cancel-by-request",
                               effective_token, body, base_url, timeout=5.0)
        status = result.get("status", "")
        if status == "cancelled":
            _log(f"server session cancelled for request {client_request_id}")
        elif status == "not_found":
            _log(f"no pending session for request {client_request_id} (already terminal)")
        elif "error" in result:
            _log(f"cancel-by-request rejected: {result}")
    except Exception as e:
        _log(f"cancel-by-request failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Channel poller — push user voice messages to Claude via Channels
# ---------------------------------------------------------------------------

_shutdown_flag = False  # simple boolean, checked by poller


def _sleep_unless_shutdown(seconds: float) -> bool:
    """Sleep in small increments, returning True if shutdown was signaled.

    Deliberately not threading.Event.wait(timeout): that is a timed lock
    wait, the kind we saw hang on Python 3.14 (see the stdout writer).
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _shutdown_flag:
            return True
        time.sleep(min(0.5, max(deadline - time.monotonic(), 0.01)))
    return _shutdown_flag


def _channel_poller(token: str, base_url: str, agent_name: str,
                    source_pid: int) -> None:
    """Background thread: long-polls /mcp/outbound and emits Channel notifications.

    When the user speaks through a pinned modal (unprompted), Prodius enqueues
    the transcribed message.  This thread picks it up and writes a
    ``notifications/claude/channel`` JSON-RPC notification to stdout so
    Claude Code receives it as a Channel message.

    Backoff: 2s → 4s → 8s → ... → 30s cap on connection errors.  Resets on
    success or empty poll (server was reachable).
    """
    backoff = 0.0
    max_backoff = 30.0
    poll_timeout = 30  # seconds per long-poll

    _log("channel poller started")

    try:
        while not _shutdown_flag:
            if backoff > 0:
                if _sleep_unless_shutdown(backoff):
                    break

            effective_token = token or find_token()
            if not effective_token:
                # No token yet — wait and retry
                _sleep_unless_shutdown(5.0)
                continue

            try:
                _poll_start = time.monotonic()
                url = (f"{base_url}/mcp/outbound"
                       f"?agent_name={quote(agent_name, safe='')}"
                       f"&source_pid={source_pid}"
                       f"&timeout={poll_timeout}")
                req = Request(url, method="GET")
                req.add_header("Authorization", f"Bearer {effective_token}")
                with urlopen(req, timeout=poll_timeout + 10) as resp:
                    # Respect Retry-After header (server sends it when no
                    # correspondent exists yet).
                    retry_after = resp.headers.get("Retry-After")
                    data = json.loads(resp.read().decode())

                messages = data.get("messages") if isinstance(data, dict) else None
                if not isinstance(messages, list):
                    messages = []
                backoff = 0.0  # Server reachable — reset backoff

                # Forward first: a queued message ends the long-poll
                # immediately, and it must not wait for the pauses below.
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    content = content if isinstance(content, str) else ""
                    attachments = msg.get("attachments")
                    attachments = attachments if isinstance(attachments, list) else []
                    meta = msg.get("meta")
                    _write_msg(_channel_notification(
                        content, meta if isinstance(meta, dict) else {}, attachments))
                    _log(f"channel push: {_preview(content)} (attachments={len(attachments)})")

                # Honour Retry-After (sent while no correspondent exists yet).
                if retry_after:
                    try:
                        wait = min(float(retry_after), 30.0)
                    except ValueError:
                        wait = 5.0
                    _sleep_unless_shutdown(wait)
                elif time.monotonic() - _poll_start < 1.0:
                    # Never re-poll more than once a second.
                    _sleep_unless_shutdown(1.0)

            except Exception as e:  # never let one bad poll end voice push
                # Desktop app restarted? Pick up its new port and token.
                base_url = find_base_url()
                if isinstance(e, HTTPError) and e.code == 401:
                    token = find_token() or token
                backoff = min(backoff * 2, max_backoff) if backoff else 2.0
                _log(f"channel poller error ({backoff:.0f}s backoff): {e}")
    except Exception as e:
        # Should not happen; log it so a silently missing poller is diagnosable.
        _log(f"channel poller crashed ({type(e).__name__}): {e}")
    finally:
        _log("channel poller stopped")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    """Entry point: raw JSON-RPC 2.0 message loop over stdio."""
    # Force UTF-8 on the stdio streams.  When Claude Code spawns this bridge,
    # stdin/stdout are pipes, and on Windows Python's TextIOWrapper defaults to
    # the locale codepage (e.g. cp1252) for pipes — so incoming UTF-8 JSON-RPC
    # (em dash E2 80 94 -> "â€"") gets mis-decoded before json.loads. Pin UTF-8
    # so the protocol is read/written correctly regardless of system locale.
    for _stream in (sys.stdin, sys.stdout):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    global _channels_enabled
    _channels_enabled = _detect_channels_enabled()
    state = _ServerState(
        token=find_token(),
        base_url=find_base_url(),
        agent_name=os.environ.get("PRODIUS_AGENT_NAME", "Unknown Agent"),
        source_window=_capture_source_window(),
        source_pid=os.getppid(),
    )

    _log(f"v{__version__} starting: agent={state.agent_name} "
         f"base_url={state.base_url} token={'set' if state.token else 'MISSING'} "
         f"channels={_channels_enabled}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError) as e:  # incl. absurd nesting or number size
            _respond_error(None, -32700, f"Parse error: {type(e).__name__}")
            continue
        try:
            _dispatch(msg, state)
        except Exception as e:  # one bad message must not end the session
            _log(f"error handling message: {type(e).__name__}: {e}")
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            if _valid_id(msg_id) and msg_id is not None:
                _respond_error(msg_id, -32603, f"Internal error: {e}")

    # stdin closed — signal the channel poller to stop
    global _shutdown_flag
    _shutdown_flag = True

    # Give pending tool calls a brief grace period.
    # Threads are daemon, so the process exits when main() returns.
    # The 2 s deadline lets fast calls (notify) finish cleanly.
    deadline = time.monotonic() + 2.0
    for t in state.pending:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)

    # Drain the stdout writer queue before exiting.
    if _writer_thread is not None:
        _stdout_q.put(None)  # poison pill
        _writer_thread.join(timeout=2.0)


class _ServerState:
    """Per-process values the dispatcher needs; set once in main()."""

    def __init__(self, *, token, base_url, agent_name, source_window, source_pid):
        self.token = token
        self.base_url = base_url
        self.agent_name = agent_name
        self.source_window = source_window
        self.source_pid = source_pid
        self.pending: list[threading.Thread] = []
        self.poller_started = False


def _valid_id(msg_id) -> bool:
    """JSON-RPC ids are strings, integers or null. bool is an int subclass."""
    return msg_id is None or isinstance(msg_id, str) or (
        isinstance(msg_id, int) and not isinstance(msg_id, bool))


def _dispatch(msg, state: _ServerState) -> None:
    """Handle one decoded JSON-RPC message."""
    if not isinstance(msg, dict):
        _respond_error(None, -32600, "Invalid Request: expected JSON object")
        return

    method = msg.get("method")
    msg_id = msg.get("id")
    is_request = msg_id is not None  # notifications carry no id
    if method is None and ("result" in msg or "error" in msg):
        return  # a response; this server sends no requests, so nothing awaits it
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str) or not _valid_id(msg_id):
        if is_request:
            _respond_error(msg_id if _valid_id(msg_id) else None, -32600,
                           "Invalid Request: expected jsonrpc \"2.0\", a string method "
                           "and a string, integer or null id")
        return
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        if is_request:
            _respond_error(msg_id, -32602, "Invalid params: expected object")
        return

    if method == "initialize":
        _log(f"initialize received (msg_id={msg_id}), channels_enabled={_channels_enabled}")
        if is_request:
            # Always declare the channel capability. Whether Claude Code
            # listens depends on its own --channels flag (_channels_enabled).
            _respond(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                    "experimental": {"claude/channel": {}},
                },
                "serverInfo": {"name": "prodius-voice", "version": __version__},
                "instructions": INSTRUCTIONS,
            })

    elif method == "notifications/initialized":
        # Without channels the client ignores channel notifications, and
        # polling would only tell the desktop app we can receive them.
        if _channels_enabled and not state.poller_started:
            state.poller_started = True
            threading.Thread(
                target=_channel_poller,
                args=(state.token, state.base_url, state.agent_name, state.source_pid),
                daemon=True,
                name="ChannelPoller",
            ).start()
        elif not _channels_enabled:
            _log("channel poller skipped (channels not enabled in Claude Code)")

    elif method == "tools/list":
        if is_request:
            _respond(msg_id, {"tools": TOOLS})

    elif method == "tools/call":
        if not is_request:
            return
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            _respond_error(msg_id, -32602,
                           "Invalid params: expected a string name and object arguments")
            return
        with _inflight_lock:
            _inflight[msg_id] = True
        t = threading.Thread(
            target=_handle_tool_call,
            args=(msg_id, name, arguments),
            kwargs=dict(token=state.token, base_url=state.base_url,
                        agent_name=state.agent_name, source_window=state.source_window,
                        source_pid=state.source_pid),
            daemon=True,
        )
        t.start()
        state.pending = [p for p in state.pending if p.is_alive()] + [t]

    elif method == "notifications/cancelled":
        cancelled_id = params.get("requestId")
        if cancelled_id is None or not _valid_id(cancelled_id):
            return
        with _inflight_lock:
            was_inflight = _inflight.pop(cancelled_id, None) is not None
        if was_inflight:
            _log(f"cancelled request {cancelled_id}")
            # Make the desktop session terminal too, so a later answer on a
            # pinned modal takes the outbound push path. Off this thread:
            # it is an HTTP call and the stdin loop must stay responsive.
            threading.Thread(
                target=_cancel_session_by_request,
                args=(state.token, state.base_url, cancelled_id),
                daemon=True,
            ).start()

    elif method == "ping":
        if is_request:
            _respond(msg_id, {})

    elif is_request:
        _respond_error(msg_id, -32601, f"Method not found: {method}")
    # else: unknown notification — ignore silently


if __name__ == "__main__":
    main()
