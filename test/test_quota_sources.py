#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Parsers and collectors in quota_sources.py, against synthetic fixtures only.

Every fixture here is invented. The Codex session files in particular hold real
conversations, so none is copied, trimmed or quoted: the fake files below carry
made-up chatter around the rate_limits lines to prove it is never read back.

Runs with plain `python3 test/test_quota_sources.py`.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import quota_sources as q  # noqa: E402

NOW = q._parse_iso("2026-01-10T12:00:00Z")

# collect() reads the limit ledger; never the real one.
_STORE = tempfile.mkdtemp(prefix="quota-store-")
os.environ["AGENT_MCP_RUN_DIR"] = _STORE
atexit.register(shutil.rmtree, _STORE, True)

AGY_JSON = {
    "command": {"name": "usage", "data": {"groups": [
        {"name": "Pool A", "buckets": [
            {"id": "a-weekly", "name": "Weekly Limit Remaining", "window": "weekly",
             "remaining_fraction": 0.75, "reset_time": "2026-01-15T00:00:00Z"},
            {"id": "a-5h", "name": "Five Hour Limit Remaining", "window": "5h",
             "remaining_fraction": 1, "reset_time": "2026-01-10T15:00:00Z"},
        ]},
        # A group with a single bucket and no `window` field.
        {"name": "Pool B", "buckets": [
            {"id": "b-weekly", "name": "Pool B Weekly Limit",
             "remaining_fraction": 0.4, "reset_time": "2026-01-16T00:00:00Z"},
        ]},
    ]}},
    "response": "ignored when groups are present",
    "num_turns": 0,
}

AGY_TEXT = ("Pool A\tWeekly Limit Remaining\t75%\t2026-01-15T00:00:00Z\n"
            "Pool A\tFive Hour Limit Remaining\t100%\t2026-01-10T15:00:00Z\n"
            "Pool B\tPool B Weekly Limit\t40%\t2026-01-16T00:00:00Z\n")

RATE_LIMITS = {
    "limit_id": "codex",
    "primary": {"used_percent": 20.0, "window_minutes": 300,
                "resets_at": int(NOW) + 3600},
    "secondary": {"used_percent": 55.5, "window_minutes": 10080,
                  "resets_at": int(NOW) + 3 * 86400},
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "plan_type": "plus",
    "rate_limit_reached_type": None,
    "some_future_field": {"nested": True},
}

SECRET = "SYNTHETIC-CONVERSATION-TEXT-MUST-NOT-LEAK"


def _session_lines(rate_limits_list):
    """A fake session file: chatter lines with SECRET, plus one token_count
    event per entry (None gives a null rate_limits)."""
    lines = [json.dumps({"type": "response_item", "payload": {"type": "message", "content": SECRET}})]
    for rl in rate_limits_list:
        lines.append(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"note": SECRET}, "rate_limits": rl}}))
        lines.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "content": SECRET + ' "rate_limits" mentioned in text'}}))
    return "\n".join(lines) + "\n"


