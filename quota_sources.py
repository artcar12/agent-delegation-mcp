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
period is {"sessions", "steps", "tokens", "cost_usd"}. Every source's extra
also carries "limit_hits": [{"cli", "pool", "model", "hit_at", "resets_at",
"line", "run_id", "state": "active"|"stale"}] from the limit ledger below.

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

try:
    import fcntl
except ImportError:          # not on POSIX: the ledger still works, unlocked
    fcntl = None

SCHEMA = 1
CMD_TIMEOUT = 30
CODEX_MAX_FILES = 300


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
    if resets_at is not None and resets_at <= 0:
        # agy reports the Unix epoch for a pool whose window has not started:
        # there is no reset to show, and nothing has reset.
        resets_at = None
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


def collect_agy(now: float, bin_path=None, timeout=CMD_TIMEOUT):
    stdout, err = _run([bin_path or agy_bin(), "-p", "/usage", "--output-format", "json"],
                       timeout=timeout, env=_child_env())
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
    bad = ""
    for mtime, path in files[:CODEX_MAX_FILES]:
        rl = last_rate_limits(path)
        if rl is None:
            continue
        try:
            windows, extra = parse_codex_rate_limits(rl, now)
        except ValueError as exc:
            bad = bad or str(exc)       # e.g. an API-key session with null windows:
            continue                    # an older file may still have real ones
        return _source("codex", windows=windows, extra=extra, as_of=_iso(mtime))
    if bad:
        return _source("codex", reason=f"unexpected rate_limits shape ({bad})")
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
# limit ledger
#
# A usage-limit hit is final for its pool until the provider resets it, but a
# run that hits one is otherwise forgotten the moment it ends: the next
# dispatch, from this session or another, walks into the same wall. The
# servers record each hit here when a run ends (see _note_limit in each
# server), and quota_status reports them. For opencode a recorded hit is the
# only headless signal there is.
#
# The file lives in a subdirectory of the run store, so it follows
# AGENT_MCP_RUN_DIR like the runs do, and the servers' run listing and pruning
# (which look only at the store's top level) never see it.
#
# PRIVACY: an entry keeps the single matched error line, cut to LINE_MAX
# characters, and nothing else from a run's output.

LEDGER_SCHEMA = 1
LINE_MAX = 300
STALE_SECONDS = 86400           # an unknown-reset hit is reported this long at most
MAX_RESET_AHEAD = 31 * 86400    # a parsed reset further out than this is not trusted


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def limit_ttl():
    """How long a hit with no stated reset blocks its pool (seconds)."""
    return max(0, _int_env("AGENT_MCP_LIMIT_TTL", 3600))


def run_dir():
    return os.path.expanduser(os.environ.get("AGENT_MCP_RUN_DIR", "").strip()
                              or "~/.agent-delegation-mcp/runs")


def ledger_path(root=None):
    """The ledger file under `root` (a server passes its own RUN_DIR) or the
    default run store."""
    return os.path.join(root or run_dir(), "quota", "limits.json")


# Pools as the provider meters them. agy names match its /usage groups, so a
# hit can be set beside the live reading. Which Gemini 4 families other than
# Argon draw on the shared Gemini pool is an assumption from /usage, which
# lists no separate group for them.
AGY_CLAUDE_GPT = "Claude and GPT models"
AGY_ARGON = "Gemini 4 Argon"
AGY_GEMINI = "Gemini Models"
# OpenCode Go: one pool for the whole plan. The roster lists per-model caps,
# but the console reports one set of Go percentages, the error reads "Go usage
# limit exceeded", and a limit on one Go model has taken its siblings out with
# it. Other opencode providers (the free Zen ids) are taken per model.
OPENCODE_GO = "opencode-go"


def pool_for(cli: str, model: str) -> str:
    """The quota pool a run of `model` on `cli` draws on."""
    m = (model or "").strip()
    low = m.lower()
    if cli == "codex":
        return "codex"
    if cli == "agy":
        if low.startswith(("claude", "gpt")):
            return AGY_CLAUDE_GPT
        if "argon" in low:
            return AGY_ARGON
        return AGY_GEMINI
    if cli == "opencode":
        if not m or m.startswith("("):
            return "default model"
        if low.startswith(OPENCODE_GO + "/"):
            return OPENCODE_GO
        return m
    return m or cli


