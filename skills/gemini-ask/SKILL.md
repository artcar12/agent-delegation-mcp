---
name: gemini-ask
description: Write a ready-to-paste web research prompt for the user to run in the Gemini web app, instead of searching yourself. Use when the user invokes /gemini-ask <topic>, asks for a research prompt, or whenever an answer depends on facts that may be newer than your training data (current versions, release dates, prices, API changes, deprecations, "is X still true") and you are not sure what you know is current. Calls no tools.
argument-hint: <topic or question>
---

# Hand a research prompt to the user

The Gemini web app is too flaky to drive automatically, so research goes
through the user: you write the prompt, they run it by hand, they paste the
answer back. Do not call `gemini_ask`, `WebSearch` or `WebFetch` for this. The
user can still tell you to run it yourself, and then `gemini_ask` is fine.

Topic: $ARGUMENTS

If the topic is empty, use whatever the conversation is currently stuck on.

## When to reach for this unprompted

Err on the side of not knowing. If a fact could sit outside your training data
and you are not sure it is current, hand over a prompt rather than answering
from memory with a caveat.

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
the previous one. Default to the same conversation when the prompt is a direct
follow-up on the same subject; use a new one for an unrelated topic. Say it
explicitly either way, for example:

- "Same thread as before: this follows up on the same vendors."
- "New conversation: unrelated topic."

## Output

1. One line on what the prompt is for.
2. The session line.
3. The prompt in a fenced `text` block, so it copies cleanly.
4. One line asking the user to paste the answer back. They can also give you
   the conversation id, which `gemini_read_conversation` can read.

Then stop and wait. When the answer comes back, check the URLs it cites before
leaning on a claim. If something came back thin, write a follow-up prompt for
the same thread that pushes on exactly that, rather than a new mega-prompt.
