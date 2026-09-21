#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Exercises gemini_web.py's pure logic without a browser.

Everything here runs offline in well under a second: markdown extraction from
captured response HTML, the meta line the MCP server parses a conversation id
out of, and the argv the server builds. Playwright is never imported - the
Session class is the only thing that needs it, and nothing here touches it.

The extraction is deliberately Python-on-HTML rather than JS-in-the-page so
that it can be tested exactly like this. When Gemini's markup changes, capture
the new response subtree into test/fixtures/ and add a case; the fixtures are
the regression record for a DOM we do not control.
"""

import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load(filename, alias):
    spec = importlib.util.spec_from_file_location(
        alias, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


gw = _load("gemini_web.py", "gw_under_test")


class MarkdownExtractionTests(unittest.TestCase):
    def md(self, source):
        return gw.html_to_markdown(source)

    def test_code_block_keeps_its_fence_and_language(self):
        """The case innerText scraping gets wrong, and the reason the DOM
        fallback exists at all: an unfenced code block is unusable to whoever
        asked for the code."""
        out = self.md('<pre><code class="language-python">def f(x):\n'
                      '    return x * 2\n</code></pre>')
        self.assertEqual(out, "```python\ndef f(x):\n    return x * 2\n```")

    def test_code_block_without_a_language_still_fences(self):
        self.assertEqual(self.md("<pre>raw\nlines\n</pre>"),
                         "```\nraw\nlines\n```")

    def test_inline_code_is_not_confused_with_a_block(self):
        self.assertEqual(self.md("<p>Run <code>ls -la</code> first.</p>"),
                         "Run `ls -la` first.")

    def test_ordered_and_nested_lists(self):
        out = self.md("<ol><li>one</li><li>two</li></ol>")
        self.assertEqual(out, "1. one\n2. two")
        nested = self.md("<ul><li>a<ul><li>b</li></ul></li></ul>")
        self.assertIn("- a", nested)
        self.assertIn("  - b", nested)

    def test_links_and_emphasis(self):
        self.assertEqual(
            self.md('<p>A <strong>bold</strong> <a href="https://x.dev">link</a>.</p>'),
            "A **bold** [link](https://x.dev).")

    def test_table_gets_a_header_separator(self):
        out = self.md("<table><tr><th>N</th><th>C</th></tr>"
                      "<tr><td>a</td><td>1</td></tr></table>")
        self.assertEqual(out, "| N | C |\n| --- | --- |\n| a | 1 |")

    def test_gemini_chrome_is_stripped(self):
        """The action row, disclaimers and icons are part of every response
        subtree and are not part of the answer."""
        out = self.md(
            "<message-content><p>answer</p>"
            '<message-actions><button aria-label="Copy">Copy</button></message-actions>'
            "<model-response-disclaimers>Gemini can make mistakes"
            "</model-response-disclaimers></message-content>")
        self.assertEqual(out, "answer")

    def test_headings_survive(self):
        self.assertEqual(self.md("<h2>Findings</h2><p>one</p>"),
                         "## Findings\n\none")

    def test_empty_input_is_empty_not_an_error(self):
        self.assertEqual(self.md(""), "")
        self.assertEqual(self.md("<div></div>"), "")

    def test_screen_reader_label_is_not_duplicated(self):
        """Every user turn carries a cdk-visually-hidden <h5> that repeats the
        whole prompt after "You said". On screen it is invisible; extracted
        naively it doubles the turn."""
        out = self.md(
            '<user-query-content><h5 class="cdk-visually-hidden '
            'screen-reader-user-query-label">You said hello there</h5>'
            '<div class="query-content">hello there</div></user-query-content>')
        self.assertEqual(out, "hello there")

    def test_language_comes_from_the_header_strip_not_a_class(self):
        """Gemini puts the language in a header span above the block, not in a
        language-* class. Untreated it becomes a stray line above an
        unlabelled fence."""
        out = self.md(
            '<code-block><div class="code-block-decoration">'
            '<span>Python</span></div>'
            '<pre><code>x = 1\n</code></pre></code-block>')
        self.assertEqual(out, "```python\nx = 1\n```")

    def test_fixture_response_matches_what_the_clipboard_gave(self):
        """The DOM fallback has to agree with the clipboard path, or a headless
        run silently returns worse markdown than a headed one."""
        path = os.path.join(FIXTURES, "response-code-block.html")
        if not os.path.isfile(path):
            self.skipTest("fixture not captured")
        with open(path) as fh:
            out = gw.html_to_markdown(fh.read())
        self.assertTrue(out.startswith("```python"), out[:60])
        self.assertTrue(out.rstrip().endswith("```"), out[-60:])
        self.assertNotIn("Python\n\n```", out, "language leaked as a text line")

    def test_a_reasoning_trace_is_not_part_of_the_answer(self):
        """This wrapper no longer drives Deep Research, but you can still start
        one by hand and read the thread back. Its panel carries the reasoning
        trace and a browse chip per site visited, which dwarf the answer."""
        out = self.md(
            "<deep-research-immersive-panel>"
            "<thinking-panel><thought-item>Analyzing the evolution of memory "
            "stores</thought-item></thinking-panel>"
            "<browse-chip-list><browse-web-chip>redis.io</browse-web-chip>"
            "</browse-chip-list>"
            "<h1>Redis vs Valkey</h1><p>The fork diverged in 2024.</p>"
            "</deep-research-immersive-panel>")
        self.assertEqual(out, "# Redis vs Valkey\n\nThe fork diverged in 2024.")

    def test_a_panel_that_is_still_working_yields_nothing(self):
        """Mid-run it is trace and a spinner and nothing else. It must come back
        empty rather than reading as a short answer."""
        out = self.md(
            "<deep-research-immersive-panel><mat-progress-spinner>"
            "</mat-progress-spinner><thinking-panel-skeleton-loader>"
            "</thinking-panel-skeleton-loader><thinking-panel>"
            "<thought-item>Researching</thought-item></thinking-panel>"
            "</deep-research-immersive-panel>")
        self.assertEqual(out, "")

    def test_captured_fixtures_round_trip(self):
        """Every .html in test/fixtures/ must still convert to something
        non-empty. Add one whenever Gemini's markup shifts."""
        if not os.path.isdir(FIXTURES):
            self.skipTest("no fixtures captured yet")
        names = [n for n in os.listdir(FIXTURES) if n.endswith(".html")]
        if not names:
            self.skipTest("no fixtures captured yet")
        for name in names:
            with self.subTest(fixture=name):
                with open(os.path.join(FIXTURES, name)) as fh:
                    out = gw.html_to_markdown(fh.read())
                self.assertTrue(out.strip(), f"{name} converted to nothing")
                self.assertNotIn("<div", out, f"{name} leaked raw HTML")


