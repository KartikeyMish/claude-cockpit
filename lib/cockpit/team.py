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
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from .core import CLAUDE_HOME
from .pricing import context_window, output_tokens, prompt_tokens, request_cost

PROJECTS_DIR = CLAUDE_HOME / "projects"
# Cost needs the whole history, so files are read in full up to this size;
# beyond it the first read starts near the end and totals are marked partial.
FIRST_READ_MAX_BYTES = 64 * 1024 * 1024
ASSISTANT_RE = re.compile(rb'"type":\s*"assistant"')
TIMESTAMP_RE = re.compile(rb'"timestamp":\s*"([^"]+)"')
# Non-assistant lines are parsed only when they can carry an agent hand-back or a
# compaction boundary; every other line (tool results, file snapshots) stays on the
# cheap timestamp path.
AUX_TYPE_RE = re.compile(rb'"type":\s*"(?:user|attachment|system)"')
AUX_HINT_RE = re.compile(rb'<task-notification>|"kind":\s*"peer"|"compact_boundary"')
TOOL_RESULT_ID_RE = re.compile(rb'"tool_use_id":\s*"([^"]+)"')
NOTIFICATION_TAG = "<task-notification>"
TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
TASK_TOOL_USE_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
SPAWN_TOOLS = frozenset({"Agent", "Task"})
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


@dataclass(frozen=True)
class Call:
    """One API response, de-duplicated by message id."""
    ts: float | None
    model: str
    usage: dict             # the logged usage block with the largest output_tokens
    cost: float | None      # None for unknown models
    stop_reason: str | None
    tool_use: bool

    @property
    def prompt(self) -> int:
        return prompt_tokens(self.usage)

    @property
    def output(self) -> int:
        return output_tokens(self.usage)


@dataclass(frozen=True)
class Spawn:
    """An Agent/Task tool call made by this transcript's agent."""
    tool_use_id: str
    ts: float | None
    call_index: int         # index into Accumulator.calls of the response that issued it (-1: none)
    brief_chars: int        # size of the tool input (the brief) as serialized JSON


