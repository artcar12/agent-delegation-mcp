#!/usr/bin/env python3
"""
agy-wrapper: exposes the Antigravity CLI (`agy`, Gemini) to Claude Code as a
local MCP stdio server.

Self-contained on purpose: nothing is imported from its sibling server, so this
file can be dropped into an existing venv on its own. Every knob is an
environment variable; the full list is in the README.
"""

import os
import shutil
import signal
import subprocess
import sys

# mcp 2.x renamed FastMCP -> MCPServer and dropped the `mcp.server.fastmcp`
# module. Same API: _Server("name"), @mcp.tool(), mcp.run() (stdio default).
try:
    from mcp.server import MCPServer as _Server          # mcp >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server    # mcp 1.x


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int_env(name: str, default: int) -> int:
    """A malformed value must not take the whole server down on import: warn to
    stderr and fall back, rather than letting int() raise (which silently drops
    the tool from Claude with no visible cause). Common trap: setting this to
    "60m", confusing it with AGY_MCP_PRINT_TIMEOUT."""
    raw = _env(name, str(default))
    try:
        val = int(raw)
        if val <= 0:
            raise ValueError("must be positive")
        return val
    except ValueError:
        sys.stderr.write(
            f"[agy-wrapper] ignoring invalid {name}={raw!r}; using {default}\n"
        )
        return default


# Absolute path beats PATH: Claude Code spawns this process without necessarily
# inheriting a login shell's PATH. install.sh records the resolved path.
AGY_BIN = _env("AGY_BIN", shutil.which("agy") or "agy")

# Claude Code spawns this server in the session's directory, so cwd is the right
# default. Set AGENT_MCP_DEFAULT_CWD to pin one project regardless of session.
DEFAULT_CWD = _env("AGENT_MCP_DEFAULT_CWD", os.getcwd())

DEFAULT_MODEL = _env("AGY_MCP_MODEL", "gemini-3.6-flash-high")

# MUST be set: agy 1.1.12 defaults its print-mode wait to 5m0s independently of
# the subprocess timeout below, and kills longer runs after the work landed.
PRINT_TIMEOUT = _env("AGY_MCP_PRINT_TIMEOUT", "60m")

# Outer cap. Keep it above PRINT_TIMEOUT so agy's own limit is the one that hits.
TIMEOUT_SECONDS = _int_env("AGY_MCP_TIMEOUT", 3900)

# The delegate runs in dangerous auto-approve mode, so a stray command can dump
# the environment. Keep the response returnable over stdio and cap memory by
# retaining only the tail of a large stream.
MAX_OUTPUT_CHARS = _int_env("AGENT_MCP_MAX_OUTPUT", 100_000)

mcp = _Server("agy-wrapper")


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return (f"...(truncated: kept the last {MAX_OUTPUT_CHARS} of {len(text)} chars)...\n"
            + text[-MAX_OUTPUT_CHARS:])


def _child_env() -> dict:
    """Never hand Claude's own credentials to the delegate; it doesn't need them
    and could exfiltrate them in dangerous mode."""
    return {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}


def _kill_group(proc: subprocess.Popen) -> None:
    """subprocess only kills the direct child; the delegate spawns its own
    workers. start_new_session makes the child a group leader (pgid == pid) so
    we can take the whole tree down and not leave an auto-approved run mutating
    the target after we've reported it killed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _finish(returncode: int, out: str, err: str) -> str:
    out = _truncate(out or "")
    if returncode != 0:
        out += (
            f"\n\n--- agy exited {returncode} ---\n"
            f"{_truncate(err) or '(no stderr)'}\n"
            "NOTE: a non-zero exit does NOT mean the work was not done. "
            "Check `git log` / `git status` and re-run the gate before believing this."
        )
    return out or "(agy produced no output)"


@mcp.tool()
def ask_agy(prompt: str, model: str = DEFAULT_MODEL, cwd: str = DEFAULT_CWD) -> str:
    """
    Delegates a coding/research task to Gemini via the Antigravity ("agy") CLI,
    running fully autonomously with NO permission prompts in between.

    WHY THIS EXISTS: Gemini quota on the Antigravity plan is plentiful but
    the model is weaker than Claude, and Anthropic quota is scarce. Route
    mechanical, bulk, or exploratory work here and keep Claude quota for
    judgment calls Gemini shouldn't be trusted with (architecture, review,
    anything security- or data-loss-sensitive).

    WHAT IT ACTUALLY DOES: runs `agy --dangerously-skip-permissions
    --new-project --disable-slash-commands --print-timeout 60m --model <model>
    --print <prompt>` as a blocking subprocess and returns stdout. "Dangerous
    mode" means the agent auto-approves its OWN shell commands, file edits and
    git operations with zero human checkpoint mid-run - including push, force
    push and reset --hard - all without asking. Calling this tool IS the
    confirmation step: apply the judgment you'd apply before running those
    commands yourself, and don't invoke it for anything destructive or
    ambiguous without checking with the user first.

    NEVER dispatch a task whose instructions would put a credential, API key,
    token or password into a log, queue payload, URL, commit or debug output.
    The delegate cannot read Claude's own skills or memory and will not stop
    itself, so any such rule has to be inlined into the prompt or plan file.

    SCOPE: working directory is `cwd`, NOT inherited from the caller. Pass it
    explicitly to target another project.

    LONG PROMPTS: for more than a few sentences, write a plan markdown file
    into `.agent-runs/` in `cwd` first and pass a short prompt like
    "read .agent-runs/<file>.md and execute it" - avoids shell arg-length and
    escaping issues and leaves a reviewable artifact.

    RETURN VALUE IS NOT EVIDENCE, in either direction. Always verify with
    `git log` / `git diff --stat` and by re-running the gate yourself.
    """
    cwd = os.path.expanduser(cwd)
    if not os.path.isdir(cwd):
        return (f"Error: cwd {cwd!r} is not an existing directory. Create it first or "
                "pass an absolute path to an existing project.")

    argv = [AGY_BIN, "--dangerously-skip-permissions", "--new-project",
            "--disable-slash-commands", "--print-timeout", PRINT_TIMEOUT,
            "--model", model, "--print", prompt]

    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
        )
    except FileNotFoundError:
        return (f"Error: `agy` CLI not found at {AGY_BIN}. Install it, or set "
                "AGY_BIN to its absolute path and reconnect the MCP server.")

    try:
        out, err = proc.communicate(timeout=TIMEOUT_SECONDS)
        return _finish(proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = proc.communicate()
        partial = _truncate((out or "") + (f"\n--- stderr ---\n{err}" if err else ""))
        return (f"agy did not finish within {TIMEOUT_SECONDS}s; its process group "
                f"was killed. Work may still have landed - check git. Partial output:\n{partial}")


if __name__ == "__main__":
    mcp.run()
