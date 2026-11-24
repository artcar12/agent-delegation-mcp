---
id: ADM-12
title: Report agy quota from the agy wrapper
status: Done
assignee: []
created_date: 2026-11-10 15:16
updated_date: '2026-11-24T17:24:00Z'
labels: []
dependencies: []
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The coordinator has no way to see agy quota, so it cannot pick a model or pace dispatches by what is left. The CLI can report it: `agy -p "/usage" --output-format json` returns in under a second, costs no tokens (num_turns 0), and carries the data under `command.data.groups[]`, each with `name`, `description` and `buckets[]` of `id`, `name`, `window` (weekly or 5h), `remaining_fraction` (0 to 1) and `reset_time` (ISO UTC). Seen on 2026-11-10 on a Pro plan: groups "Gemini Models" (gemini-weekly, gemini-5h), "Claude and GPT models" (3p-weekly, 3p-5h) and "Gemini 4 Argon" (gemini-argon-wtu-weekly, a separate weekly pool). The same rows are also in `response` as tab separated text.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A wrapper tool (for example agy_usage, or a section of delegation_status) runs the usage command and returns each bucket with its group, window, percent remaining and reset time
- [x] #2 A failed or unparseable usage call is reported as unknown and never blocks a dispatch
- [x] #3 Tests cover parsing the JSON shape above, including a group with a single bucket
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Found while coordinating block-walk on 2026-11-10: Argon was at 38 percent weekly and the coordinator only knew from screenshots. Check whether opencode has an equivalent for the opencode wrapper.

2026-11-16: ADM-15 adds quota_status (shared quota_sources.py) to every wrapper, covering AC #1 (buckets with group, window, percent and reset), #4 (fails soft as unavailable, never blocks) and #5 (tests incl. a single-bucket group). Still open: #2 (dispatch_agy surfacing the chosen pool's remaining quota) and #3 (instructions naming which models share which pool; they now only point at quota_status).

2026-11-24: former AC #2 (dispatch surfaces the target pool quota or warns) and #3 (instructions name which models share which pool) moved to ADM-19, which covers all three wrappers. Remaining ACs verified: quota_status returned all agy buckets live on 2026-11-24; test/test_quota_sources.py covers parsing and fail-soft.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Delivered by ADM-15: quota_status in every wrapper reads agy /usage (group, window, percent, reset), fails soft as unavailable, with parser tests. The dispatch-time part moved to ADM-19.
<!-- SECTION:FINAL_SUMMARY:END -->
