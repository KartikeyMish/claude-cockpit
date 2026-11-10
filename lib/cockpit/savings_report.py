"""`ccx savings`: delegation savings across recent sessions, as rows + totals and as text.

Pure apart from reading transcripts: `build_report` returns data, `render_text` and
`report_json` format it. The method lives in savings.py and README "Measuring savings".
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .parallel import session_parallel
from .parallel_report import ParallelRow, parallel_json, render_parallel
from .pricing import fmt_money, fmt_signed_usd, fmt_usd
from .savings import RunSavings, SessionSavings, session_savings
from .team import PROJECTS_DIR, TeamWatcher

DEFAULT_DAYS = 7.0
TOP_SESSIONS = 5
METHOD_NOTE = ("Method: API-equivalent estimate of doing the same work inline in the main session, "
               "every uncertain choice rounded against delegation; see README \"Measuring savings\".")
COMPACT_NOTE = ("would have overflowed main's context window inline; their context saving is an upper bound, "
                "so the second line leaves them out.")


@dataclass(frozen=True)
class SessionRow:
    project: str            # the project directory, home prefix removed
    savings: SessionSavings


@dataclass(frozen=True)
class Totals:
    sessions: int
    runs: int
    session_usd: float
    delegated_usd: float
    inline_usd: float
    saved_usd: float
    model_mix_usd: float
    context_usd: float
    work_in_context_usd: float  # context part from doing the work inside main's bigger context
    carry_usd: float            # context part from the work staying in main afterwards
    overhead_usd: float
    kept_out_tokens: int
    would_compact: int
    partial: int
    negative: int

    @property
    def pct(self) -> float | None:
        return 100.0 * self.saved_usd / self.inline_usd if self.inline_usd > 0 else None


@dataclass(frozen=True)
class Report:
    days: float
    generated: float
    rows: tuple[SessionRow, ...]   # sessions that ran agents, most saved first
    scanned: int                   # sessions active in the window, with or without agents
    total: Totals
    without_compact: Totals
    parallel: tuple = ()           # ParallelRow per session with agents or the routing policy


def project_label(directory: str, home: Path | None = None) -> str:
    encoded = str(home or Path.home()).replace("/", "-")
    if directory == encoded:
        return "~"
    return directory[len(encoded) + 1:] if directory.startswith(encoded + "-") else directory


def totals(rows: list[SessionRow] | tuple[SessionRow, ...]) -> Totals:
    known = [r for row in rows for r in row.savings.runs if r.known]
    return Totals(
        sessions=len(rows), runs=sum(len(row.savings.runs) for row in rows),
        session_usd=sum(row.savings.session_usd for row in rows),
        delegated_usd=sum(r.delegated_usd for r in known), inline_usd=sum(r.inline_usd for r in known),
        saved_usd=sum(r.saved_usd for r in known), model_mix_usd=sum(r.model_mix_usd for r in known),
        context_usd=sum(r.context_usd for r in known),
        work_in_context_usd=sum(r.inline_work_usd - r.repriced_usd for r in known),
        carry_usd=sum(r.carry_usd for r in known), overhead_usd=sum(r.overhead_usd for r in known),
        kept_out_tokens=sum(r.kept_out_tokens for r in known),
        would_compact=sum(1 for row in rows if row.savings.would_compact),
        partial=sum(1 for row in rows if row.savings.partial),
        negative=sum(1 for row in rows if row.savings.saved_usd < 0),
    )


def recent_transcripts(projects_dir: Path, days: float, now: float) -> list[Path]:
    cutoff = now - days * 86400
    found = []
    for path in projects_dir.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime >= cutoff:
                found.append(path)
        except OSError:
            continue  # deleted while scanning
    return sorted(found)


def build_report(projects_dir: Path | None = None, days: float = DEFAULT_DAYS, now: float | None = None,
                 home: Path | None = None) -> Report:
    """Every main session whose transcript changed in the last `days` days."""
    if not days > 0:
        raise ValueError("days must be a positive number")
    projects_dir = PROJECTS_DIR if projects_dir is None else projects_dir
    now = time.time() if now is None else now
    rows, parallel, scanned = [], [], 0
    for path in recent_transcripts(projects_dir, days, now):
        scanned += 1
        collected = TeamWatcher(path.stem, projects_dir, session_dir=path.with_suffix("")).collect()
        project = project_label(path.parent.name, home)
        savings = session_savings(path.stem, collected)
        if savings is not None and savings.runs:
            rows.append(SessionRow(project, savings))
        metrics = session_parallel(path.stem, collected)
        if metrics is not None and (metrics.agents or metrics.policy):  # policy sessions keep the denominator honest
            parallel.append(ParallelRow(project, metrics))
    rows.sort(key=lambda row: (-row.savings.saved_usd, row.savings.session_id))
    parallel.sort(key=lambda row: row.parallel.session_id)
    kept = [row for row in rows if not row.savings.would_compact]
    return Report(days=days, generated=now, rows=tuple(rows), scanned=scanned,
                  total=totals(rows), without_compact=totals(kept), parallel=tuple(parallel))


# ---------------------------------------------------------------- text

def _tokens(count: int) -> str:
    if abs(count) >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{count / 1000:.0f}k" if abs(count) >= 1000 else str(count)


def _pct(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None else "-"


def _totals_line(label: str, t: Totals) -> str:
    return (f"{label:<22}{t.sessions:>5} {t.runs:>5} {fmt_usd(t.delegated_usd):>10} {fmt_usd(t.inline_usd):>10} "
            f"{fmt_signed_usd(t.saved_usd):>10} {_pct(t.pct):>7} {fmt_signed_usd(t.model_mix_usd):>9} "
            f"{fmt_signed_usd(t.context_usd):>9} {fmt_money(-t.overhead_usd):>9} {_tokens(t.kept_out_tokens):>8}")


def _session_line(row: SessionRow) -> str:
    s = row.savings
    flags = " ".join(f for f, on in (("compact", s.would_compact), ("partial", s.partial)) if on)
    return (f"{s.session_id[:8]:<9} {row.project[-24:]:<24} {len(s.runs):>4} {fmt_usd(s.delegated_usd):>10} "
            f"{fmt_usd(s.inline_usd):>10} {fmt_signed_usd(s.saved_usd):>10} {_pct(s.pct):>7} "
            f"{fmt_signed_usd(s.model_mix_usd):>9} {fmt_signed_usd(s.context_usd):>9} "
            f"{fmt_money(-s.overhead_usd):>9} {_tokens(s.kept_out_tokens):>8}  {flags}").rstrip()


def render_text(report: Report, top: int = TOP_SESSIONS) -> str:
    days = f"{report.days:g}"
    lines = [f"Delegation savings · last {days} day{'s' if report.days != 1 else ''} · "
             f"{report.total.sessions} of {report.scanned} sessions ran agents · API-equivalent USD", ""]
    block = render_parallel(report.parallel)
    if not report.rows:
        lines += ["No session in this window delegated to an agent, so there is nothing to compare."]
        return "\n".join(lines + ([""] + block if block else []) + ["", METHOD_NOTE]) + "\n"
    head = (f"{'':<22}{'sess':>5} {'runs':>5} {'delegated':>10} {'inline':>10} {'saved':>10} {'%':>7} "
            f"{'mix':>9} {'context':>9} {'overhead':>9} {'kept out':>8}")
    t = report.total
    lines += [head, _totals_line("all sessions", t), _totals_line("without would-compact", report.without_compact)]
    lines.append(f"  context = {fmt_money(t.work_in_context_usd)} from working inside main's larger context "
                 f"+ {fmt_money(t.carry_usd)} from main re-reading the work afterwards")
    if t.would_compact:
        lines.append(f"  {t.would_compact} session{'s' if t.would_compact != 1 else ''} {COMPACT_NOTE}")
    if t.negative:
        lines.append(f"  {t.negative} session{'s' if t.negative != 1 else ''} lost money by delegating.")
    if t.partial:
        lines.append(f"  {t.partial} session{'s' if t.partial != 1 else ''} partial (unknown model or "
                     "a transcript too large to read whole): runs with unknown prices are left out.")
    lines += ["", f"Top {min(top, len(report.rows))} sessions by saving",
              (f"{'session':<9} {'project':<24} {'runs':>4} {'delegated':>10} {'inline':>10} {'saved':>10} "
               f"{'%':>7} {'mix':>9} {'context':>9} {'overhead':>9} {'kept out':>8}  flags")]
    lines += [_session_line(row) for row in report.rows[:top]]
    lines += ([""] + block if block else []) + ["", METHOD_NOTE]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- json

def _run_json(run: RunSavings) -> dict:
    data = asdict(run)
    data.update(model_mix_usd=run.model_mix_usd, context_usd=run.context_usd, saved_usd=run.saved_usd,
                inline_usd=run.inline_usd, delegated_usd=run.delegated_usd, kept_out_tokens=run.kept_out_tokens)
    return data


def _totals_json(t: Totals) -> dict:
    return {**asdict(t), "pct": t.pct}


def report_json(report: Report) -> dict:
    sessions = []
    for row in report.rows:
        s = row.savings
        sessions.append({
            "session_id": s.session_id, "project": row.project, "main_model": s.main_model,
            "session_usd": s.session_usd, "delegated_usd": s.delegated_usd, "inline_usd": s.inline_usd,
            "saved_usd": s.saved_usd, "pct": s.pct, "model_mix_usd": s.model_mix_usd,
            "context_usd": s.context_usd, "overhead_usd": s.overhead_usd, "kept_out_tokens": s.kept_out_tokens,
            "main_peak_tokens": s.main_peak_tokens, "inline_peak_tokens": s.inline_peak_tokens,
            "window": s.window, "would_compact": s.would_compact, "partial": s.partial,
            "runs": [_run_json(r) for r in s.runs],
        })
    return {"days": report.days, "generated": report.generated, "scanned": report.scanned,
            "totals": {"all": _totals_json(report.total),
                       "without_would_compact": _totals_json(report.without_compact)},
            "sessions": sessions, "parallelism": parallel_json(report.parallel), "method": METHOD_NOTE}
