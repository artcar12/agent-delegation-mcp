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

    def test_thinking_trace_is_not_part_of_the_report(self):
        """A Deep Research panel holds the reasoning trace and the browse chips
        alongside the report, and they dwarf it. Only the report is the
        deliverable."""
        out = self.md(
            "<deep-research-immersive-panel>"
            "<thinking-panel><thought-item>Analyzing the evolution of memory "
            "stores</thought-item></thinking-panel>"
            "<browse-chip-list><browse-web-chip>redis.io</browse-web-chip>"
            "</browse-chip-list>"
            "<h1>Redis vs Valkey</h1><p>The fork diverged in 2024.</p>"
            "</deep-research-immersive-panel>")
        self.assertEqual(out, "# Redis vs Valkey\n\nThe fork diverged in 2024.")

    def test_a_still_running_panel_yields_no_report_text(self):
        """While running, the panel is nothing but trace and a spinner. It must
        not read as a short report."""
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


class DeepResearchReportTests(unittest.TestCase):
    """The two captured panels are a matched pair, taken minutes apart from two
    conversations whose own chat turns said, verbatim, "I'm on it. I'll let you
    know when your research is done" and "I've completed your research."

    That pairing is the point. The completion marker in RESEARCH_STATE_JS was
    not reasoned about, it was read off these two DOMs by keeping only what
    differed -- and the obvious marker, "is a loading skeleton still mounted?",
    is present in BOTH, which is exactly the mistake these fixtures exist to
    stop anyone making again.
    """

    COMPLETE = "deep-research-report.html"
    RUNNING = "deep-research-running.html"

    def fixture(self, name):
        path = os.path.join(FIXTURES, name)
        if not os.path.isfile(path):
            self.skipTest(f"{name} not captured")
        with open(path) as fh:
            return fh.read()

    # The Python mirror of RESEARCH_STATE_JS's `busy` expression. Structural,
    # so it can run over captured HTML with no browser.
    @staticmethod
    def busy(html):
        body = 'id="extended-response-markdown-content"' in html
        return (not body
                or 'aria-busy="true"' in html
                or "<mat-progress-spinner" in html)

    def test_the_pair_is_actually_a_pair(self):
        """If both fixtures ever read the same way, every other test in this
        class is vacuous."""
        self.assertFalse(self.busy(self.fixture(self.COMPLETE)))
        self.assertTrue(self.busy(self.fixture(self.RUNNING)))

    def test_the_skeleton_loader_is_not_the_marker(self):
        """It is mounted, visible and 200px tall on a FINISHED report. Checking
        it reports every completed report as running, forever -- which is the
        bug this fixture was captured to close."""
        self.assertIn("thinking-panel-skeleton-loader",
                      self.fixture(self.COMPLETE))
        self.assertNotIn("thinking-panel-skeleton-loader",
                         gw.Session.RESEARCH_STATE_JS)

    def test_completed_panel_yields_the_report(self):
        out = gw.research_html_to_markdown(self.fixture(self.COMPLETE))
        self.assertTrue(out.startswith("# Architectural Foundations"), out[:80])
        self.assertIn("Rolldown", out)

    def test_the_reasoning_trace_stays_out_of_the_report(self):
        """26 thought items and 81 browse chips sit in the same panel as the
        report. They are working notes; shipping them as the deliverable buries
        it."""
        out = gw.research_html_to_markdown(self.fixture(self.COMPLETE))
        for leak in ("I'm sorting through it", "bringing it all together",
                     "thought-header"):
            self.assertNotIn(leak, out)

    def test_sources_become_a_citation_list(self):
        """A research report without its citations is a worse report, and the
        generic walk flattens each source into "[vite.devVite 8.1 is out!]"
        because the domain and title are block divs inside one <a>."""
        out = gw.research_html_to_markdown(self.fixture(self.COMPLETE))
        self.assertIn("## Sources", out)
        tail = out[out.index("## Sources"):]
        self.assertRegex(tail, r"\n- \[[^\]]+\]\(https?://[^)]+\) -- \S+")
        self.assertNotRegex(tail, r"\[[a-z0-9.-]+\.[a-z]{2,}[A-Z]")

    def test_a_running_panel_never_yields_a_report(self):
        """The failure that costs the most: returning the research PLAN, or a
        few hundred characters of trace, as though it were the deliverable."""
        out = gw.research_html_to_markdown(self.fixture(self.RUNNING))
        self.assertNotIn("## Sources", out)
        self.assertLess(len(out), 200, out[:300])

    def test_the_panel_grace_outlasts_its_measured_mount(self):
        """The panel mounts seconds after the chat turns beside it; reading
        state immediately calls a running report 'absent', which reads as a
        terminal answer."""
        self.assertGreaterEqual(gw.Session.PANEL_GRACE_MS, 10_000)


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

    def test_research_defaults_to_not_waiting(self):
        """The default has to be a single cheap check: the browser is a shared
        singleton and the research outlasts any sensible block."""
        args = self.parse(["research", "--conversation", "abc"])
        self.assertEqual(args.wait, 0.0)
        self.assertEqual(args.poll, 60.0)

    def test_research_requires_a_conversation(self):
        with self.assertRaises(SystemExit):
            self.parse(["research"])


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

    def test_deep_research_is_refused_by_the_synchronous_tool(self):
        """It runs for 10-20 minutes; letting it into gemini_ask only produces
        a timeout with the work already half-done on the account."""
        out = self.srv.gemini_ask("x", mode="deep-research")
        self.assertIn("dispatch_gemini", out)

    def test_research_tools_refuse_an_empty_conversation_id(self):
        """Without an id there is nothing to open, and a browser launch costs
        ~20s before it could say so."""
        for out in (self.srv.gemini_research(""),
                    self.srv.dispatch_research("   ")):
            self.assertIn("conversation_id is required", out)

    def test_harvest_wait_has_a_floor(self):
        """dispatch_research exists to wait. A zero or tiny wait would record a
        run that returns "still running" instantly and teaches the caller
        nothing."""
        import unittest.mock as mock
        with mock.patch.object(self.srv, "_dispatch",
                               side_effect=lambda *a, **k: a[1]) as _:
            argv = self.srv.dispatch_research("abc", wait_seconds=0)
        self.assertIn("--wait", argv)
        self.assertGreaterEqual(int(argv[argv.index("--wait") + 1]), 60)

    def test_an_unknown_mode_is_refused_before_a_browser_opens(self):
        self.assertIn("mode must be one of",
                      self.srv.dispatch_gemini("x", mode="telepathy"))
        self.assertIn("mode must be one of",
                      self.srv.gemini_ask("x", mode="telepathy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
