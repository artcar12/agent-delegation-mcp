#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.29,<3"]
# ///
"""
gemini-web-wrapper: exposes the Gemini *web app* to MCP clients as a local
stdio server.

The sibling servers reach Gemini through an API that has no Canvas, no Gems,
no conversation history and no attachments. Those live only in
the logged-in web app. gemini_web.py drives it with Playwright; this file wraps
that worker exactly the way agy_mcp_server.py wraps `agy`, so a browser run is
recorded on disk and stays checkable, tailable and cancellable across dropped
connections, session ends and server restarts.

Playwright deliberately does NOT run in this process. The worker is an ordinary
child process whose fds are redirected to files, which is the whole reason a
run outlives the server that started it.

Self-contained on purpose: nothing is imported from its sibling servers, and
the dependency is declared inline above, so `uv run --script` on this one file
is a complete way to run it. The run store is therefore duplicated into all
three servers rather than factored out; they share RUN_DIR at runtime, not
code. Every knob is an environment variable; the full list is in the README.

One browser, one profile, so genuinely one run at a time -- the per-CLI refusal
in _dispatch is not just quota etiquette here, it is a hard constraint. Chrome
will not open the same user-data-dir twice.
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

SERVER_NAME = "gemini-web-wrapper"
CLI_LABEL = "gemini-web"
BIN_ENV_VAR = "GEMINI_WEB_BIN"
TOOL_NAME = "dispatch_gemini"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int_env(name: str, default: int, allow_zero: bool = False) -> int:
    """A malformed value must not take the whole server down on import: warn to
    stderr and fall back, rather than letting int() raise (which silently drops
    the tool from the client with no visible cause)."""
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


# The worker lives beside this file. Resolved absolutely because a client may
# spawn this server from anywhere, and because the run record has to name
# something a later session can still make sense of.
WORKER = _env("GEMINI_WEB_WORKER",
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "gemini_web.py"))

# `uv run --script` is what honours the worker's inline dependency block, so it
# is the launcher, not python. Absolute path beats PATH: a client spawns this
# process without necessarily inheriting a login shell's PATH, so shutil.which
# can come up empty where `uv` runs fine in a terminal. Set GEMINI_WEB_BIN to a
# ready-made executable to skip uv entirely.
UV_BIN = _env("GEMINI_WEB_UV_BIN", shutil.which("uv") or "uv")
GEMINI_WEB_BIN = _env(BIN_ENV_VAR, "")


def _worker_argv(*args: str) -> list:
    """The command that runs one worker subcommand."""
    if GEMINI_WEB_BIN:
        return [GEMINI_WEB_BIN, *args]
    return [UV_BIN, "run", "--script", WORKER, *args]


# A client spawns this server in the session's directory, so cwd is the right
# default. Set AGENT_MCP_DEFAULT_CWD to pin one project regardless of session.
DEFAULT_CWD = _env("AGENT_MCP_DEFAULT_CWD", os.getcwd())

# The web app has no model flag; what varies is which *surface* the prompt is
# sent to. This rides in the run record's `model` field so list_runs shows
# something worth reading.
DEFAULT_MODEL = _env("GEMINI_WEB_MCP_MODE", "chat")
MODES = ("chat", "canvas", "image", "video")

# Mirrors gemini_web.py. Duplicated rather than imported, like everything else
# here: each server file has to stay a self-contained `uv run --script` target.
# These are API between the two files -- change them in both or not at all.
EXIT_NOT_LOGGED_IN = 3
EXIT_THROTTLED = 6
EXIT_TRANSIENT = 7

# MUST stay below TIMEOUT_SECONDS: the worker's own deadline is the one that
# should hit, because it exits cleanly with whatever the page had rendered
# instead of being killed with nothing to show.
WORKER_TIMEOUT = _int_env("GEMINI_WEB_MCP_WORKER_TIMEOUT", 1500)

# Outer cap. Generous because Canvas and image/video generation can
# take minutes, and a cold Chrome launch is ~20s before anything starts.
TIMEOUT_SECONDS = _int_env("GEMINI_WEB_MCP_TIMEOUT", 1800)

# Off by default, and unlike the other two servers this is not a tuning
# preference but close to a correctness requirement: a browser run prints
# NOTHING between launch and the final answer, so every healthy run looks idle
# for its entire duration. Only set this if you have added progress logging
# to the worker.
IDLE_SECONDS = _int_env("GEMINI_WEB_MCP_IDLE_TIMEOUT", 0, allow_zero=True)

# Caps what a tool RETURNS, not what the worker writes: output goes straight to
# a file on disk, so nothing is lost by keeping the response small.
MAX_OUTPUT_CHARS = _int_env("AGENT_MCP_MAX_OUTPUT", 100_000)

POLL_SECONDS = 1.0

# Matched case-insensitively against the child's stderr, and ONLY stderr.
# Narrow on purpose. The worker exits on its own errors, so these exist for the
# cases where Playwright leaves a process alive with nothing left to do.
FATAL_PATTERNS = tuple(
    p for p in (
        "not signed in",
        "executable doesn't exist",
        "browsertype.launchpersistentcontext",
    ) + tuple(
        s.strip().lower()
        for s in _env("GEMINI_WEB_MCP_FATAL_PATTERNS", "").split(",")
    ) if p
)

# Where the delegate's DELIVERABLE belongs, inside the target project. Distinct
# from RUN_DIR below, which holds this wrapper's own bookkeeping and raw logs.
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
    if code == EXIT_THROTTLED:
        return (f"Gemini answered with a limit message rather than content, after "
                f"{elapsed}s. This is a quota wall, not a failure to fix: do NOT "
                f"retry in a loop, and do not route around it by calling the tool "
                f"harder. Tell the user; the live numbers are in the web app under "
                f"Settings -> Usage limits. The message itself is in stderr below.")
    if code == EXIT_TRANSIENT:
        return (f"Gemini glitched rather than answering, after {elapsed}s - not a "
                f"limit. ONE retry is reasonable; if a second fails, stop and say "
                f"so rather than looping. The message itself is in stderr below.")
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
    the first call rather than after it. What belongs here is exactly the rules
    a tool result arrives too late to convey.
    """
    version = _plugin_version()
    return "\n\n".join([
        f"gemini-web-wrapper {version or '(dev checkout)'} - drives the Gemini "
        f"WEB APP (live Google search, Canvas, conversation history, "
        f"attachments) "
        f"through a real Chrome. Not the Gemini API.",
        "gemini_ask blocks and returns the answer. dispatch_gemini returns a run id "
        "immediately; follow it with check_run, stop it with cancel_run, and find runs "
        "left by earlier sessions with list_runs.",
        "USE THIS FOR WEB RESEARCH BY DEFAULT. Whatever built-in search you have is "
        "almost certainly worse: Gemini is searching Google, with the web app's own "
        "grounding, a live index and the ability to actually open and read the pages "
        "it finds. A WebSearch/WebFetch pair returns snippets and one page at a time. "
        "If a question needs the current web, ask gemini_ask before you conclude "
        "something is undocumented, and ask it again with a narrower prompt rather "
        "than falling back to a weaker tool.",
        "THERE IS NO DEEP RESEARCH TOOL HERE, AND YOU DO NOT NEED ONE. It was "
        "built, tested and removed in 1.3.0: two of three kick-offs wedged silently "
        "for hours with no error, and a wedged run never recovers. Known-brittle "
        "machinery is worse than none, because it invites a plan that depends on "
        "it.\n"
        "What replaces it is not a workaround, it is the better tool for nearly "
        "everything: ONE long, specific gemini_ask, then follow-ups into the same "
        "conversation_id to push on whatever came back thin. There is no prompt "
        "length worth worrying about and follow-ups are free. A thorough "
        "comparison, a survey of the options, 'what changed in X since Y', 'give me "
        "a URL per claim' - all of these land better this way, in half a minute "
        "rather than forty. A head-to-head on exactly this proved it: the long "
        "prompt answered in 37 seconds and the Deep Research run never finished.\n"
        "If a job truly does need Deep Research - dozens of sources read end to "
        "end, a written report as the deliverable - ASK THE USER to run it in the "
        "browser and hand you the conversation id. You can then read the finished "
        "thread with gemini_read_conversation. Do not try to drive it yourself.",
        "One caveat on prompting: demanding a verbatim quote for every claim can make "
        "the model announce it cannot browse and then answer from memory anyway. "
        "Asking for a URL per claim is safe; asking for quotes is where it gets "
        "skittish. If an answer says it has no web access, it is wrong - retry "
        "without that instruction.",
        "DO NOT RATION CALLS HERE. This quota is a SEPARATE pool from the "
        "agy/gemini CLI's - the sibling servers share one API quota, which is why "
        "they tell you to dispatch one at a time, and that rule does NOT apply to "
        "this server. An agy run being rate-limited says nothing about this one. "
        "The web app's allowance is large enough that ordinary use does not "
        "approach it: after a heavy day of use its rolling window read 16% "
        "consumed and its weekly limit 1%.\n"
        "So ask as many times as the work deserves. Do not bundle four questions "
        "into one prompt to save requests, do not skip a follow-up, and do not "
        "skip a verification pass because it would be a second call. The real "
        "limits are wall clock and the single browser (below), not quota.\n"
        "Running out through this server takes real effort - the one way to do it "
        "is several Deep Research runs on Pro High inside a five-hour window, and "
        "this server cannot start those. If it does happen you will get a clear "
        "error rather than a wrong answer (see the throttle rule below), and the "
        "live numbers are in the web app under Settings -> Usage limits - look "
        "there rather than trusting this text.",
        "STAY ON FLASH. Flash - including Flash with a long, detailed prompt - is "
        "enough for essentially everything a CLI agent asks for: lookups, version "
        "checks, comparisons, 'what changed in X since Y', reading and summarising "
        "pages. Reach for Pro only for a genuinely hard reasoning problem, which is "
        "rare in this kind of work. Two reasons this matters. First, Pro is the one "
        "model whose daily limits you can actually exhaust; Flash is where the "
        "allowance is effectively unreachable. Second, THIS SERVER CANNOT SWITCH "
        "MODELS - the picker holds whatever the profile was last left on, and "
        "changing it is a human action in the browser. So if you conclude a task "
        "needs Pro, say so and let the user decide; do not treat one thin Flash "
        "answer as proof, sharpen the prompt and ask again first.",
        "THE LOUDEST QUOTA SYMPTOM IS SILENCE. Gemini's own account of its "
        "limit behaviour is that exhausting Pro DOWNGRADES YOU TO FLASH with no "
        "message - you get a real answer from a smaller model and nothing says "
        "so. Every answer therefore reports which model produced it; read that "
        "field before concluding a weak answer means the question was hard. The "
        "other two silent-ish symptoms: a full conversation limit LOCKS the "
        "prompt box (surfaces here as 'the prompt box never appeared'), and "
        "compute-heavy modes - image, video, canvas - are WITHDRAWN from the "
        "tools drawer while throttled (surfaces as 'no tool labelled ...'). "
        "Neither is a DOM break, though both read like one.",
        "IF A CALL COMES BACK THROTTLED, STOP. Exit 6 means Gemini replied with a "
        "limit message instead of an answer. Exit 7 means it glitched. Seven is "
        "worth exactly one retry. SIX IS WORTH NONE - retrying into a limit is how "
        "a soft throttle becomes a hard one, and the account being throttled is the "
        "user's own paid subscription, so the cost of getting this wrong lands on "
        "them. Report it and let them decide. Note what these codes exist to "
        "prevent: Gemini renders a limit notice as an ordinary response turn, so "
        "without the check the worker would hand you 'Sorry, something went wrong' "
        "as though it were the answer. If a short, odd, system-sounding reply ever "
        "does reach you as content, treat it as a limit notice rather than a "
        "finding - the pattern list is good, not exhaustive.",
        "DO NOT TEST THIS TOOL WITH A FIXED CANARY STRING. Checking that the "
        "browser still works is reasonable; sending 'reply with exactly: pong' "
        "fifty times is not. An identical one-word prompt repeated against one "
        "account is the most obviously scripted thing in the whole flow - more "
        "so than any timing, which is what the worker already goes to some "
        "trouble to disguise. Vary it: ask something short you actually wanted "
        "to know, or at minimum change both the wording and the expected answer "
        "each time. A connectivity check that doubles as a real question costs "
        "nothing extra and looks like use rather than instrumentation.",
        "Rules a tool result cannot deliver in time:\n"
        "- There is ONE browser on ONE profile, so genuinely one call at a time. This is "
        "not quota etiquette like the sibling servers, it is a hard constraint: Chrome "
        "cannot open the same user-data-dir twice. Every tool here refuses rather than "
        "corrupting the profile.\n"
        "- gemini_ask has a floor of roughly 20 seconds: Chrome has to launch and the "
        "Angular app has to hydrate before a prompt can even be typed. It is not hung.\n"
        "- A browser run prints nothing at all between launch and the final answer, so "
        "an empty log tail in check_run means 'still working', not 'stuck'.\n"
        "- If anything reports 'not signed in', the fix is a human one: run "
        "`uv run --script gemini_web.py login` in a terminal and sign in by hand. "
        "Google rejects its own sign-in flow inside an automated browser, so no tool "
        "here can do it for you.\n"
        "- The return value is not evidence that work landed on disk. Verify with git.",
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

# --------------------------------------------------------------------------
# Synchronous path
#
# The async path above records a run and returns; these block. They exist
# because most questions are a 30-second round trip and a run id is pure
# overhead for those. They share the single browser with the async path, so
# they take the same lock and honour the same refusal.
# --------------------------------------------------------------------------

_browser_lock = threading.Lock()


def _busy_note() -> str:
    """Non-empty when something already holds the browser."""
    live = _live_runs(CLI_LABEL)
    if live:
        listing = "\n".join(
            f"  {r['run_id']}  {int(time.time() - r['started_at'])}s  {r.get('cwd','')}"
            for r in live)
        return (f"Refusing: {len(live)} {CLI_LABEL} run(s) already hold the browser.\n"
                f"{listing}\n\n"
                "Chrome cannot open the same profile twice, so this is a hard "
                "conflict rather than a courtesy. check_run one of the above, or "
                "cancel_run it.")
    return ""


def _worker_sync(args: list, timeout: int) -> tuple:
    """Run one worker subcommand to completion. Returns (rc, stdout, stderr)."""
    argv = _worker_argv(*args)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              env=_child_env())
    except FileNotFoundError:
        return 127, "", (f"{argv[0]} not found. Set {BIN_ENV_VAR} to a ready-made "
                         "executable, or GEMINI_WEB_UV_BIN to the uv binary.")
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer within {timeout}s"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _meta_of(stdout: str) -> dict:
    """The worker's trailing GEMINI_WEB_META line, as a dict."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("GEMINI_WEB_META "):
            try:
                return json.loads(line[len("GEMINI_WEB_META "):])
            except ValueError:
                return {}
    return {}


def _strip_meta(stdout: str) -> str:
    return "\n".join(l for l in stdout.splitlines()
                     if not l.startswith("GEMINI_WEB_META ")).strip()


@mcp.tool()
def gemini_ask(prompt: str, conversation_id: str = "", mode: str = "chat",
               files: str = "", timeout_seconds: int = 240) -> str:
    """
    Asks the Gemini WEB APP a question and BLOCKS until the answer is back.

    mode: chat | canvas | image | video
    files: comma-separated paths to attach (uploaded to the account)
    conversation_id: continue an earlier thread; every answer reports its id

    Gemini behind the browser, not the API - your history, Gems and uploads are
    all there.

    PREFER THIS OVER YOUR BUILT-IN WEB SEARCH. Gemini searches Google and reads
    the pages it finds; a snippet tool does not. Ask here before concluding
    something is undocumented, and ask for a URL per claim.

    ONE LONG PROMPT BEATS SEVERAL SHORT ONES, and follow-ups into the same
    conversation_id are nearly free. Never bundle questions to save calls -
    quota here is not a constraint worth managing.

    FLASH HANDLES ALMOST EVERYTHING. Pro is for rare hard reasoning and is the
    only model whose daily limit is reachable. This tool cannot switch models
    regardless, so ask the user if you genuinely need Pro.

    EXPECT ~20s MINIMUM even for a one-word reply: Chrome has to launch and the
    app has to hydrate. That is the floor, not a fault.

    Use dispatch_gemini when the work may outlast this tool's timeout.
    """
    if mode not in MODES:
        return f"Error: mode must be one of {', '.join(MODES)}; got {mode!r}."
    busy = _busy_note()
    if busy:
        return busy
    if not _browser_lock.acquire(blocking=False):
        return ("Refusing: another gemini_ask is using the browser right now. "
                "One profile, one Chrome - wait for it to return.")
    try:
        args = ["ask", "--prompt", prompt,
                "--timeout", str(max(timeout_seconds - 20, 30))]
        if conversation_id:
            args += ["--conversation", conversation_id]
        if mode != "chat":
            args += ["--mode", mode]
        for path in [f.strip() for f in files.split(",") if f.strip()]:
            args += ["--file", path]
        rc, out, err = _worker_sync(args, timeout_seconds)
    finally:
        _browser_lock.release()

    if rc != 0:
        hint = ""
        if rc == EXIT_NOT_LOGGED_IN:
            hint = ("\n\nSign in first: run `uv run --script gemini_web.py login` "
                    "in a terminal. Google blocks its own sign-in flow inside an "
                    "automated browser, so this cannot be done for you.")
        elif rc == EXIT_THROTTLED:
            hint = ("\n\nThat is a quota wall, not a bug. Do NOT retry in a loop and "
                    "do NOT compensate by calling this tool harder - that is how a "
                    "soft limit becomes a hard one. Say so plainly and let the user "
                    "decide; Settings -> Usage limits in the web app has the real "
                    "numbers.")
        elif rc == EXIT_TRANSIENT:
            hint = ("\n\nGemini glitched rather than hitting a limit. ONE retry is "
                    "reasonable here. If a second fails, stop and report it instead "
                    "of looping.")
        return f"Error: the Gemini worker exited {rc}.\n{_truncate(err.strip())}{hint}"

    meta = _meta_of(out)
    answer = _strip_meta(out)
    footer = []
    if meta.get("conversation_id"):
        footer.append(f"conversation_id: {meta['conversation_id']}  "
                      f"(pass it back to continue this thread)")
    if meta.get("model"):
        footer.append(f"answered by: {meta['model']}  (an unexpected Flash here "
                      f"can mean Pro quota ran out - the app downgrades silently)")
    if meta.get("elapsed_seconds"):
        footer.append(f"{meta['elapsed_seconds']}s, extracted via "
                      f"{meta.get('extraction', '?')}")
    return _truncate(answer) + ("\n\n---\n" + "\n".join(footer) if footer else "")


@mcp.tool()
def gemini_conversations(query: str = "", limit: int = 20) -> str:
    """
    Lists the conversations in the Gemini sidebar, newest first, as id + title.

    The ids are what gemini_ask(conversation_id=...) and
    gemini_read_conversation() take. Pass `query` to filter on title text.

    This opens a browser, so it costs the same ~20s as any other call here.
    """
    busy = _busy_note()
    if busy:
        return busy
    args = ["list", "--limit", str(max(limit, 1))]
    if query:
        args += ["--query", query]
    rc, out, err = _worker_sync(args, 180)
    if rc != 0:
        return f"Error: the Gemini worker exited {rc}.\n{_truncate(err.strip())}"
    try:
        rows = json.loads(out)
    except ValueError:
        return _truncate(out)
    if not rows:
        return "No conversations matched." if query else "No conversations found."
    return "\n".join(f"{r['id']}  {r['title']}" for r in rows)


@mcp.tool()
def gemini_read_conversation(conversation_id: str) -> str:
    """
    Dumps an existing Gemini conversation as markdown, every turn, oldest
    first, without adding to it.

    Use this to pick up a thread you or Gemini started earlier - including one
    started by hand in the browser - before continuing it with
    gemini_ask(conversation_id=...). Find ids with gemini_conversations().
    """
    if not conversation_id.strip():
        return "Error: conversation_id is required. List them with gemini_conversations()."
    busy = _busy_note()
    if busy:
        return busy
    rc, out, err = _worker_sync(
        ["read", "--conversation", conversation_id.strip()], 240)
    if rc != 0:
        return f"Error: the Gemini worker exited {rc}.\n{_truncate(err.strip())}"
    return _truncate(_strip_meta(out)) or "(the conversation rendered empty)"


@mcp.tool()
def dispatch_gemini(prompt: str, conversation_id: str = "", mode: str = DEFAULT_MODEL,
                    files: str = "", out: str = "", cwd: str = DEFAULT_CWD,
                    wait_seconds: int = 0, force: bool = False) -> str:
    """
    Sends a prompt to the Gemini WEB APP and RETURNS IMMEDIATELY with a run id.

    Follow it with check_run(run_id), stop it with cancel_run(run_id). Pass
    wait_seconds to block up to that long and get the finished result in one
    round trip; the run is unaffected if the wait expires first.

    A browser run is SILENT until it finishes - no streaming, no progress on
    stdout. An empty tail in check_run means it is still working. Judge it by
    elapsed time, not by output.

    mode: chat | canvas | image | video
    conversation_id: continue an existing thread instead of starting one.
    files: comma-separated paths to attach.
    out: write the answer here as well, relative to cwd. Use the
         .agent-runs/<topic>.md convention so check_run reports it as an
         artifact on every exit path.
    """
    if mode not in MODES:
        return f"Error: mode must be one of {', '.join(MODES)}; got {mode!r}."

    args = ["ask", "--prompt", prompt, "--timeout", str(WORKER_TIMEOUT)]
    if conversation_id:
        args += ["--conversation", conversation_id]
    if mode != "chat":
        args += ["--mode", mode]
    for path in [f.strip() for f in files.split(",") if f.strip()]:
        args += ["--file", path]
    if out:
        args += ["--out", out if os.path.isabs(out)
                 else os.path.join(os.path.expanduser(cwd), out)]

    return _dispatch(prompt, _worker_argv(*args), cwd, mode, wait_seconds, force)


@mcp.tool()
def delegation_status() -> str:
    """
    Reports what this wrapper actually is: which plugin version is running,
    where the worker and its Chrome profile live, whether that profile is still
    signed in, where runs are recorded, and the timeouts a dispatch will run
    under.

    Call this when a call behaves in a way the docstring does not explain,
    before concluding the tool is broken - a silently expired Google session
    looks like a broken tool and is not one. Checking sign-in opens a browser,
    so this is slower than the sibling servers' status.
    """
    version = _plugin_version()
    live = _live_runs()
    if live:
        signed_in = "(not checked: a run holds the browser)"
    else:
        rc, out, err = _worker_sync(["status"], 180)
        try:
            info = json.loads(out)
            signed_in = ("yes, as " + (info.get("account") or "(unknown)")
                         if info.get("logged_in")
                         else "NO - run `uv run --script gemini_web.py login`")
            profile = info.get("profile", "?")
        except ValueError:
            signed_in = f"unknown (worker exited {rc}: {err.strip()[:120]})"
            profile = "?"
    lines = [
        SERVER_NAME,
        f"  version:    {version or '(dev checkout: no plugin manifest)'}",
        f"  running:    {os.path.abspath(__file__)}",
        f"  worker:     {' '.join(_worker_argv())}",
        f"  signed in:  {signed_in}",
        f"  modes:      {', '.join(MODES)} (default {DEFAULT_MODEL})",
        f"  cwd:        {DEFAULT_CWD}",
        f"  worker cap: {WORKER_TIMEOUT}s (inner; must stay under the wall clock)",
        f"  wall clock: {TIMEOUT_SECONDS}s",
        f"  idle limit: {str(IDLE_SECONDS) + 's' if IDLE_SECONDS else 'off (browser runs are silent by design)'}",
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
            f"{mark} {r['run_id']}  {state:<8} {elapsed:>6}s  {r.get('cli',''):<10} "
            f"{verdict:<12} {r.get('cwd','')}")

    live = sum(1 for r in rows if r.get("state") == "running")
    header = (f"{len(trimmed)} of {len(rows)} run(s), {live} still live "
              f"(* marks live). Store: {RUN_DIR}")
    return header + "\n" + "\n".join(lines)

if __name__ == "__main__":
    # Off-thread so a slow or large store cannot delay the client's connect.
    threading.Thread(target=_startup, daemon=True).start()
    mcp.run()
