"""Pure logic: processes, Claude sessions, eligibility rules and formatting.

Nothing here prints or prompts. Everything that touches the OS is a small,
replaceable function so the rules can be unit-tested with fixtures.

Claude's per-process registry (~/.claude/sessions/<pid>.json) is an internal
format: every field is optional and malformed files are skipped.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CLAUDE_HOME = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")))
SESSIONS_DIR = CLAUDE_HOME / "sessions"
DEFAULT_IDLE_HOURS = "6"
MIN_IDLE_SECONDS = 60
# A registry record and the live process must agree on start time within this
# window, otherwise the pid was reused and the record is stale.
START_SKEW_MS = 120_000
TERM_GRACE_SECONDS = 5.0

# (label, macOS bundle marker, Linux/basename executables)
TERMINALS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Ghostty", "/Ghostty.app/", ("ghostty",)),
    ("iTerm2", "/iTerm.app/", ()),
    ("Terminal", "/Terminal.app/", ()),
    ("Alacritty", "/Alacritty.app/", ("alacritty",)),
    ("kitty", "/kitty.app/", ("kitty",)),
    ("WezTerm", "/WezTerm.app/", ("wezterm-gui", "wezterm")),
    ("Warp", "/Warp.app/", ("warp", "warp-terminal")),
)


class CockpitError(Exception):
    """A failure with a message fit to show the user."""


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    rss_kb: int
    args: str
    elapsed_s: int | None = None

    def started_ms(self, now_ms: int) -> int | None:
        return None if self.elapsed_s is None else now_ms - self.elapsed_s * 1000


@dataclass(frozen=True)
class Session:
    pid: int
    name: str
    status: str | None
    entrypoint: str
    cwd: str
    status_updated_ms: int | None
    rss_kb: int
    tree_rss_kb: int
    session_id: str | None = None

    @property
    def is_sdk(self) -> bool:
        return self.entrypoint.startswith("sdk")

    def idle_seconds(self, now_ms: int) -> int | None:
        if self.status != "idle" or self.status_updated_ms is None:
            return None
        return max(0, (now_ms - self.status_updated_ms) // 1000)


# ---------------------------------------------------------------- processes

def parse_etime(value: str) -> int | None:
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    try:
        days, _, clock = value.rpartition("-")
        parts = [int(p) for p in clock.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return (int(days) if days else 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_ps(text: str) -> dict[int, Proc]:
    """Rows of `pid ppid rss etime args`; a 4-column legacy form is accepted."""
    procs: dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        elapsed = parse_etime(parts[3]) if len(parts) == 5 else None
        if elapsed is None:
            args = " ".join(parts[3:])
        else:
            args = parts[4]
        procs[pid] = Proc(pid=pid, ppid=ppid, rss_kb=rss, args=args, elapsed_s=elapsed)
    return procs


def read_processes() -> dict[int, Proc]:
    try:
        result = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,ppid=,rss=,etime=,args="],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except FileNotFoundError as exc:
        raise CockpitError("`ps` not found; ccx needs procps (Linux) or BSD ps (macOS)") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise CockpitError(f"could not list processes: {exc}") from exc
    return parse_ps(result.stdout)


def children_index(procs: dict[int, Proc]) -> dict[int, list[int]]:
    index: dict[int, list[int]] = {}
    for proc in procs.values():
        index.setdefault(proc.ppid, []).append(proc.pid)
    return index


def descendants(procs: dict[int, Proc], pid: int) -> set[int]:
    index = children_index(procs)
    found: set[int] = set()
    stack = list(index.get(pid, []))
    while stack:
        child = stack.pop()
        if child in found or child == pid:
            continue
        found.add(child)
        stack.extend(index.get(child, []))
    return found


def tree_rss_kb(procs: dict[int, Proc], pid: int) -> int:
    own = procs[pid].rss_kb if pid in procs else 0
    return own + sum(procs[c].rss_kb for c in descendants(procs, pid))


def ancestors(procs: dict[int, Proc], pid: int) -> set[int]:
    chain: set[int] = set()
    current = procs.get(pid)
    while (current and current.ppid in procs
           and current.ppid not in chain and current.ppid != current.pid):
        chain.add(current.ppid)
        current = procs.get(current.ppid)
    return chain


def executable(proc: Proc) -> str:
    """argv[0]; tolerates spaces in paths of known Claude installs."""
    args = proc.args.strip()
    marker = "/claude/versions/"
    if marker in args:
        head, _, tail = args.partition(marker)
        return head + marker + tail.split(None, 1)[0] if tail else head + marker
    return args.split(None, 1)[0] if args else ""


def is_claude_proc(proc: Proc) -> bool:
    exe = executable(proc)
    return os.path.basename(exe) == "claude" or "/claude/versions/" in exe


def terminal_label(proc: Proc) -> str | None:
    exe = os.path.basename(executable(proc))
    head = proc.args.split(" --", 1)[0]
    for label, bundle, executables in TERMINALS:
        if bundle in head or exe in executables:
            return label
    return None


def terminal_totals_kb(procs: dict[int, Proc]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for proc in procs.values():
        label = terminal_label(proc)
        if label:
            totals[label] = totals.get(label, 0) + proc.rss_kb
    return totals


# ---------------------------------------------------------------- sessions

def optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def same_process(record: dict, proc: Proc, now_ms: int) -> bool:
    """True unless the evidence says the pid now belongs to a different process."""
    started = proc.started_ms(now_ms)
    if started is None:
        return True  # no etime available: fall back to the executable check alone
    recorded = optional_int(record.get("startedAt"))
    if recorded is not None:
        return abs(started - recorded) <= START_SKEW_MS
    updated = optional_int(record.get("statusUpdatedAt"))
    return updated is None or started <= updated + START_SKEW_MS


def session_from_record(record: dict, procs: dict[int, Proc], now_ms: int) -> Session | None:
    pid = optional_int(record.get("pid"))
    if pid is None or pid not in procs:
        return None
    proc = procs[pid]
    if not is_claude_proc(proc) or not same_process(record, proc, now_ms):
        return None  # dead, or the pid was reused by another program
    status = record.get("status")
    return Session(
        pid=pid,
        name=str(record.get("name") or f"pid-{pid}"),
        status=status if isinstance(status, str) else None,
        entrypoint=str(record.get("entrypoint") or "unknown"),
        cwd=str(record.get("cwd") or "?"),
        status_updated_ms=optional_int(record.get("statusUpdatedAt")),
        rss_kb=proc.rss_kb,
        tree_rss_kb=tree_rss_kb(procs, pid),
        session_id=str(record["sessionId"]) if record.get("sessionId") else None,
    )


def load_sessions(directory: Path, procs: dict[int, Proc], now_ms: int | None = None) -> list[Session]:
    if not directory.is_dir():
        return []
    now = now_ms if now_ms is not None else current_ms()
    sessions: list[Session] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            session = session_from_record(record, procs, now)
            if session:
                sessions.append(session)
    return sessions


def gc_candidates(
    sessions: Iterable[Session], now_ms: int, min_idle_s: int, protected: set[int]
) -> list[Session]:
    picked = []
    for s in sessions:
        idle = s.idle_seconds(now_ms)
        if s.is_sdk or s.pid in protected or idle is None or idle < min_idle_s:
            continue
        picked.append(s)
    return sorted(picked, key=lambda s: s.idle_seconds(now_ms) or 0, reverse=True)


def still_eligible(
    original: Session, fresh: Iterable[Session], now_ms: int, min_idle_s: int, protected: set[int]
) -> bool:
    """Re-check right before closing: same pid, untouched since listing, still idle long enough."""
    for s in gc_candidates(fresh, now_ms, min_idle_s, protected):
        if s.pid == original.pid:
            return s.status_updated_ms == original.status_updated_ms
    return False


def protected_pids(procs: dict[int, Proc]) -> set[int]:
    me = os.getpid()
    guarded = {me} | ancestors(procs, me)
    claude_pid = optional_int(os.environ.get("CLAUDE_PID"))
    if claude_pid:
        guarded.add(claude_pid)
    return guarded


def terminate(pid: int, grace_s: float = TERM_GRACE_SECONDS) -> bool:
    """SIGTERM and wait. Never escalates to SIGKILL: Claude saves on TERM."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True  # pid now belongs to another user: ours is gone
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------- formatting

def current_ms() -> int:
    return int(time.time() * 1000)


def human_duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def human_mb(kb: int) -> str:
    mb = kb / 1024
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def parse_hours(value: str) -> int:
    try:
        hours = float(value)
    except ValueError as exc:
        raise ValueError(f"not a number of hours: {value!r}") from exc
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError(f"hours must be > 0, got {value!r}")
    seconds = int(hours * 3600)
    if seconds < MIN_IDLE_SECONDS:
        raise ValueError(f"idle threshold must be at least {MIN_IDLE_SECONDS}s, got {value!r}h")
    return seconds


def session_name(cwd: str, branch: str | None) -> str:
    base = os.path.basename(cwd.rstrip("/")) or "root"
    return f"{base}@{branch}" if branch else base


def short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def system_memory_kb() -> int | None:
    """Physical RAM, for gauges. None when unknown."""
    try:
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and size > 0:
            return pages * size // 1024
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=2, check=False)
        return int(out.stdout.strip()) // 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
