---
id: ADM-17
title: >-
  Fail fast on silent delegate runs: real liveness signal and idle limits on by
  default
status: Done
assignee: []
created_date: '2026-11-17 00:38'
updated_date: '2026-11-17 01:50'
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
- [x] #1 Liveness for agy and codex runs is the newest mtime among stdout, stderr, files under the run's .agent-runs/ and the CLI's own per-run transcript (agy brain transcript_full.jsonl, Codex session file); opencode uses stdout, stderr and .agent-runs/; mtimes only, never content
- [x] #2 Idle limit on by default: agy 900 s, codex 900 s, opencode 600 s; overridable per dispatch and by env; evidence stays on disk so it survives a restart
- [x] #3 check_run warns 'no activity for N s (idle limit M s)' once N passes half the limit
- [x] #4 A silent-run test (idle limit 20 s) passes for each wrapper; tests use synthetic files only
- [x] #5 README documents the signal, the defaults, and that parents wait with check_run rather than pgrep
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Liveness = newest mtime of stdout, stderr, shallow .agent-runs/, and the run's CLI transcript (agy: conversations created since a dispatch-time snapshot of the brain dir; codex: session file by thread id). Detectors, transcript root and fail-fast patterns follow the run's CLI; only the live dispatching server scans stderr. Idle defaults agy/codex 900, opencode 600; STALL line past half.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Checked on agy 1.5.9: transcript_full.jsonl mtime moves on every step (still only during a single shell command). Live: an agy run with empty stdout for 31 s survived a 12 s idle limit on the transcript alone. opencode.log deliberately not used: shared by every opencode process, and --print-logs already mirrors this run's lines onto stderr. Six fresh-context review rounds; last found no production defects.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Idle limit on by default for all three wrappers with a real heartbeat, a STALL warning in check_run at half the limit, and every server judging a run by its own CLI. Verified live against agy and by 119 run-store tests including a 20 s silent-run test per wrapper.
<!-- SECTION:FINAL_SUMMARY:END -->
