---
id: ADM-19
title: 'Dispatch guard: check the target pool before dispatching'
status: Done
assignee:
  - '@claude'
created_date: '2026-11-24T17:24:00Z'
updated_date: '2026-11-24T17:54:00Z'
labels: []
dependencies:
  - ADM-18
priority: high
type: feature
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Quota checks are advice the coordinator has to remember (quota-check skill, working-standard routing rules), and it often does not. dispatch_agy, dispatch_opencode and dispatch_codex should look at the pool the chosen model draws on, using the live headless reading where one exists and the limit ledger, so an exhausted pool is refused before a run is started and the remaining quota is visible in every dispatch result. Takes over ADM-12 AC #2 and #3.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Each dispatch result includes a one-line quota note for the target pool: percent left, window, reset, and the reading age for Codex
- [x] #2 A dispatch is refused, naming the pool, the reason and the reset time, when the live reading shows the pool at 0% left or the ledger holds an active hit for it; force=True dispatches anyway
- [x] #3 A dispatch below a low-quota threshold (configurable per server env) proceeds with a warning in its result
- [x] #4 A quota reading that fails or times out never blocks a dispatch and adds no more than a few seconds; only the target CLI is read
- [x] #5 Tool docstrings and server instructions name which models share which pool
- [x] #6 The same behaviour is in all three servers, and tests cover refuse, warn, force and unreadable quota for each
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Quota check runs before the one-at-a-time busy check so the race window stays milliseconds. Live check measured on 2026-11-24: agy /usage 1.5-2s, Codex snapshot ~0s, opencode ledger only. Known, left as is: Codex treated as one pool (limit_id/individual_limit and has_credits ignored, force=True overrides); Argon group absent from this account's /usage today, so Argon dispatches read quota unknown and are never refused on a live figure.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Every dispatch_* reads the target pool (agy /usage group, Codex snapshot, limit ledger) and leads its result with a Quota line; refuses an exhausted pool or one with an active hit, naming the reason and reset, unless force=True; warns under AGENT_MCP_QUOTA_WARN_PERCENT; never blocks on an unreadable reading (AGENT_MCP_QUOTA_CHECK_TIMEOUT, default 5s). Docstrings and server instructions name the pools. Version 1.13.0.
<!-- SECTION:FINAL_SUMMARY:END -->
