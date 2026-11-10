"""`ccx doctor`: stale-hook detection, plugin install resolution, CLI views, ls/TUI markers.

No real processes or Claude installs are touched: every test builds a fake Claude
config dir and feeds canned `ps` output through the injectable runner.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from cockpit import cli, doctor, doctor_cmd  # noqa: E402
from cockpit.core import CockpitError, Session, parse_ps, read_processes  # noqa: E402
from cockpit.doctor import CURRENT, STALE, UNKNOWN  # noqa: E402

HOUR_MS = 3600 * 1000
NOW_MS = 10_000_000_000
HOOKS_MS = NOW_MS - 2 * HOUR_MS   # hooks.json changed two hours before "now"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def make_plugin(root, version="1.6.0", policy="1.6"):
    write_json(root / ".claude-plugin" / "plugin.json", {"name": "orchestrator", "version": version})
    write_json(root / "hooks" / "hooks.json", {"hooks": {}})
    (root / "hooks" / "routing-policy.md").write_text(f"# Orchestrator routing (claude-cockpit policy v{policy})\n\nbody\n")
    return root


def set_mtime(path, ms):
    os.utime(path, (ms / 1000, ms / 1000))


def directory_home(tmp):
    """A Claude config dir whose claude-cockpit marketplace is a local checkout."""
    home, checkout = Path(tmp) / "claude", Path(tmp) / "checkout"
    write_json(checkout / ".claude-plugin" / "marketplace.json",
               {"name": "claude-cockpit", "plugins": [{"name": "orchestrator", "source": "./plugins/orchestrator"}]})
    make_plugin(checkout / "plugins" / "orchestrator")
    stale_cache = make_plugin(home / "plugins" / "cache" / "claude-cockpit" / "orchestrator" / "1.5.0", "1.5.0", "1.5")
    write_json(home / "plugins" / "known_marketplaces.json",
               {"claude-cockpit": {"source": {"source": "directory", "path": str(checkout)}}})
    write_json(home / "plugins" / "installed_plugins.json", {"version": 2, "plugins": {"orchestrator@claude-cockpit": [
        {"scope": "user", "installPath": str(stale_cache), "version": "1.5.0"}]}})
    write_json(home / "settings.json", {"enabledPlugins": {"orchestrator@claude-cockpit": True}})
    return home, checkout


def github_home(tmp):
    """A Claude config dir installed from the #stable channel (runs from the plugin cache)."""
    home = Path(tmp) / "claude"
    cache = make_plugin(home / "plugins" / "cache" / "claude-cockpit" / "orchestrator" / "1.6.0")
    project = make_plugin(Path(tmp) / "project-copy", "1.4.0", "1.4")
    write_json(home / "plugins" / "known_marketplaces.json", {"claude-cockpit": {"source": {
        "source": "github", "repo": "KartikeyMish/claude-cockpit", "ref": "stable"}}})
    write_json(home / "plugins" / "installed_plugins.json", {"version": 2, "plugins": {"orchestrator@claude-cockpit": [
        {"scope": "project", "installPath": str(project), "version": "1.4.0"},
        {"scope": "user", "installPath": str(cache), "version": "1.6.0"}]}})
    return home, cache


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}Z"


ONE_STALE = "100 1 1000 03:00:00 claude\n"   # started 3h before NOW_MS


def flagged(home):
    """Pids the rule flags for one registered session that started 3h before NOW_MS."""
    procs = read_processes(lambda: ONE_STALE)
    return doctor.stale_pids(procs, [session(100, SID[100])], doctor.resolve_install(home).stale_after_ms, NOW_MS)


