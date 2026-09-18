---
id: ADM-3
title: Harvest finished Deep Research reports from the immersive panel
status: Done
assignee:
  - '@arthurcarroll'
created_date: '2026-09-18 17:30'
updated_date: '2026-09-18 18:53'
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
SUPERSEDED: the feature this task delivered was removed in 1.3.0. See ADM-4 for why and for what was learned. The implementation is at tag agent-delegation--v1.2.2.
<!-- SECTION:NOTES:END -->
