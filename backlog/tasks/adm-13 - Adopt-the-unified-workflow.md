---
id: ADM-13
title: Adopt the unified workflow
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-16 23:37'
updated_date: '2026-11-17 03:24'
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
- [ ] #1 CLAUDE.md, AGENTS.md, .gitignore and backlog config match the template
- [ ] #2 Any spec or plan is Backlog doc-1 and the file is gone; ADRs are decisions; no handoff file
- [ ] #3 No load-bearing project rules left in Claude memory on either machine
- [ ] #4 Gate green, merged and pushed
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Backlog config already matches (prefix adm, statuses, remote_operations, check_active_branches); refresh the Backlog block.
2. CLAUDE.md: Backlog block + working-standard block + project rules (gate, release/version rules, wrapper rules).
3. AGENTS.md one-line pointer; .gitignore template lines (+ .codex/); .claude/worktrees/ out of .git/info/exclude.
4. Knowledge: no spec/plan/ADR/handoff files exist; Mac memory dir empty. Latitude memory check left pending.
5. Main checkout: untracked AGENTS.md (Backlog block only) removed so the merge can land; .codex/config.toml (project-scoped Codex MCP config) kept and gitignored.
6. Gate, fresh-context review, merge --no-ff, push.
<!-- SECTION:PLAN:END -->