class ResolveInstallTests(unittest.TestCase):
    def test_directory_marketplace_runs_from_the_checkout_not_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            set_mtime(checkout / "plugins" / "orchestrator" / "hooks" / "hooks.json", HOOKS_MS)
            inst = doctor.resolve_install(home)
            self.assertEqual(inst.root, (checkout / "plugins" / "orchestrator").resolve())
            self.assertEqual(inst.hooks_json, inst.root / "hooks" / "hooks.json")
            self.assertEqual((inst.source, inst.version, inst.registry_version), ("directory", "1.6.0", "1.5.0"))
            self.assertEqual(inst.source_detail, str(checkout))
            self.assertEqual((inst.hooks_mtime_ms, inst.stale_after_ms), (HOOKS_MS, HOOKS_MS))
            self.assertEqual(inst.note, "")
            self.assertIs(inst.enabled, True)

    def test_directory_source_ignores_a_newer_cache_copy_and_last_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            set_mtime(checkout / "plugins" / "orchestrator" / "hooks" / "hooks.json", NOW_MS - 5 * HOUR_MS)
            cache = home / "plugins" / "cache" / "claude-cockpit" / "orchestrator" / "1.5.0"
            set_mtime(cache / "hooks" / "hooks.json", NOW_MS - HOUR_MS)   # newer than the process
            registry = json.loads((home / "plugins" / "installed_plugins.json").read_text())
            registry["plugins"]["orchestrator@claude-cockpit"][0]["lastUpdated"] = iso(NOW_MS - HOUR_MS)
            write_json(home / "plugins" / "installed_plugins.json", registry)
            self.assertIsNone(doctor.resolve_install(home).updated_ms)
            self.assertEqual(flagged(home), frozenset())

    def test_directory_source_checkout_hooks_json_newer_than_the_process_flags_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            set_mtime(checkout / "plugins" / "orchestrator" / "hooks" / "hooks.json", NOW_MS - HOUR_MS)
            self.assertEqual(flagged(home), frozenset({100}))

    def test_directory_source_script_edits_alone_never_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            hooks = checkout / "plugins" / "orchestrator" / "hooks"
            set_mtime(hooks / "hooks.json", NOW_MS - 5 * HOUR_MS)
            (hooks / "guard.py").write_text("# edited\n")
            set_mtime(hooks / "guard.py", NOW_MS - 60_000)
            self.assertEqual(flagged(home), frozenset())

    def test_plugin_path_comes_from_the_checkout_marketplace_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            write_json(checkout / ".claude-plugin" / "marketplace.json",
                       {"plugins": [{"name": "other", "source": "./x"}, {"name": "orchestrator", "source": "./pkg/orch"}]})
            make_plugin(checkout / "pkg" / "orch", "2.0.0")
            inst = doctor.resolve_install(home)
            self.assertEqual(inst.root, (checkout / "pkg" / "orch").resolve())
            self.assertEqual((inst.version, inst.note), ("2.0.0", ""))

    def test_unreadable_marketplace_json_falls_back_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            (checkout / ".claude-plugin" / "marketplace.json").write_text("{broken")
            inst = doctor.resolve_install(home)
            self.assertEqual(inst.root, checkout / "plugins" / "orchestrator")
            self.assertIn("is unreadable; assumed plugins/orchestrator", inst.note)
            write_json(checkout / ".claude-plugin" / "marketplace.json", {"plugins": []})
            self.assertIn("has no orchestrator entry", doctor.resolve_install(home).note)

    def test_plugin_json_hooks_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            root = checkout / "plugins" / "orchestrator"
            write_json(root / ".claude-plugin" / "plugin.json", {"version": "1.6.0", "hooks": "./config/hooks.json"})
            write_json(root / "config" / "hooks.json", {"hooks": {}})
            set_mtime(root / "hooks" / "hooks.json", NOW_MS - 5 * HOUR_MS)
            set_mtime(root / "config" / "hooks.json", NOW_MS - HOUR_MS)
            inst = doctor.resolve_install(home)
            self.assertEqual(inst.hooks_json, (root / "config" / "hooks.json").resolve())
            self.assertEqual(flagged(home), frozenset({100}))
            write_json(root / ".claude-plugin" / "plugin.json", {"version": "1.6.0", "hooks": {"PreToolUse": []}})
            inst = doctor.resolve_install(home)
            self.assertEqual(inst.hooks_json, inst.root / ".claude-plugin" / "plugin.json")   # hooks inline

    def test_github_install_runs_from_the_user_scope_install_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cache = github_home(tmp)
            set_mtime(cache / "hooks" / "hooks.json", HOOKS_MS)
            inst = doctor.resolve_install(home)
            self.assertEqual(inst.root, cache)
            self.assertEqual((inst.source, inst.source_detail), ("github", "KartikeyMish/claude-cockpit#stable"))
            self.assertEqual(inst.hooks_mtime_ms, HOOKS_MS)
            self.assertIsNone(inst.enabled)   # settings.json doesn't say

    def test_github_install_counts_both_hooks_mtime_and_last_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cache = github_home(tmp)
            set_mtime(cache / "hooks" / "hooks.json", NOW_MS - 5 * HOUR_MS)   # older than the process
            self.assertEqual(flagged(home), frozenset())
            registry = json.loads((home / "plugins" / "installed_plugins.json").read_text())
            registry["plugins"]["orchestrator@claude-cockpit"][1]["lastUpdated"] = iso(NOW_MS - HOUR_MS)
            write_json(home / "plugins" / "installed_plugins.json", registry)
            inst = doctor.resolve_install(home)
            self.assertEqual((inst.updated_ms, inst.stale_after_ms), (NOW_MS - HOUR_MS, NOW_MS - HOUR_MS))
            self.assertEqual(flagged(home), frozenset({100}))          # updated after the process started
            set_mtime(cache / "hooks" / "hooks.json", NOW_MS - 30 * 60_000)
            registry["plugins"]["orchestrator@claude-cockpit"][1]["lastUpdated"] = iso(NOW_MS - 5 * HOUR_MS)
            write_json(home / "plugins" / "installed_plugins.json", registry)
            self.assertEqual(flagged(home), frozenset({100}))          # hooks file newer: still counts

    def test_registry_timestamps(self):
        self.assertEqual(doctor.iso_ms("1970-01-01T00:00:01.500Z"), 1500)
        self.assertEqual(doctor.iso_ms("1970-01-01T00:00:01"), 1000)
        for bad in (None, "", "yesterday", 7):
            self.assertIsNone(doctor.iso_ms(bad))

    def test_missing_or_malformed_registry_is_not_installed_never_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual((doctor.resolve_install(home).root, doctor.resolve_install(home).source), (None, "missing"))
            write_json(home / "plugins" / "installed_plugins.json", "{nope")
            write_json(home / "plugins" / "known_marketplaces.json", ["not", "a", "dict"])
            inst = doctor.resolve_install(home)
            self.assertIsNone(inst.root)
            self.assertIsNone(inst.hooks_mtime_ms)

    def test_install_path_without_hooks_json_has_no_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, cache = github_home(tmp)
            (cache / "hooks" / "hooks.json").unlink()
            self.assertIsNone(doctor.resolve_install(home).hooks_mtime_ms)

    def test_claude_config_dir_is_respected(self):
        self.assertEqual(doctor.claude_home_from_env({"CLAUDE_CONFIG_DIR": "/opt/cc"}), Path("/opt/cc"))
        self.assertEqual(doctor.claude_home_from_env({}), Path(os.path.expanduser("~/.claude")))
        self.assertEqual(doctor.claude_home_from_env({"CLAUDE_CONFIG_DIR": ""}), Path(os.path.expanduser("~/.claude")))

    def test_probe_caches_the_lookup_for_its_ttl(self):
        calls, clock = [], [100.0]

        def resolve(home):
            calls.append(home)
            return doctor.PluginInstall(None, "missing", "", None, None, None, len(calls))

        probe = doctor.HooksProbe(Path("/x"), ttl_s=10, clock=lambda: clock[0], resolve=resolve)
        self.assertEqual([probe.stale_after_ms(), probe.stale_after_ms()], [1, 1])
        clock[0] += 10
        self.assertEqual(probe.stale_after_ms(), 2)
        self.assertEqual(len(calls), 2)


