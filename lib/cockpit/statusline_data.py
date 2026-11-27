"""Data sources for `ccx statusline`: git, transcripts, other sessions, the usage alert.

Every render is a fresh, short-lived process, so whatever is worth remembering
between renders (git results, transcript byte offsets, running totals, alert
markers) lives in a private state directory:

  $TMPDIR/claude-cockpit-<uid>/statusline/      (COCKPIT_STATE_DIR overrides)

Each reader returns None (or an empty result) on any problem; the renderer then
leaves its segment out. Nothing here talks to the network.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

from .pricing import request_cost

# Shared with the dashboard, but copied rather than imported: importing team.py and
# core.py would add ~30ms to every render (dataclasses, parallel, subprocess).
# tests/test_statusline.py pins each one to its original.
CLAUDE_HOME = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")))   # core.CLAUDE_HOME
SESSIONS_DIR = CLAUDE_HOME / "sessions"                                                    # core.SESSIONS_DIR
ASSISTANT_RE = re.compile(rb'"type":\s*"assistant"')                                       # team.ASSISTANT_RE
FIRST_READ_MAX_BYTES = 64 * 1024 * 1024                                                    # team.FIRST_READ_MAX_BYTES
STALL_SECONDS = 300                                                                        # team.STALL_SECONDS


def optional_int(value: object) -> int | None:
    """Same contract as core.optional_int."""
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_ts(value: object) -> float | None:
    """Same contract as team.parse_ts."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_meta(path: Path) -> dict:
    """Same contract as team.read_meta."""
    return load_json(path)


STATE_VERSION = 1
GIT_TTL_S = 10.0
GIT_TIMEOUT_S = 0.3
MAIN_FIRST_READ_MAX_BYTES = 4 * 1024 * 1024   # the cache segment only needs recent calls
RECENT_CALLS = 10
AGENT_READ_BUDGET_S = 0.15                    # per render; the rest is read on the next one
TTL_SECONDS = {"5m": 300, "1h": 3600}
SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
DEFAULT_ALERT_PCT = 60.0
SAME_WINDOW_SLACK_S = 3600   # resets_at jitters a little between responses
MARKER_MAX_AGE_S = 2 * 86400
STATE_MAX_AGE_S = 7 * 86400


# ---------------------------------------------------------------- state files

