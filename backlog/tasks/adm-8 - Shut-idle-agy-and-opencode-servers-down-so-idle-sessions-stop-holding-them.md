---
id: ADM-8
title: Shut idle MCP servers down so idle sessions stop holding them
status: Done
assignee:
  - '@claude'
created_date: '2026-10-08 02:03'
updated_date: '2026-10-08 02:17'
labels: []
dependencies: []
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Claude Code starts one copy of every plugin MCP server per session, and the server lives exactly as long as that session's claude process. The desktop app and `claude rc` keep finished sessions' processes alive for hours, so on 2026-10-07 four sessions were holding 12 Python servers (~70 MB each) that had not been called since morning. They were not OS orphans - every parent was alive - so stdin EOF never fires. Verified 2026-10-07 against Claude Code 2.1.29x: when a stdio server exits on its own, the next tool call in that session transparently starts a fresh one (new pid, no error), so exiting on idle costs one cold start, not lost tools.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 agy and opencode servers exit on their own after a configurable stretch with no tool calls (shared env var, 0 disables, sensible default)
- [x] #2 A server never idle-exits while a run it started is still live, since its monitor thread enforces that run's deadlines
- [x] #3 The knob is documented in the README env var list
- [x] #4 Unit tests cover the exit decision, and test/test_run_store.py passes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add AGENT_MCP_SERVER_IDLE_EXIT (seconds, default 1800, 0 = off) and a last-call timestamp to both servers.
2. Touch the timestamp at the top of every @mcp.tool function.
3. Daemon thread started in __main__ checks once a minute; skips while _live is non-empty; os._exit(0) once idle past the limit.
4. Factor the decision into a pure _should_idle_exit(now) so it is testable without a process.
5. Tests + README.
6. gemini-web server left out of scope (user asked for agy and opencode).

7. Scope extended at user request: gemini-web gets the same idle exit, guarded by _browser_lock.locked() instead of _live (no run store there; a gemini_ask can outlast the limit).
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implemented as planned, plus a self-exit line in delegation_status so a session can see the setting. Wall clock (time.time) rather than monotonic: macOS monotonic stops during sleep, and an overnight session is the main case. Tests: 4 new cases per server (8 total); python3 test/test_run_store.py -> 44 OK. End-to-end: patched agy server under claude -p stream-json with AGENT_MCP_SERVER_IDLE_EXIT=5 - call, 15s idle, server pids gone, second call answered by new pids with no error. Not released: plugin.json is still 1.7.0, so installed sessions run the old code until a version bump and plugin update.

gemini-web added: same AGENT_MCP_SERVER_IDLE_EXIT, never exits while the browser lock is held. 3 GeminiIdleExitTests; test_run_store.py 47 OK, test_gemini_web_worker.py 79 OK. Live: limit 30s, gemini_conversations call, server alive right after, gone after 60s idle, second call answered by new pids. Released as 1.8.0.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
All three servers (agy, opencode, gemini-web) exit after AGENT_MCP_SERVER_IDLE_EXIT seconds (default 1800, 0 = off) without a tool call; agy/opencode never while a run they started is live, gemini-web never mid-call. Claude Code restarts them on the session's next call. Documented in the README env table, 1.8.0 release note and delegation_status. Verified by 11 new unit tests (47/47 and 79/79 OK) and live claude -p sessions for agy and gemini-web showing exit and transparent respawn. Released as 1.8.0.
<!-- SECTION:FINAL_SUMMARY:END -->
