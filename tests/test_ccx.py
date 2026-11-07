"""Unit tests for lib/cockpit core + cli logic (no real processes touched).

Run: python3 -m unittest discover -s tests -v   (pytest works too)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from cockpit import cli, core  # noqa: E402
from cockpit.core import CockpitError, Session  # noqa: E402

NOW_MS = 10_000_000_000

# pid ppid rss etime args. pid 100 started 5h ago, matching write_session's startedAt.
PS_SAMPLE = """\
    1     0   1000 10-00:00:00 /sbin/launchd
  100     1 300000 05:00:00 claude --resume
  101   100  50000 04:59:00 node /Users/me/mcp/server.js
  102   101  20000 04:58:00 python3 helper.py
  200     1 250000 01:00:00 /Users/me/.local/bin/claude --output-format stream-json --model=sonnet
  300     1  90000 2-00:00:00 /Applications/iTerm.app/Contents/MacOS/iTerm2
  301   300  10000 1-23:00:00 /Applications/iTerm.app/Contents/MacOS/iTermServer-3.6 --x
  400     1  40000 30:00 /Applications/Ghostty.app/Contents/MacOS/ghostty
  500     1 120000 05:00:00 /Users/John Smith/.local/share/claude/versions/2.1.319 --continue
  600     1  70000 05:00:00 /usr/bin/vim notes.txt
  700     1 150000 00:10 claude
