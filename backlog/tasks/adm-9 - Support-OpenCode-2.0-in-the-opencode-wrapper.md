---
id: ADM-9
title: Support OpenCode 2.0 in the opencode wrapper
status: Done
assignee: []
created_date: '2026-10-23 18:12'
updated_date: '2026-10-29 15:48'
labels: []
dependencies: []
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
OpenCode 2.0 is out (announced at https://anoma.ly/notes/opencode-2-0, migration doc https://opencode.ai/v2/docs/migrate-v1/). Its TUI now shows an Update available prompt. Listed breaking changes: 1.x plugins do not work, the server API is completely different, and the TUI config (tui.json) moved; everything else is said to be backwards compatible. The wrapper still works on 1.x (1.19.4, npm global opencode-ai under nvm Node 24.15.0, upgraded 2026-10-23). opencode upgrade refuses there because the install is npm-managed; the 2.0 path is opencode upgrade --major from the latest 1.x, or npm install -g opencode-ai@2 when that is how it ships. Not yet known: whether opencode run still accepts --auto, --agent build, --print-logs, --dir and --model unchanged, whether the stream-error lines the wrapper greps for in --print-logs output kept their format, whether auth.json (opencode-go api key) and opencode.json (default_agent: plan) carry over, and whether any opencode plugins in ~/.config/opencode need rebuilding.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Read the 2.0 migration doc and list what touches opencode_mcp_server.py (argv, log parsing, provider error detection, model ids)
- [x] #2 Upgrade opencode on this Mac to 2.0 and record the exact install command in the README
- [x] #3 dispatch_opencode works on 2.0: a write-a-file probe and a forced provider-error run are both detected correctly
- [x] #4 The docstring, README and MODEL-ROSTER.md say which opencode major the wrapper supports, and test/ passes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Read the v2 migration doc and probe opencode run --help for what touches the wrapper.
2. argv: drop --dir (2.x rejects it) and add --standalone, so server logs reach stderr and the session's server is a per-run child rather than the shared background service.
3. Set PWD=<cwd> in the child env alongside Popen(cwd=...), because 2.x resolves the project from $PWD.
4. Fail-fast: add 'usage limit exceeded' (the 2.x quota wording). Have the report quote the latched provider line when the CLI exits non-zero on its own, which 2.x does instead of hanging.
5. Tests: argv shape and cwd, PWD handed to Popen, quota exit reports the provider line.
6. End-to-end against real 2.2.1 through dispatch_opencode with free models: write-a-file, leading-dash prompt, quota error, cancel cleanup, idle heartbeat.
7. Docs: docstring, README (prereqs and install command, section 4 flags table, failure modes, release notes, verified-against), MODEL-ROSTER.md.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-10-29: Installed 2.2.1 with npm i -g @opencode/cli@2.2.1 under nvm Node 24.15.0 after npm uninstall -g opencode-ai (1.19.4). /usr/local/bin/opencode still resolves through the nvm bin symlink. The package also installs an opencode2 bin. Backed up ~/.config/opencode (without node_modules), auth.json and opencode.db to ~/opencode-v1-backup-20261029 before the first v2 run.
Findings: v2 runs a background service by default (opencode service start/stop/status), and opencode models started it on 127.0.0.1. opencode run no longer has --dir: the wrapper's exact v1 argv exits 1 with 'Unrecognized flag: --dir in command opencode run', so dispatch_opencode is broken on v2 as shipped. --auto, --agent, --model and --print-logs still exist, but --print-logs only includes server logs with --standalone. A --standalone probe (cwd set to the target dir, free model opencode/glm-5.3-flash-free) logged 'spawning process' (opencode.exe serve --stdio --port 0) and 'cli starting', then printed nothing for about 5.5 minutes and wrote no file before I killed it. Cause unknown. A background-service probe was not run (blocked by a permission prompt). opencode-go quota is exhausted, so testing has to use free opencode/* models, and that quota state is a free provider-error test case.

2026-10-29 implementation and verification:
- Migration doc (opencode.ai/v2/docs/migrate-v1): what matters here is the background service vs standalone split, V1 plugins not running in 2.x, config keys renamed (normalized in memory, warnings only), external_directory allowed by default, and OPENCODE_LOG_LEVEL replacing logLevel. The page does not cover run flags, so those came from opencode run --help on 2.2.1: --dir is gone; --standalone and --server are new; --auto, --agent, --model and --print-logs are unchanged; --print-logs carries server logs only with --standalone.
- The earlier standalone 'hang' was the test harness's open stdin, not v2: with </dev/null it finished in about 6s. The wrapper already passes stdin=DEVNULL.
- Mutation: 2.x writes into $PWD, not the Popen cwd. A dispatch from a process whose PWD was the scratchpad wrote .agent-runs/probe.md there, exited 0 and reported success. Fixed by env PWD=cwd. After the fix the file lands in the target and nowhere else.
- Quota (opencode-go/glm-5.2 while out of quota): 2.x exits 1 in 3-6s with 'Error: Go usage limit exceeded'. ERROR log line: 'Failed to drain Session' cause='AI.Error: Go usage limit exceeded'. No 1.x pattern matched it, so 'usage limit exceeded' was added. Two runs: one latched it and was killed (verdict fatal), the other exited 1 with fatal_line latched. Both reports now quote the line.
- Leading-dash prompt: works after --.
- cancel_run: the private serve --stdio and the playwright MCP node process each take their OWN process group (not the run's pgid), but they talk over the run's stdio and were all gone 3s after cancel. No stray serve --stdio.
- Heartbeat: an 85s, 1,535-word free-model run had a largest stderr gap of 15s (149 log lines), far under IDLE_SECONDS=600.
- Model ids: all opencode-go ids named in the docstring are listed on 2.2.1. Free ids mimo-v2.5-free and ling-3.0-flash-fin-free are gone; glm-5.3-flash-free passes the probe.
- test/test_run_store.py: 47 tests OK (3 new).
- Not covered: a dispatch through the live MCP transport. The session's server holds the old code until /mcp reconnect. The e2e drove the same dispatch_opencode function against the real CLI.
- Seen in ~/.config/opencode, outside this task: the gemini-web MCP entry points at the removed gemini_web_mcp_server.py and fails on every run; 'model': 'vertex:gemini-2.5-flash' is skipped as malformed by 2.x; package.json still pins @opencode-ai/plugin 1.15.7 (1.x plugins do not run in 2.x).

Released as plugin 1.10.0 (2026-10-29).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
dispatch_opencode now targets OpenCode 2.x: argv drops --dir (rejected by 2.x) and adds --standalone; the child gets PWD=<cwd> because 2.x resolves the project from $PWD; the 2.x quota wording is a fail-fast pattern and the report quotes it when the CLI exits on its own. Verified with 3 new unit tests (47 OK) and end-to-end runs of dispatch_opencode against opencode 2.2.1 on free models: write-a-file, dash prompt, quota error, cancel cleanup, 15s max heartbeat gap. 1.x is no longer supported. Install: npm i -g @opencode/cli.
<!-- SECTION:FINAL_SUMMARY:END -->