class AgyTests(unittest.TestCase):
    def test_json_groups(self):
        rows = q.parse_agy_usage(json.dumps(AGY_JSON), NOW)
        self.assertEqual([(r["pool"], r["window"], r["used_percent"], r["left_percent"]) for r in rows], [
            ("Pool A", "weekly", 25.0, 75.0),
            ("Pool A", "5h", 0.0, 100.0),
            ("Pool B", "weekly", 60.0, 40.0),
        ])
        self.assertEqual(rows[0]["resets_at"], "2026-01-15T00:00:00Z")
        self.assertFalse(rows[0]["reset_passed"])

    def test_text_rows(self):
        rows = q.parse_agy_usage(AGY_TEXT, NOW)
        self.assertEqual([(r["pool"], r["window"], r["left_percent"]) for r in rows], [
            ("Pool A", "weekly", 75.0), ("Pool A", "5h", 100.0), ("Pool B", "weekly", 40.0)])

    def test_json_without_groups_falls_back_to_response_text(self):
        rows = q.parse_agy_usage(json.dumps({"response": AGY_TEXT}), NOW)
        self.assertEqual(len(rows), 3)

    def test_garbage_raises_value_error(self):
        with self.assertRaises(ValueError):
            q.parse_agy_usage("Error: not signed in\n", NOW)

    def test_passed_reset_is_marked(self):
        text = "Pool A\tWeekly Limit Remaining\t10%\t2026-01-01T00:00:00Z\n"
        row = q.parse_agy_usage(text, NOW)[0]
        self.assertTrue(row["reset_passed"])
        self.assertEqual(row["used_percent"], 0.0)

    def test_unstarted_pool_has_no_reset(self):
        doc = {"command": {"data": {"groups": [{"name": "Pool C", "buckets": [
            {"window": "weekly", "remaining_fraction": 1,
             "reset_time": "1970-01-01T00:00:00Z"}]}]}}}
        w = q.parse_agy_usage(json.dumps(doc), NOW)[0]
        self.assertEqual((w["resets_at"], w["reset_passed"], w["left_percent"],
                          w["not_started"]), (None, False, 100.0, True))
        table = q.render_table({"generated_at": q._iso(NOW), "sources": [
            q._source("agy", windows=[w], as_of=q._iso(NOW))]})
        self.assertNotIn("1970", table)
        self.assertNotIn("reset since snapshot", table)
        self.assertIn("window not started", table)

    def test_missing_cli_is_unavailable(self):
        saved = os.environ.get("AGY_BIN")
        os.environ["AGY_BIN"] = "/nonexistent/agy"
        try:
            src = q.collect_agy(NOW)
        finally:
            if saved is None:
                os.environ.pop("AGY_BIN")
            else:
                os.environ["AGY_BIN"] = saved
        self.assertEqual(src["status"], "unavailable")
        self.assertIn("not found", src["reason"])


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="codexhome-")
        self.day = os.path.join(self.home, "sessions", "2026", "01", "10")
        os.makedirs(self.day)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write(self, name, rls, mtime):
        path = os.path.join(self.day, name)
        with open(path, "w") as fh:
            fh.write(_session_lines(rls))
        os.utime(path, (mtime, mtime))
        return path

    def test_parse_windows_and_extra(self):
        windows, extra = q.parse_codex_rate_limits(RATE_LIMITS, NOW)
        self.assertEqual([(w["pool"], w["window"], w["used_percent"], w["left_percent"]) for w in windows],
                         [("codex", "5h", 20.0, 80.0), ("codex", "weekly", 55.5, 44.5)])
        self.assertEqual(extra["plan_type"], "plus")
        self.assertEqual(extra["credits"], {"has_credits": False, "unlimited": False, "balance": "0"})
        self.assertIsNone(extra["limit_reached"])
        self.assertNotIn("some_future_field", json.dumps(extra))

    def test_window_names(self):
        self.assertEqual(q._codex_window_name(300), "5h")
        self.assertEqual(q._codex_window_name(10080), "weekly")
        self.assertEqual(q._codex_window_name(1440), "1d")
        self.assertEqual(q._codex_window_name(90), "90m")

    def test_newest_file_with_rate_limits_wins_and_last_snapshot_in_it(self):
        older = dict(RATE_LIMITS, primary=dict(RATE_LIMITS["primary"], used_percent=5.0))
        newer = dict(RATE_LIMITS, primary=dict(RATE_LIMITS["primary"], used_percent=33.0))
        self.write("a.jsonl", [RATE_LIMITS], NOW - 7200)
        self.write("b.jsonl", [older, newer, None], NOW - 3600)   # trailing null skipped
        self.write("c.jsonl", [None], NOW - 60)                    # newest, but nothing
        src = q.collect_codex(NOW, home=self.home)
        self.assertEqual(src["status"], "ok")
        self.assertEqual(src["windows"][0]["used_percent"], 33.0)
        self.assertEqual(src["as_of"], q._iso(NOW - 3600))
        self.assertNotIn(SECRET, json.dumps(src))
        self.assertNotIn(SECRET, q.render_table({"generated_at": q._iso(NOW), "sources": [src]}))

    def test_passed_reset_marks_window_reset_and_keeps_snapshot(self):
        stale = dict(RATE_LIMITS, primary=dict(RATE_LIMITS["primary"], resets_at=int(NOW) - 10))
        self.write("a.jsonl", [stale], NOW - 7200)
        src = q.collect_codex(NOW, home=self.home)
        five = src["windows"][0]
        self.assertTrue(five["reset_passed"])
        self.assertEqual(five["used_percent"], 0.0)
        self.assertEqual(src["extra"]["snapshot_used_percent"]["5h"], 20.0)
        table = q.render_table({"generated_at": q._iso(NOW), "sources": [src]})
        self.assertIn("reset since snapshot (was 20% used)", table)

    def test_bad_newest_snapshot_falls_back_to_an_older_file(self):
        self.write("a.jsonl", [RATE_LIMITS], NOW - 7200)
        self.write("b.jsonl", [{"primary": None, "secondary": None}], NOW - 60)
        src = q.collect_codex(NOW, home=self.home)
        self.assertEqual(src["status"], "ok")
        self.assertEqual(src["as_of"], q._iso(NOW - 7200))

    def test_no_sessions_dir_is_unavailable(self):
        src = q.collect_codex(NOW, home=os.path.join(self.home, "nope"))
        self.assertEqual(src["status"], "unavailable")

    def test_no_rate_limits_anywhere_is_unavailable(self):
        self.write("a.jsonl", [None, None], NOW)
        src = q.collect_codex(NOW, home=self.home)
        self.assertEqual(src["status"], "unavailable")
        self.assertIn("no rate_limits", src["reason"])
        self.assertNotIn(SECRET, src["reason"])

    def test_corrupt_line_is_skipped_silently(self):
        path = self.write("a.jsonl", [RATE_LIMITS], NOW)
        with open(path, "a") as fh:
            fh.write('{"payload": {"rate_limits": {"primary": ' + SECRET + "\n")
        src = q.collect_codex(NOW, home=self.home)
        self.assertEqual(src["status"], "ok")


