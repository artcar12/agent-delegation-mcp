---
id: ADM-15
title: 'quota_status: one table of headless quota across agy, Codex and opencode'
status: Done
assignee: []
created_date: '2026-11-17 00:20'
updated_date: '2026-11-17 01:50'
labels: []
dependencies: []
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The coordinator routes work across Claude, agy, opencode and Codex by remaining quota, but only agy reports quota headlessly through its own CLI, and nothing in the wrappers reads any of it. The quota-check skill in claude-skills reads the web usage pages and needs a stable machine-readable source for the headless half. Sources: agy -p /usage (JSON, tab-separated text fallback), the rate_limits snapshot in the newest Codex session file, and opencode stats plus the last 'Go usage limit exceeded' line in its log. Covers ADM-12 AC #1, #4 and #5.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A quota_status tool is exposed by every wrapper from one shared module and returns a table, or JSON with format='json', with source, pool, window, percent used and left, and reset time per window
- [x] #2 agy pools come from /usage JSON, falling back to its tab-separated text lines
- [x] #3 Codex windows come from the newest session file with a non-null payload.rate_limits; only rate_limits fields are read; as-of is the file mtime; a window whose resets_at has passed is marked reset
- [x] #4 opencode reports a stats summary and the time of the last usage-limit line in its log, if any
- [x] #5 Each source fails soft as 'unavailable: reason' and never raises
- [x] #6 Parser tests use synthetic fixtures only
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. quota_sources.py beside the servers: pure parsers (agy JSON/text, Codex rate_limits line, opencode stats JSON, opencode log line) plus collectors that shell out / read files, each fail-soft, run in parallel threads.
2. Render a fixed-column table and a schema-1 JSON document.
3. Each server imports it and registers quota_status(format); ImportError degrades to an 'unavailable' answer.
4. test/test_quota_sources.py with synthetic fixtures; README section.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified live 2026-11-16: quota_status over MCP from all three servers (uv run --script); agy 5 windows, codex 2, opencode summary; 2.3 s. Review fixes: codex collector skips a malformed newest snapshot. Tests: test/test_quota_sources.py 20 OK.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
quota_sources.py, shared by all three wrappers' quota_status: agy /usage JSON (text fallback), Codex rate_limits from the newest session file (only that field decoded), opencode stats today/7d plus last usage-limit time. Table or schema-1 JSON. Verified live over MCP and with 20 synthetic-fixture tests.
<!-- SECTION:FINAL_SUMMARY:END -->
