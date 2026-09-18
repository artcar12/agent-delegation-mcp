---
id: ADM-4
title: 'Deep Research removed: kick-offs wedged about two thirds of the time'
status: Done
assignee: []
created_date: '2026-09-18 18:45'
updated_date: '2026-09-18 18:53'
labels: []
dependencies: []
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Of three deep-research kick-offs observed on 2026-09-18, one completed in ~40 minutes (5005a96d83884070, Vite vs Rspack) and two wedged with no error surfaced:

- 8350e51595585fd2 (Redis vs Valkey): panel frozen at exactly 11744 chars and 19 thought-items across readings 55+ minutes apart, chip still reading 'Researching 64 websites...', mat-progress-spinner still present. Kicked off ~12:00, still wedged at 14:33.
- 100bc647b567907f (httpx vs requests): wedged at 'Starting research...', browser closed mid-handoff, so this one is less representative.

Both wedged runs were started during the window when Playwright's --use-mock-keychain was still silently deleting the profile's Google cookies on every launch. That is a plausible cause - the session backing the job may have been invalidated server-side - but it is unproven, and the Redis run's browser was NOT closed mid-handoff, so the httpx explanation does not cover it.

Not chased further by agreement: deep research is reserved for the most taxing problems and is expected to be rare for a CLI agent, so this is documented as a known flakiness rather than fixed. Reopen if a kick-off made cleanly under 1.2.2 also wedges - that would move it from 'bad batch' to a live bug in start_research.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A deep-research kick-off made cleanly under current code is observed to completion, or the wedge is reproduced
- [ ] #2 If reproducible, the wedge is either detected and reported as failed rather than running, or the cause is found
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
CLOSED BY REMOVAL, not by a fix. gemini_research, dispatch_research, mode='deep-research' and the research worker subcommand are gone as of 1.3.0. The MCP instructions now tell a session to ask the user to run a Deep Research in the browser and hand back a conversation id, which gemini_read_conversation reads.

Rationale: two of three kick-offs wedged with no error and no recovery, and a CLI agent rarely needs the capability at all - one long gemini_ask plus follow-ups beat it head to head on the same question, 37 seconds against a run that never finished. Known-brittle code is worse than none because it invites a plan that depends on it.

The code is at tag agent-delegation--v1.2.2 if this is ever worth another attempt. The one piece of knowledge in it that is expensive to rediscover: a FINISHED report keeps thinking-panel-skeleton-loader mounted, visible and 200px tall, so the obvious 'is a loader still showing?' completion check reports every completed report as running forever. The honest marker is the presence of #extended-response-markdown-content with aria-busy != true and no mat-progress-spinner in the panel. Also: the immersive panel mounts ~4.7s after the chat turns beside it, so reading state immediately reports a running report as absent.
<!-- SECTION:NOTES:END -->
