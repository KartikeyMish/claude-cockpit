#!/usr/bin/env python3
"""Live subagent rows for Claude Code's agent panel (subagentStatusLine).

Input (stdin): {"session_id", "transcript_path", "cwd", "columns": N, "tasks": [{id, agentType,
status, description, label, startTime, model?, effort?, contextWindowSize?, tokenCount,
tokenSamples, cwd, ...}]}. A task id is the agent id: subagents/agent-<id>.jsonl.
Output: one JSON line per row, {"id": ..., "content": "<ansi text>"}; Claude Code keeps
only lines whose id is a current task.

Example row:
  ◆ senior-engineer  opus·high  ▂▃▅▇ 18.2k  9% ctx  1:42  $0.42  🔧23  ✎4  ⎇wt  Adding status column

From the agent transcripts (agent_metrics.py, read incrementally, offsets cached under
$TMPDIR) come the cost, tool calls, files edited, ⎇wt for an agent in its own worktree,
a red "stalled" after 5 minutes without progress, and a session total appended to the
last row. Rows are keyed by task id, so a separate footer line would be dropped.
Each of these is optional: missing data omits it, and narrow rows drop them before
the label. Any failure prints nothing, and Claude Code falls back to its default rows.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script dir off sys.path
try:
    import agent_metrics
except Exception:  # metrics are extras: without them, rows look as before
    agent_metrics = None

# tier -> (glyph, 256-colour)
TIERS = {
    "scout": ("◇", 117),
    "routine-worker": ("▪", 150),
    "senior-engineer": ("◆", 111),
    "debugger": ("✦", 211),
    "architect": ("▲", 183),
}
OTHER = ("●", 245)
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPARK = "▁▂▃▄▅▆▇█"
DONE = {"completed": ("✓", 150), "failed": ("✗", 211), "killed": ("■", 245)}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
EFFORT_SHORT = {"medium": "med", "xhigh": "xhigh"}
RED = 203
LABEL_MIN = 12          # extras are dropped before the label gets shorter than this
# Extra columns in display order, with the order they are dropped in when the row is narrow.
EXTRA_ORDER = ("cost", "tools", "edits", "worktree", "stalled")
EXTRA_DROP_FIRST = ("tools", "edits", "worktree", "cost", "stalled")


def paint(text: str, colour: int, bold: bool = False) -> str:
    return f"\x1b[{'1;' if bold else ''}38;5;{colour}m{text}\x1b[0m"


def dim(text: str) -> str:
    return paint(text, 245)


def visible_len(text: str) -> int:
    """Terminal cells: wide characters such as 🔧 take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in ANSI.sub("", text))


def fit(text: str, room: int) -> str:
    """TEXT cut to ROOM cells, with an ellipsis when cut."""
    if visible_len(text) <= room:
        return text
    out = ""
    for ch in text:
        if visible_len(out + ch) > room - 1:
            break
        out += ch
    return out + "…"


def tier_of(agent_type: str) -> str:
    """'orchestrator:scout' -> 'scout'; other agents keep their own name."""
    return agent_type.rsplit(":", 1)[-1] if agent_type.startswith("orchestrator:") else ""


def short_model(model: str | None) -> str:
    if not model:
        return ""
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in model:
            return family
    return model.split("-")[0]


def sparkline(samples: list) -> str:
    values = [v for v in samples if isinstance(v, (int, float))]
    deltas = [max(0, b - a) for a, b in zip(values, values[1:])][-6:]
    if not deltas or max(deltas) == 0:
        return ""
    top = max(deltas)
    return "".join(SPARK[min(len(SPARK) - 1, int(d / top * (len(SPARK) - 1)))] for d in deltas)


def tokens(count: object) -> str:
    if not isinstance(count, (int, float)) or count <= 0:
        return ""
    return f"{count / 1000:.1f}k" if count >= 1000 else str(int(count))


def elapsed(start_ms: object, now: float) -> str:
    if not isinstance(start_ms, (int, float)):
        return ""
    seconds = max(0, int(now - start_ms / 1000))
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds < 3600 else f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


def context_pct(task: dict) -> str:
    window, used = task.get("contextWindowSize"), task.get("tokenCount")
    if not isinstance(window, (int, float)) or not isinstance(used, (int, float)) or window <= 0:
        return ""
    return f"{min(100, round(used / window * 100))}% ctx"


def usd(amount: float | None, partial: bool) -> str:
    if amount is None:
        return ""
    text = "<$0.01" if 0 < amount < 0.01 else (f"${amount:,.0f}" if amount >= 100 else f"${amount:.2f}")
    return ("≥" if partial else "") + text


