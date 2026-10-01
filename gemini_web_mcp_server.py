#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.29,<3"]
# ///
"""
gemini-web-wrapper: exposes the Gemini *web app* to MCP clients as a local
stdio server.

The sibling servers reach Gemini through an API that has no Canvas, no Gems,
no conversation history and no attachments. Those live only in
the logged-in web app. gemini_web.py drives it with Playwright; this file runs
that worker as a blocking child process per call.

Since 1.7.0 this server is a fallback, not the research path. The Gemini web
app is too flaky to automate as a default, so the instructions below tell a
session to hand the user a research prompt instead, and to call gemini_ask
only when the user explicitly asks it to. dispatch_gemini and the run store
behind it were removed then: they existed for long browser runs (Deep
Research, gone since 1.3.0), and gemini_ask's timeout_seconds covers the rest.

Self-contained on purpose: nothing is imported from its sibling servers, and
the dependency is declared inline above, so `uv run --script` on this one file
is a complete way to run it. Every knob is an environment variable; the full
list is in the README.

One browser, one profile, so genuinely one call at a time. Chrome will not
open the same user-data-dir twice.
"""

import json
import os
import shutil
import subprocess
import sys
import threading

# mcp 2.x renamed FastMCP -> MCPServer and dropped the `mcp.server.fastmcp`
# module. Same API: _Server("name"), @mcp.tool(), mcp.run() (stdio default).
try:
    from mcp.server import MCPServer as _Server      # mcp >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x

SERVER_NAME = "gemini-web-wrapper"
BIN_ENV_VAR = "GEMINI_WEB_BIN"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int_env(name: str, default: int, allow_zero: bool = False) -> int:
    """A malformed value must not take the whole server down on import: warn to
    stderr and fall back, rather than letting int() raise (which silently drops
    the tool from the client with no visible cause)."""
    raw = _env(name, str(default))
    try:
        val = int(raw)
        if val < 0 or (val == 0 and not allow_zero):
            raise ValueError("must be positive")
        return val
    except ValueError:
        sys.stderr.write(
            f"[{SERVER_NAME}] ignoring invalid {name}={raw!r}; using {default}\n"
        )
        return default


# The worker lives beside this file. Resolved absolutely because a client may
# spawn this server from anywhere.
WORKER = _env("GEMINI_WEB_WORKER",
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "gemini_web.py"))

# `uv run --script` is what honours the worker's inline dependency block, so it
# is the launcher, not python. Absolute path beats PATH: a client spawns this
# process without necessarily inheriting a login shell's PATH, so shutil.which
# can come up empty where `uv` runs fine in a terminal. Set GEMINI_WEB_BIN to a
# ready-made executable to skip uv entirely.
UV_BIN = _env("GEMINI_WEB_UV_BIN", shutil.which("uv") or "uv")
GEMINI_WEB_BIN = _env(BIN_ENV_VAR, "")


def _worker_argv(*args: str) -> list:
    """The command that runs one worker subcommand."""
    if GEMINI_WEB_BIN:
        return [GEMINI_WEB_BIN, *args]
    return [UV_BIN, "run", "--script", WORKER, *args]


# The web app has no model flag; what varies is which *surface* the prompt is
# sent to.
MODES = ("chat", "canvas", "image", "video")

# Mirrors gemini_web.py. Duplicated rather than imported, like everything else
# here: each server file has to stay a self-contained `uv run --script` target.
# These are API between the two files -- change them in both or not at all.
EXIT_NOT_LOGGED_IN = 3
EXIT_THROTTLED = 6
EXIT_TRANSIENT = 7
EXIT_WRONG_MODEL = 8

# Caps what a tool RETURNS. A conversation dump can be long; the tail is kept.
MAX_OUTPUT_CHARS = _int_env("AGENT_MCP_MAX_OUTPUT", 100_000)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return (f"...(truncated: kept the last {MAX_OUTPUT_CHARS} of {len(text)} chars)...\n"
            + text[-MAX_OUTPUT_CHARS:])


