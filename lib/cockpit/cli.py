"""ccx commands. `ccx` with no arguments opens the dashboard (see tui.py).

  ccx                      interactive cockpit dashboard
  ccx [claude args...]     start Claude here, named <repo>@<branch>
  ccx run [claude args...] same, explicitly (use when your prompt is a ccx word)
  ccx wt NAME [args...]    start Claude in a new git worktree NAME
  ccx team [NAME|PID]      live view of the agents working inside a session
  ccx ls [--all] [--json]  running Claude sessions: state, idle time, RAM, cost, saved
  ccx savings [--days N] [--json]
                           what delegating to agents saved vs doing the same work
                           in the main session, over the last N days (default 7)
  ccx mem [--json]         RAM breakdown: terminals, sessions, helpers
  ccx gc [--idle H] [--yes] [--dry-run]
                           close sessions idle >= H hours (default 6); they stay
                           resumable with `claude --resume`
  ccx doctor [--json]      check the cockpit setup end to end, incl. sessions that
                           still run old plugin hooks (⟳) and how to restart them
  ccx setup [--source SRC] [--no-plugin] [--no-ghostty]
                           install or update the Claude Code plugin (release
                           channel) and the Ghostty profile; safe to re-run
  ccx version | --version
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import VERSION
from .pricing import context_level, fmt_signed_usd, fmt_usd
from .savings import SessionSavings
from .team import SessionUsage, UsageBook
from .installer import run_setup
from .doctor import HOOKS_HINT, HOOKS_MARK, STALE, UNKNOWN, hooks_states, resolve_install
from .tiers import OVERRIDE_VARS
from .core import (
    CLAUDE_HOME, DEFAULT_IDLE_HOURS, SESSIONS_DIR, CockpitError, duplicate_of, Session, current_ms, gc_candidates,
    human_duration, human_mb, is_claude_proc, load_sessions, parse_hours, protected_pids,
    read_processes, session_name, short_path, still_eligible, terminal_totals_kb, terminate,
    tree_rss_kb,
)

PLUGIN_ID = "orchestrator@claude-cockpit"
NO_AUTO_NAME = {"-n", "--name", "-r", "--resume", "-c", "--continue", "-p", "--print"}


def color(text: str, code: str, stream=None) -> str:
    stream = stream or sys.stdout
    if not stream.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


# ---------------------------------------------------------------- launching

def git_branch(cwd: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = out.stdout.strip()
    return branch if out.returncode == 0 and branch and branch != "HEAD" else None


def require_claude() -> str:
    path = shutil.which("claude")
    if not path:
        raise CockpitError("`claude` is not on PATH. Install: https://code.claude.com/docs/en/setup")
    return path


def launch_argv(claude: str, args: list[str], cwd: str) -> list[str]:
    argv = [claude, *args]
    if not NO_AUTO_NAME.intersection(a.split("=", 1)[0] for a in args):
        argv[1:1] = ["-n", session_name(cwd, git_branch(cwd))]
    return argv


def cmd_launch(args: list[str]) -> int:
    claude = require_claude()
    os.execv(claude, launch_argv(claude, args, os.getcwd()))
    return 0  # unreachable


def worktree_argv(claude: str, args: list[str]) -> list[str]:
    if not args or args[0].startswith("-"):
        raise CockpitError("usage: ccx wt NAME [claude args...]")
    return [claude, "-w", args[0], "-n", args[0], *args[1:]]


def cmd_worktree(args: list[str]) -> int:
    claude = require_claude()
    os.execv(claude, worktree_argv(claude, args))
    return 0  # unreachable


# ---------------------------------------------------------------- listing

SAVED_W = 9  # same cell as the dashboard's SAVED column


def saved_text(savings: SessionSavings | None) -> str:
    """Signed saving with the dashboard's markers: ~ partial, ! would have compacted inline, - no agents."""
    if savings is None or not savings.runs:
        return "-"
    return (fmt_signed_usd(savings.saved_usd) + ("~" if savings.partial else "")
            + ("!" if savings.would_compact else ""))


def usage_cells(usage: SessionUsage | None, savings: SessionSavings | None = None) -> str:
    saved = saved_text(savings)
    saved_cell = f"{saved:>{SAVED_W}}"
    if saved != "-":
        saved_cell = color(saved_cell, "33" if savings.saved_usd < 0 else "32")
    if usage is None:
        return f"{'-':>5} {'-':>8} {saved_cell}"
    pct = f"{usage.pct:>4.0f}%"
    level = context_level(usage.pct)
    if level != "ok":
        pct = color(pct, "31" if level == "critical" else "33")
    cost = ("≥" if usage.partial else "") + fmt_usd(usage.cost_usd if usage.known else None)
    return f"{pct} {cost:>8} {saved_cell}"


