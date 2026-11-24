---
id: ADM-20
title: 'Spike: read Codex rate limits live without spending a turn'
status: To Do
assignee: []
created_date: '2026-11-24T17:24:00Z'
labels: []
dependencies: []
type: spike
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
quota_status reads Codex limits from the newest session file, which is only as fresh as the last Codex run (3 days old on 2026-11-24). The Codex TUI /status screen and its status line items (five-hour-limit, weekly-limit) show live figures. On 2026-11-24 a test turn showed the snapshot is written as `rate_limits` on `token_count` events, which come from a turn that reached the API (that test turn used about 25k input tokens), so it is not free. Find out whether there is a zero-cost way to get a fresh reading.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Records whether opening the TUI and running /status without a prompt writes a fresh rate_limits line to a session file
- [ ] #2 Records whether any headless codex command or app-server request returns the limits without a model turn, with exact command and version
- [ ] #3 Ends in a recommendation for quota_sources, or a note that the snapshot stays the only headless source
<!-- AC:END -->
