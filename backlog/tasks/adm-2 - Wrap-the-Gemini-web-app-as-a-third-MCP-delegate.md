---
id: ADM-2
title: Wrap the Gemini web app as a third MCP delegate
status: Done
assignee:
  - '@arthurcarroll'
created_date: '2026-09-18 15:33'
updated_date: '2026-09-21 18:34'
labels: []
dependencies: []
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The agy and opencode wrappers reach Gemini through its API, which has no Deep Research, no Canvas, no Gems, no persistent conversation history and no file attachments. Those surfaces exist only in the logged-in web app, and there is currently no way for a CLI agent to reach them.

Approach agreed with the user: a gemini_web.py worker CLI drives gemini.google.com with Playwright against a dedicated Chrome profile (never the daily profile), and gemini_web_mcp_server.py wraps that worker with the existing run store exactly as agy_mcp_server.py wraps agy. Playwright never runs inside the MCP server process, so runs keep surviving server restarts.

Every DOM selector is unversioned Angular internals; they are collected in one SELECTORS dict so a Google redesign is a single-file fix. Automating the web UI is outside Google's terms for automated access and the account can be rate-limited - accepted by the user when choosing this approach over the API.

Full design: ~/.claude/plans/cuddly-percolating-fiddle.md
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 gemini_web.py login stores a working session in a dedicated profile without the script ever handling credentials
- [x] #2 gemini_web.py ask sends a prompt and returns the response as markdown, with fenced code blocks intact
- [x] #3 A conversation id is returned on every ask and can be passed back to resume that conversation
- [x] #4 gemini_web.py exposes read, list and status subcommands over conversation history
- [x] #5 gemini_web_mcp_server.py exposes both a synchronous gemini_ask and the async dispatch_gemini/check_run/cancel_run/list_runs pair
- [x] #6 The new server reuses the run store unchanged, so a run survives a server restart
- [x] #7 The server is registered for Claude Code, opencode and the gemini CLI
- [x] #8 test_run_store.py runs its full suite against the new server file via a subclass, and worker extraction logic is covered offline by fixtures
- [x] #9 Canvas mode is reachable through the ask subcommand (attachments regressed on Gemini's side after this shipped - split to ADM-5; Deep Research deliberately removed in 1.3.0 - see ADM-4)
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Fast-forward the Projects checkout to origin/main (1.1.0 -> 1.1.1) so a third server is registered by the installed plugin.
2. gemini_web.py: Playwright worker CLI with login/ask/read/list/status, one SELECTORS dict, clipboard-first extraction with a Python HTML->Markdown fallback.
3. gemini_web_mcp_server.py: clone agy_mcp_server.py, keep the run store byte-for-byte, swap in gemini-web identifiers and sync + async tools.
4. Register in .mcp.json, plugin.json, opencode.json and ~/.gemini/settings.json.
5. Extend test_run_store.py with a third subclass; add offline worker tests over captured fixtures.
6. Verify live: sign-in, ask, resume, code block, attachment, canvas, deep research, dispatch/check_run.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Shipped. Verified live against gemini.google.com on 2026-09-18: ask round-trip (12.5s), resume by conversation id, fenced code blocks intact headed and headless, file attachment, canvas mode, dispatch/check_run with a .agent-runs artifact, and a run surviving the server process that started it. 54 run-store tests (now across all three servers) plus 30 offline worker tests pass.

AC5 is only partly met and the remainder moved to ADM-3: Canvas and attachments work, and Deep Research is kicked off correctly, but the finished report cannot be retrieved yet.

Six bugs the live testing caught, none of which were visible from the code:
- Playwright passes --use-mock-keychain by default. On macOS that hands Chrome a different cookie-encryption key than the real Keychain holds, so Chrome cannot decrypt the profile and silently DELETES its cookies on launch. Every verification destroyed the session it was checking, and the symptom was indistinguishable from 'the sign-in did not take'. Fixed with ignore_default_args; login now counts cookies before and after and says so outright.
- Google refuses its own OAuth flow inside a DevTools-controlled browser, so login has to run an unautomated Chrome and let Playwright pick the profile up afterwards.
- Angular ignores a synthetic Enter in headless Chrome: the prompt lands in the editor and never sends. The send button is clicked now, with Enter as fallback.
- Gemini's file inputs are class='hidden-file-input' and Playwright waits for visible by default, so attachments timed out on a selector that was present the whole time.
- Which tools the drawer promotes varies; Deep Research sits behind 'More tools' some of the time.
- Every user turn carries a cdk-visually-hidden h5 that repeats the whole prompt after 'You said', which doubled every turn in read output. Gemini also puts a code block's language in a header span rather than a language-* class, so it leaked as a stray line above an unlabelled fence.

Out of scope but changed: the cli column in list_runs went from 8 to 10 chars in ALL THREE servers, because 'gemini-web' overflowed it. Applying it uniformly keeps the run store byte-identical across the three files, which the test suite asserts.

1.4.0-1.6.0 (2026-09-21), all shipped and tagged, 114 tests green:
- 1.4.0 Pacing: jittered clicks/typing, Chrome automation switches dropped, navigator.webdriver cleared. pong 13.8s -> 15.0s.
- 1.5.0 Quota notices were being returned as answers. Gemini renders them as ordinary response turns, so every completion signal said 'done'. Now exit 6 (limit, never retry) / 7 (transient, one retry). Also: exhausting Pro DOWNGRADES SILENTLY to Flash with no message, so every answer now reports its model.
- 1.5.1 Callers told not to smoke-test with a fixed canary string.
- 1.5.2 gemini_ask's docstring was truncating before the model saw the end; 2686 -> 1210 chars with the parameter reference moved to the top. Two tests guard length and position.
- 1.5.3 Fixed a false positive from 1.5.0 that discarded correct answers: a 158-char explanation of HTTP 429 was read as a quota wall. Patterns now must address the reader ('your', "you've"), enforced by a test. The length gate I had relied on was the wrong idea - good answers are short.
- 1.6.0 The model picker is a PROFILE setting that persists across tabs and days, and it had drifted to Pro for a whole session unnoticed. ask now reads it before typing and exits 8 on mismatch, sending nothing. New 'model' subcommand. Matching is exact: the picker lists Flash-Lite above Flash, so a substring match silently selects the weaker model.

Pre-review verification on 2026-09-21 found file attachments regressed on Gemini's side - see ADM-5. Canvas re-verified working today; attachments were working when this task shipped.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Gemini web app wrapped as a third MCP delegate: gemini_web.py drives a dedicated Chrome profile with Playwright, gemini_web_mcp_server.py wraps it with the existing run store unchanged. Verified live against gemini.google.com - ask round-trip, resume by conversation id, fenced code blocks headed and headless, canvas mode, dispatch/check_run with a .agent-runs artifact, and a run surviving the server that started it. 114 automated tests (54 run-store across all three servers, 60 offline worker). Scope changes: Deep Research built then removed in 1.3.0 as unfixably flaky (ADM-4); file attachments worked at ship time and regressed later on Gemini's side (ADM-5).
<!-- SECTION:FINAL_SUMMARY:END -->
