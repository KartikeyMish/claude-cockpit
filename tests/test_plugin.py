"""Static and behavioural checks for the orchestrator plugin.

Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "orchestrator"
AGENTS = PLUGIN / "agents"
HOOK = PLUGIN / "hooks" / "session-start.sh"
POLICY = PLUGIN / "hooks" / "routing-policy.md"

# The tier map this repo promises. Changing it is a deliberate API change.
EXPECTED_TIERS = {
    "scout": ("sonnet", "medium"),
    "routine-worker": ("opus", "medium"),
    "senior-engineer": ("opus", "high"),
    "debugger": ("opus", "xhigh"),
    "architect": ("opus", "max"),
}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "Bash"}
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
VALID_COLORS = {"red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan"}
# SessionStart additionalContext is capped at 10,000 chars; stay well under it
# because this text is paid for in every session.
POLICY_BUDGET_CHARS = 4500


def parse_frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        raise AssertionError(f"{path.name}: missing YAML frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def split_tools(value):
    return {t.strip() for t in value.split(",") if t.strip()}


def run_hook(env_overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("COCKPIT_", "EVAL_COCKPIT_"))}
    env.update({"CLAUDE_PLUGIN_ROOT": str(PLUGIN)})
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(HOOK)],
        input='{"hook_event_name":"SessionStart","source":"startup"}',
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )


class AgentDefinitionTests(unittest.TestCase):
    def test_exactly_the_five_tiers_exist(self):
        names = {p.stem for p in AGENTS.glob("*.md")}
        self.assertEqual(names, set(EXPECTED_TIERS))

    def test_each_tier_has_model_effort_and_matching_name(self):
        for name, (model, effort) in EXPECTED_TIERS.items():
            with self.subTest(agent=name):
                fields, body = parse_frontmatter(AGENTS / f"{name}.md")
                self.assertEqual(fields.get("name"), name)
                self.assertEqual(fields.get("model"), model)
                self.assertEqual(fields.get("effort"), effort)
                self.assertIn(fields["effort"], VALID_EFFORTS)
                self.assertIn(fields.get("color"), VALID_COLORS)
                self.assertGreater(len(fields.get("description", "")), 80)
                self.assertGreater(len(body.strip()), 400, "system prompt too thin")

    def test_scout_is_strictly_read_only(self):
        fields, _ = parse_frontmatter(AGENTS / "scout.md")
        self.assertIn("tools", fields, "scout must use an allowlist, not inherit tools")
        self.assertFalse(split_tools(fields["tools"]) & WRITE_TOOLS)

    def test_architect_is_read_only_by_allowlist(self):
        fields, _ = parse_frontmatter(AGENTS / "architect.md")
        tools = split_tools(fields["tools"])
        self.assertFalse(tools & (WRITE_TOOLS | {"Agent"}))

    def test_no_stacked_delegation_and_bounded_runs(self):
        for name in EXPECTED_TIERS:
            with self.subTest(agent=name):
                fields, body = parse_frontmatter(AGENTS / f"{name}.md")
                if "tools" in fields:
                    self.assertNotIn("Agent", split_tools(fields["tools"]))
                else:
                    self.assertIn("Agent", split_tools(fields.get("disallowedTools", "")))
                self.assertTrue(10 <= int(fields["maxTurns"]) <= 200)
                self.assertRegex(body, r"under \d+ words")

    def test_policy_table_matches_agent_frontmatter(self):
        policy = POLICY.read_text(encoding="utf-8")
        for name, (model, effort) in EXPECTED_TIERS.items():
            with self.subTest(agent=name):
                row = next(line for line in policy.splitlines() if f"`orchestrator:{name}`" in line)
                self.assertIn(f"{model.capitalize()} · {effort}", row)

    def test_hook_skips_resume_and_fork(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        self.assertEqual(hooks["hooks"]["SessionStart"][0]["matcher"], "startup|clear|compact")

    def test_colors_are_distinct(self):
        colors = [parse_frontmatter(AGENTS / f"{n}.md")[0]["color"] for n in EXPECTED_TIERS]
        self.assertEqual(len(colors), len(set(colors)))


class ManifestTests(unittest.TestCase):
    def test_plugin_manifest(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "orchestrator")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_marketplace_lists_the_plugin(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(market["name"], "claude-cockpit")
        self.assertIn("name", market["owner"])
        sources = {p["name"]: p["source"] for p in market["plugins"]}
        self.assertEqual(sources.get("orchestrator"), "./plugins/orchestrator")

    def test_marketplace_and_plugin_versions_match(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        entry = next(p for p in market["plugins"] if p["name"] == "orchestrator")
        self.assertEqual(entry.get("version"), manifest["version"])

    def test_hooks_json_points_at_executable_script(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        commands = [
            h["command"]
            for group in hooks["hooks"]["SessionStart"]
            for h in group["hooks"]
        ]
        self.assertEqual(len(commands), 1)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh", commands[0])
        self.assertTrue(os.access(HOOK, os.X_OK), "session-start.sh must be executable")


class RoutingHookTests(unittest.TestCase):
    def test_interactive_session_gets_policy_naming_every_tier(self):
        result = run_hook({"CLAUDE_CODE_ENTRYPOINT": "cli"})
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in EXPECTED_TIERS:
            self.assertIn(f"orchestrator:{name}", result.stdout)

    def test_policy_fits_budget(self):
        self.assertLess(len(POLICY.read_text(encoding="utf-8")), POLICY_BUDGET_CHARS)

    def test_sdk_sessions_are_skipped_by_default(self):
        for entry in ("sdk-ts", "sdk-py", "sdk-cli"):
            with self.subTest(entrypoint=entry):
                result = run_hook({"CLAUDE_CODE_ENTRYPOINT": entry})
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")

    def test_opt_out_env_var(self):
        result = run_hook({"CLAUDE_CODE_ENTRYPOINT": "cli", "COCKPIT_ROUTING": "off"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_force_on_for_sdk(self):
        result = run_hook({"CLAUDE_CODE_ENTRYPOINT": "sdk-ts", "COCKPIT_ROUTING": "on"})
        self.assertIn("orchestrator:scout", result.stdout)

    def test_eval_override_turns_routing_on_for_sdk(self):
        result = run_hook({"CLAUDE_CODE_ENTRYPOINT": "sdk-ts", "EVAL_COCKPIT_ROUTING": "on"})
        self.assertIn("orchestrator:architect", result.stdout)

    def test_cockpit_routing_beats_eval_override(self):
        result = run_hook({"CLAUDE_CODE_ENTRYPOINT": "cli", "COCKPIT_ROUTING": "off",
                           "EVAL_COCKPIT_ROUTING": "on"})
        self.assertEqual(result.stdout.strip(), "")

    def test_missing_policy_file_fails_soft(self):
        result = run_hook({"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_PLUGIN_ROOT": "/nonexistent"})
        self.assertEqual(result.returncode, 0, "a broken hook must never block session start")
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("routing policy not found", result.stderr)


STATUS_SCRIPT = PLUGIN / "scripts" / "subagent-status.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run_status(payload):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    out = subprocess.run([sys.executable, "-I", "-S", str(STATUS_SCRIPT)], input=data,
                         capture_output=True, text=True, timeout=10, check=False)
    return out.returncode, [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


class SubagentStatusLineTests(unittest.TestCase):
    def test_plugin_settings_wire_the_script(self):
        settings = json.loads((PLUGIN / "settings.json").read_text())
        command = settings["subagentStatusLine"]["command"]
        self.assertEqual(settings["subagentStatusLine"]["type"], "command")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/subagent-status.py", command)
        self.assertEqual(set(settings), {"subagentStatusLine"}, "plugins may only set agent/subagentStatusLine")

    def test_one_row_per_task_with_tier_styling_and_width_limit(self):
        tasks = [
            {"id": "a", "agentType": "orchestrator:debugger", "status": "running", "effort": "xhigh",
             "model": "claude-opus-5-5", "label": "Bisecting " + "x" * 200, "startTime": 0,
             "tokenCount": 12000, "tokenSamples": [1000, 5000, 12000], "contextWindowSize": 200000},
            {"id": "b", "agentType": "Explore", "status": "completed", "description": "find files"},
            {"agentType": "orchestrator:scout"},  # no id: must be skipped, not crash
        ]
        code, rows = run_status({"columns": 80, "tasks": tasks})
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in rows], ["a", "b"])
        plain = ANSI.sub("", rows[0]["content"])
        self.assertIn("✦ debugger", plain)
        self.assertIn("opus·xhigh", plain)
        self.assertIn("6% ctx", plain)
        self.assertLessEqual(len(plain), 80)
        self.assertTrue(ANSI.sub("", rows[1]["content"]).startswith("✓"))

    def test_bad_input_prints_nothing(self):
        for payload in ("not json", "[]", json.dumps({"tasks": "nope"})):
            with self.subTest(payload=payload):
                code, rows = run_status(payload)
                self.assertEqual((code, rows), (0, []))


class ThemeTests(unittest.TestCase):
    def test_cockpit_theme_matches_tier_colours(self):
        theme = json.loads((PLUGIN / "themes" / "cockpit.json").read_text())
        self.assertEqual(theme["base"], "dark")
        for colour in ("cyan", "green", "blue", "red", "purple"):
            self.assertRegex(theme["overrides"][f"{colour}_FOR_SUBAGENTS_ONLY"], r"^#[0-9a-f]{6}$")


class HookDataDirTests(unittest.TestCase):
    def test_hook_creates_plugin_data_dir_even_when_routing_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            run_hook({"CLAUDE_CODE_ENTRYPOINT": "cli", "COCKPIT_ROUTING": "off", "CLAUDE_PLUGIN_DATA": str(data)})
            self.assertTrue(data.is_dir())

    def test_hooks_json_runs_script_through_bash(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(command.startswith("bash "), "don't depend on the exec bit surviving plugin caching")


@unittest.skipUnless(shutil.which("claude"), "claude CLI not installed")
class ClaudeValidateTests(unittest.TestCase):
    def test_claude_plugin_validate_plugin(self):
        result = subprocess.run(
            ["claude", "plugin", "validate", str(PLUGIN)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_claude_plugin_validate_marketplace(self):
        result = subprocess.run(
            ["claude", "plugin", "validate", str(ROOT)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
