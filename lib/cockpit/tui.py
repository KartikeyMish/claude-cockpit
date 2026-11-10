"""The cockpit dashboard: a live, keyboard-driven view of every Claude session.

Design: `handle_key` is a pure function (state, key) -> (state, action), so all
interaction logic is unit-tested without a terminal. curses only draws and
executes the returned actions. Stdlib only; ~15 MB RSS.
"""

from __future__ import annotations

import curses
import locale
import os
import re
import time
from dataclasses import dataclass, field, replace

from . import VERSION
from .pricing import context_level, fmt_signed_usd, fmt_usd
from .savings import SessionSavings
from .team import SessionUsage, UsageBook
from .tiers import COLOURS, load_tiers
from .doctor import HOOKS_HINT, HOOKS_MARK, HooksProbe, stale_pids
from .core import (
    CLAUDE_HOME, DEFAULT_IDLE_HOURS, SESSIONS_DIR, CockpitError, Proc, Session, current_ms, duplicate_of, gc_candidates,
    human_duration, human_mb, load_sessions, parse_hours, protected_pids, read_processes,
    short_path, system_memory_kb, terminal_totals_kb, terminate,
)

REFRESH_SECONDS = 1.5
TICK_MS = 250  # 4 fps is enough for the busy pulse and keeps CPU near zero
STALE_SECONDS = parse_hours(DEFAULT_IDLE_HOURS)
WORKTREE_NAME = re.compile(r"[A-Za-z0-9._/-]")
BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"
PULSE = "◐◓◑◒"

# Session row: everything except NAME and DIR takes ROW_FIXED columns (incl. the SAVED cell).
ROW_FIXED = 62
SAVED_W = 9
MIN_DIR_W = 8

KEYS_MAIN = (("⏎", "claude here"), ("v", "team"), ("w", "worktree"), ("x", "close"), ("g", "clean idle"),
             ("a", "background"), ("t", "tiers"), ("d", "doctor"), ("q", "quit"))


# ---------------------------------------------------------------- state

@dataclass(frozen=True)
class UiState:
    selected: int = 0
    mode: str = "main"            # main | confirm-close | confirm-gc | input-wt
    show_sdk: bool = False
    show_tiers: bool = True
    buffer: str = ""
    message: str = ""
    message_kind: str = "info"    # info | warn | ok | error


@dataclass(frozen=True)
class Snapshot:
    sessions: tuple[Session, ...] = ()
    now: int = 0
    protected: frozenset[int] = frozenset()
    terminals: dict[str, int] = field(default_factory=dict)
    system_kb: int | None = None
    error: str = ""
    usage: dict[int, SessionUsage] = field(default_factory=dict)   # pid -> context + spend
    savings: dict[int, SessionSavings] = field(default_factory=dict)  # pid -> delegation savings
    hooks_stale: frozenset[int] = frozenset()  # pids running hooks older than hooks.json (restart them)

    @property
    def claude_kb(self) -> int:
        return sum(s.tree_rss_kb for s in self.sessions)

    @property
    def spend_usd(self) -> float:
        """Each conversation once, even when it is open in two terminals."""
        dupes = duplicate_of(self.sessions)
        return sum(u.cost_usd for pid, u in self.usage.items() if pid not in dupes)

    def context_alerts(self) -> list[tuple[Session, SessionUsage]]:
        """Interactive sessions whose context is filling up, fullest first."""
        flagged = [(s, self.usage[s.pid]) for s in self.sessions
                   if not s.is_sdk and s.pid in self.usage and context_level(self.usage[s.pid].pct) != "ok"]
        return sorted(flagged, key=lambda pair: -pair[1].pct)

    def stale(self) -> list[Session]:
        return gc_candidates(self.sessions, self.now, STALE_SECONDS, set(self.protected))


Action = tuple  # ("launch",) | ("worktree", name) | ("close", pid) | ("gc",) | ("doctor",) | ("quit",)


