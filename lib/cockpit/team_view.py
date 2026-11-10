"""Team view: the agents inside one Claude session, working together, live.

  ◆ orchestrator   opus · xhigh   3 agents · 2 running · ctx 142k
    saved ≈$4.10 (38%) · mix $0.90 · ctx $3.50 · overhead −$0.30 · 210k kept out of main
    parallel: max 3 · 41% overlap · 12m saved
  ├─ ⠼ ◇ scout      Map auth flow     sonnet·med   0:42  31k  2.1k  14  Grep verifyToken src/
  └─ ✓ ◆ senior-…   Add status col…   opus·high    4:12  96k  8.4k  57  done
  TIMELINE   ·········██████████▶ / ██████████······
  ACTIVITY   17:57:58 ◇ scout  Grep verifyToken src/

Keys are handled by a pure function; drawing reuses the dashboard's View.
"""

from __future__ import annotations

import curses
import textwrap
import time
from dataclasses import dataclass, replace

from .core import human_duration
from .pricing import context_level, fmt_money, fmt_usd
from .parallel import SessionParallel, parallel_line
from .savings import SessionSavings
from .team import AgentRun, Team, UsageBook
from .tiers import GLYPHS

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TIMELINE_MAX_SECONDS = 30 * 60
TEAM_KEYS = (("↑↓", "select"), ("⏎", "details"), ("←", "back"), ("q", "quit"))


@dataclass(frozen=True)
class TeamState:
    selected: int = 0
    detail: bool = False


def handle_team_key(state: TeamState, key: str, count: int) -> tuple[TeamState, tuple | None]:
    if key in {"KEY_UP", "k"}:
        return replace(state, selected=max(0, state.selected - 1)), None
    if key in {"KEY_DOWN", "j"}:
        return replace(state, selected=max(0, min(count - 1, state.selected + 1))), None
    if key in {"\n", "KEY_ENTER", "KEY_RIGHT", "l"}:
        return (replace(state, detail=not state.detail), None) if count else (state, None)
    if key in {"\x1b", "KEY_LEFT", "h", "KEY_BACKSPACE", "\x7f"}:
        return (replace(state, detail=False), None) if state.detail else (state, ("back",))
    if key == "q":
        return state, ("quit",)
    return state, None


# ---------------------------------------------------------------- pure helpers

def agent_label(agent_type: str) -> tuple[str, str]:
    """('orchestrator:scout') -> ('scout', 'scout'); others -> ('', short name)."""
    short = agent_type.rsplit(":", 1)[-1]
    tier = short if agent_type.startswith("orchestrator:") and short in GLYPHS else ""
    return tier, short


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def short_model(model: str) -> str:
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in model:
            return family
    return model.split("-")[0] if model else "?"


def runs_on(agent: AgentRun) -> str:
    effort = {"medium": "med"}.get(agent.effort, agent.effort)
    return "·".join(p for p in (short_model(agent.model), effort) if p)


def fmt_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{count / 1000:.0f}k" if count >= 10_000 else (f"{count / 1000:.1f}k" if count >= 1000 else str(count))


def timeline_bar(start: float | None, end: float | None, running: bool,
                 window_start: float, window_end: float, width: int) -> str:
    if width <= 0:
        return ""
    if start is None or window_end <= window_start:
        return "·" * width
    span = window_end - window_start
    first = int((max(start, window_start) - window_start) / span * width)
    last_t = window_end if running else (end or start)
    last = max(first, min(width - 1, int((min(last_t, window_end) - window_start) / span * width)))
    cells = ["·"] * width
    for i in range(max(0, first), last + 1):
        cells[i] = "█"
    if running:
        cells[last] = "▶"
    return "".join(cells)


def timeline_window(team: Team, now_s: float) -> tuple[float, float]:
    starts = [a.started for a in team.agents if a.started]
    earliest = min(starts) if starts else now_s - 60
    return max(earliest, now_s - TIMELINE_MAX_SECONDS), now_s


