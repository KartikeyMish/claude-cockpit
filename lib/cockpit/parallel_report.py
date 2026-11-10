"""The "Parallelism" block of `ccx savings`: totals, top sessions, text and JSON. Pure formatting."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .parallel import SessionParallel, fmt_minutes

TOP_PARALLEL = 5
HEURISTICS_NOTE = ("Heuristics: \"looked independent\" was right about 7 times in 10 on a hand-checked sample and "
                   "can't see dependencies that leave no shared path or id; \"over guard budget\" mirrors the "
                   "guard's default rules (including its re-check when an Agent call shares the response) but "
                   "doesn't realpath symlinks, takes TMPDIR from ccx's environment, ignores COCKPIT_* overrides "
                   "and can't tell whether the guard was installed or on. Overlap and concurrency count "
                   "top-level agents only; ~ marks sessions read from the end of a transcript over 64 MB, left "
                   "out of the totals.")


@dataclass(frozen=True)
class ParallelRow:
    project: str
    parallel: SessionParallel


@dataclass(frozen=True)
class ParallelTotals:
    sessions: int                   # sessions that ran agents
    agents: int
    active_s: float
    union_s: float
    max_concurrent_median: float | None
    max_concurrent_max: int
    delegating_requests: int
    agents_per_request_median: float | None
    agents_per_request_mean: float | None
    multi_step_delegated: int
    single_agent_multi_step: int
    sequential_pairs: int
    missed_pairs: int
    missed_s: float
    policy_sessions: int
    policy_requests: int
    over_budget_total: int
    over_budget_requests: int
    partial: int = 0                # sessions left out because a transcript was read from its end

    @property
    def saved_by_overlap_s(self) -> float:
        return max(0.0, self.active_s - self.union_s)

    @property
    def overlap_share(self) -> float | None:
        return self.saved_by_overlap_s / self.active_s if self.active_s > 0 else None


def parallel_totals(rows) -> ParallelTotals:
    """Totals over sessions read whole; partial ones (read from the end) would understate them."""
    sessions = [row.parallel for row in rows if not row.parallel.partial]
    with_agents = [p for p in sessions if p.agents]
    per_request = [n for p in sessions for n in p.agents_per_request]
    policy = [p for p in sessions if p.policy]
    peaks = [p.max_concurrent for p in with_agents]
    return ParallelTotals(
        sessions=len(with_agents), agents=sum(p.agents for p in with_agents),
        active_s=sum(p.active_s for p in with_agents), union_s=sum(p.union_s for p in with_agents),
        max_concurrent_median=statistics.median(peaks) if peaks else None, max_concurrent_max=max(peaks, default=0),
        delegating_requests=len(per_request),
        agents_per_request_median=statistics.median(per_request) if per_request else None,
        agents_per_request_mean=statistics.mean(per_request) if per_request else None,
        multi_step_delegated=sum(p.multi_step_delegated for p in sessions),
        single_agent_multi_step=sum(p.single_agent_multi_step for p in sessions),
        sequential_pairs=sum(p.sequential_pairs for p in sessions), missed_pairs=sum(p.missed_pairs for p in sessions),
        missed_s=sum(p.missed_s for p in sessions), policy_sessions=len(policy),
        policy_requests=sum(p.requests for p in policy),
        over_budget_total=sum(p.over_budget_total for p in policy),
        over_budget_requests=sum(p.over_budget_requests for p in policy),
        partial=sum(1 for row in rows if row.parallel.partial),
    )


def _num(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.{digits}f}"


def _share(value: float | None) -> str:
    return f"{100 * value:.1f}%" if value is not None else "-"


def _sid(p: SessionParallel) -> str:
    return p.session_id[:8] + ("~" if p.partial else "")


def render_parallel(rows, top: int = TOP_PARALLEL) -> list[str]:
    """Text lines of the Parallelism block; empty when no session ran agents or tripped the budget."""
    t = parallel_totals(rows)
    if not t.sessions and not t.policy_sessions and not t.partial:
        return []
    lines = ["Parallelism",
             f"  {t.sessions} session{'s' if t.sessions != 1 else ''} ran {t.agents} agents · max concurrent median {_num(t.max_concurrent_median)}, "
             f"max {t.max_concurrent_max}",
             f"  agent time {fmt_minutes(t.active_s)} · wall-clock with any agent {fmt_minutes(t.union_s)} · "
             f"overlap {_share(t.overlap_share)} · saved by overlap {fmt_minutes(t.saved_by_overlap_s)}",
             f"  agents per delegating request: median {_num(t.agents_per_request_median)}, "
             f"mean {_num(t.agents_per_request_mean)} ({t.delegating_requests} requests) · "
             f"multi-step requests done by a single agent: {t.single_agent_multi_step} of {t.multi_step_delegated}",
             f"  heuristic: {t.missed_pairs} of {t.sequential_pairs} sequential launches looked independent · "
             f"≈{fmt_minutes(t.missed_s)} if launched together"]
    if t.policy_sessions:
        lines.append(f"  approximate: {t.over_budget_total} main-thread calls over the guard budget in "
                     f"{t.over_budget_requests} of {t.policy_requests} requests ({t.policy_sessions} "
                     f"session{'s' if t.policy_sessions != 1 else ''} with the routing policy)")
    if t.partial:
        lines.append(f"  ~ {t.partial} session{'s' if t.partial != 1 else ''} left out of these totals: a transcript "
                     "over 64 MB was read from its end, so its numbers are understated")
    by_saved = sorted((r for r in rows if r.parallel.agents), key=lambda r: -r.parallel.saved_by_overlap_s)[:top]
    if by_saved:
        lines += ["", "Top sessions by wall-clock saved by overlap",
                  f"{'session':<9} {'project':<24} {'agents':>6} {'max':>4} {'overlap':>8} {'saved':>7}"]
        lines += [f"{_sid(r.parallel):<9} {r.project[-24:]:<24} {r.parallel.agents:>6} "
                  f"{r.parallel.max_concurrent:>4} {_share(r.parallel.overlap_share):>8} "
                  f"{fmt_minutes(r.parallel.saved_by_overlap_s):>7}" for r in by_saved]
    missed = sorted((r for r in rows if r.parallel.missed_pairs or r.parallel.single_agent_multi_step
                     or r.parallel.over_budget_total),
                    key=lambda r: (-r.parallel.missed_s, -r.parallel.missed_pairs,
                                   -r.parallel.single_agent_multi_step, -r.parallel.over_budget_total))[:top]
    if missed:
        lines += ["", "Top missed-parallelism sessions (heuristic)",
                  f"{'session':<9} {'project':<24} {'indep/seq':>9} {'missed':>7} {'1-agent multi':>13} {'over budget':>11}"]
        lines += [f"{_sid(r.parallel):<9} {r.project[-24:]:<24} "
                  f"{f'{r.parallel.missed_pairs}/{r.parallel.sequential_pairs}':>9} {fmt_minutes(r.parallel.missed_s):>7} "
                  f"{r.parallel.single_agent_multi_step:>13} "
                  f"{(str(r.parallel.over_budget_total) if r.parallel.policy else '-'):>11}" for r in missed]
    return lines + ["", HEURISTICS_NOTE]


def session_parallel_json(row: ParallelRow) -> dict:
    p = row.parallel
    return {
        "session_id": p.session_id, "project": row.project, "agents": p.agents,
        "max_concurrent": p.max_concurrent, "active_s": round(p.active_s, 1), "union_s": round(p.union_s, 1),
        "saved_by_overlap_s": round(p.saved_by_overlap_s, 1),
        "overlap_share": round(p.overlap_share, 4) if p.overlap_share is not None else None,
        "requests": p.requests, "agents_per_request": list(p.agents_per_request),
        "multi_step_requests": p.multi_step, "multi_step_delegated": p.multi_step_delegated,
        "single_agent_multi_step": p.single_agent_multi_step,
        "heuristic_independence": {"sequential_pairs": p.sequential_pairs, "independent_pairs": p.missed_pairs,
                                   "missed_s": round(p.missed_s, 1)},
        "approximate_guard_budget": {"policy": p.policy, "over_budget_per_request": list(p.over_budget),
                                     "over_budget_total": p.over_budget_total},
        "nested_agents": p.nested, "partial": p.partial,
    }


def parallel_json(rows) -> dict:
    t = parallel_totals(rows)
    totals = {k: getattr(t, k) for k in t.__dataclass_fields__}
    totals.update(saved_by_overlap_s=t.saved_by_overlap_s, overlap_share=t.overlap_share)
    return {"totals": totals, "sessions": [session_parallel_json(r) for r in rows], "heuristics": HEURISTICS_NOTE,
            "definitions": "see README \"Measuring savings\" (Parallelism)"}