def visible(snapshot: Snapshot, state: UiState) -> list[Session]:
    """Working sessions first, then most recently used, stale ones sink to the bottom."""
    rows = [s for s in snapshot.sessions if state.show_sdk or not s.is_sdk]
    return sorted(rows, key=lambda s: (s.status != "waiting", s.status != "busy",
                                       s.idle_seconds(snapshot.now) or 0, s.name))


def clamp(state: UiState, count: int) -> UiState:
    return replace(state, selected=max(0, min(state.selected, count - 1)) if count else 0)


# ---------------------------------------------------------------- keys (pure)

def handle_key(state: UiState, key: str, rows: list[Session], snapshot: Snapshot) -> tuple[UiState, Action | None]:
    if state.mode == "input-wt":
        return _key_input(state, key)
    if state.mode in {"confirm-close", "confirm-gc"}:
        return _key_confirm(state, key, rows)
    return _key_main(state, key, rows, snapshot)


def _key_main(state: UiState, key: str, rows: list[Session], snapshot: Snapshot) -> tuple[UiState, Action | None]:
    quiet = replace(state, message="")
    if key in {"KEY_UP", "k"}:
        return clamp(replace(quiet, selected=state.selected - 1), len(rows)), None
    if key in {"KEY_DOWN", "j"}:
        return clamp(replace(quiet, selected=state.selected + 1), len(rows)), None
    if key in {"\n", "KEY_ENTER", "c"}:
        return quiet, ("launch",)
    if key == "w":
        return replace(quiet, mode="input-wt", buffer=""), None
    if key == "a":
        return clamp(replace(quiet, show_sdk=not state.show_sdk, selected=0), len(rows)), None
    if key == "t":
        return replace(quiet, show_tiers=not state.show_tiers), None
    if key == "d":
        return quiet, ("doctor",)
    if key in {"v", "KEY_RIGHT", "l"}:
        if not rows:
            return replace(quiet, message="No session to open.", message_kind="warn"), None
        target = rows[state.selected]
        if not target.session_id:
            return replace(quiet, message="This session has no id yet; try again in a moment.", message_kind="warn"), None
        return quiet, ("team", target.pid)
    if key in {"q", "\x1b"}:
        return quiet, ("quit",)
    if key == "x":
        return _ask_close(quiet, rows, snapshot), None
    if key == "g":
        stale = snapshot.stale()
        if not stale:
            return replace(quiet, message=f"Nothing idle ≥ {DEFAULT_IDLE_HOURS}h. Nice and lean.", message_kind="ok"), None
        held = human_mb(sum(s.tree_rss_kb for s in stale))
        return replace(quiet, mode="confirm-gc", message=f"Close {len(stale)} sessions idle ≥ {DEFAULT_IDLE_HOURS}h and free ~{held}? y/n",
                       message_kind="warn"), None
    return state, None


def _ask_close(state: UiState, rows: list[Session], snapshot: Snapshot) -> UiState:
    if not rows:
        return replace(state, message="No session selected.", message_kind="warn")
    target = rows[state.selected]
    if target.pid in snapshot.protected:
        return replace(state, message="That's this session; close it from its own tab.", message_kind="warn")
    if target.is_sdk:
        return replace(state, message="Background sessions belong to tools; leave them be.", message_kind="warn")
    if target.status == "busy":
        return replace(state, message=f"{target.name} is working; interrupt it first.", message_kind="warn")
    return replace(state, mode="confirm-close", message_kind="warn",
                   message=f"Close {target.name} ({human_mb(target.tree_rss_kb)})? It stays resumable. y/n")


def _key_confirm(state: UiState, key: str, rows: list[Session]) -> tuple[UiState, Action | None]:
    back = replace(state, mode="main")
    if key not in {"y", "Y"}:
        return replace(back, message="Cancelled.", message_kind="info"), None
    if state.mode == "confirm-gc":
        return replace(back, message="Closing idle sessions…"), ("gc",)
    if not rows:
        return replace(back, message="Session is gone.", message_kind="info"), None
    return replace(back, message="Closing…"), ("close", rows[state.selected].pid)