class LinkUnwrappingTests(unittest.TestCase):
    """Gemini routes outbound links through a google.com redirect: the anchor
    text shows the real destination, the href does not. An answer whose whole
    value is that you can check it must not hand back links that go somewhere
    else."""

    def test_a_wrapped_link_comes_back_pointing_at_the_real_page(self):
        out = gw.html_to_markdown(
            '<a href="https://www.google.com/search?q=https%3A%2F%2Fgithub.com'
            '%2Fnodejs%2Fnode%2Freleases%2Ftag%2Fv26.9.0&utm_source=gemini">'
            'github.com/nodejs/node</a>')
        self.assertEqual(
            out, "[github.com/nodejs/node]"
                 "(https://github.com/nodejs/node/releases/tag/v26.9.0)")

    def test_an_ordinary_link_is_left_alone(self):
        self.assertEqual(gw._unwrap_google_redirect("https://vite.dev/blog"),
                         "https://vite.dev/blog")

    def test_a_genuine_google_search_link_survives(self):
        """The q parameter only holds a URL when it IS a redirect. A real
        search link has a query in there and must not be mangled."""
        url = "https://www.google.com/search?q=redis+vs+valkey"
        self.assertEqual(gw._unwrap_google_redirect(url), url)


class PacingTests(unittest.TestCase):
    """Timing jitter and the typing burst splitter.

    The units here have already been wrong once: the spans are named `_MS` and
    an early `_ms()` multiplied them by 1000 as well, which would have paused
    90 seconds before every click. Cheap to assert, expensive to notice live.
    """

    SPANS = ("CLICK_PAUSE_MS", "READ_PAUSE_MS", "TYPE_PAUSE_MS")

    def test_pauses_are_milliseconds_not_seconds(self):
        for name in self.SPANS:
            span = getattr(gw, name)
            with self.subTest(span=name):
                self.assertLess(max(span), 5_000, f"{name} looks like seconds")
                for _ in range(200):
                    self.assertTrue(span[0] <= gw._ms(span) <= span[1])

    def test_pacing_can_be_switched_off(self):
        """Debugging a selector against a page that pauses is miserable."""
        try:
            gw.PACING = False
            self.assertEqual(gw._ms(gw.READ_PAUSE_MS), 0)
        finally:
            gw.PACING = True

    def test_typing_bursts_reassemble_into_the_original_prompt(self):
        """The whole point is that the text is split. If a burst boundary ever
        drops or reorders a character, every prompt is silently corrupted and
        the answers just get subtly wrong."""
        class FakeKeyboard:
            def __init__(self): self.chunks = []
            def insert_text(self, t): self.chunks.append(t)

        class FakePage:
            def __init__(self): self.keyboard = FakeKeyboard()
            def wait_for_timeout(self, ms): pass

        for text in ("", "pong", "x" * 40, "unicode — ok? ✓ " * 30, "y" * 2000):
            with self.subTest(length=len(text)):
                session = gw.Session.__new__(gw.Session)
                session.page = FakePage()
                session._type(text)
                self.assertEqual("".join(session.page.keyboard.chunks), text)

    def test_a_long_prompt_is_not_typed_out_one_burst_at_a_time(self):
        """Nobody hand-types 2000 characters into a chat box; they paste. So
        past the budget the remainder goes in as one event -- which is both
        more realistic and keeps a long prompt from taking minutes."""
        class FakeKeyboard:
            def __init__(self): self.chunks = []
            def insert_text(self, t): self.chunks.append(t)

        class FakePage:
            def __init__(self): self.keyboard = FakeKeyboard()
            def wait_for_timeout(self, ms): pass

        session = gw.Session.__new__(gw.Session)
        session.page = FakePage()
        session._type("z" * 3000)
        self.assertLess(len(session.page.keyboard.chunks),
                        gw.TYPE_BUDGET_CHARS // min(gw.TYPE_BURST) + 5)
        self.assertGreater(len(session.page.keyboard.chunks[-1]), 1_000)

    def test_the_window_size_is_stable_across_runs(self):
        """Deliberately NOT random per launch: a window that is a different
        size every session is an inconsistency a fixed size never produces."""
        import tempfile, unittest.mock as mock
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gw, "PROFILE_DIR", tmp):
                first = gw._viewport_for_profile()
                self.assertEqual(first, gw._viewport_for_profile())
                self.assertEqual(first, gw._viewport_for_profile())

    def test_the_automation_switches_are_dropped(self):
        self.assertIn("--enable-automation", gw.STEALTH_IGNORE)
        self.assertTrue(any("AutomationControlled" in a for a in gw.STEALTH_ARGS))

    def test_the_keychain_flag_is_still_ignored(self):
        """STEALTH_IGNORE is spread into the same ignore_default_args list as
        --use-mock-keychain. Dropping that one destroys the saved session on
        every launch, so it must survive any edit to the stealth list."""
        import inspect
        src = inspect.getsource(gw.Session.__enter__)
        self.assertIn("--use-mock-keychain", src)
        self.assertIn("STEALTH_IGNORE", src)


