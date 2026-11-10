"""Parallelism metrics: how much a session's agents overlapped, and where they could have.

Definitions (README "Measuring savings" explains them for users):

* **Activity intervals.** An agent's transcript record timestamps, split wherever two consecutive
  records are more than ACTIVITY_GAP_S (15 min) apart, so an agent resumed later isn't counted as
  busy while it sat idle. A still-running agent's last interval ends at its latest record.
* **Duration** = sum of an agent's interval lengths. **Union** = length of the union of the
  top-level agents' intervals in the session. **Saved by overlap** = sum of top-level durations -
  union (the wall-clock time parallel agents already saved). **Overlap share** = saved / sum of
  top-level durations. Nested agents (launched by another agent) are left out of all three and of
  max concurrent: their parent waits while they run, so counting both would invent overlap.
* **Max concurrent** = most top-level agents active at one instant (touching intervals don't overlap).
* **Partial** = a transcript over 64 MB was read from its end (team.FIRST_READ_MAX_BYTES), so early
  requests, launches and activity are missing; ccx savings leaves such sessions out of its totals.
* **Request** = one human prompt in the main session and everything until the next one.
* **Multi-step request** = at least MULTI_STEP_TOOLS tool calls in the request, counting main's
  and every agent launched for it; flagged when exactly one agent did the work.
* **Missed parallelism (heuristic).** Within a request, agent B was launched only after agent A
  had finished (its last record and its final hand-back), B's brief contains no code-like token
  (path, identifier, number) that first appeared in A's report, and B touched no file A edited.
  Such chains could have been launched together: missed = sum of durations - the longest. On a
  hand-checked sample about 7 in 10 flagged pairs were truly independent.
* **Over guard budget (approximate).** Main-thread calls the claude-cockpit guard would deny under
  its default rules (guard_tool mirrors plugins/orchestrator/hooks/guard.py): file edits outside
  ~/.claude, and Read/Grep/Glob/Bash/WebFetch/WebSearch beyond 0 calls before the first agent
  launch of a prompt and 3 after each launch. Heavy shell commands (builds, tests, deploys, remote
  shells, also behind env/time/timeout/nice) are denied whatever budget is left. Shell commands made
  only of git/gh and `make release`/`release-finish` are free, as are reads of ~/.claude or Claude's temp dir.
  The guard re-checks a start-budget denial after 150 ms, so a work call that shares an assistant
  response with a later Agent call passes; the mirror holds such denials until the response ends
  and lets them through (as verify calls) when an Agent call follows. Relative paths resolve against
  the record's cwd. Where the mirror still differs: the guard's built-in agent nudge isn't mirrored
  (every Agent call counts as a launch); paths aren't realpath'd, so a symlink into ~/.claude or the
  temp dir isn't recognised; TMPDIR / CLAUDE_CODE_TMPDIR come from ccx's own environment, not
  Claude's; COCKPIT_* overrides are ignored; and it can't see whether the guard was installed or
  enabled. Read it as "work the policy says belongs to agents", not as guard failures.

Pure functions only; team.py collects the inputs incrementally. No imports from team or savings.
"""

from __future__ import annotations

import os
import re
import shlex
import statistics
from dataclasses import dataclass
from functools import lru_cache

ACTIVITY_GAP_S = 15 * 60
MULTI_STEP_TOOLS = 20
START_BUDGET = 0
VERIFY_BUDGET = 3

Interval = tuple  # (start, end) in epoch seconds


# ---------------------------------------------------------------- intervals

def add_activity(intervals: list, ts: float | None, gap: float = ACTIVITY_GAP_S) -> list:
    """Intervals with one more record timestamp folded in (a new list; the input is unchanged)."""
    if ts is None:
        return intervals
    if intervals:
        start, end = intervals[-1]
        if start <= ts <= end:
            return intervals
        if end < ts <= end + gap:
            return intervals[:-1] + [(start, ts)]
    merged = sorted(intervals + [(ts, ts)])
    out: list = []
    for start, end in merged:
        if out and start - out[-1][1] <= gap:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def total_length(intervals) -> float:
    return sum(max(0.0, end - start) for start, end in intervals)