# pid ppid rss etime args, relative to NOW_MS; hooks.json changed 2h ago.
PS_ROWS = """\
    1     0   1000 10-00:00:00 /sbin/launchd
  100     1 300000 05:00:00 claude --resume
  101   100  50000 04:59:00 node /Users/me/mcp/server.js
  200     1 250000 01:00:00 /Users/me/.local/bin/claude
  300     1 250000 05:00:00 /Users/me/.local/bin/claude --output-format stream-json --verbose
  400     1 250000 05:00:00 claude
  500     1  70000 05:00:00 /usr/bin/vim notes.txt
  600     1 150000 01:59:59 claude
  700     1 150000 claude --continue
  800     1 150000 05:00:00 claude -p summarize the logs
  810     1 150000 05:00:00 /Users/me/.local/bin/claude mcp serve
  820     1 150000 05:00:00 /Users/me/.local/share/claude/versions/2.1.322 --ripgrep
  garbage line that is not a process row
"""
SID = {pid: f"00000000-0000-4000-8000-{pid:012d}" for pid in (100, 200, 300, 400, 600, 700, 900)}


def session(pid, sid=None, entrypoint="cli", cwd="/Users/me/repo"):
    return Session(pid=pid, name=f"repo-{pid}", status="idle", entrypoint=entrypoint, cwd=cwd,
                   status_updated_ms=NOW_MS - HOUR_MS, rss_kb=1, tree_rss_kb=1, session_id=sid)


