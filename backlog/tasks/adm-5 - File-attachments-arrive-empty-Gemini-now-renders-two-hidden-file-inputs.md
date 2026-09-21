---
id: ADM-5
title: 'File attachments arrive empty: Gemini now renders two hidden file inputs'
status: Done
assignee: []
created_date: '2026-09-21 18:33'
updated_date: '2026-09-21 19:38'
labels: []
dependencies: []
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Attachments worked when ADM-2 shipped (live-verified 2026-09-18) and are broken now. `ask --file` completes with exit 0, but Gemini replies that the file is empty. Reproduced in both chat and canvas mode with a 31-byte CSV that is definitely non-empty on disk.

What the DOM probing found, so nobody has to redo it:

- There are now TWO `input[type=file]` elements, not one. `SELECTORS["file_input"]` is the bare `input[type="file"]`, so `page.set_input_files` takes the FIRST in DOM order.
- Input 0's parent is `<images-files-uploader>`; input 1's parent is a bare `<uploader>`. Both advertise the same `accept` list including .csv.
- With input 0, the chip renders the filename and then the app sits on 'Uploading file' indefinitely - still showing it at 15s.
- With input 1, the chip settles in about 2s.
- BUT settling is not sufficient: after switching to `uploader > input[type="file"]` (verified to match exactly 1 element) the upload still settled and Gemini still reported an empty file. So the chip state does not prove the bytes arrived.
- Both inputs are torn down when the drawer closes: a probe run after Escape found ZERO file inputs in the DOM.

A separate, real defect sits underneath this regardless of which input is right: `attach()` sleeps a flat 1500ms and then sends unconditionally. It never verifies the upload finished, which is why an empty attachment reaches the model dressed as a real one and the worker reports success. Whatever fixes the selector should also make a stalled upload fatal rather than silent.

An attempted fix (correct input + a real settle-wait) was written and REVERTED in the same session because it did not actually make the content arrive. Unverified churn on a broken feature is worse than none. Nothing of it is in the tree.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 ask --file delivers the file's real contents, proven by Gemini quoting a line from it
- [x] #2 A stalled or failed upload raises rather than sending a prompt about a file that never arrived
- [x] #3 The working input is selected structurally, not by DOM order
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
CORRECTION: the two-hidden-file-inputs theory in the description was a RED HERRING. Input 0 (under <images-files-uploader>) was always the right one. I had read 'the chip settled in 2s' on input 1 as 'the bytes arrived', which it does not mean, and wrote a fix around the wrong cause.

Actual cause: attach() slept a flat 1500ms and then sent unconditionally. Uploads take far longer than that - 10-30s even for a 31-byte CSV - so the prompt was sent while the file was still in flight and Gemini received an empty attachment. The worker exited 0, so a total failure looked like a correct answer.

Fix: _await_uploads() blocks until the upload finishes and raises EXIT_TIMEOUT if it does not, so a stalled upload can never again become a confident answer about a file that never arrived. Two non-obvious details:
- Scoped to <input-container>, NOT document.body. The body contains the conversation sidebar, so a previous chat's title can satisfy a naive filename search and pass the check for the wrong reason.
- Matches the basename STEM, not the filename. The chip renders type and stem on separate lines ('CSV' then 'parts'), so the string 'parts.csv' never appears in the composer at all. This is why the first attempt reported 'no chip' and refused to send.

Verified live 2026-09-21: 'Quote the first line' returned 'name,qty' plus the full parsed table; a follow-up returned 3 data rows and total qty 15, both correct. Canvas + attachment still fails and is split to ADM-6.

Live re-verification 2026-09-21 after 1.6.2's fail-closed rewrite of _await_uploads: chat-mode ask --file parts.csv (4 lines) returned the verbatim first line 'name,qty' and total qty 15, both correct, in 30.5s end to end on Flash. So the busy signal (Uploading text or a progress indicator inside <input-container>) is being observed in practice; the seen-in-progress requirement did not block a real upload.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Attachments were arriving empty because attach() slept 1500ms and sent regardless while the upload was still in flight. Replaced with _await_uploads(), which blocks on the composer's own upload state and makes a stall fatal rather than silent. Verified live: Gemini now quotes the file's real contents and computes over them correctly. The two-file-inputs theory in the description was wrong and is corrected in the notes.
<!-- SECTION:FINAL_SUMMARY:END -->