def name_cell(s: Session, hooks_stale: bool = False, width: int = 28) -> str:
    """NAME, cut to width; `⟳` at its end marks a process running hooks older than hooks.json."""
    name = s.name + (" [sdk]" if s.is_sdk else "")
    return f"{name[:width - 2]} {HOOKS_MARK}" if hooks_stale else name[:width]


def session_row(s: Session, now: int, usage: SessionUsage | None = None,
                savings: SessionSavings | None = None, hooks_stale: bool = False) -> str:
    state = s.status or "?"
    state_txt = color(f"{state:<7}", {"busy": "32", "waiting": "1;33"}.get(state, "2"))
    idle = s.idle_seconds(now)
    idle_txt = f"{human_duration(idle):>7}"
    if idle and idle >= parse_hours(DEFAULT_IDLE_HOURS):
        idle_txt = color(idle_txt, "33")
    name = name_cell(s, hooks_stale)
    return (f"{s.pid:>7}  {state_txt} {idle_txt} {human_mb(s.rss_kb):>8} "
            f"{human_mb(s.tree_rss_kb):>8} {usage_cells(usage, savings)}  {name:<28} {short_path(s.cwd)}")


SESSIONS_HEADER = (f"{'PID':>7}  {'STATE':<7} {'IDLE':>7} {'RAM':>8} {'+KIDS':>8} {'CTX':>5} {'COST':>8} "
                   f"{'SAVED':>{SAVED_W}}  {'NAME':<28} CWD")


def render_sessions(sessions: list[Session], now: int, usage: dict[int, SessionUsage] | None = None,
                    savings: dict[int, SessionSavings] | None = None,
                    hooks_stale: frozenset[int] = frozenset()) -> None:
    usage, savings = usage or {}, savings or {}
    print(color(SESSIONS_HEADER, "1"))
    dupes = duplicate_of(sessions)
    for s in sorted(sessions, key=lambda x: x.idle_seconds(now) or -1, reverse=True):
        note = color(f"  ⧉ same chat as {dupes[s.pid]}", "33") if s.pid in dupes else ""
        print(session_row(s, now, usage.get(s.pid), savings.get(s.pid), s.pid in hooks_stale) + note)


def usage_by_pid(sessions: list[Session], book: UsageBook | None = None) -> dict[int, SessionUsage]:
    book, found = book or UsageBook(), {}
    for s in sessions:
        usage = book.usage(s.session_id) if s.session_id else None
        if usage:
            found[s.pid] = usage
    return found


def savings_by_pid(sessions: list[Session], book: UsageBook) -> dict[int, SessionSavings]:
    found = {}
    for s in sessions:
        saved = book.savings(s.session_id) if s.session_id else None
        if saved:
            found[s.pid] = saved
    return found


def savings_json(savings: SessionSavings | None) -> dict:
    """`ccx ls --json` fields; saved_usd is None when there is no transcript, 0.0 without agents."""
    if savings is None:
        return {"saved_usd": None, "saved_partial": False, "would_compact": False}
    return {"saved_usd": round(savings.saved_usd, 6), "saved_partial": savings.partial,
            "would_compact": savings.would_compact}


def check_flags(args: list[str], allowed: set[str], command: str) -> None:
    unknown = [a for a in args if a not in allowed]
    if unknown:
        raise CockpitError(f"ccx {command}: unknown option {unknown[0]!r} (allowed: {', '.join(sorted(allowed))})")


def hooks_stale_json(state: str | None) -> bool | None:
    """`ccx ls --json` hooks_stale: true/false, or null when unknown (SDK children, no start time)."""
    return None if state in {None, UNKNOWN} else state == STALE