def union_length(intervals) -> float:
    total, current = 0.0, None
    for start, end in sorted(intervals):
        if current is None or start > current[1]:
            if current is not None:
                total += current[1] - current[0]
            current = [start, end]
        else:
            current[1] = max(current[1], end)
    return total + (current[1] - current[0] if current else 0.0)


def max_concurrent(intervals) -> int:
    """Most intervals open at one instant. Intervals that only touch (one ends as the next starts)
    don't overlap; a zero-length interval (an agent with one record) counts at its instant."""
    events = []
    for start, end in intervals:
        events.append((start, 1, 1))
        events.append((end, 2 if end == start else 0, -1))  # at equal times: ends, starts, point-ends
    peak = current = 0
    for _, _, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak


# ---------------------------------------------------------------- independence heuristic

CODE_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\-~:#@]{4,}")
CODE_SHAPE_RE = re.compile(r"[/_.]|\d|[a-z][A-Z]")
PLAIN_NUMBER_RE = re.compile(r"\d{1,3}(?:\.\d+)?|v?\d+(?:\.\d+)*%?")


@lru_cache(maxsize=2048)
def code_tokens(text: str) -> frozenset:
    """Code-like tokens (paths, identifiers, ids, numbers with context) in a text."""
    found = set()
    for raw in CODE_TOKEN_RE.findall(text or ""):
        token = raw.strip(".:,-/")
        if len(token) >= 4 and CODE_SHAPE_RE.search(token) and not PLAIN_NUMBER_RE.fullmatch(token):
            found.add(token)
    return frozenset(found)


def independent(first_brief: frozenset, first_report: frozenset, second_brief: frozenset,
                first_edited: frozenset, second_touched: frozenset) -> bool:
    """Heuristic: nothing new from the first agent's report reached the second brief, and the
    second agent didn't touch a file the first one edited."""
    novel = second_brief & (first_report - first_brief)
    return not novel and not (first_edited & second_touched)


# ---------------------------------------------------------------- guard mirror (approximate)
# Copied from plugins/orchestrator/hooks/guard.py, the source of truth (not imported, so lib/cockpit
# stays independent of the plugin). Keep the constants and parsing in step with it.

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
WORK_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash", "PowerShell", "WebFetch", "WebSearch"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
FREE_COMMANDS = frozenset({"git", "gh"})
NEUTRAL_COMMANDS = frozenset({"cd", "pushd", "popd", "export", "true", ":"})
PIPE_FILTERS = frozenset({"head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "sort", "uniq", "cut",
                          "cat", "tr", "column", "jq", "less", "more", "sed", "awk"})
WRAPPERS = frozenset({"env", "command", "time", "timeout", "nice"})
WRAPPER_ARG_OPTIONS = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
}
HEAVY_COMMANDS = frozenset({"make", "npm", "pnpm", "yarn", "npx", "pytest", "tox", "cargo", "gradle", "mvn",
                            "terraform", "kubectl", "docker", "helm", "aws", "ssh", "scp"})
RELEASE_TARGETS = frozenset({"release", "release-finish"})
SEPARATOR_CHARS = frozenset(";&|()\n")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FD_REDIRECT = re.compile(r"\d*[<>]&-?\d*|&>>?")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n")
GIT_FIRST_WORD = re.compile(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)*(?:\S+/)?(?:git|gh)(?:\s|$)")
INLINE_REQUEST = re.compile(
    r"\b(?:do|handle|fix|run|check|answer|edit|change|make) (?:it|this|that|them|these) yourself\b"
    r"|\b(?:don't|dont|do not) (?:delegate|spawn|use (?:any )?(?:sub)?agents?)\b"
    r"|\bno (?:sub)?agents? (?:needed|please|for this)\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(?:not|never|no|don't|dont|didn't|didnt|doesn't|won't|wont|shouldn't|why)\b",
                      re.IGNORECASE)