def _child_env() -> dict:
    """Never hand Claude's own credentials to the delegate; it doesn't need them
    and could exfiltrate them in dangerous mode."""
    return {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}


def _plugin_version() -> str:
    """The version Claude Code installed, read from the plugin manifest beside
    this file. Empty when this is a bare checkout rather than an installed
    plugin, which is an answer and not an error: `version` is optional in
    serverInfo, and a client that shows nothing for an uninstalled copy is
    right to.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".claude-plugin", "plugin.json")
    try:
        with open(path) as fh:
            return str(json.load(fh).get("version", ""))
    except (OSError, ValueError):
        return ""


def _probe(argv: list) -> str:
    """First line of a `--version`-style call, or a reason it produced none."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL, env=_child_env())
    except FileNotFoundError:
        return "(not found)"
    except subprocess.TimeoutExpired:
        return "(no answer within 10s)"
    except OSError as exc:
        return f"(failed: {exc})"
    text = (p.stdout or p.stderr or "").strip().splitlines()
    return text[0] if text else "(no output)"


def _instructions() -> str:
    """Handed to the client at connect time, so it lands in the session before
    the first call rather than after it. What belongs here is exactly the rules
    a tool result arrives too late to convey.
    """
    version = _plugin_version()
    return "\n\n".join([
        f"gemini-web-wrapper {version or '(dev checkout)'} - drives the Gemini "
        f"WEB APP (live Google search, Canvas, conversation history, "
        f"attachments) "
        f"through a real Chrome. Not the Gemini API.",
        # Claude Code cuts server instructions off at roughly 2000 characters
        # (the 1.6.x text arrived truncated mid-sentence), so everything a
        # session must act on sits in the first five paragraphs, and
        # test_the_rules_survive_client_truncation holds that line. Detail for
        # clients that show more comes after.
        "WEB RESEARCH IS HANDED TO THE USER, NOT RUN HERE - the web app is too "
        "flaky to automate as a default. When an answer needs the current web, "
        "do not call gemini_ask and do not fall back to WebSearch/WebFetch: "
        "write the research prompt, output it as plain text for the user to run "
        "in the Gemini web app, stop, and work from what they paste back. The "
        "gemini-ask skill (/agent-delegation:gemini-ask <topic>) has the full "
        "procedure.",
        "ERR ON THE SIDE OF NOT KNOWING. Hand over whenever a fact may sit "
        "outside your training data - versions, releases, prices, APIs, "
        "deprecations, 'is X still true' - and treat being unsure it is current "
        "as not knowing.",
        "THE PROMPT: one complete, specific prompt; a source URL per claim, never "
        "a verbatim quote per claim (that makes Gemini claim it cannot browse); "
        "follow-ups rather than fresh prompts. Always say whether to run it in a "
        "NEW Gemini conversation or CONTINUE the previous one - default to the "
        "same conversation for a direct follow-up on the same subject - and say "
        "so explicitly either way.",
        "UNATTENDED SESSION (a goal skill, or the user said they will be away): "
        "do not wait on a handover, and do not call gemini_ask in their place. "
        "Carry on from what you know and leave a note per fact that needs live "
        "knowledge - the claim, why it may be stale, what depends on it, and the "
        "ready-to-run prompt - and list the notes in the final report.",
        "gemini_ask IS FOR EXPLICIT REQUESTS ONLY ('run it yourself'), or for "
        "Canvas, image, video or an attachment. Every tool here blocks; there is "
        "no async dispatch (removed in 1.7.0). The rest matters when you do call "
        "one, or when writing a prompt.",
        "MORE ON THE HANDOVER PROMPT. Give Gemini the context it needs, the exact "
        "questions, and the shape of answer wanted (a comparison table, a "
        "version per item, dates). There is no prompt length worth worrying "
        "about. Unrelated questions go in separate prompts. If a pasted answer "
        "says it has no web access, it is wrong - write a retry prompt without "
        "any quote demand. Say the session line plainly: 'same thread as "
        "before, this follows up on the same vendors' or 'new conversation, "
        "unrelated topic'. gemini_read_conversation can read a thread the user "
        "ran by hand if they give you its id instead of pasting.",
        "THERE IS NO DEEP RESEARCH TOOL HERE. It was built, tested and removed in "
        "1.3.0: two of three kick-offs wedged silently for hours with no error. "
        "For nearly everything, one long specific prompt plus follow-ups is the "
        "better tool anyway - head to head, the long prompt answered in 37 "
        "seconds and the Deep Research run never finished. If a job truly needs "
        "Deep Research - dozens of sources read end to end, a written report as "
        "the deliverable - say so in the handover so the user can run it in Deep "
        "Research mode, and ask for the report pasted back or for its "
        "conversation id, which gemini_read_conversation can read.",
        "gemini_ask IS FOR EXPLICIT REQUESTS ONLY. Call it when the user tells you "
        "to run the prompt yourself ('run it yourself', 'ask Gemini directly'), "
        "or for a job that needs the web app itself rather than research - "
        "Canvas, image, video, an attachment. Never as the default research "
        "path, and never because a handover felt slow. When you do call it, the "
        "rules below apply.",
        "DO NOT RATION. The web app's quota is a SEPARATE pool from the "
        "agy/gemini CLI's, and large enough that ordinary use does not approach "
        "it: after a heavy day its rolling window read 16% consumed and its "
        "weekly limit 1%. So do not bundle unrelated questions into one prompt "
        "to save a round trip, and on an explicit gemini_ask do not skip a "
        "follow-up or a verification pass because it would be a second call. "
        "Live numbers are in the web app under Settings -> Usage limits - look "
        "there rather than trusting this text.",
        "STAY ON FLASH. Flash - including Flash with a long, detailed prompt - is "
        "enough for essentially everything a CLI agent asks for: lookups, version "
        "checks, comparisons, 'what changed in X since Y', reading and summarising "
        "pages. Reach for Pro only for a genuinely hard reasoning problem, which is "
        "rare in this kind of work. Two reasons this matters. First, Pro is the one "
        "model whose daily limits you can actually exhaust; Flash is where the "
        "allowance is effectively unreachable. Second, THIS SERVER CANNOT SWITCH "
        "MODELS - the picker holds whatever the profile was last left on, and "
        "changing it is a human action in the browser. So if you conclude a task "
        "needs Pro, say so and let the user decide; do not treat one thin Flash "
        "answer as proof, sharpen the prompt and ask again first.",
        "THE LOUDEST QUOTA SYMPTOM IS SILENCE. Gemini's own account of its "
        "limit behaviour is that exhausting Pro DOWNGRADES YOU TO FLASH with no "
        "message - you get a real answer from a smaller model and nothing says "
        "so. Every answer therefore reports which model produced it; read that "
        "field before concluding a weak answer means the question was hard. The "
        "other two silent-ish symptoms: a full conversation limit LOCKS the "
        "prompt box (surfaces here as 'the prompt box never appeared'), and "
        "compute-heavy modes - image, video, canvas - are WITHDRAWN from the "
        "tools drawer while throttled (surfaces as 'no tool labelled ...'). "
        "Neither is a DOM break, though both read like one.",
        "IF A CALL COMES BACK THROTTLED, STOP. Exit 6 means Gemini replied with a "
        "limit message instead of an answer. Exit 7 means it glitched. Seven is "
        "worth exactly one retry. SIX IS WORTH NONE - retrying into a limit is how "
        "a soft throttle becomes a hard one, and the account being throttled is the "
        "user's own paid subscription, so the cost of getting this wrong lands on "
        "them. Report it and let them decide. Note what these codes exist to "
        "prevent: Gemini renders a limit notice as an ordinary response turn, so "
        "without the check the worker would hand you 'Sorry, something went wrong' "
        "as though it were the answer. If a short, odd, system-sounding reply ever "
        "does reach you as content, treat it as a limit notice rather than a "
        "finding - the pattern list is good, not exhaustive.",
        "THE MODEL IS CHECKED BEFORE EVERY PROMPT, and a mismatch refuses the "
        "call (exit 8) without sending anything. The picker is a PROFILE "
        "setting - switch it once in the browser and it persists across tabs, "
        "sessions and days - so a change made long ago silently applies to "
        "every call until someone notices. It sat on Pro for an entire session "
        "of calls before this check existed, which is exactly the quiet, "
        "expensive drift it now prevents. Flash is expected by default. If you "
        "get exit 8, say so and let the user choose; do not route around it.",
        "DO NOT TEST THIS TOOL WITH A FIXED CANARY STRING. Checking that the "
        "browser still works is reasonable; sending 'reply with exactly: pong' "
        "fifty times is not. An identical one-word prompt repeated against one "
        "account is the most obviously scripted thing in the whole flow - more "
        "so than any timing, which is what the worker already goes to some "
        "trouble to disguise. Vary it: ask something short you actually wanted "
        "to know, or at minimum change both the wording and the expected answer "
        "each time. A connectivity check that doubles as a real question costs "
        "nothing extra and looks like use rather than instrumentation.",
        "Rules a tool result cannot deliver in time, when you do call a tool here:\n"
        "- There is ONE browser on ONE profile, so genuinely one call at a time. Chrome "
        "cannot open the same user-data-dir twice; a second call is refused rather "
        "than corrupting the profile.\n"
        "- Every call has a floor of roughly 20 seconds: Chrome has to launch and the "
        "Angular app has to hydrate before a prompt can even be typed. It is not hung.\n"
        "- If anything reports 'not signed in', the fix is a human one: run "
        "`uv run --script gemini_web.py login` in a terminal and sign in by hand. "
        "Google rejects its own sign-in flow inside an automated browser, so no tool "
        "here can do it for you.",
    ])


