# Example delegation spec — build task

A worked example of the brief `dispatch_opencode` / `dispatch_agy` is meant to receive.
It is a real task against this repo, not a placeholder, so the structure can be
judged against something that actually has to work. Copy it and replace the
body; the section order is the part worth keeping.

Each `<!-- why -->` comment explains what the section is defending against.
Delete them in a real spec.

---

<!-- why: the routing decision belongs IN the spec, not in the chat message that
     dispatched it. When a run fails you need to know which model ran it, and
     stdout may be gone. Dating it matters because MODEL-ROSTER.md goes stale. -->

## Delegate

| | |
|---|---|
| **Model** | `opencode-go/minimax-m3` |
| **Fallback** | `opencode-go/qwen3.8-flash` (quota), `opencode/mimo-v2.5-free` (quota exhausted) |
| **Chosen** | 2026-09-10, against MODEL-ROSTER.md of the same date |
| **Basis** | Roster's top-ranked reliable model that is not opt-in gated. ~24h / 1,959 tool calls unattended is the strongest long-horizon evidence in the roster, and 100 tok/s keeps it clear of the wall-clock timeout. `deepseek-v4-pro` ranks first overall but is China-hosted and rejected without a workspace opt-in, so it is not usable here. |
| **Do not use** | `kimi-k2.6`, `glm-5.3-flash`, `omen-alpha`, either nemotron — see the roster's avoid table |

If you are not the model named above, **stop and say so in your first line of
output.** A silent substitution invalidates the run, because the whole point of
recording the model is to be able to attribute the result to it later.

<!-- why: stdout is captured to a file and survives a dropped transport, but it
     is an undifferentiated stream, and the report truncates it to a tail. The
     .log is the only signal that says which PHASE the work reached. -->

## Deliverable

- Write your findings and a summary of what you changed to
  **`.agent-runs/probe-drift-detect.md`**.
- Append one line per phase to **`.agent-runs/probe-drift-detect.log`** as you
  go — start, each file touched, each test run, done. Write it as you work, not
  at the end.
- Do **not** put the deliverable in stdout alone. Assume the reader will only
  ever see a tail of it.
- Code changes go in the repo as normal edits. The `.md` is your report on them,
  not a substitute for making them.

<!-- why: state the outcome, not the implementation. A delegate given only a
     list of edits will make exactly those edits and miss the point when one of
     them turns out to be wrong. -->

## Goal

The free-tier probe harness currently records PASS when the delegate exits 0 and
the expected output file exists. That misses the failure mode the roster calls
the hardest to detect automatically: a model that **returns a plan or a
description of intended changes instead of editing files**, or that emits a
tool call the CLI parses as plain text. Both produce a confident-looking success
with nothing — or the wrong thing — on disk.

Make the harness able to tell those apart from a real pass.

<!-- why: this is the section that prevents the delegate from "helpfully"
     rewriting things you did not ask about. Out-of-scope items must name the
     obvious-but-wrong suggestion explicitly, or you will get it anyway. -->

## Scope

In scope:

- The probe script and its result schema.
- A content assertion, not just an existence assertion, on the probe's expected
  output file.
- Recording enough of the delegate's stdout to classify a failure after the
  fact.

Out of scope, and **deliberately** so — do not implement these and do not
recommend them in your report:

- Changing the 120s cap or making the harness concurrent. One dispatch at a
  time is a hard constraint: models sharing a provider share a quota pool, so a
  second call can be the reason the first one dies.
- Adding new models to the probe list.
- Any change to `opencode_mcp_server.py` or `agy_mcp_server.py`. The harness is
  a test tool; the servers are shipped code and are not in this task.
- Retry logic. A probe that retries cannot distinguish flaky from broken, which
  is the only thing it exists to measure.

<!-- why: name the existing convention and point at the file. A delegate with no
     precedent invents one, and you get a second style in the same repo. -->

## Pattern to follow

The existing harness writes one JSON object per model to `results.json` with
`model`, `elapsed_s`, `exit_code`, and `result`. Extend that shape; do not
replace it. `result` is currently `PASS` / `FAIL` / `TIMEOUT` — add the new
classifications as additional values of the same field rather than a parallel
field, so old result files stay readable.

Timeout handling is already correct and is worth reading before you touch
anything: the harness starts each probe with `start_new_session=True` and kills
it with `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)`. A plain
`proc.kill()` leaves the CLI's own children alive as orphans. Preserve that.

<!-- why: every rule gets its reason. A constraint without a reason reads as
     arbitrary and gets optimized away by a model trying to be helpful. -->

## Hard constraints

- **Do not run the probe against paid models.** The `opencode-go/` tier draws on
  a metered quota; `qwen3.8-max` in particular has a $15/month cap that a probe
  sweep can eat. Free-tier ids only.
- **One dispatch at a time**, as above.
- **Do not commit.** Leave changes in the working tree. Do not run `git commit`,
  `push`, `reset`, `checkout`, `stash`, or `add`. Read-only git is fine and
  encouraged (`git log`, `git diff`, `git show`, `git status`).
- **Do not edit `MODEL-ROSTER.md`.** It records researched evidence with a
  sourcing date. If your work produces a finding that belongs in it, put the
  finding in your report and say which row it changes.
- Fresh directory per probe run, as the harness already does. A probe that finds
  a file left by the previous model's run is worthless.

<!-- why: "definition of done" is what lets the delegate self-check before
     returning, and what lets Claude verify without re-deriving intent. -->

## Definition of done

1. A probe against a model that writes the file correctly still reports `PASS`.
2. A probe against a model that exits 0 having written nothing reports something
   distinguishable from both `PASS` and a timeout.
3. A probe against a model that writes the file with wrong or placeholder
   content reports as a failure, with the actual content captured somewhere the
   report can quote.
4. `nemotron-3.5-lightning-free` — a known-bad model, documented upstream as
   dropping the closing brace in tool JSON — is classified as a failure with a
   reason attached, not a bare `FAIL`.
5. Your report states which of 1–4 you verified by actually running it and which
   you only reasoned about. Do not present the second as the first.

<!-- why: an unattended delegate that hits an ambiguity will guess, and a guess
     is indistinguishable from a decision in the output. Give it somewhere to
     put the ambiguity instead. -->

## When blocked

Do not guess and do not stop silently. If something here is contradictory,
underspecified, or turns out to be wrong once you read the code:

- Implement the rest of the task in full.
- Write what you hit, what you assumed, and why, into your report under a
  heading called **Assumptions and blockers**.
- If a constraint above is the thing blocking you, say that explicitly rather
  than working around it. A constraint I have to re-litigate is cheaper than a
  workaround I have to discover.
