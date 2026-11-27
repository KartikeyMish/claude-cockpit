"""Per-agent metrics for the subagent rows, read incrementally from Claude Code's transcripts.

Claude Code writes, per session,
  <projects>/<project>/<sessionId>.jsonl                          (stdin `transcript_path`)
  <projects>/<project>/<sessionId>/subagents/agent-<taskId>.jsonl
  <projects>/<project>/<sessionId>/subagents/agent-<taskId>.meta.json
These are internal formats, so every field is optional and bad lines are skipped.

The status script runs every few seconds while agents work, so each transcript is
read only from the byte offset reached last time. Offsets and running totals are kept
in a per-user state dir ($TMPDIR/claude-cockpit-<uid>/subagent-status/<sessionId>.json).
Each render reads about READ_BUDGET bytes in total (plus at most one line of up to
LONG_LINE where the budget runs out), the first read of a huge file starts near its end
(totals are then marked partial), and a line longer than LONG_LINE is skipped unread.
No network, stdlib only.

This file ships inside the plugin, which is installed without lib/cockpit, so it can't
import cockpit.team. It mirrors team.py's rules: cost de-duplicated per API response
(the record with the most output tokens wins), edits = Edit/Write/MultiEdit/NotebookEdit
paths, stalled = no transcript write for STALL_SECONDS (300 s).
"""

from __future__ import annotations

import json
import os
import re
import zlib
from collections import namedtuple
from datetime import datetime
from pathlib import Path

STATE_VERSION = 2                    # 2: edited paths stored as short hashes, offsets carry the inode
STATE_MAX_AGE_S = 7 * 86400          # session state files untouched this long are deleted
TMP_MAX_AGE_S = 3600                 # temp files a cancelled render left behind
PRUNE_EVERY_S = 86400
READ_BUDGET = 4 * 1024 * 1024        # bytes read per render, across all agents
LONG_LINE = 4 * 1024 * 1024          # a longer line (a giant tool result) is skipped unread
MISS_RETRY_S = 30                    # how often to look again for a task id that has no transcript
FIRST_READ_MAX = 16 * 1024 * 1024    # a longer transcript is read from this far before its end
BLOCK = 1024 * 1024
STALL_SECONDS = 300                  # as cockpit.team.STALL_SECONDS
RECENT_RESPONSES = 8                 # message ids remembered (a response's records are adjacent)
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
MAX_EDITED = 500                     # distinct edited files remembered per agent (8-hex-digit hashes)
WORKTREE_MARK = "/.claude/worktrees/"
DONE_STATUSES = frozenset({"completed", "failed", "killed"})
QUIET_STATUSES = frozenset({"idle", "waiting", "pending"})   # not working, so never "stalled"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")   # always used with fullmatch
ASSISTANT_RE = re.compile(rb'"type":\s*"assistant"')
TIMESTAMP_RE = re.compile(rb'"timestamp":\s*"([^"]+)"')
CWD_RE = re.compile(rb'"cwd":\s*"((?:[^"\\]|\\.)*)"')


# Immutable records (namedtuple: dataclasses costs ~9 ms of import on every render).
AgentStats = namedtuple("AgentStats", "cost cost_partial tools edits worktree stalled")
AgentStats.__doc__ = """cost: USD or None before any priced response; cost_partial: history skipped,
still catching up, or an unknown model; tools: tool calls; edits: distinct files edited;
worktree: works in its own Claude worktree; stalled: no transcript write for STALL_SECONDS."""
Totals = namedtuple("Totals", "running done cost cost_partial peak")
Metrics = namedtuple("Metrics", "rows totals")   # rows: task id -> AgentStats; totals: Totals or None


# ---------------------------------------------------------------- state

def state_dir() -> Path | None:
    """$TMPDIR/claude-cockpit-<uid>/subagent-status, or None when it isn't safely ours (no caching then)."""
    uid = getattr(os, "getuid", lambda: 0)()
    base = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    root = Path(base) / f"claude-cockpit-{uid}"
    path = root / "subagent-status"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return None
    return path if _ours(root) and _ours(path) else None


def _ours(path: Path) -> bool:
    """PATH is a real directory (not a symlink) owned by this user; tightened to 0700."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path) or not os.path.isdir(path):
        return False
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return False   # someone else's directory in a shared /tmp
    try:
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        return False
    return True


def prune_state(directory: Path, now: float) -> None:
    """At most once a day: delete state files older than STATE_MAX_AGE_S and stale temp files."""
    stamp = directory / ".pruned"
    try:
        if stamp.exists() and now - stamp.stat().st_mtime < PRUNE_EVERY_S:
            return
        stamp.touch()
        with os.scandir(directory) as listing:
            for item in listing:
                name = item.name
                limit = TMP_MAX_AGE_S if name.startswith(".") and name.endswith(".tmp") else (
                    STATE_MAX_AGE_S if name.endswith(".json") else None)
                if limit is not None and item.is_file(follow_symlinks=False) and \
                        now - item.stat(follow_symlinks=False).st_mtime > limit:
                    os.unlink(item.path)
    except OSError:
        return


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("v") != STATE_VERSION or not isinstance(data.get("agents"), dict):
        return {}
    return data


def save_state(path: Path, state: dict) -> None:
    """Write atomically (temp file + rename) into the state dir; failures are ignored."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, separators=(",", ":")))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------- locating transcripts