# `version` is where MCP expects a server to advertise itself (it rides in
# serverInfo), so a client can show it without parsing prose. It is the same
# string the plugin manifest carries, so what a client reports and what Claude
# Code installed cannot drift apart.
try:
    mcp = _Server(SERVER_NAME,
                  version=_plugin_version(),
                  instructions=_instructions())
except TypeError:                   # older SDK without one or both parameters
    try:
        mcp = _Server(SERVER_NAME, instructions=_instructions())
    except TypeError:
        mcp = _Server(SERVER_NAME)

# Every tool below drives the one browser, so they all take this lock and
# refuse rather than queue. Chrome would refuse a second launch on the same
# profile anyway; refusing here says why instead of surfacing a
# ProcessSingleton error.
_browser_lock = threading.Lock()

BUSY = ("Refusing: another call is using the browser right now. "
        "One profile, one Chrome - wait for it to return.")


def _refuse_canvas_files(mode: str, files: str) -> str:
    """Attachments never upload in canvas mode (worker ADM-6): no chip, no
    progress, and Gemini answers about an empty file. The worker refuses the
    pair too; refusing here as well saves the Chrome launch."""
    if mode == "canvas" and files.strip():
        return ("Error: attachments do not survive mode=canvas - the upload "
                "never starts and Gemini answers about an empty file. Nothing "
                "was sent. Use mode=chat for the file.")
    return ""


