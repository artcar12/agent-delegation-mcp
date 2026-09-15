#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.29,<3"]
# ///
"""
opencode-wrapper: exposes the OpenCode CLI to Claude Code as a local MCP stdio
server.

A dispatch does not block. It spawns the delegate, records the run on disk and
returns a run id; check_run, cancel_run and list_runs work on that record from
any session, including one started after this server has been restarted. See
the run-store comment below for why.

Self-contained on purpose: nothing is imported from its sibling server, and the
dependency is declared inline above, so `uv run --script` on this one file is a
complete way to run it. The run store is therefore duplicated into both servers
rather than factored out; they share RUN_DIR at runtime, not code. Every knob is
an environment variable; the full list is in the README.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

# mcp 2.x renamed FastMCP -> MCPServer and dropped the `mcp.server.fastmcp`
# module. Same API: _Server("name"), @mcp.tool(), mcp.run() (stdio default).
try:
    from mcp.server import MCPServer as _Server      # mcp >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x

SERVER_NAME = "opencode-wrapper"
CLI_LABEL = "opencode"
BIN_ENV_VAR = "OPENCODE_BIN"
TOOL_NAME = "dispatch_opencode"


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
            f"[{SERVER_NAME}] ignoring invalid {name}={raw!r}; using {default}\n"
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

# Caps what a tool RETURNS, not what the delegate writes: output goes straight
# to a file on disk, so nothing is lost by keeping the response small.
MAX_OUTPUT_CHARS = _int_env("AGENT_MCP_MAX_OUTPUT", 100_000)

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

# Where the delegate's DELIVERABLE belongs, inside the target project. Distinct
# from RUN_DIR below, which holds this wrapper's own bookkeeping and raw logs.
# Reported on every exit path. Matches the convention in README 5.2.
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

# ---------------------------------------------------------------------------
# Run store
#
# A dispatch used to exist only as a Popen handle held inside a blocking tool
# call. Anything that killed this server before the call returned - transport
# drop, plugin reload, another session tearing MCP down, SIGKILL - left the
# delegate running in its own session with nothing able to find it, report on
# it or stop it.
#
# The delegate surviving is usually CORRECT: killing an hour-long run because
# the transport blipped is worse than letting it finish. What was wrong is that
# nothing tracked it. So a run is a record on disk - pid, pgid, argv, cwd and
# the paths its output is being written to - which any later server process can
# read, tail, reconcile and cancel, including one started after a full restart.
#
# The store is global rather than per-project: pids are machine-global, and a
# session in one repo has to be able to see a run dispatched into another.
# Deliverables still go to the project's .agent-runs/ (see ARTIFACT_DIR).
RUN_DIR = os.path.expanduser(
    _env("AGENT_MCP_RUN_DIR", "~/.agent-delegation-mcp/runs"))

# Finished records and their output files are pruned after this long. 0 keeps
# them forever.
RETENTION_DAYS = _int_env("AGENT_MCP_RUN_RETENTION_DAYS", 7, allow_zero=True)

# Serialises this process's writes. Two servers sharing RUN_DIR can still race,
# but every write lands via os.replace, so the loser of a race loses an update
# rather than leaving half a record on disk.
_store_lock = threading.Lock()

# Popen handles for runs THIS process started, by run id. The only source of a
# real exit status: waitpid works on your own children and nothing else, so a
# run adopted from a previous server can be observed to have ended but never
# to have ended with a particular code.
_live: dict = {}


def _run_paths(run_id: str) -> tuple:
    base = os.path.join(RUN_DIR, run_id)
    return base + ".json", base + ".out", base + ".err"


def _new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def _proc_info(pid: int) -> tuple:
    """(state, start time) for a pid, or ("", "") when there is no such process.

    Two facts from one `ps`, because both are needed together and neither is
    available from os.kill:

    - Start time is what makes a RECYCLED pid detectable. pids wrap, so a
      record an hour old may name a pid that now belongs to something else, and
      signalling it would kill an innocent process group. Start time comes from
      fork and is not changed by exec, so it can be read immediately after
      Popen returns and still matches later; pid plus fork-second is not an
      identity a subsequent unrelated process plausibly collides with.

    - State is what makes a ZOMBIE visible. os.kill(pid, 0) succeeds against a
      process that has already exited and is merely waiting to be reaped, so
      liveness built on it alone reports a finished delegate as still running,
      forever, whenever nobody is left to call wait() on it - which is exactly
      the situation this whole store exists to handle.
    """
    try:
        p = subprocess.run(["ps", "-p", str(pid), "-o", "state=,lstart="],
                           capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "", ""
    parts = p.stdout.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _proc_identity(pid: int) -> str:
    return _proc_info(pid)[1]


def _pid_exists(pid: int) -> bool:
    """Running or sleeping - not gone, and not an unreaped corpse."""
    if not pid:
        return False
    state, _ = _proc_info(pid)
    return bool(state) and not state.startswith("Z")


def _alive(record: dict) -> bool:
    """Live AND still the process we started. One `ps` answers both halves."""
    pid = record.get("pid")
    if not pid:
        return False
    state, start = _proc_info(pid)
    if not state or state.startswith("Z"):
        return False
    recorded = record.get("identity") or ""
    return not recorded or start == recorded     # nothing recorded: nothing to contradict


def _read_record(run_id: str):
    try:
        with open(_run_paths(run_id)[0]) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_record(record: dict) -> None:
    path = _run_paths(record["run_id"])[0]
    tmp = f"{path}.tmp{os.getpid()}"
    with _store_lock:
        try:
            os.makedirs(RUN_DIR, exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump(record, fh, indent=2)
            os.replace(tmp, path)   # atomic: a concurrent reader gets one whole
        except OSError as exc:      # record or the other, never a partial one
            sys.stderr.write(f"[{SERVER_NAME}] could not write run record: {exc}\n")
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _record_ids() -> list:
    try:
        return sorted(f[:-5] for f in os.listdir(RUN_DIR) if f.endswith(".json"))
    except OSError:
        return []


def _scan_fatal(record: dict) -> None:
    """Latch the first fatal stderr line, resuming from where the last scan
    stopped.

    The offset lives in the record rather than in memory, so a server that
    adopts this run after a restart rescans from zero exactly once and then
    advances like any other. Only complete lines are consumed: stopping at the
    last newline keeps a fatal message that happens to straddle two reads from
    being split down the middle and missed by both.
    """
    if record.get("fatal_line") or not FATAL_PATTERNS:
        return
    start = record.get("scan_offset", 0)
    try:
        with open(record["stderr_path"], "rb") as fh:
            fh.seek(start)
            chunk = fh.read()
    except (OSError, KeyError):
        return
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return                  # nothing complete yet; re-read this next time
    record["scan_offset"] = start + cut + 1
    for line in chunk[:cut].decode("utf-8", "replace").splitlines():
        low = line.lower()
        for p in FATAL_PATTERNS:
            if p in low:
                record["fatal_line"] = line.strip()
                return


def _last_output(record: dict) -> float:
    """When the delegate last wrote anything, on either stream.

    Taken from file mtimes rather than a timer in this process, which is what
    lets the idle rule survive a restart: the evidence is on disk, so a server
    that never saw the earlier output can still tell a thinking run from a hung
    one.
    """
    newest = 0.0
    for key in ("stdout_path", "stderr_path"):
        try:
            newest = max(newest, os.stat(record[key]).st_mtime)
        except (OSError, KeyError):
            pass
    return newest or record.get("started_at", 0.0)


def _terminate(record: dict, verdict: str) -> None:
    """Take down the delegate's whole process group, having first proved the
    pid is still the one we started."""
    recorded = record.get("identity") or ""
    if recorded and _proc_identity(record["pid"]) != recorded:
        record["state"] = "lost"
        record["verdict"] = "pid-recycled"
        record["ended_at"] = time.time()
        return

    # Publish the intent BEFORE signalling. Two reconcilers can be looking at
    # one record - this server's monitor and another session's check_run - and
    # without this the second one sees a process that vanished for no stated
    # reason and writes a bare "exited" over the real verdict. Killing is the
    # one transition where the reason is only known beforehand.
    pid = record["pid"]
    record["state"] = "stopping"
    record["verdict"] = verdict
    _write_record(record)

    pgid = record.get("pgid") or pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        record["state"] = "finished"
        record["ended_at"] = time.time()
        return
    # SIGTERM first so the CLI can flush and clean up; escalate only if it will
    # not go. Polling rather than waiting on the Popen keeps this usable for a
    # run inherited from a previous server, which we cannot wait on. Identity
    # was proved above, so re-checking it 40 times would be 40 more `ps` calls
    # to re-answer a settled question.
    deadline = time.time() + 10
    while time.time() < deadline and _pid_exists(pid):
        time.sleep(0.25)
    if _pid_exists(pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    record["state"] = "killed"
    record["ended_at"] = time.time()


def _reconcile(run_id: str):
    """Bring one record up to date with reality, enforcing its deadlines.

    The single enforcement path. The monitor thread calls it while this server
    lives; check_run and list_runs call it when nobody was watching. That
    second caller is the point: a run whose server died still gets the wall
    clock, idle limit and fail-fast patterns it was started with, applied by
    whichever session looks at it next.

    Deadlines come from the record, not from this process's environment, so a
    run keeps the limits it was dispatched under even when the server that
    adopts it is configured differently - or is the sibling CLI's server.
    """
    record = _read_record(run_id)
    if record is None:
        return record
    if record.get("state") == "stopping":
        # Someone started killing this and did not live to finish. The verdict
        # they recorded is the true one; all that is missing is the finding
        # that the process is now gone.
        if not _pid_exists(record.get("pid", 0)):
            record["state"] = "killed"
            record["ended_at"] = record.get("ended_at") or time.time()
            _write_record(record)
        return record
    if record.get("state") != "running":
        return record
    before = json.dumps(record, sort_keys=True)

    proc = _live.get(run_id)
    if proc is not None:
        # Our own child. poll() answers liveness without a `ps`, and while we
        # hold an unreaped handle the kernel cannot recycle the pid, so there
        # is no identity question to ask. This is the common case and it runs
        # once a second for the length of the run.
        code = proc.poll()
        if code is not None:
            # Note this says the DIRECT child is gone. A delegate that spawned
            # workers into the same group may still have some running.
            _scan_fatal(record)
            record["exit_code"] = code
            record["state"] = "finished"
            record["verdict"] = "exited"
            record["ended_at"] = time.time()
            _write_record(record)
            return record
        running = True
    else:
        running = _alive(record)

    _scan_fatal(record)
    now = time.time()

    if record.get("fatal_line"):
        _terminate(record, "fatal")
    elif not running:
        if _pid_exists(record["pid"]):
            # The pid answers but is no longer the process we started, so the
            # delegate ended some time ago and this pid has been recycled to
            # something unrelated. Say that rather than reporting a clean exit,
            # and - the reason this branch exists at all - never signal it.
            record["state"] = "lost"
            record["verdict"] = "pid-recycled"
        else:
            # Ended while nobody was watching. No exit status is recoverable
            # here: only a parent can reap its child, and that parent is gone.
            record["state"] = "finished"
            record["verdict"] = "exited"
        record["ended_at"] = now
    elif record.get("timeout_seconds") and now - record["started_at"] > record["timeout_seconds"]:
        _terminate(record, "timeout")
    elif record.get("idle_seconds") and now - _last_output(record) > record["idle_seconds"]:
        _terminate(record, "idle")

    if record.get("state") != "running" and proc is not None:
        proc.poll()             # reap our own child rather than leaving a corpse

    # A live run is reconciled once a second; rewriting an unchanged record
    # that often would be pure filesystem churn.
    if json.dumps(record, sort_keys=True) != before:
        _write_record(record)
    return record


def _monitor(run_id: str) -> None:
    """Enforce deadlines promptly while this server is alive. Nothing depends
    on this thread surviving - it is the same _reconcile that check_run calls -
    so losing it costs latency on a verdict, not the verdict itself."""
    while run_id in _live:
        record = _reconcile(run_id)
        if record is None or record.get("state") != "running":
            break
        time.sleep(POLL_SECONDS)
    _live.pop(run_id, None)


def _prune() -> None:
    if not RETENTION_DAYS:
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for run_id in _record_ids():
        record = _read_record(run_id)
        if record is None or record.get("state") in ("running", "stopping"):
            continue
        if (record.get("ended_at") or record.get("started_at") or 0) > cutoff:
            continue
        for path in _run_paths(run_id):
            try:
                os.unlink(path)
            except OSError:
                pass


def _startup() -> None:
    """Reconcile the whole store, then prune. Runs off-thread so a slow
    filesystem cannot delay the client's connect."""
    for run_id in _record_ids():
        try:
            _reconcile(run_id)
        except Exception as exc:        # one bad record must not stop the rest
            sys.stderr.write(f"[{SERVER_NAME}] reconcile {run_id}: {exc}\n")
    _prune()