class OpencodeTests(unittest.TestCase):
    STATS = {"sessions": 4, "steps": 120,
             "tokens": {"input": 1000, "output": 200, "reasoning": 50,
                        "cache": {"read": 5000, "write": 300}},
             "cost": 1.23456, "models": [{"model": {"id": "x"}}]}

    def test_stats_summary(self):
        self.assertEqual(q.parse_opencode_stats(json.dumps(self.STATS)),
                         {"sessions": 4, "steps": 120, "tokens": 6550, "cost_usd": 1.23})

    def test_stats_garbage_raises(self):
        with self.assertRaises(ValueError):
            q.parse_opencode_stats("not json")

    def test_last_limit_line(self):
        lines = [
            "timestamp=2026-01-01T10:00:00.000Z level=INFO run=aa step done",
            "timestamp=2026-01-02T10:00:00.123Z level=ERROR run=bb AI.Error: Go usage limit exceeded",
            "timestamp=2026-01-03T09:30:00.000Z level=ERROR run=cc Go usage limit exceeded",
            "timestamp=2026-01-04T09:30:00.000Z level=INFO run=dd fine again",
        ]
        self.assertEqual(q.last_limit_exceeded(lines), "2026-01-03T09:30:00Z")
        self.assertIsNone(q.last_limit_exceeded(lines[:1]))

    def test_table_row(self):
        src = q._source("opencode", extra={
            "today": {"sessions": 1, "steps": 2, "tokens": 3, "cost_usd": 0.5},
            "last_7_days": {"sessions": 4, "steps": 5, "tokens": 6, "cost_usd": 7.0},
            "last_limit_exceeded_at": "2026-01-08T12:00:00Z"}, as_of=q._iso(NOW))
        table = q.render_table({"generated_at": q._iso(NOW), "sources": [src]})
        self.assertIn("today: 1 sessions, 2 steps, $0.50; 7d: 4 sessions, 5 steps, $7.00", table)
        self.assertIn("2026-01-08T12:00:00Z (2d ago)", table)


