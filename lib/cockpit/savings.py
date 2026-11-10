"""Delegation savings: what subagents saved versus doing the same work inline in main.

Everything is an API-equivalent estimate built only from the transcripts on disk.
Every uncertain choice rounds *against* delegation, so the published number is a
floor on the saving, and a run (or a whole session) can come out negative.

Per top-level agent run r (nested agents are folded into the run that spawned them):

  calls k=1..n, prompt P_k, output O_k (from the agent's own transcript)
  S  = P_1                      the agent's baseline: system prompt + tools + brief
  W  = (P_n - S) + O_n          work that stayed out of main
  H  = hand-back tokens in main (tool_result, task-notification, peer messages)
  B  = brief tokens main wrote (the Agent tool input)
  L  = launch-ack tokens in main for background agents
       (H, B and L are sizes in chars / 2.5, rounded up: measured on real wake-ups the
       prompt grows by one token per ~2.75 chars of hand-back text, so the usual chars/4
       would understate what delegation adds to main)
  C  = prompt tokens of the main call that issued the spawn
  m  = main API calls after the last hand-back, up to the next compaction

  actual        = the agent's calls at its own model's prices (as logged)
  repriced      = the same calls, same token split, at main's model prices
  inline_work   = each call at main's prices with prompt C + (P_k - S): the extra
                  (C - S) is priced as cache reads. Call 1 is all cache read (inline,
                  main's context is already cached; the agent's first-call cache
                  write of S is a delegation cost, not something inline would pay).
  carry         = m * (W - H) * main cache-read rate
  overhead      = B * main output rate
                + (B + L + H) * main cache-write rate        (written into main once)
                + m_spawn * (B + L) * main cache-read rate   (re-read after the spawn)
                + every main call that only happened because a hand-back arrived
                  while main was idle, or that only acknowledged a launch
                  (split evenly between the runs that triggered it)

  saved = inline_work + carry - actual - overhead
        = model_mix (repriced - actual)
        + context   (inline_work - repriced + carry)
        - overhead

Conservative choices: the agent's 5-minute cache writes stay 5-minute writes when
repriced (main writes 1-hour, which costs more); carry uses the cache-read rate even
where main's cache would have expired; a run with no hand-back gets m = 0; m stops at
main's next compaction; a nested agent's net work counts only when its hand-back to
its parent is found. Inline peaks beyond the context window are flagged, never priced.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from .pricing import context_window, rate, request_cost
from .team import Accumulator, Call, Handback

CHARS_PER_TOKEN = 2.5   # below the observed median (2.75) on purpose: larger H, B, L
WAKE_STOP_REASON = "end_turn"


def chars_to_tokens(chars: int) -> int:
    return math.ceil(max(0, chars) / CHARS_PER_TOKEN)  # rounds hand-off sizes up


@dataclass(frozen=True)
class RunSavings:
    agent_id: str
    agent_type: str
    model: str
    main_model: str
    nested: int                 # descendant agents folded into this run
    calls: int
    baseline_tokens: int        # S
    work_tokens: int            # W, including nested agents' net work
    handback_tokens: int        # H
    brief_tokens: int           # B
    launch_tokens: int          # L
    main_context: int           # C
    later_main_calls: int       # m
    actual_usd: float
    repriced_usd: float
    inline_work_usd: float
    carry_usd: float
    overhead_usd: float
    handback: str = ""          # kind of the last final hand-back; "" when none was found
    known: bool = True          # False: an unknown model; the run is left out of totals

    @property
    def model_mix_usd(self) -> float:
        return self.repriced_usd - self.actual_usd

    @property
    def context_usd(self) -> float:
        return self.inline_work_usd - self.repriced_usd + self.carry_usd

    @property
    def saved_usd(self) -> float:
        return self.model_mix_usd + self.context_usd - self.overhead_usd

    @property
    def inline_usd(self) -> float:
        return self.inline_work_usd + self.carry_usd

    @property
    def delegated_usd(self) -> float:
        return self.actual_usd + self.overhead_usd

    @property
    def kept_out_tokens(self) -> int:
        return self.work_tokens - self.handback_tokens


@dataclass(frozen=True)
class SessionSavings:
    session_id: str
    main_model: str
    runs: tuple[RunSavings, ...]
    session_usd: float          # everything the session spent: main + all agents
    main_peak_tokens: int       # actual largest main prompt
    inline_peak_tokens: int     # estimated largest main prompt had the work been inline
    window: int
    partial: bool               # unknown model, or a transcript too large to read whole

    def _sum(self, attr: str) -> float:
        return sum(getattr(r, attr) for r in self.runs if r.known)

    @property
    def saved_usd(self) -> float:
        return self._sum("saved_usd")

    @property
    def model_mix_usd(self) -> float:
        return self._sum("model_mix_usd")

    @property
    def context_usd(self) -> float:
        return self._sum("context_usd")

    @property
    def overhead_usd(self) -> float:
        return self._sum("overhead_usd")

    @property
    def inline_usd(self) -> float:
        return self._sum("inline_usd")

    @property
    def delegated_usd(self) -> float:
        return self._sum("delegated_usd")

    @property
    def kept_out_tokens(self) -> int:
        return int(self._sum("kept_out_tokens"))

    @property
    def pct(self) -> float | None:
        """Saved as a share of the inline estimate; None when there is nothing to compare."""
        return 100.0 * self.saved_usd / self.inline_usd if self.inline_usd > 0 else None

    @property
    def would_compact(self) -> bool:
        return self.inline_peak_tokens > self.window


# ---------------------------------------------------------------- token repricing (pure)

def _split(usage: dict) -> tuple[int, int, int, int, int]:
    """(input, write_5m, write_1h, read, output) of one usage block."""
    def num(source: dict, key: str) -> int:
        value = source.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    created = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    write_5m, write_1h = num(created, "ephemeral_5m_input_tokens"), num(created, "ephemeral_1h_input_tokens")
    if write_5m + write_1h == 0:
        write_5m = num(usage, "cache_creation_input_tokens")
    return (num(usage, "input_tokens"), write_5m, write_1h, num(usage, "cache_read_input_tokens"),
            num(usage, "output_tokens"))


def _usage(fresh: int, write_5m: int, write_1h: int, read: int, output: int, flags: dict) -> dict:
    return {"input_tokens": fresh, "cache_creation_input_tokens": write_5m + write_1h,
            "cache_creation": {"ephemeral_5m_input_tokens": write_5m, "ephemeral_1h_input_tokens": write_1h},
            "cache_read_input_tokens": read, "output_tokens": output, **flags}


def repriced_usage(usage: dict, flags: dict) -> dict:
    """The same tokens, with main's speed / geography flags."""
    return _usage(*_split(usage), flags)


