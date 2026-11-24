---
id: ADM-22
title: 'quota_status: unstarted agy pool shows a 1970 reset as "reset since snapshot"'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-24T17:24:00Z'
updated_date: '2026-11-24T17:37:00Z'
labels: []
dependencies: []
priority: low
type: bug
ordinal: 21000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
agy /usage returns reset_time 1970-01-01T00:00:00Z for a pool whose window has not started (seen on 2026-11-24 for the Claude and GPT models pool, remaining_fraction 1). quota_sources treats that as a passed reset, so the table shows a 1970 reset date and the note "reset since snapshot". The percentages are right; the label and date are wrong.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A reset time at or before the Unix epoch (or missing) for an agy bucket is reported as window not started, with no reset date and no "reset since snapshot" note
- [ ] #2 A test covers the unstarted-pool shape
<!-- AC:END -->