def registered():
    """Registry sessions for PS_ROWS: 400 is an SDK session, 300 an SDK child, 900 is gone."""
    return [session(pid, SID[pid], entrypoint="sdk-ts" if pid == 400 else "cli")
            for pid in (100, 200, 300, 400, 600, 700, 900)]


class ClassifyTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(doctor.classify(HOOKS_MS - 1, HOOKS_MS, NOW_MS), STALE)
        self.assertEqual(doctor.classify(HOOKS_MS + doctor.START_SLACK_MS, HOOKS_MS, NOW_MS), CURRENT)
        self.assertEqual(doctor.classify(HOOKS_MS, HOOKS_MS, NOW_MS), UNKNOWN)          # too close to call
        self.assertEqual(doctor.classify(None, HOOKS_MS, NOW_MS), UNKNOWN)
        self.assertEqual(doctor.classify(HOOKS_MS, None, NOW_MS), UNKNOWN)

    def test_future_dated_hooks_json_dates_nothing(self):
        future = NOW_MS + doctor.START_SLACK_MS + 1
        self.assertTrue(doctor.hooks_in_future(future, NOW_MS))
        self.assertFalse(doctor.hooks_in_future(NOW_MS + doctor.START_SLACK_MS, NOW_MS))
        self.assertFalse(doctor.hooks_in_future(None, NOW_MS))
        self.assertEqual(doctor.classify(NOW_MS - HOUR_MS, future, NOW_MS), UNKNOWN)
        procs = read_processes(lambda: PS_ROWS)
        self.assertEqual(doctor.stale_pids(procs, registered(), future, NOW_MS), frozenset())

    def test_processes_before_and_after_the_change_and_skips(self):
        procs = read_processes(lambda: PS_ROWS)
        found = {p.pid: p for p in doctor.hooked_processes(procs, registered(), HOOKS_MS, NOW_MS)}
        self.assertEqual(sorted(found), [100, 200, 600, 700])   # no SDK sessions/children, no dead pid
        self.assertEqual(found[100].state, STALE)
        self.assertEqual(found[200].state, CURRENT)
        self.assertEqual(found[600].state, UNKNOWN)   # 1 s after the change: ps can't tell
        self.assertEqual(found[700].state, UNKNOWN)   # no start time in this row
        self.assertEqual((found[100].session_id, found[100].name), (SID[100], "repo-100"))
        self.assertEqual(doctor.stale_pids(procs, registered(), HOOKS_MS, NOW_MS), frozenset({100}))
        self.assertEqual(doctor.hooks_states(procs, registered(), HOOKS_MS, NOW_MS)[200], CURRENT)

    def test_unregistered_claude_processes_are_never_classified(self):
        procs = read_processes(lambda: PS_ROWS)   # claude -p, mcp serve, a versions helper: all 5h old
        self.assertEqual(doctor.hooked_processes(procs, [], HOOKS_MS, NOW_MS), [])
        found = {p.pid for p in doctor.hooked_processes(procs, registered(), HOOKS_MS, NOW_MS)}
        self.assertFalse(found & {800, 810, 820})

    def test_unparsable_ps_output_never_flags_anything_stale(self):
        procs = parse_ps("??? ??? ???\nclaude claude claude claude claude\n 9 1 x 05:00 claude\n")
        self.assertEqual(doctor.stale_pids(procs, [session(9, SID[100])], HOOKS_MS, NOW_MS), frozenset())
        self.assertEqual(doctor.hooked_processes(read_processes(lambda: ""), registered(), HOOKS_MS, NOW_MS), [])

    def test_no_hooks_json_means_unknown_for_everyone(self):
        procs = read_processes(lambda: PS_ROWS)
        self.assertEqual({p.state for p in doctor.hooked_processes(procs, registered(), None, NOW_MS)}, {UNKNOWN})

    def test_restart_command(self):
        home, sid = str(Path.home()), SID[100]
        proc = doctor.HookedProcess(1, 0, STALE, sid, "n", home + "/repo")
        self.assertEqual(proc.restart_command(), f"cd ~/repo && claude --resume {sid}")
        odd = doctor.HookedProcess(1, 0, STALE, sid, "n", "/tmp/my repo")
        self.assertEqual(odd.restart_command(), f"cd '/tmp/my repo' && claude --resume {sid}")
        self.assertEqual(doctor.HookedProcess(1, 0, STALE, None, None, None).restart_command(), "claude --resume")

    def test_restart_command_never_pastes_a_non_uuid_session_id(self):
        for bad in ("abc", "x; rm -rf ~", "$(reboot)", SID[100] + " --dangerously", "\n" + SID[100]):
            with self.subTest(sid=bad):
                proc = doctor.HookedProcess(1, 0, STALE, bad, "n", None)
                self.assertEqual(proc.restart_command(), "claude --resume")


