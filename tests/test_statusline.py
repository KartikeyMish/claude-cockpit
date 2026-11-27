"""`ccx statusline`: segment rendering, width fitting, and end-to-end runs of bin/ccx.

End-to-end runs get a temporary CLAUDE_CONFIG_DIR, state dir and HOME, and a PATH
whose `osascript` is a recording shim, so no real notification is ever sent.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from cockpit import statusline as sl  # noqa: E402
from test_statusline_data import assistant, usage, write_lines  # noqa: E402

CCX = ROOT / "bin" / "ccx"
NOW = 1_800_000_000.0
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(lines):
    return [ANSI.sub("", line) for line in lines]


def payload(**overrides):
    base = {
        "session_id": "sess-1",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "effort": {"level": "high"},
        "workspace": {"current_dir": "/work/quickflow-bot-builder-master"},
        "context_window": {"total_input_tokens": 64000, "context_window_size": 200000, "used_percentage": 32},
        "cost": {"total_cost_usd": 4.123, "total_duration_ms": 38 * 60_000 + 5000,
                 "total_lines_added": 212, "total_lines_removed": 40},
        "rate_limits": {"five_hour": {"used_percentage": 54, "resets_at": NOW + 2 * 3600 + 10 * 60 + 30},
                        "seven_day": {"used_percentage": 31.2, "resets_at": NOW + 86400}},
        "prompt_cache": {"warm": True, "caching_observed": True, "ttl": "5m", "expires_at": NOW + 4 * 60 + 20,
                         "hit_ratio": 0.5},
    }
    base.update(overrides)
    return base


GIT = {"branch": "kartikeymishra/dev1-14854-fix-the-status-line", "oid": "abc1234", "ahead": 1, "behind": 0,
       "dirty": 3}
FACTS = {"cache": {"hit_pct": 92.4, "expires_at": None},
         "agents": {"running": 1, "done": 3, "stalled": 0, "cost": 1.8, "partial": False}}
WAITING = ["api@main", "web@dev"]


class FormattingTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual([sl.span(s) for s in (0, 45, 240, 7800, 7200, 90000)],
                         ["0s", "45s", "4m", "2h10m", "2h", "1d1h"])
        self.assertEqual([sl.tokens(n) for n in (950, 64000, 200000, 1_000_000, 1_250_000)],
                         ["950", "64k", "200k", "1M", "1.2M"])
        self.assertEqual(sl.short_branch("kartikeymishra/dev1-14854-fix-the-status-line"), "dev1-14854")
        self.assertEqual(sl.short_branch("feature/short"), "short")
        self.assertEqual(sl.short_branch("a-very-long-branch-without-ticket"), "a-very-long-branch-…")
        self.assertEqual(sl.width("\x1b[31m💀 ab\x1b[0m"), 5, "an emoji is two columns, colour codes none")

    def test_context_thresholds_match_the_gsd_line(self):
        def ctx(pct):
            return sl.context_segment({"context_window": {"used_percentage": pct}}).variants[0]
        self.assertIn("\x1b[32m", ctx(49))
        self.assertIn("\x1b[33m", ctx(50))
        self.assertIn("\x1b[38;5;208m", ctx(65))
        self.assertIn("\x1b[5;31m💀", ctx(80))


class RenderTests(unittest.TestCase):
    def test_wide_output_matches_the_approved_layout(self):
        lines = plain(sl.build(payload(), FACTS, GIT, WAITING, NOW))
        self.assertEqual(lines, [
            ("Opus 5.5·high │ quickflow-bot-builder-master ⎇ dev1-14854 ●3 ↑1 │ ▓▓▓░░░░░░░ 32% 64k/200k"
             " │ $4.12 · 38m · +212 −40"),
            "5h ▓▓▓░░░ 54% ↻2h10m · 7d 31% │ cache 92% ⏱4m │ agents 1▶ 3✓ $1.80 │ ⚠ 2 sessions waiting",
        ])

    def test_narrow_terminal_drops_low_priority_segments_first(self):
        for columns in (100, 70, 50, 30):
            lines = sl.build(payload(), FACTS, GIT, WAITING, NOW, limit=columns - sl.EDGE_COLUMNS)
            for line in lines:
                self.assertLessEqual(sl.width(line), columns - sl.EDGE_COLUMNS, (columns, plain([line])))
            self.assertIn("32%", plain(lines)[0], "context always survives")
        narrow = plain(sl.build(payload(), FACTS, GIT, WAITING, NOW, limit=46))
        self.assertNotIn("$4.12", narrow[0], "cost (lowest on row 1) goes first")
        self.assertIn("⚠", narrow[1], "waiting sessions outrank cache and agents")
        self.assertNotIn("agents", narrow[1])

    def test_one_line_mode_folds_everything_by_priority(self):
        lines = sl.build(payload(), FACTS, GIT, WAITING, NOW, limit=120, one_line=True)
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(sl.width(lines[0]), 120)
        text = plain(lines)[0]
        for kept in ("Opus 5.5", "32%", "5h"):
            self.assertIn(kept, text)
        self.assertNotIn("agents", text, "the lowest priority goes first")

    def test_missing_and_malformed_fields_drop_segments_silently(self):
        self.assertEqual(sl.build({}, {}, None, [], NOW), [])
        bad = {"model": "x", "context_window": [], "cost": {"total_cost_usd": "1"}, "rate_limits": None,
               "effort": 3, "workspace": {"current_dir": 7}, "prompt_cache": {"expires_at": "soon"}}
        self.assertEqual(sl.build(bad, {}, None, [], NOW), [])
        only_model = plain(sl.build({"model": {"display_name": "Sonnet 5"}}, {}, None, [], NOW))
        self.assertEqual(only_model, ["Sonnet 5"])
        early = payload(context_window={"used_percentage": None, "total_input_tokens": 30000,
                                        "context_window_size": 200000}, rate_limits={"seven_day": {
                                            "used_percentage": 40}}, prompt_cache=None)
        lines = plain(sl.build(early, {}, None, [], NOW))
        self.assertIn("▓░░░░░░░░░ 15% 30k/200k", lines[0], "derived from token counts when used_percentage is null")
        self.assertEqual(lines[1], "7d 40%", "7-day usage alone when the 5-hour window is absent")

    def test_cache_countdown_cold_and_transcript_fallback(self):
        expired = payload(prompt_cache={"warm": True, "caching_observed": True, "expires_at": NOW - 1})
        self.assertIn("cold", plain(sl.build(expired, {}, None, [], NOW))[1])
        never = payload(prompt_cache={"warm": False, "caching_observed": True, "expires_at": None})
        self.assertIn("cold", plain(sl.build(never, {}, None, [], NOW))[1])
        facts = {"cache": {"hit_pct": 80.0, "expires_at": NOW + 3000}}
        line = plain(sl.build(payload(prompt_cache=None), facts, None, [], NOW))[1]
        self.assertIn("cache 80% ⏱50m", line)
        session_ratio = plain(sl.build(payload(), {}, None, [], NOW))[1]
        self.assertIn("cache 50% ⏱4m", session_ratio, "session-wide hit ratio when no transcript")

    def test_segment_failure_never_blanks_the_line(self):
        facts = {"agents": {"running": "x"}}   # would raise inside agents_segment
        lines = plain(sl.build(payload(), facts, None, [], NOW))
        self.assertTrue(lines[0].startswith("Opus 5.5·high"))
        self.assertNotIn("agents", lines[1])

    def test_partial_agent_cost_is_marked(self):
        facts = {"agents": {"running": 0, "done": 2, "stalled": 1, "cost": 3.0, "partial": True}}
        self.assertIn("agents 2✓ 1⏸ ≥$3.00", plain(sl.build(payload(), facts, None, [], NOW))[1])


class EndToEndTests(unittest.TestCase):
    """bin/ccx statusline against fixture files, as Claude Code would run it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.claude = base / "claude"
        self.project = self.claude / "projects" / "-work-repo"
        (self.project / "sess-1" / "subagents").mkdir(parents=True)
        (self.claude / "sessions").mkdir()
        self.repo = base / "repo"
        self.repo.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.notified = base / "notified.log"
        shim = self.bin / "osascript"
        shim.write_text(f'#!/bin/sh\necho "$@" >> "{self.notified}"\n')
        shim.chmod(0o755)
        self.env = {"HOME": str(base), "PATH": os.pathsep.join([str(self.bin), "/usr/bin", "/bin"]),
                    "CLAUDE_CONFIG_DIR": str(self.claude), "COCKPIT_STATE_DIR": str(base / "state"),
                    "TMPDIR": str(base), "COLUMNS": "200", "GIT_CONFIG_GLOBAL": "/dev/null"}
        self.transcript = self.project / "sess-1.jsonl"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 60))
        write_lines(self.transcript, [assistant("m1", usage(write=1000, ttl="1h"), stamp),
                                      assistant("m2", usage(inp=0, read=9000), stamp)])
        agent_dir = self.project / "sess-1" / "subagents"
        write_lines(agent_dir / "agent-a1.jsonl", [assistant("r1", usage(out=1000))])
        (agent_dir / "agent-a1.meta.json").write_text(json.dumps({"finished": True}))
        write_lines(agent_dir / "agent-a2.jsonl", [assistant("r2", usage(out=1000))])
        (agent_dir / "agent-a2.meta.json").write_text(json.dumps({}))
        (self.claude / "sessions" / "1.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": "other", "status": "waiting", "name": "api@main"}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_ccx(self, data, **env):
        stdin = data if isinstance(data, str) else json.dumps(data)
        return subprocess.run([sys.executable, str(CCX), "statusline"], input=stdin, capture_output=True,
                              text=True, env={**self.env, **env}, timeout=30, check=False)

    def fixture(self, **overrides):
        fields = {"workspace": {"current_dir": str(self.repo)}, "transcript_path": str(self.transcript),
                  "prompt_cache": None,
                  "rate_limits": {"five_hour": {"used_percentage": 54, "resets_at": time.time() + 7830}}}
        return payload(**{**fields, **overrides})

    def test_full_render_from_files(self):
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", "-b", "kartikeymishra/dev1-14854-x", str(self.repo)],
                           check=True, env=self.env)
            (self.repo / "new.txt").write_text("x\n")
        out = self.run_ccx(self.fixture())
        self.assertEqual((out.returncode, out.stderr), (0, ""))
        lines = plain(out.stdout.splitlines())
        self.assertEqual(len(lines), 2, out.stdout)
        self.assertTrue(lines[0].startswith("Opus 5.5·high │ repo"))
        if shutil.which("git"):
            self.assertIn("⎇ dev1-14854-x ●1", lines[0])
        self.assertRegex(lines[1], r"^5h ▓▓▓░░░ 54% ↻2h1\dm │ cache 90% ⏱5\dm │ agents 1▶ 1✓ \$\d+\.\d\d"
                                   r" │ ⚠ 1 session waiting$")
        self.assertFalse(self.notified.exists(), "54% is under the alert threshold")

    def test_runs_isolated_like_the_plugin_scripts(self):
        """`python3 -I -S`: no site-packages, no PYTHON* env, no cwd on sys.path."""
        out = subprocess.run([sys.executable, "-I", "-S", str(CCX), "statusline"], input=json.dumps(self.fixture()),
                             capture_output=True, text=True, env=self.env, timeout=30, check=False)
        self.assertEqual((out.returncode, out.stderr), (0, ""))
        self.assertEqual(len(out.stdout.splitlines()), 2)

    def test_narrow_and_single_line(self):
        out = self.run_ccx(self.fixture(), COLUMNS="44")
        for line in out.stdout.splitlines():
            self.assertLessEqual(sl.width(line), 40, line)
        out = self.run_ccx(self.fixture(), COCKPIT_STATUSLINE_LINES="1")
        self.assertEqual(len(out.stdout.splitlines()), 1)

    def test_garbage_and_empty_input_exit_zero_without_traceback(self):
        for data in ("", "not json", "[1,2]", json.dumps({"transcript_path": "/etc/passwd", "session_id": "../x"})):
            out = self.run_ccx(data)
            self.assertEqual((out.returncode, out.stderr), (0, ""), data)

    def test_usage_alert_fires_once_per_window_when_on_macos(self):
        data = self.fixture(rate_limits={"five_hour": {"used_percentage": 75, "resets_at": time.time() + 3600}})
        for _ in range(2):
            self.assertEqual(self.run_ccx(data).returncode, 0)
        if sys.platform != "darwin":
            self.assertFalse(self.notified.exists())
            return
        deadline = time.time() + 10
        while not self.notified.exists() and time.time() < deadline:
            time.sleep(0.05)   # the notifier is detached; give it a moment
        time.sleep(0.2)
        calls = self.notified.read_text().splitlines()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("5-hour plan usage at 75%", calls[0])
        self.assertEqual(self.run_ccx(data, COCKPIT_USAGE_ALERT_PCT="0").returncode, 0)

    def test_help_and_cli_dispatch(self):
        out = subprocess.run([sys.executable, str(CCX), "help"], capture_output=True, text=True, timeout=30,
                             env=self.env, check=False)
        self.assertIn("ccx statusline", out.stdout)
        sys.path.insert(0, str(ROOT / "lib"))
        from cockpit import cli
        self.assertIs(cli.COMMANDS["statusline"], cli.cmd_statusline)


if __name__ == "__main__":
    unittest.main()
