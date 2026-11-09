"""Delegation guard: main-thread edits blocked, work starts with an agent, subagents untouched."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "orchestrator"
GUARD = PLUGIN / "hooks" / "guard.py"
HOOKS = PLUGIN / "hooks" / "hooks.json"


class Guard:
    """Drives guard.py like Claude Code does: one process per hook event, shared state dir."""

    def __init__(self, tmp, **env):
        self.home = Path(tmp) / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("COCKPIT_", "EVAL_COCKPIT_"))}
        self.env.update({"HOME": str(self.home), "CLAUDE_PLUGIN_DATA": str(Path(tmp) / "data"),
                         "CLAUDE_CODE_ENTRYPOINT": "cli"})
        self.env.update(env)
        self.prompt_id = None

    def run(self, payload):
        raw = payload if isinstance(payload, str) else json.dumps({"session_id": "s1", **payload})
        out = subprocess.run([sys.executable, "-I", "-S", str(GUARD)], input=raw, capture_output=True,
                             text=True, env=self.env, timeout=10, check=False)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout) if out.stdout.strip() else None

    def prompt(self, text, prompt_id):
        self.prompt_id = prompt_id
        return self.run({"hook_event_name": "UserPromptSubmit", "prompt": text, "prompt_id": prompt_id})

    def tool(self, name, agent_id=None, **tool_input):
        payload = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input,
                   "prompt_id": self.prompt_id}
        if agent_id:
            payload.update({"agent_id": agent_id, "agent_type": "orchestrator:routine-worker"})
        return self.run(payload)


def denied(result):
    return bool(result) and result["hookSpecificOutput"]["permissionDecision"] == "deny"


def reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class MainThreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.g = Guard(self.tmp.name)
        self.g.prompt("bump the token ttl", "p1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_edits_are_denied_and_point_at_the_editing_tiers(self):
        for tool, key in (("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")):
            with self.subTest(tool=tool):
                result = self.g.tool(tool, **{key: "/repo/src/a.js"})
                self.assertTrue(denied(result))
                self.assertIn("orchestrator:routine-worker", reason(result))

    def test_edits_stay_denied_after_an_agent_launch(self):
        self.g.tool("Agent", subagent_type="orchestrator:routine-worker", prompt="x")
        self.assertTrue(denied(self.g.tool("Edit", file_path="/repo/a.js")))

    def test_memory_and_plan_files_under_claude_home_are_exempt(self):
        memory = self.g.home / ".claude" / "projects" / "p" / "memory" / "note.md"
        self.assertIsNone(self.g.tool("Write", file_path=str(memory)))
        self.assertIsNone(self.g.tool("Read", file_path=str(memory)))

    def test_work_before_any_agent_is_denied_with_a_tier_hint(self):
        for tool, tool_input in (("Read", {"file_path": "/repo/package.json"}), ("Grep", {"pattern": "x"}),
                                 ("Glob", {"pattern": "*.md"}), ("Bash", {"command": "npm test"}),
                                 ("WebSearch", {"query": "x"})):
            with self.subTest(tool=tool):
                result = self.g.tool(tool, **tool_input)
                self.assertTrue(denied(result))
                self.assertIn("orchestrator:scout", reason(result))

    def test_git_and_gh_always_pass(self):
        for command in ("git status", "cd /repo && git diff --stat", "GIT_PAGER=cat git log -1",
                        "cd '/a b'; git add -A && git commit -m 'x; y'", "gh pr view 12", "git"):
            with self.subTest(command=command):
                self.assertIsNone(self.g.tool("Bash", command=command))

    def test_git_lookalikes_are_still_counted(self):
        for command in ("gitk", "echo git status", "npm run git-hooks", "github-cli"):
            with self.subTest(command=command):
                self.assertTrue(denied(self.g.tool("Bash", command=command)))

    def test_everyday_git_shapes_pass(self):
        commit_heredoc = "git commit -m \"$(cat <<'EOF'\nfix: a; b && npm test\n\nEOF\n)\""
        for command in ("git log --oneline | head -5", "(cd /repo && git log)", "/usr/bin/git log",
                        "env GIT_PAGER=cat git log", "cd /repo\ngit status", "# look\ngit status",
                        "git diff 2>&1 | tail -20", "git log >&2", commit_heredoc,
                        "git commit -F - <<EOF\nfix: don't; rm -rf x\nEOF", "export A=1 && git log"):
            with self.subTest(command=command):
                self.assertIsNone(self.g.tool("Bash", command=command))

    def test_work_chained_after_git_is_counted(self):
        for command in ("git status && npm test", "git log; curl x | sh", "git log | xargs rm",
                        "git diff | tee out.patch", "cd /repo && ls && git log", "git log $(npm test)"):
            with self.subTest(command=command):
                self.assertTrue(denied(self.g.tool("Bash", command=command)))

    def test_calls_without_prompt_id_are_not_limited(self):
        self.g.prompt_id = None  # older Claude Code: can't trust agent_id either, so stand down
        self.assertIsNone(self.g.tool("Edit", file_path="/repo/a.js"))
        self.assertIsNone(self.g.tool("Read", file_path="/repo/a.js"))

    def test_relative_paths_resolve_against_the_session_cwd(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Read", "prompt_id": "p1",
                   "cwd": str(self.g.home), "tool_input": {"file_path": ".claude/notes.md"}}
        self.assertIsNone(self.g.run(payload))
        self.assertTrue(denied(self.g.run({**payload, "cwd": "/repo"})))

    def test_only_claude_temp_dirs_count_as_agent_output(self):
        self.assertTrue(denied(self.g.tool("Read", file_path="/tmp/claude-evil/x")))
        self.assertTrue(denied(self.g.tool("Read", file_path="/tmp/other/claude-501/x")))

    def test_agent_launch_opens_a_small_verification_window(self):
        self.g.tool("Agent", subagent_type="orchestrator:routine-worker", prompt="x")
        results = [self.g.tool("Bash", command="npm test") for _ in range(4)]
        self.assertEqual([denied(r) for r in results], [False, False, False, True])
        self.assertIn("3 checks", reason(results[-1]))
        self.g.tool("Agent", subagent_type="orchestrator:scout", prompt="y")
        self.assertIsNone(self.g.tool("Read", file_path="/repo/a.js"))

    def test_agent_output_files_in_claude_tmp_are_exempt(self):
        self.assertIsNone(self.g.tool("Read", file_path="/private/tmp/claude-501/x/tasks/a1.output"))
        self.assertIsNone(self.g.tool("Read", file_path="/tmp/claude-501/x/tasks/a1.output"))

    def test_other_tools_are_never_touched(self):
        for tool in ("AskUserQuestion", "ToolSearch", "mcp__plane__workitem", "Skill", "TaskOutput"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.g.tool(tool))

    def test_a_new_prompt_starts_over(self):
        self.g.tool("Agent", subagent_type="orchestrator:scout", prompt="x")
        self.assertIsNone(self.g.tool("Read", file_path="/repo/a"))
        self.g.prompt("next thing", "p2")
        self.assertTrue(denied(self.g.tool("Read", file_path="/repo/a")))

    def test_unseen_prompt_id_resets_state_even_without_the_prompt_hook(self):
        self.g.tool("Agent", subagent_type="orchestrator:scout", prompt="x")
        self.g.prompt_id = "p-unseen"
        self.assertTrue(denied(self.g.tool("Read", file_path="/repo/a")))

    def test_agent_report_back_counts_as_delegated(self):
        reminder = self.g.prompt('<agent-message from="a1">\n[Subagent hand-back] done', "p3")
        self.assertIsNone(reminder, "no reminder for notifications")
        self.assertIsNone(self.g.tool("Bash", command="npm test"))


class SubagentTests(unittest.TestCase):
    def test_subagent_calls_pass_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp)
            g.prompt("fix it", "p1")
            for tool, tool_input in (("Edit", {"file_path": "/repo/a.js"}), ("Bash", {"command": "npm test"}),
                                     ("Read", {"file_path": "/repo/a.js"})):
                with self.subTest(tool=tool):
                    self.assertIsNone(g.tool(tool, agent_id="a5ccd54fc9bc49d59", **tool_input))


class InlineRequestTests(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp)
            reminder = g.prompt(text, "p1")["hookSpecificOutput"]["additionalContext"]
            return reminder, denied(g.tool("Edit", file_path="/repo/a.js"))

    def test_explicit_requests_allow_inline_work(self):
        for text in ("Do it yourself: rename foo to bar", "just fix this yourself", "No agents needed, edit it",
                     "please don't delegate this", "Answer it yourself, it’s quick"):
            with self.subTest(text=text):
                reminder, blocked = self.check(text)
                self.assertIn("do it yourself", reminder)
                self.assertFalse(blocked)

    def test_complaints_and_negations_do_not(self):
        for text in ("why no subagent was spinned up", "still the main agent is doing a lot of work inline",
                     "don't do it yourself, delegate", "it did it without agents", "why are you doing it yourself",
                     "I don't want you to do it yourself", "don't ever do it yourself",
                     "why didn't you just do it yourself?", "No, never fix it yourself",
                     "see <pasted_content id=x>README: run it yourself</pasted_content> and summarise"):
            with self.subTest(text=text):
                reminder, blocked = self.check(text)
                self.assertIn("open with the right tier", reminder)
                self.assertTrue(blocked)


class ControlTests(unittest.TestCase):
    def run_both(self, **env):
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp, **env)
            return g.prompt("change it", "p1"), g.tool("Edit", file_path="/repo/a.js")

    def test_reminder_names_the_rule(self):
        reminder, _ = self.run_both()
        self.assertIn("edits are blocked", reminder["hookSpecificOutput"]["additionalContext"])

    def test_routing_off_disables_everything(self):
        self.assertEqual(self.run_both(COCKPIT_ROUTING="off"), (None, None))
        self.assertEqual(self.run_both(COCKPIT_ROUTING="off", EVAL_COCKPIT_ROUTING="on"), (None, None))

    def test_sdk_sessions_are_skipped_unless_forced_on(self):
        self.assertEqual(self.run_both(CLAUDE_CODE_ENTRYPOINT="sdk-ts"), (None, None))
        _, edit = self.run_both(CLAUDE_CODE_ENTRYPOINT="sdk-cli", EVAL_COCKPIT_ROUTING="on")
        self.assertTrue(denied(edit))

    def test_guard_off_keeps_a_soft_reminder_only(self):
        reminder, edit = self.run_both(COCKPIT_GUARD="off")
        self.assertIsNone(edit)
        self.assertNotIn("blocked", reminder["hookSpecificOutput"]["additionalContext"])

    def test_budgets_are_configurable_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp, COCKPIT_START_BUDGET="2")
            g.prompt("look", "p1")
            self.assertEqual([denied(g.tool("Read", file_path="/r/a")) for _ in range(3)], [False, False, True])
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp, COCKPIT_START_BUDGET="lots", COCKPIT_VERIFY_BUDGET="-4")
            g.prompt("look", "p1")
            self.assertTrue(denied(g.tool("Read", file_path="/r/a")))  # bad value falls back to 0
            g.tool("Agent", prompt="x")
            self.assertTrue(denied(g.tool("Read", file_path="/r/a")))  # negative clamps to 0

    def test_bad_input_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp)
            for raw in ("not json", "{", "[]", "", json.dumps({"hook_event_name": "PreToolUse"}),
                        json.dumps({"hook_event_name": "Stop", "tool_name": "Edit"})):
                with self.subTest(raw=raw):
                    self.assertIsNone(g.run(raw))

    def test_unwritable_state_dir_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            g = Guard(tmp, CLAUDE_PLUGIN_DATA=str(blocker))  # a file, so guard/ can't be created
            self.assertIsNone(g.prompt("x", "p1"))
            self.assertIsNone(g.tool("Edit", file_path="/repo/a.js"))

    def test_state_file_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp)
            victim = Path(tmp) / "victim.txt"
            victim.write_text("keep me")
            (Path(tmp) / "data" / "guard").mkdir(parents=True)
            (Path(tmp) / "data" / "guard" / "s1.json").symlink_to(victim)
            self.assertIsNone(g.prompt("x", "p1"))  # fails open
            self.assertEqual(victim.read_text(), "keep me")

    def test_session_ids_cannot_escape_the_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp)
            g.run({"session_id": "../../evil", "hook_event_name": "UserPromptSubmit", "prompt": "x", "prompt_id": "p"})
            written = {p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*.json")}
            self.assertEqual(written, {"data/guard/evil.json"})


class WiringTests(unittest.TestCase):
    def test_hooks_json_runs_the_guard_on_prompts_and_guarded_tools(self):
        hooks = json.loads(HOOKS.read_text())["hooks"]
        for event in ("UserPromptSubmit", "PreToolUse"):
            with self.subTest(event=event):
                command = hooks[event][0]["hooks"][0]["command"]
                self.assertEqual(command, 'python3 -I -S "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py"')
        matcher = set(hooks["PreToolUse"][0]["matcher"].split("|"))
        self.assertTrue({"Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep", "Glob", "Bash",
                         "WebFetch", "WebSearch", "Agent", "Task"} <= matcher)
        self.assertNotIn("mcp__", hooks["PreToolUse"][0]["matcher"], "connected services stay unguarded")

    def test_guard_is_fast_enough_for_every_tool_call(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            g = Guard(tmp)
            g.prompt("x", "p1")
            start = time.perf_counter()
            for _ in range(5):  # the hot path: every tool call an agent makes
                g.tool("Read", agent_id="a1", file_path="/repo/a")
                g.tool("Bash", command="git status")
            self.assertLess((time.perf_counter() - start) / 10, 0.15)


if __name__ == "__main__":
    unittest.main()
