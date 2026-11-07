#!/usr/bin/env python3
"""Live subagent rows for Claude Code's agent panel (subagentStatusLine).

Input (stdin): {"columns": N, "tasks": [{id, agentType, status, description, label,
startTime, model?, effort?, contextWindowSize?, tokenCount, tokenSamples, ...}]}
Output: one JSON line per row, {"id": ..., "content": "<ansi text>"}.

Example row:
  ◆ senior-engineer  opus·high  ▂▃▅▇ 18.2k  9% ctx  1:42  Adding status column with backfill

Runs every refresh tick, so it is stdlib-only and does no I/O beyond stdin/stdout.
Any failure prints nothing, and Claude Code falls back to its default rows.
"""

from __future__ import annotations

import json
import re
import sys
import time

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


def paint(text: str, colour: int, bold: bool = False) -> str:
    return f"\x1b[{'1;' if bold else ''}38;5;{colour}m{text}\x1b[0m"


def dim(text: str) -> str:
    return paint(text, 245)


def visible_len(text: str) -> int:
    return len(ANSI.sub("", text))


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


def render(task: dict, width: int, now: float) -> str:
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
    line = "  ".join(parts)
    label = str(task.get("label") or task.get("description") or "").replace("\n", " ").strip()
    room = width - visible_len(line) - 2
    if label and room > 8:
        line += "  " + (label if len(label) <= room else label[: room - 1] + "…")
    return line


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        width = int(payload.get("columns") or 100)
        now = time.time()
        for task in payload.get("tasks") or []:
            if isinstance(task, dict) and task.get("id"):
                print(json.dumps({"id": task["id"], "content": render(task, width, now)}, ensure_ascii=False))
    except Exception:  # never break the agent panel; default rows take over
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
