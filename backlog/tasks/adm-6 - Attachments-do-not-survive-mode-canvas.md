---
id: ADM-6
title: Attachments do not survive --mode canvas
status: Done
assignee: []
created_date: '2026-09-21 18:46'
updated_date: '2026-09-21 19:12'
labels: []
dependencies: []
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
ask --file works in chat mode (fixed in ADM-5) but not with --mode canvas: Gemini replies that the file is empty regardless of ordering. Tested both attach-then-select-canvas and select-canvas-then-attach; both fail.

In canvas mode the composer shows no upload indicator and no chip for the file, so the upload appears never to start rather than to fail midway. Chat mode shows 'CSV' + the stem as a chip within a few seconds.

Current behaviour is safe, not silent: _await_uploads times out after 60s and refuses to send, with an error naming canvas as the likely cause. So this costs a slow failure, never a wrong answer.

Probing notes: document.body.innerText is useless for detecting chips - it includes the conversation sidebar, so earlier chats titled e.g. 'Inspecting CSV File Content' match filename-ish searches. Scope to <input-container>.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 ask --mode canvas --file delivers the file's real contents, or the worker refuses the combination up front with a clear message instead of timing out after 60s
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Refuse --mode canvas together with --file in cmd_ask before a browser opens (EXIT_USAGE), since the upload never starts in canvas mode. 2. Mirror the refusal in gemini_ask and dispatch_gemini so the Chrome launch is saved too. 3. Drop the unconditional canvas hint from the upload timeout, which misled chat-mode users. 4. Pin both refusals with unit tests.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Took the 'refuse up front' branch of the AC rather than making canvas uploads work: the composer renders no chip and no indicator in canvas mode, so there is no state to wait on. cmd_ask raises EXIT_USAGE before Session() is built; the server's _refuse_canvas_files() returns the same refusal from gemini_ask and dispatch_gemini before any subprocess. Verified by test_canvas_with_a_file_is_refused_before_a_browser_opens (worker) and test_canvas_with_files_is_refused_before_a_browser_opens (server): python3 test/test_gemini_web_worker.py, 71 tests OK.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
canvas + --file is refused before a browser opens, in the worker (EXIT_USAGE) and in both server tools, with a message naming the cause and the fix (use chat). Replaces a 60s upload timeout that cost a Chrome launch per attempt. Verified by unit tests that call cmd_ask / gemini_ask / dispatch_gemini directly and assert no browser is reached.
<!-- SECTION:FINAL_SUMMARY:END -->
