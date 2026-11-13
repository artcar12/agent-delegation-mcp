---
id: ADM-10
title: Per-run deadlines and long-command guidance for delegated runs
status: Done
assignee:
  - '@claude'
created_date: '2026-11-13 19:07'
updated_date: '2026-11-13 19:09'
labels: []
dependencies: []
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
On 2026-11-10 three opencode-go/deepseek-v4-pro review runs in sound-sprouts died on `make test`, which takes about 23 minutes there. In one, opencode's own bash tool stopped the command at the 600000 ms timeout the model had passed; in the other two the wrapper's 600 s idle watchdog killed the run because `make test 2>&1 | tail -80` printed nothing until the end. Neither limit is model-specific, and the wrapper only exposes them as server-wide env vars, so a caller who knows a run will hit a long quiet command cannot raise them for that run. opencode 2.2.1's bash tool defaults to 120000 ms in the foreground, accepts any larger timeout (0 disables it), and has no timeout for background commands.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 dispatch_opencode and dispatch_agy accept optional timeout_seconds and idle_seconds that override the server defaults for that run only; idle_seconds=0 disables the idle check
- [x] #2 Invalid values (negative, or a zero timeout_seconds) are refused with a clear error and nothing is started
- [x] #3 The run record, the dispatch reply and check_run report the deadlines the run actually uses
- [x] #4 The dispatch_opencode docstring tells callers how to brief delegates for commands longer than the bash default: pass a bash timeout above the expected runtime or 0, do not pipe long commands through tail or similar, and raise idle_seconds above the longest silent command
- [x] #5 Tests cover the per-run overrides and the refusal; the existing suite passes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Thread optional timeout_seconds/idle_seconds through dispatch_* into _dispatch in both servers, validate, store in the record, report in the reply.
2. Add a LONG COMMANDS section to dispatch_opencode's docstring (opencode 2.2.1 bash timeout semantics, no tail pipes, raise idle_seconds).
3. Tests in test/test_run_store.py for overrides and refusals; run the suite.
4. Bump to 1.11.0, README/changelog note.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
opencode 2.2.1 bash tool semantics read from the installed binary: foreground default 120000 ms, any timeout accepted, 0 disables, background has none. Validation: python3 test/test_run_store.py, 55 tests OK (4 new per class pair: overrides, defaults, per-run idle enforced, refusals). Docstring check: LONG COMMANDS section present in dispatch_opencode.__doc__. Released as 1.11.0 (plugin.json, marketplace.json, README release notes).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
dispatch_opencode and dispatch_agy take optional timeout_seconds and idle_seconds that override the server limits for one run (idle 0 disables), validated before anything starts; the record, the dispatch reply and check_run show the limits in force. dispatch_opencode's docstring has a LONG COMMANDS section on opencode's 120 s bash default, tail pipes looking hung, and raising idle_seconds, prompted by three deepseek-v4-pro reviews killed on a 23 minute make test on 2026-11-10. Verified with test/test_run_store.py (55 OK). Release 1.11.0.
<!-- SECTION:FINAL_SUMMARY:END -->
