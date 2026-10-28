# prodius-mcp

[![PyPI](https://img.shields.io/pypi/v/prodius-mcp)](https://pypi.org/project/prodius-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/prodius-mcp)](https://pypi.org/project/prodius-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Let your AI agents reach you by voice.**

`prodius-mcp` is a lightweight [MCP](https://modelcontextprotocol.io) server that bridges any MCP-capable AI tool (Claude Code, Cursor, Windsurf, Codex CLI, and more) to the [Prodius](https://prodius.ai) desktop app. When an agent needs your input, a popup appears on your screen — wherever you are — and you answer by voice. No switching back to the terminal.

It's a thin stdio shim: **standard library only, no third-party dependencies**, fast startup, and it talks only to the Prodius desktop app running locally on `127.0.0.1`.

> **This package needs the Prodius desktop app.** On its own it does nothing: the popup, the voice capture and the transcription all happen in the desktop app. Get it at [prodius.ai/download](https://prodius.ai/download/).

## Requirements

- Python 3.10+
- The [Prodius desktop app](https://prodius.ai/download/) installed and running, with **Voice Bridge** enabled in settings.

## Install

Published on PyPI as [`prodius-mcp`](https://pypi.org/project/prodius-mcp/):

```bash
pip install prodius-mcp
```

If `prodius-mcp` isn't found afterwards, make sure the Python environment you installed into is on your `PATH` — or use `pipx install prodius-mcp`.

## Connect your tool

### Claude Code

The quickest start is one command:

```bash
claude mcp add prodius -- prodius-mcp
```

For the full experience (correct agent label in the modal header, automatic conversation reset on `/clear`), add the block below to your `~/.claude/settings.json` instead. The `claude mcp add` shortcut omits both pieces.

```json
{
  "env": { "PRODIUS_AGENT_NAME": "Claude Code" },
  "mcpServers": {
    "prodius": {
      "command": "prodius-mcp",
      "args": [],
      "env": { "PRODIUS_AGENT_NAME": "Claude Code" }
    }
  },
  "hooks": {
    "SessionStart": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "prodius-mcp-reset" }] }
    ]
  }
}
```

The `SessionStart` hook calls `prodius-mcp-reset` whenever you run `/clear` or open a new session so Prodius doesn't carry stale context across conversations. It leaves the conversation alone when a session is resumed, compacted or forked. The top-level `env` makes the agent name visible to that hook.

To enable **voice push** (microphone stays available between questions so you can speak to Claude anytime, not just when it asks), start Claude with the channels flag:

```bash
claude --dangerously-load-development-channels server:prodius
```

### Cursor / Windsurf / any MCP client

Add to your client's MCP config (e.g. `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "prodius": { "command": "prodius-mcp" }
  }
}
```

### Codex CLI

```toml
[mcp_servers.prodius]
command = "prodius-mcp"
env = { PRODIUS_AGENT_NAME = "Codex CLI" }
```

Full setup instructions for each tool, including session-reset hooks and optional voice push, live at **[prodius.ai/connect](https://prodius.ai/connect/)**.

## Tools exposed

| Tool | What it does |
| --- | --- |
| `ask_user` | Pops a question on your screen and captures your spoken (or typed) reply. Supports optional clickable answer buttons (max 4), an expandable Markdown `detail` body, and a `task_name` label so you can tell concurrent sessions apart. Blocks until you answer or `timeout_s` expires (default 120 s, max 600). |
| `notify` | Shows you a short message in a desktop overlay, fire-and-forget. Takes `message`, optional Markdown `detail`, and a `level` of `info` / `success` / `error`. |

**How often the agent reaches out is your call, not the server's.** Without instructions it only contacts you when it is blocked (an approval or a decision). Tell it otherwise in your `CLAUDE.md` / `AGENTS.md`; presets are at [prodius.ai/connect](https://prodius.ai/connect/#teach-your-agent).

**Sub-agents must not call either tool.** They return their results to the parent agent; only the top-level agent talks to you through Prodius. The server advertises this in its instructions, but it is worth repeating in your own `CLAUDE.md`.

## How it works

```
your AI tool  ──stdio (JSON-RPC)──▶  prodius-mcp  ──HTTP──▶  Prodius desktop app
                                       (this pkg)              (overlay + voice)
```

`prodius-mcp` is a stdio shim and nothing more. It speaks MCP over stdin/stdout to your AI tool and forwards each call to the Prodius desktop app's local **Voice Bridge** — a set of `/mcp/*` HTTP routes on `127.0.0.1`, authenticated with a `Bearer` token that the desktop app generates on first run. The overlay rendering, the voice pipeline, and the routing of answers back to the right agent all live in the desktop app.

Consequences worth knowing:

- **Nothing leaves your machine via this package.** The shim only ever connects to `127.0.0.1` (or, under WSL in NAT networking mode, the Windows host). Speech transcription is performed by the desktop app, which routes it through Prodius' EU inference gateway — see [prodius.ai/privacy](https://prodius.ai/privacy/).
- **No third-party dependencies**, by design. Everything is Python standard library, so the shim installs in a second and adds no supply-chain surface to your agent's environment.
- **The bridge is plain HTTP**, so anything that can `curl` can reach you too — cron jobs, deploy scripts, CI. That endpoint is the desktop app's, not this package's; see [prodius.ai/connect](https://prodius.ai/connect/).

## Configuration

The shim auto-discovers the running Prodius app. You rarely need to set anything, but these environment variables override the defaults:

| Variable | Purpose | Default |
| --- | --- | --- |
| `PRODIUS_MCP_TOKEN` | Auth token for the local Prodius server. | Read from the Prodius app's `settings.json`. |
| `PRODIUS_BASE_URL` | Prodius local server URL. | Discovered from settings (`http://127.0.0.1:5555`). |
| `PRODIUS_AGENT_NAME` | Label shown so you know which agent is asking. | `Unknown Agent` |
| `PRODIUS_MCP_DEBUG` | Set to `1` to log the text of voice messages to stderr (otherwise only their length). | off |
| `PRODIUS_HOOK_DEBUG` | Set to `1` to let `prodius-permission-hook` log commands and answers, and write a log file. | off |

## Console scripts

- `prodius-mcp` — the MCP server (run by your AI tool, not by hand).
- `prodius-mcp-reset` — resets the Prodius conversation. Wire it to a Claude Code `SessionStart` hook (see the snippet above) so a new session doesn't inherit stale context.
- `prodius-permission-hook` — answers Claude Code's permission prompts by voice, so you can say "allow" / "deny" from anywhere. Add it as a `PermissionRequest` hook in `~/.claude/settings.json`:

  ```json
  "hooks": {
    "PermissionRequest": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "prodius-permission-hook" }] }
    ]
  }
  ```

  It only runs when Claude Code itself would ask you, after its own permission modes and allow/deny rules, and offers *Allow once*, *Always allow* and *Deny*. *Always allow* saves exactly the rule Claude Code's own prompt would offer. If Prodius isn't running or you don't answer, you get the normal terminal prompt, so it never blocks you.

  Set it up under `PreToolUse` before version 0.3? Move it to `PermissionRequest`. Under `PreToolUse` the hook now does nothing.

## Troubleshooting

- **Popup doesn't appear?** Confirm the Prodius desktop app is running with Voice Bridge enabled, and that `prodius-mcp` is on your `PATH`.
- **Agent doesn't receive your answer?** Check your microphone works in Prodius (try a normal dictation), then restart your AI tool after editing its config.
- **Two overlays for the same agent?** Only the first shim process registered for an agent may show overlays; later ones get a `409 suppressed` and stay quiet. If overlays stop appearing entirely, restart the desktop app to clear the registration.

## Development

```bash
git clone <repo-url> && cd prodius-mcp
pip install -e . pytest
python -m pytest tests/
```

The tests don't need the desktop app: they run the server as a subprocess and talk JSON-RPC to it over stdio.

The package is stdlib-only and must stay that way — `dependencies` in `pyproject.toml` is deliberately empty, and `tests/test_version.py` guards the single-sourced version (`pyproject.toml` is the one version string; `__init__.py` reads it back via `importlib.metadata`).

### Versioning and releases

Only `MAJOR.MINOR` in `pyproject.toml` is meaningful in git. At release time `ci/inject_pypi_version.sh` sets the patch number to the CI pipeline ID and adds a PEP 440 suffix per channel (`X.Y.N` stable, `X.Y.Nb1` beta, `X.Y.N.dev1` everything else), so every build has a unique version. `ci/local_validate.sh` rehearses the whole release (inject, version guard, tests, build, `twine check`, wheel smoke test) locally without uploading.

## License

MIT — see [LICENSE](LICENSE).

Note that this covers **the shim only**. The Prodius desktop app it connects to is closed-source and separately licensed; it is free to use on the Free tier. See [prodius.ai](https://prodius.ai).