def extras(stats: object, colour: int) -> dict:
    """name -> painted column, only for the metrics that have data."""
    if stats is None:
        return {}
    found = {
        "cost": dim(usd(stats.cost, stats.cost_partial)) if stats.cost is not None else "",
        "tools": dim(f"🔧{stats.tools}") if stats.tools else "",
        "edits": dim(f"✎{stats.edits}") if stats.edits else "",
        "worktree": paint("⎇wt", colour) if stats.worktree else "",
        "stalled": paint("stalled", RED, bold=True) if stats.stalled else "",
    }
    return {name: text for name, text in found.items() if text}


def totals_text(totals: object) -> tuple:
    """(long, short) session totals: '2 running · 2 done · agents $1.69 · peak 3 parallel', 'Σ 2▶ 2✓ $1.69 ⇈3'."""
    if totals is None:
        return "", ""
    cost = usd(totals.cost, totals.cost_partial) if totals.cost is not None else ""
    long = [f"{totals.running} running" if totals.running else "", f"{totals.done} done" if totals.done else "",
            f"agents {cost}" if cost else "", f"peak {totals.peak} parallel" if totals.peak > 1 else ""]
    short = [f"{totals.running}▶" if totals.running else "", f"{totals.done}✓" if totals.done else "",
             cost, f"⇈{totals.peak}" if totals.peak > 1 else ""]
    long, short = " · ".join(p for p in long if p), " ".join(p for p in short if p)
    return long, ("Σ " + short if short else "")


def base_parts(task: dict, now: float) -> tuple[list, int]:
    """Today's columns (head, model·effort, tokens, ctx, elapsed) and the tier colour."""
    agent_type = str(task.get("agentType") or task.get("name") or "agent")
    tier = tier_of(agent_type)
    glyph, colour = TIERS.get(tier, OTHER)
    status = str(task.get("status") or "running")
    if status in DONE:
        mark, mark_colour = DONE[status]
        head = paint(mark, mark_colour, bold=True)
    else:
        head = paint(SPINNER[int(now * 10) % len(SPINNER)], colour, bold=True)
    name = tier or agent_type.rsplit(":", 1)[-1]
    effort = task.get("effort")
    runs = "·".join(p for p in (short_model(task.get("model")),
                                EFFORT_SHORT.get(str(effort), str(effort)) if effort else "") if p)
    parts = [f"{head} {paint(glyph, colour)} {paint(name, colour, bold=True)}"]
    if runs:
        parts.append(dim(runs))
    meter = " ".join(p for p in (sparkline(task.get("tokenSamples") or []), tokens(task.get("tokenCount"))) if p)
    for piece in (paint(meter, colour) if meter else "", dim(context_pct(task)), dim(elapsed(task.get("startTime"), now))):
        if ANSI.sub("", piece):
            parts.append(piece)
    return parts, colour


def choose_extras(base: str, found: dict, label: str, width: int) -> list:
    """Drop extras (EXTRA_DROP_FIRST order) until the label keeps LABEL_MIN cells, or all of it if shorter."""
    want = min(len(label), LABEL_MIN) + 2 if label else 0
    kept = dict(found)
    for name in EXTRA_DROP_FIRST:
        used = visible_len(base) + sum(visible_len(t) + 2 for t in kept.values())
        if width - used >= want:
            break
        kept.pop(name, None)
    return [kept[name] for name in EXTRA_ORDER if name in kept]


def render(task: dict, width: int, now: float, stats: object = None, totals: tuple = ()) -> str:
    parts, colour = base_parts(task, now)
    base = "  ".join(parts)
    label = str(task.get("label") or task.get("description") or "").replace("\n", " ").strip()
    line = "  ".join([base] + choose_extras(base, extras(stats, colour), label, width))
    room = width - visible_len(line) - 2
    # The long totals only beside a whole label, the short ones while it keeps LABEL_MIN cells;
    # otherwise totals are the first thing to go, before any extra column.
    needs = (len(label), min(len(label), LABEL_MIN))
    fits = [t for t, need in zip(totals, needs) if t and room - visible_len(t) - 4 >= need]
    tail = dim("│ " + fits[0]) if fits else ""
    if tail:
        room -= visible_len(tail) + 2
    if label and room > 8:
        line += "  " + fit(label, room)
    return line + ("  " + tail if tail else "")


def collect_metrics(payload: dict, now: float) -> object:
    if agent_metrics is None:
        return None
    try:
        return agent_metrics.collect(payload, now)
    except Exception:  # a transcript we can't read must never cost the user their rows
        return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        width = int(payload.get("columns") or 100)
        now = time.time()
        tasks = [t for t in payload.get("tasks") or [] if isinstance(t, dict) and t.get("id")]
        metrics = collect_metrics(payload, now) if tasks else None
        rows = metrics.rows if metrics is not None else {}
        summary = totals_text(metrics.totals) if metrics is not None else ()
        for index, task in enumerate(tasks):
            line = render(task, width, now, rows.get(str(task["id"])), summary if index == len(tasks) - 1 else ())
            print(json.dumps({"id": task["id"], "content": line}, ensure_ascii=False))
    except Exception:  # never break the agent panel; default rows take over
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
