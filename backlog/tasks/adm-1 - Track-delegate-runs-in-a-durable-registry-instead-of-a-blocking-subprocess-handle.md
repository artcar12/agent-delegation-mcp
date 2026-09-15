---
id: ADM-1
title: >-
  Track delegate runs in a durable registry instead of a blocking subprocess
  handle
status: Done
assignee:
  - '@arthur'
created_date: '2026-09-15 19:23'
updated_date: '2026-09-15 19:47'
labels: []
dependencies: []
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A dispatch currently lives only as an in-memory Popen handle inside a blocking tool call. When the MCP server dies for any reason the client did not initiate cleanly - transport drop, plugin reload, another session tearing MCP down, SIGKILL - the delegate keeps running in its own session (start_new_session) with nothing left that can find it, report on it, or stop it. README 5.1 documents this as an unrecoverable 'Connection closed' case and tells the operator to pgrep by hand.

The delegate outliving the server is often the RIGHT outcome: killing an hour-long run because the transport blipped is worse than letting it finish. The defect is not that the process survives, it is that nothing tracks it. A run recorded on disk with its pid, pgid, argv, cwd and output paths is recoverable by any later session, including one started after a full restart; an in-memory handle is not.

Both server files are self-contained by design (each is a complete 'uv run --script' target, importing nothing from its sibling), so the registry is duplicated into both rather than factored into a shared module.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A dispatch returns a run id without blocking, and the delegate's stdout and stderr are written to files that survive the server exiting
- [x] #2 A run started by one server process can be inspected, tailed and cancelled by a different server process, including after a restart
- [x] #3 Wall-clock, idle and fatal-stderr-pattern verdicts are still enforced, and are enforced lazily on inspection so a run adopted after a restart still gets its deadline
- [x] #4 Cancelling or reconciling a run verifies process identity before signalling, so a reused pid is never killed
- [x] #5 A second concurrent dispatch to the same CLI is refused by default, naming the live run, with an explicit override
- [x] #6 Finished run records and their output files are pruned after a retention period
- [x] #7 README and the connect-time instructions describe the new tools, and no longer tell the operator to pgrep by hand
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add a run store: records at $AGENT_MCP_RUN_DIR (default ~/.agent-delegation-mcp/runs) as <run-id>.json, with <run-id>.out and <run-id>.err beside them. Global rather than per-project so list_runs finds a run dispatched into another cwd, and because pid/pgid are machine-global anyway. Deliverables still go to the project's .agent-runs/.
2. Replace the PIPE + reader-thread plumbing with direct file redirection. The child writes to the fd itself, so output needs no live parent and survives the server by construction. Drops _reader, the deques and MAX_OUTPUT_LINES/MAX_LINE_CHARS; keeps _truncate for bounding the returned tail.
3. Record process identity at spawn via 'ps -p <pid> -o lstart=' (fork time, unchanged by exec). Every later signal path requires an exact match, so a reused pid is marked lost rather than killed.
4. Write records atomically (tmp + os.replace) under a module lock. Cross-process races between two servers can lose an update but cannot corrupt a record.
5. One _reconcile(record) is the single enforcement path, called by both the per-run monitor thread (while the server lives) and check_run/list_runs (lazily, after a restart). Idle is measured from max(mtime) of the two output files; fatal patterns are scanned incrementally from a persisted byte offset so a restart rescans from 0 once and then advances.
6. Tools: dispatch_<cli>(prompt, model, cwd, wait_seconds=0, force=False), check_run(run_id, tail_lines), cancel_run(run_id), list_runs(all_projects, include_finished, limit). wait_seconds>0 blocks up to that long and returns the finished result inline, so short tasks still take one round trip. Concurrency refusal uses the registry, which is the first time that documented rule is enforceable.
7. Reconcile the whole store at startup, and prune finished records older than AGENT_MCP_RUN_RETENTION_DAYS (default 7).
8. Mirror the whole block into both servers - they are self-contained on purpose - then update _instructions(), delegation_status(), README 5.1 and the plugin description.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Two defects surfaced during testing that the original design did not anticipate, both now fixed and covered:

1. Lost verdict under concurrent reconcile. Two reconcilers can look at one record - this server's monitor and another session's check_run. The second would see a process that had just been killed, conclude it had simply exited, and overwrite the real verdict with a bare 'exited'. _terminate now publishes state='stopping' with the verdict BEFORE signalling; killing is the one transition whose reason is only knowable beforehand. A reconciler that finds an abandoned 'stopping' record finalises it rather than reinterpreting it.

2. Zombies read as alive. Liveness was os.kill(pid, 0), which succeeds against a process that has exited and is merely waiting to be reaped. For a run whose owner is alive but no longer holds the Popen handle - exactly the adopted case - a finished delegate would have read as running forever. Liveness now comes from 'ps -o state=,lstart=', one call answering both halves: state rules out a zombie, start time rules out a recycled pid. _reconcile also reaps its own child after a kill rather than leaving the corpse it just made.

Performance note: the hot path never shells out. While a server owns a run it uses proc.poll(); ps is only reached on the adopted path and the kill path.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
A dispatch now returns a run id in milliseconds and records the run on disk (pid, pgid, argv, cwd, output paths) under AGENT_MCP_RUN_DIR, with the delegate's streams redirected straight to files rather than through a pipe the server must stay alive to drain. check_run, cancel_run and list_runs operate on those records from any session, including one started after a restart, and both servers share the store so one list_runs accounts for every delegate on the machine. The delegate still outlives its server - that was always correct - but it is no longer unreachable while it does.

_reconcile is the single enforcement path, called by the owning server's monitor thread once a second and by check_run/list_runs when nobody is watching, so wall-clock, idle and fatal-stderr verdicts still apply to a run whose server died. Deadlines are read from the record, not the current environment, so they travel with the run. Process identity (fork time from ps) is verified before any signal, so a recycled pid is marked lost rather than killed. The documented 'one dispatch at a time' rule is enforced for the first time, with force=True as the override.

Verified by test/test_run_store.py: 36 tests, all passing, run against BOTH servers (the suite subclasses to cover the opencode wrapper, since the two files are self-contained duplicates by design and a fix applied to one and not the other is the exact regression to catch). It loads each server twice under different module names to model a run started by one server process and inspected by a later one. Covered: non-blocking dispatch, output surviving the originating server, exit code present for an owned child and absent for an adopted one, lazy wall-clock enforcement by a later server, idle detection from output mtime, fatal-pattern kill, a fatal line split across two scans, recycled-pid refusal, concurrency refusal and override, wait_seconds both ways, artifact reporting, retention pruning, and atomic record writes.

Also verified outside the unit tests: both servers complete a real MCP initialize/tools/list/tools/call handshake over stdio under 'uv run --script'; and an end-to-end run where the MCP server is SIGKILLed mid-dispatch, a fresh server finds the delegate via list_runs, reads its captured output and .agent-runs/ artifact via check_run, and cancel_run terminates the process group (confirmed gone via ps state, not just ps exit code).

Docs: README section 4 gains a tool table and a rewritten dispatch/reconcile diagram pair, 5.1 no longer tells the operator to pgrep by hand, the config table documents AGENT_MCP_RUN_DIR and AGENT_MCP_RUN_RETENTION_DAYS, and the failure-mode table gains rows for leftover delegates and pid-recycled runs. Tool names changed ask_* -> dispatch_* to make the non-blocking contract unmissable; plugin bumped to 1.1.0 and 'claude plugin validate .' passes.
<!-- SECTION:FINAL_SUMMARY:END -->