def _key_input(state: UiState, key: str) -> tuple[UiState, Action | None]:
    if key == "\x1b":
        return replace(state, mode="main", buffer="", message="Cancelled."), None
    if key in {"\n", "KEY_ENTER"}:
        name = state.buffer.strip("/")
        if not name:
            return replace(state, message="Type a worktree name, or Esc.", message_kind="warn"), None
        return replace(state, mode="main", buffer=""), ("worktree", name)
    if key in {"KEY_BACKSPACE", "\x7f", "\b"}:
        return replace(state, buffer=state.buffer[:-1]), None
    if len(key) == 1 and WORKTREE_NAME.fullmatch(key) and len(state.buffer) < 60:
        return replace(state, buffer=state.buffer + key, message=""), None
    return state, None


# ---------------------------------------------------------------- drawing helpers (pure)

def bar(fraction: float, width: int) -> str:
    fraction = max(0.0, min(1.0, fraction))
    eighths = int(round(fraction * width * 8))
    full, part = divmod(eighths, 8)
    text = "█" * full + (BAR_EIGHTHS[part] if part and full < width else "")
    return text.ljust(width, "·")


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def session_columns(width: int) -> tuple[int, int]:
    """(NAME width, DIR width) for the session table. DIR is dropped (0) when the terminal
    is too narrow for every column, so the row never wraps (an 80-column window has no DIR)."""
    name_w = max(16, min(28, width - 84))
    if ROW_FIXED + name_w + MIN_DIR_W + 1 > width:
        return name_w, 0
    return name_w, max(MIN_DIR_W, width - name_w - ROW_FIXED - 21)  # 21: room for "◀ you" notes


def sessions_header(name_w: int, dir_w: int) -> str:
    directory = f"{'DIR':<{dir_w}} " if dir_w else ""
    return (f"   {'NAME':<{name_w}} {directory}{'STATE':<7} {'IDLE':>7} {'RAM':>8}  "
            f"{'CONTEXT':<11} {'COST':>8} {'SAVED':>{SAVED_W}}")


def saved_cell(savings: SessionSavings | None) -> tuple[str, str]:
    """Signed delegation saving and its colour; ~ = partial (unknown model or truncated
    transcript), ! = the inline estimate would have overflowed the context window."""
    if savings is None or not savings.runs:
        return "-", "dim"
    text = fmt_signed_usd(savings.saved_usd) + ("~" if savings.partial else "") + ("!" if savings.would_compact else "")
    return text, "warn" if savings.saved_usd < 0 else "ok"


def name_cell(session: Session, name_w: int, hooks_stale: bool) -> str:
    """The NAME cell, name_w - 1 wide; a stale-hooks session ends in ` ⟳` without widening it."""
    name = session.name + (" [bg]" if session.is_sdk else "")
    width = name_w - 1
    text = f"{fit(name, width - 2)} {HOOKS_MARK}" if hooks_stale else fit(name, width)
    return f"{text:<{width}}"


def footer_hint(snap: Snapshot, rows: list[Session]) -> str:
    """Legend for ⟳, shown above the key bar while a listed session needs a restart."""
    return HOOKS_HINT if any(s.pid in snap.hooks_stale for s in rows) else ""


def state_glyph(session: Session, now: int, tick: int) -> tuple[str, str]:
    if session.status == "waiting":
        return ("◉" if tick % 2 else "○"), "warn"   # blocked on the user: blink
    if session.status == "busy":
        return PULSE[tick % len(PULSE)], "busy"
    idle = session.idle_seconds(now)
    if idle is None:
        return "?", "dim"
    return ("◌", "stale") if idle >= STALE_SECONDS else ("○", "idle")


# ---------------------------------------------------------------- data

_PROBE: HooksProbe | None = None


def hooks_probe() -> HooksProbe:
    """One cached hooks.json lookup per dashboard process (re-checked every few seconds)."""
    global _PROBE
    if _PROBE is None:
        _PROBE = HooksProbe(CLAUDE_HOME)
    return _PROBE


