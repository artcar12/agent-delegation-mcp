---
id: ADM-13
title: Adopt the unified workflow
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-16 23:37'
updated_date: '2026-11-17 04:04'
labels: []
dependencies: []
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Bring this repo onto the unified workflow (claude-skills: skills/working-standard, docs/workflow-audit/decisions.md). Follow the migration steps in claude-skills templates/project/README.md: backlog config, CLAUDE.md as Backlog block + working-standard block + project-only rules, AGENTS.md pointer, .agent-runs/ and .claude/worktrees/ in .gitignore, spec/plan/ADRs/handoff files imported into Backlog (spec as doc-1), load-bearing Claude memory moved into Backlog or CLAUDE.md, repo-local git-sync hooks removed once the git-sync mod is on every machine.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 CLAUDE.md, AGENTS.md, .gitignore and backlog config match the template
- [x] #2 Any spec or plan is Backlog doc-1 and the file is gone; ADRs are decisions; no handoff file
- [ ] #3 No load-bearing project rules left in Claude memory on either machine
- [ ] #4 Gate green, merged and pushed
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Backlog config already matched the template (prefix adm, To Do / In Progress / Done, remote_operations and check_active_branches true); Backlog block already at the installed 1.51.0.
2. CLAUDE.md: Backlog block + working-standard block + project rules.
3. AGENTS.md one-line pointer; .gitignore template lines plus .codex/; .claude/worktrees/ removed from .git/info/exclude.
4. Knowledge: no spec/plan/ADR/handoff file existed; Mac memory dir empty. Latitude memory check pending.
5. Main checkout: untracked AGENTS.md (Backlog block only) removed so the merge lands; .codex/config.toml kept, now ignored.
6. Gate, fresh-context reviews, merge --no-ff, push.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
No spec, plan, ADR or handoff file existed in the repo (examples/example-build-spec.md and gemini-model-research-prompt.md are user-facing material), so there is no doc-1: CLAUDE.md names README.md as the design doc and scopes that to routine work.
Main checkout leftovers: untracked AGENTS.md held only the Backlog block (same as CLAUDE.md's), nothing to fold in; removed so the tracked pointer could merge. Untracked .codex/config.toml is a project-scoped Codex MCP config registering the agy and opencode wrappers via uv run --script (dated 2026-11-03); no wrapper code or test reads it (they use ~/.codex), kept in place and gitignored.
Mac memory dir ~/.claude/projects/-Users-arthurcarroll-Projects-agent-delegation-mcp/memory is empty.
Reviews: nine fresh-context subagent rounds on CLAUDE.md; findings were wording and accuracy of project rules (shared run store across working-tree and installed servers, release rule covering MODEL-ROSTER.md, README sections 5/6 being user advice). Round 9 fixed; no round 10 per Arthur.
Gate: python3 -m unittest discover -s test, 139 OK (6 skipped); claude plugin validate . passes with the two expected warnings.
PENDING (Latitude, AC #3): check the Latitude's Claude memory dir for this project (~/.claude/projects/*agent-delegation-mcp*/memory) for load-bearing rules; move any into CLAUDE.md or a backlog decision, delete the file, then check AC #3.
Follow-ups not done: README section 2 (around line 212) says the root .mcp.json is gone and servers always run from the installed copy; .mcp.json is tracked and loads the working-tree servers. agent-delegation--v1.12.0 tag not made (needs Arthur's go). Commits in this repo carry the work email although the repo is public (pre-existing, 71 of 73 commits on main).

README section 2 follow-up done on this branch at Arthur's request (7191355): it now says the root .mcp.json starts the working-tree servers next to the installed plugin and shares the run store.
<!-- SECTION:NOTES:END -->
