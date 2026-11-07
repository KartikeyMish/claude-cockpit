"""Team view: transcript parsing, incremental tailing, agent tree, keys, and a pty run."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))  # for the shared pty driver in test_tui

from cockpit import cli  # noqa: E402
from cockpit.core import Session  # noqa: E402
from cockpit.team import Accumulator, TeamWatcher, TranscriptTail, summarize_tool  # noqa: E402
from cockpit.team_view import (  # noqa: E402
    TeamState, agent_label, fmt_tokens, handle_team_key, timeline_bar,
)
from cockpit.tui import Snapshot, UiState, handle_key  # noqa: E402

T0 = "2026-11-07T10:00:00.000Z"


def assistant(ts, *blocks, usage=None, model="claude-opus-5-5", effort="high"):
    return {"type": "assistant", "timestamp": ts, "effort": effort,
            "message": {"model": model, "content": list(blocks),
                        "usage": usage or {"input_tokens": 5, "cache_read_input_tokens": 995, "output_tokens": 50}}}


def tool(name, tool_id="t1", **data):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": data}


def write_jsonl(path, records, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode) as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class SummarizeToolTests(unittest.TestCase):
    def test_human_summaries(self):
        self.assertEqual(summarize_tool("Read", {"file_path": "/a/b/token.js"}), "Read token.js")
        self.assertEqual(summarize_tool("Grep", {"pattern": "verifyToken", "path": "src/"}), "Grep verifyToken src/")
        self.assertEqual(summarize_tool("Bash", {"command": "npm  test\n --x"}), "Bash npm test --x")
        self.assertEqual(summarize_tool("Agent", {"subagent_type": "orchestrator:scout", "description": "Map auth"}),
                         "Agent orchestrator:scout: Map auth")
        self.assertEqual(summarize_tool("Mystery", "nope"), "Mystery")
        self.assertLessEqual(len(summarize_tool("Bash", {"command": "x" * 500})), 120)


class AccumulatorTests(unittest.TestCase):
    def test_totals_now_report_and_spawns(self):
        acc = Accumulator()
        acc.feed({"type": "user", "timestamp": T0}, "a1")
        acc.feed(assistant("2026-11-07T10:00:05Z", tool("Grep", "g1", pattern="x")), "a1")
        acc.feed(assistant("2026-11-07T10:00:09Z", tool("Agent", "spawn1", subagent_type="orchestrator:scout"),
                           usage={"input_tokens": 10, "cache_creation_input_tokens": 1990, "output_tokens": 70}), "a1")
        acc.feed(assistant("2026-11-07T10:00:12Z", {"type": "text", "text": "  All done.  "}), "a1")
        self.assertEqual(acc.tool_calls, 2)
        self.assertEqual(acc.output_tokens, 170)
        self.assertEqual(acc.context_tokens, 1000)  # last usage-bearing record
        self.assertEqual(acc.now, "Agent orchestrator:scout:")
        self.assertEqual(acc.report, "All done.")
        self.assertEqual(acc.spawned, {"spawn1": "a1"})
        self.assertEqual(acc.last_activity - acc.started, 12)
        self.assertEqual((acc.model, acc.effort), ("claude-opus-5-5", "high"))

    def test_ignores_malformed_records(self):
        acc = Accumulator()
        acc.feed({"type": "assistant", "message": "not a dict"}, "x")
        acc.feed({"type": "assistant", "message": {"content": ["str", {"type": "tool_use"}]}}, "x")
        self.assertEqual(acc.tool_calls, 1)


class TranscriptTailTests(unittest.TestCase):
    def test_reads_only_new_complete_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            write_jsonl(path, [assistant(T0, tool("Read", file_path="a"))])
            tail = TranscriptTail(path, "a")
            self.assertEqual(tail.refresh().tool_calls, 1)
            with path.open("a") as handle:
                handle.write(json.dumps(assistant(T0, tool("Read", file_path="b")))[:30])  # half a line
            self.assertEqual(tail.refresh().tool_calls, 1)
            with path.open("a") as handle:
                handle.write(json.dumps(assistant(T0, tool("Read", file_path="b")))[30:] + "\n")
            self.assertEqual(tail.refresh().tool_calls, 2)
            self.assertEqual(tail.refresh().tool_calls, 2)  # nothing new: no double counting

    def test_truncated_file_starts_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            write_jsonl(path, [assistant(T0, tool("Read")), assistant(T0, tool("Read"))])
            tail = TranscriptTail(path, "a")
            tail.refresh()
            write_jsonl(path, [assistant(T0, tool("Bash", command="ls"))])
            acc = tail.refresh()
            self.assertEqual((acc.tool_calls, acc.now), (1, "Bash ls"))

    def test_missing_file_is_empty(self):
        self.assertEqual(TranscriptTail(Path("/nonexistent.jsonl"), "a").refresh().tool_calls, 0)


def build_session(projects: Path, session_id: str = "sess-1") -> Path:
    base = projects / "-Users-me-repo" / session_id
    write_jsonl(base.with_suffix(".jsonl"), [
        {"type": "user", "timestamp": T0},
        assistant("2026-11-07T10:00:01Z", tool("Agent", "spawn-a", subagent_type="orchestrator:senior-engineer",
                                                 description="Add status column"), effort="xhigh"),
    ])
    subs = base / "subagents"
    write_jsonl(subs / "agent-aaa.jsonl", [
        assistant("2026-11-07T10:00:02Z", tool("Read", "r1", file_path="/x/orders.js")),
        assistant("2026-11-07T10:00:04Z", tool("Agent", "spawn-b", subagent_type="orchestrator:scout")),
        assistant("2026-11-07T10:01:00Z", {"type": "text", "text": "Migration added and tested."}),
    ])
    (subs / "agent-aaa.meta.json").write_text(json.dumps({
        "agentType": "orchestrator:senior-engineer", "description": "Add status column",
        "toolUseId": "spawn-a", "spawnDepth": 1, "finished": True}))
    write_jsonl(subs / "agent-bbb.jsonl", [
        assistant("2026-11-07T10:00:05Z", tool("Grep", "g1", pattern="status", path="src/"),
                  model="claude-sonnet-5-5", effort="medium"),
    ])
    (subs / "agent-bbb.meta.json").write_text(json.dumps({
        "agentType": "orchestrator:scout", "description": "Find status readers",
        "toolUseId": "spawn-b", "spawnDepth": 2}))
    (subs / "agent-ccc.meta.json").write_text("{broken")
    return base


class TeamWatcherTests(unittest.TestCase):
    def test_builds_nested_tree_with_states_and_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_session(Path(tmp))
            team = TeamWatcher("sess-1", Path(tmp)).snapshot()
            self.assertTrue(team.found)
            self.assertEqual(team.main.effort, "xhigh")
            ordered = [(a.agent_id, prefix) for a, prefix in team.ordered()]
            self.assertEqual(ordered[:2], [("aaa", "├─ "), ("bbb", "│  └─ ")])
            by_id = {a.agent_id: a for a in team.agents}
            self.assertTrue(by_id["aaa"].finished)
            self.assertEqual(by_id["aaa"].report, "Migration added and tested.")
            self.assertEqual(by_id["bbb"].parent_id, "aaa")
            self.assertEqual(by_id["bbb"].model, "claude-sonnet-5-5")
            self.assertEqual(by_id["ccc"].agent_type, "agent")  # broken meta tolerated
            self.assertEqual(team.feed[0].summary, "Grep status src/")  # newest first

    def test_unknown_session_is_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            team = TeamWatcher("nope", Path(tmp)).snapshot()
            self.assertFalse(team.found)
            self.assertEqual(team.ordered(), [])


class TeamViewHelperTests(unittest.TestCase):
    def test_timeline_bar(self):
        self.assertEqual(timeline_bar(None, None, False, 0, 100, 5), "·····")
        self.assertEqual(timeline_bar(0, 100, False, 0, 100, 5), "█████")
        self.assertEqual(timeline_bar(50, None, True, 0, 100, 10), "·····████▶")
        self.assertEqual(len(timeline_bar(-500, 20, False, 0, 100, 10)), 10)
        self.assertEqual(timeline_bar(0, 10, False, 0, 100, 0), "")

    def test_labels_and_tokens(self):
        self.assertEqual(agent_label("orchestrator:debugger"), ("debugger", "debugger"))
        self.assertEqual(agent_label("code-reviewer"), ("", "code-reviewer"))
        self.assertEqual(agent_label("other:scout"), ("", "scout"))
        self.assertEqual([fmt_tokens(n) for n in (900, 2100, 54000, 1_500_000)], ["900", "2.1k", "54k", "1.5M"])

    def test_team_keys(self):
        state, action = handle_team_key(TeamState(), "j", 3)
        self.assertEqual((state.selected, action), (1, None))
        state, _ = handle_team_key(state, "\n", 3)
        self.assertTrue(state.detail)
        state, action = handle_team_key(state, "KEY_LEFT", 3)
        self.assertEqual((state.detail, action), (False, None))  # first back closes details
        self.assertEqual(handle_team_key(state, "KEY_LEFT", 3)[1], ("back",))
        self.assertEqual(handle_team_key(state, "q", 3)[1], ("quit",))
        self.assertFalse(handle_team_key(TeamState(), "\n", 0)[0].detail)


def session(pid, status="idle", sid="s", name="n", idle_ms=1000):
    return Session(pid=pid, name=name, status=status, entrypoint="cli", cwd="/x",
                   status_updated_ms=10_000_000 - idle_ms, rss_kb=1, tree_rss_kb=1, session_id=sid)


class PickAndOpenTests(unittest.TestCase):
    def test_pick_session(self):
        sessions = [session(1, name="web@main", idle_ms=50_000), session(2, status="busy", name="api@feat"),
                    session(3, sid=None, name="new")]
        self.assertEqual(cli.pick_session(sessions, None, 10_000_000).pid, 2)
        self.assertEqual(cli.pick_session(sessions, "WEB", 10_000_000).pid, 1)
        self.assertEqual(cli.pick_session(sessions, "1", 10_000_000).pid, 1)
        self.assertIsNone(cli.pick_session(sessions, "zzz", 10_000_000))

    def test_dashboard_v_opens_team_for_selected_session(self):
        rows = [session(7, sid="abc")]
        _, action = handle_key(UiState(), "v", rows, Snapshot(sessions=tuple(rows)))
        self.assertEqual(action, ("team", 7))
        state, action = handle_key(UiState(), "v", [session(8, sid=None)], Snapshot())
        self.assertIsNone(action)
        self.assertIn("no id", state.message)


@unittest.skipUnless(sys.platform != "win32" and shutil.which("sleep"), "needs POSIX")
class TeamViewPtyTests(unittest.TestCase):
    """Dashboard → v → team view → back → quit, against a fake Claude home."""

    def test_team_view_end_to_end(self):
        from test_tui import DashboardSmokeTests  # reuse the pty driver

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # A real `sleep` whose argv[0] says claude (copied binaries get killed by code signing).
            fake = home / "bin" / "claude"
            proc = subprocess.Popen(["bash", "-c", f'exec -a "{fake}" sleep 30'])
            try:
                time.sleep(0.3)
                build_session(home / "projects", "sess-live")
                (home / "sessions").mkdir()
                (home / "sessions" / f"{proc.pid}.json").write_text(json.dumps({
                    "pid": proc.pid, "sessionId": "sess-live", "cwd": "/Users/me/repo", "entrypoint": "cli",
                    "name": "fixture-session", "status": "busy", "startedAt": int(time.time() * 1000),
                    "statusUpdatedAt": int(time.time() * 1000)}))
                os.environ["CLAUDE_CONFIG_DIR"] = str(home)
                try:
                    driver = DashboardSmokeTests()
                    # Only keys that are harmless if a screen fails to open: never ⏎ (it launches Claude).
                    code, screen = driver.run_dashboard(["v", "l", "h", "h", "q"], timeout=12)
                finally:
                    del os.environ["CLAUDE_CONFIG_DIR"]
            finally:
                proc.kill()
                proc.wait()
        self.assertEqual(code, 0)
        for text in ("fixture-session", "team", "senior-engineer", "Add status column", "TIMELINE",
                     "DETAILS", "Migration added"):
            self.assertIn(text, screen)


if __name__ == "__main__":
    unittest.main()