def state_dir(environ=os.environ) -> Path | None:
    """A directory only this user can write, or None (caching is then skipped)."""
    override = environ.get("COCKPIT_STATE_DIR")
    tmp = environ.get("TMPDIR") or "/tmp"
    root = Path(override) if override else Path(tmp) / f"claude-cockpit-{os.getuid()}"
    path = root / "statusline"
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = os.lstat(root)
    except OSError:
        return None
    if info.st_uid != os.getuid() or os.path.islink(root):
        return None   # someone else's directory in a shared /tmp
    return path


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data: dict) -> None:
    """Atomic: a render that Claude Code cancels mid-write leaves the old file intact."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def prune_state(directory: Path, max_age_s: float = STATE_MAX_AGE_S) -> int:
    """Delete session and git state untouched for MAX_AGE_S; runs at most once a day."""
    stamp = directory / ".pruned"
    now = time.time()
    try:
        if stamp.exists() and now - stamp.stat().st_mtime < 86400:
            return 0
        stamp.touch()
        removed = 0
        for path in [*directory.glob("session-*.json"), *directory.glob("git-*.json")]:
            if now - path.stat().st_mtime > max_age_s:
                path.unlink()
                removed += 1
    except OSError:
        return 0
    return removed


# ---------------------------------------------------------------- git

def parse_porcelain(text: str) -> dict:
    """`git status --porcelain=v2 --branch` -> {branch, oid, ahead, behind, dirty}."""
    info = {"branch": None, "oid": None, "ahead": 0, "behind": 0, "dirty": 0}
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
            info["branch"] = None if head == "(detached)" else head
        elif line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):].strip()
            info["oid"] = None if oid == "(initial)" else oid[:7]
        elif line.startswith("# branch.ab "):
            for part in line.split()[2:]:
                value = optional_int(part[1:])
                if value is not None:
                    info["ahead" if part.startswith("+") else "behind"] = abs(value)
        elif line and not line.startswith("#"):
            info["dirty"] += 1
    return info


def run_git_status(cwd: str, timeout: float = GIT_TIMEOUT_S) -> tuple[str, dict | None]:
    """("ok", info) | ("none", None) outside a repo | ("timeout"/"error", None)."""
    import subprocess  # only when the cache is stale: keeps the common render fast
    argv = ["git", "--no-optional-locks", "-C", cwd, "status", "--porcelain=v2", "--branch"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                             stdin=subprocess.DEVNULL, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except subprocess.TimeoutExpired:
        return "timeout", None
    except OSError:
        return "error", None
    if out.returncode != 0:
        return "none", None
    return "ok", parse_porcelain(out.stdout)


def head_branch(cwd: str) -> str | None:
    """Branch name from .git/HEAD without running git (the fallback when git is slow)."""
    here = Path(cwd)
    for folder in (here, *here.parents):
        dotgit = folder / ".git"
        try:
            if dotgit.is_file():
                text = dotgit.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir:"):
                    return None
                gitdir = Path(text[len("gitdir:"):].strip())
                dotgit = gitdir if gitdir.is_absolute() else folder / gitdir
            elif not dotgit.is_dir():
                continue
            head = (dotgit / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        prefix = "ref: refs/heads/"
        return head[len(prefix):] if head.startswith(prefix) else None
    return None


def git_info(cwd: str, directory: Path | None, now: float, runner=run_git_status) -> dict | None:
    """Git facts for CWD, cached per directory for GIT_TTL_S (failures too)."""
    if not cwd or not os.path.isdir(cwd):
        return None
    cache = directory / f"git-{zlib.crc32(cwd.encode()):08x}.json" if directory else None
    hit = load_json(cache) if cache else {}
    if hit.get("cwd") == cwd and isinstance(hit.get("t"), (int, float)) and 0 <= now - hit["t"] < GIT_TTL_S:
        status, info = hit.get("status"), hit.get("info")
    else:
        status, info = runner(cwd)
        if cache:
            save_json(cache, {"cwd": cwd, "t": now, "status": status, "info": info})
    if status == "ok" and isinstance(info, dict):
        return info
    if status in {"timeout", "error"}:
        branch = head_branch(cwd)
        return {"branch": branch, "oid": None, "ahead": 0, "behind": 0, "dirty": None} if branch else None
    return None


# ---------------------------------------------------------------- transcripts

def read_new_lines(path: Path, offset: int, first_cap: int) -> tuple:
    """Complete lines appended since OFFSET: (lines, new offset, skipped head, mtime, restarted).

    Only whole lines are consumed, so the saved offset never splits a record."""
    try:
        info = path.stat()
    except OSError:
        return [], offset, False, 0, False
    size, mtime = info.st_size, int(info.st_mtime)
    restarted = size < offset          # rewritten: start over
    if restarted:
        offset = 0
    if size == offset:
        return [], offset, False, mtime, restarted
    skipped = offset == 0 and size > first_cap
    start = size - first_cap if skipped else offset
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(size - start)
    except OSError:
        return [], offset, False, mtime, restarted
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset, False, mtime, restarted
    lines = chunk[:end].split(b"\n")
    if skipped and lines:
        lines.pop(0)           # probably cut mid-line
    return lines, start + end + 1, skipped, mtime, restarted


def assistant_records(lines: list[bytes]):
    for line in lines:
        if not ASSISTANT_RE.search(line):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            yield record, message


def _num(usage: dict, key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def call_ttl(usage: dict) -> str | None:
    """'1h' or '5m' when this call wrote cache, else None."""
    detail = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    if _num(detail, "ephemeral_1h_input_tokens") > 0:
        return "1h"
    if _num(detail, "ephemeral_5m_input_tokens") > 0 or _num(usage, "cache_creation_input_tokens") > 0:
        return "5m"
    return None


def update_main(state: dict, path: Path) -> dict:
    """Fold new main-transcript calls into STATE: recent cache reads and the last call's TTL."""
    lines, offset, _, _, restarted = read_new_lines(
        path, int(state.get("offset") or 0) if state.get("path") == str(path) else 0, MAIN_FIRST_READ_MAX_BYTES)
    if restarted or state.get("path") != str(path):
        state = {}
    recent = [r for r in state.get("recent") or [] if isinstance(r, list) and len(r) == 3]
    last_ts, ttl = state.get("last_ts"), state.get("ttl")
    for record, message in assistant_records(lines):
        usage = message["usage"]
        total = _num(usage, "input_tokens") + _num(usage, "cache_creation_input_tokens") + \
            _num(usage, "cache_read_input_tokens")
        if total == 0:
            continue   # "<synthetic>" placeholders
        key = str(message.get("id") or record.get("requestId") or "")
        entry = [key, _num(usage, "cache_read_input_tokens"), total]
        recent = [r for r in recent if not key or r[0] != key] + [entry]
        ts = parse_ts(record.get("timestamp"))
        last_ts = ts if ts is not None else last_ts
        ttl = call_ttl(usage) or ttl
    return {"path": str(path), "offset": offset, "recent": recent[-RECENT_CALLS:], "last_ts": last_ts, "ttl": ttl}


