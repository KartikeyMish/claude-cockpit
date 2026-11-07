"""Team data: what every agent in a Claude session is doing, read from disk.

Claude Code writes, per session,
  ~/.claude/projects/<project>/<sessionId>.jsonl                 the main transcript
  ~/.claude/projects/<project>/<sessionId>/subagents/agent-<id>.jsonl
  ~/.claude/projects/<project>/<sessionId>/subagents/agent-<id>.meta.json
These are internal formats: every field is optional and bad lines are skipped.

Nothing here talks to Claude or adds hooks, so watching costs no tokens. Each
transcript is read incrementally (only bytes appended since the last refresh),
and the first read of a huge file starts near its end.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .core import CLAUDE_HOME
from .pricing import context_window, prompt_tokens, request_cost

PROJECTS_DIR = CLAUDE_HOME / "projects"
# Cost needs the whole history, so files are read in full up to this size;
# beyond it the first read starts near the end and totals are marked partial.
FIRST_READ_MAX_BYTES = 64 * 1024 * 1024
ASSISTANT_RE = re.compile(rb'"type":\s*"assistant"')
TIMESTAMP_RE = re.compile(rb'"timestamp":\s*"([^"]+)"')
FEED_SIZE = 200
STALL_SECONDS = 300


@dataclass(frozen=True)
class Event:
    ts: float
    agent_id: str
    tool: str
    summary: str


@dataclass(frozen=True)
class AgentRun:
    agent_id: str           # "main" for the orchestrator itself
    agent_type: str
    description: str
    parent_id: str          # "main" or another agent_id
    depth: int
    finished: bool
    model: str
    effort: str
    started: float | None
    last_activity: float | None
    context_tokens: int
    output_tokens: int
    tool_calls: int
    now: str                # latest tool call summary
    report: str             # latest assistant text (the final report once finished)
    cost_usd: float = 0.0   # API-equivalent, de-duplicated per response
    cost_known: bool = True
    last_turn_usd: float = 0.0
    partial: bool = False   # transcript too large to read from the start

    @property
    def window(self) -> int:
        return context_window(self.model, self.context_tokens)

    @property
    def context_pct(self) -> float:
        return 100.0 * self.context_tokens / self.window if self.window else 0.0

    def state(self, now_s: float) -> str:
        if self.finished:
            return "done"
        if self.last_activity and now_s - self.last_activity > STALL_SECONDS:
            return "stalled"
        return "running"

    def elapsed(self, now_s: float) -> int | None:
        if self.started is None:
            return None
        end = self.last_activity if self.finished and self.last_activity else now_s
        return max(0, int(end - self.started))


@dataclass(frozen=True)
class Team:
    session_id: str
    main: AgentRun | None
    agents: tuple[AgentRun, ...] = ()
    feed: tuple[Event, ...] = ()
    found: bool = False

    def running(self, now_s: float) -> int:
        return sum(1 for a in self.agents if a.state(now_s) == "running")

    def ordered(self) -> list[tuple[AgentRun, str]]:
        """Depth-first tree order with box-drawing prefixes."""
        children: dict[str, list[AgentRun]] = {}
        for agent in self.agents:
            children.setdefault(agent.parent_id, []).append(agent)
        for group in children.values():
            group.sort(key=lambda a: (a.started is None, a.started or 0, a.agent_id))
        rows: list[tuple[AgentRun, str]] = []

        def walk(parent: str, prefix: str) -> None:
            kids = children.get(parent, [])
            for index, agent in enumerate(kids):
                last = index == len(kids) - 1
                rows.append((agent, prefix + ("└─ " if last else "├─ ")))
                walk(agent.agent_id, prefix + ("   " if last else "│  "))

        walk("main", "")
        orphans = [a for a in self.agents if a not in {r[0] for r in rows}]
        rows.extend((a, "?─ ") for a in orphans)
        return rows


# ---------------------------------------------------------------- parsing (pure)

def parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize_tool(name: str, data: object) -> str:
    """One short, human line for a tool call."""
    if not isinstance(data, dict):
        return name
    pick = {
        "Bash": data.get("command"), "Read": data.get("file_path"), "Edit": data.get("file_path"),
        "Write": data.get("file_path"), "NotebookEdit": data.get("notebook_path"),
        "Glob": data.get("pattern"), "WebFetch": data.get("url"), "WebSearch": data.get("query"),
    }.get(name)
    if name == "Grep":
        pick = " ".join(str(v) for v in (data.get("pattern"), data.get("path")) if v)
    elif name == "Agent":
        pick = f"{data.get('subagent_type') or 'agent'}: {data.get('description') or ''}".strip()
    elif name in {"Read", "Edit", "Write", "NotebookEdit"} and isinstance(pick, str):
        pick = os.path.basename(pick)
    if not pick:
        pick = next((v for v in data.values() if isinstance(v, str) and v.strip()), "")
    text = " ".join(str(pick).split())
    return f"{name} {text}".strip()[:120]


@dataclass
class Accumulator:
    """Running totals for one transcript. Mutable by design: it is a reader cache."""
    started: float | None = None
    last_activity: float | None = None
    model: str = ""
    effort: str = ""
    context_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    now: str = ""
    report: str = ""
    spawned: dict[str, str] = field(default_factory=dict)   # Agent tool_use id -> owner agent id
    events: deque = field(default_factory=lambda: deque(maxlen=FEED_SIZE))
    cost_usd: float = 0.0
    cost_known: bool = True
    last_turn_usd: float = 0.0
    partial: bool = False
    seen_responses: set = field(default_factory=set)

    def touch(self, ts: float | None) -> None:
        if ts is not None:
            self.started = ts if self.started is None else min(self.started, ts)
            self.last_activity = ts if self.last_activity is None else max(self.last_activity, ts)

    def feed(self, record: dict, agent_id: str) -> None:
        ts = parse_ts(record.get("timestamp"))
        self.touch(ts)
        if record.get("type") != "assistant":
            return
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        model = message.get("model")
        if isinstance(model, str) and model.startswith("claude-"):  # skip "<synthetic>" placeholders
            self.model = model
        self.effort = str(record.get("effort") or self.effort)
        # One API response is logged as several records (thinking, text, tool_use),
        # each repeating the same usage: count usage once, but every content block.
        response_id = message.get("id") or record.get("requestId")
        if response_id is None or response_id not in self.seen_responses:
            if response_id is not None:
                self.seen_responses.add(response_id)
            self._usage(message.get("usage"))
        for block in message.get("content") or []:
            if isinstance(block, dict):
                self._block(block, ts, agent_id)

    def _usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        self.context_tokens = prompt_tokens(usage) or self.context_tokens
        self.output_tokens += int(usage.get("output_tokens") or 0)
        cost = request_cost(self.model, usage)
        if cost is None:
            self.cost_known = False
        else:
            self.cost_usd += cost
            self.last_turn_usd = cost

    def _block(self, block: dict, ts: float | None, agent_id: str) -> None:
        if block.get("type") == "tool_use":
            name = str(block.get("name") or "tool")
            summary = summarize_tool(name, block.get("input"))
            self.tool_calls += 1
            self.now = summary
            if name in {"Agent", "Task"} and block.get("id"):
                self.spawned[str(block["id"])] = agent_id
            self.events.append(Event(ts or 0.0, agent_id, name, summary))
        elif block.get("type") == "text" and str(block.get("text") or "").strip():
            self.report = str(block["text"]).strip()


class TranscriptTail:
    """Reads only what was appended since last time."""

    def __init__(self, path: Path, agent_id: str) -> None:
        self.path, self.agent_id = path, agent_id
        self.offset, self.partial, self.acc = 0, b"", Accumulator()

    def refresh(self) -> Accumulator:
        try:
            size = self.path.stat().st_size
        except OSError:
            return self.acc
        if size < self.offset:  # rewritten: start over
            self.offset, self.partial, self.acc = 0, b"", Accumulator()
        if size == self.offset:
            return self.acc
        skip_first = False
        if self.offset == 0 and size > FIRST_READ_MAX_BYTES:
            self.offset, skip_first = size - FIRST_READ_MAX_BYTES, True
            self.acc.partial = True
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = self.partial + handle.read(size - self.offset)
        self.offset = size
        lines = chunk.split(b"\n")
        self.partial = lines.pop()  # incomplete last line waits for the next refresh
        if skip_first and lines:
            lines.pop(0)
        for line in lines:
            if not ASSISTANT_RE.search(line):  # cheap path: only the timestamp matters
                match = TIMESTAMP_RE.search(line)
                self.acc.touch(parse_ts(match.group(1).decode()) if match else None)
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                self.acc.feed(record, self.agent_id)
        return self.acc


def read_meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- watcher

class TeamWatcher:
    """Keeps incremental readers for one session and builds Team snapshots."""

    def __init__(self, session_id: str, projects_dir: Path = PROJECTS_DIR) -> None:
        self.session_id = session_id
        self.projects_dir = projects_dir
        self.session_dir: Path | None = None
        self.tails: dict[str, TranscriptTail] = {}

    def _locate(self) -> Path | None:
        if self.session_dir is None:
            matches = list(self.projects_dir.glob(f"*/{self.session_id}.jsonl"))
            self.session_dir = matches[0].with_suffix("") if matches else None
        return self.session_dir

    def _tail(self, key: str, path: Path) -> Accumulator:
        if key not in self.tails:
            self.tails[key] = TranscriptTail(path, key)
        return self.tails[key].refresh()

    def snapshot(self) -> Team:
        base = self._locate()
        if base is None:
            return Team(session_id=self.session_id, main=None)
        main_acc = self._tail("main", base.with_suffix(".jsonl"))
        owners = dict(main_acc.spawned)
        metas = {p.name[len("agent-"):-len(".meta.json")]: read_meta(p)
                 for p in sorted((base / "subagents").glob("agent-*.meta.json"))}
        accs = {aid: self._tail(aid, base / "subagents" / f"agent-{aid}.jsonl") for aid in metas}
        for acc in accs.values():
            owners.update(acc.spawned)
        agents = tuple(self._run(aid, metas[aid], accs[aid], owners) for aid in metas)
        events = list(main_acc.events) + [e for acc in accs.values() for e in acc.events]
        events.sort(key=lambda e: e.ts, reverse=True)
        return Team(session_id=self.session_id, main=self._run("main", {"agentType": "orchestrator"}, main_acc, {}),
                    agents=agents, feed=tuple(events[:FEED_SIZE]), found=True)

    @staticmethod
    def _run(agent_id: str, meta: dict, acc: Accumulator, owners: dict[str, str]) -> AgentRun:
        parent = owners.get(str(meta.get("toolUseId") or ""), "main") if agent_id != "main" else ""
        return AgentRun(
            agent_id=agent_id, agent_type=str(meta.get("agentType") or "agent"),
            description=str(meta.get("description") or ""), parent_id=parent,
            depth=int(meta.get("spawnDepth") or (0 if agent_id == "main" else 1)),
            finished=bool(meta.get("finished")), model=acc.model or str(meta.get("pickedEffortModel") or ""),
            effort=acc.effort, started=acc.started, last_activity=acc.last_activity,
            context_tokens=acc.context_tokens, output_tokens=acc.output_tokens,
            tool_calls=acc.tool_calls, now=acc.now, report=acc.report,
            cost_usd=acc.cost_usd, cost_known=acc.cost_known, last_turn_usd=acc.last_turn_usd,
            partial=acc.partial,
        )


# ---------------------------------------------------------------- per-session usage

@dataclass(frozen=True)
class SessionUsage:
    """Context fill and API-equivalent spend of one session (main thread + its agents)."""
    model: str
    context_tokens: int
    window: int
    cost_usd: float
    agents_usd: float
    last_turn_usd: float
    known: bool
    partial: bool

    @property
    def pct(self) -> float:
        return 100.0 * self.context_tokens / self.window if self.window else 0.0


def session_usage(team: Team) -> SessionUsage | None:
    main = team.main
    if not team.found or main is None:
        return None
    agents_usd = sum(a.cost_usd for a in team.agents)
    return SessionUsage(
        model=main.model, context_tokens=main.context_tokens, window=main.window,
        cost_usd=main.cost_usd + agents_usd, agents_usd=agents_usd, last_turn_usd=main.last_turn_usd,
        known=main.cost_known and all(a.cost_known for a in team.agents),
        partial=main.partial or any(a.partial for a in team.agents),
    )


class UsageBook:
    """One incremental watcher per live session; forgets sessions that are gone."""

    def __init__(self, projects_dir: Path = PROJECTS_DIR) -> None:
        self.projects_dir = projects_dir
        self.watchers: dict[str, TeamWatcher] = {}

    def usage(self, session_id: str) -> SessionUsage | None:
        if session_id not in self.watchers:
            self.watchers[session_id] = TeamWatcher(session_id, self.projects_dir)
        return session_usage(self.watchers[session_id].snapshot())

    def keep_only(self, session_ids: set[str]) -> None:
        for stale in set(self.watchers) - session_ids:
            del self.watchers[stale]