class ReportTests(unittest.TestCase):
    def test_every_source_fails_soft(self):
        def boom(now):
            raise RuntimeError("secret detail")
        saved = q.COLLECTORS
        q.COLLECTORS = (("agy", boom), ("codex", boom), ("opencode", boom))
        try:
            doc = q.collect(NOW)
        finally:
            q.COLLECTORS = saved
        self.assertEqual([s["status"] for s in doc["sources"]], ["unavailable"] * 3)
        self.assertNotIn("secret detail", json.dumps(doc))
        table = q.render_table(doc)
        self.assertEqual(table.count("unavailable: internal error: RuntimeError"), 3)

    def test_json_schema_shape(self):
        saved = q.COLLECTORS
        q.COLLECTORS = (("agy", lambda now: q._source(
            "agy", windows=q.parse_agy_usage(AGY_TEXT, now), as_of=q._iso(now))),)
        try:
            doc = json.loads(q.report("json", now=NOW))
        finally:
            q.COLLECTORS = saved
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["generated_at"], "2026-01-10T12:00:00Z")
        src = doc["sources"][0]
        self.assertEqual(set(src), {"source", "status", "reason", "as_of", "windows", "extra"})
        self.assertEqual(set(src["windows"][0]), {"pool", "window", "used_percent", "left_percent",
                                                  "resets_at", "reset_passed", "not_started"})