def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(base) / "projects"


def find_session_dir(payload: dict) -> Path | None:
    """<project>/<sessionId>: from transcript_path, else by session id under the projects dir."""
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript.endswith(".jsonl"):
        return Path(transcript[: -len(".jsonl")])
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and SAFE_ID.fullmatch(session_id):
        matches = sorted(projects_dir().glob(f"*/{session_id}.jsonl"))
        if matches:
            return matches[0].with_suffix("")
    return None


def agent_file(session_dir: Path, agent_id: str) -> Path | None:
    """agent-<id>.jsonl in this session, or in a sibling session of the same project (resumes)."""
    if not SAFE_ID.fullmatch(agent_id):
        return None
    own = session_dir / "subagents" / f"agent-{agent_id}.jsonl"
    if own.is_file():
        return own
    others = sorted(session_dir.parent.glob(f"*/subagents/agent-{agent_id}.jsonl"))
    return others[0] if others else None


# ---------------------------------------------------------------- incremental reading

def new_entry() -> dict:
    return {"off": 0, "ino": 0, "cost": 0.0, "priced": False, "known": True, "partial": False, "tools": 0,
            "edited": [], "cwd": "", "start": None, "last": None, "model": "", "resp": {}, "path": ""}


def cached_entry(raw: object) -> dict:
    """A reader cache from the state file, or a fresh one if it is not of the expected shape."""
    fresh = new_entry()
    if not isinstance(raw, dict):
        return fresh
    for key, default in fresh.items():
        value = raw.get(key, default)
        expected = (int, float) if key in ("cost", "start", "last") else type(default)
        if value is not None and not isinstance(value, expected):
            return fresh
    return {**fresh, **raw}


def parse_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _touch(entry: dict, line: bytes) -> None:
    match = TIMESTAMP_RE.search(line)
    ts = parse_ts(match.group(1).decode(errors="replace")) if match else None
    if ts is not None:
        entry["start"] = ts if entry["start"] is None else min(entry["start"], ts)
        entry["last"] = ts if entry["last"] is None else max(entry["last"], ts)
    if not entry["cwd"]:
        cwd = CWD_RE.search(line)
        if cwd:
            try:
                entry["cwd"] = json.loads(b'"' + cwd.group(1) + b'"')
            except ValueError:
                pass


def _count_response(entry: dict, message: dict) -> None:
    # Imported here: it costs ~8 ms (dataclasses), paid only on renders that read new responses.
    from cockpit_pricing import output_tokens, prompt_tokens, request_cost
    usage = message.get("usage")
    if not isinstance(usage, dict) or prompt_tokens(usage) + output_tokens(usage) == 0:
        return
    key = str(message.get("id") or "")
    out, cost = output_tokens(usage), request_cost(entry["model"], usage)
    seen = entry["resp"].get(key) if key else None
    if seen is not None:
        if out <= seen[0]:
            return
        if cost is not None and seen[1] is not None:
            entry["cost"] += cost - seen[1]
    elif cost is None:
        entry["known"] = False
    else:
        entry["cost"] += cost
        entry["priced"] = True
    if key:
        entry["resp"][key] = [out, cost]
        while len(entry["resp"]) > RECENT_RESPONSES:
            entry["resp"].pop(next(iter(entry["resp"])))


def feed_line(entry: dict, line: bytes) -> None:
    """Fold one transcript line into ENTRY (a reader cache, updated in place)."""
    _touch(entry, line)
    if not ASSISTANT_RE.search(line):
        return
    try:
        record = json.loads(line)
    except ValueError:
        return
    message = record.get("message") if isinstance(record, dict) else None
    if not isinstance(message, dict):
        return
    model = message.get("model")
    if isinstance(model, str) and model.startswith("claude-"):
        entry["model"] = model
    _count_response(entry, message)
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        entry["tools"] += 1
        data = block.get("input") if isinstance(block.get("input"), dict) else {}
        path = data.get("file_path") or data.get("notebook_path")
        if block.get("name") in EDIT_TOOLS and isinstance(path, str) and len(entry["edited"]) < MAX_EDITED:
            digest = format(zlib.crc32(path.encode("utf-8", "replace")), "08x")   # the count needs no path
            if digest not in entry["edited"]:
                entry["edited"].append(digest)