def cache_from_main(state: dict) -> dict | None:
    recent = state.get("recent") or []
    total = sum(r[2] for r in recent)
    if not total:
        return None
    expires = state["last_ts"] + TTL_SECONDS[state["ttl"]] if state.get("last_ts") and state.get("ttl") else None
    return {"hit_pct": 100.0 * sum(r[1] for r in recent) / total, "expires_at": expires, "source": "recent"}


def update_agent(state: dict, path: Path) -> dict:
    """Running API-equivalent cost of one agent transcript, read incrementally."""
    lines, offset, skipped, mtime, restarted = read_new_lines(path, int(state.get("offset") or 0),
                                                              FIRST_READ_MAX_BYTES)
    if restarted:
        state = {"finished": state.get("finished")}
    cost, known, model = float(state.get("cost") or 0.0), bool(state.get("known", True)), str(state.get("model") or "")
    last_id, last_cost, last_out = state.get("last_id") or "", float(state.get("last_cost") or 0.0), \
        int(state.get("last_out") or 0)
    for record, message in assistant_records(lines):
        if isinstance(message.get("model"), str) and message["model"].startswith("claude-"):
            model = message["model"]
        usage = message["usage"]
        rid = str(message.get("id") or record.get("requestId") or "")
        price = request_cost(model, usage)
        out = _num(usage, "output_tokens")
        if rid and rid == last_id:   # another record of the same response: keep the fullest count
            if out > last_out and price is not None:
                cost, last_cost, last_out = cost - last_cost + price, price, out
            continue
        if price is None:
            known = known and _num(usage, "output_tokens") + _num(usage, "input_tokens") == 0
        else:
            cost += price
        last_id, last_cost, last_out = rid, price or 0.0, out
    return {"offset": offset, "mtime": mtime, "cost": cost, "known": known,
            "partial": bool(state.get("partial")) or skipped, "last_id": last_id, "last_cost": last_cost,
            "last_out": last_out, "model": model, "finished": bool(state.get("finished"))}


def agents_summary(states: dict, session_dir: Path, now: float, budget_s: float = AGENT_READ_BUDGET_S) -> tuple:
    """(new states, summary dict | None) for the agents under SESSION_DIR/subagents."""
    folder = session_dir / "subagents"
    try:
        metas = sorted(folder.glob("agent-*.meta.json"))
    except OSError:
        return states, None
    if not metas:
        return states, None
    deadline = time.monotonic() + budget_s
    fresh, running, done, stalled, cost, partial = {}, 0, 0, 0, 0.0, False
    for meta_path in metas:
        aid = meta_path.name[len("agent-"):-len(".meta.json")]
        old = states.get(aid) if isinstance(states.get(aid), dict) else {}
        finished = bool(old.get("finished")) or bool(read_meta(meta_path).get("finished"))
        transcript = folder / f"agent-{aid}.jsonl"
        if time.monotonic() < deadline:
            new = update_agent(old, transcript)
        else:
            new, partial = dict(old), True   # out of time: count it next render
        new["finished"] = finished
        fresh[aid] = new
        cost += float(new.get("cost") or 0.0)
        partial = partial or bool(new.get("partial")) or not new.get("known", True)
        if finished:
            done += 1
        elif new.get("mtime") and now - new["mtime"] > STALL_SECONDS:
            stalled += 1
        else:
            running += 1
    return fresh, {"running": running, "done": done, "stalled": stalled, "cost": cost, "partial": partial}


