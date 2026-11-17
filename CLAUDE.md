
<!-- BACKLOG.MD GUIDELINES START -->
<!-- backlog.md-instructions-version: 1.51.0 -->
<CRITICAL_INSTRUCTION>

## Backlog.md Workflow

This project uses Backlog.md for task and project management.

**At the beginning of each conversation in this project, run `backlog instructions overview` before answering or taking action. Re-read it only if you have not read it yet in the current conversation.**

Use the overview to decide whether to search, read, create, or update Backlog tasks.

Before task lifecycle actions, read the matching detailed guide:
- `backlog instructions task-creation` before creating or splitting tasks
- `backlog instructions task-execution` before planning, changing status or assignee, adding a plan or implementation notes, or implementing task work
- `backlog instructions task-finalization` before checking acceptance criteria, writing final summaries, or moving tasks to terminal statuses

Use `backlog <command> --help` before running unfamiliar commands. Help shows options, fields, and examples.

Do not edit Backlog task, draft, document, decision, or milestone markdown files directly. Use the `backlog` CLI so metadata, relationships, and history stay consistent.

</CRITICAL_INSTRUCTION>
<!-- BACKLOG.MD GUIDELINES END -->

<!-- working-standard START -->
## Working method

Arthur's `working-standard` skill applies to every session in this project. It's manual-only, so read `~/.claude/skills/working-standard/SKILL.md` at session start (and `references/lifecycle.md` for phase work) unless he has run `/working-standard`.
<!-- working-standard END -->

## Project rules

The `agent-delegation` Claude Code plugin: three MCP servers (`agy_mcp_server.py`, `opencode_mcp_server.py`, `codex_mcp_server.py`) that dispatch work to the agy, opencode and Codex CLIs, plus `quota_sources.py` and the `gemini-ask` skill. Public GitHub repo (`artcar12/agent-delegation-mcp`).

No doc-1 for routine work (a big project under `references/lifecycle.md` creates one as that file says). `README.md` is the design and user documentation and `MODEL-ROSTER.md` the model routing reference: a design change updates README and records the why as a `backlog decision`. Without a doc-1, the "default (change if wrong)" note for a default you pick goes in a `backlog decision` with status accepted; README documents the resulting behaviour without that note. README sections 5 and 6 (state files, handoff log, plan-file naming) are advice to the plugin's users; this repo keeps its state in Backlog and names delegate files per working-standard.

Gate (all must pass before a merge):

```bash
python3 -m unittest discover -s test   # ~4 min; run in the background
claude plugin validate .               # passes; CLAUDE.md-at-plugin-root and missing-icon warnings are expected
```

Tests use fake CLIs and invented fixtures only.

The three servers each carry their own copy of the run store (dispatch, `check_run`, `cancel_run`, `list_runs`, `delegation_status`, liveness, idle limit) and the same `quota_status`; only `quota_sources.py` is shared. A change to that behaviour goes into all three. Fail-fast patterns: each server has its own `FATAL_PATTERNS` and an identical `_BUILTIN_FATAL` table keyed by CLI; a pattern change goes into that server's `FATAL_PATTERNS` and into `_BUILTIN_FATAL` in all three, which `test/test_run_store.py` checks.

Releases: any shipped change to a file the installed plugin reads (servers, `quota_sources.py`, skills, manifests, `MODEL-ROSTER.md`) bumps `version` in `.claude-plugin/plugin.json` and the matching entry in `.claude-plugin/marketplace.json` together (servers read the version from `plugin.json` at runtime), and adds a `README.md` section 9 entry when users have something to act on. Tags are `agent-delegation--v<version>`; when Arthur asks for one, make it on main after the merge is pushed, with a clean tree (`claude plugin tag . --push`). The tracked root `.mcp.json` starts the working-tree servers in any session opened in this repo or a worktree, as `agy-wrapper`, `opencode-wrapper` and `codex-wrapper`, next to the installed plugin's `plugin:agent-delegation:*` servers; both report the version in `plugin.json`, so tell them apart by the tool prefix, and dispatch real work through the `plugin:` ones while a branch changes server code. The installed plugin comes from GitHub, so a release reaches it only once merged and pushed: `claude plugin marketplace update agent-delegation-mcp`, `claude plugin update agent-delegation`, then `/reload-plugins`.