def _skip_long_line(handle, offset: int, size: int) -> int:
    """Offset just past the newline that ends the line at OFFSET (or SIZE if none yet)."""
    position = offset
    handle.seek(offset)
    while position < size:
        block = handle.read(min(BLOCK, size - position))
        if not block:
            break
        cut = block.find(b"\n")
        if cut >= 0:
            return position + cut + 1
        position += len(block)
    return offset   # incomplete last line: wait for the writer to finish it


def refresh(entry: dict, path: Path, budget: int) -> int:
    """Read what was appended to PATH since entry['off'], up to BUDGET bytes. Returns bytes used."""
    try:
        info = path.stat()
    except OSError:
        return 0
    size = info.st_size
    if size < entry["off"] or (entry["ino"] and entry["ino"] != info.st_ino):   # rewritten or replaced
        entry.clear()
        entry.update(new_entry())
    entry["ino"] = info.st_ino
    start = entry["off"]
    if start == 0 and size > FIRST_READ_MAX:
        start, entry["partial"] = size - FIRST_READ_MAX, True
    if size == start or budget <= 0:
        entry["behind"] = size > start
        return 0
    with path.open("rb") as handle:
        if start != entry["off"]:                # landed mid-line: skip to the next one
            start = _skip_long_line(handle, start, size)
        handle.seek(start)
        chunk = handle.read(min(budget, size - start))
        end = chunk.rfind(b"\n")
        if end < 0 and start + len(chunk) < size:    # the budget ran out inside one line: finish it
            chunk += handle.read(max(0, min(LONG_LINE, size - start) - len(chunk)))
            end = chunk.find(b"\n")
        entry["behind"] = start + len(chunk) < size   # more to read next time
        if end < 0:
            entry["off"] = start                     # the writer is mid-line: wait for the rest
            if len(chunk) >= LONG_LINE:              # a line too long to parse: skip it if it has ended
                past = start + len(chunk)
                after = _skip_long_line(handle, past, size)
                entry["off"] = after if after != past else start
            return len(chunk)
    for line in chunk[:end].split(b"\n"):
        if line:
            feed_line(entry, line)
    entry["off"] = start + end + 1
    return end + 1


# ---------------------------------------------------------------- deriving metrics

def worktree_root(path: str) -> str:
    """'/repo/.claude/worktrees/x/src' -> '/repo/.claude/worktrees/x'; '' outside a worktree."""
    index = path.find(WORKTREE_MARK) if isinstance(path, str) else -1
    if index < 0:
        return ""
    name = path[index + len(WORKTREE_MARK):].split("/", 1)[0]
    return path[: index + len(WORKTREE_MARK)] + name if name else ""


def in_own_worktree(agent_cwds: list, session_cwd: object, spawned_with_worktree: bool = False) -> bool:
    """True when the agent works in a worktree that the session itself is not in.

    Signals: meta `spawnedWithWorktree` (also set when the agent merely inherits the
    session's worktree, so it counts only while the session is outside one), or an agent
    cwd under a Claude-made `.claude/worktrees/<name>` other than the session's.
    """
    own = worktree_root(str(session_cwd) if isinstance(session_cwd, str) else "")
    if spawned_with_worktree and not own:
        return True
    return any(root and root != own for root in map(worktree_root, agent_cwds))


def peak_parallel(intervals: list) -> int:
    """Most agents active at once; an end sorts before a start at the same instant."""
    events = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals])
    peak = live = 0
    for _, step in events:
        live += step
        peak = max(peak, live)
    return peak


def meta_flags(meta_path: Path, cache: dict) -> dict:
    """{'finished', 'worktree'} from agent-<id>.meta.json, re-read only when its mtime changes.

    Claude Code writes `spawnedWithWorktree: true` there for an agent started in a worktree
    of its own (or inheriting its parent's), and `finished: true` once it is done.
    """
    try:
        mtime = meta_path.stat().st_mtime
    except OSError:
        return {}
    if cache.get("mtime") != mtime:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        cache.update(mtime=mtime, finished=data.get("finished") is True,
                     worktree=data.get("spawnedWithWorktree") is True)
    return cache


def _stalled(path: Path, status: str, now: float) -> bool:
    if status in DONE_STATUSES or status in QUIET_STATUSES:
        return False
    try:
        return now - path.stat().st_mtime > STALL_SECONDS
    except OSError:
        return False


def _caught_up(entry: dict) -> bool:
    return not entry["partial"] and entry["known"] and not entry.get("behind", False)


