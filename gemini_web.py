#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49"]
# ///
"""
gemini_web: drives the Gemini *web app* from the command line.

This exists because `gemini` and `agy` talk to the Gemini API, which has no
Canvas, no Gems, no conversation history and no file attachments. Those live
only in the web app, behind a logged-in Pro account.

Deep Research is deliberately NOT here. It was built and then removed in 1.3.0:
two of three kick-offs wedged silently, and a CLI agent can get most of the same
value from one long prompt plus follow-ups in half a minute rather than forty.
If a job genuinely needs it, ask the human to run it in the browser.

One invocation does one job and exits, exactly like `agy --print`, so that
gemini_web_mcp_server.py can wrap it with the same run store the other
delegates use. Playwright never runs inside the MCP server.

The browser is a real Chrome (channel="chrome") against a dedicated profile at
~/.agent-delegation-mcp/gemini-profile. It is deliberately NOT your daily
profile: Chrome will not share a user-data-dir with a running instance, and an
automation crash should never take your own session down with it. Log in once
with `gemini_web.py login`; the cookies persist from then on.

Every selector in SELECTORS is unversioned Angular internals. When Google
reshuffles the DOM this file is the only thing that breaks, and it fails with
the selector name rather than hanging -- see _require().
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser

ORIGIN = "https://gemini.google.com"
PROFILE_DIR = os.path.expanduser(
    os.environ.get("GEMINI_WEB_PROFILE", "").strip()
    or "~/.agent-delegation-mcp/gemini-profile"
)

# Exit codes. The MCP server maps these to verdicts, so they are API.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_LOGGED_IN = 3
EXIT_SELECTOR = 4
EXIT_TIMEOUT = 5
# Gemini answered, but with a system message rather than content. Two codes
# rather than one because the right reaction differs: a transient glitch is
# worth a single retry, a limit message is worth none -- retrying into a
# throttle is how a soft limit becomes a hard one.
EXIT_THROTTLED = 6
EXIT_TRANSIENT = 7

# The SPA takes ~8s to hydrate a deep-linked conversation on a cold load; 30s
# is slack for a slow morning, not an expectation.
HYDRATE_MS = 30_000
NAV_MS = 60_000

SELECTORS = {
    # Quill editor. contenteditable, so .fill() does nothing useful -- see _send().
    "editor": 'div.ql-editor[contenteditable="true"]',
    "user_query": "user-query",
    "model_response": "model-response",
    "message_content": "message-content",
    # Completion signal: Gemini only renders the action row once a response is
    # finished streaming. Cheaper and far more reliable than watching for the
    # send button to flip back from "stop".
    "response_actions": "message-actions",
    "copy_button": "copy-button button",
    # Only rendered once the editor is non-empty. Clicking it is the primary
    # submit: a synthetic Enter is ignored by Angular in headless Chrome, where
    # the text lands in the editor and simply never sends.
    "send_button": 'button[aria-label="Send message"]',
    "conversation_link": 'conversations-list a[href^="/app/"]',
    # The sidebar now ships collapsed, and Angular does not render the history
    # list until it is opened -- so conversation_link never appears and the
    # failure reads like a DOM change when nothing moved. See open_sidebar().
    "open_sidebar": 'button[aria-label="Open sidebar"]',
    "tools_button": 'input-container button[aria-label="Upload & tools"]',
    "tool_toggle": 'toolbox-drawer-item button[role="menuitemcheckbox"]',
    # The drawer shows a few tools and hides the rest behind this. Which ones
    # are promoted varies, so anything not found at the top level is looked for
    # again after expanding.
    "more_tools": 'button:has-text("More tools")',
    "file_input": 'input[type="file"]',
    "mode_switcher": "bard-mode-switcher",
    # The switcher renders its label lazily, so inner_text is often empty even
    # though the element is there. This button always carries it, as
    # "Open mode picker, currently Flash".
    "mode_picker": '[aria-label^="Open mode picker"]',
    # Signed-out marker. Gemini serves a fully working composer to anonymous
    # visitors, so the editor appearing proves nothing -- the only honest
    # signal is whether the app is still offering to sign you in.
    "account_footer": "sidenav-mavatar-footer",
}

# Label text inside the "Upload & tools" drawer. Matched case-insensitively on
# the button's own text, so a trailing " (new)" badge will not break it.
TOOL_LABELS = {
    "canvas": "canvas",
    "image": "create image",
    "video": "create video",
}

CONVERSATION_ID_RE = re.compile(r"/app/([0-9a-f]{6,})")

# Gemini reports being over quota, or having glitched, as an ORDINARY response
# turn: it gets an action row, its text stops growing, and every completion
# signal we have says "done". Without this table the worker hands that message
# back as though it were the answer, and a delegating agent treats "Sorry,
# something went wrong" as a research finding.
#
# Honesty about provenance: only the TRANSIENT patterns have been seen from
# this worker. The THROTTLE ones match how Gemini describes its own limit
# behaviour ("an in-line message stating you have reached your usage limit,
# displaying the time until limits refresh") but have NOT been observed here,
# and that description traces to third-party write-ups rather than Google
# documentation. A wrong throttle pattern costs a false alarm, which is the
# safe direction. An unrecognised message is still returned as an answer --
# that residual gap is real and the README says so rather than implying this
# is airtight.
#
# Text matching is also NOT the main event. Per the same description the
# primary response to Pro exhaustion is a SILENT DOWNGRADE to Flash, with no
# message at all, and a full conversation limit LOCKS THE COMPOSER rather than
# replying. Those are handled in open() and by reporting the answering model in
# the meta line; this table only catches the case where Gemini does reply.
SYSTEM_REPLY_MAX_CHARS = 400

THROTTLE_PATTERNS = (
    "reached your limit", "reached the limit", "usage limit", "rate limit",
    "daily limit", "too many requests", "out of requests",
    "try again later", "come back later", "upgrade to",
)
TRANSIENT_PATTERNS = (
    "something went wrong", "try your request again",
    "please try again", "unable to complete",
)


def classify_response(markdown: str) -> tuple[str, str]:
    """('ok' | 'throttled' | 'transient', the pattern that matched).

    Gated on LENGTH first, which is the whole trick. These notices are short
    and are the entire response; a real answer that happens to discuss rate
    limiting is long. Without the gate, asking Gemini about a service's quotas
    would classify its own answer as a throttle -- a plausible thing for a CLI
    agent to ask, and a maddening thing to debug.
    """
    text = (markdown or "").strip()
    if not text or len(text) > SYSTEM_REPLY_MAX_CHARS:
        return "ok", ""
    low = text.lower()
    # Throttle first, deliberately: "please try again later" matches both
    # tables, and calling an ambiguous message a throttle means we decline to
    # retry. Failing closed on retries is the cheaper mistake of the two.
    for pat in THROTTLE_PATTERNS:
        if pat in low:
            return "throttled", pat
    for pat in TRANSIENT_PATTERNS:
        if pat in low:
            return "transient", pat
    return "ok", ""


def _chrome_binary() -> str:
    override = os.environ.get("GEMINI_WEB_CHROME", "").strip()
    if override:
        return os.path.expanduser(override)
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser(
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(path):
            return path
    return shutil.which("google-chrome") or shutil.which("chrome") or ""


# Used only by `login`, which has to run Chrome WITHOUT Playwright attached.
CHROME_BIN = _chrome_binary()

# Landing page for `login`. Going straight at the account chooser rather than
# at Gemini matters: Gemini serves anonymous visitors happily, so someone can
# use that window for a while and quit it still signed out, with nothing on
# screen having looked wrong.
LOGIN_URL = ("https://accounts.google.com/ServiceLogin?continue="
             "https%3A%2F%2Fgemini.google.com%2Fapp")


# --------------------------------------------------------------------------
# Pacing
#
# Two separate problems, worth not confusing:
#
#   1. A script clicks the instant an element exists, types a 900-character
#      prompt as ONE insertText event, and polls on an exact 500ms metronome.
#      No person produces that timing profile. It is the cheapest possible
#      signal to collect and the cheapest to stop emitting.
#   2. The browser announces itself. navigator.webdriver is true under the
#      DevTools protocol, and Chrome's automation switches are visible.
#
# What follows addresses both, and it is worth being honest about the ceiling:
# this defeats trivial checks. It does not defeat serious fingerprinting, and
# nothing here touches a CAPTCHA or any other challenge -- if one appears, the
# run fails and a human deals with it. The real protections were already in
# place before any of this: a stock Chrome build rather than Playwright's
# chromium, a persistent profile with genuine history, and headed by default.
#
# Set GEMINI_WEB_NO_PACING=1 to turn the delays off when debugging; the launch
# flags stay either way, since they cost nothing.
# --------------------------------------------------------------------------

def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


PACING = not _truthy("GEMINI_WEB_NO_PACING")

# A short beat before committing to a click, the way a hand arrives at a
# target before pressing it.
CLICK_PAUSE_MS = (90, 320)
# After a navigation, before touching anything: a person looks at the page.
READ_PAUSE_MS = (600, 1900)
# Between bursts of typing.
TYPE_PAUSE_MS = (30, 130)
# Characters per burst. Real typing is not per-character uniform either, it
# comes in runs, so bursts model it better than a fixed per-key delay.
TYPE_BURST = (3, 11)
# A long prompt would take minutes at human speed, which is its own anomaly
# (nobody types 4000 characters into a chat box in one go -- they paste). Past
# this, send the remainder as a paste, which is what a person would do.
TYPE_BUDGET_CHARS = 450

# Chrome's own automation tells. --enable-automation sets navigator.webdriver
# and shows the "controlled by automated test software" infobar;
# AutomationControlled is the Blink feature behind the same flag.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_IGNORE = ["--enable-automation"]

# Runs before any page script on every document, including iframes.
STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


def _ms(span) -> int:
    """A random count of MILLISECONDS from (lo, hi), or 0 when pacing is off.

    Every span above is already in milliseconds; this does not scale them.
    """
    return int(random.uniform(*span)) if PACING else 0


def _viewport_for_profile() -> dict:
    """A window size that stays the same for this profile across runs.

    Deliberately NOT randomised per launch. A real person's window size barely
    changes; one that is different every session is an inconsistency a fixed
    size would not have produced. So it is chosen once, from a plausible range
    of real laptop-sized windows, and then remembered.
    """
    path = os.path.join(PROFILE_DIR, "viewport.json")
    try:
        with open(path) as fh:
            saved = json.load(fh)
        if {"width", "height"} <= saved.keys():
            return {"width": int(saved["width"]), "height": int(saved["height"])}
    except Exception:
        pass
    vp = {"width": random.choice([1440, 1512, 1680, 1728]),
          "height": random.choice([812, 852, 900, 946])}
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(vp, fh)
    except Exception:
        pass
    return vp


def google_cookie_count(profile_dir: str = "") -> int:
    """How many google.com cookies the profile holds.

    Cheap, offline evidence of whether a sign-in actually landed -- far more
    trustworthy than asking the user, and it works without launching anything.
    The values are encrypted; only the host column is read.
    """
    import shutil as _shutil, sqlite3, tempfile
    path = os.path.join(profile_dir or PROFILE_DIR, "Default", "Cookies")
    if not os.path.isfile(path):
        return 0
    tmp = tempfile.mktemp(suffix=".sqlite")
    try:
        _shutil.copy(path, tmp)     # Chrome holds a lock on the live file
        with sqlite3.connect(tmp) as con:
            return con.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%google.com'"
                " AND host_key NOT LIKE '%gemini.google.com'"
            ).fetchone()[0]
    except Exception:
        return 0
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


class GeminiWebError(Exception):
    """Carries the exit code the CLI should die with."""

    def __init__(self, message: str, code: int = EXIT_SELECTOR):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# HTML -> Markdown
#
# The clipboard path below gives us Google's own markdown and is preferred.
# This is the fallback for when the clipboard is unreadable (headless, no
# window focus, permission denied). It is plain Python on an outerHTML string
# rather than JS in the page precisely so the fixture tests can exercise it
# without a browser.
# --------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "svg", "mat-icon", "message-actions",
              "model-response-disclaimers", "response-info-line", "button",
              # This wrapper no longer drives Deep Research, but you can
              # still start one by hand in the browser and read the thread back
              # with `read`. Its panel carries the reasoning trace and a browse
              # chip per site visited, which dwarf the answer.
              "thinking-panel", "thinking-panel-skeleton-loader",
              "browse-chip-list", "mat-progress-spinner"}
_BLOCK_TAGS = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5",
               "h6", "ul", "ol", "pre", "blockquote", "table", "tr", "hr"}

# Angular Material hides a screen-reader label inside every user turn -- an
# <h5 class="cdk-visually-hidden screen-reader-user-query-label"> that repeats
# the whole prompt after the words "You said". It is invisible on screen and
# duplicates the turn in the extracted markdown, so it is dropped by class.
_HIDDEN_CLASS_RE = re.compile(
    r"\b(cdk-visually-hidden|visually-hidden|sr-only|screen-reader)")

# Gemini does NOT put the language on the <code> element. It renders a header
# strip above each block whose first span is the language name ("Python"). Left
# alone that lands in the output as a stray line above an unlabelled fence, so
# the strip is captured and used as the fence's language instead of emitted.
_CODE_HEADER_CLASS = "code-block-decoration"


class _MarkdownExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0
        # Names of the tags currently being skipped, so the matching end tag
        # pops the right one -- a skip decision can come from a class rather
        # than the tag name, which the end tag does not carry.
        self._skip_stack: list[str] = []
        self._list_stack: list[dict] = []
        self._in_pre = False
        self._pending_fence = False
        self._lang_stack: list[str] = []
        self._lang_buf: list[str] = []
        self._pending_lang = ""
        self._href = ""
        self._link_text: list[str] = []
        self._row: list[str] = []
        self._table_rows: list[list[str]] = []
        self._cell: list[str] = []
        self._in_cell = False
        self._header_row = False

    # -- helpers ----------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self._in_cell:
            self._cell.append(text)
        elif self._href:
            self._link_text.append(text)
        else:
            self.out.append(text)

    def _open_fence(self, lang: str = "") -> None:
        if self._pending_fence:
            self._pending_fence = False
            self.out.append("```" + (lang or self._pending_lang) + "\n")
            self._pending_lang = ""

    def _newline(self, count: int = 1) -> None:
        # Runs of newlines are collapsed once, in result(); emitting freely
        # here keeps every call site a one-liner.
        if not self._in_cell:
            self.out.append("\n" * count)

    # -- parser hooks -----------------------------------------------------
    def handle_starttag(self, tag, attrs):
        attrd = dict(attrs)
        if (tag in _SKIP_TAGS
                or attrd.get("aria-hidden") == "true"
                or _HIDDEN_CLASS_RE.search(attrd.get("class") or "")):
            self._skip_depth += 1
            self._skip_stack.append(tag)
            return
        if self._skip_depth:
            return
        if _CODE_HEADER_CLASS in (attrd.get("class") or "") or self._lang_stack:
            # Inside the language strip: swallow its text, keep the language.
            self._lang_stack.append(tag)
            return

        if tag == "br":
            self._emit("  \n" if not self._in_pre else "\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(2)
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "p":
            self._newline(2)
        elif tag == "hr":
            self._newline(2)
            self._emit("---")
            self._newline(2)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code":
            if self._in_pre:
                m = re.search(r"language-([\w+#.-]+)", attrd.get("class", ""))
                self._open_fence(m.group(1) if m else "")
            else:
                self._emit("`")
        elif tag == "pre":
            # The language lives on the <code> *inside* the <pre>, so the
            # opening fence cannot be written until we have seen it (or seen
            # text arrive without one). _open_fence() settles it.
            self._in_pre = True
            self._pending_fence = True
            self._newline(2)
        elif tag in ("ul", "ol"):
            self._list_stack.append({"ordered": tag == "ol", "n": 0})
            self._newline(2 if len(self._list_stack) == 1 else 1)
        elif tag == "li":
            self._newline()
            depth = max(len(self._list_stack) - 1, 0)
            marker = "- "
            if self._list_stack:
                item = self._list_stack[-1]
                item["n"] += 1
                if item["ordered"]:
                    marker = f"{item['n']}. "
            self._emit("  " * depth + marker)
        elif tag == "blockquote":
            self._newline(2)
            self._emit("> ")
        elif tag == "a":
            self._href = _unwrap_google_redirect(attrd.get("href", ""))
            self._link_text = []
        elif tag == "table":
            self._table_rows = []
        elif tag == "tr":
            self._row = []
            self._header_row = False
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell = []
            if tag == "th":
                self._header_row = True

    def handle_endtag(self, tag):
        if self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
            self._skip_depth = max(self._skip_depth - 1, 0)
            return
        if self._skip_depth:
            return
        if self._lang_stack:
            self._lang_stack.pop()
            if not self._lang_stack:
                word = " ".join("".join(self._lang_buf).split()).split(" ")[:1]
                self._pending_lang = word[0].lower() if word else ""
                self._lang_buf = []
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote"):
            self._newline(2)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag == "pre":
            self._open_fence()  # an empty <pre> still needs both fences
            # Trim the trailing newline the source almost always carries, so
            # the closing fence does not end up after a blank line.
            while self.out and self.out[-1].endswith("\n"):
                self.out[-1] = self.out[-1][:-1]
                if self.out[-1]:
                    break
                self.out.pop()
            self._emit("\n```")
            self._in_pre = False
            self._newline(2)
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            if not self._list_stack:
                self._newline(2)
        elif tag == "a":
            text = "".join(self._link_text).strip()
            href, self._href = self._href, ""
            self._link_text = []
            if text and href and not href.startswith("javascript:"):
                self.out.append(f"[{text}]({href})")
            elif text:
                self.out.append(text)
        elif tag in ("td", "th"):
            self._in_cell = False
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = []
        elif tag == "tr":
            if self._row:
                self._table_rows.append(self._row)
                if self._header_row and len(self._table_rows) == 1:
                    self._table_rows.append(["---"] * len(self._row))
            self._row = []
        elif tag == "table":
            if self._table_rows:
                self._newline(2)
                for row in self._table_rows:
                    self.out.append("| " + " | ".join(row) + " |\n")
                self._newline()
            self._table_rows = []
        elif tag in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._lang_stack:
            self._lang_buf.append(data)
            return
        if self._in_pre:
            self._open_fence()
            self._emit(data)
            return
        if not data.strip():
            # Collapse inter-tag whitespace to a single space, but never
            # introduce one at the start of a line.
            if self.out and not self.out[-1].endswith(("\n", " ")):
                self._emit(" ")
            return
        self._emit(re.sub(r"\s+", " ", data))

    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


_REDIRECT_HOSTS = ("www.google.com/url", "www.google.com/search",
                   "google.com/url", "google.com/search")


def _unwrap_google_redirect(href: str) -> str:
    """Gemini rewrites outbound links through a google.com redirect.

    The anchor TEXT shows the real destination while the href points at
    `google.com/search?q=<the real url>&utm_source=gemini`. Left alone, a
    markdown link looks right and goes somewhere else -- which is the worst
    failure for an answer whose whole job is to be checkable. The real URL is
    sitting in the q (or url) parameter, so take it back.
    """
    if not href or not any(h in href for h in _REDIRECT_HOSTS):
        return href
    try:
        from urllib.parse import parse_qs, unquote, urlsplit
        qs = parse_qs(urlsplit(href).query)
    except Exception:
        return href
    for key in ("q", "url"):
        for value in qs.get(key, []):
            target = unquote(value)
            if target.startswith(("http://", "https://")):
                return target
    return href


def html_to_markdown(source: str) -> str:
    """Best-effort HTML -> Markdown for one Gemini response subtree."""
    parser = _MarkdownExtractor()
    parser.feed(source)
    parser.close()
    return parser.result()


def parse_meta_line(stdout: str) -> dict:
    """Pull the trailing GEMINI_WEB_META line out of a worker's stdout.

    The MCP server uses this to recover the conversation id from a run whose
    output it only sees as a file. Returns {} when absent or malformed --
    a missing id must never turn a good answer into an error.
    """
    for line in reversed(stdout.splitlines()):
        if line.startswith("GEMINI_WEB_META "):
            try:
                return json.loads(line[len("GEMINI_WEB_META "):])
            except json.JSONDecodeError:
                return {}
    return {}


def conversation_id_from_url(url: str) -> str:
    m = CONVERSATION_ID_RE.search(url or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------

class Session:
    """A live browser on the Gemini app. Use as a context manager."""

    def __init__(self, headless: bool | None = None):
        if headless is None:
            headless = _truthy("GEMINI_WEB_HEADLESS")
        self.headless = headless
        self._pw = None
        self.context = None
        self.page = None

    def __enter__(self) -> "Session":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment problem
            raise GeminiWebError(
                "playwright is not installed. Run this file with "
                "`uv run --script gemini_web.py ...` so the inline dependency "
                f"block is honoured. ({exc})",
                EXIT_USAGE,
            ) from exc

        os.makedirs(PROFILE_DIR, exist_ok=True)
        self._pw = sync_playwright().start()
        # channel="chrome" uses the Chrome already on this machine instead of
        # downloading Playwright's chromium: one less 150MB artifact, and a
        # stock Chrome build is less interesting to Google's bot heuristics.
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                PROFILE_DIR,
                channel="chrome",
                headless=self.headless,
                viewport=_viewport_for_profile(),
                args=["--no-first-run", "--no-default-browser-check",
                      *STEALTH_ARGS],
                # THE flag that matters on macOS. Playwright passes
                # --use-mock-keychain by default, which hands Chrome a
                # different cookie-encryption key than the one the real
                # Keychain holds. Chrome cannot decrypt the profile's existing
                # cookies, so it DELETES them -- silently, on launch. Against a
                # signed-in profile that means every run destroys the session
                # it was supposed to use, and the symptom looks exactly like
                # "the sign-in never took". Ignoring it makes Chrome use the
                # real Keychain; expect a one-time macOS prompt to allow it.
                #
                # --enable-automation is dropped for an unrelated reason: it is
                # what sets navigator.webdriver and raises the "controlled by
                # automated test software" infobar.
                ignore_default_args=["--use-mock-keychain", *STEALTH_IGNORE],
            )
        except Exception as exc:
            if "ProcessSingleton" in str(exc) or "already in use" in str(exc):
                raise GeminiWebError(
                    f"{PROFILE_DIR} is already open in another Chrome. Quit it "
                    "(this profile is the one `login` uses) and try again. If "
                    "nothing is running, delete the stale "
                    f"{os.path.join(PROFILE_DIR, 'SingletonLock')}.",
                    EXIT_USAGE,
                ) from exc
            raise
        try:
            self.context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=ORIGIN
            )
        except Exception:
            # Not fatal: html_to_markdown() is the fallback extraction path.
            pass
        try:
            # Belt to --disable-blink-features' braces: the flag covers the
            # main world, this covers anything that re-reads the property.
            self.context.add_init_script(STEALTH_INIT_JS)
        except Exception:
            pass
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    # -- pacing -----------------------------------------------------------
    def _pause(self, span=CLICK_PAUSE_MS) -> None:
        ms = _ms(span)
        if ms:
            self.page.wait_for_timeout(ms)

    def _tap(self, selector, timeout: float = 10_000) -> None:
        """Click, but arrive at the target first.

        Playwright's click already moves the mouse to the element, so the
        point of hovering separately is the gap between arriving and pressing
        -- and the scroll, which a script otherwise skips entirely because it
        can click an element that is off-screen.
        """
        loc = self.page.locator(selector).first if isinstance(selector, str) else selector
        try:
            loc.scroll_into_view_if_needed(timeout=timeout)
            loc.hover(timeout=timeout)
            self._pause()
        except Exception:
            pass  # hovering is a nicety; never let it block the click
        loc.click(timeout=timeout)

    def _type(self, text: str) -> None:
        """Enter text in uneven bursts rather than one atomic event.

        Past TYPE_BUDGET_CHARS the rest goes in as a single insert, because
        typing thousands of characters at a steady human rate is not human
        either -- a person pastes a long prompt, and a paste is one event.
        """
        if not text:
            return
        if not PACING:
            self.page.keyboard.insert_text(text)
            return
        i = 0
        while i < len(text) and i < TYPE_BUDGET_CHARS:
            n = random.randint(*TYPE_BURST)
            self.page.keyboard.insert_text(text[i:i + n])
            i += n
            self._pause(TYPE_PAUSE_MS)
        if i < len(text):
            self._pause(TYPE_PAUSE_MS)
            self.page.keyboard.insert_text(text[i:])

    def __exit__(self, *exc_info):
        for closer in (getattr(self.context, "close", None),
                       getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        return False

    # -- navigation -------------------------------------------------------
    def open(self, conversation_id: str = "", expect_history: bool = False) -> None:
        url = f"{ORIGIN}/app/{conversation_id}" if conversation_id else f"{ORIGIN}/app"
        self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_MS)
        try:
            self._require("editor", HYDRATE_MS)
        except GeminiWebError:
            # Gemini locks the input box outright once conversation limits are
            # hit entirely, so a missing composer is not only a redesign. Not
            # observed here, so this names a candidate rather than diagnosing.
            raise GeminiWebError(
                "the prompt box never appeared. Usually that means the editor "
                "selector moved - but Gemini also LOCKS the composer when the "
                "account is fully out of quota, and the two look identical from "
                "here. Open the profile in a browser and look before assuming a "
                "selector broke.",
                EXIT_SELECTOR,
            ) from None
        if expect_history:
            self._require("model_response", HYDRATE_MS)
        # The app is usable before this; a person is not looking at it yet.
        self._pause(READ_PAUSE_MS)

    def _require(self, key: str, timeout_ms: int, state: str = "visible"):
        """wait_for_selector, but the failure names the selector that moved.

        state="attached" is for elements that are in the DOM and deliberately
        never visible -- Gemini's file inputs are class="hidden-file-input",
        and the default "visible" wait times out on them forever even though
        set_input_files works fine.
        """
        try:
            return self.page.wait_for_selector(SELECTORS[key], timeout=timeout_ms,
                                               state=state)
        except Exception as exc:
            if "accounts.google.com" in (self.page.url or ""):
                raise GeminiWebError(
                    "Not signed in. Run: uv run --script gemini_web.py login",
                    EXIT_NOT_LOGGED_IN,
                ) from exc
            raise GeminiWebError(
                f"selector {key!r} ({SELECTORS[key]}) never appeared at {self.page.url}. "
                "Google most likely reshuffled the DOM; fix SELECTORS in gemini_web.py.",
                EXIT_SELECTOR,
            ) from exc

    def logged_in(self, navigate: bool = True) -> bool:
        """True only when the app has stopped offering to sign you in.

        Do NOT shortcut this to "the composer rendered": an anonymous visitor
        gets the same composer, just wired to Flash-Lite with no history. The
        account footer reads "Sign in" when signed out and the account name
        when signed in, which is the cheapest honest signal on the page.
        """
        try:
            if navigate:
                self.page.goto(f"{ORIGIN}/app", wait_until="domcontentloaded",
                               timeout=NAV_MS)
            self.page.wait_for_selector(SELECTORS["editor"], timeout=HYDRATE_MS)
            if "accounts.google.com" in (self.page.url or ""):
                return False
            return bool(self.page.evaluate(
                """(sel) => {
                    const foot = document.querySelector(sel);
                    if (foot && /^sign in$/i.test((foot.innerText || '').trim()))
                        return false;
                    return ![...document.querySelectorAll('a,button')].some(
                        e => /^sign in$/i.test((e.innerText || '').trim()));
                }""",
                SELECTORS["account_footer"],
            ))
        except Exception:
            return False

    def account(self) -> str:
        """Who the profile is signed in as. Needs the sidebar open -- the
        footer is inside it, and reading it collapsed returns ""."""
        try:
            self.open_sidebar()
            el = self.page.locator(SELECTORS["account_footer"])
            return " ".join((el.first.inner_text() or "").split()) if el.count() else ""
        except Exception:
            return ""

    # -- composing --------------------------------------------------------
    def select_tool(self, mode: str) -> None:
        """Toggle Canvas / image / video on for the next send."""
        label = TOOL_LABELS.get(mode)
        if not label:
            return
        self._tap(SELECTORS["tools_button"])
        self._require("tool_toggle", 10_000)
        self._pause()

        def _click_labelled() -> bool:
            toggles = self.page.locator(SELECTORS["tool_toggle"])
            for i in range(toggles.count()):
                btn = toggles.nth(i)
                if label in (btn.inner_text() or "").strip().lower():
                    if btn.get_attribute("aria-checked") != "true":
                        self._tap(btn)
                    return True
            return False

        found = _click_labelled()
        if not found:
            # Which tools get promoted varies by session; the rest sit
            # behind "More tools".
            try:
                self._tap(SELECTORS["more_tools"], timeout=5_000)
                self.page.wait_for_timeout(800 + _ms(CLICK_PAUSE_MS))
                found = _click_labelled()
            except Exception:
                pass
        if found:
            self.page.keyboard.press("Escape")
            return

        toggles = self.page.locator(SELECTORS["tool_toggle"])
        available = [toggles.nth(i).inner_text().strip()
                     for i in range(toggles.count())]
        self.page.keyboard.press("Escape")
        raise GeminiWebError(
            f"no tool labelled {label!r} in the Upload & tools drawer, even "
            f"after expanding More tools. Available: {available}.\n"
            f"Compute-heavy tools (image, video, canvas) are also WITHDRAWN from "
            f"this drawer while the account is over quota, so one that worked "
            f"yesterday and is missing today is more likely throttled than "
            f"renamed - check the browser before chasing a selector.",
            EXIT_SELECTOR,
        )

    def attach(self, paths: list[str]) -> None:
        missing = [p for p in paths if not os.path.isfile(os.path.expanduser(p))]
        if missing:
            raise GeminiWebError(f"no such file(s): {', '.join(missing)}", EXIT_USAGE)
        # The file input only exists once the drawer has rendered.
        self._tap(SELECTORS["tools_button"])
        self._require("file_input", 10_000, state="attached")
        self.page.set_input_files(
            SELECTORS["file_input"], [os.path.expanduser(p) for p in paths]
        )
        self.page.keyboard.press("Escape")
        # Gemini refuses to send while an upload is in flight; the send key is
        # simply swallowed. Give the chips time to settle.
        self.page.wait_for_timeout(1500 + _ms(READ_PAUSE_MS))

    def send(self, prompt: str) -> int:
        """Type the prompt and submit. Returns the model-response count before
        sending, which wait_for_response() needs as its baseline."""
        prior = self.page.locator(SELECTORS["model_response"]).count()
        editor = self.page.locator(SELECTORS["editor"]).first
        self._tap(editor)
        self._pause()
        # Quill listens for real input events, so neither .fill() nor setting
        # innerText registers. insert_text dispatches one insertText event for
        # the whole string -- far faster than per-character typing on a long
        # prompt, and Quill treats it the same.
        lines = prompt.split("\n")
        for i, line in enumerate(lines):
            if i:
                # Enter submits, so a newline has to be Shift+Enter.
                self.page.keyboard.press("Shift+Enter")
            if line:
                self._type(line)
        # A beat between finishing the prompt and sending it: re-reading what
        # you just wrote is the most universal thing people do here.
        self._pause(READ_PAUSE_MS)
        # Click the button when it is there, and keep Enter as the fallback
        # for the headed case where a redesign moves the button: Enter works
        # there, it is only headless that swallows it.
        try:
            self.page.wait_for_selector(SELECTORS["send_button"], timeout=5_000)
            self._tap(SELECTORS["send_button"])
        except Exception:
            self.page.keyboard.press("Enter")
        return prior

    def wait_for_response(self, prior_count: int, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        sel_resp = SELECTORS["model_response"]
        sel_actions = SELECTORS["response_actions"]

        while self.page.locator(sel_resp).count() <= prior_count:
            if time.monotonic() > deadline:
                raise GeminiWebError(
                    f"Gemini never started a response within {timeout_s:.0f}s. "
                    f"A hard throttle is indistinguishable from a hang at this "
                    f"point -- if the browser is showing a limit notice, that "
                    f"is the cause rather than a wedged page.",
                    EXIT_TIMEOUT,
                )
            self.page.wait_for_timeout(500 + _ms((0, 400)))

        # Done when the action row has rendered AND the text has stopped
        # growing. Either signal alone lies: actions can appear a beat before
        # the last token lands, and a slow model pauses mid-stream.
        probe = """([resp, actions]) => {
            const nodes = document.querySelectorAll(resp);
            const last = nodes[nodes.length - 1];
            if (!last) return {len: 0, done: false};
            return {len: (last.innerText || '').length,
                    done: !!last.querySelector(actions)};
        }"""

        last_len, stable = -1, 0
        while True:
            info = self.page.evaluate(probe, [sel_resp, sel_actions])
            if info["done"] and info["len"] == last_len and info["len"] > 0:
                stable += 1
            else:
                stable = 0
            last_len = info["len"]
            if stable >= 2:
                return
            if time.monotonic() > deadline:
                raise GeminiWebError(
                    f"response still streaming after {timeout_s:.0f}s "
                    f"({last_len} chars so far). Raise --timeout.",
                    EXIT_TIMEOUT,
                )
            self.page.wait_for_timeout(1500 + _ms((0, 600)))

    # -- extraction -------------------------------------------------------
    def last_response(self) -> tuple[str, str]:
        """Returns (markdown, how) where how is 'clipboard' or 'dom'."""
        last = self.page.locator(SELECTORS["model_response"]).last
        try:
            self.page.bring_to_front()
            last.locator(SELECTORS["copy_button"]).first.click(timeout=5_000)
            self.page.wait_for_timeout(300)
            text = self.page.evaluate("navigator.clipboard.readText()")
            if text and text.strip():
                return text.strip(), "clipboard"
        except Exception:
            pass  # headless, unfocused, or the button moved -- fall through

        content = last.locator(SELECTORS["message_content"])
        source = (content.first.inner_html() if content.count()
                  else last.inner_html())
        return html_to_markdown(source), "dom"

    def transcript(self) -> list[dict]:
        """Every turn in the open conversation, oldest first."""
        turns = self.page.evaluate(
            """([qs, rs, ms]) => {
                const out = [];
                document.querySelectorAll(qs).forEach(
                    q => out.push({role: 'user',
                                   html: (q.querySelector('user-query-content')
                                          || q).innerHTML,
                                   order: q.getBoundingClientRect().top + window.scrollY}));
                document.querySelectorAll(rs).forEach(r => {
                    const c = r.querySelector(ms);
                    out.push({role: 'gemini', html: (c || r).innerHTML,
                              order: r.getBoundingClientRect().top + window.scrollY});
                });
                return out.sort((a, b) => a.order - b.order);
            }""",
            [SELECTORS["user_query"], SELECTORS["model_response"],
             SELECTORS["message_content"]],
        )
        return [{"role": t["role"], "markdown": html_to_markdown(t["html"])}
                for t in turns]

    def open_sidebar(self) -> None:
        """Expand the sidebar if it is collapsed, and wait for the list.

        Gemini collapsed the sidebar by default at some point, and Angular does
        not render the history until it is open. Nothing about the markup
        changed, so the symptom was a selector error naming
        `conversations-list a[href^="/app/"]` -- which is still exactly right
        once the panel exists. Clicking is idempotent enough: when the sidebar
        is already open the button is absent and this is a no-op.
        """
        try:
            self._tap(SELECTORS["open_sidebar"], timeout=5_000)
            self.page.wait_for_timeout(1_200 + _ms(CLICK_PAUSE_MS))
        except Exception:
            pass  # already open, or Google renamed the control -- the wait below decides

    def conversations(self, limit: int = 20, query: str = "") -> list[dict]:
        self.open_sidebar()
        self._require("conversation_link", HYDRATE_MS)
        rows = self.page.evaluate(
            """(sel) => [...document.querySelectorAll(sel)].map(a => ({
                   id: (a.getAttribute('href') || '').split('/').pop(),
                   title: (a.innerText || '').trim()}))""",
            SELECTORS["conversation_link"],
        )
        if query:
            q = query.lower()
            rows = [r for r in rows if q in r["title"].lower()]
        return rows[:limit]

    def current_model(self) -> str:
        """The model in the picker, e.g. "Flash".

        Read from the picker button's aria-label first: bard-mode-switcher is
        present but frequently renders no text, so the obvious inner_text call
        returns "" on a perfectly healthy page.
        """
        try:
            label = self.page.get_attribute(SELECTORS["mode_picker"], "aria-label",
                                            timeout=3_000) or ""
            _, _, model = label.partition("currently")
            if model.strip():
                return model.strip()
        except Exception:
            pass
        try:
            el = self.page.locator(SELECTORS["mode_switcher"])
            return (el.first.inner_text() or "").strip() if el.count() else ""
        except Exception:
            return ""


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _emit_meta(**fields) -> None:
    sys.stdout.write("\nGEMINI_WEB_META " + json.dumps(fields, sort_keys=True) + "\n")


def cmd_login(args) -> int:
    """Sign in by hand, in a Chrome that Playwright is NOT driving.

    Google refuses OAuth in a browser under the DevTools protocol -- the flow
    dead-ends on "Couldn't sign you in / This browser or app may not be
    secure", and no user-agent or flag tweak gets past it. That block applies
    to the sign-in flow only, not to cookies that already exist. So: launch a
    plain Chrome against the same user-data-dir, let the account get
    established there, quit, and let Playwright pick the profile up afterwards.
    Chrome refuses to share a user-data-dir between instances, which is why
    this waits for the window to close rather than running alongside it.
    """
    if not os.path.isfile(CHROME_BIN):
        print(f"Error: no Chrome at {CHROME_BIN}. Set GEMINI_WEB_CHROME.",
              file=sys.stderr)
        return EXIT_USAGE

    os.makedirs(PROFILE_DIR, exist_ok=True)
    print(f"Opening a separate Chrome against {PROFILE_DIR}.\n")
    print("That window is its own Chrome instance: no bookmarks, no other")
    print("tabs, not signed in. It is NOT your everyday Chrome, so signing in")
    print("to your everyday window will do nothing here.\n")
    print("  1. Complete the Google sign-in on the page that opens.")
    print("  2. Wait to land on Gemini and see your chats in the sidebar.")
    print("  3. Quit THAT Chrome (Cmd+Q) while it is focused.\n")
    print("This script never sees or types your credentials.")

    proc = subprocess.Popen(
        [CHROME_BIN, f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", LOGIN_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        # Do NOT fall through to the Playwright check. Chrome holds a
        # SingletonLock on the profile for as long as it is open, so launching
        # against it now aborts with "Failed to create a ProcessSingleton" and
        # buries the actual situation under a stack trace.
        print(f"\nChrome is still open after {args.timeout}s, so nothing has "
              "been verified. Finish the sign-in, quit that window, and run "
              "`login` again - it will confirm in seconds if the session took.",
              file=sys.stderr)
        return EXIT_NOT_LOGGED_IN

    cookies = google_cookie_count()
    print(f"\nChrome closed. {cookies} google.com cookie(s) in the profile.")
    if not cookies:
        print("None of them are account cookies, so the sign-in did not "
              "complete in that window. Re-run `login` and finish the Google "
              "sign-in before quitting.", file=sys.stderr)
        return EXIT_NOT_LOGGED_IN
    print("Verifying with Playwright...")
    with Session(headless=False) as s:
        signed_in = s.logged_in()
        account = s.account()
    after = google_cookie_count()
    if after < cookies // 2:
        print(f"\nThe verification launch destroyed the session: {cookies} "
              f"google.com cookies before, {after} after. That is Chrome being "
              "unable to decrypt the profile with the key Playwright gave it. "
              "Check that ignore_default_args still drops --use-mock-keychain, "
              "and allow the macOS Keychain prompt if one appeared.",
              file=sys.stderr)
        return EXIT_NOT_LOGGED_IN
    if signed_in:
        print(f"Signed in as: {account or '(unknown)'}")
        print(f"{after} google.com cookies survived the Playwright launch, "
              "which is the part that used to break. You should not need to "
              "do this again.")
        return EXIT_OK
    print("Still signed out. Re-run `login` and make sure the Gemini sidebar "
          "shows your chats before quitting Chrome.", file=sys.stderr)
    return EXIT_NOT_LOGGED_IN


def cmd_status(args) -> int:
    info = {"profile": PROFILE_DIR, "headless": _truthy("GEMINI_WEB_HEADLESS"),
            "google_cookies": google_cookie_count()}
    with Session() as s:
        info["logged_in"] = s.logged_in()
        info["chrome_version"] = s.context.browser.version if s.context.browser else ""
        info["account"] = s.account()
        info["model"] = s.current_model() if info["logged_in"] else ""
        if info["logged_in"]:
            try:
                info["conversations"] = len(s.conversations(limit=1000))
            except GeminiWebError:
                info["conversations"] = None
    print(json.dumps(info, indent=2, sort_keys=True))
    return EXIT_OK if info["logged_in"] else EXIT_NOT_LOGGED_IN


def cmd_ask(args) -> int:
    started = time.monotonic()
    with Session() as s:
        s.open(args.conversation, expect_history=bool(args.conversation))
        if args.mode != "chat":
            s.select_tool(args.mode)
        if args.file:
            s.attach(args.file)
        prior = s.send(args.prompt)
        s.wait_for_response(prior, args.timeout)
        markdown, how = s.last_response()
        cid = conversation_id_from_url(s.page.url)
        model = s.current_model()

    # Raised outside the `with` so Chrome has already shut down cleanly.
    kind, pat = classify_response(markdown)
    if kind == "throttled":
        raise GeminiWebError(
            f"Gemini replied with a limit message rather than an answer "
            f"(matched {pat!r}): {markdown.strip()}\n"
            f"Do NOT retry in a loop - that is how a soft limit becomes a hard "
            f"one. The live numbers are in the web app under Settings -> Usage "
            f"limits. Conversation: {cid or '(none)'}",
            EXIT_THROTTLED,
        )
    if kind == "transient":
        raise GeminiWebError(
            f"Gemini glitched rather than answering (matched {pat!r}): "
            f"{markdown.strip()}\n"
            f"This one is usually transient and a single retry succeeds. "
            f"Conversation: {cid or '(none)'}",
            EXIT_TRANSIENT,
        )

    sys.stdout.write(markdown.rstrip() + "\n")
    if args.out:
        path = os.path.expanduser(args.out)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(markdown.rstrip() + "\n")
    _emit_meta(conversation_id=cid, mode=args.mode, extraction=how, model=model,
               elapsed_seconds=round(time.monotonic() - started, 1),
               chars=len(markdown), out=args.out or "")
    return EXIT_OK


def cmd_read(args) -> int:
    with Session() as s:
        s.open(args.conversation, expect_history=True)
        turns = s.transcript()
        cid = conversation_id_from_url(s.page.url)
    for turn in turns:
        sys.stdout.write(f"\n## {'You' if turn['role'] == 'user' else 'Gemini'}\n\n")
        sys.stdout.write(turn["markdown"].rstrip() + "\n")
    _emit_meta(conversation_id=cid, turns=len(turns))
    return EXIT_OK


def cmd_list(args) -> int:
    with Session() as s:
        s.open()
        rows = s.conversations(limit=args.limit, query=args.query)
    print(json.dumps(rows, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gemini_web.py",
        description="Drive the Gemini web app (Canvas, conversation history, "
                    "attachments) from the command line.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    lg = sub.add_parser("login", help="open Chrome so you can sign in by hand")
    lg.add_argument("--timeout", type=int, default=300)
    lg.set_defaults(func=cmd_login)

    st = sub.add_parser("status", help="profile path, sign-in state, model")
    st.set_defaults(func=cmd_status)

    ak = sub.add_parser("ask", help="send a prompt and print the response")
    ak.add_argument("--prompt", required=True)
    ak.add_argument("--conversation", default="",
                    help="resume this /app/<id> conversation instead of a new one")
    ak.add_argument("--mode", default="chat",
                    choices=["chat"] + sorted(TOOL_LABELS))
    ak.add_argument("--file", action="append", default=[],
                    help="attach a file; repeatable")
    ak.add_argument("--out", default="", help="also write the markdown here")
    ak.add_argument("--timeout", type=float, default=300.0)
    ak.set_defaults(func=cmd_ask)

    rd = sub.add_parser("read", help="dump a conversation as markdown")
    rd.add_argument("--conversation", required=True)
    rd.set_defaults(func=cmd_read)

    ls = sub.add_parser("list", help="list conversations as JSON")
    ls.add_argument("--query", default="")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=cmd_list)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GeminiWebError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