def inline_usage(usage: dict, main_context: int, baseline: int, first: bool, flags: dict) -> dict:
    """One agent call as main would have made it: prompt = C + (P_k - S)."""
    fresh, write_5m, write_1h, read, output = _split(usage)
    if first:  # P_1 = S, all of it replaced by main's already-cached context
        return _usage(0, 0, 0, max(0, main_context), output, flags)
    read += main_context - baseline
    deficit = max(0, -read)  # C < S by more than this call's reads: shrink the uncached part too
    read = max(0, read)
    take = min(deficit, write_5m)
    write_5m, deficit = write_5m - take, deficit - take
    take = min(deficit, write_1h)
    write_1h, deficit = write_1h - take, deficit - take
    fresh = max(0, fresh - deficit)
    return _usage(fresh, write_5m, write_1h, read, output, flags)


def _flags(call: Call | None) -> dict:
    usage = call.usage if call else {}
    return {k: usage[k] for k in ("speed", "inference_geo") if k in usage}


# ---------------------------------------------------------------- session assembly

@dataclass(frozen=True)
class _MainTimeline:
    calls: tuple[Call, ...]       # main calls with a timestamp, sorted
    stamps: tuple[float, ...]
    compactions: tuple[float, ...]
    write_category: str           # main's cache-write TTL

    @classmethod
    def build(cls, main: Accumulator) -> "_MainTimeline":
        calls = tuple(sorted((c for c in main.calls if c.ts is not None), key=lambda c: c.ts))
        one_hour = any(_split(c.usage)[2] > 0 for c in main.calls)
        return cls(calls, tuple(c.ts for c in calls), tuple(sorted(t for t in main.compactions if t is not None)),
                   "write_1h" if one_hour else "write_5m")

    def next_compaction(self, ts: float) -> float:
        index = bisect.bisect_right(self.compactions, ts)
        return self.compactions[index] if index < len(self.compactions) else float("inf")

    def calls_after(self, ts: float | None) -> int:
        """Main calls strictly after ts and before the next compaction."""
        if ts is None:
            return 0
        start = bisect.bisect_right(self.stamps, ts)
        end = bisect.bisect_left(self.stamps, self.next_compaction(ts))
        return max(0, end - start)

    def call_before(self, ts: float | None) -> Call | None:
        if ts is None:
            return None
        index = bisect.bisect_right(self.stamps, ts)
        return self.calls[index - 1] if index else None


def _lineage(metas: dict[str, dict], main: Accumulator, accs: dict[str, Accumulator]) -> dict[str, str]:
    """agent id -> parent: "main", another agent id, or "" when the spawn wasn't found."""
    owners = {tool_use_id: "main" for tool_use_id in main.spawns}
    for agent_id, acc in accs.items():
        owners.update({tool_use_id: agent_id for tool_use_id in acc.spawns})
    return {aid: owners.get(str(meta.get("toolUseId") or ""), "") for aid, meta in metas.items()}


