---
id: ADM-17
title: >-
  Fail fast on silent delegate runs: real liveness signal and idle limits on by
  default
status: In Progress
assignee: []
created_date: '2026-11-17 00:38'
labels: []
dependencies: []
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
On 2026-11-16 a delegate on the second machine started a test in the background, the test hung, and agy sat until its 60 minute print timeout with nobody noticing: agy's idle check is off by default because its stdout has no verified heartbeat, and Codex's is off for the same reason. A run's own files and the CLI's per-run transcript are better evidence of life than stdout alone.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Liveness for agy and codex runs is the newest mtime among stdout, stderr, files under the run's .agent-runs/ and the CLI's own per-run transcript (agy brain transcript_full.jsonl, Codex session file); opencode uses stdout, stderr and .agent-runs/; mtimes only, never content
- [ ] #2 Idle limit on by default: agy 900 s, codex 900 s, opencode 600 s; overridable per dispatch and by env; evidence stays on disk so it survives a restart
- [ ] #3 check_run warns 'no activity for N s (idle limit M s)' once N passes half the limit
- [ ] #4 A silent-run test (idle limit 20 s) passes for each wrapper; tests use synthetic files only
- [ ] #5 README documents the signal, the defaults, and that parents wait with check_run rather than pgrep
<!-- AC:END -->