# What counts as a quota wall, as opposed to the other fatal lines (auth, a
# bad key), which are not limits and must never block a pool.
# Per-minute rate limits ("rate limit reached", RESOURCE_EXHAUSTED) are left
# out on purpose: they clear in seconds, and blocking a pool for the TTL on
# one would cost far more than the run it lost. agy's live /usage reading
# still shows a real wall as 0% left.
_LIMIT_HIT = re.compile(
    r"usage limit|quota exceeded|exceeded (?:your |the )?quota|insufficient credit", re.I)


def is_limit_line(line) -> bool:
    return isinstance(line, str) and bool(_LIMIT_HIT.search(line))


_ISO_IN_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_REL_UNIT = r"(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)"
_REL = re.compile(r"\bin\s+((?:\d+(?:\.\d+)?\s*" + _REL_UNIT + r"\b[\s,]*(?:and\s+)?)+)", re.I)
_REL_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(" + _REL_UNIT + r")\b", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_CLOCK = re.compile(
    r"\bat\s+(?:([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(?:(\d{4}),?\s+)?)?"
    r"(\d{1,2}):(\d{2})\s*([ap])?\.?m?\.?", re.I)


def parse_reset(line: str, now: float):
    """Epoch seconds of the reset a limit message states, or None. Understands
    ISO times ("2026-11-24T18:00:00Z"; one with no offset is local time, as
    the CLIs print it), "in 2 hours 5 minutes" / "in 2h 5m" / "in 1.5s", and
    "at 3:05 PM" or "at Nov 25th, 2026 1:07 PM" (local). Every candidate is
    read, since a line can start with its own log timestamp; the earliest one
    in the future and within MAX_RESET_AHEAD wins, else None."""
    if not isinstance(line, str):
        return None
    found = []
    for m in _ISO_IN_TEXT.finditer(line):
        text = m.group(0).replace(" ", "T")
        if re.search(r"(Z|[+-]\d{2}:?\d{2})$", text):
            found.append(_parse_iso(text))
        else:
            try:
                found.append(_dt.datetime.fromisoformat(text).timestamp())  # naive: local
            except ValueError:
                pass
    m = _REL.search(line)
    if m:
        found.append(now + sum(float(n) * _UNIT_SECONDS[u[0].lower()]
                               for n, u in _REL_PART.findall(m.group(1))))
    m = _CLOCK.search(line)
    if m:
        found.append(_clock_time(m, now))
    good = [t for t in found if t is not None and now < t <= now + MAX_RESET_AHEAD]
    return min(good) if good else None


def _clock_time(m, now):
    """Local wall-clock time -> epoch seconds. Naive datetimes throughout, so
    .timestamp() applies the offset in force on that date (DST included)."""
    mon, day, year, hour, minute, ampm = m.groups()
    hour, minute = int(hour), int(minute)
    if ampm:
        if hour > 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    base = _dt.datetime.fromtimestamp(now)
    try:
        if mon:
            month = _MONTHS.get(mon[:3].lower())
            if not month:
                return None
            when = base.replace(year=int(year) if year else base.year, month=month,
                                day=int(day), hour=hour, minute=minute, second=0,
                                microsecond=0)
            if not year and when.timestamp() <= now:
                when = when.replace(year=when.year + 1)
            return when.timestamp()
        when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if when.timestamp() <= now:
        when += _dt.timedelta(days=1)
    return when.timestamp()


def _ledger_load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []               # missing or corrupt: start clean, never raise
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("cli") and e.get("pool")
            and _parse_iso(e.get("hit_at")) is not None]


def _expired(entry, now):
    hit = _parse_iso(entry.get("hit_at")) or 0
    reset = _parse_iso(entry.get("resets_at"))
    if reset is not None:
        return reset <= now
    return now - hit > max(STALE_SECONDS, limit_ttl())


def _ledger_update(fn, now, root=None):
    """Apply fn(entries) -> entries under an exclusive lock, dropping expired
    entries, and write the result atomically. False if the ledger could not be
    written; never raises."""
    path = ledger_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            entries = [e for e in fn(_ledger_load(path)) if not _expired(e, now)]
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"schema": LEDGER_SCHEMA, "entries": entries}, fh, indent=1)
            os.replace(tmp, path)
        return True
    except OSError:
        return False


