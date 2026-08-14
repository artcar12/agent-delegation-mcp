#!/usr/bin/env python3
"""
opencode-wrapper: exposes the OpenCode CLI to Claude Code as a local MCP stdio
server.

Self-contained on purpose: nothing is imported from its sibling server, so this
file can be dropped into an existing venv on its own. Every knob is an
environment variable; the full list is in the README.
"""

import os
import shutil
import subprocess

# mcp 2.x renamed FastMCP -> MCPServer and dropped the `mcp.server.fastmcp`
# module. Same API: _Server("name"), @mcp.tool(), mcp.run() (stdio default).
try:
    from mcp.server import MCPServer as _Server          # mcp >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server    # mcp 1.x


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


# `opencode` usually lives in an nvm-managed node dir, which an MCP subprocess
# may not inherit on PATH - and whose path carries the node version, so it moves
# on a node upgrade. install.sh resolves it once and records it in OPENCODE_BIN;
# re-run the installer (or re-point a stable symlink) after a node upgrade.
OPENCODE_BIN = _env("OPENCODE_BIN", shutil.which("opencode") or "opencode")

# Claude Code spawns this server in the session's directory, so cwd is the right
# default. Set AGENT_MCP_DEFAULT_CWD to pin one project regardless of session.
DEFAULT_CWD = _env("AGENT_MCP_DEFAULT_CWD", os.getcwd())

DEFAULT_MODEL = _env("OPENCODE_MCP_MODEL", "opencode-go/glm-5.2")

# Overrides opencode.json's "default_agent". See the docstring: the usual
# default is `plan`, which is read-only and silently edits nothing.
AGENT = _env("OPENCODE_MCP_AGENT", "build")

TIMEOUT_SECONDS = int(_env("OPENCODE_MCP_TIMEOUT", "3600"))

mcp = _Server("opencode-wrapper")


def _finish(result) -> str:
    out = result.stdout or ""
    if result.returncode != 0:
        out += (
            f"\n\n--- opencode exited {result.returncode} ---\n"
            f"{result.stderr or '(no stderr)'}\n"
            "NOTE: a non-zero exit does NOT mean the work was not done. "
            "Check `git log` / `git status` and re-run the gate before believing this."
        )
    return out or "(opencode produced no output)"


@mcp.tool()
def ask_opencode(prompt: str, model: str = DEFAULT_MODEL, cwd: str = DEFAULT_CWD) -> str:
    """
    Delegates a coding/research task to OpenCode, running fully autonomously
    with NO permission prompts in between.

    Runs `opencode run --auto --agent build --dir <cwd> --model <model>
    <prompt>` as a blocking subprocess (capped at 3600s) and returns stdout.
    Same dangerous-mode, secrets and scope caveats as ask_agy: it self-approves
    shell commands, file edits and git operations under `cwd` with no mid-run
    checkpoint, so calling this tool IS the confirmation step. For long
    prompts, write a plan .md into `.agent-runs/` in `cwd` and point this at it.

    `--agent build` is NOT optional whenever ~/.config/opencode/opencode.json
    sets "default_agent": "plan", which is read-only. Without the override this
    tool silently returns a plan and edits nothing while looking like it worked.

    `--dir` is NOT optional either: opencode ignores the subprocess working
    directory. Verified by mutation - with only subprocess(cwd=...) set, it
    wrote the file into the CALLER's directory while reporting success. This is
    the opencode analogue of agy's --new-project trap.

    MODELS: the two deepseek tiers are China-hosted and rejected without an
    explicit workspace opt-in, so they are not usable defaults. Verified
    working here: opencode-go/glm-5.2 (default), opencode-go/kimi-k2.7-code
    (higher quota, code-tuned), opencode-go/gpt-5.6-luna. Re-check with
    `opencode models` - ids go stale.

    OpenCode's top models are Claude-tier, so unlike ask_agy, judgment-shaped
    work - including writing the plan itself - is in scope here. Run
    `opencode stats` before and after big dispatches for real usage numbers.

    RETURN VALUE IS NOT EVIDENCE, in either direction. Verify with git and the
    gate.
    """
    try:
        result = subprocess.run(
            [OPENCODE_BIN, "run", "--auto", "--agent", AGENT,
             "--dir", cwd, "--model", model, prompt],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
        return _finish(result)
    except subprocess.TimeoutExpired as e:
        partial = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return (f"opencode did not finish within {TIMEOUT_SECONDS}s and was killed. "
                f"Work may still have landed - check git. Partial output:\n{partial}")
    except FileNotFoundError:
        return (f"Error: `opencode` CLI not found at {OPENCODE_BIN}. Install it, or "
                "set OPENCODE_BIN to its absolute path and reconnect the MCP server.")
    except NotADirectoryError:
        return f"Error: cwd {cwd!r} is not a directory."


if __name__ == "__main__":
    mcp.run()
