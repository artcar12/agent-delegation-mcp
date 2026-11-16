---
id: ADM-10
title: Explore replacing the agy MCP wrapper with a skill
status: To Do
assignee: []
created_date: '2026-11-10 14:57'
labels: []
dependencies: []
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
In a session where Claude was asked to call Codex it did so correctly through plain Bash, with no MCP server and no skill. That undercuts the assumption that agy needs agy_mcp_server.py. The wrapper adds real behavior (async run ids, check_run/cancel_run/list_runs, a refusal of concurrent dispatches because models share a provider quota pool, idle heartbeat, provider-error fail-fast, .agent-runs output convention), so the open question is which of that a skill (instructions plus maybe a small shell script) can carry and what is lost. A skill also costs no tool schemas in context and works in any client that reads skills, not just MCP hosts. gemini-ask already ships as a skill in skills/, so there is precedent.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Write down what agy_mcp_server.py does that a bare `agy` CLI call via Bash does not, and for each item say whether a skill (with or without a helper script) can cover it
- [ ] #2 Prototype an agy skill and run the same delegation cases the wrapper is tested with: write-a-file probe, forced provider error, cancel, long run that outlives a single tool call
- [ ] #3 Record a recommendation (replace, keep both, or keep MCP) with the evidence, as a decision under backlog/decisions
- [ ] #4 If the recommendation is to replace, list the follow-up tasks needed (README, plugin manifest, test/ changes); do not remove the MCP server in this task
<!-- AC:END -->