def savings_lines(savings: SessionSavings | None) -> list[tuple[str, str]]:
    """Header lines (text, tone) for delegation savings; none until an agent has run."""
    if savings is None or not savings.runs:
        return []
    pct = f" ({savings.pct:.0f}%)" if savings.pct is not None else ""
    line = (f"saved ≈{fmt_money(savings.saved_usd)}{pct} · mix {fmt_money(savings.model_mix_usd)}"
            f" · ctx {fmt_money(savings.context_usd)} · overhead −{fmt_usd(savings.overhead_usd)}"
            f" · {fmt_tokens(max(0, savings.kept_out_tokens))} kept out of main")
    if savings.partial:
        line += " · partial"
    lines = [(line, "warn" if savings.saved_usd < 0 else "ok")]
    if savings.would_compact:
        lines.append((f"inline, main would have reached ≈{fmt_tokens(savings.inline_peak_tokens)} of "
                      f"{fmt_tokens(savings.window)} and compacted: treat ctx as an upper bound", "warn"))
    return lines


def state_mark(agent: AgentRun, now_s: float, tick: int) -> tuple[str, str]:
    state = agent.state(now_s)
    if state == "done":
        return "✓", "ok"
    if state == "stalled":
        return "◌", "stale"
    return SPINNER[tick % len(SPINNER)], "busy"


# ---------------------------------------------------------------- drawing