PASTED = re.compile(r"<pasted_content\b.*?</pasted_content>", re.IGNORECASE | re.DOTALL)
CLAUDE_TMP_CHILD = re.compile(r"claude-\d+(?:/|$)")
TMP_ROOTS = ("/tmp", "/private/tmp")


@dataclass(frozen=True)
class GuardState:
    delegated: bool = False
    used: int = 0
    inline_ok: bool = False


def asks_for_inline(prompt: str) -> bool:
    text = PASTED.sub(" ", prompt.replace("’", "'"))
    for match in INLINE_REQUEST.finditer(text):
        sentence_start = max(text.rfind(mark, 0, match.start()) for mark in ".!?\n") + 1
        if not NEGATION.search(text, sentence_start, match.start()):
            return True
    return False


def guard_prompt(text: str, notification: bool, previous: GuardState = GuardState()) -> GuardState:
    """State at the start of a prompt: an agent's report counts as already delegated."""
    if notification:
        return GuardState(delegated=True, inline_ok=previous.inline_ok)
    return GuardState(inline_ok=asks_for_inline(text))


def _strip_heredocs(command: str) -> str:
    out, pos = [], 0
    for match in HEREDOC.finditer(command):
        if match.start() < pos:
            continue
        end = re.compile(rf"^[ \t]*{re.escape(match.group(2))}[ \t]*$", re.MULTILINE).search(command, match.end())
        out.append(command[pos:match.end()])
        pos = match.end() if end is None else end.end()
    out.append(command[pos:])
    return "".join(out)


def _segments(command: str) -> list | None:
    text = FD_REDIRECT.sub(" ", command.replace("\\\n", " "))  # backslash-newline continues the line
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments, words, after_pipe = [], [], False
    for token in tokens:
        if token and set(token) <= SEPARATOR_CHARS:
            if words:
                segments.append((after_pipe, words))
            words, after_pipe = [], token.strip("()\n") in {"|", "|&"}
        else:
            words.append(token)
    if words:
        segments.append((after_pipe, words))
    return segments


def _command_words(words: list) -> list:
    """A segment's words from the command name on: leading VAR=value, wrappers and their options dropped."""
    rest = list(words)
    while rest:
        if ASSIGNMENT.match(rest[0]):
            rest = rest[1:]
            continue
        name = os.path.basename(rest[0])
        if name not in WRAPPERS:
            break
        rest = _after_wrapper(name, rest[1:])
    return rest


def _after_wrapper(name: str, rest: list) -> list:
    if name == "command" and rest[:1] and rest[0] in {"-v", "-V"}:
        return []  # a lookup; runs nothing
    takes_arg = WRAPPER_ARG_OPTIONS.get(name, frozenset())
    while rest and rest[0].startswith("-") and rest[0] != "-":
        rest = rest[2:] if rest[0] in takes_arg else rest[1:]
    return rest[1:] if name == "timeout" else rest


def _is_release(words: list) -> bool:
    """`make [VAR=value ...] release|release-finish [VAR=value ...]`, and nothing else."""
    targets = [w for w in words[1:] if not w.startswith("-") and not ASSIGNMENT.match(w)]
    return bool(targets) and set(targets) <= RELEASE_TARGETS


def is_heavy(command: str) -> bool:
    """Copy of guard.py's is_heavy (the source of truth): any segment runs a build, test, deploy or
    remote command. Heredoc bodies are data; a command that can't be tokenised isn't heavy."""
    for _, words in _segments(_strip_heredocs(command)) or []:
        rest = _command_words(words)
        name = os.path.basename(rest[0]) if rest else ""
        if name in HEAVY_COMMANDS and not (name == "make" and _is_release(rest)):
            return True
    return False


