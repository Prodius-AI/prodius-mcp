"""prodius-permission-hook — answer Claude Code permission prompts by voice.

Register it as a Claude Code ``PermissionRequest`` hook. That event fires
only when Claude Code itself is about to ask for permission, after its own
modes and allow/deny/ask rules have been applied, so this hook never has to
re-implement any of that. It shows the request in the Prodius overlay and
maps the spoken answer to a decision:

  Allow once   — allow this call
  Always allow — allow, and apply the permission updates Claude Code itself
                 suggested for this request (the same rules its own
                 "don't ask again" option would save)
  Deny         — deny this call; Claude is told the user said no

Whenever Prodius can't give a clear answer (not running, no token, timeout,
dismissed, unclassified reply) the hook exits without output and Claude Code
shows its normal terminal prompt. It never blocks you.

Older setups registered this command as a ``PreToolUse`` hook. That event
fires for every tool call, before Claude Code's own rules run, so answering
there meant guessing at Claude Code's permission logic. The hook now ignores
every event except ``PermissionRequest``.

Uses only the standard library.
"""

import datetime
import json
import os
import sys
from http.client import HTTPException
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from prodius_mcp._config import find_base_url, find_token

# By default the hook logs only to stderr. Persistent file logging is opt-in
# with PRODIUS_HOOK_DEBUG=1: the hook sees every command it is asked about and
# the user's spoken answers, and that should not pile up on disk by default.
_DEBUG = os.environ.get("PRODIUS_HOOK_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
_REDACTED = "[redacted; set PRODIUS_HOOK_DEBUG=1 to log]"
_LOG_FILE: Path | None = None

ASK_TIMEOUT_S = 120
QUESTION_MAX = 100

OPT_ALLOW_ONCE = "Allow once"
OPT_ALWAYS = "Always allow"
OPT_DENY = "Deny"


def _get_log_file() -> Path | None:
    if not _DEBUG:
        return None
    global _LOG_FILE
    if _LOG_FILE is not None:
        return _LOG_FILE
    try:
        if sys.platform == "darwin":
            log_dir = Path.home() / "Library" / "Logs" / "Prodius"
        elif sys.platform == "win32":
            log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Prodius" / "logs"
        else:
            log_dir = Path.home() / ".config" / "Prodius" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = log_dir / "permission-hook.log"
        return _LOG_FILE
    except OSError:
        return None


def _redact(value: str) -> str:
    return value if _DEBUG else _REDACTED


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{ts}] prodius-permission-hook: {msg}"
    print(line, file=sys.stderr, flush=True)
    log_file = _get_log_file()
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ── Describing the request ───────────────────────────────────────────


