"""Manual test harness for `_detect_channels_enabled()`.

How it works
------------
The shim inspects its PARENT process's command line. Whatever launches this
Python script becomes the shim's grandparent — but Python itself becomes the
shim's parent. So if YOU pass ``--dangerously-load-development-channels`` on
the python command line, that string ends up in the parent (python) process's
command line, and the shim should detect it.

Usage
-----
Install the package first (any of these works):
    pip install -e .          # editable install
    pip install dist/*.whl    # built wheel

Then run from PowerShell or any shell:

    # Should print `channels=True`:
    python ci/test_channels_harness.py \
        --dangerously-load-development-channels server:prodius

    # Control case — should print `channels=False`:
    python ci/test_channels_harness.py

    # The other supported form (channels graduated):
    python ci/test_channels_harness.py --channels server:prodius

This harness does NOT need Claude Code, the desktop app, or a Prodius token.
It feeds the shim empty stdin so the JSON-RPC loop exits immediately; we just
want to see the startup banner.

Exit code
---------
0 if the detection result matches expectation given the args you passed.
1 if there's a mismatch (the detection logic is broken or your CIM/ps call
  isn't returning the parent command line as expected).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _our_command_line() -> str:
    """Return this Python process's own command line via the same primitive
    the shim uses (CIM on Windows, ``ps`` elsewhere). Lets you see exactly
    what the shim will see when it inspects its parent."""
    pid = os.getpid()
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
                ],
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5.0,
            )
        else:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "args="],
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            )
        return out.strip()
    except Exception as exc:  # pragma: no cover - diagnostic only
        return f"<lookup failed: {exc}>"


def _resolve_shim() -> list[str]:
    """Find the shim entry point. Prefers the installed console script
    (matches how Claude Code actually launches it); falls back to
    ``python -m prodius_mcp`` for source-only environments."""
    exe = shutil.which("prodius-mcp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "prodius_mcp"]


def main() -> int:
    cmd_line = _our_command_line()
    print("-" * 72)
    print("Harness command line (what the shim's parent looks like):")
    print(f"  {cmd_line}")
    print()

    expected_channels = (
        "load-development-channels" in cmd_line
        or "--channels " in cmd_line
        or cmd_line.endswith("--channels")
    )
    print(f"Expected: channels={'True' if expected_channels else 'False'}")
    print()

    shim_cmd = _resolve_shim()
    print(f"Spawning shim: {' '.join(shim_cmd)}")
    print("-" * 72)

    # Feed empty stdin so the shim's NDJSON loop exits immediately after
    # printing its startup banner. Capture stderr only — the banner goes
    # there ("prodius-mcp: ... channels=True/False ...").
    result = subprocess.run(
        shim_cmd,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        timeout=15.0,
    )

    print("Shim stderr (filtered):")
    detected: bool | None = None
    for line in result.stderr.splitlines():
        if "channels=" in line or "channel poller" in line:
            print(f"  {line}")
        if "channels=True" in line:
            detected = True
        elif "channels=False" in line:
            detected = False

    print()
    if detected is None:
        print("RESULT: FAIL - shim did not print a startup banner; full stderr below")
        print(result.stderr)
        return 1

    print(f"Detected: channels={detected}")
    if detected == expected_channels:
        print(f"RESULT: PASS - detection matches expectation (channels={detected})")
        return 0
    print(
        f"RESULT: FAIL - MISMATCH: expected channels={expected_channels}, "
        f"got channels={detected}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
