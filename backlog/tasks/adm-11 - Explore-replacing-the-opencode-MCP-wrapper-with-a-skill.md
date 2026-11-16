---
id: ADM-11
title: Explore replacing the opencode MCP wrapper with a skill
status: To Do
assignee: []
created_date: '2026-11-10 14:57'
labels: []
dependencies: []
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
In a session where Claude was asked to call Codex it did so correctly through plain Bash, with no MCP server and no skill. That undercuts the assumption that opencode needs opencode_mcp_server.py. The wrapper adds real behavior (async run ids, check_run/cancel_run/list_runs, concurrent-dispatch refusal over shared provider quota, idle heartbeat, grep of --print-logs for stream/quota errors, PWD and flag handling that changed in OpenCode 2.x per ADM-9, .agent-runs output convention), so the open question is which of that a skill can carry and what is lost. The 2.x argv and env quirks in particular would have to live in skill text or a helper script. Related: the agy equivalent is tracked as a sibling task, and the two should reach the same conclusion unless there is a reason they should not.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Write down what opencode_mcp_server.py does that a bare `opencode run` call via Bash does not, and for each item say whether a skill (with or without a helper script) can cover it
- [ ] #2 Prototype an opencode skill and run the cases ADM-9 verified on 2.x: write-a-file, leading-dash prompt, quota error, cancel cleanup, idle heartbeat
- [ ] #3 Record a recommendation (replace, keep both, or keep MCP) with the evidence, as a decision under backlog/decisions, consistent with the agy task or explaining the difference
- [ ] #4 If the recommendation is to replace, list the follow-up tasks needed; do not remove the MCP server in this task
<!-- AC:END -->
