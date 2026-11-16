---
id: ADM-12
title: Report agy quota from the agy wrapper
status: To Do
assignee: []
created_date: '2026-11-10 15:16'
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
- [ ] #1 A wrapper tool (for example agy_usage, or a section of delegation_status) runs the usage command and returns each bucket with its group, window, percent remaining and reset time
- [ ] #2 dispatch_agy includes the remaining quota for the chosen model pool in its result, or warns when that pool is below a threshold
- [ ] #3 The server instructions tell the caller to check usage before dispatching and name the pools that models share
- [ ] #4 A failed or unparseable usage call is reported as unknown and never blocks a dispatch
- [ ] #5 Tests cover parsing the JSON shape above, including a group with a single bucket
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Found while coordinating block-walk on 2026-11-10: Argon was at 38 percent weekly and the coordinator only knew from screenshots. Check whether opencode has an equivalent for the opencode wrapper.
<!-- SECTION:NOTES:END -->