def take_snapshot(book: UsageBook | None = None, probe: HooksProbe | None = None) -> Snapshot:
    try:
        procs: dict[int, Proc] = read_processes()
    except CockpitError as exc:
        return Snapshot(now=current_ms(), error=str(exc))
    now = current_ms()
    sessions = tuple(load_sessions(SESSIONS_DIR, procs, now))
    usage: dict[int, SessionUsage] = {}
    savings: dict[int, SessionSavings] = {}
    if book is not None:
        for s in sessions:
            found = book.usage(s.session_id) if s.session_id else None
            if found:
                usage[s.pid] = found
                saved = book.savings(s.session_id)  # cached until a transcript grows
                if saved:
                    savings[s.pid] = saved
        book.keep_only({s.session_id for s in sessions if s.session_id})
    hooks_ms = (probe or hooks_probe()).stale_after_ms()
    return Snapshot(
        sessions=sessions, now=now, protected=frozenset(protected_pids(procs)),
        terminals=terminal_totals_kb(procs), system_kb=system_memory_kb(), usage=usage, savings=savings,
        hooks_stale=stale_pids(procs, sessions, hooks_ms, now),
    )


def close_one(pid: int, expected_update: int | None) -> tuple[str, str]:
    procs = read_processes()
    fresh = {s.pid: s for s in load_sessions(SESSIONS_DIR, procs)}
    target = fresh.get(pid)
    if not target or target.status != "idle" or target.status_updated_ms != expected_update:
        return "It was used since you selected it; left running.", "warn"
    if pid in protected_pids(procs):
        return "Refusing to close this session.", "warn"
    ok = terminate(pid)
    return ((f"Closed {target.name}, freed ~{human_mb(target.tree_rss_kb)}.", "ok") if ok
            else (f"{target.name} did not exit; try again or close its tab.", "error"))


def close_stale(snapshot: Snapshot) -> tuple[str, str]:
    closed, freed, skipped = 0, 0, 0
    for s in snapshot.stale():
        message, kind = close_one(s.pid, s.status_updated_ms)
        if kind == "ok":
            closed, freed = closed + 1, freed + s.tree_rss_kb
        else:
            skipped += 1
    note = f" ({skipped} skipped)" if skipped else ""
    return f"Closed {closed} idle sessions, freed ~{human_mb(freed)}{note}.", "ok" if closed else "warn"


# ---------------------------------------------------------------- curses view

