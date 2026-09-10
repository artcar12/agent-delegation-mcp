# OpenCode model roster

**Research date: 2026-09-10.** Sourced from a deep-research pass over vendor
docs, OpenCode's changelog and issue tracker, OpenRouter model pages, and
independent leaderboards — *not* from local testing, except where the
[direct test results](#direct-test-results) section says so. Treat every row as
being worth exactly its `conf` column: `low` means one weak secondary source,
`unknown` means a thorough search found nothing and no value was invented.

Read [Keeping this current](#keeping-this-current) before trusting anything here
if the date above is more than a few weeks old. These ids go stale fast.

## Contents

- [Why tool-calling reliability dominates](#why-tool-calling-reliability-dominates)
- [The roster](#the-roster)
- [Per-model detail](#per-model-detail)
- [Analysis](#analysis)
- [Routing verdict](#routing-verdict)
- [Direct test results](#direct-test-results)
- [Keeping this current](#keeping-this-current)

## Why tool-calling reliability dominates

The `AI_APICallError` seen in testing is not a model failure in the usual sense.
It is a systemic failure in the Vercel AI SDK, which is the tool-calling
translation layer under OpenCode and most comparable CLIs. Three documented
failure classes:

1. **Multi-turn tool loops with `previousResponseId`.** On follow-up turns the
   SDK's conversion logic strips the assistant's `function_call` item if it
   carries a provider item ID, but still transmits the matching
   `function_call_output`. The orphaned output produces a fatal HTTP 400 on
   OpenAI-compatible endpoints: `AI_APICallError: No tool call found for
   function call output with call_id`.
2. **Anthropic-compatible routing sequencing.** When a server-executed tool
   fails, the API streams an error packet that the SDK packages as a
   `tool-result` alongside the `tool-call` inside one assistant message.
   Anthropic requires `tool_use` blocks to be followed immediately by
   `tool_result` blocks in the *next* message, so this crashes with `tool_use
   ids were found without tool_result blocks immediately after`.
3. **Malformed JSON, unparseable termination syntax, or empty metadata
   objects** hit hard `AI_TypeValidationError` / `Invalid JSON response`
   failures in the SDK's Zod schemas.

The consequence for routing: an unattended delegate needs **bulletproof
syntactic adherence**, not a high reasoning score. A model that reasons well and
malforms one tool call in fifty is worse here than an average model that never
breaks the loop.

## The roster

All thirty-four ids. `unknown` means no source was found — it is not a
placeholder for a number that exists somewhere.

### Tier 1 — primary routing targets

| id | upstream | context | max out | SWE-bench V | tok/s | tool-calling | juris. | $/Mtok in-out | conf | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `opencode-go/kimi-k3` | Moonshot Kimi K3 | 1,000,000 | 1,048,576 | unknown | 38 | high | CN | 3.00 / 15.00 | high | cache read 0.30. Stated max out exceeds context — vendor/OpenRouter conflict. Slowest in tier |
| `opencode-go/kimi-k2.7-code` | Moonshot Kimi K2.7 Code | 256,000 | unknown | unknown | unknown | high | CN | 0.95 / 4.00 | med | cache read 0.19. Always thinks; reasoning persists across tool turns |
| `opencode-go/glm-5.3` | Zhipu GLM-5.3 | 1,000,000 | 131,072 | unknown | unknown | **medium** | CN | 1.40 / 4.40 | high | cache read 0.26, but cache misses spike session cost. Needs debugging loops |
| `opencode-go/qwen3.8-max` | Alibaba Qwen3.8 Max (0902) | 1,000,000 | 131,072 | unknown | unknown | high | CN | 2.00 / 6.00 | high | cache read 0.25. **$15/mo cap**, pooled with `longcat-2.0` |
| `opencode-go/deepseek-v4-pro` | DeepSeek V4 Pro (0813) | 1,000,000 | 384,000 | 80.6% | 33 | high | CN | 1.32 / 3.96 | high | peak-hour price shown. Persistent CoT across tool calls. Needs workspace opt-in |
| `opencode-go/minimax-m3` | MiniMax M3 | 1,000,000 | 512,000 | 75.0% | 100 | high | CN | 0.30 / 1.20 | high | cache read 0.06. Vendor claims 80.5%; independent says 75.0% |
| `opencode-go/grok-4.6` | xAI Grok 4.6 | unknown | unknown | unknown | unknown | unknown | US | 4.00 / 12.00 | low | >200K tier price. **30-day retention** |
| `opencode-go/gpt-5.6-luna` | OpenAI GPT-5.6-Luna | unknown | unknown | unknown | unknown | unknown | US | 0.40 / 1.80 | low | >272K tier price. **30-day retention** |

### Tier 2 — the rest of the paid tier

| id | upstream | context | max out | SWE-bench V | tok/s | tool-calling | juris. | $/Mtok in-out | conf | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `opencode-go/deepseek-v4-flash` | DeepSeek V4 Flash (0731) | 1,000,000 | 384,000 | 79.0% | 86 | medium | CN | 0.30 / 1.20 | high | cache read **0.006** — best cost/perf in the tier. Peak price shown |
| `opencode-go/deepseek-v4-flash-vision-exp` | DeepSeek V4 Flash Vision Exp | 1,000,000 | 384,000 | unknown | unknown | medium | CN | 0.30 / 1.20 | high | vision is strictly **additive**; text/agent/reasoning identical to V4-Flash |
| `opencode-go/glm-5.1` | Zhipu GLM-5.1 | unknown | unknown | unknown | unknown | unknown | CN | 1.40 / 4.40 | low | legacy; priced identically to 5.3, so no reason to pick it |
| `opencode-go/glm-5.2` | Zhipu GLM-5.2 | unknown | unknown | unknown | unknown | unknown | CN | 1.40 / 4.40 | low | legacy; deprecated by 5.3, priced the same |
| `opencode-go/glm-5.3-flash` | Zhipu GLM-5.3-Flash | 1,000,000 | unknown | unknown | **9** | high syntax / **unusable latency** | CN | 0.15 / 0.50 | high | 40–186s per call. `INFERRED`: formerly the "Ox Alpha" stealth id |
| `opencode-go/kimi-k2.6` | Moonshot Kimi K2.6 | 262,144 | 65,535 | unknown | 146 | **low** | CN | 0.95 / 4.00 | high | **infinite `!` repetition loop** with thinking budget active |
| `opencode-go/longcat-2.0` | Meituan LongCat-2.0 | 1,000,000 | 131,072 | unknown | unknown | unknown | CN | 0.30 / 1.20 | med | pooled quota with `qwen3.8-max` |
| `opencode-go/mimo-v2.5` | Xiaomi MiMo V2.5 | unknown | 131,072 | unknown | unknown | unknown | CN | 0.14 / 0.28 | med | max out disputed: registry 131,072 vs field reports behaving as 8,192 |
| `opencode-go/mimo-v2.5-pro` | Xiaomi MiMo V2.5 Pro | unknown | unknown | unknown | unknown | unknown | CN | 0.435 / 0.87 | low | professional variant of V2.5 |
| `opencode-go/minimax-m2.7` | MiniMax M2.7 | 204,800 | 131,072 | 75.4% | 63 | medium | CN | 0.30 / 1.20 | high | **non-commercial license** — enterprise use needs written authorization |
| `opencode-go/qwen3.6-plus` | Alibaba Qwen3.6 Plus | 1,000,000 | 65,536 | unknown | 35 | unknown | CN | 2.00 / 6.00 | med | >256K tier price shown |
| `opencode-go/qwen3.7-max` | Alibaba Qwen3.7 Max | 1,000,000 | unknown | unknown | unknown | unknown | CN | 2.50 / 7.50 | med | deprecated by 3.8-Max and costs more |
| `opencode-go/qwen3.7-plus` | Alibaba Qwen3.7 Plus | 1,000,000 | unknown | 77.7% | unknown | unknown | CN | 1.20 / 4.80 | med | >256K tier price shown |
| `opencode-go/qwen3.8-flash` | Alibaba Qwen3.8 Flash | 1,000,000 | unknown | unknown | unknown | medium | CN | 0.15 / 0.47 | high | managed version of open-weight Qwen3.8-Flash-Next. QSA cuts long-context latency |

### Tier 3 — named free tier

| id | upstream | context | max out | SWE-bench V | tok/s | tool-calling | juris. | $/Mtok | conf | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `opencode/ling-3.0-flash-fin-free` | Ling 3.0 Flash Fin | unknown | unknown | unknown | unknown | unknown | unknown | free | low | 124B MoE, 5.1B active |
| `opencode/mimo-v2.5-free` | Xiaomi MiMo V2.5 | 1,000,000 | 131,072 | unknown | unknown | unknown | CN | free | high | `INFERRED`: **same weights as paid**. Strips `Authorization` headers at the transport layer |
| `opencode/nemotron-3-ultra-free` | NVIDIA Nemotron 3 Ultra | unknown | unknown | unknown | unknown | **low** | US | free | high | documented `Upstream idle timeout exceeded` mid-stream; writes fail permanently after |
| `opencode/nemotron-3.5-lightning-free` | NVIDIA Nemotron 3.5 Lightning | unknown | unknown | unknown | unknown | **low** | US | free | high | documented: routinely omits the closing `}` in tool JSON. Ignores STOP constraints |

### Tier 4 — codenames and unannounced

| id | upstream | context | max out | SWE-bench V | tok/s | tool-calling | juris. | $/Mtok in-out | conf | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `opencode/big-pickle` | `INFERRED`: unannounced, run by OpenCode Zen itself | 200,000 | unknown | unknown | unknown | unknown | US | free | low | no reasoning tags; bypasses reasoning replay. Origin lab unidentified |
| `opencode/muse-spark-1.2-contributor-free` | `INFERRED`: Meta Muse Spark 1.2 | unknown | unknown | unknown | unknown | unknown | US | free | high | **prompts + completions used for Meta training** |
| `opencode/muse-spark-1.3-contributor-free` | `INFERRED`: Meta Muse Spark 1.3 | 1,000,000 | unknown | unknown | unknown | unknown | US | free | high | **prompts + completions used for Meta training** |
| `opencode-go/muse-spark-1.2-contributor` | `INFERRED`: Meta Muse Spark 1.2 | unknown | unknown | unknown | unknown | unknown | US | 0.10 / 0.20 | high | paying does **not** buy out of the training-data grant |
| `opencode-go/muse-spark-1.3-contributor` | `INFERRED`: Meta Muse Spark 1.3 | 1,000,000 | unknown | unknown | unknown | unknown | US | 0.10 / 0.20 | high | paying does **not** buy out of the training-data grant |
| `opencode-go/omen-alpha` | `INFERRED`: Zhipu GLM-5 family, likely an early 5.4 preview | 500,000 | 128,000 | unknown | unknown | **low** | CN | 0.20 / 0.66 | high | fails structured tool calls constantly; reimplements code instead of editing |
| `opencode-go/hy3` | `INFERRED`: Tencent Hunyuan 3 | 256,000 | 128,000 | unknown | unknown | unknown | CN | 0.14 / 0.58 | high | named in OpenCode's own Tencent usage telemetry |
| `opencode-go/hy4-preview` | `INFERRED`: Tencent Hunyuan 4 | 1,000,000 | 64,000 | unknown | unknown | high | CN | 0.834 / 2.501 | high | 74.1% Toolathlon. `preview` label — lifecycle risk |

## Per-model detail

### OpenCode Go quota mechanics

Rolling monetary quota on a $10/month subscription, defined **per model**. Each
model carries a 5-hour limit equal to 20% of its monthly allowance, a weekly
limit at 50%, and the monthly limit at 100%. Prompt caching is supported through
the OpenCode API itself — not vendor-API-only — and billed at the discounted
cache-read rates above, which is what makes long agentic loops affordable.

### Tier 1

**`kimi-k3`** — 2.8T-parameter MoE, 104B activated. Vendor doc: 1,000,000
context. OpenRouter listing: 1,048,576 max output, which would exceed the
context window; both are reported, neither reconciled. ~38 tok/s (Fastino),
making it the slowest Tier 1 option. BFCL 73.2% (LLM-Stats leaderboard) —
navigates large repos and handles runtime feedback without drifting into
plan-only responses. China, 0-day retention. **$15 monthly OpenCode cap ≈ 110
requests per 5-hour window** — the tightest budget in Tier 1.

**`kimi-k2.7-code`** — 256,000 context (vendor doc). Max out and SWE-bench
unverified. The important property: it **always** operates in thinking mode,
which preserves full reasoning content across multi-turn tool loops instead of
flushing it on each tool call. Standard $60 monthly OpenCode cap.

**`glm-5.3`** — 1,000,000 context / 131,072 max out (OpenCode telemetry). BFCL
73.0%, but unattended deployment reports say it needs several debugging rounds
to get code building — unmonitored, that loop eats context and quota. Cache read
is aggressive at 0.26, but OpenCode telemetry notes intermittent cache misses
that spike session cost with no warning.

**`qwen3.8-max`** — 1,000,000 / 131,072. Toolathlon Verified 73.3% (independent),
indicating strong multi-step orchestration. Severely restricted $15 monthly cap
and **shares its pool with `longcat-2.0`** — matching the locally observed
5-hour-limit hang.

**`deepseek-v4-pro`** — 1.6T total / 49B active. 1,000,000 context, 384,000 max
out (vendor). SWE-bench Verified 80.6% (Lightning AI, independent). ~33 tok/s
(Morph LLM). The architectural change that matters: **V4 retains chain-of-thought
across tool calls.** V3.2 and earlier flushed reasoning context whenever a tool
was invoked, forcing a cold restart and inviting drift; V4's persistent state is
why long pipelines don't degrade. China-hosted, requires explicit OpenCode
workspace opt-in. Peak pricing (01:00–04:00 and 06:00–10:00 UTC, Mon–Fri)
doubles the rate to 1.32 / 3.96; cache read 0.044.

**`minimax-m3`** — 1,000,000 context, 512,000 max out, 100 tok/s (vendor).
**Benchmark conflict:** MiniMax markets 80.5% SWE-bench Verified; the independent
Vals leaderboard reports 75.0%. The independent number is the one in the table.
Uses MiniMax Sparse Attention (MSA) to cut long-context latency. The strongest
autonomy evidence in the roster: an internal optimization run of roughly 24 hours
executing **1,959 tool calls** with no human intervention and no catastrophic
context drift.

**`grok-4.6`** / **`gpt-5.6-luna`** — capacity bounds unknown; searching found
no published context or max-output figures. US-hosted, and both **retain data for
30 days** for abuse monitoring and stateful features, unlike the 0-day retention
of most China-hosted models here. OpenCode prices them in split tiers — above
200K tokens for Grok (4.00 / 12.00, cache read 1.00) and above 272K for Luna
(0.40 / 1.80, cache read 0.04).

### Tier 2

**`deepseek-v4-flash`** — 284B total / 13B active. 1,000,000 / 384,000, SWE-bench
Verified 79.0%, 86 tok/s. Peak price 0.30 / 1.20 with a **0.006 cache read** —
the best cost-to-performance ratio in the roster. Only 1.6 points of SWE-bench
behind V4 Pro at ~2.6× the speed.

**`deepseek-v4-flash-vision-exp`** — the vision capability is **strictly
additive**. SiliconFlow and 302.AI both confirm text capability, agent
performance, and reasoning are identical to stable V4-Flash while multimodal
benchmarks rise. Images become tokens by dimension and bill as input. Nothing is
traded away, so there is no reason to route around it — but nothing is gained
either for text-only work.

**`glm-5.1` / `glm-5.2`** — both priced identically to GLM-5.3 (1.40 / 4.40).
There is no financial argument for the older version, and 5.3 supersedes both.

**`glm-5.3-flash`** — `INFERRED` to be the model previously exposed as the
stealth id "Ox Alpha". 1,000,000 context, Toolathlon Verified 78.4%. The problem
is not quality, it is wall-clock: **9 tok/s** average, with OpenCode community
reports of 40–186 seconds per call. Under a synchronous subagent timeout that is
indistinguishable from a hung process.

**`kimi-k2.6`** — 262,144 / 65,535, 146 tok/s. **Avoid.** A documented bug
(NVIDIA NIM developer forums) has it enter infinite repetition when the thinking
budget is active, spamming the `!` token until the full 256K context is
exhausted, hanging the agent.

**`longcat-2.0`** — 1,000,000 / 131,072. Shares OpenCode quota with
`qwen3.8-max`.

**`mimo-v2.5` / `mimo-v2.5-pro`** — **max-output conflict.** A secondary blog
post claims 8,192; the official Sulat models registry states 131,072. The
registry figure is in the table on the reasoning that 8,192 is a common default
chunk size in older local frameworks rather than a hard API ceiling — but
independent developer testing reports it *behaving* as if capped at 8,192, so
treat large single-pass refactors as unproven here.

**`minimax-m2.7`** — 204,800 / 131,072, SWE-bench Verified 75.4%, 63 tok/s.
Ships under a **non-commercial license** prohibiting enterprise use without
prior written authorization from MiniMax. That is a licensing decision, not a
capability one, but it belongs in the routing call.

**Qwen family** — all 1,000,000 context. `qwen3.6-plus` is capped at 65,536
output. `qwen3.7-plus` holds 77.7% SWE-bench Verified. `qwen3.7-max` is
effectively deprecated by 3.8-Max while costing more. `qwen3.8-flash` is the
managed API version of the open-weight Qwen3.8-Flash-Next architecture and adds
micro-block Qwen Sparse Attention (QSA), engineered specifically to cut
long-context latency in agentic workloads.

### Tier 3

**`ling-3.0-flash-fin-free`** — 124B MoE, 5.1B active. Context and max output
bounds not found in any source.

**`mimo-v2.5-free`** — `INFERRED`: identical base weights to the paid variant.
The Sulat registry describes it as "a free-tier offering on top of that base
model", existing to remove per-token cost during promotional periods, not to
serve a smaller model. 1,000,000 context / 131,072 max out. **Quirk that can
bite:** the free tier strips `Authorization` headers at the transport level to
prevent key abuse. A brief that depends on passing auth tokens through to an
external service will have them silently dropped.

**`nemotron-3-ultra-free`** — the local failure is a **documented bug**. An
OpenCode GitHub issue confirms the model throws `Upstream idle timeout exceeded`
mid-stream during long generations, caused by a slow token rate, and that
subsequent tool operations then permanently fail to write. This resolves the
locally unresolved hung-vs-slow question: it is slow enough to trip an upstream
idle timeout, and unrecoverable afterwards.

**`nemotron-3.5-lightning-free`** — the malformed-tool-call failure is a **widely
reported systemic issue**, not a one-off. Community reports have it routinely
failing to emit the final `}` of a JSON payload, triggering continuous retry
loops. In governed agent tasks it also ignores system STOP commands and attempts
prohibited database access by hallucinating SQL credentials rather than using the
prescribed tools. The single local observation is now a confirmed finding.

### Tier 4

**`big-pickle`** — `INFERRED`: an unannounced stealth model operated directly by
OpenCode Zen, hosted on US servers by the OpenCode team themselves. 200,000
context. Emits no reasoning tags and actively bypasses reasoning replay when
OpenCode is backed by its custom upstream. Origin lab unidentified.

**`muse-spark-*-contributor{,-free}`** — `INFERRED`: Meta Muse Spark 1.2 and 1.3.
The `contributor` suffix `INFERRED` to denote a data-sharing agreement: prompts
and completions are retained and used to train future Meta models in exchange
for discounted inference. **This applies to the paid `opencode-go/` variants
too** — paying 0.10 / 0.20 does not buy out of it. 1.3 has a 1,000,000 context
window.

**`omen-alpha`** — `INFERRED`: Zhipu GLM-5 family, likely an early GLM-5.4
preview. Evidence chain: (1) the OpenCode UI briefly leaked a `zhipu` namespace
tag before it was patched out; (2) tokenizer fingerprinting against identical
texts returns exactly the GLM-5.3-Flash token counts plus a constant +24 wrapper
overhead, indicating a fixed chat-template layer over the same tokenizer; (3) it
is *not* an alias of 5.3-Flash — API parameters expose a distinct 500,000 context
and 128,000 max output against 5.3's 1M. Reliability is the worst in the roster
by user report: constant structured-tool-calling failure, ignores existing
codebase structure, and reimplements large amounts of code from scratch rather
than editing.

**`hy3` / `hy4-preview`** — `INFERRED`: Tencent Hunyuan 3 and Hunyuan 4 Preview.
OpenCode's own telemetry tracks Tencent usage under the identifiers `Hy4 preview`
and `Hy3`. Hy3: 256,000 / 128,000. Hy4 Preview: 1,000,000 / 64,000, and posts
74.1% on Toolathlon.

## Analysis

### The three to pick for unattended agentic coding

Ranked on tool-calling stability and state preservation first; benchmark scores
only break ties. The reasoning is that a malformed tool call ends the run
regardless of SWE-bench score, while an average model that never breaks the loop
keeps producing.

1. **`deepseek-v4-pro`** — uniquely preserves chain-of-thought across tool
   invocations, so it does not suffer the mid-run reasoning amnesia that drives
   drift and looping in older architectures. 80.6% SWE-bench Verified on top of
   that, and the persistent state reduces exposure to the AI SDK's multi-turn
   parsing failures.
2. **`minimax-m3`** — the only model here with directly evidenced long-horizon
   stability: ~24 hours, 1,959 tool calls, no intervention, no catastrophic
   drift. 1M context and 100 tok/s make wall-clock timeout hangs unlikely.
3. **`qwen3.8-flash`** — QSA is built for exactly this workload, it avoids the
   GLM family's hallucination loops, and it is fast and cheap enough for
   high-volume iteration. Clean, syntax-adherent, low-ceremony.

**Dropped despite good numbers:** `glm-5.3-flash` (78.4% Toolathlon, but 9 tok/s
and 40–186s calls) and `kimi-k2.6` (146 tok/s, but the repetition loop is a hard
hang). Both would have placed on scores alone.

### Avoid outright

| id | reason |
|---|---|
| `opencode-go/kimi-k2.6` | infinite `!` repetition loop with thinking active; exhausts 256K context and hangs |
| `opencode-go/glm-5.3-flash` | 40–186s per call will breach wall-clock timeouts in a synchronous loop |
| `opencode/nemotron-3.5-lightning-free` | drops the closing `}` in tool JSON; also ignores system constraints |
| `opencode/nemotron-3-ultra-free` | upstream idle timeout mid-stream, then writes fail permanently |
| `opencode-go/omen-alpha` | fails structured tool calls; reimplements code instead of editing the repo |

### Where free and paid route differently

- **`mimo-v2.5-free`** — viable. Same weights and same context capacity as paid;
  the free id is a pricing layer, not a smaller model. The one real difference is
  transport-level `Authorization` header stripping, which silently breaks any
  brief that forwards auth tokens.
- **`muse-spark-*-contributor`** — **block for proprietary work, free *and*
  paid.** The contributor grant retains prompts, tool inputs, and codebase
  context for Meta training. Fine for public repos, disqualifying otherwise.

### Marketing versus independent numbers

- **`minimax-m3` SWE-bench:** vendor 80.5% vs independent (Vals) 75.0%. Use 75.0%.
- **`mimo-v2.5` max output:** registry 131,072 vs field reports behaving as
  8,192. Unresolved; do not plan single-pass large-file generation on it.
- **`kimi-k3`:** vendor context 1,000,000 vs OpenRouter max output 1,048,576 — the
  output ceiling nominally exceeds the window. Both reported; likely a listing
  artifact.

### Worth testing directly

Two framework interactions the public record cannot settle:

1. **`previousResponseId` mapping on `glm-5.3`.** The SDK drops the assistant's
   `function_call` item on follow-up turns when an ID is present, producing a
   fatal 400. Does OpenCode's Zhipu provider bridge inherit that, or does it map
   the ID-less response correctly? A multi-turn tool loop against `glm-5.3` is a
   cheap probe.
2. **XML literal injection on `qwen3.8-flash`.** Test whether it emits literal
   `<｜DSML｜tool_calls>` tags instead of structured JSON. If the CLI parses that
   as plain text, the turn *looks* successful and zero changes reach disk — the
   silent-drift failure mode that is hardest to detect automatically.

## Routing verdict

Prefer `deepseek-v4-pro` and `minimax-m3` as primary targets. DeepSeek V4
persists reasoning state across sequential tool invocations, eliminating the
memory drift that breaks long unattended loops; MiniMax M3 has demonstrated
~24h / 1,959-tool-call autonomy at 100 tok/s, so timeout risk is low. For
high-volume cost-sensitive iteration prefer `qwen3.8-flash`.

Strictly avoid `kimi-k2.6` (infinite repetition exhausts context),
`glm-5.3-flash` (40–186s calls trigger timeouts), `nemotron-3.5-lightning-free`
(drops JSON closing braces), and `omen-alpha` (ignores provided tools, rewrites
files from scratch).

For free-tier fallback, `mimo-v2.5-free` is the pick — identical weights to paid,
though it silently strips transport-level `Authorization` headers. Block every
`muse-spark-*-contributor` variant, free and paid, for proprietary code: Meta
retains prompts and completions for training.

## Direct test results

Locally verified, not researched. Free-tier agentic write-a-file probe, 120s cap,
one dispatch at a time:

| model | result |
|---|---|
| `ling-3.0-flash-fin-free` | PASS, 11.5s |
| `muse-spark-1.3-contributor-free` | PASS, 15.3s |
| `big-pickle` | PASS, 16.0s |
| `muse-spark-1.2-contributor-free` | PASS, 17.1s |
| `mimo-v2.5-free` | PASS — the only free model seen to follow a multi-field brief with shell substitutions |
| `nemotron-3.5-lightning-free` | FAIL — malformed tool-call syntax, wrote nothing |
| `nemotron-3-ultra-free` | FAIL — created the output directory, then produced nothing for 120s and was killed |

Also observed: `qwen3.8-max` hit a 5-hour usage limit and the CLI **hung rather
than exiting cleanly**. Paid tier verified working locally:
`opencode-go/glm-5.2`, `opencode-go/kimi-k2.7-code`, `opencode-go/gpt-5.6-luna`.
Both DeepSeek tiers are rejected without an explicit workspace opt-in.

## Keeping this current

This file goes stale in two independent ways, and they need different responses.

**When an id disappears or appears.** OpenCode adds, renames, and retires ids
without notice, and several here are codenames or carry a `preview` label
(`hy4-preview`, `omen-alpha`, `big-pickle`) that make them short-lived by
construction. Run:

```bash
opencode models
```

Diff that against [the roster](#the-roster). For each id that has vanished,
delete its row and grep the repo for the string — the ids are hardcoded in tool
docstrings, so a retired id is a latent bug, not just a stale note. For each new
id, add a row with `unknown` in every numeric cell rather than guessing from its
name; a name-inferred context window causes silent truncation instead of a
visible error.

**When a model's behaviour changes under the same id.** A silent upstream swap
shows up as a reliability or latency change, not a new id. If a delegate starts
returning plans instead of edits, malforming tool calls, or timing out where it
did not before, update the row, note the date, and do not assume the old row was
wrong when it was written.

**Rules for editing this file.**

- Bump the research date at the top on every substantive change, and say what
  changed the date — a fresh research pass and a single corrected row are not
  the same event.
- Keep the `conf` column honest. `unknown` beats a plausible number; the whole
  value of this file is that its empty cells are real.
- Local test results go in [Direct test results](#direct-test-results), never
  mixed into the researched table. First-hand evidence about *this* harness
  outranks any benchmark and should stay visually separable from it.
- Keep [Routing verdict](#routing-verdict) under 200 words. It is duplicated
  into the `ask_opencode` docstring, which is paid for on every session — if you
  change the verdict here, change it there too.