def row_stats(entry: dict, task: dict, path: Path, meta: dict, session_cwd: object, now: float) -> AgentStats:
    return AgentStats(
        cost=entry["cost"] if entry["priced"] else None, cost_partial=not _caught_up(entry),
        tools=entry["tools"], edits=len(entry["edited"]),
        worktree=in_own_worktree([task.get("cwd"), entry["cwd"]], session_cwd, bool(meta.get("worktree"))),
        stalled=_stalled(path, str(task.get("status") or "running"), now))


def session_totals(agents: dict, tasks: dict, finished: set, now: float) -> Totals:
    """AGENTS: id -> entry for every agent of the session; TASKS: id -> stdin task."""
    live = {tid for tid, task in tasks.items() if str(task.get("status") or "running") not in DONE_STATUSES}
    done = (finished | {tid for tid in tasks if tid not in live}) - live
    priced = [e for e in agents.values() if e["priced"]]
    intervals = [(e["start"], now if aid in live else (e["last"] or e["start"]))
                 for aid, e in agents.items() if e["start"] is not None]
    return Totals(running=len(live), done=len(done),
                  cost=sum(e["cost"] for e in priced) if priced else None,
                  cost_partial=not all(_caught_up(e) for e in agents.values()),
                  peak=peak_parallel(intervals))


def _session_agents(session_dir: Path) -> dict:
    """agent id -> transcript path for every agent-*.jsonl of the session."""
    found = {}
    try:
        with os.scandir(session_dir / "subagents") as listing:
            for item in listing:
                name = item.name
                if name.startswith("agent-") and name.endswith(".jsonl") and SAFE_ID.fullmatch(name[6:-6]):
                    found[name[6:-6]] = Path(item.path)
    except OSError:
        pass
    return found


def collect(payload: dict, now: float, state_path: Path | None = None) -> Metrics | None:
    """Refresh every agent transcript of the session (within READ_BUDGET) and derive the metrics."""
    session_dir = find_session_dir(payload)
    if session_dir is None:
        return None
    key = payload.get("session_id") if isinstance(payload.get("session_id"), str) else session_dir.name
    if state_path is None and SAFE_ID.fullmatch(key or ""):
        folder = state_dir()
        if folder is not None:
            prune_state(folder, now)
            state_path = folder / f"{key}.json"
    loaded = load_state(state_path) if state_path else {}
    state = loaded
    if state.get("dir") != str(session_dir):
        state = {"v": STATE_VERSION, "dir": str(session_dir), "agents": {}, "metas": {}}
    tasks = {str(t["id"]): t for t in payload.get("tasks") or []
             if isinstance(t, dict) and isinstance(t.get("id"), str) and SAFE_ID.fullmatch(t["id"])}
    paths = _session_agents(session_dir)
    old_misses = state.get("misses") if isinstance(state.get("misses"), dict) else {}
    misses = {}
    for tid in tasks:
        cached = cached_entry(state["agents"].get(tid))["path"]
        if not cached.startswith(str(session_dir.parent) + os.sep):
            cached = ""                            # only ever read transcripts of this project
        found = paths.get(tid) or (Path(cached) if cached else None)
        tried = old_misses.get(tid)
        if found is None and isinstance(tried, (int, float)) and now - tried < MISS_RETRY_S:
            misses[tid] = tried                    # looked recently: don't glob the project again
            continue
        found = found or agent_file(session_dir, tid)
        if found is None:
            misses[tid] = now
        else:
            paths[tid] = found
    agents = {aid: cached_entry(state["agents"].get(aid)) for aid in paths}
    budget = READ_BUDGET
    for aid in sorted(paths, key=lambda a: a not in tasks):     # visible rows first
        agents[aid]["path"] = str(paths[aid])
        try:
            budget -= refresh(agents[aid], paths[aid], budget)
        except OSError:                            # unreadable right now: keep its cached totals
            continue
    old_metas = state.get("metas") if isinstance(state.get("metas"), dict) else {}
    metas = {aid: dict(old_metas[aid]) if isinstance(old_metas.get(aid), dict) else {} for aid in paths}
    for aid in paths:
        meta_flags(paths[aid].with_suffix("").with_suffix(".meta.json"), metas[aid])
    finished = {aid for aid in paths if metas[aid].get("finished")}
    rows = {tid: row_stats(agents[tid], tasks[tid], paths[tid], metas[tid], payload.get("cwd"), now)
            for tid in tasks if tid in agents}
    totals = session_totals(agents, tasks, finished, now) if agents else None
    new_state = {"v": STATE_VERSION, "dir": str(session_dir), "agents": agents, "metas": metas, "misses": misses}
    if state_path is not None and new_state != loaded:   # nothing read and nothing new: skip the write
        save_state(state_path, new_state)
    return Metrics(rows=rows, totals=totals)
