# Talk decks

Two versions of the same talk, for different rooms. Both are 16:9 with speaker
notes on every slide, and both are generated — the scripts that build them are
not in the repo, so edit the `.pptx` directly.

| Deck | Slides | Audience | Runs |
|---|---|---|---|
| [`agent-delegation-mcp.pptx`](agent-delegation-mcp.pptx) | 13 | Engineers. Assumes CLIs, tests, git and process behaviour. | ~10-12 min plus a live demo |
| [`agent-delegation-overview.pptx`](agent-delegation-overview.pptx) | 10 | Works in tech, not a CS background — product, design, QA, ops, management. | ~10 min, no demo |

The technical deck carries the argv, the seven silent flags, the four exit paths
of the await loop, and the model-routing decision. The overview deck carries
none of that: no code, no flag names, no internals. It keeps the three
false-report stories, the "all 635 tests passed" problem, and the four rules,
because those need no jargon and are the parts that generalise to delegating
work to anything unattended.

Both end on the same point from a different angle. The technical one closes on
the failure-mode table; the overview closes on the observation that none of the
rules are really about AI.