def git_only(command: str) -> bool:
    """Copy of guard.py's is_free_shell: git/gh and `make release`/`release-finish`, with only
    navigation and read-only pipe filters around them."""
    segments = _segments(_strip_heredocs(command))
    if segments is None:
        return bool(GIT_FIRST_WORD.match(command))
    saw_free = False
    for after_pipe, words in segments:
        rest = _command_words(words)
        if not rest or rest[0].startswith("#"):
            continue
        name = os.path.basename(rest[0])
        if name in FREE_COMMANDS or (name == "make" and _is_release(rest)):
            saw_free = True
        elif not (name in NEUTRAL_COMMANDS or (after_pipe and name in PIPE_FILTERS)):
            return False
    return saw_free


def _norm(raw: object, home: str, cwd: str = "") -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    path = raw.replace("~", home, 1) if raw.startswith("~") else raw
    if not os.path.isabs(path) and os.path.isabs(cwd or ""):
        path = os.path.join(cwd, path)
    return os.path.normpath(path) if os.path.isabs(path) else ""


@lru_cache(maxsize=1)
def tmp_roots() -> tuple:
    """/tmp, /private/tmp, plus TMPDIR / CLAUDE_CODE_TMPDIR from this process's environment
    (as given and resolved), as the guard does with Claude's environment."""
    roots = set(TMP_ROOTS)
    for key in ("CLAUDE_CODE_TMPDIR", "TMPDIR"):
        value = os.environ.get(key)
        if value and os.path.isabs(value):
            roots.update({os.path.normpath(value), os.path.realpath(value)})
    return tuple(sorted(r.rstrip("/") or "/" for r in roots))


def _under(path: str, root: str) -> bool:
    return bool(path) and (path == root or path.startswith(root.rstrip("/") + "/"))


def free_read(raw: object, home: str, cwd: str = "") -> bool:
    path = _norm(raw, home, cwd)
    return _under(path, os.path.join(home, ".claude")) or any(
        path.startswith(root + "/") and CLAUDE_TMP_CHILD.match(path[len(root) + 1:]) for root in tmp_roots())


def guard_verdict(state: GuardState, name: str, tool_input: object, home: str = "",
                  cwd: str = "") -> tuple[GuardState, str | None]:
    """(next state, why the guard would deny this main-thread call or None) under the default budgets.
    Reasons: "edit", "heavy", "verify", and "start" (the one the guard re-checks after 150 ms)."""
    home = home or os.path.expanduser("~")
    data = tool_input if isinstance(tool_input, dict) else {}
    if name in AGENT_TOOLS:
        return GuardState(delegated=True, used=0, inline_ok=state.inline_ok), None
    if name in EDIT_TOOLS:
        target = _norm(data.get("file_path") or data.get("notebook_path"), home, cwd)
        if _under(target, os.path.join(home, ".claude")) or state.inline_ok:
            return state, None
        return state, "edit"
    if name not in WORK_TOOLS:
        return state, None
    if name in SHELL_TOOLS and git_only(str(data.get("command") or "")):
        return state, None
    if name == "Read" and free_read(data.get("file_path"), home, cwd):
        return state, None
    if name in {"Grep", "Glob"} and free_read(data.get("path"), home, cwd):
        return state, None
    if state.inline_ok:
        return state, None
    if name in SHELL_TOOLS and is_heavy(str(data.get("command") or "")):
        return state, "heavy"  # denied whatever budget is left, and uses none of it
    limit = VERIFY_BUDGET if state.delegated else START_BUDGET
    if state.used < limit:
        return GuardState(state.delegated, state.used + 1, state.inline_ok), None
    return state, "verify" if state.delegated else "start"


def guard_tool(state: GuardState, name: str, tool_input: object, home: str = "",
               cwd: str = "") -> tuple[GuardState, bool]:
    """(next state, would the guard deny this call), judging the call on its own (no race re-check)."""
    state, reason = guard_verdict(state, name, tool_input, home, cwd)
    return state, reason is not None


