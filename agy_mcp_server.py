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
import threading
import time
from collections import deque

# mcp 2.x renamed FastMCP -> MCPServer and dropped the `mcp.server.fastmcp`
# module. Same API: _Server("name"), @mcp.tool(), mcp.run() (stdio default).
try:
    from mcp.server import MCPServer as _Server          # mcp >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server    # mcp 1.x


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int_env(name: str, default: int, allow_zero: bool = False) -> int:
    """A malformed value must not take the whole server down on import: warn to
    stderr and fall back, rather than letting int() raise (which silently drops
    the tool from Claude with no visible cause). Common trap: setting this to
    "60m", confusing it with AGY_MCP_PRINT_TIMEOUT."""
    raw = _env(name, str(default))
    try:
        val = int(raw)
        if val < 0 or (val == 0 and not allow_zero):
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

# Off by default, unlike the opencode server. Two reasons: PRINT_TIMEOUT above
# is a real inner limit that agy honours, so a hung run here already fails on
# its own; and agy has no verified per-step heartbeat on either stream, so a
# healthy run that thinks quietly for long enough would be killed for it. Set a
# value in seconds if you have watched a dispatch and know it streams steadily.
IDLE_SECONDS = _int_env("AGY_MCP_IDLE_TIMEOUT", 0, allow_zero=True)

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
# false match kills a healthy run. Each one is a provider-side wall that a CLI
# may report without exiting, leaving the process alive at 0% CPU - which turns
# a 20-second failure into a full wall-clock wait.
#
# Deliberately narrow. "stream error" is excluded: it also covers transient
# drops a CLI retries past on its own. Add provider-specific strings via
# AGY_MCP_FATAL_PATTERNS (comma-separated) rather than widening these.
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
        for s in _env("AGY_MCP_FATAL_PATTERNS", "").split(",")
    ) if p
)

# stdout is lost outright when the MCP transport drops and truncated when a run
# is killed; a file on disk survives both. Report anything the delegate left
# here, on every exit path. Matches the convention in README 5.2.
ARTIFACT_DIR = ".agent-runs"
ARTIFACT_LIMIT = 20

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
        return (f"agy hit a provider-side failure after {elapsed}s and its process group "
                f"was killed, rather than waiting out the {TIMEOUT_SECONDS}s wall clock on "
                f"an error that is already final.\n\n"
                f"  {fatal[0]}\n\n"
                f"That message is the whole diagnosis, including any reset time: decide "
                f"whether to wait or switch provider before re-dispatching.\n\n"
                f"Partial output:\n{out or '(none)'}{artifacts}")

    if verdict == "idle":
        return (f"agy produced no output for {IDLE_SECONDS}s (of {elapsed}s total) and its "
                f"process group was killed as hung. This is NOT the wall-clock timeout: the "
                f"run went silent rather than running long. Raise or unset AGY_MCP_IDLE_TIMEOUT "
                f"if the task legitimately runs silent for that long. Work may still have "
                f"landed - check git.\n\n"
                f"Partial output:\n{out or '(none)'}\n"
                f"--- stderr ---\n{err or '(none)'}{artifacts}")

    if verdict == "timeout":
        return (f"agy did not finish within {TIMEOUT_SECONDS}s; its process group "
                f"was killed. Work may still have landed - check git. Partial output:\n"
                f"{out}{artifacts}")

    if proc.returncode != 0:
        out += (
            f"\n\n--- agy exited {proc.returncode} ---\n"
            f"{err or '(no stderr)'}\n"
            "NOTE: a non-zero exit does NOT mean the work was not done. "
            "Check `git log` / `git status` and re-run the gate before believing this."
        )
    return (out or "(agy produced no output)") + artifacts


def _git(repo: str, *args: str) -> str:
    """One-line git query with a hard timeout. Never network, never mutating -
    a diagnostic must not be able to hang the dispatch it is annotating."""
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip().splitlines()[0] if p.returncode == 0 and p.stdout.strip() else ""


