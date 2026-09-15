#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Exercises the run store in both servers against fake CLIs.

Runs with plain `python3 test/test_run_store.py` - no MCP install needed. The
decorator is the only thing the servers use from the SDK, so a stub that
returns the function unchanged is enough to import them, and every function
under test is ordinary code underneath.

Each server is loaded twice under different module names to model the case the
store exists for: the run is started by one server process and inspected by a
LATER one that never held the Popen handle.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import warnings
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stub_mcp():
    """Minimal stand-in for the SDK: @mcp.tool() must return the function."""
    if "mcp" in sys.modules:
        return
    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")

    class MCPServer:
        def __init__(self, name, version=None, instructions=None):
            self.name, self.version, self.instructions = name, version, instructions

        def tool(self, *a, **kw):
            return lambda fn: fn

        def run(self):
            raise AssertionError("mcp.run() must not be reached under test")

    server.MCPServer = MCPServer
    mcp.server = server
    sys.modules["mcp"] = mcp
    sys.modules["mcp.server"] = server


def _load(filename, alias, env):
    """Import a server file under a private module name with `env` applied, so
    two 'server processes' can exist side by side in one interpreter."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        path = os.path.join(ROOT, filename)
        spec = importlib.util.spec_from_file_location(alias, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fake_cli(directory, body):
    """A stand-in for agy/opencode: a shell script we fully control."""
    path = os.path.join(directory, "fake-cli")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


class RunStoreTests(unittest.TestCase):
    SERVER = "agy_mcp_server.py"
    BIN_VAR = "AGY_BIN"
    TIMEOUT_VAR = "AGY_MCP_TIMEOUT"
    IDLE_VAR = "AGY_MCP_IDLE_TIMEOUT"
    counter = 0

    def setUp(self):
        _stub_mcp()
        # orphan() drops Popen handles deliberately, which is the whole point
        # of the run store; the interpreter's complaint about it is noise here.
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.mkdtemp(prefix="runstore-")
        self.run_dir = os.path.join(self.tmp, "runs")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)

    def tearDown(self):
        for mod in list(sys.modules):
            if mod.startswith("srv_"):
                del sys.modules[mod]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, script_body="sleep 30\n", **extra):
        """A freshly imported server whose CLI is a script we control."""
        RunStoreTests.counter += 1
        env = {
            "AGENT_MCP_RUN_DIR": self.run_dir,
            self.BIN_VAR: _fake_cli(self.tmp, script_body),
            "AGENT_MCP_DEFAULT_CWD": self.project,
        }
        env.update(extra)
        return _load(self.SERVER, f"srv_{RunStoreTests.counter}", env)

    def dispatch(self, mod, **kw):
        fn = getattr(mod, mod.TOOL_NAME)
        return fn(kw.pop("prompt", "do the thing"), cwd=self.project, **kw)

    def orphan(self, mod, run_id):
        """Model the server dying: drop the Popen handle, which is the only
        thing that made this process the run's owner, and stop its monitor."""
        mod._live.pop(run_id, None)
        time.sleep(1.2)         # let the monitor notice and exit its loop
        return run_id

    def run_id_from(self, text):
        for line in text.splitlines():
            if "run id:" in line:
                return line.split("run id:")[1].strip()
        self.fail(f"no run id in:\n{text}")

    def wait_until(self, fn, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fn():
                return True
            time.sleep(0.1)
        return False

    # ---------------------------------------------------------------- tests

    def test_dispatch_returns_immediately_and_records_the_run(self):
        mod = self.server("sleep 30\n")
        started = time.time()
        out = self.dispatch(mod)
        self.assertLess(time.time() - started, 5, "dispatch blocked")
        run_id = self.run_id_from(out)

        record = mod._read_record(run_id)
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["cwd"], self.project)
        self.assertEqual(record["pgid"], record["pid"])
        self.assertTrue(record["identity"], "process identity was not captured")
        self.assertTrue(os.path.exists(record["stdout_path"]))
        mod.cancel_run(run_id)

    def test_output_survives_the_server_that_started_it(self):
        """The whole point: a second server process, holding no Popen handle,
        still finds the run, reads its output and can stop it."""
        first = self.server("echo hello-from-delegate; sleep 30\n")
        run_id = self.run_id_from(self.dispatch(first))
        self.assertTrue(self.wait_until(
            lambda: "hello-from-delegate" in open(
                first._read_record(run_id)["stdout_path"]).read()))

        second = self.server()          # a different "server process"
        self.assertNotIn(run_id, second._live)
        report = second.check_run(run_id)
        self.assertIn("hello-from-delegate", report)
        self.assertIn("running", report)

        listing = second.list_runs()
        self.assertIn(run_id, listing)
        self.assertIn("1 still live", listing)

        pid = second._read_record(run_id)["pid"]
        second.cancel_run(run_id)
        self.assertFalse(second._pid_exists(pid), "cancel left the process alive")
        self.assertEqual(second._read_record(run_id)["state"], "killed")

    def test_exit_code_is_recorded_for_our_own_child(self):
        mod = self.server("exit 3\n")
        run_id = self.run_id_from(self.dispatch(mod))
        self.assertTrue(self.wait_until(
            lambda: mod._read_record(run_id)["state"] != "running"))
        record = mod._reconcile(run_id)
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("exited 3", mod.check_run(run_id))

    def test_adopted_run_reports_finished_without_an_exit_code(self):
        """Only a parent can reap its child. An adopted run can be seen to have
        ended but not to have ended with a particular code - and must say so
        rather than implying success."""
        first = self.server("sleep 2\n")
        run_id = self.orphan(first, self.run_id_from(self.dispatch(first)))
        self.assertTrue(self.wait_until(lambda: not first._pid_exists(
            first._read_record(run_id)["pid"])))

        second = self.server()
        record = second._reconcile(run_id)
        self.assertEqual(record["state"], "finished")
        self.assertIsNone(record["exit_code"])
        self.assertIn("No exit status", second.check_run(run_id))

    def test_wall_clock_is_enforced_lazily_by_a_later_server(self):
        """A run whose server died still gets the deadline it was dispatched
        under, applied by whoever looks at it next."""
        first = self.server("sleep 300\n", **{self.TIMEOUT_VAR: "2"})
        run_id = self.orphan(first, self.run_id_from(self.dispatch(first)))
        pid = first._read_record(run_id)["pid"]
        time.sleep(2.5)

        second = self.server()
        record = second._reconcile(run_id)
        self.assertEqual(record["verdict"], "timeout")
        self.assertEqual(record["state"], "killed")
        self.assertFalse(second._pid_exists(pid))

    def test_idle_timeout_uses_output_mtime_not_a_live_timer(self):
        mod = self.server("echo tick; sleep 300\n", **{self.IDLE_VAR: "2"})
        run_id = self.run_id_from(self.dispatch(mod))
        pid = mod._read_record(run_id)["pid"]
        self.assertTrue(self.wait_until(
            lambda: mod._read_record(run_id)["state"] != "running", timeout=20))
        record = mod._read_record(run_id)
        self.assertEqual(record["verdict"], "idle")
        self.assertFalse(mod._pid_exists(pid))

    def test_fatal_stderr_pattern_kills_the_run(self):
        mod = self.server("echo 'AI_APICallError: quota exceeded' >&2; sleep 300\n")
        run_id = self.run_id_from(self.dispatch(mod))
        self.assertTrue(self.wait_until(
            lambda: mod._read_record(run_id)["state"] != "running"))
        record = mod._read_record(run_id)
        self.assertEqual(record["verdict"], "fatal")
        self.assertIn("quota exceeded", record["fatal_line"])

    def test_fatal_line_split_across_two_scans_is_still_caught(self):
        """The scan consumes only up to the last newline, so a message still
        being written cannot be half-read by one pass and skipped by the next."""
        mod = self.server()
        record = {
            "run_id": "x", "stderr_path": os.path.join(self.tmp, "e"),
            "scan_offset": 0, "fatal_line": None,
        }
        with open(record["stderr_path"], "wb") as fh:
            fh.write(b"fine\nusage limit ")          # no trailing newline
        mod._scan_fatal(record)
        self.assertIsNone(record["fatal_line"])
        self.assertEqual(record["scan_offset"], 5)   # only "fine\n" consumed
        with open(record["stderr_path"], "ab") as fh:
            fh.write(b"reached now\n")
        mod._scan_fatal(record)
        self.assertEqual(record["fatal_line"], "usage limit reached now")

    def test_recycled_pid_is_never_signalled(self):
        """A stale record must not be able to kill an unrelated process."""
        mod = self.server("sleep 300\n")
        run_id = self.run_id_from(self.dispatch(mod))
        record = mod._read_record(run_id)
        victim = subprocess.Popen(["sleep", "300"])
        self.addCleanup(victim.kill)

        # Exactly the stale-record case: the pid now names something else.
        record["pid"] = victim.pid
        record["pgid"] = victim.pid
        record["identity"] = "Thu Jan  1 00:00:00 1970"
        mod._live.pop(run_id, None)
        time.sleep(1.2)
        mod._write_record(record)

        after = mod._reconcile(run_id)
        self.assertEqual(after["verdict"], "pid-recycled")
        self.assertEqual(after["state"], "lost")
        self.assertIsNone(victim.poll(), "an unrelated process was killed")

    def test_second_concurrent_dispatch_is_refused_unless_forced(self):
        mod = self.server("sleep 30\n")
        first_id = self.run_id_from(self.dispatch(mod))
        refused = self.dispatch(mod)
        self.assertIn("Refusing to dispatch", refused)
        self.assertIn(first_id, refused)

        forced = self.dispatch(mod, force=True)
        second_id = self.run_id_from(forced)
        self.assertNotEqual(first_id, second_id)
        mod.cancel_run(first_id)
        mod.cancel_run(second_id)

    def test_wait_seconds_returns_the_finished_result_inline(self):
        mod = self.server("echo done-quickly\n")
        out = self.dispatch(mod, wait_seconds=15)
        self.assertIn("done-quickly", out)
        self.assertIn("exited 0", out)
        self.assertNotIn("run id:", out)

    def test_wait_seconds_expiring_leaves_the_run_going(self):
        mod = self.server("sleep 30\n")
        out = self.dispatch(mod, wait_seconds=2)
        self.assertIn("Still running after 2s", out)
        live = mod._live_runs()
        self.assertEqual(len(live), 1)
        mod.cancel_run(live[0]["run_id"])

    def test_artifacts_written_by_the_delegate_are_reported(self):
        mod = self.server("mkdir -p .agent-runs && echo report > .agent-runs/out.md\n")
        out = self.dispatch(mod, wait_seconds=15)
        self.assertIn(".agent-runs/out.md", out)

    def test_prune_removes_finished_runs_past_retention(self):
        mod = self.server("exit 0\n", AGENT_MCP_RUN_RETENTION_DAYS="1")
        run_id = self.run_id_from(self.dispatch(mod))
        self.assertTrue(self.wait_until(
            lambda: mod._read_record(run_id)["state"] != "running"))
        record = mod._read_record(run_id)
        record["ended_at"] = time.time() - 3 * 86400
        mod._write_record(record)

        mod._prune()
        self.assertIsNone(mod._read_record(run_id))
        self.assertFalse(os.path.exists(record["stdout_path"]))

    def test_prune_never_removes_a_live_run(self):
        mod = self.server("sleep 30\n", AGENT_MCP_RUN_RETENTION_DAYS="1")
        run_id = self.run_id_from(self.dispatch(mod))
        record = mod._read_record(run_id)
        record["started_at"] = time.time() - 9 * 86400
        mod._write_record(record)
        mod._prune()
        self.assertIsNotNone(mod._read_record(run_id))
        mod.cancel_run(run_id)

    def test_missing_cli_reports_clearly_and_leaves_no_record(self):
        mod = self.server()
        setattr(mod, self.BIN_VAR, "/nonexistent/cli")   # the argv is built from this
        out = self.dispatch(mod)
        self.assertIn("not found", out)
        self.assertEqual(mod._record_ids(), [])

    def test_check_and_cancel_on_an_unknown_id_are_not_errors(self):
        mod = self.server()
        self.assertIn("No run", mod.check_run("nope"))
        self.assertIn("No run", mod.cancel_run("nope"))

    def test_record_write_is_atomic(self):
        """A reader must never see a half-written record."""
        mod = self.server()
        record = {"run_id": "atomic", "state": "running", "started_at": time.time()}
        mod._write_record(record)
        path = mod._run_paths("atomic")[0]
        self.assertTrue(os.path.exists(path))
        self.assertEqual(json.load(open(path))["run_id"], "atomic")
        leftovers = [f for f in os.listdir(mod.RUN_DIR) if ".tmp" in f]
        self.assertEqual(leftovers, [], "temp file was left behind")


class OpenCodeRunStoreTests(RunStoreTests):
    """The two servers are self-contained duplicates by design, so the store
    has to be verified in both - a fix applied to one and not the other is the
    exact failure this suite exists to catch."""
    SERVER = "opencode_mcp_server.py"
    BIN_VAR = "OPENCODE_BIN"
    TIMEOUT_VAR = "OPENCODE_MCP_TIMEOUT"
    IDLE_VAR = "OPENCODE_MCP_IDLE_TIMEOUT"


if __name__ == "__main__":
    unittest.main(verbosity=2)