def record_limit_hit(cli, model, line, run_id="", now=None, root=None):
    """Record a usage-limit hit for the pool `model` draws on. Returns the
    entry, or None when `line` is not a limit line or the write failed. A
    newer hit replaces an older one for the same pool."""
    if not is_limit_line(line):
        return None
    now = time.time() if now is None else now
    pool = pool_for(cli, model)
    reset = parse_reset(line, now)
    entry = {
        "cli": cli,
        "pool": pool,
        "model": model or "",
        "hit_at": _iso(now),
        "resets_at": _iso(reset) if reset is not None else None,
        "line": line.strip()[:LINE_MAX],
        "run_id": run_id or "",
    }

    def add(entries):
        return [e for e in entries if (e["cli"], e["pool"]) != (cli, pool)] + [entry]

    return entry if _ledger_update(add, now, root) else None


def clear_limit_hit(cli, model, before, now=None, root=None):
    """Forget a hit on this pool that happened before `before` (epoch
    seconds): a later run on the pool finished cleanly, so the wall is gone."""
    now = time.time() if now is None else now
    pool = pool_for(cli, model)
    path = ledger_path(root)
    if not any((e["cli"], e["pool"]) == (cli, pool) for e in _ledger_load(path)):
        return False            # nothing to clear: do not create the file
    return _ledger_update(
        lambda entries: [e for e in entries
                         if (e["cli"], e["pool"]) != (cli, pool)
                         or (_parse_iso(e["hit_at"]) or 0) >= before], now, root)


def limit_hits(now=None, cli=None, root=None):
    """Hits still worth reporting, each with "state": "active" (the pool is
    blocked: before its stated reset, or within limit_ttl() of an unknown one)
    or "stale" (reset unknown and the TTL has passed). Read-only."""
    now = time.time() if now is None else now
    out = []
    for e in _ledger_load(ledger_path(root)):
        if (cli and e["cli"] != cli) or _expired(e, now):
            continue
        hit = _parse_iso(e["hit_at"]) or 0
        active = e.get("resets_at") is not None or now - hit <= limit_ttl()
        out.append(dict(e, state="active" if active else "stale"))
    return out


def active_limit(cli, model, now=None, root=None):
    """The active hit blocking `model`'s pool on `cli`, or None."""
    pool = pool_for(cli, model)
    for e in limit_hits(now, cli, root):
        if e["pool"] == pool and e["state"] == "active":
            return e
    return None


# ---------------------------------------------------------------------------
# dispatch guard
#
# What a dispatch needs to know about the one pool it is about to draw on,
# read from that CLI only and bounded by a short timeout: a guard that slows
# every dispatch, or blocks one on an unreadable source, costs more than it
# saves.

def warn_percent():
    """Below this much left, a dispatch proceeds with a warning."""
    return max(0, _int_env("AGENT_MCP_QUOTA_WARN_PERCENT", 10))


