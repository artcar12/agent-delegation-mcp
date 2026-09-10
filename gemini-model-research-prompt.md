# Research request: OpenCode model roster

I maintain a Claude Code plugin that delegates coding tasks to the `opencode`
CLI as an unattended subagent. Claude picks which model to delegate to at
runtime, with no human checkpoint mid-run. I need decision-relevant facts about
each model in the roster below.

Take as long as you need and search as widely as you want — there is no budget
constraint on this research. The only length constraint is on one specific
paragraph at the end, called out where it appears.

## Priority order

The roster is split into four tiers, ordered by cost-of-being-wrong rather than
by how much data you expect to find. Cover all thirty-four ids; go *deepest* on
Tier 1, where a bad routing call is most expensive.

**Tier 1 — primary routing targets.** These get chosen for real work.

```
opencode-go/kimi-k3
opencode-go/kimi-k2.7-code
opencode-go/glm-5.3
opencode-go/qwen3.8-max
opencode-go/deepseek-v4-pro
opencode-go/minimax-m3
opencode-go/grok-4.6
opencode-go/gpt-5.6-luna
```

**Tier 2 — the rest of the paid tier.** Mostly version siblings and `flash`
variants. The decision these drive is when the cheaper or faster sibling is
actually the better choice, so the sibling-to-sibling *delta* matters more here
than absolute numbers.

```
opencode-go/deepseek-v4-flash
opencode-go/deepseek-v4-flash-vision-exp
opencode-go/glm-5.1
opencode-go/glm-5.2
opencode-go/glm-5.3-flash
opencode-go/kimi-k2.6
opencode-go/longcat-2.0
opencode-go/mimo-v2.5
opencode-go/mimo-v2.5-pro
opencode-go/minimax-m2.7
opencode-go/qwen3.6-plus
opencode-go/qwen3.7-max
opencode-go/qwen3.7-plus
opencode-go/qwen3.8-flash
```

For `deepseek-v4-flash-vision-exp` specifically: is the vision capability
additive to the text model, or does it trade text/coding quality for it? I have
no use for vision and would route around it if it costs anything.

**Tier 3 — the named free-tier models.** I have direct test results for every
free id (see the last section), so do not spend effort re-establishing whether
they work. Three narrower questions instead:

- Context window and max output tokens.
- Is the `-free` id the same weights as its paid counterpart, or a smaller
  model? `mimo-v2.5-free` versus `mimo-v2.5` is the case that matters most.
- Are the two Nemotron failures I observed *known reported behaviour*? I have
  one observation each, which is thin evidence. If the malformed-tool-call
  problem in `nemotron-3.5-lightning-free` is a documented issue, that
  upgrades my single data point into a real finding.

```
opencode/ling-3.0-flash-fin-free
opencode/mimo-v2.5-free
opencode/nemotron-3-ultra-free
opencode/nemotron-3.5-lightning-free
```

**Tier 4 — codenames and unannounced models.** Nothing in these ids maps to a
public vendor name, so identifying them is the whole task and deep search is
where it pays. Changelog diffs, provider release notes, arena/leaderboard
entries for codenamed models, GitHub and Discord discussion, pricing-page
archaeology — all fair game. `hy3`/`hy4-preview` and `muse-spark`'s
"contributor" suffix both look like they encode something.

```
opencode/big-pickle
opencode/muse-spark-1.2-contributor-free
opencode/muse-spark-1.3-contributor-free
opencode-go/muse-spark-1.2-contributor
opencode-go/muse-spark-1.3-contributor
opencode-go/omen-alpha
opencode-go/hy3
opencode-go/hy4-preview
```

Note on the ids generally: the first segment is the OpenCode provider tier, not
the model vendor. `opencode/` is their free tier, `opencode-go/` the paid one.
Several ids are OpenCode's own naming rather than an upstream vendor name, so
**establishing which upstream model each id actually is, is part of the task.**

## What I need, per model

**1. Agentic tool-calling reliability — the most important axis, by a wide
margin.** These run unsupervised in a loop through a CLI that emits tool calls
repeatedly. Any reported weakness in structured output, function calling, or
multi-turn tool loops matters more to me than a raw intelligence score. Cover:

- Malformed or non-parseable tool-call syntax. This is the failure I have
  already hit in testing, so it is not hypothetical.
- Looping, repeating the same call, or failing to terminate.
- Refusing edits, or asking for confirmation in a context where nobody will
  answer.
- Ignoring or drifting from system prompts over a long run.
- Degradation as the context fills.
- **Returning a plan or a description of intended changes instead of editing
  files.** OpenCode has a read-only `plan` agent, so a model that drifts toward
  describing work rather than doing it produces a confident-looking success with
  nothing on disk. This is the failure mode I am least able to detect
  automatically.
- Whether tool-calling stability differs between the vendor's own API and
  third-party/aggregator hosting, since I reach these through OpenCode.

**Search OpenCode's own issue tracker and community channels per model.** A
GitHub issue saying "model X breaks tool calls in opencode" is worth more to me
than any benchmark, because it is the exact harness I run. Same for the
Vercel AI SDK repo, which is what a lot of these CLIs use underneath — the error
I have seen surfaces as `AI_APICallError`.

**2. Upstream identity.** Vendor and real model name behind each id. Flag
aliases, especially where an `opencode/…-free` id is the same weights as an
`opencode-go/…` id versus a genuinely smaller model.

**3. Context window (input) and max output tokens.** I brief delegates with plan
files and they read source trees, so a small window causes silent truncation
rather than an error. Exact numbers. Note where a stated window is only
achievable at reduced quality, and any tokenizer or context-accounting quirk
that makes the usable window smaller than the advertised one.

**4. Coding benchmarks.** SWE-bench Verified above all, then Aider Polyglot,
LiveCodeBench, Terminal-Bench. Give the number, the reporting date, and who
reported it. Add any benchmark that measures *tool use or function calling
specifically* — BFCL or similar — which is closer to what I care about than the
coding scores are.

**5. Speed.** Output tokens/sec and time-to-first-token. These are blocking
subprocesses under a wall-clock timeout, so slowness is a direct cost and a
slow-enough model is indistinguishable from a hung one.

**6. Reasoning behaviour.** Is it a reasoning model, is the budget controllable,
and **does reasoning mode change its tool-calling reliability?** That last part
is why I am asking — a model that is stable in direct mode and flaky with
reasoning on is a routing decision, not a footnote. Also: does reasoning inflate
latency enough to matter under a timeout, and are reasoning tokens billed.

**7. Hosting jurisdiction and data policy.** Where inference runs, whether
prompts are retained or trained on. I have already hit one case — the DeepSeek
tiers — that OpenCode blocks without an explicit workspace opt-in, and I need to
know which others carry a similar constraint.

**8. Price** per million input/output tokens, cache-read discount, and whether
prompt caching is actually available through OpenCode as opposed to only on the
vendor's own API. Long system prompts get resent on every turn of an agentic
loop, so caching is a larger cost factor here than the headline rate.

**9. Quota, pooling, and rate limits on OpenCode specifically.** Which ids draw
on a shared limit — I know `qwen3.8-max` and `longcat-2.0` share one. Also
per-model rate or concurrency limits, and what happens on exhaustion (clean
error versus hang). Anything from OpenCode's docs, pricing page, changelog, or
community about how their 5-hour limits are grouped.

**10. Deprecation and lifecycle signals.** Announced sunset dates, "preview" or
"experimental" labels, models that have already been silently swapped or
renamed, ids that have disappeared from the roster before. I hardcode these ids
into tooling, so one that is about to be retired is a latent bug.

## Evidence rules — apply while collecting, not while writing up

These are collection-time constraints. By synthesis time a guessed number is
indistinguishable from a sourced one, so the discipline has to happen when the
cell is first filled in.

For every numeric field — context window, max output, benchmark score,
tokens/sec, price — either cite a specific source or write `unknown`. **Never
infer a number from a sibling model, an earlier version, the same vendor's other
models, or the id itself.** These numbers get written into tooling, where a wrong
context window causes silent truncation rather than a visible error. An empty
cell is strictly better than a plausible one.