def release_after_launch(state: GuardState, held: int) -> tuple[GuardState, int]:
    """An Agent call followed `held` start-denied calls in the same response: the guard's re-check
    lets them through as checks after the launch. Returns (state, how many stay denied)."""
    allowed = min(held, VERIFY_BUDGET)
    return GuardState(True, allowed, state.inline_ok), held - allowed


# ---------------------------------------------------------------- session assembly

@dataclass(frozen=True)
class SessionParallel:
    session_id: str
    agents: int
    active_s: float                 # sum of agent durations
    union_s: float                  # wall-clock with at least one agent active
    max_concurrent: int
    requests: int                   # human requests in main
    agents_per_request: tuple       # Agent launches per request that delegated at all
    multi_step: int                 # requests with >= MULTI_STEP_TOOLS tool calls
    multi_step_delegated: int       # ... that launched at least one agent
    single_agent_multi_step: int    # ... that launched exactly one
    sequential_pairs: int           # B launched after A finished, same request
    missed_pairs: int               # heuristic: ... and B looks independent of A
    missed_s: float                 # heuristic: wall-clock a joint launch would have saved
    policy: bool                    # the claude-cockpit routing policy was injected
    over_budget: tuple              # per request: main calls the guard would deny (approximate)
    nested: int = 0                 # agents launched by other agents (left out of overlap/concurrency)
    partial: bool = False           # a transcript was too large to read whole: numbers are understated

    @property
    def saved_by_overlap_s(self) -> float:
        return max(0.0, self.active_s - self.union_s)

    @property
    def overlap_share(self) -> float | None:
        return self.saved_by_overlap_s / self.active_s if self.active_s > 0 else None

    @property
    def over_budget_total(self) -> int:
        return sum(self.over_budget) if self.policy else 0

    @property
    def over_budget_requests(self) -> int:
        return sum(1 for n in self.over_budget if n) if self.policy else 0


def _done(acc, handbacks: list) -> float | None:
    ends = [t for t in [acc.last_activity] + [h.ts for h in handbacks if h.final] if t is not None]
    return max(ends) if ends else None


def _for_agent(main, agent_id: str, tool_use_id: str) -> list:
    return [h for h in main.handbacks if h.kind != "launch"
            and ((h.agent_id and h.agent_id == agent_id) or (tool_use_id and h.tool_use_id == tool_use_id))]


def _missed(chains: list) -> float:
    return sum(sum(durations) - max(durations) for durations in chains if len(durations) > 1)


def _request_runs(main, metas: dict, accs: dict) -> dict:
    """request index -> top-level agent ids, ordered by launch."""
    by_request: dict = {}
    for agent_id, meta in metas.items():
        spawn = main.spawns.get(str(meta.get("toolUseId") or ""))
        if spawn is not None and agent_id in accs:
            by_request.setdefault(spawn.request, []).append((spawn.ts or 0.0, agent_id, spawn))
    return {req: sorted(runs, key=lambda r: r[0]) for req, runs in by_request.items()}


def _close(chains: list, chain: list) -> list:
    if len(chain) > 1:
        chains.append(chain)
    return []


def _sequential(main, accs: dict, by_request: dict) -> tuple[int, int, float]:
    """(sequential pairs, heuristically independent pairs, wall-clock a joint launch would save)."""
    pairs = missed_pairs = 0
    chains: list = []
    for runs in by_request.values():
        chain: list = []  # durations of consecutive, independent, sequential agents
        for (_, a_id, a_spawn), (_, b_id, b_spawn) in zip(runs, runs[1:]):
            a, b = accs[a_id], accs[b_id]
            a_handbacks = _for_agent(main, a_id, a_spawn.tool_use_id)
            done = _done(a, a_handbacks)
            if done is None or b_spawn.ts is None or b_spawn.ts < done:
                chain = _close(chains, chain)  # B started while A ran: already parallel
                continue
            pairs += 1
            report = code_tokens(a.handback_report or a.report)
            for handback in a_handbacks:
                report = report | handback.tokens
            if independent(a_spawn.brief_tokens, report, b_spawn.brief_tokens,
                           frozenset(a.edited), frozenset(b.touched)):
                missed_pairs += 1
                chain = (chain or [total_length(a.intervals)]) + [total_length(b.intervals)]
            else:
                chain = _close(chains, chain)
        _close(chains, chain)
    return pairs, missed_pairs, _missed(chains)