class PolicyAndSwitchTests(unittest.TestCase):
    def test_injected_version_counts_only_session_start_deliveries(self):
        lines = [
            b'{"type":"attachment","attachment":{"hookEvent":"SessionStart","content":"# x (claude-cockpit policy v1.4)"}}',
            b'{"type":"assistant","message":"we should ship (claude-cockpit policy v9.9)"}',
            b'{"type":"attachment","attachment":{"hookEvent":"SessionStart","content":"# x (claude-cockpit policy v1.5)"}}',
            b'{"type":"user","message":"hello"}',
        ]
        self.assertEqual(doctor.injected_policy_version(lines), "1.5")
        self.assertIsNone(doctor.injected_policy_version(lines[1:2]))

    def test_transcript_and_installed_policy_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, checkout = directory_home(tmp)
            self.assertEqual(doctor.installed_policy_version(doctor.resolve_install(home)), "1.6")
            project = home / "projects" / "-Users-me-repo"
            project.mkdir(parents=True)
            (project / "sid-1.jsonl").write_bytes(
                b'{"attachment":{"hookEvent":"SessionStart","content":"(claude-cockpit policy v1.5)"}}\n')
            self.assertEqual(doctor.transcript_policy_version(home / "projects", "sid-1"), "1.5")
            self.assertIsNone(doctor.transcript_policy_version(home / "projects", "nope"))
        self.assertIsNone(doctor.installed_policy_version(doctor.PluginInstall(None, "missing", "", None, None, None, None)))

    def test_switched_off_reads_environment_and_settings(self):
        self.assertEqual(doctor.switched_off({}, {}), [])
        self.assertEqual(doctor.switched_off({"COCKPIT_GUARD": "OFF", "COCKPIT_ROUTING": "on"},
                                             {"COCKPIT_ROUTING": "0"}),
                         ["COCKPIT_ROUTING=0 (settings.json env)", "COCKPIT_GUARD=OFF (environment)"])

    def test_data_dir_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = doctor.guard_data_dir(Path(tmp))
            self.assertIsNone(doctor.dir_writable(data))
            data.mkdir(parents=True)
            self.assertTrue(doctor.dir_writable(data))
            if os.geteuid() != 0:
                data.chmod(0o500)
                try:
                    self.assertFalse(doctor.dir_writable(data))
                finally:
                    data.chmod(0o700)