def _root(agent_id: str, parents: dict[str, str]) -> str:
    seen = {agent_id}
    while parents.get(agent_id) in parents and parents[agent_id] not in seen:
        agent_id = parents[agent_id]
        seen.add(agent_id)
    return agent_id


def _handbacks_for(acc: Accumulator, agent_id: str, tool_use_id: str) -> list[Handback]:
    return [h for h in acc.handbacks
            if (h.agent_id and h.agent_id == agent_id) or (tool_use_id and h.tool_use_id == tool_use_id)]


def _work(calls: list[Call]) -> int:
    return max(0, calls[-1].prompt - calls[0].prompt + calls[-1].output) if calls else 0


@dataclass(frozen=True)
class _Pricing:
    model: str
    flags: dict
    context: int
    read: float
    write: float
    output: float


def _main_pricing(main: Accumulator, timeline: _MainTimeline, spawn_call: Call | None,
                  first_ts: float | None) -> _Pricing | None:
    call = spawn_call or timeline.call_before(first_ts)
    model = (call.model if call else "") or main.model
    flags = _flags(call)
    rates = [rate(model, cat, speed=flags.get("speed"), geo=flags.get("inference_geo"),
                  prompt=call.prompt if call else 0) for cat in ("read", timeline.write_category, "output")]
    if None in rates:
        return None
    return _Pricing(model, flags, call.prompt if call else 0, *rates)


def _price_calls(members: list[Accumulator], pricing: _Pricing) -> tuple[float, float, float, bool]:
    actual = repriced = inline = 0.0
    known = True
    for acc in members:
        if not acc.calls:
            continue
        baseline = acc.calls[0].prompt
        for index, call in enumerate(acc.calls):
            if call.cost is None:
                known = False
                continue
            actual += call.cost
            repriced += request_cost(pricing.model, repriced_usage(call.usage, pricing.flags)) or 0.0
            inline += request_cost(pricing.model, inline_usage(call.usage, pricing.context, baseline,
                                                                index == 0, pricing.flags)) or 0.0
    return actual, repriced, inline, known


def _nested_work(root: str, members: list[str], parents: dict[str, str], metas: dict[str, dict],
                 accs: dict[str, Accumulator]) -> int:
    """Net work of nested agents: W_d - H_d, counted only when the hand-back to the parent is found."""
    total = 0
    for agent_id in members:
        if agent_id == root:
            continue
        parent = accs.get(parents.get(agent_id, ""))
        found = _handbacks_for(parent, agent_id, str(metas[agent_id].get("toolUseId") or "")) if parent else []
        handed = sum(chars_to_tokens(h.chars) for h in found if h.kind != "launch")
        if found:
            total += max(0, _work(accs[agent_id].calls) - handed)
    return total


def _reaction_calls(timeline: _MainTimeline, per_root: dict[str, list[Handback]]) -> dict[str, float]:
    """Cost of main calls that only reacted to an agent: a wake-up after a hand-back that arrived
    while main was idle, or an end-of-turn acknowledgement right after a launch."""
    triggered: dict[int, set[str]] = {}
    for root, handbacks in per_root.items():
        for handback in handbacks:
            if handback.ts is None:
                continue
            index = bisect.bisect_right(timeline.stamps, handback.ts)
            if index >= len(timeline.calls):
                continue
            nxt, prev = timeline.calls[index], timeline.calls[index - 1] if index else None
            if handback.kind == "launch":
                reacts = nxt.stop_reason == WAKE_STOP_REASON and not nxt.tool_use
            else:
                reacts = prev is not None and prev.stop_reason == WAKE_STOP_REASON
            if reacts:
                triggered.setdefault(index, set()).add(root)
    shares: dict[str, float] = {}
    for index, roots in triggered.items():
        cost = timeline.calls[index].cost or 0.0
        for root in roots:
            shares[root] = shares.get(root, 0.0) + cost / len(roots)
    return shares