def _tail(path: str, lines: int) -> str:
    """Last `lines` lines of a file, read from the end so a 400MB log costs the
    same as a small one."""
    if lines <= 0:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            window = min(size, lines * 400 + 4096)
            fh.seek(size - window)
            chunk = fh.read()
    except OSError:
        return ""
    text = chunk.decode("utf-8", "replace")
    if window < size:
        text = text.split("\n", 1)[-1]      # drop the partial leading line
    return _truncate("\n".join(text.splitlines()[-lines:]))


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


def _verdict_note(record: dict) -> str:
    """The prose a verdict needs so it is not misread. Every one of these has
    been misread at least once in practice, which is why they are sentences
    rather than status codes."""
    verdict = record.get("verdict")
    if record.get("state") == "stopping":
        return (f"Being stopped now ({verdict}); the process group has been signalled "
                "and has not gone yet. Check again in a few seconds.")
    elapsed = int((record.get("ended_at") or time.time()) - record["started_at"])
    cli = record.get("cli", CLI_LABEL)

    if verdict == "fatal":
        return (f"{cli} hit a provider-side failure after {elapsed}s and its process group "
                f"was killed, rather than waiting out the wall clock on an error that is "
                f"already final.\n\n  {record.get('fatal_line')}\n\n"
                "That message is the whole diagnosis, including any reset time: decide "
                "whether to wait or switch provider before re-dispatching.")
    if verdict == "idle":
        return (f"{cli} produced no output for {record.get('idle_seconds')}s (of {elapsed}s "
                f"total) and its process group was killed as hung. This is NOT the "
                f"wall-clock timeout: the run went silent rather than running long. Raise "
                f"or unset the idle timeout if the task legitimately runs silent for that "
                f"long. Work may still have landed - check git.")
    if verdict == "timeout":
        return (f"{cli} did not finish within {record.get('timeout_seconds')}s; its process "
                f"group was killed. Work may still have landed - check git.")
    if verdict == "cancelled":
        return f"Cancelled after {elapsed}s. Work already committed is still committed."
    if verdict == "pid-recycled":
        return ("This run's pid now belongs to an unrelated process, so the original "
                "delegate is long gone and nothing was signalled. Its output files below "
                "are still whatever it left.")
    code = record.get("exit_code")
    if code is None:
        return (f"Finished after {elapsed}s. No exit status: this run was started by a "
                f"previous server process, and only a parent can read its child's exit "
                f"code. The output files and the repo are the evidence.")
    if code != 0:
        return (f"{cli} exited {code} after {elapsed}s.\nNOTE: a non-zero exit does NOT "
                f"mean the work was not done. Check `git log` / `git status` and re-run "
                f"the gate before believing it.")
    return f"{cli} exited 0 after {elapsed}s."