class TeamScreen:
    def __init__(self, view, session_name: str) -> None:
        self.view, self.session_name = view, session_name

    def tone(self, agent: AgentRun) -> str:
        tier, _ = agent_label(agent.agent_type)
        return f"tier:{tier}" if tier else "dim"

    def draw(self, team: Team, state: TeamState, tick: int, savings: SessionSavings | None = None,
             parallel: SessionParallel | None = None) -> None:
        v, screen = self.view, self.view.screen
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 16 or width < 80:
            v.put(0, 0, "Make the window a little bigger (≥ 80×16) for the team view.", v.attr("warn"))
            screen.refresh()
            return
        now_s = time.time()
        x = v.put(0, 1, " ◆ claude-cockpit ", v.attr("accent", curses.A_BOLD, curses.A_REVERSE))
        v.put(0, x, f" › team · {self.session_name}", v.attr("accent", curses.A_BOLD))
        v.put(0, width - 10, time.strftime("%H:%M:%S"), v.attr("dim"))
        y = self._orchestrator(2, team, now_s, tick, width)
        for text, tone in savings_lines(savings):
            v.put(y, 4, clip(text, width - 6), v.attr(tone))
            y += 1
        line = parallel_line(parallel)
        if line:
            v.put(y, 4, clip(line, width - 6), v.attr("warn" if parallel.partial else "dim"))
            y += 1
        rows = team.ordered()
        y = self._agents(y + 1, rows, state, now_s, tick, width, height)
        if state.detail and rows:
            self._detail(y + 1, rows[min(state.selected, len(rows) - 1)][0], team, height, width)
        else:
            y = self._timeline(y + 1, rows, team, now_s, width, height)
            self._activity(y + 1, team, height, width)
        self._footer(height, width)
        screen.refresh()

    def _orchestrator(self, y: int, team: Team, now_s: float, tick: int, width: int) -> int:
        v = self.view
        if not team.found:
            v.put(y, 2, "No transcript found for this session yet. It appears after the first message.", v.attr("dim"))
            return y + 1
        main = team.main
        x = v.put(y, 2, ("◐◓◑◒"[tick % 4] if team.running(now_s) else "◆") + " orchestrator",
                  v.attr("accent", curses.A_BOLD))
        if main:
            x = v.put(y, x, f"   {runs_on(main)}", v.attr("accent"))
        x = v.put(y, x, f"   {len(team.agents)} agents · {team.running(now_s)} running", curses.A_BOLD)
        if main and main.context_tokens:
            level = context_level(main.context_pct)
            tone = {"ok": "bar", "warn": "warn", "critical": "error"}[level]
            x = v.put(y, x, f"   context {fmt_tokens(main.context_tokens)}/{fmt_tokens(main.window)} ", v.attr("dim"))
            x = v.put(y, x, f"{main.context_pct:.0f}%", v.attr(tone, curses.A_BOLD))
            total = main.cost_usd + sum(a.cost_usd for a in team.agents)
            x = v.put(y, x, f"   ≈{fmt_usd(total)} session · {fmt_usd(main.last_turn_usd)}/turn", v.attr("dim"))
        if main and main.now:
            v.put(y + 1, 4, f"now: {main.now}"[: width - 6], v.attr("dim"))
        return y + 2

    def _agents(self, y: int, rows: list, state: TeamState, now_s: float, tick: int,
                width: int, height: int) -> int:
        v = self.view
        v.put(y, 2, "AGENTS", v.attr("accent", curses.A_BOLD))
        desc_w = max(12, min(30, width - 100))
        v.put(y, 10, f"{'':<24}{'TASK':<{desc_w}} {'MODEL':<12}{'TIME':>6} {'CTX':>6} {'COST':>7} {'TOOLS':>5}  NOW",
              v.attr("dim"))
        y += 1
        if not rows:
            v.put(y, 4, "No agents in this session yet. They'll appear here the moment one starts.", v.attr("dim"))
            return y + 1
        budget = max(3, min(len(rows), (height - y) // 2))
        start = max(0, state.selected - budget + 1)
        for index, (agent, prefix) in enumerate(rows[start:start + budget], start=start):
            self._agent_row(y, agent, prefix, index == state.selected, now_s, tick, width, desc_w)
            y += 1
        return y

    def _agent_row(self, y: int, agent: AgentRun, prefix: str, selected: bool, now_s: float,
                   tick: int, width: int, desc_w: int) -> None:
        v = self.view
        tier, short = agent_label(agent.agent_type)
        mark, mark_tone = state_mark(agent, now_s, tick)
        sel = curses.A_REVERSE if selected else 0
        x = v.put(y, 2, prefix, v.attr("dim"))
        x = v.put(y, x, mark + " ", v.attr(mark_tone, curses.A_BOLD))
        x = v.put(y, x, f"{GLYPHS.get(tier, '●')} {clip(short, 16):<16} ", v.attr(self.tone(agent), curses.A_BOLD) | sel)
        x = max(x, 34)
        x = v.put(y, x, f"{agent.description[:desc_w]:<{desc_w}} ", sel)
        x = v.put(y, x, f"{runs_on(agent):<12}", v.attr(self.tone(agent)))
        x = v.put(y, x, f"{human_duration(agent.elapsed(now_s)):>6} ", v.attr("dim"))
        x = v.put(y, x, f"{fmt_tokens(agent.context_tokens):>6} {fmt_usd(agent.cost_usd):>7} "
                        f"{agent.tool_calls:>5}  ", v.attr("dim"))
        now_text = "done" if agent.state(now_s) == "done" else (agent.now or "starting…")
        v.put(y, x, now_text[: max(0, width - x - 2)], v.attr("ok" if now_text == "done" else "idle"))

    def _timeline(self, y: int, rows: list, team: Team, now_s: float, width: int, height: int) -> int:
        v = self.view
        if not rows or height - y < 6:
            return y
        start, end = timeline_window(team, now_s)
        v.put(y, 2, "TIMELINE", v.attr("accent", curses.A_BOLD))
        v.put(y, 12, f"last {human_duration(int(end - start))}, all agents on one clock", v.attr("dim"))
        y += 1
        bar_w = width - 22
        for agent, _ in rows[: min(len(rows), max(1, (height - y) // 2 - 1))]:
            _, short = agent_label(agent.agent_type)
            v.put(y, 4, f"{clip(short, 15):<16}", v.attr(self.tone(agent)))
            bar = timeline_bar(agent.started, agent.last_activity, agent.state(now_s) == "running", start, end, bar_w)
            v.put(y, 20, bar, v.attr(self.tone(agent), curses.A_BOLD))
            y += 1
        return y

    def _activity(self, y: int, team: Team, height: int, width: int) -> None:
        v = self.view
        if height - y < 4:
            return
        v.put(y, 2, "ACTIVITY", v.attr("accent", curses.A_BOLD))
        y += 1
        by_id = {a.agent_id: a for a in team.agents}
        for event in team.feed[: height - y - 2]:
            agent = by_id.get(event.agent_id)
            tier, short = agent_label(agent.agent_type) if agent else ("", "orchestrator")
            tone = f"tier:{tier}" if tier else ("accent" if not agent else "dim")
            x = v.put(y, 4, time.strftime("%H:%M:%S", time.localtime(event.ts)) + "  ", v.attr("dim"))
            x = v.put(y, x, f"{GLYPHS.get(tier, '◆' if not agent else '●')} {clip(short, 14):<15}", v.attr(tone, curses.A_BOLD))
            v.put(y, x, event.summary[: max(0, width - x - 2)])
            y += 1

    def _detail(self, y: int, agent: AgentRun, team: Team, height: int, width: int) -> None:
        v = self.view
        _, short = agent_label(agent.agent_type)
        v.put(y, 2, f"DETAILS · {short}", v.attr(self.tone(agent), curses.A_BOLD))
        v.put(y, 24, f"{agent.description}  ·  {agent.model} {agent.effort}  ·  out {fmt_tokens(agent.output_tokens)}"
                     f"  ·  ≈{fmt_usd(agent.cost_usd)}", v.attr("dim"))
        y += 1
        calls = [e for e in team.feed if e.agent_id == agent.agent_id][:6]
        for event in calls:
            v.put(y, 4, time.strftime("%H:%M:%S", time.localtime(event.ts)) + "  " + event.summary[: width - 16])
            y += 1
        if agent.report and height - y > 4:
            v.put(y, 2, "LATEST REPORT", v.attr("accent", curses.A_BOLD))
            y += 1
            for line in textwrap.wrap(" ".join(agent.report.split()), width - 8)[: height - y - 2]:
                v.put(y, 4, line, v.attr("idle"))
                y += 1

    def _footer(self, height: int, width: int) -> None:
        v, x = self.view, 2
        for key, label in TEAM_KEYS:
            x = v.put(height - 1, x, f" {key} ", v.attr("accent", curses.A_REVERSE, curses.A_BOLD))
            x = v.put(height - 1, x, f" {label}  ", v.attr("dim"))
        v.put(height - 1, max(x, width - 34), "read from disk · zero tokens", v.attr("dim"))


def team_loop(screen, view, session_id: str, session_name: str, read_key, refresh_s: float = 1.0) -> str:
    """Runs until the user goes back ("back") or quits ("quit")."""
    book, ui, state, tick = UsageBook(), TeamScreen(view, session_name), TeamState(), 0
    watcher = book.watcher(session_id)

    def fetch() -> tuple[Team, SessionSavings | None, SessionParallel | None]:
        team = watcher.snapshot()
        if not team.found:
            return team, None, None
        # Both are cached in the book and recomputed only when a transcript grew.
        return team, book.savings(session_id), book.parallel(session_id)

    (team, saved, parallel), fetched = fetch(), time.monotonic()
    while True:
        if time.monotonic() - fetched >= refresh_s:
            (team, saved, parallel), fetched = fetch(), time.monotonic()
        ui.draw(team, state, tick, saved, parallel)
        tick += 1
        key = read_key(screen)
        if not key or key == "KEY_RESIZE":
            continue
        state, action = handle_team_key(state, key, len(team.agents))
        if action:
            return action[0]