def _run(root: str, members: list[str], parents: dict[str, str], metas: dict[str, dict],
         main: Accumulator, accs: dict[str, Accumulator], timeline: _MainTimeline,
         handbacks: list[Handback], reaction_usd: float) -> RunSavings:
    meta, acc = metas[root], accs[root]
    tool_use_id = str(meta.get("toolUseId") or "")
    spawn = main.spawns.get(tool_use_id) if parents.get(root) == "main" else None
    spawn_call = main.calls[spawn.call_index] if spawn and 0 <= spawn.call_index < len(main.calls) else None
    first_ts = acc.calls[0].ts if acc.calls else acc.started
    pricing = _main_pricing(main, timeline, spawn_call, spawn.ts if spawn else first_ts)
    handback_tokens = sum(chars_to_tokens(h.chars) for h in handbacks if h.kind != "launch")
    launch_tokens = sum(chars_to_tokens(h.chars) for h in handbacks if h.kind == "launch")
    brief_tokens = chars_to_tokens(spawn.brief_chars) if spawn else 0
    finals = sorted((h for h in handbacks if h.final and h.ts is not None), key=lambda h: h.ts)
    later = timeline.calls_after(finals[-1].ts) if finals else 0
    work = _work(acc.calls) + _nested_work(root, members, parents, metas, accs)
    base = dict(agent_id=root, agent_type=str(meta.get("agentType") or "agent"), model=acc.model,
                nested=len(members) - 1, calls=sum(len(accs[m].calls) for m in members),
                baseline_tokens=acc.calls[0].prompt if acc.calls else 0, work_tokens=work,
                handback_tokens=handback_tokens, brief_tokens=brief_tokens, launch_tokens=launch_tokens,
                later_main_calls=later, handback=finals[-1].kind if finals else "")
    if pricing is None:
        return RunSavings(main_model=main.model, main_context=0, actual_usd=0.0, repriced_usd=0.0,
                          inline_work_usd=0.0, carry_usd=0.0, overhead_usd=0.0, known=False, **base)
    actual, repriced, inline, known = _price_calls([accs[m] for m in members], pricing)
    carry = later * (work - handback_tokens) * pricing.read
    after_spawn = timeline.calls_after(spawn.ts) if spawn else 0
    overhead = (brief_tokens * pricing.output
                + (brief_tokens + launch_tokens + handback_tokens) * pricing.write
                + after_spawn * (brief_tokens + launch_tokens) * pricing.read
                + reaction_usd)
    return RunSavings(main_model=pricing.model, main_context=pricing.context, actual_usd=actual,
                      repriced_usd=repriced, inline_work_usd=inline, carry_usd=carry, overhead_usd=overhead,
                      known=known, **base)


def _inline_peak(timeline: _MainTimeline, runs: list[RunSavings], hand_times: dict[str, float | None],
                 accs: dict[str, Accumulator], actual_peak: int) -> int:
    """Largest main prompt had each run's work stayed in main (until main's next compaction)."""
    deltas: list[tuple[float, int]] = []
    for run in runs:
        ts = hand_times.get(run.agent_id)
        if ts is not None:
            deltas.append((ts, run.kept_out_tokens))
            end = timeline.next_compaction(ts)
            if end != float("inf"):
                deltas.append((end, -run.kept_out_tokens))
    deltas.sort(key=lambda d: d[0])
    peak, carried, cursor = actual_peak, 0, 0
    for call in timeline.calls:
        while cursor < len(deltas) and deltas[cursor][0] < call.ts:
            carried += deltas[cursor][1]
            cursor += 1
        peak = max(peak, call.prompt + carried)
    for run in runs:  # while the work itself ran inline
        calls = accs[run.agent_id].calls
        if calls:
            peak = max(peak, run.main_context + max(c.prompt for c in calls) - calls[0].prompt)
    return peak


def session_savings(session_id: str, collected: tuple | None) -> SessionSavings | None:
    """Savings for one session from TeamWatcher.collect() output; None if the session isn't on disk."""
    if collected is None:
        return None
    main, metas, accs = collected
    timeline = _MainTimeline.build(main)
    parents = _lineage(metas, main, accs)
    groups: dict[str, list[str]] = {}
    for agent_id in metas:
        groups.setdefault(_root(agent_id, parents), []).append(agent_id)
    per_root = {root: _handbacks_for(main, root, str(metas[root].get("toolUseId") or "")) for root in groups}
    reactions = _reaction_calls(timeline, per_root)
    runs = [_run(root, members, parents, metas, main, accs, timeline, per_root[root], reactions.get(root, 0.0))
            for root, members in sorted(groups.items())]
    hand_times = {root: max((h.ts for h in hbs if h.final and h.ts is not None), default=None)
                  for root, hbs in per_root.items()}
    actual_peak = max((c.prompt for c in main.calls), default=0)
    inline_peak = _inline_peak(timeline, runs, hand_times, accs, actual_peak)
    main_model = main.model or next((r.main_model for r in runs if r.main_model), "")
    return SessionSavings(
        session_id=session_id, main_model=main_model, runs=tuple(runs),
        session_usd=main.cost_usd + sum(a.cost_usd for a in accs.values()),
        main_peak_tokens=actual_peak, inline_peak_tokens=inline_peak,
        window=context_window(main_model, actual_peak),
        partial=(main.partial or not main.cost_known or any(a.partial for a in accs.values())
                 or any(not r.known for r in runs)),
    )
