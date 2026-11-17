---
id: ADM-16
title: 'dispatch_codex: a third delegation wrapper for the Codex CLI'
status: In Progress
assignee: []
created_date: '2026-11-17 00:20'
updated_date: '2026-11-17 00:20'
labels: []
dependencies: []
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Codex is a judgment-capable delegate with its own quota pool, but there is no wrapper for it, so routing to it means hand-running codex exec without run records, deadlines or the one-at-a-time guard the agy and opencode wrappers enforce.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 codex_mcp_server.py exposes dispatch_codex, check_run, cancel_run, list_runs, delegation_status and quota_status with the same run store, deadlines and concurrency guard as the other wrappers
- [ ] #2 Runs codex exec unattended: workspace-write sandbox, approval never, confined to cwd plus its git dir so commits work
- [ ] #3 The codex binary is found via CODEX_BIN, PATH, or the ChatGPT.app bundle on macOS
- [ ] #4 Registered in plugin.json, .mcp.json and documented in README and MODEL-ROSTER.md routing
- [ ] #5 Existing and new tests pass
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. codex_mcp_server.py cloned from the agy server (same run store), argv: codex exec --json -s workspace-write -c approval_policy=never -C cwd --add-dir <git dirs> --color never -o <run>.last [-m model] -- prompt.
2. Verified 2026-11-16 on codex-cli 0.167.0-alpha.4.1: plain mode writes the transcript to stderr (unsafe for fail-fast patterns); --json leaves stderr clean; workspace-write makes .git read-only unless --add-dir names it. No --ephemeral, so runs keep refreshing the rate_limits snapshot quota_status reads.
3. Report shows the -o last message and any error event from stdout.
4. Register in plugin.json and .mcp.json; tests extended to load the third server; docs.
<!-- SECTION:PLAN:END -->