def _report(record: dict, tail_lines: int = 80) -> str:
    """Everything known about one run. Deliberately the same shape whether the
    run is live, finished, or was inherited from a server that no longer
    exists - so a caller never has to branch on which."""
    _out, out_path, err_path = _run_paths(record["run_id"])
    state = record.get("state", "?")
    elapsed = int((record.get("ended_at") or time.time()) - record["started_at"])

    head = [
        f"run {record['run_id']}  [{state}]  {record.get('cli', CLI_LABEL)} "
        f"{record.get('model', '')}".rstrip(),
        f"  cwd:     {record.get('cwd', '')}",
        f"  elapsed: {elapsed}s" + ("" if state == "running" else " (ended)"),
        f"  stdout:  {out_path}",
        f"  stderr:  {err_path}",
    ]
    if state in ("running", "stopping"):
        silent = int(time.time() - _last_output(record))
        head.append(f"  silent:  {silent}s since the last line on either stream")

    body = [_verdict_note(record)] if state != "running" else []


    out = _tail(out_path, tail_lines)
    err = _tail(err_path, max(tail_lines // 4, 20))
    body.append(f"--- stdout (last {tail_lines} lines) ---\n{out or '(none)'}")
    if err:
        body.append(f"--- stderr (tail) ---\n{err}")
    body.append(_artifact_note(record.get("cwd", ""), record.get("started_at", 0)).lstrip("\n"))

    if state == "running":
        body.append(f"Still running. Check again with check_run('{record['run_id']}'), or "
                    f"stop it with cancel_run('{record['run_id']}').")
    else:
        body.append("RETURN VALUE IS NOT EVIDENCE, in either direction. Verify with "
                    "`git log` / `git diff --stat` and by re-running the gate yourself.")

    return "\n".join(head) + "\n\n" + "\n\n".join(p for p in body if p)

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
    rules a tool result arrives too late to convey.
    """
    version = _plugin_version()
    return "\n\n".join([
        f"opencode-wrapper {version or '(dev checkout)'} - delegates coding and "
        f"research tasks to the opencode CLI, unattended and self-approving.",
        "dispatch_opencode returns a run id immediately; it does not block. Follow the run "
        "with check_run, stop it with cancel_run, and find runs left by earlier "
        "sessions with list_runs.",
        "Rules a tool result cannot deliver in time:\n"
        "- Brief every delegate to write its deliverable to .agent-runs/<topic>.md. Its "
        "raw stdout is captured to a file too, but the deliverable is what you will "
        "actually want to read.\n"
        "- One dispatch at a time. Models sharing a provider share a quota pool, so a "
        "second call can be why the first one dies. dispatch_opencode now refuses a "
        "concurrent run rather than trusting this to be remembered.\n"
        "- `Connection closed` means this connection dropped, NOT that the delegate "
        "died - it keeps running. Reconnect and call list_runs; do not re-dispatch.\n"
        "- The return value is not evidence in either direction. Verify with git and by "
        "re-running the gate yourself.",
    ])


# `version` is where MCP expects a server to advertise itself (it rides in
# serverInfo), so a client can show it without parsing prose. It is the same
# string the plugin manifest carries, so what a client reports and what Claude
# Code installed cannot drift apart.
try:
    mcp = _Server(SERVER_NAME,
                  version=_plugin_version(),
                  instructions=_instructions())
except TypeError:                   # older SDK without one or both parameters
    try:
        mcp = _Server(SERVER_NAME, instructions=_instructions())
    except TypeError:
        mcp = _Server(SERVER_NAME)

def _live_runs(cli: str = "") -> list:
    """Reconcile the store and return whatever is still running. Reconciling
    here rather than trusting the file is what keeps a dead run from blocking a
    new dispatch forever."""
    out = []
    for run_id in _record_ids():
        record = _reconcile(run_id)
        if record and record.get("state") == "running":
            if not cli or record.get("cli") == cli:
                out.append(record)
    return out


def _dispatch(prompt: str, argv: list, cwd: str, model: str,
              wait_seconds: int, force: bool) -> str:
    cwd = os.path.expanduser(cwd)
    if not os.path.isdir(cwd):
        return (f"Error: cwd {cwd!r} is not an existing directory. Create it first or "
                "pass an absolute path to an existing project.")

    # The "one dispatch at a time" rule has been in the docs since the start
    # and was unenforceable while a run existed only as a local variable. The
    # registry makes it checkable, so it is checked: models sharing a provider
    # share a quota pool, and a second call is a common reason the first dies.
    if not force:
        busy = _live_runs(CLI_LABEL)
        if busy:
            listing = "\n".join(
                f"  {r['run_id']}  {int(time.time() - r['started_at'])}s  {r.get('cwd','')}"
                for r in busy)
            return (f"Refusing to dispatch: {len(busy)} {CLI_LABEL} run(s) already live.\n"
                    f"{listing}\n\n"
                    "Concurrent load on one provider is a common reason a run dies, and "
                    "these share a quota pool. check_run one of the above, cancel_run it, "
                    "or pass force=True if you have a reason to overlap them.")

    run_id = _new_run_id()
    _rec_path, out_path, err_path = _run_paths(run_id)
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        out_fh = open(out_path, "wb")
        err_fh = open(err_path, "wb")
    except OSError as exc:
        return (f"Error: could not open run output files under {RUN_DIR}: {exc}. "
                "Set AGENT_MCP_RUN_DIR to a writable path.")

    # Output goes to these fds, not to a pipe. That is the whole reason a run
    # survives this server intact: the child writes to the file itself, so no
    # part of the transfer needs a live parent, and nothing is buffered in a
    # process that can be killed. start_new_session additionally makes the
    # child a group leader (pgid == pid) so its own workers can be taken down
    # with it.
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=_child_env(),
            stdout=out_fh, stderr=err_fh,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except FileNotFoundError:
        for path in (out_path, err_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        return (f"Error: `{CLI_LABEL}` CLI not found at {argv[0]}. Install it, or set "
                f"{BIN_ENV_VAR} to its absolute path and reconnect the MCP server.")
    except OSError as exc:
        return f"Error: could not start `{CLI_LABEL}`: {exc}"
    finally:
        # The child holds its own dups; keeping ours open would leak two fds
        # per dispatch for the life of the server.
        out_fh.close()
        err_fh.close()

    record = {
        "schema": 1,
        "run_id": run_id,
        "server": SERVER_NAME,
        "cli": CLI_LABEL,
        "tool": TOOL_NAME,
        "pid": proc.pid,
        "pgid": proc.pid,           # start_new_session: the child leads its group
        "identity": _proc_identity(proc.pid),
        "argv": argv,
        "cwd": cwd,
        "model": model,
        "prompt_preview": prompt[:500],
        "started_at": time.time(),
        "timeout_seconds": TIMEOUT_SECONDS,
        "idle_seconds": IDLE_SECONDS,
        "stdout_path": out_path,
        "stderr_path": err_path,
        "scan_offset": 0,
        "fatal_line": None,
        "exit_code": None,
        "ended_at": None,
        "state": "running",
        "verdict": None,
        "server_pid": os.getpid(),
    }
    _live[run_id] = proc
    _write_record(record)
    threading.Thread(target=_monitor, args=(run_id,), daemon=True).start()

    if wait_seconds > 0:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(min(POLL_SECONDS, max(deadline - time.time(), 0.05)))
            current = _read_record(run_id)
            if current and current.get("state") != "running":
                return _report(current)
        current = _read_record(run_id) or record
        return (f"Still running after {wait_seconds}s - the run is unaffected, only the "
                f"waiting stopped.\n\n{_report(current, tail_lines=20)}")

    return (
        f"Dispatched {CLI_LABEL} ({model}) in {cwd}.\n"
        f"  run id: {run_id}\n"
        f"  stdout: {out_path}\n\n"
        f"This returned immediately; the delegate is still running. It is recorded on "
        f"disk, so it survives this server and any later session can reach it:\n"
        f"  check_run('{run_id}')   - state, elapsed, log tail, artifacts\n"
        f"  cancel_run('{run_id}')  - stop it and its workers\n"
        f"  list_runs()             - everything still live\n\n"
        f"Deadlines ({TIMEOUT_SECONDS}s wall clock"
        + (f", {IDLE_SECONDS}s idle" if IDLE_SECONDS else "")
        + ") are enforced on inspection as well as by this server, so they still apply "
        "if this connection drops."
    )

@mcp.tool()
def dispatch_opencode(prompt: str, model: str = DEFAULT_MODEL, cwd: str = DEFAULT_CWD,
                      wait_seconds: int = 0, force: bool = False) -> str:
    """
    Delegates a coding/research task to OpenCode, running fully autonomously
    with NO permission prompts in between.

    RETURNS IMMEDIATELY with a run id - it does not block for the length of the
    run. Follow it with check_run(run_id), stop it with cancel_run(run_id).
    Pass wait_seconds to block up to that long for a short task and get the
    finished result in one round trip; the run is unaffected if the wait
    expires first, only the waiting stops.

    Runs `opencode run --auto --agent build --print-logs --dir <cwd>
    --model <model> -- <prompt>`, with stdout and stderr redirected to files
    under the run store. Same dangerous-mode, secrets and scope caveats as
    dispatch_agy: it self-approves shell commands, file edits and git
    operations under `cwd` with no mid-run checkpoint, so calling this tool IS
    the confirmation step. For long prompts, write a plan .md into
    `.agent-runs/` in `cwd` and point this at it.

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

    MODELS - routing verdict (researched 2026-09-10). Full roster of all 34
    ids, with per-model evidence, sourcing confidence and what is still
    `unknown`, is in MODEL-ROSTER.md at the plugin root. Read it before
    picking anything not named here.

    Prefer opencode-go/deepseek-v4-pro and opencode-go/minimax-m3. DeepSeek V4
    persists reasoning state across sequential tool calls, so long unattended
    loops do not drift; MiniMax M3 has demonstrated ~24h / 1,959 tool calls
    unattended at 100 tok/s, so timeout risk is low. Caveat: both deepseek
    tiers are rejected without an explicit workspace opt-in, so deepseek-v4-pro
    is not a usable default until that is set. opencode-go/qwen3.8-flash is the
    cheap high-volume fallback; glm-5.2 (current default), kimi-k2.7-code and
    gpt-5.6-luna are the ids verified working here by direct test.

    Strictly avoid kimi-k2.6 (infinite repetition loop exhausts the context),
    glm-5.3-flash (40-186s per call breaches wall-clock timeouts),
    nemotron-3.5-lightning-free (drops the closing brace in tool JSON), and
    omen-alpha (ignores the provided tools and rewrites files from scratch).

    Free tier (`opencode/` rather than `opencode-go/`), for smoke tests and
    mechanical work when quota is tight: mimo-v2.5-free is the pick - same
    weights as paid, though it silently strips Authorization headers in
    transport. ling-3.0-flash-fin-free, big-pickle and the muse-sparks also
    pass a write-a-file probe. Block every muse-spark-*-contributor variant,
    free AND paid, for proprietary code: Meta retains prompts and completions
    for training. Both nemotron tiers FAILED here and the failures are
    confirmed upstream bugs.

    Models sharing a provider share one quota pool - when one reports a usage
    limit its siblings are also out; qwen3.8-max and longcat-2.0 are a known
    pair. Free models are small: keep briefs single-step and verify output,
    since a plausible-looking return here is weaker evidence than usual.

    Re-check with `opencode models` - ids go stale, and several above are
    codenames or `preview` labels. When the roster moves, update
    MODEL-ROSTER.md and this docstring together; MODEL-ROSTER.md's "Keeping
    this current" section says how.

    OpenCode's top models are Claude-tier, so unlike dispatch_agy, judgment-
    shaped work - including writing the plan itself - is in scope here. Run
    `opencode stats` before and after big dispatches for real usage numbers.

    DELIVERABLE GOES IN A FILE. Brief the delegate to write its result to
    `.agent-runs/<topic>-<model>.md` and to append one line per phase to
    `.agent-runs/<topic>.log`. Raw stdout is now captured to a file as well, so
    it is no longer lost when this connection drops - but it is still whatever
    the CLI happened to print, and the .log is the progress signal worth
    reading while the run is going.

    ONE DISPATCH AT A TIME, and this is now enforced: a second concurrent
    opencode run is refused, naming the live one, unless you pass force=True.
    Concurrency risks a rate limit, and models sharing a provider share a quota
    pool, so a second call can be the reason the first one dies.

    RETURN VALUE IS NOT EVIDENCE, in either direction. Verify with git and the
    gate.
    """
    # `--` stops flag parsing so a prompt that starts with `-` (e.g. "--help")
    # is treated as the prompt, not as an opencode flag.
    argv = [OPENCODE_BIN, "run", "--auto", "--agent", AGENT, "--print-logs",
            "--dir", os.path.expanduser(cwd), "--model", model, "--", prompt]
    return _dispatch(prompt, argv, cwd, model, wait_seconds, force)


@mcp.tool()
def delegation_status() -> str:
    """
    Reports what this wrapper actually is: which plugin version is running,
    where the opencode CLI resolved to, where runs are recorded, and the
    timeouts a dispatch will actually run under.

    Call this when a dispatch behaves in a way the docstring does not explain,
    before concluding the tool is broken. Claude Code starts an MCP server once
    per session and holds its code in memory, so after a plugin update this
    reports the version still serving this session, not the one on disk.
    """
    version = _plugin_version()
    live = _live_runs()
    lines = [
        SERVER_NAME,
        f"  version:    {version or '(dev checkout: no plugin manifest)'}",
        f"  running:    {os.path.abspath(__file__)}",
        f"  CLI:        {OPENCODE_BIN} -> {_probe([OPENCODE_BIN, '--version'])}",
        f"  agent:      {AGENT}",
        f"  model:      {DEFAULT_MODEL}",
        f"  cwd:        {DEFAULT_CWD}",
        f"  wall clock: {TIMEOUT_SECONDS}s",
        f"  idle limit: {str(IDLE_SECONDS) + 's' if IDLE_SECONDS else 'off'}",
        f"  fail-fast:  {len(FATAL_PATTERNS)} stderr patterns",
        f"  run store:  {RUN_DIR} ({len(_record_ids())} records, "
        f"retention {RETENTION_DAYS or 'forever'}d)",
        f"  live now:   {len(live)} "
        + (", ".join(r["run_id"] for r in live) if live else "(none)"),
    ]
    return "\n".join(lines)

@mcp.tool()
def check_run(run_id: str, tail_lines: int = 80) -> str:
    """
    Reports on a dispatched run: whether it is still going, how long it has
    been silent, the tail of its stdout and stderr, and anything it has written
    under .agent-runs/ in the target project.

    Works on ANY run in the store, not just one this server started - including
    a run whose server has since died. That is the case the old blocking tool
    could not cover: `Connection closed` no longer means the delegate is
    unreachable, it just means this connection dropped.

    Safe and cheap to call repeatedly; this is the intended way to follow a long
    dispatch. Calling it is also what applies the run's deadlines when no server
    is watching, so a hung run left by a dead session is killed the next time
    anyone looks.

    Reading `.agent-runs/<topic>.log` in the project is still the better
    progress signal - the delegate writes that deliberately, whereas the tail
    here is whatever the CLI happened to print.
    """
    record = _reconcile(run_id)
    if record is None:
        known = _record_ids()
        hint = ("\nKnown run ids:\n  " + "\n  ".join(known[-10:])) if known else ""
        return (f"No run {run_id!r} in {RUN_DIR}. It may have been pruned "
                f"(retention: {RETENTION_DAYS or 'forever'} days).{hint}")
    return _report(record, tail_lines=max(tail_lines, 0))


@mcp.tool()
def cancel_run(run_id: str) -> str:
    """
    Stops a dispatched run and every worker it spawned, then reports what it
    had produced up to that point.

    SIGTERM first so the CLI can flush, SIGKILL after 10s if it will not go.
    The whole process group goes, not just the direct child, so an auto-
    approving delegate cannot keep mutating the repo after this returns.

    Works across sessions and server restarts. Before signalling anything it
    re-checks that the recorded pid is still the process that was started: pids
    are recycled, and a stale record must never be allowed to kill something
    unrelated. If the pid has been reused the run is marked lost and nothing is
    signalled.

    Work the delegate already committed stays committed - this stops the agent,
    it does not roll anything back.
    """
    record = _reconcile(run_id)
    if record is None:
        return f"No run {run_id!r} in {RUN_DIR}."
    if record.get("state") != "running":
        return "Already ended - nothing to cancel.\n\n" + _report(record, tail_lines=20)
    _terminate(record, "cancelled")
    _write_record(record)
    _live.pop(run_id, None)
    return _report(record, tail_lines=40)


@mcp.tool()
def list_runs(cwd: str = "", include_finished: bool = True, limit: int = 20) -> str:
    """
    Lists delegate runs, newest last, reconciling each one first.

    Covers both wrapper CLIs - they share one store - and every session on this
    machine, so this is how you find a delegate left behind by a session that
    ended, a plugin reload, or a dropped connection. Pass `cwd` to show only
    runs dispatched into that project; leave it empty for all of them.

    Call this when a previous dispatch's outcome is unknown, before
    re-dispatching anything. A re-dispatch on top of a run that is still going
    doubles the load on a shared quota pool, which is a common way to kill both.
    """
    ids = _record_ids()
    rows = []
    for run_id in ids:
        record = _reconcile(run_id)
        if record is None:
            continue
        if cwd and os.path.abspath(os.path.expanduser(cwd)) != record.get("cwd"):
            continue
        if not include_finished and record.get("state") != "running":
            continue
        rows.append(record)
    if not rows:
        return (f"No runs recorded in {RUN_DIR}"
                + (f" for cwd {cwd}" if cwd else "")
                + f". Retention is {RETENTION_DAYS or 'forever'} days.")

    trimmed = rows[-max(limit, 1):]
    lines = []
    for r in trimmed:
        state = r.get("state", "?")
        mark = "*" if state == "running" else " "
        elapsed = int((r.get("ended_at") or time.time()) - r["started_at"])
        verdict = r.get("verdict") or ""
        if state == "finished" and r.get("exit_code") is not None:
            verdict = f"exit {r['exit_code']}"
        lines.append(
            f"{mark} {r['run_id']}  {state:<8} {elapsed:>6}s  {r.get('cli',''):<8} "
            f"{verdict:<12} {r.get('cwd','')}")

    live = sum(1 for r in rows if r.get("state") == "running")
    header = (f"{len(trimmed)} of {len(rows)} run(s), {live} still live "
              f"(* marks live). Store: {RUN_DIR}")
    return header + "\n" + "\n".join(lines)

if __name__ == "__main__":
    # Off-thread so a slow or large store cannot delay the client's connect.
    threading.Thread(target=_startup, daemon=True).start()
    mcp.run()
