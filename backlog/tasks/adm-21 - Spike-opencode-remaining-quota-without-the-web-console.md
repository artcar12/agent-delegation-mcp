---
id: ADM-21
title: 'Spike: opencode remaining quota without the web console'
status: To Do
assignee: []
created_date: '2026-11-24T17:24:00Z'
labels: []
dependencies: []
type: spike
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
opencode exposes no remaining-quota percentage headlessly: quota_status can only show usage totals from opencode stats and the time of the last usage-limit line in the log. The workspace console page shows rolling, weekly and monthly percent used per model. Find out whether that data can be read without driving a browser.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Records whether the opencode CLI (2.x) has any command or flag that reports remaining quota, with exact version
- [ ] #2 Records whether the console page loads its figures from an endpoint that the CLI credentials can read, without using any banked reset, credit or purchase flow
- [ ] #3 Ends in a recommendation for quota_sources, or a note that the ledger and the console stay the only sources
<!-- AC:END -->
