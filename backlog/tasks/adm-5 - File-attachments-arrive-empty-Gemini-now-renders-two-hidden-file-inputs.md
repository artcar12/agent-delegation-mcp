---
id: ADM-5
title: 'File attachments arrive empty: Gemini now renders two hidden file inputs'
status: To Do
assignee: []
created_date: '2026-09-21 18:33'
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
- [ ] #1 ask --file delivers the file's real contents, proven by Gemini quoting a line from it
- [ ] #2 A stalled or failed upload raises rather than sending a prompt about a file that never arrived
- [ ] #3 The working input is selected structurally, not by DOM order
<!-- AC:END -->