class SidebarTests(unittest.TestCase):
    """The history list is behind a collapsed sidebar.

    This cost a real debugging detour: Gemini began shipping the sidebar
    collapsed, Angular does not render the conversation list until it opens,
    and the failure surfaced as `selector 'conversation_link' never appeared` --
    which reads like Google moved the markup. Nothing had moved. The selector
    was right the whole time; the panel holding it did not exist yet.
    """

    def test_the_sidebar_toggle_is_a_named_selector(self):
        self.assertIn("open_sidebar", gw.SELECTORS)

    def test_listing_conversations_opens_the_sidebar_first(self):
        """Without this call the list is empty on a collapsed sidebar, and the
        error blames the wrong selector."""
        import inspect
        src = inspect.getsource(gw.Session.conversations)
        self.assertIn("open_sidebar", src)


class MetaLineTests(unittest.TestCase):
    """The MCP server only ever sees the worker's stdout as a file, so this
    line is the whole channel for the conversation id."""

    def test_parses_the_trailing_meta_line(self):
        out = ('the answer\n'
               'GEMINI_WEB_META {"conversation_id": "abc123", "chars": 10}\n')
        self.assertEqual(gw.parse_meta_line(out)["conversation_id"], "abc123")

    def test_last_meta_line_wins(self):
        out = ('GEMINI_WEB_META {"conversation_id": "old"}\n'
               'GEMINI_WEB_META {"conversation_id": "new"}\n')
        self.assertEqual(gw.parse_meta_line(out)["conversation_id"], "new")

    def test_missing_or_malformed_meta_is_empty_not_fatal(self):
        """A lost id must never turn a good answer into an error."""
        self.assertEqual(gw.parse_meta_line("just an answer"), {})
        self.assertEqual(gw.parse_meta_line("GEMINI_WEB_META {oops"), {})

    def test_an_answer_mentioning_the_marker_does_not_confuse_it(self):
        out = 'I would print GEMINI_WEB_META {"x": 1} to stdout.\n'
        self.assertEqual(gw.parse_meta_line(out), {})


