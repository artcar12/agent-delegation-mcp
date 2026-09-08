#!/usr/bin/env python3
"""
agy-wrapper: exposes the Antigravity CLI (`agy`, Gemini) to Claude Code as a
local MCP stdio server.

Self-contained on purpose: nothing is imported from its sibling server, so this
file can be dropped into an existing venv on its own. Every knob is an
environment variable; the full list is in the README.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

# `--check` also runs from a SessionStart hook, where latency is paid on every
# session start. Importing the MCP SDK costs about a second - eight times the
# rest of the check - and the check never serves a request, so stand in a no-op
# registrar rather than making every session wait for an import it will not use.
_CHECK_ONLY = "--check" in sys.argv[1:]

if _CHECK_ONLY:
    class _Server:                                       # never serves anything
        def __init__(self, *args, **kwargs): pass
        def tool(self, *args, **kwargs): return lambda fn: fn
else:
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


# Most installs are not git checkouts - the repo is public, so ask GitHub
# instead of assuming a local clone. Overridable for a fork.
REPO_SLUG = _env("AGENT_MCP_REPO", "artcar12/agent-delegation-mcp")

# One unauthenticated GET per day, per server, to api.github.com. It sends
# nothing but the request itself. Set AGENT_MCP_UPDATE_CHECK=0 to disable it
# outright; the local-checkout comparison keeps working either way.
UPDATE_CHECK = _env("AGENT_MCP_UPDATE_CHECK", "1") not in ("0", "false", "no")
UPDATE_TTL = _int_env("AGENT_MCP_UPDATE_TTL", 86_400)

# Filled from the cache file at import, then refreshed in the background. Read
# through this rather than memoising, so a refresh that lands mid-session is
# picked up by the next dispatch instead of waiting for a reconnect.
_UPDATE: dict = {}


def _cache_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".update-check.json")


def _fetch_update(slug: str, commit: str, _branch: str = "") -> dict:
    """Ask GitHub whether the latest published RELEASE is newer than this copy.

    Deliberately the release, not the default branch: unreleased commits on main
    are work in progress, and telling every user to reinstall because a branch
    moved is noise. No releases published means nothing to compare, not an error.

    `compare/BASE...HEAD` reports HEAD relative to BASE, so status "ahead" means
    the release tag is ahead of what is installed. That distinction is what
    keeps a local build quiet: a dev commit sitting ahead of the last release
    comes back "behind" or "diverged", and a commit GitHub has never seen comes
    back 404 - neither is an update.
    """
    tag = _api(f"https://api.github.com/repos/{slug}/releases/latest", commit)
    if isinstance(tag, dict) and tag.get("_error"):
        return {"status": "no-release"} if tag["_error"] == 404 else {
            "status": "error", "detail": f"HTTP {tag['_error']}"}
    if not isinstance(tag, dict) or not tag.get("tag_name"):
        return {"status": "error", "detail": "unreadable release"}
    name = tag["tag_name"]

    body = _api(f"https://api.github.com/repos/{slug}/compare/{commit}...{name}", commit)
    if isinstance(body, dict) and body.get("_error"):
        return {"status": "unknown-commit", "tag": name} if body["_error"] == 404 else {
            "status": "error", "detail": f"HTTP {body['_error']}", "tag": name}
    return {"status": body.get("status", "error"),
            "ahead_by": body.get("ahead_by", 0), "tag": name}


def _api(url: str, commit: str):
    """Unauthenticated GET. Returns the decoded body, or {"_error": code}."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"agent-delegation-mcp/{commit}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code}
    except Exception:                           # offline, DNS, TLS, malformed
        return {"_error": 0}


def _refresh_update(commit: str, branch: str, slug: str) -> None:
    result = _fetch_update(slug, commit, branch)
    result.update({"checked": int(time.time()), "commit": commit, "slug": slug})
    _UPDATE.update(result)
    try:
        with open(_cache_path(), "w") as fh:
            json.dump(result, fh)
    except OSError:
        pass                                    # a read-only install dir is not fatal


