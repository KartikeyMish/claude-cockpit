"""Subagent rows: per-agent cost, tool calls, edits, worktree, stalled, and the session totals.

Fixtures are a fake ~/.claude/projects tree written per test; the status script runs as
Claude Code runs it (python3 -I -S, JSON on stdin), with TMPDIR pointing at the test dir
so its offset cache stays private.

Run: python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "orchestrator" / "scripts"
STATUS_SCRIPT = SCRIPTS / "subagent-status.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SESSION = "11111111-2222-3333-4444-555555555555"
OPUS = "claude-opus-5-5"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "lib"))
import agent_metrics  # noqa: E402
from cockpit.pricing import request_cost  # noqa: E402

# subagent-status.py has a hyphen in its name, so load it by path for its width helper.
_SPEC = importlib.util.spec_from_file_location("subagent_status", STATUS_SCRIPT)
status_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(status_script)


def stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - seconds_ago))


def usage(inp, out):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0}


def assistant(msg_id, out, ago, tools=(), cwd="/repo", model=OPUS):
    content = [{"type": "tool_use", "id": f"tu-{msg_id}-{i}", "name": name, "input": data}
               for i, (name, data) in enumerate(tools)]
    return {"type": "assistant", "timestamp": stamp(ago), "cwd": cwd,
            "message": {"id": msg_id, "model": model, "usage": usage(1000, out), "content": content}}


def user(ago, cwd="/repo"):
    return {"type": "user", "timestamp": stamp(ago), "cwd": cwd,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


class Fixture:
    """A projects dir with one session and its agents."""

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.project = self.tmp / "claude" / "projects" / "-repo"
        self.session = self.project / SESSION
        (self.session / "subagents").mkdir(parents=True)
        (self.project / f"{SESSION}.jsonl").write_text("")

    def agent(self, agent_id, records, finished=False, age=None, raw=b""):
        path = self.session / "subagents" / f"agent-{agent_id}.jsonl"
        with path.open("ab") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in records).encode() + raw)
        meta = path.with_suffix("").with_suffix(".meta.json")
        meta.write_text(json.dumps({"agentType": "orchestrator:senior-engineer", "finished": finished}))
        if age is not None:
            when = time.time() - age
            os.utime(path, (when, when))
        return path

    def payload(self, tasks, columns=200, cwd="/repo", with_path=True):
        data = {"session_id": SESSION, "cwd": cwd, "columns": columns, "tasks": tasks}
        if with_path:
            data["transcript_path"] = str(self.project / f"{SESSION}.jsonl")
        return data

    def run(self, payload):
        env = {"PATH": os.environ.get("PATH", ""), "TMPDIR": str(self.tmp / "tmp"),
               "HOME": str(self.tmp / "home"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}
        (self.tmp / "tmp").mkdir(exist_ok=True)
        out = subprocess.run([sys.executable, "-I", "-S", str(STATUS_SCRIPT)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=20, check=False, env=env)
        assert out.returncode == 0 and out.stderr == "", out.stderr
        rows = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
        return {r["id"]: ANSI.sub("", r["content"]) for r in rows}, [r["id"] for r in rows]


def task(task_id, status="running", label="Searching memory for DB access", **extra):
    return {"id": task_id, "agentType": "orchestrator:senior-engineer", "status": status, "model": OPUS,
            "effort": "high", "startTime": (time.time() - 100) * 1000, "tokenCount": 78500,
            "contextWindowSize": 1_000_000, "tokenSamples": [1000, 20000, 50000, 78500], "label": label, **extra}


def edits(*paths):
    return [("Edit", {"file_path": p}) for p in paths]


class RowMetricsTests(unittest.TestCase):
    def test_wide_row_shows_cost_tools_edits_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 500, 60, edits("/repo/a.py", "/repo/b.py") + [("Read", {"file_path": "/x"})]),
                            user(59), assistant("m2", 300, 30, edits("/repo/a.py", "/repo/c.py"))])
            rows, _ = fx.run(fx.payload([task("a1")]))
            row = rows["a1"]
            expected = request_cost(OPUS, usage(1000, 500)) + request_cost(OPUS, usage(1000, 300))
            self.assertIn(f"${expected:.2f}  🔧5  ✎3  Searching memory for DB access", row)
            self.assertRegex(row, r"^. ◆ senior-engineer  opus·high  \S+ 78\.5k  8% ctx  1:\d\d  \$")
            self.assertNotIn("⎇wt", row)
            self.assertNotIn("stalled", row)

    def test_response_logged_twice_is_costed_once_with_final_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 5, 20), assistant("m1", 4000, 19)])
            rows, _ = fx.run(fx.payload([task("a1")]))
            self.assertIn(f"${request_cost(OPUS, usage(1000, 4000)):.2f}", rows["a1"])

    def test_worktree_marker_only_for_a_worktree_the_session_is_not_in(self):
        wt = "/repo/.claude/worktrees/fix-auth"
        cases = [  # (agent transcript cwd, task cwd, session cwd, expected)
            (wt, None, "/repo", True),
            ("/repo", wt + "/src", "/repo", True),          # stdin task cwd alone is enough
            (wt + "/src", None, wt, False),                 # the whole session runs in that worktree
            ("/elsewhere/project", None, "/repo", False),   # a different cwd is not a worktree
        ]
        for agent_cwd, task_cwd, session_cwd, expected in cases:
            with self.subTest(agent=agent_cwd, task=task_cwd, session=session_cwd), \
                    tempfile.TemporaryDirectory() as tmp:
                fx = Fixture(tmp)
                fx.agent("a1", [assistant("m1", 10, 5, cwd=agent_cwd)])
                extra = {"cwd": task_cwd} if task_cwd else {}
                rows, _ = fx.run(fx.payload([task("a1", **extra)], cwd=session_cwd))
                self.assertEqual("⎇wt" in rows["a1"], expected, rows["a1"])

    def test_worktree_from_meta_spawned_with_worktree(self):
        wt = "/repo/.claude/worktrees/session-wt"
        # (session cwd, agent cwd, expected): the flag is also set for an agent that merely
        # inherits the session's worktree, so it counts only while the session is outside one.
        for session_cwd, agent_cwd, expected in (("/repo", "/somewhere/else", True), (wt, wt, False)):
            with self.subTest(session=session_cwd), tempfile.TemporaryDirectory() as tmp:
                fx = Fixture(tmp)
                path = fx.agent("a1", [assistant("m1", 10, 5, cwd=agent_cwd)])
                meta = path.with_suffix("").with_suffix(".meta.json")
                meta.write_text(json.dumps({"agentType": "orchestrator:routine-worker", "spawnedWithWorktree": True}))
                rows, _ = fx.run(fx.payload([task("a1")], cwd=session_cwd))
                self.assertEqual("⎇wt" in rows["a1"], expected, rows["a1"])

    def test_task_without_transcript_is_looked_up_again_only_after_a_while(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            state, now = fx.tmp / "state.json", time.time()
            payload = fx.payload([task("late"), task("moved")])
            self.assertEqual(agent_metrics.collect(payload, now, state).rows, {})
            fx.agent("late", [assistant("m1", 10, 5)])
            other = fx.project / "other-session" / "subagents"     # a resumed agent's file lives elsewhere
            other.mkdir(parents=True)
            (other / "agent-moved.jsonl").write_text(json.dumps(assistant("m9", 10, 5)) + "\n")
            soon = agent_metrics.collect(payload, now + 1, state).rows
            self.assertIn("late", soon, "this session's own files are listed on every render")
            self.assertNotIn("moved", soon, "the project-wide search waits MISS_RETRY_S")
            later = agent_metrics.collect(payload, now + agent_metrics.MISS_RETRY_S + 1, state).rows
            self.assertIn("moved", later)

    def test_stalled_after_five_minutes_without_a_transcript_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            for agent_id in ("quiet", "fresh", "done", "idle"):
                fx.agent(agent_id, [assistant(f"m-{agent_id}", 10, 400)],
                         age=1 if agent_id == "fresh" else 400)
            rows, _ = fx.run(fx.payload([task("quiet"), task("fresh"), task("done", status="completed"),
                                         task("idle", status="idle")]))
            self.assertIn("stalled", rows["quiet"])
            for agent_id in ("fresh", "done", "idle"):
                self.assertNotIn("stalled", rows[agent_id], agent_id)

    def test_narrow_row_drops_extras_before_the_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 500, 30, edits("/repo/a.py"), cwd="/repo/.claude/worktrees/w")])
            wide, _ = fx.run(fx.payload([task("a1")], columns=200))
            self.assertIn("🔧1  ✎1  ⎇wt  Searching", wide["a1"])
            # today's columns take 55 cells, the four extras 21, the label keeps at least 12 (+2)
            for columns, gone, kept in ((90, [], ["🔧1", "✎1", "⎇wt", "$"]), (89, ["🔧"], ["✎1", "⎇wt", "$"]),
                                        (82, ["🔧", "✎"], ["⎇wt", "$"]), (72, ["🔧", "✎", "⎇wt", "$"], [])):
                with self.subTest(columns=columns):
                    rows, _ = fx.run(fx.payload([task("a1")], columns=columns))
                    row = rows["a1"]
                    for text in gone:
                        self.assertNotIn(text, row)
                    for text in kept:
                        self.assertIn(text, row)
                    self.assertIn("Search", row, "the label survives; extras go first")
                    self.assertLessEqual(status_script.visible_len(row), columns)
            rows, _ = fx.run(fx.payload([task("a1")], columns=60))
            self.assertNotIn("Search", rows["a1"], "today's columns fill the row: no label, as before")
            self.assertNotIn("$", rows["a1"])

    def test_stalled_is_the_last_extra_to_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 500, 400, edits("/repo/a.py"))], age=400)
            rows, _ = fx.run(fx.payload([task("a1")], columns=84))
            self.assertIn("stalled", rows["a1"])
            self.assertNotIn("$", rows["a1"])


class TotalsTests(unittest.TestCase):
    def test_totals_ride_on_the_last_row_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            # a1 ran 100s..10s ago, a2 90s..20s ago (overlap), a3 finished long before
            fx.agent("a1", [assistant("m1", 100, 100), assistant("m2", 100, 10)])
            fx.agent("a2", [assistant("m3", 100, 90), assistant("m4", 100, 20)], finished=True)
            fx.agent("a3", [assistant("m5", 100, 900), assistant("m6", 100, 800)], finished=True)
            rows, order = fx.run(fx.payload([task("a1"), task("a2", status="completed")]))
            self.assertEqual(order, ["a1", "a2"], "one row per task, in stdin order, nothing else")
            total = 6 * request_cost(OPUS, usage(1000, 100))
            self.assertNotIn("│", rows["a1"])
            self.assertTrue(rows["a2"].endswith(f"│ 1 running · 2 done · agents ${total:.2f} · peak 2 parallel"),
                            rows["a2"])

    def test_totals_shrink_then_go_before_the_label_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100_000, 100), assistant("m2", 100, 10)])
            fx.agent("a2", [assistant("m3", 100_000, 90), assistant("m4", 100, 20)], finished=True)
            tasks = [task("a2", status="completed"), task("a1")]
            cost = request_cost(OPUS, usage(1000, 100_000)) * 2 + request_cost(OPUS, usage(1000, 100)) * 2
            # today's columns 55 cells, cost 5 (+2); short totals 18 (+2) need the label to keep 12 (+2): 96
            for columns, tail in ((160, f"│ 1 running · 1 done · agents ${cost:.2f} · peak 2 parallel"),
                                  (120, f"│ Σ 1▶ 1✓ ${cost:.2f} ⇈2"), (95, "")):
                with self.subTest(columns=columns):
                    rows, _ = fx.run(fx.payload(tasks, columns=columns))
                    row = rows["a1"]
                    self.assertIn("Search", row)
                    self.assertLessEqual(status_script.visible_len(row), columns)
                    if tail:
                        self.assertTrue(row.endswith(tail), row)
                    else:
                        self.assertNotIn("│", row)

    def test_no_peak_for_a_single_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100_000, 10)])
            rows, _ = fx.run(fx.payload([task("a1")], columns=220))
            self.assertIn("│ 1 running · agents $", rows["a1"])
            self.assertNotIn("peak", rows["a1"])


class MissingDataTests(unittest.TestCase):
    def test_without_transcript_location_rows_are_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100, 10)])
            payload = fx.payload([task("a1")], with_path=False)
            payload.pop("session_id")
            rows, _ = fx.run(payload)
            self.assertRegex(rows["a1"], r"ctx  1:\d\d  Searching memory for DB access$")
            self.assertNotIn("$", rows["a1"])

    def test_session_found_by_id_when_transcript_path_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100, 10, [("Bash", {"command": "ls"})])])
            rows, _ = fx.run(fx.payload([task("a1")], with_path=False))
            self.assertIn("🔧1", rows["a1"])

    def test_unknown_agents_bad_ids_and_garbage_never_break_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [], raw=b'not json\n{"type": "assistant", "message": "nope"}\n{"type":"assistant"\n')
            tasks = [task("a1"), task("missing"), task("../../etc/passwd"), {"id": "bare"}]
            rows, order = fx.run(fx.payload(tasks))
            self.assertEqual(order, ["a1", "missing", "../../etc/passwd", "bare"])
            self.assertNotIn("$", rows["a1"])
            self.assertNotIn("🔧", rows["missing"])

    def test_corrupt_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100, 10, [("Bash", {"command": "ls"})])])
            state = fx.tmp / "tmp" / f"claude-cockpit-{os.getuid()}" / "subagent-status"
            state.mkdir(parents=True, mode=0o700)
            (state / f"{SESSION}.json").write_text("{broken")
            rows, _ = fx.run(fx.payload([task("a1")]))
            self.assertIn("🔧1", rows["a1"])


    def test_state_of_the_wrong_shape_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100, 10, [("Bash", {"command": "ls"})])])
            state = fx.tmp / "tmp" / f"claude-cockpit-{os.getuid()}" / "subagent-status"
            state.mkdir(parents=True, mode=0o700)
            bad = {"v": 1, "dir": str(fx.session), "agents": {"a1": {"off": "x", "tools": []}}, "metas": []}
            (state / f"{SESSION}.json").write_text(json.dumps(bad))
            rows, _ = fx.run(fx.payload([task("a1")]))
            self.assertIn("🔧1", rows["a1"])


class IncrementalReadTests(unittest.TestCase):
    def test_second_render_reads_only_appended_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100, 30, [("Bash", {"command": "ls"})])])
            rows, _ = fx.run(fx.payload([task("a1")]))
            self.assertIn("🔧1", rows["a1"])
            fx.agent("a1", [assistant("m2", 100, 5, edits("/repo/x.py"))])
            rows, _ = fx.run(fx.payload([task("a1")]))
            self.assertIn("🔧2  ✎1", rows["a1"])
            cost = 2 * request_cost(OPUS, usage(1000, 100))
            self.assertIn(f"${cost:.2f}", rows["a1"])

    def test_state_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.agent("a1", [assistant("m1", 100, 10)])
            fx.run(fx.payload([task("a1")]))
            folder = fx.tmp / "tmp" / f"claude-cockpit-{os.getuid()}" / "subagent-status"
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertEqual((folder / f"{SESSION}.json").stat().st_mode & 0o077, 0)


class ReaderUnitTests(unittest.TestCase):
    def setUp(self):
        self.saved = agent_metrics.READ_BUDGET, agent_metrics.LONG_LINE

    def tearDown(self):
        agent_metrics.READ_BUDGET, agent_metrics.LONG_LINE = self.saved

    def write(self, tmp, lines):
        path = Path(tmp) / "agent-a1.jsonl"
        path.write_bytes(b"".join(lines))
        return path

    def test_budget_cut_marks_partial_then_catches_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [(json.dumps(assistant(f"m{i}", 10, 5, [("Bash", {"command": "ls"})])) + "\n").encode()
                     for i in range(20)]
            path = self.write(tmp, lines)
            entry = agent_metrics.new_entry()
            agent_metrics.READ_BUDGET = len(lines[0]) * 5 + 3
            agent_metrics.refresh(entry, path, agent_metrics.READ_BUDGET)
            self.assertEqual(entry["tools"], 5, "whole lines only; the cut line waits")
            self.assertTrue(entry["behind"])
            for _ in range(5):
                agent_metrics.refresh(entry, path, agent_metrics.READ_BUDGET)
            self.assertEqual(entry["tools"], 20)
            self.assertFalse(entry["behind"])

    def test_starved_budget_still_makes_progress(self):
        """An agent late in the read order may get only a few bytes; it still moves a line per render."""
        with tempfile.TemporaryDirectory() as tmp:
            lines = [(json.dumps(assistant(f"m{i}", 10, 5, [("Bash", {"command": "ls"})])) + "\n").encode()
                     for i in range(3)]
            path = self.write(tmp, lines)
            entry = agent_metrics.new_entry()
            for _ in range(3):
                agent_metrics.refresh(entry, path, 10)
            self.assertEqual(entry["tools"], 3)
            self.assertEqual(entry["off"], path.stat().st_size)

    def test_line_longer_than_long_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = (json.dumps(assistant("m1", 10, 5, [("Bash", {"command": "ls"})])) + "\n").encode()
            path = self.write(tmp, [good, b'{"type":"user","pad":"' + b"x" * 5000 + b'"}\n', good.replace(b"m1", b"m2")])
            agent_metrics.LONG_LINE = 1000
            entry = agent_metrics.new_entry()
            agent_metrics.refresh(entry, path, 10)   # starved: the skip must not need a full budget
            agent_metrics.refresh(entry, path, 10)
            for _ in range(4):
                agent_metrics.refresh(entry, path, 10)
            self.assertEqual(entry["tools"], 2)
            self.assertEqual(entry["off"], path.stat().st_size)

    def test_unfinished_last_line_waits_for_the_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            whole = (json.dumps(assistant("m1", 10, 5, [("Bash", {"command": "ls"})])) + "\n").encode()
            path = self.write(tmp, [whole[:40]])
            entry = agent_metrics.new_entry()
            agent_metrics.refresh(entry, path, agent_metrics.READ_BUDGET)
            self.assertEqual((entry["off"], entry["tools"]), (0, 0))
            path.write_bytes(whole)
            agent_metrics.refresh(entry, path, agent_metrics.READ_BUDGET)
            self.assertEqual(entry["tools"], 1)

    def test_huge_transcript_is_read_from_near_its_end_and_marked_partial(self):
        saved = agent_metrics.FIRST_READ_MAX
        self.addCleanup(setattr, agent_metrics, "FIRST_READ_MAX", saved)
        with tempfile.TemporaryDirectory() as tmp:
            lines = [(json.dumps(assistant(f"m{i}", 10, 5, [("Bash", {"command": "ls"})])) + "\n").encode()
                     for i in range(10)]
            path = self.write(tmp, lines)
            agent_metrics.FIRST_READ_MAX = len(lines[0]) * 3 + 7    # starts inside the 4th line from the end
            entry = agent_metrics.new_entry()
            agent_metrics.refresh(entry, path, agent_metrics.READ_BUDGET)
            self.assertEqual(entry["tools"], 3, "the cut line is skipped, the last three are read")
            self.assertTrue(entry["partial"])
            self.assertFalse(agent_metrics._caught_up(entry))

    def test_counts_match_cockpit_team_reader(self):
        """The plugin can't import cockpit.team, so check its reader agrees with team.TranscriptTail."""
        from cockpit.team import TranscriptTail
        records = [assistant("m1", 5, 50, edits("/a.py")), assistant("m1", 900, 49, edits("/b.py")), user(48),
                   assistant("m2", 300, 40, [("Write", {"file_path": "/a.py"}), ("Grep", {"pattern": "x"})]),
                   assistant("m3", 70, 30, [("NotebookEdit", {"notebook_path": "/n.ipynb"})], model="claude-sonnet-5-5"),
                   assistant("m4", 10, 20, model="claude-unknown-9")]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [(json.dumps(r) + "\n").encode() for r in records] + [b"garbage\n"])
            team = TranscriptTail(path, "a1").refresh()
            entry = agent_metrics.new_entry()
            agent_metrics.refresh(entry, path, agent_metrics.READ_BUDGET)
            self.assertAlmostEqual(entry["cost"], team.cost_usd, places=9)
            self.assertEqual((entry["tools"], len(entry["edited"]), entry["known"]),
                             (team.tool_calls, len(team.edited), team.cost_known))

    def test_peak_parallel(self):
        self.assertEqual(agent_metrics.peak_parallel([]), 0)
        self.assertEqual(agent_metrics.peak_parallel([(0, 10), (5, 15), (12, 20)]), 2)
        self.assertEqual(agent_metrics.peak_parallel([(0, 10), (10, 20)]), 1, "back to back is not parallel")
        self.assertEqual(agent_metrics.peak_parallel([(0, 30), (1, 29), (2, 28)]), 3)

    def test_vendored_pricing_matches_cockpit_pricing(self):
        """The plugin ships without lib/, so it carries a copy of pricing.py; keep them identical."""
        self.assertEqual((SCRIPTS / "cockpit_pricing.py").read_bytes(),
                         (ROOT / "lib" / "cockpit" / "pricing.py").read_bytes(),
                         "copy lib/cockpit/pricing.py to plugins/orchestrator/scripts/cockpit_pricing.py")


if __name__ == "__main__":
    unittest.main()
