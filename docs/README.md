# Talk decks

Two versions of the same talk, for different rooms. Both are 16:9 with speaker
notes on every slide, and both are generated — the scripts that build them are
not in the repo, so edit the `.pptx` directly.

| Deck | Slides | Audience | Runs |
|---|---|---|---|
| [`agent-delegation-mcp.pptx`](agent-delegation-mcp.pptx) | 13 | Engineers. Assumes CLIs, tests, git and process behaviour. | ~10-12 min plus a live demo |
| [`agent-delegation-overview.pptx`](agent-delegation-overview.pptx) | 10 | Product, QA, ops — comfortable with tests, git and code review, but not implementing this. | ~10 min, no demo |

The technical deck carries the argv, the seven silent flags, the four exit paths
of the await loop, and the model-routing decision. The overview deck drops those
— no flag names, no process handling, no SDK internals — but keeps real
terminology, because its audience reads code even if they do not write this
part. It is the same material at the altitude of decisions and trade-offs:
what the split is, what one dispatch looks like, why the return value is not
evidence, why "gate passed" is not "it works", and why reliability beats
benchmark scores.

Both end on the same point from a different angle. The technical one closes on
the failure-mode table; the overview closes on the observation that none of the
rules are AI-specific.
