"""`ccx statusline`: the claude-cockpit status line for Claude Code.

Claude Code runs the command with a JSON payload on stdin and shows what it
prints (https://code.claude.com/docs/en/statusline). Two rows by default:

  Opus 5.5·high │ repo ⎇ branch ●3 ↑1 │ ▓▓▓░░░░░░░ 32% 64k/200k │ $4.12 · 38m · +212 −40
  5h ▓▓▓▓▓░ 54% ↻2h10m · 7d 31% │ cache 92% ⏱4m │ agents 1▶ 3✓ $1.80 │ ⚠ 2 sessions waiting

Every segment is optional: missing data leaves it out. When a row is wider than
COLUMNS - 4, lower-priority segments are shortened, then dropped.
COCKPIT_STATUSLINE_LINES=1 folds everything into one row. The script always
exits 0 (a non-zero exit blanks the status line), and COCKPIT_STATUSLINE_DEBUG=1
prints tracebacks to stderr, which `claude --debug` logs.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata

from . import statusline_data as data

SEP = " │ "
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TICKET_RE = re.compile(r"^[A-Za-z]+[0-9]*-[0-9]+")
BRANCH_MAX = 20
CTX_BAR, USAGE_BAR = 10, 6
EDGE_COLUMNS = 4   # docs: keep lines at least four columns narrower than the terminal
DEFAULT_COLUMNS = 120

GREEN, YELLOW, ORANGE, RED, DIM, BOLD = "32", "33", "38;5;208", "31", "2", "1"
BRANCH_COLOUR, BLINK_RED = "38;5;141", "5;31"


# ---------------------------------------------------------------- formatting

def paint(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if code else text


def width(text: str) -> int:
    plain = ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1
               for ch in plain)


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def epoch_s(value: object) -> float | None:
    """Epoch seconds, also when a payload sends milliseconds."""
    seconds = number(value)
    if seconds is not None and seconds > 1e11:
        seconds /= 1000
    return seconds


def section(payload: dict, key: str) -> dict:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def tokens(count: float) -> str:
    if count >= 999_500:
        return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if count >= 1000:
        return f"{count / 1000:.0f}k"
    return str(int(count))


def span(seconds: float) -> str:
    """Compact duration: 45s, 4m, 2h10m, 3d4h."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def bar(pct: float, cells: int) -> str:
    filled = max(0, min(cells, int(pct * cells // 100)))
    return "▓" * filled + "░" * (cells - filled)


def ctx_colour(pct: float) -> str:
    """The thresholds of the GSD status line this replaces."""
    return GREEN if pct < 50 else YELLOW if pct < 65 else ORANGE if pct < 80 else BLINK_RED


def usage_colour(pct: float) -> str:
    return GREEN if pct < 50 else YELLOW if pct < 80 else RED


def short_branch(name: str) -> str:
    """'kartikeymishra/dev1-14854-fix-the-thing' -> 'dev1-14854'; long names are cut."""
    tail = name.rsplit("/", 1)[-1] or name
    if len(tail) <= BRANCH_MAX:
        return tail
    ticket = TICKET_RE.match(tail)
    return ticket.group(0) if ticket else tail[:BRANCH_MAX - 1] + "…"


# ---------------------------------------------------------------- segments

class Segment:
    """Display variants, fullest first, and a priority (higher survives longer)."""

    def __init__(self, priority: int, *variants: str) -> None:
        self.priority = priority
        self.variants = tuple(v for v in variants if v)


def model_segment(payload: dict) -> Segment | None:
    model = section(payload, "model")
    name = model.get("display_name") or model.get("id")
    if not isinstance(name, str) or not name.strip():
        return None
    effort = section(payload, "effort").get("level")
    full = f"{name}·{effort}" if isinstance(effort, str) and effort else name
    return Segment(95, paint(full, DIM), paint(name, DIM))


def git_text(git: dict | None) -> str:
    if not git:
        return ""
    head = git.get("branch") or (f"@{git['oid']}" if git.get("oid") else "")
    parts = [paint(f"⎇ {short_branch(head)}", BRANCH_COLOUR)] if head else []
    dirty = git.get("dirty")
    if dirty is None:
        parts.append(paint("●?", DIM))
    elif dirty:
        parts.append(paint(f"●{dirty}", YELLOW))
    if git.get("ahead"):
        parts.append(paint(f"↑{git['ahead']}", GREEN))
    if git.get("behind"):
        parts.append(paint(f"↓{git['behind']}", RED))
    return " ".join(parts)


def place_segment(cwd: str, git: dict | None) -> Segment | None:
    base = os.path.basename(cwd.rstrip("/")) if cwd else ""
    status = git_text(git)
    if not base and not status:
        return None
    full = " ".join(p for p in (base, status) if p)
    return Segment(85, full, status or base)


def context_segment(payload: dict) -> Segment | None:
    ctx = section(payload, "context_window")
    used, size = number(ctx.get("total_input_tokens")), number(ctx.get("context_window_size"))
    pct = number(ctx.get("used_percentage"))
    if pct is None and used is not None and size:
        pct = 100.0 * used / size
    if pct is None:
        return None
    pct = max(0.0, min(100.0, pct))
    colour = ctx_colour(pct)
    skull = "💀 " if colour == BLINK_RED else ""
    label = paint(f"{skull}{bar(pct, CTX_BAR)} {pct:.0f}%", colour)
    absolute = f" {tokens(used)}/{tokens(size)}" if used is not None and size else ""
    return Segment(100, label + paint(absolute, DIM), paint(f"{skull}{pct:.0f}%", colour))


def cost_segment(payload: dict) -> Segment | None:
    cost = section(payload, "cost")
    usd, ms = number(cost.get("total_cost_usd")), number(cost.get("total_duration_ms"))
    added, removed = number(cost.get("total_lines_added")), number(cost.get("total_lines_removed"))
    parts = [f"${usd:.2f}"] if usd is not None else []
    if ms is not None:
        parts.append(span(ms / 1000))
    if added or removed:
        parts.append(paint(f"+{int(added or 0)}", GREEN) + " " + paint(f"−{int(removed or 0)}", RED))
    if not parts:
        return None
    return Segment(60, " · ".join(parts), parts[0])


def usage_segment(payload: dict, now: float) -> Segment | None:
    limits = section(payload, "rate_limits")
    five, seven = section(limits, "five_hour"), section(limits, "seven_day")
    pct5, pct7 = number(five.get("used_percentage")), number(seven.get("used_percentage"))
    week = paint(f"7d {pct7:.0f}%", usage_colour(pct7)) if pct7 is not None else ""
    if pct5 is None:
        return Segment(90, week) if week else None
    resets = epoch_s(five.get("resets_at"))
    countdown = paint(f" ↻{span(resets - now)}", DIM) if resets and resets > now else ""
    head = paint(f"5h {bar(pct5, USAGE_BAR)} {pct5:.0f}%", usage_colour(pct5))
    full = head + countdown + (" · " + week if week else "")
    return Segment(90, full, head + countdown, paint(f"5h {pct5:.0f}%", usage_colour(pct5)))


def cache_view(payload: dict, facts: dict) -> tuple[float | None, float | None, bool]:
    """(hit %, expires_at, known cold). Hit % prefers the transcript's last calls; the
    countdown prefers Claude Code's own prompt_cache.expires_at."""
    cache = section(payload, "prompt_cache")
    recent = facts.get("cache") or {}
    hit = number(recent.get("hit_pct"))
    if hit is None and number(cache.get("hit_ratio")) is not None:
        hit = 100.0 * cache["hit_ratio"]
    expires = epoch_s(cache.get("expires_at"))
    cold = cache.get("warm") is False and bool(cache.get("caching_observed"))
    if expires is None and not cold:
        expires = epoch_s(recent.get("expires_at"))
    return hit, expires, cold


def cache_segment(payload: dict, facts: dict, now: float) -> Segment | None:
    hit, expires, cold = cache_view(payload, facts)
    if expires is not None and expires <= now:
        cold = True
    if cold:
        timer = paint("cold", RED)
    elif expires is not None:
        left = expires - now
        timer = paint(f"⏱{span(left)}", YELLOW if left < 60 else DIM)
    else:
        timer = ""
    if hit is None and not timer:
        return None
    rate = paint(f"cache {hit:.0f}%", GREEN if hit >= 80 else YELLOW if hit >= 50 else RED) if hit is not None else ""
    return Segment(70, " ".join(p for p in (rate, timer) if p), timer or rate)


def agents_segment(facts: dict) -> Segment | None:
    agents = facts.get("agents")
    if not agents:
        return None
    counts = []
    if agents["running"]:
        counts.append(paint(f"{agents['running']}▶", GREEN))
    if agents["done"]:
        counts.append(f"{agents['done']}✓")
    if agents["stalled"]:
        counts.append(paint(f"{agents['stalled']}⏸", YELLOW))
    cost = ("≥" if agents["partial"] else "") + f"${agents['cost']:.2f}"
    tally = " ".join(counts)
    return Segment(55, f"agents {tally} {paint(cost, DIM)}".replace("  ", " "), tally or cost)


def waiting_segment(names: list) -> Segment | None:
    if not names:
        return None
    noun = "session" if len(names) == 1 else "sessions"
    return Segment(80, paint(f"⚠ {len(names)} {noun} waiting", "1;33"), paint(f"⚠{len(names)}", "1;33"))


def safe(fn, *args, default=None):
    """One failing segment or source must not blank the whole status line."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        if os.environ.get("COCKPIT_STATUSLINE_DEBUG"):
            import traceback
            traceback.print_exc()
        return default


# ---------------------------------------------------------------- layout

def fit(segments: list, limit: int | None) -> str:
    """Join SEGMENTS within LIMIT columns. The lowest-priority segment is shortened,
    then dropped, before any higher one is touched; the last one left always stays."""
    chosen = {i: 0 for i, s in enumerate(segments) if s and s.variants}

    def line() -> str:
        return paint(SEP, DIM).join(segments[i].variants[v] for i, v in sorted(chosen.items()))

    for i in sorted(chosen, key=lambda i: segments[i].priority):
        while limit is not None and width(line()) > limit and chosen[i] + 1 < len(segments[i].variants):
            chosen[i] += 1
        if limit is None or width(line()) <= limit:
            break
        if len(chosen) > 1:
            del chosen[i]
    return line()


def tty_columns() -> int | None:
    """Width of the controlling terminal, for when Claude Code passes no COLUMNS."""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        return os.get_terminal_size(fd).columns or None
    except OSError:
        return None
    finally:
        os.close(fd)


def columns(environ=os.environ, probe=tty_columns) -> int:
    """Usable width: COLUMNS (the docs say Claude Code sets it on every run), else the
    terminal's size, else DEFAULT_COLUMNS; minus the edge Claude Code keeps."""
    try:
        value = int(environ.get("COLUMNS", ""))
    except ValueError:
        value = 0
    if value <= 0:
        value = probe() or DEFAULT_COLUMNS
    return max(10, value - EDGE_COLUMNS)


def build(payload: dict, facts: dict, git: dict | None, waiting: list, now: float,
          limit: int | None = None, one_line: bool = False) -> list[str]:
    cwd = section(payload, "workspace").get("current_dir") or payload.get("cwd") or ""
    first = [safe(model_segment, payload), safe(place_segment, cwd if isinstance(cwd, str) else "", git),
             safe(context_segment, payload), safe(cost_segment, payload)]
    second = [safe(usage_segment, payload, now), safe(cache_segment, payload, facts, now),
              safe(agents_segment, facts), safe(waiting_segment, waiting)]
    rows = [first + second] if one_line else [first, second]
    return [text for text in (fit([s for s in row if s], limit) for row in rows) if text]


# ---------------------------------------------------------------- entry

def gather(payload: dict, now: float, environ=os.environ) -> tuple[dict, dict | None, list]:
    directory = safe(data.state_dir, environ)
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    transcript = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else ""
    cwd = section(payload, "workspace").get("current_dir") or payload.get("cwd")
    facts = safe(data.transcript_facts, transcript, session_id, directory, now, default={})
    git = safe(data.git_info, cwd, directory, now) if isinstance(cwd, str) else None
    waiting = safe(data.waiting_sessions, session_id, default=[])
    five = section(section(payload, "rate_limits"), "five_hour")
    if five:
        safe(data.maybe_alert, five, directory, environ)
    safe(data.write_context_bridge, payload, environ)
    if directory:
        safe(data.prune_state, directory)
    return facts, git, waiting


def render(payload: dict, environ=os.environ, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    facts, git, waiting = gather(payload, now, environ)
    return build(payload, facts, git, waiting, now, columns(environ),
                 one_line=environ.get("COCKPIT_STATUSLINE_LINES", "").strip() == "1")


def read_payload(stream) -> dict | None:
    try:
        payload = json.loads(stream.read() or "null")
    except (ValueError, OSError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[2:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"} or sys.stdin.isatty():
        print(__doc__.strip())
        return 0
    try:
        payload = read_payload(sys.stdin)
        if payload is not None:
            sys.stdout.write("\n".join(render(payload)) + "\n")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 (a status line must never fail)
        if os.environ.get("COCKPIT_STATUSLINE_DEBUG"):
            import traceback
            traceback.print_exc()
    return 0