def _root_request(agent_id: str, metas: dict, main, owners: dict) -> int | None:
    seen = set()
    while agent_id not in seen:
        seen.add(agent_id)
        tool_use_id = str(metas.get(agent_id, {}).get("toolUseId") or "")
        if tool_use_id in main.spawns:
            return main.spawns[tool_use_id].request
        agent_id = owners.get(tool_use_id, "")
        if agent_id not in metas:
            return None
    return None


def session_parallel(session_id: str, collected: tuple | None) -> SessionParallel | None:
    """Parallelism metrics from TeamWatcher.collect() output; None if the session isn't on disk."""
    if collected is None:
        return None
    main, metas, accs = collected
    owners = {tool_use_id: agent_id for agent_id, acc in accs.items() for tool_use_id in acc.spawns}
    nested = {aid for aid in accs if str(metas.get(aid, {}).get("toolUseId") or "") in owners}
    intervals = [iv for aid, acc in accs.items() if aid not in nested for iv in acc.intervals]
    tools = list(main.request_tools)
    for agent_id, acc in accs.items():
        request = _root_request(agent_id, metas, main, owners)
        if request is not None and 0 <= request < len(tools):
            tools[request] += acc.tool_calls
    launches: dict = {}
    for spawn in main.spawns.values():
        launches[spawn.request] = launches.get(spawn.request, 0) + 1
    multi = [i for i, n in enumerate(tools) if n >= MULTI_STEP_TOOLS]
    pairs, missed_pairs, missed_s = _sequential(main, accs, _request_runs(main, metas, accs))
    return SessionParallel(
        session_id=session_id, agents=len(accs), active_s=total_length(intervals), union_s=union_length(intervals),
        max_concurrent=max_concurrent(intervals) if intervals else 0, requests=main.human_requests,
        agents_per_request=tuple(n for _, n in sorted(launches.items())),
        multi_step=len(multi), multi_step_delegated=sum(1 for i in multi if launches.get(i, 0) >= 1),
        single_agent_multi_step=sum(1 for i in multi if launches.get(i, 0) == 1),
        sequential_pairs=pairs, missed_pairs=missed_pairs, missed_s=missed_s, policy=main.policy,
        over_budget=tuple(_with_held(main.request_over, main.guard_held)), nested=len(nested),
        partial=main.partial or any(acc.partial for acc in accs.values()),
    )


def _with_held(over: list, held: list) -> list:
    """Start-denied calls whose response may still add an Agent call count as denied for now."""
    out = list(over)
    for request in held:
        if 0 <= request < len(out):
            out[request] += 1
    return out


def median(values) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


def fmt_minutes(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m" if seconds < 6000 else f"{seconds / 3600:.1f}h"


def parallel_line(p: SessionParallel | None) -> str:
    """Team view header: `parallel: max 3 · 41% overlap · 12m saved`, plus misses when found."""
    if p is None or not p.agents:
        return ""
    share = f"{100 * p.overlap_share:.0f}%" if p.overlap_share is not None else "-"
    mark = "~" if p.partial else ""
    parts = [f"parallel{mark}: max {p.max_concurrent}", f"{share} overlap", f"{fmt_minutes(p.saved_by_overlap_s)} saved"]
    if p.missed_pairs:
        parts.append(f"{p.missed_pairs} sequential launch{'es' if p.missed_pairs != 1 else ''} looked independent "
                     f"(heuristic, ≈{fmt_minutes(p.missed_s)})")
    if p.over_budget_total:
        parts.append(f"{p.over_budget_total} main calls over guard budget (approx.)")
    if p.partial:
        parts.append("~ transcript read from its end, so these are understated")
    return " · ".join(parts)
