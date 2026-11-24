---
id: ADM-18
title: 'Limit ledger: remember usage-limit hits across runs and sessions'
status: To Do
assignee: []
created_date: '2026-11-24T17:24:00Z'
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
