---
id: ADM-13
title: Adopt the unified workflow
status: To Do
assignee: []
created_date: '2026-11-16 23:37'
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