def cmd_ls(args: list[str]) -> int:
    check_flags(args, {"--all", "--json"}, "ls")
    procs = read_processes()
    now = current_ms()
    sessions = load_sessions(SESSIONS_DIR, procs, now)
    hooks = hooks_states(procs, sessions, resolve_install(CLAUDE_HOME).stale_after_ms, now)
    stale = frozenset(pid for pid, state in hooks.items() if state == STALE)
    if "--all" not in args:
        sessions = [s for s in sessions if not s.is_sdk]
    book = UsageBook()
    usage, savings = usage_by_pid(sessions, book), savings_by_pid(sessions, book)
    if "--json" in args:
        rows = [dict(asdict(s), idle_seconds=s.idle_seconds(now), is_sdk=s.is_sdk,
                     usage=(dict(asdict(usage[s.pid]), pct=round(usage[s.pid].pct, 1)) if s.pid in usage else None),
                     hooks_stale=hooks_stale_json(hooks.get(s.pid)), **savings_json(savings.get(s.pid)))
                for s in sessions]
        print(json.dumps(rows, indent=2))
        return 0
    if not sessions:
        print("No running Claude sessions found." + ("" if "--all" in args else " (try --all)"))
        return 0
    render_sessions(sessions, now, usage, savings, stale)
    total = sum(s.tree_rss_kb for s in sessions)
    dupes = duplicate_of(sessions)
    spend = sum(u.cost_usd for pid, u in usage.items() if pid not in dupes)
    print(color(f"\n{len(sessions)} sessions · {human_mb(total)} incl. child processes · "
                f"≈{fmt_usd(spend)} API-equivalent spend", "2"))
    if any(s.pid in stale for s in sessions):
        print(color(HOOKS_HINT, "33"))
    return 0


def parse_savings_args(args: list[str]) -> tuple[float, bool]:
    """(days, as_json) from `ccx savings [--days N] [--json]`; raises CockpitError like the other commands."""
    days, as_json, rest = 7.0, False, list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--json":
            as_json = True
        elif arg == "--days" or arg.startswith("--days="):
            value = arg.split("=", 1)[1] if "=" in arg else (rest.pop(0) if rest else "")
            try:
                days = float(value)
            except ValueError:
                days = 0.0
            if not days > 0 or days == float("inf"):
                raise CockpitError(f"ccx savings: --days needs a positive number of days, got {value!r}")
        else:
            raise CockpitError(f"ccx savings: unknown option {arg!r} (allowed: --days N, --json)")
    return days, as_json


def cmd_savings(args: list[str]) -> int:
    days, as_json = parse_savings_args(args)
    from .savings_report import build_report, render_text, report_json
    report = build_report(days=days)
    print(json.dumps(report_json(report), indent=2) if as_json else render_text(report), end="\n" if as_json else "")
    return 0


def memory_report(procs: dict, sessions: list[Session]) -> dict[str, int]:
    known = {s.pid for s in sessions}
    stray = [p for p in procs.values() if is_claude_proc(p) and p.pid not in known]
    report = {f"terminal: {k}": v for k, v in terminal_totals_kb(procs).items()}
    report["claude: interactive sessions"] = sum(s.tree_rss_kb for s in sessions if not s.is_sdk)
    report["claude: sdk/background sessions"] = sum(s.tree_rss_kb for s in sessions if s.is_sdk)
    report["claude: unregistered processes"] = sum(tree_rss_kb(procs, p.pid) for p in stray)
    return report


def cmd_mem(args: list[str]) -> int:
    check_flags(args, {"--json"}, "mem")
    procs = read_processes()
    now = current_ms()
    sessions = load_sessions(SESSIONS_DIR, procs, now)
    report = memory_report(procs, sessions)
    if "--json" in args:
        print(json.dumps({k: v * 1024 for k, v in report.items()}, indent=2))
        return 0
    width = max(len(k) for k in report)
    for key, kb in sorted(report.items(), key=lambda kv: -kv[1]):
        print(f"  {key:<{width}}  {human_mb(kb):>9}")
    print(color(f"  {'total':<{width}}  {human_mb(sum(report.values())):>9}", "1"))
    reclaim = gc_candidates(sessions, now, parse_hours(DEFAULT_IDLE_HOURS), protected_pids(procs))
    if reclaim:
        freed = human_mb(sum(s.tree_rss_kb for s in reclaim))
        print(color(f"\n  {len(reclaim)} sessions idle > {DEFAULT_IDLE_HOURS}h hold {freed}: run `ccx gc`", "33"))
    return 0


# ---------------------------------------------------------------- gc

def parse_gc_args(args: list[str]) -> tuple[int, bool, bool]:
    idle, assume_yes, dry_run = DEFAULT_IDLE_HOURS, False, False
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--yes":
            assume_yes = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--idle":
            if not rest:
                raise ValueError("--idle needs a number of hours")
            idle = rest.pop(0)
        elif arg.startswith("--idle="):
            idle = arg.split("=", 1)[1]
        else:
            raise ValueError(f"unknown option {arg!r} (allowed: --idle H, --yes, --dry-run)")
    return parse_hours(idle), assume_yes, dry_run


