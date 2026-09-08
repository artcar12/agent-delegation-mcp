#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.29,<3"]
# ///
"""
opencode-wrapper: exposes the OpenCode CLI to Claude Code as a local MCP stdio
server.

Self-contained on purpose: nothing is imported from its sibling server, and the
dependency is declared inline above, so `uv run --script` on this one file is a
complete way to run it. Every knob is an environment variable; the full list is
in the README.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque

# mcp 2.x renamed FastMCP -> MCPServer and dropped the `mcp.server.fastmcp`
# module. Same API: _Server("name"), @mcp.tool(), mcp.run() (stdio default).
try:
    from mcp.server import MCPServer as _Server      # mcp >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int_env(name: str, default: int, allow_zero: bool = False) -> int:
    """A malformed value must not take the whole server down on import: warn to
    stderr and fall back, rather than letting int() raise (which silently drops
    the tool from Claude with no visible cause)."""
    raw = _env(name, str(default))
    try:
        val = int(raw)
        if val < 0 or (val == 0 and not allow_zero):
            raise ValueError("must be positive")
        return val
    except ValueError:
        sys.stderr.write(
            f"[opencode-wrapper] ignoring invalid {name}={raw!r}; using {default}\n"
        )
        return default


# `opencode` usually lives in an nvm-managed node dir, which an MCP subprocess
# may not inherit on PATH - and whose path carries the node version, so it moves
# on a node upgrade. shutil.which answers from whatever PATH this process was
# given, which is not necessarily a login shell's: set OPENCODE_BIN to an
# absolute path, or point a stable symlink at it, when that comes up empty.
OPENCODE_BIN = _env("OPENCODE_BIN", shutil.which("opencode") or "opencode")

# Claude Code spawns this server in the session's directory, so cwd is the right
# default. Set AGENT_MCP_DEFAULT_CWD to pin one project regardless of session.
DEFAULT_CWD = _env("AGENT_MCP_DEFAULT_CWD", os.getcwd())

DEFAULT_MODEL = _env("OPENCODE_MCP_MODEL", "opencode-go/glm-5.2")

# Overrides opencode.json's "default_agent". See the docstring: the usual
# default is `plan`, which is read-only and silently edits nothing.
AGENT = _env("OPENCODE_MCP_AGENT", "build")

TIMEOUT_SECONDS = _int_env("OPENCODE_MCP_TIMEOUT", 3600)

# Wall clock cannot tell "thinking hard" from "dead": a quota-exhausted run sits
# at 0% CPU forever and burns the full TIMEOUT_SECONDS in silence. This is the
# inner limit `opencode run` does not have - unlike agy, it exposes no timeout
# flag of its own, so nothing else in the stack will ever cut a hung run short.
# Safe to enable by default only because --print-logs below gives a per-step
# heartbeat on stderr; without it a quiet-but-healthy run would be killed.
# 0 disables.
IDLE_SECONDS = _int_env("OPENCODE_MCP_IDLE_TIMEOUT", 600, allow_zero=True)

# The delegate runs in dangerous auto-approve mode, so a stray command can dump
# the environment. Keep the response returnable over stdio and cap memory by
# retaining only the tail of a large stream.
MAX_OUTPUT_CHARS = _int_env("AGENT_MCP_MAX_OUTPUT", 100_000)

# Bound memory for real, which _truncate alone never did: it trimmed only after
# the whole stream had been buffered. Lines beyond these caps are dropped as
# they arrive, oldest first.
MAX_OUTPUT_LINES = 50_000
MAX_LINE_CHARS = 8_000

POLL_SECONDS = 1.0

# Matched case-insensitively against the child's stderr, and ONLY stderr: these
# strings appear routinely in stdout that merely discusses quotas or auth, and a
# false match kills a healthy run. Each one is a wall the CLI reports and then
# does NOT exit on - it keeps the process alive at 0% CPU, which is what turns a
# 20-second failure into an hour of waiting.
#
# Deliberately narrow. "stream error" is excluded: it also covers transient
# drops the CLI retries past on its own, and the quota line that motivated this
# list carries AI_APICallError in the same record anyway. Add provider-specific
# strings via OPENCODE_MCP_FATAL_PATTERNS (comma-separated) rather than widening
# these.
FATAL_PATTERNS = tuple(
    p for p in (
        "ai_apicallerror",
        "usage limit reached",
        "quota exceeded",
        "insufficient credit",
        "invalid api key",
        "authentication failed",
    ) + tuple(
        s.strip().lower()
        for s in _env("OPENCODE_MCP_FATAL_PATTERNS", "").split(",")
    ) if p
)

# stdout is lost outright when the MCP transport drops and truncated when a run
# is killed; a file on disk survives both. Report anything the delegate left
# here, on every exit path. Matches the convention in README 5.2.
ARTIFACT_DIR = ".agent-runs"
ARTIFACT_LIMIT = 20


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


def _reader(pipe, sink: deque, clock: list, fatal: list, patterns: tuple) -> None:
    """Drain one pipe on its own thread.

    communicate() cannot do this job: it blocks until EOF, so nothing can look
    at the stream while the run is still going, and every check below would be
    unreachable. `clock` holds the monotonic time of the most recent output on
    EITHER stream - that is what the idle timer watches. Scanning happens here
    rather than over `sink` so a fatal line cannot be evicted by maxlen before
    anyone reads it.
    """
    try:
        for line in pipe:
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "...(line truncated)\n"
            sink.append(line)
            clock[0] = time.monotonic()
            if patterns and fatal[0] is None:
                low = line.lower()
                for p in patterns:
                    if p in low:
                        fatal[0] = line.strip()
                        break
    except (ValueError, OSError):
        pass                    # pipe closed under us by the kill path
    finally:
        try:
            pipe.close()
        except (ValueError, OSError):
            pass


def _artifact_note(cwd: str, since: float) -> str:
    """Point at whatever the delegate wrote, whether or not the run succeeded.
    This is the only part of a failed dispatch that is recoverable."""
    root = os.path.join(cwd, ARTIFACT_DIR)
    found = []
    try:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                # A second of slack: filesystem mtime granularity is coarser
                # than time.time() and a file written immediately after spawn
                # can carry a slightly earlier stamp.
                if st.st_mtime >= since - 1:
                    found.append((os.path.relpath(path, cwd), st.st_size))
    except OSError:
        return ""
    if not found:
        return ""
    found.sort()
    listing = "\n".join(f"  {p} ({s:,} bytes)" for p, s in found[:ARTIFACT_LIMIT])
    more = ("" if len(found) <= ARTIFACT_LIMIT
            else f"\n  ...and {len(found) - ARTIFACT_LIMIT} more")
    return f"\n\nFiles written under {ARTIFACT_DIR}/ during this run:\n{listing}{more}"


def _await(proc: subprocess.Popen, cwd: str, since: float) -> str:
    """Watch a running delegate and return once it finishes - or once it is
    provably not going to."""
    out_lines: deque = deque(maxlen=MAX_OUTPUT_LINES)
    err_lines: deque = deque(maxlen=MAX_OUTPUT_LINES)
    clock = [time.monotonic()]
    fatal: list = [None]

    threads = (
        threading.Thread(target=_reader,
                         args=(proc.stdout, out_lines, clock, fatal, ()),
                         daemon=True),
        threading.Thread(target=_reader,
                         args=(proc.stderr, err_lines, clock, fatal, FATAL_PATTERNS),
                         daemon=True),
    )
    for t in threads:
        t.start()

    started = time.monotonic()
    verdict = None
    while proc.poll() is None:
        now = time.monotonic()
        if fatal[0] is not None:
            verdict = "fatal"
            break
        if IDLE_SECONDS and now - clock[0] > IDLE_SECONDS:
            verdict = "idle"
            break
        if now - started > TIMEOUT_SECONDS:
            verdict = "timeout"
            break
        time.sleep(POLL_SECONDS)

    if verdict:
        _kill_group(proc)
    for t in threads:
        t.join(timeout=5)

    elapsed = int(time.monotonic() - started)
    out = _truncate("".join(out_lines))
    err = _truncate("".join(err_lines))
    artifacts = _artifact_note(cwd, since)

    if verdict == "fatal":
        return (f"opencode hit a provider-side failure after {elapsed}s and its process "
                f"group was killed. It does not exit on these by itself - it idles until "
                f"the {TIMEOUT_SECONDS}s wall clock, which is why this is cut short here.\n\n"
                f"  {fatal[0]}\n\n"
                f"That message is the whole diagnosis, including any reset time: decide "
                f"whether to wait or switch provider before re-dispatching.\n\n"
                f"Partial output:\n{out or '(none)'}{artifacts}")

    if verdict == "idle":
        return (f"opencode produced no output for {IDLE_SECONDS}s (of {elapsed}s total) and "
                f"its process group was killed as hung. This is NOT the wall-clock timeout: "
                f"the run went silent rather than running long. Check "
                f"~/.local/share/opencode/log/opencode.log and `opencode stats` for the "
                f"cause, and raise OPENCODE_MCP_IDLE_TIMEOUT if the task legitimately runs "
                f"silent for that long. Work may still have landed - check git.\n\n"
                f"Partial output:\n{out or '(none)'}\n"
                f"--- stderr ---\n{err or '(none)'}{artifacts}")

    if verdict == "timeout":
        return (f"opencode did not finish within {TIMEOUT_SECONDS}s; its process group "
                f"was killed. Work may still have landed - check git. Partial output:\n"
                f"{out}{artifacts}")

    if proc.returncode != 0:
        out += (
            f"\n\n--- opencode exited {proc.returncode} ---\n"
            f"{err or '(no stderr)'}\n"
            "NOTE: a non-zero exit does NOT mean the work was not done. "
            "Check `git log` / `git status` and re-run the gate before believing this."
        )
    return (out or "(opencode produced no output)") + artifacts


def _plugin_version() -> str:
    """The version Claude Code installed, read from the plugin manifest beside
    this file. Empty when this is a bare checkout rather than an installed
    plugin, which is an answer and not an error: `version` is optional in
    serverInfo, and a client that shows nothing for an uninstalled copy is
    right to.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".claude-plugin", "plugin.json")
    try:
        with open(path) as fh:
            return str(json.load(fh).get("version", ""))
    except (OSError, ValueError):
        return ""