class View:
    def __init__(self, screen) -> None:
        self.screen = screen
        self.colors: dict[str, int] = {}
        self.tiers = load_tiers()
        self._init_colors()

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        rich = curses.COLORS >= 256
        palette = {
            "accent": (183, curses.COLOR_MAGENTA), "busy": (150, curses.COLOR_GREEN),
            "idle": (252, curses.COLOR_WHITE), "stale": (223, curses.COLOR_YELLOW),
            "dim": (245, curses.COLOR_WHITE), "warn": (223, curses.COLOR_YELLOW),
            "ok": (150, curses.COLOR_GREEN), "error": (211, curses.COLOR_RED),
            "info": (117, curses.COLOR_CYAN), "bar": (111, curses.COLOR_BLUE),
        }
        palette.update({f"tier:{t.name}": COLOURS.get(t.colour, (252, curses.COLOR_WHITE)) for t in self.tiers})
        for index, (name, (c256, c8)) in enumerate(palette.items(), start=1):
            curses.init_pair(index, c256 if rich else c8, background)
            self.colors[name] = curses.color_pair(index)

    def attr(self, name: str, *extra: int) -> int:
        value = self.colors.get(name, 0)
        for flag in extra:
            value |= flag
        return value

    def put(self, y: int, x: int, text: str, attr: int = 0) -> int:
        height, width = self.screen.getmaxyx()
        if y >= height or x >= width or y < 0:
            return x
        clipped = text[: max(0, width - x - (1 if y == height - 1 else 0))]
        try:
            self.screen.addstr(y, x, clipped, attr)
        except curses.error:
            pass
        return x + len(clipped)

    def draw(self, snap: Snapshot, state: UiState, rows: list[Session], tick: int) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 14 or width < 64:
            self.put(0, 0, "Make the window a little bigger (≥ 64×14) for the cockpit.", self.attr("warn"))
            self.screen.refresh()
            return
        y = self._header(snap, width)
        y = self._sessions(y + 1, snap, state, rows, tick, height, width)
        if state.show_tiers and height - y >= 9:
            y = self._tiers(y + 1, width)
        self._footer(state, height, width, footer_hint(snap, rows))
        self.screen.refresh()

    def _header(self, snap: Snapshot, width: int) -> int:
        x = self.put(0, 1, " ◆ claude-cockpit ", self.attr("accent", curses.A_BOLD, curses.A_REVERSE))
        self.put(0, x + 1, f"v{VERSION}", self.attr("dim"))
        clock = time.strftime("%H:%M:%S")
        self.put(0, width - len(clock) - 2, clock, self.attr("dim"))
        total = snap.claude_kb
        x = self.put(2, 2, "claude RAM ", self.attr("dim"))
        if snap.system_kb:
            x = self.put(2, x, bar(total / snap.system_kb, 24), self.attr("bar"))
            x = self.put(2, x, f"  {human_mb(total)} of {human_mb(snap.system_kb)}", curses.A_BOLD)
        else:
            x = self.put(2, x, human_mb(total), curses.A_BOLD)
        for name, kb in sorted(snap.terminals.items(), key=lambda kv: -kv[1]):
            x = self.put(2, x, f"   {name} {human_mb(kb)}", self.attr("dim"))
        x = self.put(3, 2, "spend      ", self.attr("dim"))
        x = self.put(3, x, f"≈ {fmt_usd(snap.spend_usd)}", curses.A_BOLD)
        self.put(3, x, "  API-equivalent, live sessions incl. their agents", self.attr("dim"))
        y = 4
        waiting = [s for s in snap.sessions if s.status == "waiting" and not s.is_sdk]
        if waiting:
            names = ", ".join(s.name for s in waiting[:3]) + (" …" if len(waiting) > 3 else "")
            self.put(y, 2, f"◉ {len(waiting)} waiting for you: {names}", self.attr("warn", curses.A_BOLD))
            y += 1
        for session, usage in snap.context_alerts()[:2]:
            critical = context_level(usage.pct) == "critical"
            advice = "run /compact or start fresh" if critical else "consider /compact soon"
            self.put(y, 2, f"{'⚠' if critical else '◔'} {session.name} is at {usage.pct:.0f}% context "
                           f"({usage.context_tokens // 1000}k of {usage.window // 1000}k): {advice}. "
                           f"Each message now costs ~{fmt_usd(usage.last_turn_usd)}.",
                     self.attr("error" if critical else "warn", curses.A_BOLD))
            y += 1
        stale = snap.stale()
        if stale:
            held = human_mb(sum(s.tree_rss_kb for s in stale))
            self.put(y, 2, f"⚑ {len(stale)} sessions idle ≥ {DEFAULT_IDLE_HOURS}h hold {held}. Press g to reclaim.",
                     self.attr("stale"))
            y += 1
        if snap.error:
            self.put(y, 2, f"✗ {snap.error}", self.attr("error"))
            y += 1
        return y

    def _sessions(self, y: int, snap: Snapshot, state: UiState, rows: list[Session],
                  tick: int, height: int, width: int) -> int:
        hidden = sum(1 for s in snap.sessions if s.is_sdk) if not state.show_sdk else 0
        title = f"SESSIONS {len(rows)}" + (f"   +{hidden} background (a)" if hidden else "")
        self.put(y, 2, title, self.attr("accent", curses.A_BOLD))
        y += 1
        name_w, dir_w = session_columns(width)
        self.put(y, 2, sessions_header(name_w, dir_w), self.attr("dim"))
        y += 1
        budget = height - y - (11 if state.show_tiers else 3)
        if not rows:
            self.put(y, 4, "No Claude sessions running. Press ⏎ to start one here.", self.attr("dim"))
            return y + 1
        start = max(0, state.selected - max(1, budget) + 1)
        for index, session in enumerate(rows[start:start + max(1, budget)], start=start):
            self._session_row(y, session, snap, index == state.selected, tick, name_w, dir_w)
            y += 1
        return y

    def _session_row(self, y: int, s: Session, snap: Snapshot, selected: bool, tick: int,
                     name_w: int, dir_w: int) -> None:
        glyph, tone = state_glyph(s, snap.now, tick)
        sel = curses.A_REVERSE if selected else 0
        marker = "▸" if selected else " "
        x = self.put(y, 1, f" {marker} ", self.attr("accent", curses.A_BOLD))
        x = self.put(y, x, glyph + " ", self.attr(tone, curses.A_BOLD))
        dupes = duplicate_of(snap.sessions)
        you = "  ◀ you" if s.pid in snap.protected else (f"  ⧉ same chat as {dupes[s.pid]}" if s.pid in dupes else "")
        name, name_x = name_cell(s, name_w, s.pid in snap.hooks_stale), x  # column 6, same right edge as NAME
        x = self.put(y, x, f"{name} ", sel | (curses.A_BOLD if selected else 0))
        if s.pid in snap.hooks_stale:  # recolour the mark so it reads as an alert
            self.put(y, name_x + name.rindex(HOOKS_MARK), HOOKS_MARK, self.attr("warn", curses.A_BOLD) | sel)
        if dir_w:
            x = self.put(y, x, f"{fit(short_path(s.cwd), dir_w):<{dir_w}} ", self.attr("dim") | sel)
        x = self.put(y, x, f"{(s.status or '?'):<7} ", self.attr(tone) | sel)
        x = self.put(y, x, f"{human_duration(s.idle_seconds(snap.now)):>7} ", self.attr(tone) | sel)
        x = self.put(y, x, f"{human_mb(s.tree_rss_kb):>8}  ", sel)
        usage = snap.usage.get(s.pid)
        if usage:
            level = context_level(usage.pct)
            tone_ctx = {"ok": "bar", "warn": "warn", "critical": "error"}[level]
            x = self.put(y, x, bar(usage.pct / 100, 6), self.attr(tone_ctx, curses.A_BOLD))
            x = self.put(y, x, f"{usage.pct:>4.0f}% ", self.attr(tone_ctx))
            cost = ("≥" if usage.partial else "") + fmt_usd(usage.cost_usd if usage.known else None)
            x = self.put(y, x, f"{cost:>8}", sel)
        else:
            x = self.put(y, x, f"{'·' * 6}{'':>5} {'-':>8}", self.attr("dim"))
        saved, saved_tone = saved_cell(snap.savings.get(s.pid))
        x = self.put(y, x, f" {saved:>{SAVED_W}}", self.attr(saved_tone) | sel)
        self.put(y, x, you, self.attr("accent", curses.A_BOLD))

    def _tiers(self, y: int, width: int) -> int:
        self.put(y, 2, "AGENT TIERS", self.attr("accent", curses.A_BOLD))
        self.put(y, 15, "the orchestrator routes work to the lowest tier that fits", self.attr("dim"))
        y += 1
        if not self.tiers:
            self.put(y, 4, "agent files not found next to ccx; reinstall from the repo", self.attr("warn"))
            return y + 1
        for tier in self.tiers:
            tone = self.attr(f"tier:{tier.name}", curses.A_BOLD)
            x = self.put(y, 4, f"{tier.glyph} {tier.name:<16}", tone)
            x = self.put(y, x, f"{tier.runs_on:<17}", self.attr(f"tier:{tier.name}"))
            self.put(y, x, fit(tier.lane, width - x - 2), self.attr("dim"))
            y += 1
        return y

    def _footer(self, state: UiState, height: int, width: int, hint: str = "") -> None:
        if state.mode == "input-wt":
            prompt = "worktree name ⟩ "
            x = self.put(height - 2, 2, prompt, self.attr("accent", curses.A_BOLD))
            self.put(height - 2, x, state.buffer + "█", curses.A_BOLD)
            self.put(height - 1, 2, "⏎ start Claude in a new git worktree   Esc cancel", self.attr("dim"))
            return
        if state.message:
            self.put(height - 2, 2, fit(state.message, width - 4), self.attr(state.message_kind, curses.A_BOLD))
        elif hint:
            self.put(height - 2, 2, fit(hint, width - 4), self.attr("warn"))
        x = 2
        for key, label in KEYS_MAIN:
            if x + len(key) + len(label) + 3 >= width:
                break
            x = self.put(height - 1, x, f" {key} ", self.attr("accent", curses.A_REVERSE, curses.A_BOLD))
            x = self.put(height - 1, x, f" {label}  ", self.attr("dim"))


