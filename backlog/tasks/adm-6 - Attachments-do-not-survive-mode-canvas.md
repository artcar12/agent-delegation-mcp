---
id: ADM-6
title: Attachments do not survive --mode canvas
status: To Do
assignee: []
created_date: '2026-09-21 18:46'
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
- [ ] #1 ask --mode canvas --file delivers the file's real contents, or the worker refuses the combination up front with a clear message instead of timing out after 60s
<!-- AC:END -->