@dataclass(frozen=True)
class Handback:
    """Text an agent put into this transcript: its result, report, notification or launch ack."""
    ts: float | None
    kind: str               # "result" (sync tool_result), "notification", "peer", "launch"
    chars: int
    agent_id: str = ""
    tool_use_id: str = ""
    final: bool = True      # False for launch acks and interim peer messages


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
    responses: dict = field(default_factory=dict)            # message id -> index into calls
    calls: list = field(default_factory=list)                 # Call per API response, in log order
    spawns: dict = field(default_factory=dict)                # Agent tool_use id -> Spawn
    handbacks: list = field(default_factory=list)             # Handback, in log order
    compactions: list = field(default_factory=list)           # timestamps of compact boundaries
    seen_handbacks: set = field(default_factory=set)

    def touch(self, ts: float | None) -> None:
        if ts is not None:
            self.started = ts if self.started is None else min(self.started, ts)
            self.last_activity = ts if self.last_activity is None else max(self.last_activity, ts)

    def feed(self, record: dict, agent_id: str) -> None:
        ts = parse_ts(record.get("timestamp"))
        self.touch(ts)
        if record.get("type") != "assistant":
            self._aux(record, ts)
            return
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        model = message.get("model")
        if isinstance(model, str) and model.startswith("claude-"):  # skip "<synthetic>" placeholders
            self.model = model
        self.effort = str(record.get("effort") or self.effort)
        blocks = [b for b in message.get("content") or [] if isinstance(b, dict)]
        # One API response is logged as several records (thinking, text, tool_use),
        # each repeating the same usage: count usage once, but every content block.
        index = self._response(message.get("id") or record.get("requestId"), message, blocks, ts)
        for block in blocks:
            self._block(block, ts, agent_id, index)

    def _response(self, response_id: object, message: dict, blocks: list, ts: float | None) -> int:
        usage = message.get("usage")
        stop = message.get("stop_reason") if isinstance(message.get("stop_reason"), str) else None
        tool_use = any(b.get("type") == "tool_use" for b in blocks)
        if response_id is not None and response_id in self.responses:
            index = self.responses[response_id]
            if index >= 0:
                self._revise(index, usage, stop, tool_use)
                return index
            if not isinstance(usage, dict) or prompt_tokens(usage) + output_tokens(usage) == 0:
                return index  # still nothing to count
        index = self._usage(usage, ts, stop, tool_use)
        if response_id is not None:
            self.responses[response_id] = index
        return index

    def _usage(self, usage: object, ts: float | None, stop: str | None, tool_use: bool) -> int:
        """Count a new response; returns its index in calls, or -1 if it carried no usage."""
        if not isinstance(usage, dict):
            return -1
        self.context_tokens = prompt_tokens(usage) or self.context_tokens
        self.output_tokens += output_tokens(usage)
        cost = request_cost(self.model, usage)
        if cost is None:
            self.cost_known = False
        else:
            self.cost_usd += cost
            self.last_turn_usd = cost
        if prompt_tokens(usage) + output_tokens(usage) == 0:
            return -1  # placeholders such as "<synthetic>" messages are not API calls
        self.calls.append(Call(ts, self.model, usage, cost, stop, tool_use))
        return len(self.calls) - 1

    def _revise(self, index: int, usage: object, stop: str | None, tool_use: bool) -> None:
        """A later record of a response already counted. Older Claude Code builds logged a
        streaming-start snapshot (stop_reason null, a handful of output tokens) first and the
        final count later, so the record with the most output tokens wins."""
        old = self.calls[index]
        new = replace(old, stop_reason=stop or old.stop_reason, tool_use=old.tool_use or tool_use)
        if isinstance(usage, dict) and output_tokens(usage) > old.output:
            cost = request_cost(old.model, usage)
            self.output_tokens += output_tokens(usage) - old.output
            if cost is not None and old.cost is not None:
                self.cost_usd += cost - old.cost
                if index == len(self.calls) - 1:
                    self.last_turn_usd = cost
            new = replace(new, usage=usage, cost=cost)
        self.calls[index] = new

    def _block(self, block: dict, ts: float | None, agent_id: str, index: int = -1) -> None:
        if block.get("type") == "tool_use":
            name = str(block.get("name") or "tool")
            summary = summarize_tool(name, block.get("input"))
            self.tool_calls += 1
            self.now = summary
            if name in SPAWN_TOOLS and block.get("id"):
                tool_use_id = str(block["id"])
                self.spawned[tool_use_id] = agent_id
                self.spawns[tool_use_id] = Spawn(tool_use_id, ts, index, len(json.dumps(block.get("input"))))
            self.events.append(Event(ts or 0.0, agent_id, name, summary))
        elif block.get("type") == "text" and str(block.get("text") or "").strip():
            self.report = str(block["text"]).strip()

    # -- non-assistant records: only hand-backs and compaction boundaries matter

    def _aux(self, record: dict, ts: float | None) -> None:
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "compact_boundary":
            self.compactions.append(ts)
        elif kind == "attachment":
            attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
            prompt = attachment.get("prompt")
            if attachment.get("type") == "queued_command" and isinstance(prompt, str):
                self._notification(prompt, ts)
        elif kind == "user":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                self._user_text(content, record.get("origin"), ts)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        self._tool_result(block, record.get("toolUseResult"), ts)

    def _add_handback(self, handback: Handback, text_key: str) -> None:
        # The same notification can be logged twice (queued attachment and user record).
        key = (handback.kind == "launch", handback.agent_id, handback.tool_use_id, hash(text_key))
        if key not in self.seen_handbacks:
            self.seen_handbacks.add(key)
            self.handbacks.append(handback)

    def _notification(self, text: str, ts: float | None) -> None:
        if not text.lstrip().startswith(NOTIFICATION_TAG):
            return
        task, tool_use = TASK_ID_RE.search(text), TASK_TOOL_USE_RE.search(text)
        if task is None and tool_use is None:
            return
        self._add_handback(Handback(ts, "notification", len(text), task.group(1).strip() if task else "",
                                    tool_use.group(1).strip() if tool_use else ""), text)

    def _user_text(self, text: str, origin: object, ts: float | None) -> None:
        origin = origin if isinstance(origin, dict) else {}
        if origin.get("kind") == "peer":
            sender = str(origin.get("from") or origin.get("senderTaskId") or "")
            if sender:
                self._add_handback(Handback(ts, "peer", len(text), sender, "", bool(origin.get("handback"))), text)
        elif text.lstrip().startswith(NOTIFICATION_TAG):
            self._notification(text, ts)

    def _tool_result(self, block: dict, result: object, ts: float | None) -> None:
        tool_use_id = str(block.get("tool_use_id") or "")
        if tool_use_id not in self.spawns or not isinstance(result, dict):
            return  # not an agent spawn, or the spawn failed (the result is an error string)
        content = block.get("content")
        text = content if isinstance(content, str) else "".join(
            str(b.get("text") or "") for b in content or [] if isinstance(b, dict))
        launched = result.get("status") == "async_launched"
        self._add_handback(Handback(ts, "launch" if launched else "result", len(text),
                                    str(result.get("agentId") or ""), tool_use_id, not launched), text)


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
            if not ASSISTANT_RE.search(line) and not self._aux_wanted(line):  # cheap path: only the timestamp
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

    def _aux_wanted(self, line: bytes) -> bool:
        if not AUX_TYPE_RE.search(line):
            return False
        if AUX_HINT_RE.search(line):
            return True
        match = TOOL_RESULT_ID_RE.search(line) if self.acc.spawns else None
        return bool(match) and match.group(1).decode(errors="replace") in self.acc.spawns