# ---------------------------------------------------------------- loop

def read_key(screen) -> str | None:
    try:
        key = screen.get_wch()
    except curses.error:
        return None
    if isinstance(key, int):
        return {curses.KEY_UP: "KEY_UP", curses.KEY_DOWN: "KEY_DOWN", curses.KEY_ENTER: "KEY_ENTER",
                curses.KEY_LEFT: "KEY_LEFT", curses.KEY_RIGHT: "KEY_RIGHT",
                curses.KEY_BACKSPACE: "KEY_BACKSPACE", curses.KEY_RESIZE: "KEY_RESIZE"}.get(key, "")
    return "\n" if key == "\r" else key


def pause_for(screen, run) -> None:
    curses.def_prog_mode()
    curses.endwin()
    try:
        run()
        input("\n  press ⏎ to return to the cockpit ")
    except (EOFError, KeyboardInterrupt):
        pass
    curses.reset_prog_mode()
    screen.refresh()


def dashboard(screen) -> Action | None:
    curses.curs_set(0)
    screen.timeout(TICK_MS)
    view = View(screen)
    book = UsageBook()
    state, snap, fetched, tick = UiState(), take_snapshot(book), time.monotonic(), 0
    while True:
        if time.monotonic() - fetched >= REFRESH_SECONDS:
            snap, fetched = take_snapshot(book), time.monotonic()
        rows = visible(snap, state)
        state = clamp(state, len(rows))
        view.draw(snap, state, rows, tick)
        tick += 1
        key = read_key(screen)
        if not key or key == "KEY_RESIZE":
            continue
        state, action = handle_key(state, key, rows, snap)
        if not action:
            continue
        if action[0] in {"quit", "launch", "worktree"}:
            return action
        if action[0] == "team":
            target = next((s for s in rows if s.pid == action[1]), None)
            if target and target.session_id:
                from .team_view import team_loop
                if team_loop(screen, view, target.session_id, target.name, read_key) == "quit":
                    return ("quit",)
        elif action[0] == "doctor":
            from .cli import cmd_doctor
            pause_for(screen, lambda: cmd_doctor([]))
        elif action[0] == "close":
            target = next((s for s in rows if s.pid == action[1]), None)
            message, kind = close_one(action[1], target.status_updated_ms if target else None)
            state = replace(state, message=message, message_kind=kind)
        elif action[0] == "gc":
            message, kind = close_stale(snap)
            state = replace(state, message=message, message_kind=kind)
        snap, fetched = take_snapshot(book), time.monotonic()


def run_team(session_id: str, session_name: str) -> int:
    """`ccx team`: open the team view directly; ← or q exits."""
    from .team_view import team_loop
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")

    def loop(screen) -> str:
        curses.curs_set(0)
        screen.timeout(TICK_MS)
        return team_loop(screen, View(screen), session_id, session_name, read_key)

    curses.wrapper(loop)
    return 0


def run_dashboard() -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    action = curses.wrapper(dashboard)
    if not action or action[0] == "quit":
        return 0
    from .cli import launch_argv, require_claude, worktree_argv
    claude = require_claude()
    if action[0] == "launch":
        argv = launch_argv(claude, [], os.getcwd())
    else:
        argv = worktree_argv(claude, [action[1]])
    print(f"\033[2m→ {' '.join(argv[1:]) or 'claude'}\033[0m", flush=True)
    os.execv(claude, argv)
    return 0  # unreachable


__all__ = ["run_dashboard", "handle_key", "UiState", "Snapshot", "bar", "fit", "visible", "saved_cell",
           "session_columns", "sessions_header", "name_cell", "footer_hint"]