Searching exhaustively is encouraged; the cap is on what you are permitted to
*write*, not on how long you look. When a thorough search turns up nothing, the
answer is `unknown` — not a reasonable-looking value, and not a value borrowed
from the nearest relative. Expect a meaningful fraction of these ids to end that
way, particularly in Tier 4. "No sources found after searching X, Y, Z" is a
useful result and I would rather read it than a filled-in guess.

**Reasoned inference is wanted** for identity and lineage — which id is a
rebrand of what, whether a `-free` id shares weights with its paid counterpart,
whether a codename matches a known release — but label every such call
`INFERRED` and give the evidence chain that supports it.

**Grade source quality** per claim: primary vendor documentation, independent
benchmark leaderboard, single secondary source, or vendor marketing.

**Where sources conflict** — especially vendor-claimed versus independently
measured benchmarks, or differing context windows — report both numbers and say
which you trust and why. Do not silently pick one.

Worth checking beyond the vendors' own pages: OpenRouter model pages (context
windows, real throughput, pricing), Artificial Analysis, the Aider and
SWE-bench leaderboards, LMArena, Hugging Face model cards for the open-weight
ones, the `opencode` GitHub repo and its discussions, and OpenCode's own
changelog. Today's date is 2026-09-10; flag anything recent enough that the data
is still thin.

## Output format

**Part 1 — the table.** One row per model id, grouped by tier:

`id | upstream model | context | max out | SWE-bench Verified | tokens/sec |
tool-calling reliability | jurisdiction | $/Mtok in-out | confidence | notes`

The `confidence` column is your own read on how well-sourced that row is —
high / medium / low / none — so I can see at a glance which rows to distrust.

**Part 2 — per-model detail.** A short section per model carrying the things a
table cell cannot hold: the citations, the benchmark reporting dates, the
`INFERRED` evidence chains, the specific failure reports, source-quality grades,
and any conflict between sources. Be as long as the evidence warrants; this is
the part I read once, carefully. Models where you found nothing get one line
saying so and what you searched.

**Part 3 — analysis.** Prose covering:

- **The three you would pick for unattended agentic coding.** Rank on
  tool-calling reliability and structured-output stability **first**, with
  benchmark scores only as a tiebreaker. A model that emits malformed tool calls
  is useless regardless of its SWE-bench score, while an average model that
  never breaks the loop is valuable. **State the ranking basis explicitly so I
  can audit it**, including which models you dropped for reliability reasons
  despite good scores.
- **Which to avoid outright**, and why.
- **Any model where the free and paid variants would route differently.** Quota
  pressure is the usual reason to reach for the free tier, so knowing what
  capability is given up is the actual decision.
- **Anywhere marketing and independent numbers disagree.**
- **What you would want to test directly**, given that I have a working harness
  for probing these. If the public record cannot settle something that matters,
  tell me what experiment would.

**Part 4 — the routing verdict, under 200 words, hard limit.** A compact
prefer / avoid / free-tier-viable summary. This one paragraph gets pasted into a
tool docstring that is loaded on every session, so it is the only place where
length has a running cost. Everything above it can be as long as it needs to be.

## Already established by direct testing

Do not re-derive these. Do correct me if your sources disagree.

Free-tier agentic write-a-file probe, 120s cap, one dispatch at a time:

| model | result |
|---|---|
| `ling-3.0-flash-fin-free` | PASS, 11.5s |
| `muse-spark-1.3-contributor-free` | PASS, 15.3s |
| `big-pickle` | PASS, 16.0s |
| `muse-spark-1.2-contributor-free` | PASS, 17.1s |
| `mimo-v2.5-free` | PASS; the only free model seen to follow a multi-field brief with shell substitutions |
| `nemotron-3.5-lightning-free` | FAIL — emitted malformed tool-call syntax, wrote nothing |
| `nemotron-3-ultra-free` | FAIL — created the output directory, then produced nothing for 120s and was killed. Hung or merely slow is unresolved |

Also observed: `qwen3.8-max` hit a 5-hour usage limit and the CLI hung rather
than exiting cleanly.
