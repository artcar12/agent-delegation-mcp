---
id: ADM-9
title: Support OpenCode 2.0 in the opencode wrapper
status: To Do
assignee: []
created_date: '2026-10-23 18:12'
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
- [ ] #1 Read the 2.0 migration doc and list what touches opencode_mcp_server.py (argv, log parsing, provider error detection, model ids)
- [ ] #2 Upgrade opencode on this Mac to 2.0 and record the exact install command in the README
- [ ] #3 dispatch_opencode works on 2.0: a write-a-file probe and a forced provider-error run are both detected correctly
- [ ] #4 The docstring, README and MODEL-ROSTER.md say which opencode major the wrapper supports, and test/ passes
<!-- AC:END -->
