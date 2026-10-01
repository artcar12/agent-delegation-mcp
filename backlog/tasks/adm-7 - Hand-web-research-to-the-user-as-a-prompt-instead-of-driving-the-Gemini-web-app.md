---
id: ADM-7
title: >-
  Hand web research to the user as a prompt instead of driving the Gemini web
  app
status: Done
assignee:
  - '@claude'
created_date: '2026-10-01 15:05'
updated_date: '2026-10-01 15:26'
labels: []
dependencies: []
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Automated research through the Gemini web app is too flaky to rely on: the web app is buggy, changes often, and throws odd errors even when Arthur drives it by hand. The 1.6.x MCP instructions told every session to use gemini_ask for web research BY DEFAULT and to retry rather than fall back, which made Claude depend on the flakiest path for the most common job. Decided with Arthur on 2026-10-01: the default becomes prompt handover (Claude writes one complete prompt, says same or new Gemini session, waits for pasted results), including whenever a fact may postdate training data. gemini_ask stays for explicit "run it yourself" requests. dispatch_gemini is dropped as redundant: it existed for long-running work (Deep Research, removed in 1.3.0), and gemini_ask already takes timeout_seconds.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Server instructions make prompt handover the default for web research, including whenever a fact may be newer than training data, and say gemini_ask is only for explicit user requests
- [x] #2 Handover guidance requires one complete specific prompt asking for a source URL per claim, and an explicit same-session or new-session call, defaulting to same session for a direct follow-up
- [x] #3 A plugin skill turns /agent-delegation:gemini-ask <topic> into a ready-to-paste research prompt without calling any tool
- [x] #4 dispatch_gemini and the server-local check_run, cancel_run and list_runs are removed, with their run-store code and tests
- [x] #5 README and gemini_ask docstring no longer say to use the tool for research by default
- [x] #6 Test suite passes, including a test that the instructions carry the handover default
- [x] #7 Version bumped to 1.7.0 in plugin.json and marketplace.json with release notes
- [x] #8 Unattended sessions (goal skill, or user says they will be away) carry on from own knowledge and leave a note with a ready-to-run prompt per fact that needs live knowledge, without calling gemini_ask
- [x] #9 Every rule a session must act on lands in the first 2000 characters of the instructions, enforced by a test
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Rewrite _instructions() research guidance: handover by default, err on the side of not knowing, unattended exception, URL per claim, session line, gemini_ask explicit-only.
2. Add skills/gemini-ask/SKILL.md (/agent-delegation:gemini-ask <topic>), calls no tools.
3. Remove dispatch_gemini, check_run, cancel_run, list_runs and the run-store copy; route every tool through _browser_lock.
4. Update tests (drop GeminiWebRunStoreTests, add instruction and lock tests), README, manifests; bump 1.7.0.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
DECISION (Arthur, 2026-10-01): web research defaults to prompt handover. Claude writes one complete prompt (source URL per claim, never verbatim quotes), says new vs same Gemini conversation (same by default for a direct follow-up), stops, works from the pasted answer. Applies whenever a fact may postdate training data; being unsure counts as not knowing. Exception: unattended sessions (goal skill, or user says they will be away) carry on from own knowledge and leave a note per fact needing live knowledge, with the ready-to-run prompt; they do NOT call gemini_ask instead. gemini_ask kept for explicit "run it yourself" and for Canvas/image/video/attachments. dispatch_gemini removed as redundant: built for long runs (Deep Research, removed 1.3.0); gemini_ask timeout_seconds covers the rest. Do not re-add automated research by default without Arthur.
Rationale: the Gemini web app is buggy, changes often and errors even when driven by hand; the 1.6.x "USE THIS FOR WEB RESEARCH BY DEFAULT... retry rather than fall back" text put every research question through the flakiest path.
FINDING: Claude Code truncates MCP server instructions at ~2050 chars. The 1.6.x text arrived cut off mid-sentence in the quote caveat, so its quota/Flash/throttle rules never reached sessions. New essentials sit in the first ~1780 chars; test_the_rules_survive_client_truncation fails if one moves past 2000.
Implemented: instructions rewrite, skills/gemini-ask/SKILL.md, removal of dispatch_gemini + run store from gemini_web_mcp_server.py (all remaining tools now take _browser_lock and return BUSY), tests, README (tools, config, research section, 1.7.0 notes), manifests at 1.7.0. 115 tests pass; claude plugin validate passes (pre-existing CLAUDE.md warning only); real mcp SDK loads the server with 4 tools. Not done: commit, tag, release; talk decks in docs/ (pptx) not checked for dispatch_gemini mentions.

Validation before release: python3 -m unittest discover -s test -> 115 OK (ResearchHandoverInstructionsTests pins the handover, unattended and truncation rules; ServerArgvTests pins the removed tools and the browser lock); claude plugin validate . passes; real mcp SDK lists gemini_ask, gemini_conversations, gemini_read_conversation, delegation_status.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Web research now defaults to a prompt handover: the gemini-web-wrapper instructions tell Claude to write one complete prompt (URL per claim, explicit new/same Gemini session), stop and work from the pasted answer, whenever a fact may postdate training; unattended sessions proceed and leave notes with ready prompts instead. Added the gemini-ask plugin skill. Removed dispatch_gemini and this server's run store; gemini_ask stays for explicit requests and Canvas/image/video/attachments. Instructions restructured to fit Claude Code's ~2000-char truncation. Released as 1.7.0. Verified by 115 passing tests, plugin validate, and loading the server under the real MCP SDK.
<!-- SECTION:FINAL_SUMMARY:END -->
