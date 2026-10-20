---
name: gemini-ask
description: Write a ready-to-paste web research prompt for the user to run in the Gemini web app, instead of searching yourself. Use when the user invokes /gemini-ask <topic>, asks for a research prompt, or when an answer needs open-ended research across many sources (comparisons, surveys, current state of the art). NOT for a single fact, a yes/no about something common but newer than your training data, or a direct URL; look those up yourself with WebSearch/WebFetch. Calls no tools.
argument-hint: <topic or question>
---

# Hand a research prompt to the user

Driving the Gemini web app automatically is not supported (Google restricts
automated use of it), so research goes through the user: you write the prompt,
they run it by hand, they paste the answer back. Do not call `WebSearch` or
`WebFetch` for this, and do not try to drive the web app through a browser
tool.

Exceptions, handled yourself with `WebFetch`/`WebSearch`, because handing them
over wastes the user's time:

- The user gives you a direct URL: fetch and read it.
- A single fact, or a yes/no about something common that may be newer than
  your training data (a current version, a release date, "does X support Y
  yet"): look it up.

Only open-ended research that needs many sources comes here.

Topic: $ARGUMENTS

If the topic is empty, use whatever the conversation is currently stuck on.

## When to reach for this unprompted

When an answer depends on open-ended research (comparing options, surveying
what exists, the current state of the art) and what you know may be out of
date, hand over a prompt rather than answering from memory with a caveat. A
single checkable fact is not that: look it up instead (see the exceptions
above).

**Exception: the user is away.** If the session is running a goal skill, or the
user said they will not be around, do not stop to wait for a handover. Carry on
from what you know, and for each fact that really needs live knowledge leave a
note covering the claim you relied on, why it may be stale, what depends on it,
and the prompt below, ready to run. Put the notes where the user will look
when they are back (task notes, a file the work already writes, the final
report), and list them in the final report.

## The prompt

Write ONE complete, specific prompt. Length is not a problem:

- The context Gemini needs to answer well: project, stack, versions in use,
  constraints, what has already been ruled out.
- The exact questions, numbered if there are several on the same subject.
  Unrelated questions go in separate prompts.
- The shape of answer wanted: a comparison table, one version per item, dates,
  a recommendation split by situation.
- **A source URL for every claim.** Do not ask for verbatim quotes per claim.
  That can make Gemini say it cannot browse and then answer from memory.
- If the job truly needs Deep Research (dozens of sources read end to end, a
  written report as the deliverable), say so, so the user can switch modes.
  Otherwise a normal chat prompt on Flash is enough.

## The session line

Always say whether to run it in a **new** Gemini conversation or **continue**
the previous one. Same Gemini conversation only if the prompt needs something
only the earlier answers contain; otherwise a new one. No default. Say it
explicitly either way, for example:

- "Same thread as before: this builds on the vendor list it gave."
- "New conversation: nothing here depends on the earlier answers."

## Output

1. One line on what the prompt is for.
2. The session line.
3. The prompt in a fenced `text` block, so it copies cleanly.
4. One line asking the user to paste the answer back.

Then stop and wait. When the answer comes back, check the URLs it cites before
leaning on a claim. If something came back thin, write a follow-up prompt for
the same thread that pushes on exactly that, rather than a new mega-prompt.