def _shorten(text: str, limit: int) -> str:
    """Shorten to ``limit`` chars, keeping both ends.

    The tail of a shell command is where `&& curl … | sh` hides, so it must
    stay visible in the headline rather than only in the collapsed detail.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = (limit - 1) * 2 // 3
    tail = limit - 1 - head
    return f"{text[:head]}…{text[-tail:]}"


def describe_tool_call(tool_name: str, tool_input: dict, limit: int) -> str:
    """Short human-readable description of a tool call, at most ``limit`` chars."""
    if tool_name == "Bash":
        return "Run: " + _shorten(str(tool_input.get("command", "")), limit - 5)
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return _shorten(f"{tool_name}: {Path(path).name or 'unknown file'}", limit)
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        server = parts[1] if len(parts) > 1 else "?"
        method = parts[2] if len(parts) > 2 else "?"
        return _shorten(f"MCP {server}: {method}", limit)
    return _shorten(tool_name, limit)


_DESTINATIONS = {
    "session": "this session only",
    "localSettings": ".claude/settings.local.json",
    "projectSettings": ".claude/settings.json",
    "userSettings": "~/.claude/settings.json",
}


def describe_suggestion(entry: dict) -> str:
    """Human-readable form of one Claude Code permission-update entry."""
    kind = entry.get("type")
    where = _DESTINATIONS.get(entry.get("destination", ""), entry.get("destination", "?"))
    if kind in ("addRules", "replaceRules", "removeRules"):
        rules = []
        for rule in entry.get("rules") or []:
            name = rule.get("toolName", "?")
            content = rule.get("ruleContent")
            rules.append(f"{name}({content})" if content else name)
        verb = {"addRules": "add", "replaceRules": "replace with", "removeRules": "remove"}[kind]
        return f"{verb} {entry.get('behavior', '?')} rule {', '.join(rules)} ({where})"
    if kind == "setMode":
        return f"switch to {entry.get('mode', '?')} mode ({where})"
    if kind in ("addDirectories", "removeDirectories"):
        verb = "add" if kind == "addDirectories" else "remove"
        return f"{verb} directories {', '.join(entry.get('directories') or [])} ({where})"
    return json.dumps(entry)


def _suggestions(hook_input: dict) -> list:
    raw = hook_input.get("permission_suggestions")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def build_ask_body(hook_input: dict, agent_name: str) -> dict:
    """Build the /mcp/ask request for a PermissionRequest hook input."""
    tool_name = str(hook_input.get("tool_name", ""))
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    suggestions = _suggestions(hook_input)

    prefix = "Allow? "
    question = prefix + describe_tool_call(tool_name, tool_input, QUESTION_MAX - len(prefix))

    detail = [f"**Tool:** {tool_name}"]
    if tool_name == "Bash":
        detail += ["", "```", str(tool_input.get("command", "")), "```"]
    elif tool_input.get("file_path"):
        detail.append(f"**File:** `{tool_input['file_path']}`")
    if suggestions:
        detail += ["", "*Always allow* will:"]
        detail += [f"- {describe_suggestion(s)}" for s in suggestions]

    options = [OPT_ALLOW_ONCE, OPT_ALWAYS, OPT_DENY] if suggestions else [OPT_ALLOW_ONCE, OPT_DENY]
    return {
        "question": question,
        "options": options,
        "detail": "\n".join(detail),
        "agent_name": agent_name,
        "task_name": "Permission",
        "timeout_s": ASK_TIMEOUT_S,
        "source_pid": os.getppid(),
        "shim_pid": os.getpid(),
    }


# ── Mapping the answer ───────────────────────────────────────────────


def _output(decision: dict) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def decide(body: dict, hook_input: dict, result: object) -> dict | None:
    """Map a /mcp/ask result to hook output, or None to fall through.

    ``selected_option`` is the desktop pipeline's 1-based classification of
    the spoken answer against ``body["options"]``.
    """
    if not isinstance(result, dict) or "error" in result:
        return None
    selected = result.get("selected_option")
    options = body["options"]
    if isinstance(selected, bool) or not isinstance(selected, int) or not 1 <= selected <= len(options):
        return None
    choice = options[selected - 1]
    if choice == OPT_ALLOW_ONCE:
        return _output({"behavior": "allow"})
    if choice == OPT_ALWAYS:
        return _output({"behavior": "allow", "updatedPermissions": _suggestions(hook_input)})
    return _output({"behavior": "deny", "message": "The user denied this via Prodius."})


# ── Main ─────────────────────────────────────────────────────────────


def _ask(base_url: str, token: str, body: dict) -> object:
    req = Request(
        f"{base_url}/mcp/ask",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    # The desktop app answers within timeout_s; allow some slack on top.
    with urlopen(req, timeout=ASK_TIMEOUT_S + 10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read() or "null")
    except ValueError as e:
        _log(f"could not parse stdin: {e}")
        return
    if not isinstance(hook_input, dict):
        _log("no hook input; leaving the decision to Claude Code")
        return

    event = hook_input.get("hook_event_name")
    if event != "PermissionRequest":
        _log(f"ignoring {event!r} event; register prodius-permission-hook "
             "under hooks.PermissionRequest instead (see https://prodius.ai/connect/)")
        return

    token = find_token()
    if not token:
        _log("no Prodius token; leaving the decision to the terminal prompt")
        return

    body = build_ask_body(hook_input, os.environ.get("PRODIUS_AGENT_NAME", "Claude Code"))
    base_url = find_base_url()
    _log(f"asking via {base_url}: {_redact(repr(body['question']))}")
    try:
        result = _ask(base_url, token, body)
    except (URLError, OSError, ValueError, HTTPException) as e:
        _log(f"Prodius unavailable ({type(e).__name__}: {e}); leaving the decision to the terminal prompt")
        return

    output = decide(body, hook_input, result)
    if output is None:
        _log(f"no usable answer ({_redact(repr(result))}); leaving the decision to the terminal prompt")
        return
    _log(f"decision: {output['hookSpecificOutput']['decision']['behavior']}")
    print(json.dumps(output), flush=True)


if __name__ == "__main__":
    main()