def read_meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- watcher

class TeamWatcher:
    """Keeps incremental readers for one session and builds Team snapshots."""

    def __init__(self, session_id: str, projects_dir: Path = PROJECTS_DIR, session_dir: Path | None = None) -> None:
        self.session_id = session_id
        self.projects_dir = projects_dir
        self.session_dir: Path | None = session_dir   # <project>/<sessionId>, found on first use if None
        self.tails: dict[str, TranscriptTail] = {}

    def read_position(self) -> tuple:
        """Bytes consumed per transcript: equal positions mean nothing new was read."""
        return tuple(sorted((key, tail.offset) for key, tail in self.tails.items()))

    def _locate(self) -> Path | None:
        if self.session_dir is None:
            matches = list(self.projects_dir.glob(f"*/{self.session_id}.jsonl"))
            self.session_dir = matches[0].with_suffix("") if matches else None
        return self.session_dir

    def _tail(self, key: str, path: Path) -> Accumulator:
        if key not in self.tails:
            self.tails[key] = TranscriptTail(path, key)
        return self.tails[key].refresh()

    def collect(self) -> tuple[Accumulator, dict[str, dict], dict[str, Accumulator]] | None:
        """Refresh every transcript: (main accumulator, agent metas, agent accumulators)."""
        base = self._locate()
        if base is None:
            return None
        main_acc = self._tail("main", base.with_suffix(".jsonl"))
        metas = {p.name[len("agent-"):-len(".meta.json")]: read_meta(p)
                 for p in sorted((base / "subagents").glob("agent-*.meta.json"))}
        accs = {aid: self._tail(aid, base / "subagents" / f"agent-{aid}.jsonl") for aid in metas}
        return main_acc, metas, accs

    def snapshot(self) -> Team:
        collected = self.collect()
        if collected is None:
            return Team(session_id=self.session_id, main=None)
        main_acc, metas, accs = collected
        owners = dict(main_acc.spawned)
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
        self.saved: dict[str, tuple] = {}   # session id -> (read position, SessionSavings)

    def _watcher(self, session_id: str) -> TeamWatcher:
        if session_id not in self.watchers:
            self.watchers[session_id] = TeamWatcher(session_id, self.projects_dir)
        return self.watchers[session_id]

    def usage(self, session_id: str) -> SessionUsage | None:
        return session_usage(self._watcher(session_id).snapshot())

    def savings(self, session_id: str):  # -> savings.SessionSavings | None (imported lazily: no cycle)
        """Recomputed only when a transcript grew since the last call (the dashboard asks often)."""
        from .savings import session_savings
        watcher = self._watcher(session_id)
        collected = watcher.collect()
        position = watcher.read_position()
        cached = self.saved.get(session_id)
        if cached is not None and cached[0] == position:
            return cached[1]
        result = session_savings(session_id, collected)
        self.saved[session_id] = (position, result)
        return result

    def keep_only(self, session_ids: set[str]) -> None:
        for stale in set(self.watchers) - session_ids:
            del self.watchers[stale]
        for stale in set(self.saved) - session_ids:
            del self.saved[stale]