def _worker_sync(args: list, timeout: int) -> tuple:
    """Run one worker subcommand to completion. Returns (rc, stdout, stderr),
    or None when another call already holds the browser."""
    if not _browser_lock.acquire(blocking=False):
        return None
    argv = _worker_argv(*args)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              env=_child_env())
    except FileNotFoundError:
        return 127, "", (f"{argv[0]} not found. Set {BIN_ENV_VAR} to a ready-made "
                         "executable, or GEMINI_WEB_UV_BIN to the uv binary.")
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer within {timeout}s"
    finally:
        _browser_lock.release()
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _meta_of(stdout: str) -> dict:
    """The worker's trailing GEMINI_WEB_META line, as a dict."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("GEMINI_WEB_META "):
            try:
                return json.loads(line[len("GEMINI_WEB_META "):])
            except ValueError:
                return {}
    return {}


def _strip_meta(stdout: str) -> str:
    return "\n".join(l for l in stdout.splitlines()
                     if not l.startswith("GEMINI_WEB_META ")).strip()


@mcp.tool()
def gemini_ask(prompt: str, conversation_id: str = "", mode: str = "chat",
               files: str = "", timeout_seconds: int = 240) -> str:
    """
    Asks the Gemini WEB APP a question and BLOCKS until the answer is back.

    mode: chat | canvas | image | video
    files: comma-separated paths to attach (uploaded to the account)
    conversation_id: continue an earlier thread; every answer reports its id

    Gemini behind the browser, not the API - your history, Gems and uploads are
    all there.

    ONLY WHEN THE USER EXPLICITLY ASKS ("run it yourself"), or for Canvas,
    image, video or an attachment. For web research the default is to write
    the prompt and hand it to the user to run - see the server instructions.

    ONE LONG PROMPT BEATS SEVERAL SHORT ONES, and follow-ups into the same
    conversation_id are nearly free. Ask for a URL per claim, not a quote.

    FLASH HANDLES ALMOST EVERYTHING. Pro is for rare hard reasoning and is the
    only model whose daily limit is reachable. This tool cannot switch models
    regardless, so ask the user if you genuinely need Pro.

    EXPECT ~20s MINIMUM even for a one-word reply: Chrome has to launch and the
    app has to hydrate. That is the floor, not a fault. Raise timeout_seconds
    for Canvas or image/video work, which can take minutes.
    """
    if mode not in MODES:
        return f"Error: mode must be one of {', '.join(MODES)}; got {mode!r}."
    if refused := _refuse_canvas_files(mode, files):
        return refused
    # The worker treats --timeout as a budget for its whole run, launch
    # and upload wait included, so the 20s here only has to cover Chrome
    # shutting down after the worker's own deadline fires.
    args = ["ask", "--prompt", prompt,
            "--timeout", str(max(timeout_seconds - 20, 30))]
    if conversation_id:
        args += ["--conversation", conversation_id]
    if mode != "chat":
        args += ["--mode", mode]
    for path in [f.strip() for f in files.split(",") if f.strip()]:
        args += ["--file", path]
    result = _worker_sync(args, timeout_seconds)
    if result is None:
        return BUSY
    rc, out, err = result

    if rc != 0:
        hint = ""
        if rc == EXIT_NOT_LOGGED_IN:
            hint = ("\n\nSign in first: run `uv run --script gemini_web.py login` "
                    "in a terminal. Google blocks its own sign-in flow inside an "
                    "automated browser, so this cannot be done for you.")
        elif rc == EXIT_THROTTLED:
            hint = ("\n\nThat is a quota wall, not a bug. Do NOT retry in a loop and "
                    "do NOT compensate by calling this tool harder - that is how a "
                    "soft limit becomes a hard one. Say so plainly and let the user "
                    "decide; Settings -> Usage limits in the web app has the real "
                    "numbers.")
        elif rc == EXIT_WRONG_MODEL:
            hint = ("\n\nNothing was sent and no quota was spent - this is a "
                    "guard, not a failure. The model picker is a profile "
                    "setting that persists across runs, so it will keep "
                    "refusing until someone changes it back. Tell the user and "
                    "let them decide; do NOT retry, and do NOT work around it "
                    "by accepting whatever model happens to be selected.")
        elif rc == EXIT_TRANSIENT:
            hint = ("\n\nGemini glitched rather than hitting a limit. ONE retry is "
                    "reasonable here. If a second fails, stop and report it instead "
                    "of looping.")
        return f"Error: the Gemini worker exited {rc}.\n{_truncate(err.strip())}{hint}"

    meta = _meta_of(out)
    answer = _strip_meta(out)
    footer = []
    if meta.get("conversation_id"):
        footer.append(f"conversation_id: {meta['conversation_id']}  "
                      f"(pass it back to continue this thread)")
    if meta.get("model"):
        footer.append(f"answered by: {meta['model']}  (an unexpected Flash here "
                      f"can mean Pro quota ran out - the app downgrades silently)")
    if meta.get("elapsed_seconds"):
        footer.append(f"{meta['elapsed_seconds']}s, extracted via "
                      f"{meta.get('extraction', '?')}")
    return _truncate(answer) + ("\n\n---\n" + "\n".join(footer) if footer else "")


@mcp.tool()
def gemini_conversations(query: str = "", limit: int = 20) -> str:
    """
    Lists the conversations in the Gemini sidebar, newest first, as id + title.

    The ids are what gemini_ask(conversation_id=...) and
    gemini_read_conversation() take. Pass `query` to filter on title text.

    This opens a browser, so it costs the same ~20s as any other call here.
    """
    args = ["list", "--limit", str(max(limit, 1))]
    if query:
        args += ["--query", query]
    result = _worker_sync(args, 180)
    if result is None:
        return BUSY
    rc, out, err = result
    if rc != 0:
        return f"Error: the Gemini worker exited {rc}.\n{_truncate(err.strip())}"
    try:
        rows = json.loads(out)
    except ValueError:
        return _truncate(out)
    if not rows:
        return "No conversations matched." if query else "No conversations found."
    return "\n".join(f"{r['id']}  {r['title']}" for r in rows)


@mcp.tool()
def gemini_read_conversation(conversation_id: str) -> str:
    """
    Dumps an existing Gemini conversation as markdown, every turn, oldest
    first, without adding to it.

    This is how a handover comes back without copy-paste: the user runs your
    research prompt by hand in the browser and gives you its conversation id.
    Find ids with gemini_conversations().
    """
    if not conversation_id.strip():
        return "Error: conversation_id is required. List them with gemini_conversations()."
    result = _worker_sync(
        ["read", "--conversation", conversation_id.strip()], 240)
    if result is None:
        return BUSY
    rc, out, err = result
    if rc != 0:
        return f"Error: the Gemini worker exited {rc}.\n{_truncate(err.strip())}"
    return _truncate(_strip_meta(out)) or "(the conversation rendered empty)"


@mcp.tool()
def delegation_status() -> str:
    """
    Reports what this wrapper actually is: which plugin version is running,
    where the worker and its Chrome profile live, whether that profile is still
    signed in.

    Call this when a call behaves in a way the docstring does not explain,
    before concluding the tool is broken - a silently expired Google session
    looks like a broken tool and is not one. Checking sign-in opens a browser,
    so this is slower than the sibling servers' status.
    """
    version = _plugin_version()
    result = _worker_sync(["status"], 180)
    if result is None:
        signed_in = "(not checked: another call holds the browser)"
    else:
        rc, out, err = result
        try:
            info = json.loads(out)
            signed_in = ("yes, as " + (info.get("account") or "(unknown)")
                         if info.get("logged_in")
                         else "NO - run `uv run --script gemini_web.py login`")
        except ValueError:
            signed_in = f"unknown (worker exited {rc}: {err.strip()[:120]})"
    lines = [
        SERVER_NAME,
        f"  version:    {version or '(dev checkout: no plugin manifest)'}",
        f"  running:    {os.path.abspath(__file__)}",
        f"  worker:     {' '.join(_worker_argv())}",
        f"  signed in:  {signed_in}",
        f"  modes:      {', '.join(MODES)}",
        f"  research:   handed to the user as a prompt; gemini_ask on explicit request",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
