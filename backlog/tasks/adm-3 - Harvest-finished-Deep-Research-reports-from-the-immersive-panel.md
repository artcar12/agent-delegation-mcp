---
id: ADM-3
title: Harvest finished Deep Research reports from the immersive panel
status: Done
assignee:
  - '@arthurcarroll'
created_date: '2026-09-18 17:30'
updated_date: '2026-09-18 17:59'
labels: []
dependencies:
  - ADM-2
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
dispatch_gemini(mode='deep-research') currently kicks the research off and returns the conversation id. It cannot bring the report back, so the caller has to open the conversation in a browser.

Two things make this mode unlike every other one, both established by live testing on 2026-09-18:

1. The chat turn for a deep-research prompt is a PERMANENT stub - 'I'll let you know when your research is done. In the meantime, you can leave this chat.' It never gets the message-actions row that every other mode completes with, so the normal completion check waits forever. The first attempt burned a 1500s timeout and returned 169 characters.
2. The report is written into a <deep-research-immersive-panel>, not into the chat turn. A live run was observed growing that panel from 1880 to 3690 characters while the chat turn stayed frozen.

Google runs the research server-side, so the browser does not need to stay open - which is why holding one for 20 minutes was the wrong shape and should not be reintroduced. The right shape is kick-off (done) plus a separate harvest call that opens the conversation and reads the panel.

Still unknown, and the reason this is not already built: what marks a report as FINISHED. Candidates seen but not confirmed on a completed run are the absence of <thinking-panel-skeleton-loader>, a message-actions or copy-button inside the panel, and panel text length going stable. A watcher was polling conversation 8350e51595585fd2 for exactly this when the work was cut; that conversation and 5005a96d83884070 should both hold completed reports now.

Note that conversation 100bc647b567907f is wedged at 'Starting research...' and is not representative - the browser was closed mid-handoff on that one.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A finished Deep Research report can be retrieved as markdown from its conversation id
- [x] #2 An unfinished report reports progress instead of returning the plan or an empty string
- [x] #3 The completion marker is established against a genuinely completed run, not inferred
- [x] #4 The browser is not held open for the duration of the research
- [x] #5 Extraction is covered offline by a captured fixture of a completed panel
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Completion marker established live on 2026-09-18 by sampling two conversations side by side: 5005a96d83884070 (chat turn: "I've completed your research") against 8350e51595585fd2 ("I'm on it... you can leave this chat"), keeping only what differed.

The obvious marker is wrong. thinking-panel-skeleton-loader is present, visible and 200px tall in BOTH states, so the absence-of-a-loader check reports every finished report as running forever. What actually differs: #extended-response-markdown-content (absent vs present with aria-busy=false), mat-progress-spinner (1 vs 0), toc-menu (0 vs 1). The report body's own presence is now the marker.

Two further live findings:
- The immersive panel mounts ~4.7s AFTER the chat turns beside it, so reading state immediately returned 'absent' for a running report. 15s grace added.
- Extraction is scoped to the report body, not the panel: 32KB of report inside 692KB of panel on the measured run. Sources get their own renderer because the generic walk flattens each citation's domain and title into one link label.

Verified end to end with the real subcommand: complete (42409 chars in 11.4s), running (progress text), absent (ordinary chat). Both panels captured to test/fixtures/; a test asserts the skeleton loader appears in the COMPLETED fixture so the bad check cannot come back.
<!-- SECTION:NOTES:END -->