def close_sessions(picked: list[Session], min_idle: int, report=print) -> int:
    """Close each session only if it is still exactly as listed. Returns failures."""
    failures = 0
    for s in picked:
        procs = read_processes()
        fresh = load_sessions(SESSIONS_DIR, procs)
        if not still_eligible(s, fresh, current_ms(), min_idle, protected_pids(procs)):
            report(f"  skip   {s.pid} {s.name}: used since listing")
            continue
        ok = terminate(s.pid)
        failures += 0 if ok else 1
        report(f"  {'closed' if ok else 'still running'}  {s.pid} {s.name}  ({human_mb(s.tree_rss_kb)})")
    return failures


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise CockpitError("not a terminal; re-run with --yes to confirm")
    try:
        return input(question).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def cmd_gc(args: list[str]) -> int:
    try:
        min_idle, assume_yes, dry_run = parse_gc_args(args)
    except ValueError as exc:
        raise CockpitError(f"ccx gc: {exc}") from exc
    procs = read_processes()
    now = current_ms()
    picked = gc_candidates(load_sessions(SESSIONS_DIR, procs, now), now, min_idle, protected_pids(procs))
    if not picked:
        print(f"Nothing to close: no interactive session idle ≥ {human_duration(min_idle)}.")
        return 0
    render_sessions(picked, now)
    freed = human_mb(sum(s.tree_rss_kb for s in picked))
    print(f"\n{len(picked)} idle sessions hold {freed}. Each stays resumable with `claude --resume`.")
    if dry_run:
        return 0
    if not assume_yes and not confirm("Close them? [y/N] "):
        print("Left them running.")
        return 0
    return 1 if close_sessions(picked, min_idle) else 0


# ---------------------------------------------------------------- doctor
# The checks live in doctor_cmd.py (stale hooks: doctor.py); these helpers are shared.

def run_quiet(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def settings_env(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def tier_overrides(environ: dict[str, str], settings: dict[str, str]) -> list[str]:
    """Env vars that would flatten every tier to one model or effort."""
    return [f"{name}={(environ.get(name) or settings.get(name))}"
            for name in OVERRIDE_VARS if environ.get(name) or settings.get(name)]


def cmd_doctor(args: list[str]) -> int:
    from .doctor_cmd import cmd_doctor as run_doctor  # imports cli: load lazily
    return run_doctor(args)


# ---------------------------------------------------------------- entry

def cmd_dashboard(_args: list[str]) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return cmd_ls([])
    from .tui import run_dashboard  # curses only loads when needed
    return run_dashboard()


def pick_session(sessions: list[Session], query: str | None, now: int) -> Session | None:
    """By pid, else by name substring, else the most recently active interactive session."""
    candidates = [s for s in sessions if s.session_id and not s.is_sdk]
    if query and query.isdigit():
        return next((s for s in sessions if s.pid == int(query)), None)
    if query:
        needle = query.lower()
        return next((s for s in candidates if needle in s.name.lower()), None)
    ranked = sorted(candidates, key=lambda s: (s.status != "busy", s.idle_seconds(now) or 0))
    return ranked[0] if ranked else None


def cmd_team(args: list[str]) -> int:
    if len(args) > 1 or (args and args[0].startswith("-")):
        raise CockpitError("usage: ccx team [session name or pid]")
    now = current_ms()
    target = pick_session(load_sessions(SESSIONS_DIR, read_processes(), now), args[0] if args else None, now)
    if not target or not target.session_id:
        raise CockpitError("no matching Claude session; see `ccx ls`")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CockpitError("the team view needs a terminal")
    from .tui import run_team
    return run_team(target.session_id, target.name)


COMMANDS = {
    "ls": cmd_ls, "mem": cmd_mem, "gc": cmd_gc, "doctor": cmd_doctor, "wt": cmd_worktree,
    "run": cmd_launch, "ui": cmd_dashboard, "team": cmd_team, "setup": run_setup, "savings": cmd_savings,
}


def dispatch(argv: list[str]) -> int:
    if not argv:
        return cmd_dashboard([])
    head = argv[0]
    if head in {"-h", "--help", "help"}:
        print(__doc__.strip())
        return 0
    if head in {"version", "--version", "-V"}:
        print(f"ccx {VERSION}")
        return 0
    if head in COMMANDS:
        return COMMANDS[head](argv[1:])
    return cmd_launch(argv)


def main(argv: list[str]) -> int:
    try:
        return dispatch(argv)
    except CockpitError as exc:
        print(color(f"ccx: {exc}", "31", sys.stderr), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
