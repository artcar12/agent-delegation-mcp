---
id: ADM-18
title: 'Limit ledger: remember usage-limit hits across runs and sessions'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-24T17:24:00Z'
updated_date: '2026-11-24T17:34:00Z'
labels: []
dependencies: []
priority: high
type: feature
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
When a delegate hits a provider usage limit, the wrapper fails the run fast (fatal stderr patterns, or a codex `error`/`turn.failed` event) and then forgets it. The next dispatch, from this session or any other, walks into the same wall. For opencode a recorded hit is the only signal there is: it exposes no remaining-quota figure headlessly, only the web console does. Codex numbers come from the last session snapshot and can be days old. A small on-disk ledger of hits, shared by all three servers like the run store, lets quota_status and dispatch know a pool is exhausted until it resets.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A run that ends on a usage-limit failure (any of the three CLIs, including codex quota errors reported only as stdout JSON events) adds a ledger entry with CLI, pool, hit time, the reset time when the message states one, and the run id; auth failures are not recorded as limit hits
- [ ] #2 The pool is derived from the run model: agy by model family (Gemini, Argon, Claude and GPT), Codex as one pool, opencode per model with known shared pools grouped
- [ ] #3 The ledger lives beside the run store, honours AGENT_MCP_RUN_DIR, is written atomically, and is never picked up as a run record or removed by pruning
- [ ] #4 An entry with a known reset stops counting once that time passes; one without a reset counts for a fixed default period, then is reported as a stale hit until it ages out
- [ ] #5 quota_status shows active ledger entries per source, in both table and JSON output, as a compatible schema 1 addition
- [ ] #6 The ledger stores no prompt, transcript or output text beyond the single matched error line
- [ ] #7 Tests cover recording from each CLI, reset parsing, expiry, a corrupt or missing ledger, and isolation from run listing and pruning
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. quota_sources.py: ledger at <run dir>/quota/limits.json (subdir, so _record_ids and _prune never see it), flock-guarded read-modify-write with os.replace; pool_for(cli, model); is_limit_line (usage limit, quota exceeded, insufficient credit, rate limit, resource exhausted; never auth); parse_reset (ISO time, "in 2 hours 5 minutes", "in 2h 5m", "at 3:05 PM"); entries keyed by cli+pool, latest hit wins.
2. Expiry: known reset -> active until then; unknown -> active for AGENT_MCP_LIMIT_TTL (default 3600s), then stale until 24h, then dropped. A later run of the same pool that exits 0 with no limit error clears an older entry.
3. Servers (all three, identical): _note_limit(record) once per run when it leaves running, using fatal_line or, for codex, the stdout error event; latched with record["limit_noted"]. Fails soft when quota_sources is missing.
4. quota_status: extra.limit_hits per source (schema 1 compatible), table note shows active and stale hits.
5. Tests in test_quota_sources.py and test_run_store.py; README quota section; version bump 1.13.0 with ADM-19.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-24: implemented on claude/ADM-18-limit-ledger. Full suite 163 tests OK (6 agy-only skips), plugin validate passes, real ~/.agent-delegation-mcp ledger untouched by tests. Merges together with ADM-19 as 1.13.0.
<!-- SECTION:NOTES:END -->