def _load_cached(commit: str) -> bool:
    """Populate _UPDATE from the cache file. Returns True when that result is
    still within its TTL, i.e. when no fetch is needed. A cache written for a
    different commit is ignored: after an update, the old answer is not about
    this install any more."""
    try:
        with open(_cache_path()) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(cached, dict) or cached.get("commit") != commit:
        return False
    _UPDATE.update(cached)                      # stale-while-revalidate
    return int(time.time()) - int(cached.get("checked", 0)) < UPDATE_TTL


def _start_update_check() -> None:
    """Load the cache now, refresh it later. Never block: this runs at import,
    and a client that gives a server a startup deadline must not be held up by
    a network call - which is the same class of bug as the hang this whole
    wrapper exists to cut short."""
    version = _installed_version()
    commit = version.get("commit", "")
    if not UPDATE_CHECK or not commit or commit == "unknown":
        return
    slug = version.get("remote") or REPO_SLUG
    branch = version.get("branch") or "main"
    if _load_cached(commit):                    # still fresh, nothing to do
        return
    threading.Thread(target=_refresh_update, args=(commit, branch, slug),
                     daemon=True).start()


def _remote_note() -> str:
    """What GitHub said, as of the cache. Silent unless it is actionable."""
    if _UPDATE.get("status") == "ahead" and _UPDATE.get("ahead_by"):
        slug = _UPDATE.get("slug", REPO_SLUG)
        return (f"UPDATE AVAILABLE: release {_UPDATE.get('tag', '?')} is "
                f"{_UPDATE['ahead_by']} commit(s) ahead of this install. See "
                f"https://github.com/{slug}/releases/latest, re-run install.sh, "
                f"then /mcp reconnect.")
    return ""



def _staleness_suffix() -> str:
    """Same finding, appended to a tool result. Belt to the instructions' braces:
    a client that drops server instructions still sees this one."""
    note = _remote_note()
    return f"\n\n[{note}]" if note else ""


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
    the first dispatch rather than after it. Two things belong here and nowhere
    else: a stale install, which the caller needs to know BEFORE spending an
    hour of delegate time on code that does not contain the fix; and the rules
    whose whole point is that a tool result arrives too late to convey them.
    """
    version = _installed_version()
    head = [f"agy-wrapper {version.get('describe') or '(unstamped)'} - delegates coding and "
            f"research tasks to the agy CLI, unattended and self-approving."]
    note = _remote_note()
    if note:
        head.insert(0, f"!! {note}")
    head.append(
        "Rules a tool result cannot deliver in time:\n"
        "- Brief every delegate to write its deliverable to .agent-runs/<topic>.md, never to "
        "stdout alone. stdout is lost outright if this connection drops mid-run.\n"
        "- One dispatch at a time. Models sharing a provider share a quota pool, so a second "
        "call can be why the first one dies.\n"
        "- `Connection closed` is NOT evidence the delegate died; it keeps running as an "
        "orphan. pgrep, check .agent-runs/, wait. Never re-dispatch.\n"
        "- Call delegation_status for versions, the resolved CLI path and effective timeouts."
    )
    return "\n\n".join(head)


# Constructed here, not at the top: the instructions below are computed, and the
# helpers that compute them have to exist first.
_start_update_check()

# `version` is where MCP expects a server to advertise itself (it rides in
# serverInfo), so a client can show it without parsing prose. `instructions`
# carries the part a client cannot infer: whether that version is the one the
# source tree actually holds.
try:
    mcp = _Server("agy-wrapper",
                  version=_installed_version().get("describe", ""),
                  instructions=_instructions())
except TypeError:                   # older SDK without one or both parameters
    try:
        mcp = _Server("agy-wrapper", instructions=_instructions())
    except TypeError:
        mcp = _Server("agy-wrapper")


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
                + _staleness_suffix())

    return _await(proc, cwd, since) + _staleness_suffix()


@mcp.tool()
def delegation_status() -> str:
    """
    Reports what this wrapper actually is: which commit is installed, whether
    GitHub has a newer one, where the agy CLI resolved to, and the timeouts a
    dispatch will actually run under.

    Call this when a dispatch behaves in a way the docstring does not explain,
    before concluding the tool is broken. A wrapper is installed as a file copy
    and a running server holds its code in memory, so "a fix exists" and "a fix
    is running" are independent facts, and nothing else surfaces the difference.
    This queries GitHub live rather than reading the daily cache, so it costs a
    network round trip.
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
    slug = version.get("remote") or REPO_SLUG
    branch = version.get("branch") or "main"
    if not commit or commit == "unknown":
        lines.append("  vs GitHub:  no version stamp; re-run install.sh")
    elif not UPDATE_CHECK:
        lines.append("  vs GitHub:  update check disabled (AGENT_MCP_UPDATE_CHECK=0)")
    else:
        _refresh_update(commit, branch, slug)
        status, ahead = _UPDATE.get("status"), _UPDATE.get("ahead_by", 0)
        tag = _UPDATE.get("tag", "?")
        if status == "ahead" and ahead:
            lines.append(f"  vs release: UPDATE AVAILABLE - {tag} is {ahead} commit(s) "
                         f"ahead. Re-run install.sh, then /mcp reconnect.")
        elif status == "identical":
            lines.append(f"  vs release: up to date with {tag}")
        elif status == "no-release":
            lines.append(f"  vs release: {slug} has published no releases; nothing to compare")
        elif status == "unknown-commit":
            lines.append(f"  vs release: {commit} is not on {slug} (a local build?)")
        elif status in ("behind", "diverged"):
            lines.append(f"  vs release: ahead of {tag} ({status}); no update needed")
        else:
            lines.append(f"  vs release: unreachable ({_UPDATE.get('detail', 'unknown')})")

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


