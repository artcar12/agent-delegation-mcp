"""
quota_sources: the headless half of "how much quota is left", shared by every
wrapper's quota_status tool.

Three sources, each read without spending a token:

  agy       `agy -p /usage --output-format json`: per-pool weekly and 5-hour
            windows with a remaining fraction and a reset time. Falls back to
            the tab-separated text rows the same command prints.
  codex     the `rate_limits` snapshot Codex writes into its session files
            (`~/.codex/sessions/**/*.jsonl`) on every turn. Valid as of the last
            Codex run, so the file's mtime is reported as "as of".
  opencode  `opencode stats --json` (today and the last 7 days), plus the time
            of the last "usage limit exceeded" line in opencode's log. opencode
            exposes no remaining-quota number headlessly; the web console has it.

Every collector fails soft: a missing CLI, a timeout or an unexpected shape
becomes {"status": "unavailable", "reason": ...}, never an exception.

PRIVACY: Codex session files hold whole conversations. Only lines that carry a
non-null `rate_limits` are parsed, only the fields named in _codex_window and
_codex_extra are kept, and no error message ever includes file content.

The output is consumed by the quota-check skill, so its shape is a contract.
JSON (format="json") is schema 1:

  {"schema": 1, "generated_at": ISO-UTC,
   "sources": [{"source": "agy"|"codex"|"opencode",
                "status": "ok"|"unavailable",
                "reason": str|null,           # set when unavailable
                "as_of": ISO-UTC|null,        # when the numbers were true
                "windows": [{"pool": str, "window": "5h"|"weekly"|"<n>m",
                             "used_percent": float, "left_percent": float,
                             "resets_at": ISO-UTC|null,
                             "reset_passed": bool}],
                "extra": {...}}]}             # source-specific, see below

extra: agy {} ; codex {"plan_type", "credits": {...}, "limit_reached": str|null,
"snapshot_used_percent": {pool: float}} ; opencode {"today": {...},
"last_7_days": {...}, "last_limit_exceeded_at": ISO-UTC|null}, where each
period is {"sessions", "steps", "tokens", "cost_usd"}.

Adding a field is compatible; renaming or removing one bumps "schema".
"""

import datetime as _dt
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time

SCHEMA = 1
CMD_TIMEOUT = 30
CODEX_MAX_FILES = 300
MAC_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


# ---------------------------------------------------------------------------
# helpers

def _iso(ts) -> str:
    """Epoch seconds -> ISO 8601 UTC with a Z, second precision."""
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text):
    """ISO 8601 (with Z or offset) -> epoch seconds, or None."""
    if not text or not isinstance(text, str):
        return None
    try:
        return _dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _window(pool, window, used, resets_at, now):
    """One normalised window row. `resets_at` is epoch seconds or None. A
    window whose reset time has passed is shown as reset (0 used), with the
    stale reading kept in the caller's extra if it wants it."""
    used = max(0.0, min(100.0, round(used, 1)))
    passed = resets_at is not None and resets_at <= now
    if passed:
        used = 0.0
    return {
        "pool": pool,
        "window": window,
        "used_percent": used,
        "left_percent": round(100.0 - used, 1),
        "resets_at": _iso(resets_at) if resets_at is not None else None,
        "reset_passed": passed,
    }


def _source(name, windows=None, extra=None, as_of=None, reason=None):
    return {
        "source": name,
        "status": "unavailable" if reason else "ok",
        "reason": reason,
        "as_of": as_of,
        "windows": windows or [],
        "extra": extra or {},
    }


