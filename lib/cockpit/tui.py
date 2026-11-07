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
from .tiers import COLOURS, load_tiers
from .core import (
    DEFAULT_IDLE_HOURS, SESSIONS_DIR, CockpitError, Proc, Session, current_ms, gc_candidates,
    human_duration, human_mb, load_sessions, parse_hours, protected_pids, read_processes,
    short_path, system_memory_kb, terminal_totals_kb, terminate,
)

REFRESH_SECONDS = 1.5
TICK_MS = 250  # 4 fps is enough for the busy pulse and keeps CPU near zero
STALE_SECONDS = parse_hours(DEFAULT_IDLE_HOURS)
WORKTREE_NAME = re.compile(r"[A-Za-z0-9._/-]")
BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"
PULSE = "◐◓◑◒"

KEYS_MAIN = (("⏎", "claude here"), ("w", "worktree"), ("x", "close"), ("g", "clean idle"),
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

    @property
    def claude_kb(self) -> int:
        return sum(s.tree_rss_kb for s in self.sessions)

    def stale(self) -> list[Session]:
        return gc_candidates(self.sessions, self.now, STALE_SECONDS, set(self.protected))


Action = tuple  # ("launch",) | ("worktree", name) | ("close", pid) | ("gc",) | ("doctor",) | ("quit",)


def visible(snapshot: Snapshot, state: UiState) -> list[Session]:
    """Working sessions first, then most recently used, stale ones sink to the bottom."""
    rows = [s for s in snapshot.sessions if state.show_sdk or not s.is_sdk]
    return sorted(rows, key=lambda s: (s.status != "busy", s.idle_seconds(snapshot.now) or 0, s.name))


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


def state_glyph(session: Session, now: int, tick: int) -> tuple[str, str]:
    if session.status == "busy":
        return PULSE[tick % len(PULSE)], "busy"
    idle = session.idle_seconds(now)
    if idle is None:
        return "?", "dim"
    return ("◌", "stale") if idle >= STALE_SECONDS else ("○", "idle")


# ---------------------------------------------------------------- data

def take_snapshot() -> Snapshot:
    try:
        procs: dict[int, Proc] = read_processes()
    except CockpitError as exc:
        return Snapshot(now=current_ms(), error=str(exc))
    now = current_ms()
    return Snapshot(
        sessions=tuple(load_sessions(SESSIONS_DIR, procs, now)),
        now=now,
        protected=frozenset(protected_pids(procs)),
        terminals=terminal_totals_kb(procs),
        system_kb=system_memory_kb(),
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
        self._footer(state, height, width)
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
        stale = snap.stale()
        if stale:
            held = human_mb(sum(s.tree_rss_kb for s in stale))
            self.put(3, 2, f"⚑ {len(stale)} sessions idle ≥ {DEFAULT_IDLE_HOURS}h hold {held}. Press g to reclaim.",
                     self.attr("stale"))
        if snap.error:
            self.put(3, 2, f"✗ {snap.error}", self.attr("error"))
        return 4

    def _sessions(self, y: int, snap: Snapshot, state: UiState, rows: list[Session],
                  tick: int, height: int, width: int) -> int:
        hidden = sum(1 for s in snap.sessions if s.is_sdk) if not state.show_sdk else 0
        title = f"SESSIONS {len(rows)}" + (f"   +{hidden} background (a)" if hidden else "")
        self.put(y, 2, title, self.attr("accent", curses.A_BOLD))
        y += 1
        name_w = max(16, min(30, width - 64))
        dir_w = max(10, width - name_w - 50)
        header = f"   {'NAME':<{name_w}} {'DIR':<{dir_w}} {'STATE':<6} {'IDLE':>7} {'RAM':>8}  "
        self.put(y, 2, header, self.attr("dim"))
        y += 1
        budget = height - y - (11 if state.show_tiers else 3)
        if not rows:
            self.put(y, 4, "No Claude sessions running. Press ⏎ to start one here.", self.attr("dim"))
            return y + 1
        biggest = max(s.tree_rss_kb for s in rows) or 1
        start = max(0, state.selected - max(1, budget) + 1)
        for index, session in enumerate(rows[start:start + max(1, budget)], start=start):
            self._session_row(y, session, snap, index == state.selected, tick, name_w, dir_w, biggest)
            y += 1
        return y

    def _session_row(self, y: int, s: Session, snap: Snapshot, selected: bool, tick: int,
                     name_w: int, dir_w: int, biggest: int) -> None:
        glyph, tone = state_glyph(s, snap.now, tick)
        sel = curses.A_REVERSE if selected else 0
        marker = "▸" if selected else " "
        x = self.put(y, 1, f" {marker} ", self.attr("accent", curses.A_BOLD))
        x = self.put(y, x, glyph + " ", self.attr(tone, curses.A_BOLD))
        you = "  ◀ you" if s.pid in snap.protected else ""
        name = fit(s.name + (" [bg]" if s.is_sdk else ""), name_w - 2)
        x = self.put(y, x, f"{name:<{name_w - 2}} ", sel | (curses.A_BOLD if selected else 0))
        x = self.put(y, x, f"{fit(short_path(s.cwd), dir_w):<{dir_w}} ", self.attr("dim") | sel)
        x = self.put(y, x, f"{(s.status or '?'):<6} ", self.attr(tone) | sel)
        x = self.put(y, x, f"{human_duration(s.idle_seconds(snap.now)):>7} ", self.attr(tone) | sel)
        x = self.put(y, x, f"{human_mb(s.tree_rss_kb):>8} ", sel)
        x = self.put(y, x, " " + bar(s.tree_rss_kb / biggest, 8), self.attr("bar"))
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

    def _footer(self, state: UiState, height: int, width: int) -> None:
        if state.mode == "input-wt":
            prompt = "worktree name ⟩ "
            x = self.put(height - 2, 2, prompt, self.attr("accent", curses.A_BOLD))
            self.put(height - 2, x, state.buffer + "█", curses.A_BOLD)
            self.put(height - 1, 2, "⏎ start Claude in a new git worktree   Esc cancel", self.attr("dim"))
            return
        if state.message:
            self.put(height - 2, 2, fit(state.message, width - 4), self.attr(state.message_kind, curses.A_BOLD))
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
    state, snap, fetched, tick = UiState(), take_snapshot(), time.monotonic(), 0
    while True:
        if time.monotonic() - fetched >= REFRESH_SECONDS:
            snap, fetched = take_snapshot(), time.monotonic()
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
        if action[0] == "doctor":
            from .cli import cmd_doctor
            pause_for(screen, lambda: cmd_doctor([]))
        elif action[0] == "close":
            target = next((s for s in rows if s.pid == action[1]), None)
            message, kind = close_one(action[1], target.status_updated_ms if target else None)
            state = replace(state, message=message, message_kind=kind)
        elif action[0] == "gc":
            message, kind = close_stale(snap)
            state = replace(state, message=message, message_kind=kind)
        snap, fetched = take_snapshot(), time.monotonic()


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


__all__ = ["run_dashboard", "handle_key", "UiState", "Snapshot", "bar", "fit", "visible"]