class LedgerTests(unittest.TestCase):
    """The limit ledger, under a temporary run store. Every error line is
    invented."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ledger-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def hit(self, cli="codex", model="", line="usage limit reached", now=NOW):
        return q.record_limit_hit(cli, model, line, "run-1", now=now, root=self.root)

    def test_pools(self):
        self.assertEqual(q.pool_for("codex", "(codex default)"), "codex")
        self.assertEqual(q.pool_for("agy", "claude-sonnet-5-5-high"), q.AGY_CLAUDE_GPT)
        self.assertEqual(q.pool_for("agy", "gpt-oss-120b-medium"), q.AGY_CLAUDE_GPT)
        self.assertEqual(q.pool_for("agy", "gemini-4-argon-high"), q.AGY_ARGON)
        self.assertEqual(q.pool_for("agy", "gemini-3.8-flash-high"), q.AGY_GEMINI)
        self.assertEqual(q.pool_for("opencode", "opencode-go/kimi-k3"), "opencode-go")
        self.assertEqual(q.pool_for("opencode", "opencode-go/longcat-2.0"), "opencode-go")
        self.assertEqual(q.pool_for("opencode", "opencode/big-pickle"), "opencode/big-pickle")
        self.assertEqual(q.pool_for("opencode", ""), "default model")

    def test_limit_lines_exclude_auth(self):
        for line in ("AI.Error: Go usage limit exceeded", "You have hit your usage limit.",
                     "quota exceeded for model", "You exceeded your current quota.",
                     "insufficient credit balance"):
            self.assertTrue(q.is_limit_line(line), line)
        # Per-minute rate limits clear in seconds and must not block a pool.
        for line in ("invalid api key", "authentication failed", "AI_APICallError: bad request",
                     "429 RESOURCE_EXHAUSTED", "Rate limit reached, try again in 1.5s",
                     "RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Generate "
                     "requests' and limit 'requests per minute'",
                     "", None):
            self.assertFalse(q.is_limit_line(line), line)

    def test_reset_parsing(self):
        self.assertEqual(q.parse_reset("try again in 2 hours 5 minutes", NOW), NOW + 7500)
        self.assertEqual(q.parse_reset("resets in 1h 30m", NOW), NOW + 5400)
        self.assertEqual(q.parse_reset("try again in 3 days, 4 hours and 1 minute.", NOW),
                         NOW + 3 * 86400 + 4 * 3600 + 60)
        self.assertEqual(q.parse_reset("limit resets at 2026-01-10T15:00:00Z", NOW),
                         q._parse_iso("2026-01-10T15:00:00Z"))
        # Clock times are local, as the CLIs print them.
        base = q._dt.datetime.fromtimestamp(NOW).astimezone()
        later = (base + q._dt.timedelta(hours=2)).replace(second=0, microsecond=0)
        text = later.strftime("try again at %I:%M %p")
        self.assertEqual(q.parse_reset(text, NOW), later.timestamp())
        earlier = (base - q._dt.timedelta(hours=2)).replace(second=0, microsecond=0)
        self.assertEqual(q.parse_reset(earlier.strftime("try again at %H:%M"), NOW),
                         (earlier + q._dt.timedelta(days=1)).timestamp())
        dated = (base + q._dt.timedelta(days=2)).replace(second=0, microsecond=0)
        self.assertEqual(q.parse_reset(dated.strftime("try again at %b %d, %Y %I:%M %p"), NOW),
                         dated.timestamp())
        self.assertEqual(q.parse_reset("try again in 1.5s", NOW), NOW + 1.5)
        # A leading log timestamp (past) must not hide the stated reset.
        self.assertEqual(q.parse_reset("2026-01-10T10:00:00Z ERROR usage limit reached, "
                                       "resets in 2 hours", NOW), NOW + 7200)
        # An ISO time without an offset is local, as the CLIs print it.
        local = (q._dt.datetime.fromtimestamp(NOW) + q._dt.timedelta(hours=3)).replace(
            microsecond=0)
        self.assertEqual(q.parse_reset("resets at " + local.strftime("%Y-%m-%d %H:%M:%S"), NOW),
                         local.timestamp())
        # Times before the reset phrase are log stamps or event times.
        stamp = q._dt.datetime.fromtimestamp(NOW + 3600).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertIsNone(q.parse_reset(f"ERROR {stamp} Go usage limit exceeded", NOW))
        self.assertIsNone(q.parse_reset("request failed at 23:59: usage limit reached", NOW))
        for text in ("usage limit reached", "try again in 2 months",
                     "resets at 2025-01-01T00:00:00Z",          # past
                     "try again in 400 days"):                  # too far
            self.assertIsNone(q.parse_reset(text, NOW), text)

    def test_hit_with_reset_blocks_until_then(self):
        e = self.hit(line="You have hit your usage limit. Try again in 2 hours.")
        self.assertEqual(e["resets_at"], q._iso(NOW + 7200))
        self.assertEqual(q.active_limit("codex", "x", now=NOW + 7000, root=self.root)["run_id"],
                         "run-1")
        self.assertIsNone(q.active_limit("codex", "x", now=NOW + 7300, root=self.root))
        self.assertEqual(q.limit_hits(NOW + 7300, root=self.root), [])

    def test_hit_without_reset_goes_stale_then_ages_out(self):
        self.hit()
        ttl = q.limit_ttl()
        self.assertEqual(q.limit_hits(NOW + ttl - 1, root=self.root)[0]["state"], "active")
        self.assertEqual(q.limit_hits(NOW + ttl + 1, root=self.root)[0]["state"], "stale")
        self.assertIsNone(q.active_limit("codex", "", now=NOW + ttl + 1, root=self.root))
        self.assertEqual(q.limit_hits(NOW + q.STALE_SECONDS + 1, root=self.root), [])

    def test_ttl_is_configurable(self):
        os.environ["AGENT_MCP_LIMIT_TTL"] = "60"
        try:
            self.hit()
            self.assertEqual(q.limit_hits(NOW + 61, root=self.root)[0]["state"], "stale")
            # A TTL longer than the stale period still blocks for all of it.
            os.environ["AGENT_MCP_LIMIT_TTL"] = str(2 * q.STALE_SECONDS)
            self.assertEqual(q.limit_hits(NOW + q.STALE_SECONDS + 60,
                                          root=self.root)[0]["state"], "active")
        finally:
            del os.environ["AGENT_MCP_LIMIT_TTL"]

    def test_newer_hit_replaces_older_and_pools_are_separate(self):
        self.hit(cli="agy", model="gemini-3.8-flash-high", now=NOW - 100)
        self.hit(cli="agy", model="gemini-3.8-flash-low", now=NOW)
        self.hit(cli="agy", model="claude-opus-5-5-high", now=NOW)
        hits = q.limit_hits(NOW, root=self.root)
        self.assertEqual(sorted(h["pool"] for h in hits), [q.AGY_CLAUDE_GPT, q.AGY_GEMINI])
        gem = [h for h in hits if h["pool"] == q.AGY_GEMINI][0]
        self.assertEqual(gem["model"], "gemini-3.8-flash-low")

    def test_late_older_hit_does_not_replace_a_newer_one(self):
        self.hit(line="usage limit reached, resets in 3 days", now=NOW)
        self.hit(line="usage limit reached", now=NOW - 600)
        hits = q.limit_hits(NOW, root=self.root)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["resets_at"], q._iso(NOW + 3 * 86400))

    def test_non_limit_line_is_not_recorded(self):
        self.assertIsNone(self.hit(line="invalid api key"))
        self.assertFalse(os.path.exists(q.ledger_path(self.root)))

    def test_clear_only_removes_hits_before_the_clean_run(self):
        self.hit(now=NOW)
        q.clear_limit_hit("codex", "", before=NOW - 10, now=NOW, root=self.root)
        self.assertEqual(len(q.limit_hits(NOW, root=self.root)), 1)
        q.clear_limit_hit("codex", "", before=NOW + 10, now=NOW, root=self.root)
        self.assertEqual(q.limit_hits(NOW, root=self.root), [])

    def test_clear_without_a_ledger_creates_nothing(self):
        self.assertFalse(q.clear_limit_hit("codex", "", before=NOW, now=NOW, root=self.root))
        self.assertFalse(os.path.exists(os.path.join(self.root, "quota")))

    def test_corrupt_ledger_reads_empty_and_is_rewritten(self):
        os.makedirs(os.path.dirname(q.ledger_path(self.root)))
        for junk in ("{not json", '["a list"]', '{"entries": [{"cli": "codex"}]}'):
            with open(q.ledger_path(self.root), "w") as fh:
                fh.write(junk)
            self.assertEqual(q.limit_hits(NOW, root=self.root), [])
        self.assertIsNotNone(self.hit())
        self.assertEqual(len(q.limit_hits(NOW, root=self.root)), 1)

    def test_unwritable_ledger_never_raises(self):
        blocker = os.path.join(self.root, "quota")
        with open(blocker, "w") as fh:
            fh.write("a file where the directory should be")
        self.assertIsNone(self.hit())
        self.assertEqual(q.limit_hits(NOW, root=self.root), [])

    def test_line_is_cut_and_nothing_else_kept(self):
        e = self.hit(line="usage limit reached " + "x" * 1000)
        self.assertEqual(len(e["line"]), q.LINE_MAX)
        self.assertEqual(set(e), {"cli", "pool", "model", "hit_at", "resets_at", "line", "run_id"})

    def test_collect_and_table_show_hits(self):
        self.hit(line="usage limit reached, try again in 1 hour")
        self.hit(cli="opencode", model="opencode-go/kimi-k3", line="Go usage limit exceeded",
                 now=NOW - q.limit_ttl() - 5)
        saved = q.COLLECTORS
        q.COLLECTORS = (("codex", lambda now: q._source("codex", reason="no session files")),
                        ("opencode", lambda now: q._source("opencode", as_of=q._iso(now))))
        try:
            doc = q.collect(NOW, root=self.root)
        finally:
            q.COLLECTORS = saved
        by = {s["source"]: s for s in doc["sources"]}
        self.assertEqual(by["codex"]["extra"]["limit_hits"][0]["state"], "active")
        self.assertEqual(by["opencode"]["extra"]["limit_hits"][0]["state"], "stale")
        table = q.render_table(doc)
        self.assertIn("LIMIT HIT, blocked until reset", table)
        self.assertIn("unavailable: no session files", table)
        self.assertIn("opencode-go ", table)
        self.assertIn("stale, not blocking", table)


if __name__ == "__main__":
    unittest.main(verbosity=1)