def real_ps_rows(now_ms, rows):
    """`ps` text for (pid, started_ms, args) rows, relative to the real clock."""
    out = []
    for pid, started, args in rows:
        secs = max(0, (now_ms - started) // 1000)
        out.append(f"{pid} 1 1000 {secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d} {args}")
    return "\n".join(out) + "\n"


class DoctorCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home, checkout = directory_home(self.tmp.name)
        now = int(time.time() * 1000)
        hooks = checkout / "plugins" / "orchestrator" / "hooks" / "hooks.json"
        set_mtime(hooks, now - 2 * HOUR_MS)
        self.rows = [(100, now - 5 * HOUR_MS, "claude --resume"), (200, now - HOUR_MS, "claude"),
                     (300, now - 5 * HOUR_MS, "claude --output-format stream-json")]
        for pid, started, _ in self.rows[:2]:
            write_json(self.home / "sessions" / f"{pid}.json", {
                "pid": pid, "sessionId": SID[pid], "cwd": "/srv/repo", "name": f"repo-{pid}",
                "entrypoint": "cli", "status": "idle", "startedAt": started, "statusUpdatedAt": started})
        (self.home / "plugins" / "data" / doctor.DATA_DIR_NAME).mkdir(parents=True)

    def context(self, env=None, run_ps=None):
        env = dict({"CLAUDE_CONFIG_DIR": str(self.home)}, **(env or {}))
        return doctor_cmd.build_context(env, run_ps or (lambda: real_ps_rows(int(time.time() * 1000), self.rows)))

    def run_doctor(self, args, ctx, checks):
        out = io.StringIO()
        with redirect_stdout(out):
            code = doctor_cmd.cmd_doctor(args, context=lambda: ctx, checks=checks)
        return code, out.getvalue()

    LOCAL = (doctor_cmd.check_install, doctor_cmd.check_hooks, doctor_cmd.check_switches, doctor_cmd.check_data_dir)

    def test_stale_session_fails_with_exact_restart_command(self):
        code, text = self.run_doctor([], self.context(), self.LOCAL)
        self.assertEqual(code, 1)
        self.assertIn("orchestrator 1.6.0 in effect from directory", text)
        self.assertIn("✗ 1 of 2 claude sessions run an older hook list than the plugin in effect", text)
        self.assertIn("local checkout: hook scripts are live, only hooks.json changes need a restart", text)
        self.assertIn("⟳ repo-100 (pid 100", text)
        self.assertIn(f"exit, then cd /srv/repo && claude --resume {SID[100]}", text)
        self.assertNotIn(SID[200], text)
        self.assertNotIn("pid 300", text)

    def test_json_view_has_plugin_processes_and_summary(self):
        code, text = self.run_doctor(["--json"], self.context(), self.LOCAL)
        report = json.loads(text)
        self.assertEqual(code, 1)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["plugin"]["source"], "directory")
        self.assertEqual(report["plugin"]["version"], "1.6.0")
        self.assertEqual(report["summary"], {"current": 1, "stale": 1, "unknown": 0})
        by_pid = {p["pid"]: p for p in report["processes"]}
        self.assertEqual(by_pid[100]["restart"], f"exit, then cd /srv/repo && claude --resume {SID[100]}")
        self.assertIsNone(by_pid[200]["restart"])
        self.assertEqual({c["name"] for c in report["checks"] if c["status"] == "fail"}, {"hooks-current"})

    def test_healthy_when_every_session_is_current(self):
        now = int(time.time() * 1000)
        ctx = self.context(run_ps=lambda: real_ps_rows(now, [(200, now - HOUR_MS, "claude")]))
        code, text = self.run_doctor([], ctx, self.LOCAL)
        self.assertEqual(code, 0, text)
        self.assertIn("✓ 1 claude sessions run the current hooks", text)

    def test_ps_failure_warns_instead_of_guessing(self):
        def broken():
            raise CockpitError("could not list processes")
        code, text = self.run_doctor([], self.context(run_ps=broken), self.LOCAL[1:2])
        self.assertEqual(code, 0)
        self.assertIn("! could not check running sessions' hooks", text)

    def test_future_dated_hooks_json_warns_and_flags_nobody(self):
        hooks = self.home.parent / "checkout" / "plugins" / "orchestrator" / "hooks" / "hooks.json"
        set_mtime(hooks, int(time.time() * 1000) + 24 * HOUR_MS)
        code, text = self.run_doctor([], self.context(), self.LOCAL)
        self.assertEqual(code, 0, text)
        self.assertIn("or the install date is in the future", text)
        self.assertIn("→ touch ", text)
        self.assertNotIn("✗", text)
        self.assertIn("hooks age unknown for pid 100, 200", text)

    def test_switch_off_and_unwritable_data_dir(self):
        ctx = self.context(env={"COCKPIT_GUARD": "off"})
        self.assertEqual(doctor_cmd.check_switches(ctx)[0].status, doctor_cmd.WARN)
        self.assertIn("COCKPIT_GUARD=off (environment)", doctor_cmd.check_switches(ctx)[0].label)
        self.assertEqual(doctor_cmd.check_data_dir(ctx)[0].status, doctor_cmd.OK)

    def test_policy_check_flags_sessions_with_an_older_policy(self):
        project = self.home / "projects" / "-srv-repo"
        project.mkdir(parents=True)
        (project / f"{SID[100]}.jsonl").write_bytes(
            b'{"attachment":{"hookEvent":"SessionStart","content":"(claude-cockpit policy v1.5)"}}\n')
        (project / f"{SID[200]}.jsonl").write_bytes(
            b'{"attachment":{"hookEvent":"SessionStart","content":"(claude-cockpit policy v1.6)"}}\n')
        result = doctor_cmd.check_policy(self.context())[0]
        self.assertEqual(result.status, doctor_cmd.WARN)
        self.assertIn("v1.6 installed; older in repo-100: v1.5", result.label)
        self.assertEqual(result.hint, "exit, then claude --resume <id> in that directory")
        self.assertNotIn("/clear", result.hint)

    def test_missing_install_fails(self):
        ctx = doctor_cmd.build_context({"CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "empty")}, lambda: "")
        code, text = self.run_doctor([], ctx, self.LOCAL[:2])
        self.assertEqual(code, 1)
        self.assertIn("not installed", text)
        self.assertIn("no hooks.json in effect", text)

    def test_unknown_option_exits_2(self):
        self.assertEqual(cli.main(["doctor", "--jsn"]), 2)

    def test_exit_code_rules(self):
        make = doctor_cmd.Check
        self.assertEqual(doctor_cmd.exit_code([make("a", "ok", "x"), make("b", "warn", "y")]), 0)
        self.assertEqual(doctor_cmd.exit_code([make("a", "ok", "x"), make("b", "fail", "y")]), 1)


class ListMarkerTests(unittest.TestCase):
    def test_ls_row_marks_stale_hooks_without_moving_columns(self):
        s = session(100, "sid")
        plain, marked = cli.session_row(s, NOW_MS), cli.session_row(s, NOW_MS, hooks_stale=True)
        self.assertIn("repo-100 ⟳", marked)
        self.assertEqual(len(plain), len(marked))
        self.assertEqual(plain.index("/Users"), marked.index("/Users"))
        long = Session(**dict(s.__dict__, name="x" * 40))
        self.assertEqual(cli.name_cell(long, True), "x" * 26 + " ⟳")
        self.assertEqual(len(cli.name_cell(long, False)), 28)

    def test_ls_json_field(self):
        self.assertIs(cli.hooks_stale_json(STALE), True)
        self.assertIs(cli.hooks_stale_json(CURRENT), False)
        self.assertIsNone(cli.hooks_stale_json(UNKNOWN))
        self.assertIsNone(cli.hooks_stale_json(None))


if __name__ == "__main__":
    unittest.main()