def transcript_facts(payload_path: str, session_id: str, directory: Path | None, now: float) -> dict:
    """{"cache": ..., "agents": ...} from the session's transcripts, with state carried across renders."""
    path = Path(payload_path) if payload_path else None
    if path is None or path.suffix != ".jsonl" or not path.is_file():
        return {}
    cache_file = directory / f"session-{session_id}.json" if directory and SAFE_ID.fullmatch(session_id) else None
    state = load_json(cache_file) if cache_file else {}
    if state.get("v") != STATE_VERSION:
        state = {}
    main = update_main(state.get("main") if isinstance(state.get("main"), dict) else {}, path)
    agents_state = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    agents_state, agents = agents_summary(agents_state, path.with_suffix(""), now)
    if cache_file:
        save_json(cache_file, {"v": STATE_VERSION, "main": main, "agents": agents_state})
    return {"cache": cache_from_main(main), "agents": agents}


# ---------------------------------------------------------------- other sessions

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def waiting_sessions(current_id: str, directory: Path = SESSIONS_DIR, alive=pid_alive) -> list[str]:
    """Names of other live interactive sessions waiting for the user (as the dashboard counts them)."""
    names = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return []
    for path in files:
        record = load_json(path)
        pid = optional_int(record.get("pid"))
        if (record.get("status") != "waiting" or pid is None or record.get("sessionId") == current_id
                or str(record.get("entrypoint") or "").startswith("sdk")):
            continue
        if alive(pid):
            names.append(str(record.get("name") or f"pid-{pid}"))
    return names


# ---------------------------------------------------------------- usage alert

def alert_threshold(environ=os.environ) -> float:
    """COCKPIT_USAGE_ALERT_PCT (default 60); 0 or less disables the alert."""
    raw = environ.get("COCKPIT_USAGE_ALERT_PCT")
    if raw is None or not raw.strip():
        return DEFAULT_ALERT_PCT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_ALERT_PCT


def spawn_detached(argv: list) -> None:
    import subprocess
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def claim_window(folder: Path, resets_at: int) -> bool:
    """True exactly once per 5-hour window (an O_EXCL marker named after resets_at)."""
    now = time.time()   # marker age is wall-clock file age
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = list(folder.glob("5h-*"))
    except OSError:
        return False
    for marker in existing:
        key = optional_int(marker.name[3:])
        try:
            if now - marker.stat().st_mtime > MARKER_MAX_AGE_S:
                marker.unlink()
                continue
        except OSError:
            continue
        if key is not None and abs(key - resets_at) <= SAME_WINDOW_SLACK_S:
            return False
    try:
        os.close(os.open(folder / f"5h-{resets_at}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return False
    return True


def maybe_alert(five_hour: dict, directory: Path | None, environ=os.environ,
                platform: str = sys.platform, spawn=spawn_detached) -> bool:
    """Fire-and-forget macOS notification when 5-hour usage crosses the threshold. True if sent."""
    threshold = alert_threshold(environ)
    pct, resets_at = five_hour.get("used_percentage"), optional_int(five_hour.get("resets_at"))
    if (threshold <= 0 or platform != "darwin" or directory is None or resets_at is None
            or not isinstance(pct, (int, float)) or isinstance(pct, bool) or pct < threshold):
        return False
    import shutil
    osascript = shutil.which("osascript")
    if not osascript or not claim_window(directory / "alerts", resets_at):
        return False
    message = f"5-hour plan usage at {int(pct)}%, resets at {time.strftime('%H:%M', time.localtime(resets_at))}"
    try:
        spawn([osascript, "-e", "on run argv", "-e",
               'display notification (item 1 of argv) with title "Claude Code"', "-e", "end run", message])
    except OSError:
        return False
    return True
