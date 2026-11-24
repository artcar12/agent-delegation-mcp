---
id: ADM-21
title: 'opencode quota: headless remaining figure, free-model pooling, roster refresh'
status: To Do
assignee: []
created_date: '2026-11-24T17:24:00Z'
updated_date: '2026-11-24T18:08:00Z'
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
- [ ] #4 MODEL-ROSTER.md free tier and the opencode_mcp_server.py docstring list only free ids the current CLI offers (opencode models), each with its data terms, and drop ids that are gone (e.g. glm-5.3-flash-free, mimo-v2.5-free); the date at the top is updated
- [ ] #5 pool_for groups free opencode ids per verified pooling: one shared free pool if confirmed (currently each free id is its own pool), with the free-limit error string ("Free usage limit reached" or the current wording) recognised by is_limit_line and the fail-fast patterns
- [ ] #6 The free-tier IP block ("not available from your current network") is recorded as a fail-fast line but not as a pool limit hit
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-11-24, from Gemini web research (UNVERIFIED, third-party summary; check each claim against opencode docs or the GitHub issues it names before acting):
- Free models are limited-time promotions with unpublished daily request caps; the 200 requests/day FAQ figure was removed around 2026-10-22. LongCat 2.5 Free is said to be the only one with a published cap (100/day).
- Rate limiting is by IP and by key/account; VPN, proxy or shared NAT can trigger "free models are not available from your current network" (abuse block).
- Free models never draw on Console (Zen) credit or the Go plan; adding credit does not lift a free limit. After Go limits are exhausted (without extra usage), OpenCode falls back to free models.
- Free endpoints work only inside OpenCode; denying read/bash permissions in agent config has caused false "free tier can only be used from within OpenCode" errors.
- 2.1.2 shows the reset time on free-limit errors; OPENCODE_DISABLE_FREE_MODELS=1 hides free models.
- All built-in *-free models reportedly SHARE one daily pool: after "Free usage limit reached" on one, others fail the same way until the daily reset (issues #57540, #59252 on anomalyco/opencode; officially undocumented). If confirmed, the ADM-19 guard is wrong to treat each free id as its own pool.
- Free models need CLI v1.18.0 or newer (issue #59387).
- The Go pricing table lists a few promotional free models (Beam Free, Hy4 Flash Free, Iceman Contributor Free) with no monthly dollar cap, still under daily limits.
Also 2026-11-24: opencode.ai/docs/zen (Console models page) lists current free ids big-pickle, deepseek-v4.1-flash-free, beam-free, flint-40b-free, hy4-flash-free, mimo-v2.6-flash-free, nemotron-3.5-lightning-free, nemotron-3.5-super-vl-free, iceman-free, tidepool-alpha-free, jev-1.13-free, with per-model retention/training terms; MODEL-ROSTER free tier predates this. Local opencode log (1,971 lines) had no free-model limit errors.
<!-- SECTION:NOTES:END -->