def _probe(argv: list) -> str:
    """First line of a `--version`-style call, or a reason it produced none."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL, env=_child_env())
    except FileNotFoundError:
        return "(not found)"
    except subprocess.TimeoutExpired:
        return "(no answer within 10s)"
    except OSError as exc:
        return f"(failed: {exc})"
    text = (p.stdout or p.stderr or "").strip().splitlines()
    return text[0] if text else "(no output)"


def _instructions() -> str:
    """Handed to the client at connect time, so it lands in the session before
    the first dispatch rather than after it. What belongs here is exactly the
    rules whose whole point is that a tool result arrives too late to convey
    them - a dropped connection or a killed run returns nothing to read.
    """
    version = _plugin_version()
    return "\n\n".join([
        f"opencode-wrapper {version or '(dev checkout)'} - delegates coding and "
        f"research tasks to the opencode CLI, unattended and self-approving.",
        "Rules a tool result cannot deliver in time:\n"
        "- Brief every delegate to write its deliverable to .agent-runs/<topic>.md, never "
        "to stdout alone. stdout is lost outright if this connection drops mid-run.\n"
        "- One dispatch at a time. Models sharing a provider share a quota pool, so a "
        "second call can be why the first one dies.\n"
        "- `Connection closed` is NOT evidence the delegate died; it keeps running as an "
        "orphan. pgrep, check .agent-runs/, wait. Never re-dispatch.\n"
        "- Call delegation_status for the running version, the resolved CLI path and the "
        "timeouts a dispatch will actually run under.",
    ])


# `version` is where MCP expects a server to advertise itself (it rides in
# serverInfo), so a client can show it without parsing prose. It is the same
# string the plugin manifest carries, so what a client reports and what Claude
# Code installed cannot drift apart.
try:
    mcp = _Server("opencode-wrapper",
                  version=_plugin_version(),
                  instructions=_instructions())
except TypeError:                   # older SDK without one or both parameters
    try:
        mcp = _Server("opencode-wrapper", instructions=_instructions())
    except TypeError:
        mcp = _Server("opencode-wrapper")


@mcp.tool()
def ask_opencode(prompt: str, model: str = DEFAULT_MODEL, cwd: str = DEFAULT_CWD) -> str:
    """
    Delegates a coding/research task to OpenCode, running fully autonomously
    with NO permission prompts in between.

    Runs `opencode run --auto --agent build --print-logs --dir <cwd>
    --model <model> -- <prompt>` as a blocking subprocess and returns stdout.
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

    `--print-logs` is what makes failure visible: opencode's stream errors go to
    its own logfile, never to stdout, and it does not exit on them. Routed to
    stderr they can be caught in seconds instead of blocking for the full hour.

    MODELS: the two deepseek tiers are China-hosted and rejected without an
    explicit workspace opt-in, so they are not usable defaults. Verified
    working here: opencode-go/glm-5.2 (default), opencode-go/kimi-k2.7-code
    (higher quota, code-tuned), opencode-go/gpt-5.6-luna. Re-check with
    `opencode models` - ids go stale. Note that models sharing a provider share
    one quota pool: when one reports a usage limit, its siblings are also out.

    OpenCode's top models are Claude-tier, so unlike ask_agy, judgment-shaped
    work - including writing the plan itself - is in scope here. Run
    `opencode stats` before and after big dispatches for real usage numbers.

    DELIVERABLE GOES IN A FILE, NOT STDOUT. Brief the delegate to write its
    result to `.agent-runs/<topic>-<model>.md` and to append one line per phase
    to `.agent-runs/<topic>.log`. Never brief it to "print the review to
    stdout": stdout is lost outright if the MCP transport drops mid-run, and
    truncated to a tail if the run is killed, so a stdout-only deliverable can
    vanish after an hour of real work. A file survives both, and this tool
    lists whatever was written there on every exit path. Read the .log while
    the run is still going - a blocking subprocess gives no other progress
    signal.

    ONE DISPATCH AT A TIME. Do not put two delegation calls in a single tool
    block. Concurrency risks a rate limit, and models sharing a provider share
    a quota pool, so a second call can be the reason the first one dies.

    RETURN VALUE IS NOT EVIDENCE, in either direction. Verify with git and the
    gate.
    """
    cwd = os.path.expanduser(cwd)
    if not os.path.isdir(cwd):
        return (f"Error: cwd {cwd!r} is not an existing directory. Create it first or "
                "pass an absolute path to an existing project.")

    # `--` stops flag parsing so a prompt that starts with `-` (e.g. "--help")
    # is treated as the prompt, not as an opencode flag.
    argv = [OPENCODE_BIN, "run", "--auto", "--agent", AGENT, "--print-logs",
            "--dir", cwd, "--model", model, "--", prompt]

    since = time.time()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
        )
    except FileNotFoundError:
        return (f"Error: `opencode` CLI not found at {OPENCODE_BIN}. Install it, or "
                "set OPENCODE_BIN to its absolute path and reconnect the MCP server.")

    return _await(proc, cwd, since)


@mcp.tool()
def delegation_status() -> str:
    """
    Reports what this wrapper actually is: which plugin version is running,
    where the opencode CLI resolved to, and the timeouts a dispatch will
    actually run under.

    Call this when a dispatch behaves in a way the docstring does not explain,
    before concluding the tool is broken. Claude Code starts an MCP server once
    per session and holds its code in memory, so after a plugin update this
    reports the version still serving this session, not the one on disk.
    """
    version = _plugin_version()
    lines = [
        "opencode-wrapper",
        f"  version:    {version or '(dev checkout: no plugin manifest)'}",
        f"  running:    {os.path.abspath(__file__)}",
        f"  CLI:        {OPENCODE_BIN} -> {_probe([OPENCODE_BIN, '--version'])}",
        f"  agent:      {AGENT}",
        f"  model:      {DEFAULT_MODEL}",
        f"  cwd:        {DEFAULT_CWD}",
        f"  wall clock: {TIMEOUT_SECONDS}s",
        f"  idle limit: {str(IDLE_SECONDS) + 's' if IDLE_SECONDS else 'off'}",
        f"  fail-fast:  {len(FATAL_PATTERNS)} stderr patterns",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