def _fmt_age(seconds):
    s = int(max(0, seconds))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def pool_check(cli, model, now=None, root=None, timeout=5, agy=None, codex_home=None):
    """{"pool", "status": "ok"|"low"|"exhausted"|"unknown", "note": str,
    "hit": ledger entry|None}. "exhausted" means refuse: an active ledger hit
    on the pool, or a live window at 0% left whose reset has not passed.
    timeout=0 skips the live reading (the ledger is still read). Never
    raises."""
    now = time.time() if now is None else now
    pool = pool_for(cli, model)
    out = {"pool": pool, "status": "unknown", "note": "", "hit": None}
    try:
        hit = active_limit(cli, model, now, root)
    except Exception:
        hit = None
    if hit:
        reset = hit.get("resets_at")
        age = _fmt_age(now - (_parse_iso(hit["hit_at"]) or now))
        out.update(status="exhausted", hit=hit, note=(
            f"{cli} pool {pool!r} hit its usage limit {age} ago"
            + (f"; resets {reset}" if reset else
               f"; reset time not stated, treated as blocked for {_fmt_age(limit_ttl())}")
            + f". Error was: {hit.get('line', '')}"))
        return out
    windows, as_of, reason = [], None, "no headless reading"
    if timeout and cli == "agy":
        src = collect_agy(now, bin_path=agy, timeout=timeout)
        reason = src["reason"]
        windows = [w for w in src["windows"] if w["pool"] == pool]
        as_of = src["as_of"]
    elif timeout and cli == "codex":
        src = collect_codex(now, home=codex_home or "")
        reason = src["reason"]
        windows = src["windows"]
        as_of = src["as_of"]
    elif cli == "opencode":
        reason = "opencode reports no remaining quota headlessly; only recorded hits count"
    elif not timeout:
        reason = "live reading off"
    if not windows:
        out["note"] = f"{cli} pool {pool!r}: quota unknown ({reason or 'no window for this pool'})"
        return out
    w = min(windows, key=lambda w: w["left_percent"])
    stale = ""
    if cli == "codex" and as_of:
        stale = f", reading {_fmt_age(now - (_parse_iso(as_of) or now))} old"
    desc = (f"{cli} pool {pool!r}: {w['left_percent']:g}% left of the {w['window']} window"
            + (f", resets {w['resets_at']}" if w.get("resets_at") else "") + stale)
    if w["left_percent"] <= 0 and not w["reset_passed"]:
        out.update(status="exhausted", note=desc)
    elif w["left_percent"] < warn_percent():
        out.update(status="low", note=desc)
    else:
        out.update(status="ok", note=desc)
    return out


# ---------------------------------------------------------------------------
# report

COLLECTORS = (("agy", collect_agy), ("codex", collect_codex), ("opencode", collect_opencode))


def collect(now=None, root=None):
    """All sources, in parallel, each bounded by CMD_TIMEOUT, plus the limit
    ledger under `root` (default: the default run store)."""
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
    for s in sources:
        try:
            hits = limit_hits(now, s["source"], root)
        except Exception:                 # the ledger never takes a reading down
            hits = []
        s["extra"] = dict(s.get("extra") or {}, limit_hits=hits)
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
        else:
            rows.extend(_window_rows(s, now))
        rows.extend(_hit_rows(s, now))
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head) - 1)]
    lines = []
    for r in [head] + rows:
        cells = [str(c).ljust(widths[i]) for i, c in enumerate(r[:-1])] + [str(r[-1])]
        lines.append("  ".join(cells).rstrip())
    return f"quota_status (generated {doc['generated_at']})\n" + "\n".join(lines)


def _hit_rows(s, now):
    """One row per ledger hit: the pool is blocked (active) or was, with no
    known reset (stale)."""
    rows = []
    for h in (s.get("extra") or {}).get("limit_hits") or []:
        if h.get("state") == "active":
            what = "LIMIT HIT, blocked until " + ("reset" if h.get("resets_at") else
                                                  "the hit ages out (reset unknown)")
        else:
            what = "limit hit, reset unknown (stale, not blocking)"
        rows.append((s["source"], h.get("pool", "-"), "-", "-", "-", h.get("resets_at") or "-",
                     f"{what}; at {h.get('hit_at')} ({_ago(h.get('hit_at'), now)})"))
    return rows


def _window_rows(s, now):
    rows = []
    name = s["source"]
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
    return rows


def report(fmt: str = "table", now=None, root=None) -> str:
    doc = collect(now, root)
    if (fmt or "").lower() == "json":
        return json.dumps(doc, indent=2)
    return render_table(doc)


QUOTA_DOC = """
    One table of the quota every delegate CLI reports headlessly, read without
    spending a token: agy's pools (weekly and 5-hour), Codex's 5-hour and
    weekly windows from its last session snapshot, and opencode's usage
    summary plus its last usage-limit hit, and every usage-limit hit a
    delegate run has recorded in the shared limit ledger, active (the pool is
    blocked) or stale. Claude's own usage and the web-only pages (opencode
    console, ChatGPT usage settings) are not covered here.

    format="table" (default) for reading; format="json" for a stable,
    documented shape (schema 1, see quota_sources.py) that scripts and the
    quota-check skill parse. Each source fails soft as "unavailable: reason".

    Codex numbers are true as of its last run (the "as of" column); a window
    whose reset time has passed is shown as reset. Call before a fan-out or a
    long dispatch, and when a delegate hits a usage limit.
"""