def _installed_version() -> dict:
    """install.sh stamps a VERSION next to this file. Its absence is a real
    answer, not an error: dropping a single .py into a venv by hand is a
    supported way to run this, and "unstamped" is what that looks like."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    data = {}
    try:
        with open(path) as fh:
            for line in fh:
                key, _, val = line.partition("=")
                if val:
                    data[key.strip()] = val.strip()
    except OSError:
        return {}
    return data


# Memo, not a cache with a lifetime: a running server holds its code in memory,
# so this answer cannot change without a /mcp reconnect, which re-imports.
_STALENESS: list = []


def _staleness() -> str:
    """Appended to every dispatch, because a stale install is silent by
    construction. The servers are file copies, so the only other evidence is an
    mtime compared against `git log` by hand - which is how a committed
    process-group fix sat uninstalled through a real incident.

    Strictly local: comparing against the remote would mean a network call on
    every dispatch. The upstream half reads the origin ref as of the checkout's
    last fetch, so it under-reports rather than crying wolf. `delegation_status`
    does the live check.
    """
    if _STALENESS:
        return _STALENESS[0]
    note = ""
    version = _installed_version()
    commit, src = version.get("commit", ""), version.get("source", "")
    if commit and commit != "unknown" and src and os.path.isdir(src):
        head = _git(src, "rev-parse", "--short", "HEAD")
        if head and head != commit:
            note = (f"\n\n[STALE WRAPPER: this server is running {commit}; {src} is at "
                    f"{head}. Fixes committed there are NOT in effect. Run "
                    f"{src}/install.sh, then /mcp reconnect.]")
        elif head:
            behind = _git(src, "rev-list", "--count", "HEAD..@{upstream}")
            if behind.isdigit() and int(behind) > 0:
                note = (f"\n\n[POSSIBLY BEHIND REMOTE: {src} is {behind} commit(s) behind its "
                        f"upstream as of its last fetch. `git -C {src} pull && ./install.sh`, "
                        f"then /mcp reconnect. Run delegation_status for a live check.]")
    _STALENESS.append(note)
    return note


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

    since = time.time()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
        )
    except FileNotFoundError:
        return (f"Error: `agy` CLI not found at {AGY_BIN}. Install it, or set "
                "AGY_BIN to its absolute path and reconnect the MCP server."
                + _staleness())

    return _await(proc, cwd, since) + _staleness()


@mcp.tool()
def delegation_status() -> str:
    """
    Reports what this wrapper actually is: which commit is installed, whether
    that is behind the checkout it came from or behind the remote, where the
    agy CLI resolved to, and the timeouts a dispatch will run under.

    Call this when a dispatch behaves in a way the docstring does not explain,
    before concluding the tool is broken. A wrapper is installed as a file copy
    and a running server holds its code in memory, so "the fix is committed" and
    "the fix is running" are independent facts, and nothing else surfaces the
    difference. Unlike the check baked into every dispatch, this one queries the
    remote, so it costs a network round trip.
    """
    version = _installed_version()
    src = version.get("source", "")
    lines = [
        "agy-wrapper",
        f"  installed:  {version.get('describe') or '(unstamped: not installed via install.sh)'}"
        f"{' [dirty tree]' if version.get('dirty') == '1' else ''}",
        f"  installed at: {version.get('installed', '(unknown)')}",
        f"  source:     {src or '(unknown)'}",
        f"  running:    {os.path.abspath(__file__)}",
    ]

    commit = version.get("commit", "")
    if src and os.path.isdir(src) and commit and commit != "unknown":
        head = _git(src, "rev-parse", "--short", "HEAD")
        if not head:
            lines.append(f"  vs source:  cannot read git in {src}")
        elif head != commit:
            lines.append(f"  vs source:  STALE - installed {commit}, checkout is at {head}. "
                         f"Run {src}/install.sh, then /mcp reconnect.")
        else:
            lines.append(f"  vs source:  current ({head})")
            remote = _git(src, "ls-remote", "--quiet", "origin", "HEAD")
            remote_sha = remote.split()[0][:len(head)] if remote else ""
            if not remote_sha:
                lines.append("  vs remote:  could not reach origin")
            elif remote_sha != head:
                # Deliberately not called "behind": without fetching the remote
                # objects we cannot tell ahead from behind, and telling someone
                # to pull their own unpushed branch is worse than saying less.
                lines.append(f"  vs remote:  DIFFERS - origin/HEAD is {remote_sha}, this checkout "
                             f"is {head}. Either you are ahead (unpushed work) or behind "
                             f"(`git -C {src} pull && ./install.sh`, then /mcp reconnect).")
            else:
                lines.append("  vs remote:  current")
    else:
        lines.append("  vs source:  not comparable (no stamp, or the source is gone)")

    lines += [
        f"  CLI:        {AGY_BIN} -> {_probe([AGY_BIN, '--version'])}",
        f"  print-timeout: {PRINT_TIMEOUT} (agy's own inner limit)",
        f"  model:      {DEFAULT_MODEL}",
        f"  cwd:        {DEFAULT_CWD}",
        f"  wall clock: {TIMEOUT_SECONDS}s",
        f"  idle limit: {str(IDLE_SECONDS) + 's' if IDLE_SECONDS else 'off'}",
        f"  fail-fast:  {len(FATAL_PATTERNS)} stderr patterns",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