"""


def write_session(directory, pid, **overrides):
    record = {
        "pid": pid, "sessionId": f"sess-{pid}", "cwd": "/Users/me/repo", "kind": "interactive",
        "entrypoint": "cli", "name": f"repo-{pid}", "status": "idle",
        "statusUpdatedAt": NOW_MS - 3 * 3600 * 1000, "startedAt": NOW_MS - 5 * 3600 * 1000,
    }
    record.update(overrides)
    (Path(directory) / f"{pid}.json").write_text(json.dumps(record))


def make_session(pid, **kw):
    defaults = dict(pid=pid, name=f"s{pid}", status="idle", entrypoint="cli", cwd="/x",
                    status_updated_ms=NOW_MS - 8 * 3600 * 1000, rss_kb=100, tree_rss_kb=100)
    defaults.update(kw)
    return Session(**defaults)


class ParsePsTests(unittest.TestCase):
    def test_parses_rows_with_elapsed_time(self):
        procs = core.parse_ps(PS_SAMPLE)
        self.assertEqual(procs[100].rss_kb, 300000)
        self.assertEqual(procs[100].elapsed_s, 5 * 3600)
        self.assertEqual(procs[1].elapsed_s, 10 * 86400)
        self.assertEqual(procs[400].elapsed_s, 30 * 60)
        self.assertEqual(procs[200].args.split()[0], "/Users/me/.local/bin/claude")

    def test_legacy_four_column_rows_still_parse(self):
        procs = core.parse_ps("  7 1 10 claude --resume\n")
        self.assertEqual(procs[7].args, "claude --resume")
        self.assertIsNone(procs[7].elapsed_s)

    def test_ignores_garbage_lines(self):
        self.assertEqual(list(core.parse_ps("not a row\n\n  7 1 10 00:01 ok\n")), [7])

    def test_parse_etime(self):
        self.assertEqual(core.parse_etime("00:05"), 5)
        self.assertEqual(core.parse_etime("1-02:03:04"), 93784)
        self.assertIsNone(core.parse_etime("--resume"))
        self.assertIsNone(core.parse_etime("1:2:3:4"))


class ProcessTreeTests(unittest.TestCase):
    def setUp(self):
        self.procs = core.parse_ps(PS_SAMPLE)

    def test_descendants_and_tree_rss(self):
        self.assertEqual(core.descendants(self.procs, 100), {101, 102})
        self.assertEqual(core.tree_rss_kb(self.procs, 100), 370000)

    def test_ancestors_stop_at_real_processes(self):
        self.assertEqual(core.ancestors(self.procs, 102), {101, 100, 1})

    def test_claude_detection_tolerates_spaces_in_install_path(self):
        detected = {pid for pid, p in self.procs.items() if core.is_claude_proc(p)}
        self.assertEqual(detected, {100, 200, 500, 700})

    def test_terminal_totals_group_helpers_by_app(self):
        self.assertEqual(core.terminal_totals_kb(self.procs), {"iTerm2": 100000, "Ghostty": 40000})


class LoadSessionsTests(unittest.TestCase):
    def setUp(self):
        self.procs = core.parse_ps(PS_SAMPLE)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        return core.load_sessions(self.dir, self.procs, NOW_MS)

    def test_loads_live_claude_sessions_only(self):
        write_session(self.dir, 100)
        write_session(self.dir, 999)  # dead pid
        write_session(self.dir, 600)  # pid reused by vim
        sessions = self.load()
        self.assertEqual([s.pid for s in sessions], [100])
        self.assertEqual(sessions[0].tree_rss_kb, 370000)
        self.assertEqual(sessions[0].session_id, "sess-100")

    def test_rejects_stale_record_when_pid_reused_by_another_claude(self):
        # pid 700 is a claude process started 10s ago; the record is from a crashed 5h-old session.
        write_session(self.dir, 700)
        self.assertEqual(self.load(), [])

    def test_without_startedAt_rejects_process_newer_than_status(self):
        write_session(self.dir, 700, startedAt=None)
        self.assertEqual(self.load(), [])
        write_session(self.dir, 100, startedAt=None)
        self.assertEqual([s.pid for s in self.load()], [100])

    def test_skips_malformed_files_and_key_files(self):
        (self.dir / "100.json").write_text("{not json")
        (self.dir / "100.abc.key").write_text("secret")
        (self.dir / "101.json").write_text("[1, 2]")
        write_session(self.dir, 500)
        self.assertEqual([s.pid for s in self.load()], [500])

    def test_missing_directory_returns_empty(self):
        self.assertEqual(core.load_sessions(self.dir / "nope", self.procs, NOW_MS), [])

    def test_sdk_flag(self):
        write_session(self.dir, 200, entrypoint="sdk-ts", startedAt=NOW_MS - 3600 * 1000)
        (session,) = self.load()
        self.assertTrue(session.is_sdk)


class GcRulesTests(unittest.TestCase):
    def test_selects_only_long_idle_interactive_unprotected(self):
        sessions = [
            make_session(1),
            make_session(2, status="busy"),
            make_session(3, entrypoint="sdk-ts"),
            make_session(4, status_updated_ms=NOW_MS - 3600 * 1000),
            make_session(5),
            make_session(6, status=None),
            make_session(7, status_updated_ms=None),
        ]
        picked = core.gc_candidates(sessions, NOW_MS, min_idle_s=6 * 3600, protected={5})
        self.assertEqual([s.pid for s in picked], [1])

    def test_still_eligible_requires_untouched_session(self):
        original = make_session(1)
        self.assertTrue(core.still_eligible(original, [make_session(1)], NOW_MS, 3600, set()))
        used_again = make_session(1, status_updated_ms=NOW_MS - 7 * 3600 * 1000)
        self.assertFalse(core.still_eligible(original, [used_again], NOW_MS, 3600, set()))
        self.assertFalse(core.still_eligible(original, [make_session(1, status="busy")], NOW_MS, 3600, set()))
        self.assertFalse(core.still_eligible(original, [], NOW_MS, 3600, set()))

    def test_idle_seconds_none_when_busy(self):
        self.assertIsNone(make_session(1, status="busy").idle_seconds(NOW_MS))
        self.assertEqual(make_session(1).idle_seconds(NOW_MS), 8 * 3600)


class GcArgsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(cli.parse_gc_args([]), (6 * 3600, False, False))

    def test_flags_and_equals_form(self):
        self.assertEqual(cli.parse_gc_args(["--idle=2", "--dry-run"]), (7200, False, True))
        self.assertEqual(cli.parse_gc_args(["--idle", "0.5", "--yes"]), (1800, True, False))

    def test_typos_are_rejected_not_ignored(self):
        for bad in (["--dryrun", "--yes"], ["--dry_run"], ["-y"], ["--idle"], ["--idle", "0.0001"]):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                cli.parse_gc_args(bad)


class FormattingTests(unittest.TestCase):
    def test_human_duration(self):
        self.assertEqual(core.human_duration(None), "-")
        self.assertEqual(core.human_duration(45), "45s")
        self.assertEqual(core.human_duration(125), "2m")
        self.assertEqual(core.human_duration(3 * 3600 + 120), "3h02m")
        self.assertEqual(core.human_duration(2 * 86400 + 3600), "2d01h")

    def test_human_mb(self):
        self.assertEqual(core.human_mb(0), "0 MB")
        self.assertEqual(core.human_mb(1536), "2 MB")
        self.assertEqual(core.human_mb(1024 * 1500), "1.46 GB")

    def test_parse_hours(self):
        self.assertEqual(core.parse_hours("6"), 6 * 3600)
        self.assertEqual(core.parse_hours("0.5"), 1800)
        for bad in ("-1", "abc", "0", "nan", "inf", "0.001"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                core.parse_hours(bad)

    def test_session_name_and_short_path(self):
        self.assertEqual(core.session_name("/a/b/repo", "feat/x"), "repo@feat/x")
        self.assertEqual(core.session_name("/a/b/repo", None), "repo")
        self.assertEqual(core.session_name("/", None), "root")
        home = str(Path.home())
        self.assertEqual(core.short_path(home + "/x"), "~/x")
        self.assertEqual(core.short_path(home + "x"), home + "x")


class LaunchArgvTests(unittest.TestCase):
    def test_names_new_sessions(self):
        argv = cli.launch_argv("/bin/claude", [], "/tmp")
        self.assertEqual(argv[:3], ["/bin/claude", "-n", "tmp"])

    def test_keeps_explicit_name_and_resume(self):
        for args in (["-n", "x"], ["--name=x"], ["--resume"], ["-c"], ["-p", "hi"]):
            with self.subTest(args=args):
                self.assertEqual(cli.launch_argv("/bin/claude", args, "/tmp"), ["/bin/claude", *args])

    def test_worktree_argv(self):
        self.assertEqual(cli.worktree_argv("c", ["fix", "--model", "sonnet"]),
                         ["c", "-w", "fix", "-n", "fix", "--model", "sonnet"])
        with self.assertRaises(CockpitError):
            cli.worktree_argv("c", ["--oops"])


class TierTests(unittest.TestCase):
    def test_tiers_come_from_agent_files_in_order(self):
        from cockpit.tiers import load_tiers
        tiers = load_tiers()
        self.assertEqual([t.name for t in tiers],
                         ["scout", "routine-worker", "senior-engineer", "debugger", "architect"])
        self.assertEqual(tiers[0].runs_on, "sonnet · medium")
        self.assertEqual(tiers[-1].runs_on, "opus · max")

    def test_missing_agent_dir_yields_no_tiers(self):
        from cockpit.tiers import load_tiers
        self.assertEqual(load_tiers(Path("/nonexistent")), [])

    def test_override_detection(self):
        self.assertEqual(cli.tier_overrides({}, {}), [])
        found = cli.tier_overrides({"CLAUDE_CODE_EFFORT_LEVEL": "high"},
                                   {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"})
        self.assertEqual(found, ["CLAUDE_CODE_EFFORT_LEVEL=high", "CLAUDE_CODE_SUBAGENT_MODEL=haiku"])

    def test_settings_env_tolerates_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(cli.settings_env(path), {})
            path.write_text("{bad")
            self.assertEqual(cli.settings_env(path), {})
            path.write_text(json.dumps({"env": {"A": 1}}))
            self.assertEqual(cli.settings_env(path), {"A": "1"})


class CliErrorTests(unittest.TestCase):
    def test_unknown_flags_exit_2_with_message(self):
        self.assertEqual(cli.main(["ls", "--bogus"]), 2)
        self.assertEqual(cli.main(["gc", "--dryrun", "--yes"]), 2)

    def test_version_and_help(self):
        self.assertEqual(cli.main(["version"]), 0)
        self.assertEqual(cli.main(["help"]), 0)


if __name__ == "__main__":
    unittest.main()