class ConversationIdTests(unittest.TestCase):
    def test_extracts_the_id_from_an_app_url(self):
        self.assertEqual(
            gw.conversation_id_from_url(
                "https://gemini.google.com/app/aed9ce8c04659255"),
            "aed9ce8c04659255")

    def test_a_new_chat_url_has_no_id_yet(self):
        self.assertEqual(gw.conversation_id_from_url("https://gemini.google.com/app"), "")
        self.assertEqual(gw.conversation_id_from_url(""), "")


class WorkerCliTests(unittest.TestCase):
    def parse(self, argv):
        return gw.build_parser().parse_args(argv)

    def test_ask_defaults_to_chat(self):
        args = self.parse(["ask", "--prompt", "hi"])
        self.assertEqual(args.mode, "chat")
        self.assertEqual(args.conversation, "")
        self.assertEqual(args.file, [])

    def test_every_tool_label_is_an_accepted_mode(self):
        """The server's MODES tuple and the worker's choices have to agree, or
        a dispatch is rejected by one and accepted by the other."""
        for mode in gw.TOOL_LABELS:
            with self.subTest(mode=mode):
                self.assertEqual(self.parse(["ask", "--prompt", "x",
                                             "--mode", mode]).mode, mode)

    def test_files_are_repeatable(self):
        args = self.parse(["ask", "--prompt", "x", "--file", "a", "--file", "b"])
        self.assertEqual(args.file, ["a", "b"])

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(["ask", "--prompt", "x", "--mode", "telepathy"])

    def test_read_requires_a_conversation(self):
        with self.assertRaises(SystemExit):
            self.parse(["read"])


class ServerArgvTests(unittest.TestCase):
    """The server builds the worker's command line; a drift between the two is
    only visible at runtime otherwise."""

    @classmethod
    def setUpClass(cls):
        import types
        if "mcp" not in sys.modules:
            mcp = types.ModuleType("mcp")
            server = types.ModuleType("mcp.server")

            class MCPServer:
                def __init__(self, name, version=None, instructions=None):
                    self.name = name

                def tool(self, *a, **kw):
                    return lambda fn: fn

                def run(self):
                    raise AssertionError("mcp.run() must not be reached")

            server.MCPServer = MCPServer
            mcp.server = server
            sys.modules["mcp"], sys.modules["mcp.server"] = mcp, server
        cls.srv = _load("gemini_web_mcp_server.py", "gw_server_under_test")

    def test_modes_match_the_worker(self):
        self.assertEqual(set(self.srv.MODES),
                         {"chat"} | set(gw.TOOL_LABELS))

    def test_worker_argv_runs_the_script_through_uv(self):
        argv = self.srv._worker_argv("status")
        self.assertEqual(argv[1:3], ["run", "--script"])
        self.assertTrue(argv[3].endswith("gemini_web.py"))
        self.assertEqual(argv[4], "status")

    def test_worker_cap_stays_under_the_wall_clock(self):
        """The worker's own deadline has to hit first: it exits with whatever
        the page rendered, where the outer kill leaves nothing to show."""
        self.assertLess(self.srv.WORKER_TIMEOUT, self.srv.TIMEOUT_SECONDS)

    def test_meta_helpers_agree_with_the_worker(self):
        out = 'answer\nGEMINI_WEB_META {"conversation_id": "zz"}\n'
        self.assertEqual(self.srv._meta_of(out),
                         gw.parse_meta_line(out))
        self.assertEqual(self.srv._strip_meta(out), "answer")

    def test_an_unknown_mode_is_refused_before_a_browser_opens(self):
        self.assertIn("mode must be one of",
                      self.srv.dispatch_gemini("x", mode="telepathy"))
        self.assertIn("mode must be one of",
                      self.srv.gemini_ask("x", mode="telepathy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