def _run(argv, timeout=CMD_TIMEOUT, env=None):
    """(stdout, error reason). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        return None, f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return None, f"no answer within {timeout}s"
    except OSError as exc:
        return None, f"could not run {argv[0]}: {exc.strerror or exc}"
    if p.returncode != 0:
        first = (p.stderr or p.stdout or "").strip().splitlines()
        return None, f"exit {p.returncode}" + (f": {first[0][:200]}" if first else "")
    return p.stdout, None


def _child_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}


# ---------------------------------------------------------------------------
# agy

def _agy_window_name(bucket):
    w = str(bucket.get("window") or "").strip().lower()
    if w:
        return w
    name = str(bucket.get("name") or bucket.get("id") or "").lower()
    if "five hour" in name or "5h" in name:
        return "5h"
    if "week" in name:
        return "weekly"
    return "?"


def parse_agy_usage(stdout: str, now: float):
    """Windows from `agy -p /usage --output-format json`, or from the text form
    (`<group>\\t<bucket name>\\t<n>%\\t<ISO reset>` per line). Raises
    ValueError when neither shape is present."""
    data = None
    try:
        data = json.loads(stdout)
    except ValueError:
        pass
    if isinstance(data, dict):
        groups = (((data.get("command") or {}).get("data") or {}).get("groups"))
        if isinstance(groups, list) and groups:
            out = []
            for g in groups:
                pool = str(g.get("name") or "?")
                for b in g.get("buckets") or []:
                    frac = _num(b.get("remaining_fraction"))
                    if frac is None:
                        continue
                    out.append(_window(pool, _agy_window_name(b), (1.0 - frac) * 100.0,
                                       _parse_iso(b.get("reset_time")), now))
            if out:
                return out
        text = data.get("response")
        if isinstance(text, str):
            return parse_agy_text(text, now)
        raise ValueError("JSON without command.data.groups or response")
    return parse_agy_text(stdout, now)


_PCT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")


def parse_agy_text(text: str, now: float):
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        m = _PCT.match(parts[2])
        if not m:
            continue
        left = float(m.group(1))
        out.append(_window(parts[0].strip(), _agy_window_name({"name": parts[1]}),
                           100.0 - left, _parse_iso(parts[3]), now))
    if not out:
        raise ValueError("no usage rows in output")
    return out


def agy_bin():
    return os.environ.get("AGY_BIN", "").strip() or shutil.which("agy") or "agy"


def collect_agy(now: float):
    stdout, err = _run([agy_bin(), "-p", "/usage", "--output-format", "json"],
                       env=_child_env())
    if err:
        return _source("agy", reason=err)
    try:
        windows = parse_agy_usage(stdout, now)
    except ValueError as exc:
        return _source("agy", reason=f"unparseable /usage output ({exc})")
    return _source("agy", windows=windows, as_of=_iso(now))


# ---------------------------------------------------------------------------
# codex

def _codex_window_name(minutes):
    m = _num(minutes)
    if m is None:
        return "?"
    m = int(m)
    if m == 300:
        return "5h"
    if m == 10080:
        return "weekly"
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def parse_codex_rate_limits(rl: dict, now: float):
    """(windows, extra) from one `payload.rate_limits` object. Reads only the
    named fields; anything else in the object is ignored."""
    if not isinstance(rl, dict):
        raise ValueError("rate_limits is not an object")
    windows, snapshot = [], {}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict):
            continue
        used = _num(w.get("used_percent"))
        if used is None:
            continue
        name = _codex_window_name(w.get("window_minutes"))
        resets = _num(w.get("resets_at"))
        windows.append(_window(str(rl.get("limit_id") or "codex"), name, used, resets, now))
        snapshot[name] = used
    if not windows:
        raise ValueError("rate_limits has no primary or secondary window")
    credits = rl.get("credits") if isinstance(rl.get("credits"), dict) else {}
    extra = {
        "plan_type": rl.get("plan_type") if isinstance(rl.get("plan_type"), str) else None,
        "credits": {k: credits.get(k) for k in ("has_credits", "unlimited", "balance")
                    if k in credits},
        "limit_reached": (rl.get("rate_limit_reached_type")
                          if isinstance(rl.get("rate_limit_reached_type"), str) else None),
        "snapshot_used_percent": snapshot,
    }
    return windows, extra


_RL_MARK = b'"rate_limits"'
_RL_NULL = re.compile(rb'"rate_limits"\s*:\s*null')


def last_rate_limits(path: str):
    """The last non-null payload.rate_limits in one session file, or None.

    Only lines containing the literal key are decoded at all, and of those only
    `payload.rate_limits` is returned. Decode errors are swallowed without
    their message, which can quote the line."""
    last = None
    try:
        with open(path, "rb") as fh:
            for line in fh:
                if _RL_MARK not in line or (_RL_NULL.search(line) and line.count(_RL_MARK) == 1):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload") if isinstance(obj, dict) else None
                rl = payload.get("rate_limits") if isinstance(payload, dict) else None
                if isinstance(rl, dict):
                    last = rl
    except OSError:
        return None
    return last


def codex_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME", "").strip() or "~/.codex")


def collect_codex(now: float, home: str = ""):
    root = os.path.join(home or codex_home(), "sessions")
    if not os.path.isdir(root):
        return _source("codex", reason=f"no session directory at {root}")
    files = []
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            files.append((os.path.getmtime(path), path))
        except OSError:
            continue
    if not files:
        return _source("codex", reason="no Codex session files yet")
    files.sort(reverse=True)
    for mtime, path in files[:CODEX_MAX_FILES]:
        rl = last_rate_limits(path)
        if rl is None:
            continue
        try:
            windows, extra = parse_codex_rate_limits(rl, now)
        except ValueError as exc:
            return _source("codex", reason=f"unexpected rate_limits shape ({exc})")
        return _source("codex", windows=windows, extra=extra, as_of=_iso(mtime))
    return _source("codex", reason=f"no rate_limits in the newest {min(len(files), CODEX_MAX_FILES)} session files")


# ---------------------------------------------------------------------------
# opencode

def parse_opencode_stats(stdout: str):
    """{"sessions", "steps", "tokens", "cost_usd"} from `opencode stats --json`."""
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    tok = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    total = 0
    for key in ("input", "output", "reasoning"):
        v = _num(tok.get(key))
        total += int(v) if v else 0
    cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
    for key in ("read", "write"):
        v = _num(cache.get(key))
        total += int(v) if v else 0
    cost = _num(data.get("cost"))
    return {
        "sessions": int(_num(data.get("sessions")) or 0),
        "steps": int(_num(data.get("steps")) or 0),
        "tokens": total,
        "cost_usd": round(cost, 2) if cost is not None else None,
    }


_LIMIT_LINE = re.compile(r"usage limit exceeded", re.I)
_LOG_TS = re.compile(r"timestamp=(\S+)")


def last_limit_exceeded(lines):
    """ISO time of the last usage-limit line, or None. Takes an iterable of
    text lines; only the timestamp is kept from a match."""
    last = None
    for line in lines:
        if _LIMIT_LINE.search(line):
            m = _LOG_TS.search(line)
            ts = _parse_iso(m.group(1)) if m else None
            if ts is not None:
                last = _iso(ts)
    return last


def opencode_bin():
    return os.environ.get("OPENCODE_BIN", "").strip() or shutil.which("opencode") or "opencode"


def opencode_log():
    data = os.environ.get("XDG_DATA_HOME", "").strip() or os.path.expanduser("~/.local/share")
    return os.path.join(data, "opencode", "log", "opencode.log")


def collect_opencode(now: float):
    extra, reasons = {}, []
    for label, days in (("today", "0"), ("last_7_days", "7")):
        stdout, err = _run([opencode_bin(), "stats", "--json", "--days", days], env=_child_env())
        if err:
            reasons.append(f"stats: {err}")
            break
        try:
            extra[label] = parse_opencode_stats(stdout)
        except ValueError:
            reasons.append("stats: unparseable --json output")
            break
    log = opencode_log()
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            extra["last_limit_exceeded_at"] = last_limit_exceeded(fh)
    except FileNotFoundError:
        extra["last_limit_exceeded_at"] = None
    except OSError as exc:
        reasons.append(f"log: {exc.strerror or exc}")
    if "today" not in extra and "last_7_days" not in extra:
        return _source("opencode", extra=extra, reason="; ".join(reasons) or "no data")
    return _source("opencode", extra=extra, as_of=_iso(now))


# ---------------------------------------------------------------------------
# report

COLLECTORS = (("agy", collect_agy), ("codex", collect_codex), ("opencode", collect_opencode))


def collect(now=None):
    """All sources, in parallel, each bounded by CMD_TIMEOUT."""
    now = time.time() if now is None else now
    results = {}

    def run(name, fn):
        try:
            results[name] = fn(now)
        except Exception as exc:          # fail soft, whatever went wrong
            results[name] = _source(name, reason=f"internal error: {type(exc).__name__}")

    threads = [threading.Thread(target=run, args=c, daemon=True) for c in COLLECTORS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(CMD_TIMEOUT * 2 + 5)
    sources = [results.get(name) or _source(name, reason="timed out")
               for name, _fn in COLLECTORS]
    return {"schema": SCHEMA, "generated_at": _iso(now), "sources": sources}


def _ago(iso, now):
    ts = _parse_iso(iso)
    if ts is None:
        return ""
    s = int(now - ts)
    if s < 0:
        return "in the future"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _period(p):
    if not p:
        return "?"
    cost = f"${p['cost_usd']:.2f}" if p.get("cost_usd") is not None else "$?"
    return f"{p['sessions']} sessions, {p['steps']} steps, {cost}"


def render_table(doc: dict) -> str:
    """Fixed columns, tab-free, one row per window; a source with no windows
    gets one row with dashes and its note."""
    now = _parse_iso(doc["generated_at"]) or time.time()
    head = ("source", "pool", "window", "used", "left", "resets (UTC)", "note")
    rows = []
    for s in doc["sources"]:
        name = s["source"]
        if s["status"] != "ok":
            rows.append((name, "-", "-", "-", "-", "-", f"unavailable: {s['reason']}"))
            continue
        ex = s.get("extra") or {}
        if name == "codex":
            note = f"as of {s['as_of']} ({_ago(s['as_of'], now)})"
            if ex.get("plan_type"):
                note += f"; plan {ex['plan_type']}"
            if ex.get("limit_reached"):
                note += f"; limit reached: {ex['limit_reached']}"
        elif name == "opencode":
            note = (f"today: {_period(ex.get('today'))}; 7d: {_period(ex.get('last_7_days'))}; "
                    f"last usage-limit hit: "
                    + (f"{ex['last_limit_exceeded_at']} ({_ago(ex['last_limit_exceeded_at'], now)})"
                       if ex.get("last_limit_exceeded_at") else "none in log"))
        else:
            note = ""
        if not s["windows"]:
            rows.append((name, "-", "-", "-", "-", "-", note))
        for w in s["windows"]:
            wnote = note
            if w["reset_passed"]:
                was = ex.get("snapshot_used_percent", {}).get(w["window"])
                wnote = ("reset since snapshot" + (f" (was {was:g}% used)" if was is not None else "")
                         + ("; " + note if note else ""))
            rows.append((name, w["pool"], w["window"], f"{w['used_percent']:g}%",
                         f"{w['left_percent']:g}%", w["resets_at"] or "-", wnote))
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head) - 1)]
    lines = []
    for r in [head] + rows:
        cells = [str(c).ljust(widths[i]) for i, c in enumerate(r[:-1])] + [str(r[-1])]
        lines.append("  ".join(cells).rstrip())
    return f"quota_status (generated {doc['generated_at']})\n" + "\n".join(lines)


def report(fmt: str = "table", now=None) -> str:
    doc = collect(now)
    if (fmt or "").lower() == "json":
        return json.dumps(doc, indent=2)
    return render_table(doc)


QUOTA_DOC = """
    One table of the quota every delegate CLI reports headlessly, read without
    spending a token: agy's pools (weekly and 5-hour), Codex's 5-hour and
    weekly windows from its last session snapshot, and opencode's usage
    summary plus its last usage-limit hit. Claude's own usage and the web-only
    pages (opencode console, ChatGPT usage settings) are not covered here.

    format="table" (default) for reading; format="json" for a stable,
    documented shape (schema 1, see quota_sources.py) that scripts and the
    quota-check skill parse. Each source fails soft as "unavailable: reason".

    Codex numbers are true as of its last run (the "as of" column); a window
    whose reset time has passed is shown as reset. Call before a fan-out or a
    long dispatch, and when a delegate hits a usage limit.
"""
