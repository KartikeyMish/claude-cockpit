"""Parallelism metrics: intervals, the independence heuristic, the guard-budget mirror, report and CLI."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from cockpit import cli  # noqa: E402
from cockpit.parallel import (  # noqa: E402
    GuardState, add_activity, code_tokens, git_only, guard_prompt, guard_tool, independent, is_heavy, max_concurrent,
    parallel_line, session_parallel, total_length, union_length,
)
from cockpit.parallel_report import parallel_json, parallel_totals, render_parallel  # noqa: E402
from cockpit.savings_report import build_report, render_text, report_json  # noqa: E402
from cockpit.team import TeamWatcher, TranscriptTail, UsageBook  # noqa: E402
from test_savings import call, iso, notification, spawn, tool_result, write_jsonl, write_session  # noqa: E402

HOME = os.path.expanduser("~")


def human(ts, text):
    return {"type": "user", "timestamp": iso(ts), "origin": {"kind": "human"},
            "message": {"role": "user", "content": text}}


def policy(ts=0):
    """The SessionStart hook attachment, shaped like a real transcript record."""
    return {"type": "attachment", "timestamp": iso(ts),
            "attachment": {"type": "hook_success", "hookName": "SessionStart:startup", "hookEvent": "SessionStart",
                           "content": "# Orchestrator routing (claude-cockpit policy v1.5)\n\nThe user installed…"}}


def use(name, tool_id="t", **data):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": data}


def agent_call(ts, msg_id, *blocks):
    return call(ts, msg_id, w5=10, out=1, blocks=list(blocks) or None)


def brief(text):
    return {"prompt": text, "description": "task"}


def main_session():
    """Request 0: A then B sequential and independent; C depends on B (BANNER_TEXT); D overlaps C.
    Request 1: one agent E with 20 tool calls (multi-step, single agent); main over budget 3 times."""
    main = [
        policy(0), human(1, "fix billing and the web banner"),
        call(2, "m1", read=100, blocks=[spawn("spA", brief("Fix rounding in billing"))]),
        tool_result(2.5, "spA", "A", 40, status="async_launched"),
        notification(305, "A", "spA", 300),
        call(310, "m2", read=100, blocks=[spawn("spB", brief("Update web/app.ts banner"))]),
        notification(440, "B", "spB", 300),
        call(450, "m3", read=100, blocks=[spawn("spC", brief("Check BANNER_TEXT renders"))]),
        call(455, "m4", read=100, blocks=[spawn("spD", brief("Unrelated lookup"))]),
        human(990, "run the full suite"),
        call(991, "m10", read=100, blocks=[use("Read", "r1", file_path="/r/README.md")]),          # over 1
        call(992, "m11", read=100, blocks=[spawn("spE", brief("Run the suite"))]),
        *[call(993 + i, f"m{12 + i}", read=100, blocks=[use("Bash", f"b{i}", command="ls src")])
          for i in range(4)],                                                                    # 4th: over 2
        call(997, "m16", read=100, blocks=[use("Bash", "g1", command="git status && git diff | head")]),
        call(998, "m17", read=100, blocks=[use("Edit", "e1", file_path="/r/x.py")]),               # over 3
        call(999, "m18", read=100, blocks=[use("Read", "r2", file_path="~/.claude/notes.md")]),    # free
        notification(1300, "E", "spE", 300),
    ]
    agents = {
        "A": ({"toolUseId": "spA", "agentType": "orchestrator:senior-engineer"}, [
            agent_call(2, "a1", use("Read", "x", file_path="/r/src/billing/invoice.py")),
            agent_call(100, "a2", use("Edit", "y", file_path="/r/src/billing/invoice.py")),
            agent_call(302, "a3", {"type": "text", "text": "Fixed src/billing/invoice.py; added tests/test_invoice.py"})]),
        "B": ({"toolUseId": "spB", "agentType": "orchestrator:routine-worker"}, [
            agent_call(311, "b1", use("Edit", "y", file_path="/r/web/app.ts")),
            agent_call(431, "b2", use("SubagentHandback", "h", message="Changed BANNER_TEXT in web/app.ts"))]),
        "C": ({"toolUseId": "spC", "agentType": "orchestrator:scout"}, [
            agent_call(451, "c1", use("Read", "x", file_path="/r/web/app.ts")),
            agent_call(551, "c2", use("Grep", "z", pattern="BANNER_TEXT", path="/r/web"))]),
        "D": ({"toolUseId": "spD", "agentType": "orchestrator:scout"}, [
            agent_call(456, "d1", use("Glob", "x", pattern="*.md", path="/r"))]),
        "D2": ({"toolUseId": "nope"}, []),
        "E": ({"toolUseId": "spE", "agentType": "orchestrator:routine-worker"}, [
            agent_call(1001 + 10 * i, f"e{i}", use("Bash", f"x{i}", command="pytest -k part")) for i in range(20)]),
    }
    agents["D"][1].append(agent_call(500, "d2", use("Read", "x", file_path="/r/notes.md")))
    del agents["D2"]
    return main, agents


def metrics(main, agents):
    with tempfile.TemporaryDirectory() as tmp:
        write_session(Path(tmp), main, agents)
        return session_parallel("sess", TeamWatcher("sess", Path(tmp)).collect())


class IntervalTests(unittest.TestCase):
    def test_add_activity_splits_on_long_gaps_and_never_mutates(self):
        ivs = []
        for ts in (0, 60, 120):
            ivs = add_activity(ivs, ts)
        self.assertEqual(ivs, [(0, 120)])
        split = add_activity(add_activity(ivs, 120 + 901), 1100)
        self.assertEqual(split, [(0, 120), (1021, 1100)])  # 901 s after the last record: a new interval
        self.assertEqual(add_activity([(0, 100)], 2000), [(0, 100), (2000, 2000)])
        self.assertEqual(add_activity([(0, 100)], 50), [(0, 100)])           # out of order, inside
        self.assertEqual(add_activity([(1000, 1100)], 500), [(500, 1100)])   # out of order, within the gap
        before = [(0, 10)]
        add_activity(before, 20)
        self.assertEqual(before, [(0, 10)])
        self.assertEqual(add_activity([(0, 10)], None), [(0, 10)])

    def test_gap_boundary(self):
        self.assertEqual(add_activity([(0, 0)], 900), [(0, 900)])            # exactly 15 min: same interval
        self.assertEqual(add_activity([(0, 0)], 901), [(0, 0), (901, 901)])

    def test_nested_identical_disjoint_touching_and_points(self):
        cases = {  # intervals: (total, union, max concurrent)
            "nested": ([(0, 100), (20, 50)], 130, 100, 2),
            "identical": ([(0, 60), (0, 60)], 120, 60, 2),
            "disjoint": ([(0, 10), (20, 30)], 20, 20, 1),
            "touching": ([(0, 10), (10, 20)], 20, 20, 1),
            "point inside": ([(0, 10), (5, 5)], 10, 10, 2),
            "point alone": ([(5, 5)], 0, 0, 1),
            "three-way": ([(0, 30), (10, 40), (20, 25)], 65, 40, 3),
            "empty": ([], 0, 0, 0),
        }
        for name, (ivs, total, union, peak) in cases.items():
            with self.subTest(name):
                self.assertEqual((total_length(ivs), union_length(ivs), max_concurrent(ivs)), (total, union, peak))

    def test_still_running_agent_counts_up_to_its_last_record(self):
        main = [human(0, "go"), call(0.5, "m1", read=10, blocks=[spawn("sx"), spawn("sy")])]
        agents = {"X": ({"toolUseId": "sx"}, [agent_call(1 + 60 * i, f"x{i}") for i in range(11)]),   # 1..601
                  "Y": ({"toolUseId": "sy"}, [agent_call(301, "y1"), agent_call(401, "y2")])}       # no hand-back yet
        p = metrics(main, agents)
        self.assertEqual((p.active_s, p.union_s, p.saved_by_overlap_s, p.max_concurrent), (700, 600, 100, 2))
        self.assertAlmostEqual(p.overlap_share, 100 / 700)
        self.assertEqual((p.agents_per_request, p.sequential_pairs), ((2,), 0))


class IndependenceTests(unittest.TestCase):
    def test_code_tokens(self):
        self.assertEqual(code_tokens("Edit src/a.py and BANNER_TEXT, not words or 12 or v1.2; see MR !5630 x9y"),
                         frozenset({"src/a.py", "BANNER_TEXT"}))
        self.assertIn("getUserById", code_tokens("call getUserById twice"))

    def test_independent_cases(self):
        empty = frozenset()
        self.assertTrue(independent(empty, frozenset({"src/a.py"}), frozenset({"web/b.ts"}), empty, empty))
        self.assertFalse(independent(empty, frozenset({"src/a.py"}), frozenset({"src/a.py"}), empty, empty))
        # a token both briefs share came from the user, not from A's result
        self.assertTrue(independent(frozenset({"src/a.py"}), frozenset({"src/a.py"}), frozenset({"src/a.py"}),
                                    empty, empty))
        self.assertFalse(independent(empty, empty, empty, frozenset({"/r/x.py"}), frozenset({"/r/x.py"})))

    def test_session_pairs_and_missed_wall_clock(self):
        p = metrics(*main_session())
        # (A,B) sequential + independent; (B,C) sequential but C uses B's BANNER_TEXT; D overlaps C.
        self.assertEqual((p.sequential_pairs, p.missed_pairs), (2, 1))
        self.assertEqual(p.missed_s, (300 + 120) - 300)

    def test_file_dependency_breaks_the_chain(self):
        main, agents = main_session()
        agents["B"][1][0] = agent_call(311, "b1", use("Read", "y", file_path="/r/src/billing/invoice.py"))
        self.assertEqual(metrics(main, agents).missed_pairs, 0)


class GuardMirrorTests(unittest.TestCase):
    def run_calls(self, calls, state=GuardState()):
        denied = []
        for name, data in calls:
            state, deny = guard_tool(state, name, data, HOME)
            denied.append(deny)
        return denied

    def test_start_and_verify_budgets(self):
        self.assertEqual(self.run_calls([("Read", {"file_path": "/r/a"}), ("Agent", {}),
                                         *[("Bash", {"command": "ls /r"})] * 4,
                                         ("Task", {}), ("Grep", {"path": "/r"})]),
                         [True, False, False, False, False, True, False, False])

    def test_free_calls(self):
        self.assertEqual(self.run_calls([
            ("Bash", {"command": "git status && git diff | head -20"}),
            ("Bash", {"command": "cd /r && git log --oneline"}),
            ("Read", {"file_path": "~/.claude/plans/x.md"}),
            ("Read", {"file_path": "/private/tmp/claude-501/sess/tasks/a.output"}),
            ("Edit", {"file_path": HOME + "/.claude/settings.json"}),
            ("mcp__plane__workitem", {}), ("AskUserQuestion", {}),
        ]), [False] * 7)
        self.assertTrue(git_only("git commit -m x <<'EOF'\nrm -rf /\nEOF"))
        self.assertFalse(git_only("git status && npm test"))

    def test_denied(self):
        self.assertEqual(self.run_calls([("Bash", {"command": "git status && npm test"}),
                                         ("Read", {"file_path": "/tmp/other/a"}),
                                         ("Edit", {"file_path": "/r/x.py"})]), [True, True, True])

    def test_heavy_commands(self):
        delegated = GuardState(delegated=True)
        self.assertEqual(self.run_calls([("Bash", {"command": "make test"})], delegated), [True])
        after = guard_tool(delegated, "Bash", {"command": "npm test"}, HOME)[0]
        self.assertEqual(after.used, 0)  # a heavy denial uses no budget
        self.assertEqual(self.run_calls([("Bash", {"command": "make release VERSION=1.6.0"})], delegated), [False])
        self.assertEqual(self.run_calls([("Bash", {"command": "cd x && make test"})], delegated), [True])
        self.assertEqual(self.run_calls([("Bash", {"command": "make test"})],
                                        guard_prompt("just do it yourself", False)), [False])
        self.assertEqual(self.run_calls([("Bash", {"command": "echo make test"}),
                                         ("Bash", {"command": "grep pytest"})], delegated), [False, False])

    def test_prompts(self):
        self.assertTrue(guard_prompt("please do it yourself", False).inline_ok)
        self.assertFalse(guard_prompt("why didn't you do it yourself?", False).inline_ok)
        self.assertEqual(self.run_calls([("Edit", {"file_path": "/r/x"}), ("Read", {"file_path": "/r/y"})],
                                        guard_prompt("just do it yourself", False)), [False, False])
        woken = guard_prompt("<task-notification>…", True, GuardState(delegated=False, used=3))
        self.assertEqual((woken.delegated, woken.used), (True, 0))
        self.assertEqual(self.run_calls([("Read", {"file_path": "/r/a"})] * 4, woken), [False, False, False, True])

    def test_session_over_budget_per_request(self):
        p = metrics(*main_session())
        self.assertTrue(p.policy)
        self.assertEqual((p.over_budget, p.over_budget_total, p.over_budget_requests, p.requests), ((0, 3), 3, 1, 2))

    def test_without_the_policy_the_budget_is_not_reported(self):
        main, agents = main_session()
        p = metrics(main[1:], agents)
        self.assertEqual((p.policy, p.over_budget_total), (False, 0))


class GuardMirrorAgreementTests(unittest.TestCase):
    """The mirror must classify shell commands exactly like guard.py, the source of truth."""

    COMMANDS = (
        "git status", "git add -A && \\\ngit commit -m 'x'", "cd /r && git log | head -5", "time git status",
        "make release VERSION=1.6.0", "YES=1 make release-finish VERSION=1.6.0", "make VERSION=1 DRY_RUN=1 release",
        "cd /r && make release VERSION=1.6.0 | tail -20", "make release && make test", "make release && ls",
        "make test", "make test release", "make", "/usr/bin/make build", "cd x && \\\nmake test", "npm \\\n test",
        "time make test", "timeout 5 pytest", "timeout -k 2 --signal=KILL 30s npm test", "env -i PATH=/bin make",
        "env -u HOME npm test", "/usr/bin/env CI=1 npx jest", "nice -n 10 cargo build", "nice -5 make",
        "command make test", "command -v make", "echo make test", "grep -r pytest .", "ls docker/",
        "cat > n.txt <<'EOF'\nmake test\nEOF", "git commit -F - <<EOF\nnpm ci\nEOF", "make 'test", "ssh h uptime",
        "bash -c 'make test'",
    )

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("guard_source_of_truth",
                                                      ROOT / "plugins" / "orchestrator" / "hooks" / "guard.py")
        cls.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.guard)

    def test_heavy_and_free_shell_agree_with_the_guard(self):
        for command in self.COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(is_heavy(command), self.guard.is_heavy(command))
                self.assertEqual(git_only(command), self.guard.is_free_shell(command))

    def test_shared_constants_agree_with_the_guard(self):
        from cockpit import parallel
        for name in ("WRAPPERS", "WRAPPER_ARG_OPTIONS", "HEAVY_COMMANDS", "RELEASE_TARGETS", "FREE_COMMANDS",
                     "NEUTRAL_COMMANDS", "PIPE_FILTERS"):
            with self.subTest(name=name):
                self.assertEqual(getattr(parallel, name), getattr(self.guard, name))

    def test_mirror_follows_continuations_wrappers_and_free_release(self):
        fresh, delegated = GuardState(), GuardState(delegated=True)
        for command in ("make release VERSION=1.6.0", "YES=1 make release-finish VERSION=1.6.0",
                        "git add . && \\\ngit commit -m x"):
            with self.subTest(command=command):
                state, deny = guard_tool(fresh, "Bash", {"command": command}, HOME)
                self.assertEqual((deny, state.used), (False, 0), "free: no launch needed, no budget used")
        for command in ("cd x && \\\nmake test", "time make test", "timeout 5 pytest", "env -i A=1 npm test",
                        "make release && make test"):
            with self.subTest(command=command):
                state, deny = guard_tool(delegated, "Bash", {"command": command}, HOME)
                self.assertEqual((deny, state.used), (True, 0))
        self.assertEqual(guard_tool(delegated, "Bash", {"command": "make release && ls"}, HOME)[0].used, 1)


class RequestTests(unittest.TestCase):
    def test_agents_per_request_and_single_agent_multi_step(self):
        p = metrics(*main_session())
        self.assertEqual(p.agents_per_request, (4, 1))
        self.assertEqual((p.multi_step, p.multi_step_delegated, p.single_agent_multi_step), (1, 1, 1))
        self.assertEqual((p.active_s, p.union_s, p.max_concurrent), (300 + 120 + 100 + 44 + 190, 300 + 120 + 100 + 190, 2))

    def test_tail_sees_human_prompts_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            write_jsonl(path, [human(0, "one"), call(1, "m1", read=10)])
            tail = TranscriptTail(path, "main")
            self.assertEqual(tail.refresh().human_requests, 1)
            with path.open("a") as handle:
                handle.write(json.dumps(human(2, "two")) + "\n")
                handle.write(json.dumps({"type": "user", "timestamp": iso(3), "isMeta": True,
                                         "message": {"role": "user", "content": "Caveat: meta"}}) + "\n")
            self.assertEqual(tail.refresh().human_requests, 2)


class OutputTests(unittest.TestCase):
    def test_team_view_line(self):
        self.assertEqual(parallel_line(metrics(*main_session())),
                         "parallel: max 2 · 6% overlap · 44s saved · 1 sequential launch looked independent "
                         "(heuristic, ≈2m) · 3 main calls over guard budget (approx.)")
        self.assertEqual(parallel_line(None), "")

    def test_team_screen_draws_the_parallel_line(self):
        from cockpit.team_view import TeamScreen, TeamState
        from test_savings_ui import fake_view
        with tempfile.TemporaryDirectory() as tmp:
            write_session(Path(tmp), *main_session())
            watcher = TeamWatcher("sess", Path(tmp))
            team = watcher.snapshot()
            par = session_parallel("sess", watcher.collect())
        view = fake_view(30, 200)
        TeamScreen(view, "api@main").draw(team, TeamState(), 0, None, par)
        self.assertTrue(any(view.screen.line(y).strip().startswith("parallel: max 2 · 6% overlap")
                            for y in range(30)))

    def test_usage_book_parallel_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_session(Path(tmp), *main_session())
            book = UsageBook(Path(tmp))
            first = book.parallel("sess")
            self.assertIs(book.parallel("sess"), first)
            book.keep_only(set())
            self.assertEqual(book.parallels, {})

    def projects(self, tmp):
        projects = Path(tmp) / "projects"
        write_session(projects, *main_session(), sid="s-par")
        write_session(projects, [human(0, "hi"), call(1, "m1", read=10, out=1, stop="end_turn")], {}, sid="s-plain")
        return projects

    def test_report_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_report(self.projects(tmp), days=7)
        text = render_text(report)
        for needle in ("Parallelism", "1 session ran 5 agents · max concurrent median 2, max 2",
                       "overlap 5.8% · saved by overlap 44s",
                       "agents per delegating request: median 2.5, mean 2.5 (2 requests)",
                       "multi-step requests done by a single agent: 1 of 1",
                       "heuristic: 1 of 2 sequential launches looked independent · ≈2m if launched together",
                       "approximate: 3 main-thread calls over the guard budget in 1 of 2 requests (1 session with",
                       "Top sessions by wall-clock saved by overlap", "Top missed-parallelism sessions (heuristic)",
                       "Heuristics:"):
            self.assertIn(needle, text)
        data = json.loads(json.dumps(report_json(report)))
        par = data["parallelism"]
        self.assertEqual((par["totals"]["missed_pairs"], par["totals"]["over_budget_total"]), (1, 3))
        session = par["sessions"][0]
        self.assertEqual(session["heuristic_independence"], {"sequential_pairs": 2, "independent_pairs": 1,
                                                             "missed_s": 120})
        self.assertEqual(session["approximate_guard_budget"]["over_budget_per_request"], [0, 3])
        self.assertIn("hand-checked", par["heuristics"])

    def test_cli_json_includes_parallelism(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("cockpit.savings_report.PROJECTS_DIR", self.projects(tmp)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["savings", "--json", "--days", "3"]), 0)
        self.assertEqual(json.loads(out.getvalue())["parallelism"]["totals"]["sessions"], 1)

    def test_empty_block(self):
        self.assertEqual(render_parallel(()), [])
        self.assertEqual(parallel_totals(()).overlap_share, None)
        self.assertEqual(parallel_json(())["sessions"], [])


class ReviewFixTests(unittest.TestCase):
    """M1 start-race re-check, path divergences, M2 partial transcripts, M3 policy detection, M7 nesting."""

    def over(self, records, policy_record=True):
        main = ([policy(0)] if policy_record else []) + [human(1, "go")] + records
        return metrics(main, {})

    def test_work_call_before_an_agent_call_in_the_same_response_passes(self):
        p = self.over([call(2, "m1", read=10, blocks=[use("Grep", "g", pattern="x", path="/r")]),
                       call(2, "m1", read=10, blocks=[spawn("s1")])])
        self.assertEqual(p.over_budget, (0,))

    def test_agent_call_in_a_later_response_does_not_release(self):
        p = self.over([call(2, "m1", read=10, blocks=[use("Grep", "g", pattern="x", path="/r")]),
                       call(3, "m2", read=10, blocks=[spawn("s1")])])
        self.assertEqual(p.over_budget, (1,))

    def test_release_uses_the_verify_budget(self):
        reads = [call(2, "m1", read=10, blocks=[use("Read", f"r{i}", file_path=f"/r/{i}")]) for i in range(4)]
        p = self.over(reads + [call(2, "m1", read=10, blocks=[spawn("s1")]),
                               call(3, "m2", read=10, blocks=[use("Read", "r9", file_path="/r/9")])])
        self.assertEqual(p.over_budget, (2,))  # 3 released, 1 still denied, and the budget is spent

    def test_heavy_command_is_not_released(self):
        p = self.over([call(2, "m1", read=10, blocks=[use("Bash", "b", command="npm test")]),
                       call(2, "m1", read=10, blocks=[spawn("s1")])])
        self.assertEqual(p.over_budget, (1,))

    def test_held_denials_count_until_resolved_and_at_a_new_prompt(self):
        held = self.over([call(2, "m1", read=10, blocks=[use("Grep", "g", pattern="x", path="/r")])])
        self.assertEqual(held.over_budget, (1,))  # the response may still grow; counted for now
        p = self.over([call(2, "m1", read=10, blocks=[use("Grep", "g", pattern="x", path="/r")]),
                       human(3, "next"), call(4, "m2", read=10, blocks=[spawn("s1")])])
        self.assertEqual(p.over_budget, (1, 0))

    def test_relative_paths_resolve_against_the_record_cwd(self):
        record = call(2, "m1", read=10, blocks=[use("Read", "r", file_path="notes.md")])
        record["cwd"] = HOME + "/.claude"
        self.assertEqual(self.over([record]).over_budget, (0,))
        state, deny = guard_tool(GuardState(), "Read", {"file_path": "notes.md"}, HOME, cwd="/r")
        self.assertTrue(deny)

    def test_tmpdir_from_the_environment_is_a_free_root(self):
        from cockpit.parallel import tmp_roots
        with mock.patch.dict(os.environ, {"TMPDIR": "/var/folders/ab/T/"}):
            tmp_roots.cache_clear()
            try:
                self.assertFalse(guard_tool(GuardState(), "Read", {"file_path": "/var/folders/ab/T/claude-501/x"},
                                            HOME)[1])
            finally:
                tmp_roots.cache_clear()

    def test_policy_needs_the_session_start_attachment(self):
        quoting = {"type": "user", "timestamp": iso(2), "toolUseResult": {"stdout": ""},
                   "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content":
                               '# Orchestrator routing (claude-cockpit policy v1.5) "hookEvent": "SessionStart"'}]}}
        other_hook = {"type": "attachment", "timestamp": iso(2),
                      "attachment": {"type": "hook_success", "hookEvent": "UserPromptSubmit",
                                     "content": "claude-cockpit policy v1.5"}}
        read_doc = call(3, "m1", read=10, blocks=[use("Read", "r", file_path="/r/hooks/routing-policy.md")])
        self.assertFalse(self.over([quoting, other_hook, read_doc], policy_record=False).policy)
        self.assertTrue(self.over([quoting], policy_record=True).policy)

    def test_nested_agents_do_not_invent_overlap(self):
        main = [human(0, "go"), call(0.5, "m1", read=10, blocks=[spawn("sp")])]
        agents = {"P": ({"toolUseId": "sp"}, [agent_call(0, "p0", spawn("sn")), agent_call(600, "p1")]),
                  "N": ({"toolUseId": "sn"}, [agent_call(100, "n0"), agent_call(500, "n1")])}
        p = metrics(main, agents)
        self.assertEqual((p.agents, p.nested, p.active_s, p.union_s, p.saved_by_overlap_s, p.max_concurrent),
                         (2, 1, 600, 600, 0, 1))

    def test_partial_transcripts_are_marked_and_left_out_of_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            write_session(projects, *main_session(), sid="s-big")
            write_session(projects, *main_session(), sid="s-ok")
            big = projects / "-proj" / "s-big.jsonl"
            big.write_text(big.read_text() + "".join(json.dumps(human(2000 + i, "x" * 400)) + "\n" for i in range(40)))
            limit = big.stat().st_size - 100
            with mock.patch("cockpit.team.FIRST_READ_MAX_BYTES", limit):
                report = build_report(projects, days=7)
                part = session_parallel("s-big", TeamWatcher("s-big", projects).collect())
        self.assertTrue(part.partial)
        self.assertTrue(parallel_line(part).startswith("parallel~: "))
        self.assertIn("transcript read from its end", parallel_line(part))
        totals = parallel_totals(report.parallel)
        self.assertEqual((totals.partial, totals.sessions), (1, 1))
        text = render_text(report)
        self.assertIn("~ 1 session left out of these totals", text)
        self.assertIn("s-big~", text)
        data = report_json(report)["parallelism"]
        self.assertEqual(sorted(x["partial"] for x in data["sessions"]), [False, True])
        self.assertEqual(data["totals"]["partial"], 1)


if __name__ == "__main__":
    unittest.main()