def _check_cli() -> int:
    """`python agy_mcp_server.py --check` - a staleness check that needs no git
    checkout, which is the normal case: almost nobody installs this by cloning.
    Forces a fetch rather than trusting the cache, and leaves the result in the
    cache for the running servers. Exit 1 means an update is available.
    """
    version = _installed_version()
    commit = version.get("commit", "")
    print(f"agy-wrapper {version.get('describe') or '(unstamped)'}")
    print(f"  {os.path.abspath(__file__)}")

    if not commit or commit == "unknown":
        print("  no version stamp, so nothing to compare. Re-run install.sh.")
        return 0
    if not UPDATE_CHECK:
        print("  update check disabled (AGENT_MCP_UPDATE_CHECK=0)")
        return 0

    slug = version.get("remote") or REPO_SLUG
    branch = version.get("branch") or "main"
    # Honour the daily cache unless --force. The SessionStart hook runs this on
    # every session start, and the unauthenticated GitHub budget is 60 requests
    # an hour: a forced fetch each time would spend it on an answer that cannot
    # have changed. --force is there for CI and for checking right after a
    # release lands.
    if "--force" in sys.argv[1:] or not _load_cached(commit):
        _refresh_update(commit, branch, slug)
    status, ahead = _UPDATE.get("status"), _UPDATE.get("ahead_by", 0)
    tag = _UPDATE.get("tag", "?")
    if status == "ahead" and ahead:
        print(f"  UPDATE AVAILABLE: release {tag} is {ahead} commit(s) ahead of this install.")
        print(f"  https://github.com/{slug}/releases/latest - re-run install.sh, then /mcp reconnect.")
        return 1
    if status == "identical":
        print(f"  up to date with release {tag}")
    elif status == "no-release":
        print(f"  {slug} has published no releases; nothing to compare")
    elif status == "unknown-commit":
        print(f"  {commit} is not a commit on {slug} (a local build?); nothing to compare")
    elif status in ("behind", "diverged"):
        print(f"  ahead of release {tag} ({status}); no update needed")
    else:
        print(f"  could not reach GitHub ({_UPDATE.get('detail', 'unknown')}); try again later")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(_check_cli())
    mcp.run()
