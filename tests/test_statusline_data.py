"""statusline_data: git, transcripts read across renders, other sessions, the usage alert.

Fixtures live in temporary directories; nothing reads the real ~/.claude, and the
alert never reaches the real osascript (spawn is injected).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from cockpit import core, team  # noqa: E402
from cockpit import statusline_data as data  # noqa: E402
from cockpit.pricing import request_cost  # noqa: E402

MODEL = "claude-opus-5-5"


def usage(inp=2, write=0, read=0, out=10, ttl="5m"):
    detail = {"ephemeral_5m_input_tokens": write if ttl == "5m" else 0,
              "ephemeral_1h_input_tokens": write if ttl == "1h" else 0}
    return {"input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read,
            "output_tokens": out, "cache_creation": detail}


def assistant(msg_id, use, ts="2026-11-27T10:00:00.000Z", model=MODEL):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": msg_id, "model": model, "role": "assistant", "usage": use,
                        "content": [{"type": "text", "text": "hi"}]}}


def write_lines(path, records, mode="w"):
    with open(path, mode, encoding="utf-8") as handle:
        handle.writelines((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records)


class SharedConstantsTests(unittest.TestCase):
    """The copies in statusline_data must stay identical to the dashboard's originals."""

    def test_copies_match_team_and_core(self):
        self.assertEqual(data.ASSISTANT_RE.pattern, team.ASSISTANT_RE.pattern)
        self.assertEqual(data.STALL_SECONDS, team.STALL_SECONDS)
        self.assertEqual(data.CLAUDE_HOME, core.CLAUDE_HOME)
        self.assertEqual(data.SESSIONS_DIR, core.SESSIONS_DIR)
        for value in (None, "12", 12, True, "x", 3.7, [1]):
            self.assertEqual(data.optional_int(value), core.optional_int(value), value)
        for value in ("2026-11-27T10:00:00.000Z", "2026-11-27T10:00:00+05:30", "nope", None, 5):
            self.assertEqual(data.parse_ts(value), team.parse_ts(value), value)


class StateDirTests(unittest.TestCase):
    def test_private_dir_created_and_foreign_or_linked_dirs_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = data.state_dir({"COCKPIT_STATE_DIR": tmp + "/s"})
            self.assertEqual(path, Path(tmp) / "s" / "statusline")
            self.assertTrue(path.is_dir())
            os.symlink(tmp + "/s", tmp + "/link")
            self.assertIsNone(data.state_dir({"COCKPIT_STATE_DIR": tmp + "/link"}))

    def test_old_state_is_pruned_at_most_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            old, fresh = folder / "session-old.json", folder / "git-fresh.json"
            old.write_text("{}")
            fresh.write_text("{}")
            os.utime(old, (time.time() - 30 * 86400,) * 2)
            self.assertEqual(data.prune_state(folder), 1)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), [".pruned", "git-fresh.json"])
            os.utime(fresh, (time.time() - 30 * 86400,) * 2)
            self.assertEqual(data.prune_state(folder), 0, "already pruned today")

    def test_leftover_temp_files_older_than_an_hour_are_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            stale, live = folder / ".session-a.json.99.tmp", folder / ".session-b.json.98.tmp"
            stale.write_text("{")
            live.write_text("{")
            os.utime(stale, (time.time() - 2 * 3600,) * 2)
            data.prune_state(folder)
            self.assertFalse(stale.exists())
            self.assertTrue(live.exists(), "a render may still be writing it")

    def test_corrupt_state_is_ignored_and_writes_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text("{not json")
            self.assertEqual(data.load_json(path), {})
            data.save_json(path, {"a": 1})
            self.assertEqual(data.load_json(path), {"a": 1})
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["x.json"])


class GitTests(unittest.TestCase):
    def test_porcelain_v2_parsing(self):
        text = ("# branch.oid 0123456789abcdef\n# branch.head feature/x\n# branch.upstream origin/feature/x\n"
                "# branch.ab +2 -1\n1 .M N... 100644 100644 100644 a b f1\n? new.txt\n")
        self.assertEqual(data.parse_porcelain(text),
                         {"branch": "feature/x", "oid": "0123456", "ahead": 2, "behind": 1, "dirty": 2})
        detached = data.parse_porcelain("# branch.oid abcdef1234\n# branch.head (detached)\n")
        self.assertEqual((detached["branch"], detached["oid"], detached["dirty"]), (None, "abcdef1", 0))

    def test_results_are_cached_per_directory_for_ten_seconds(self):
        calls = []

        def runner(cwd):
            calls.append(cwd)
            return "ok", {"branch": "main", "oid": None, "ahead": 0, "behind": 0, "dirty": 1}

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            for now in (100.0, 105.0, 109.9):
                self.assertEqual(data.git_info(tmp, state, now, runner)["branch"], "main")
            self.assertEqual(len(calls), 1)
            data.git_info(tmp, state, 110.5, runner)
            self.assertEqual(len(calls), 2)

    def test_timeout_falls_back_to_head_file_and_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / ".git" / "HEAD").write_text("ref: refs/heads/slow-repo\n")
            calls = []

            def slow(cwd):
                calls.append(cwd)
                return "timeout", None

            state = Path(tmp) / "state"
            state.mkdir()
            info = data.git_info(tmp, state, 1.0, slow)
            self.assertEqual((info["branch"], info["dirty"]), ("slow-repo", None))
            data.git_info(tmp, state, 2.0, slow)
            self.assertEqual(len(calls), 1, "a timeout is cached too, so a slow repo costs one wait per 10s")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_non_utf8_branch_names_do_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
            subprocess.run(["git", "init", "-q", tmp], env=env, check=True)
            subprocess.run(["git", "-C", tmp, "symbolic-ref", "HEAD", b"refs/heads/caf\xe9"], env=env, check=True)
            status, info = data.run_git_status(tmp, timeout=5)
            self.assertEqual(status, "ok")
            self.assertEqual(info["branch"], "caf\ufffd")
            self.assertEqual(data.head_branch(tmp), "caf\ufffd", "the slow-git fallback decodes the same way")

    def test_outside_a_repo_and_missing_directory_give_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(data.git_info(tmp, None, 1.0, lambda cwd: ("none", None)))
        self.assertIsNone(data.git_info("/no/such/dir", None, 1.0, lambda cwd: ("ok", {})))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_real_repo_dirty_count_and_ahead(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}

            def git(*args, cwd=tmp):
                subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

            origin, clone = Path(tmp) / "origin", Path(tmp) / "clone"
            git("init", "-q", "-b", "main", str(origin))
            (origin / "a.txt").write_text("a\n")
            git("add", ".", cwd=origin)
            git("commit", "-qm", "a", cwd=origin)
            git("clone", "-q", str(origin), str(clone))
            git("checkout", "-qb", "kartikeymishra/dev1-14854-long-branch-name", cwd=clone)
            git("branch", "-q", "--set-upstream-to", "origin/main", cwd=clone)
            (clone / "b.txt").write_text("b\n")
            git("add", ".", cwd=clone)
            git("commit", "-qm", "b", cwd=clone)
            (clone / "a.txt").write_text("changed\n")
            (clone / "untracked.txt").write_text("u\n")
            with mock.patch.dict(os.environ, env):
                status, info = data.run_git_status(str(clone), timeout=5)
            self.assertEqual(status, "ok")
            self.assertEqual((info["branch"], info["dirty"], info["ahead"], info["behind"]),
                             ("kartikeymishra/dev1-14854-long-branch-name", 2, 1, 0))
            self.assertEqual(data.run_git_status(tmp, timeout=5)[0], "none")


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.main = self.dir / "sess-1.jsonl"
        self.agents = self.dir / "sess-1" / "subagents"
        self.agents.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_recent_hit_rate_and_ttl_countdown_from_main_transcript(self):
        write_lines(self.main, [
            {"type": "user", "timestamp": "2026-11-27T09:59:00.000Z", "message": {"content": "hi"}},
            assistant("m1", usage(inp=100, write=900, read=0, ttl="1h"), "2026-11-27T10:00:00.000Z"),
            assistant("m1", usage(inp=100, write=900, read=0, out=50, ttl="1h"), "2026-11-27T10:00:01.000Z"),
            assistant("m2", usage(inp=0, write=0, read=1000), "2026-11-27T10:05:00.000Z"),
        ])
        facts = data.transcript_facts(str(self.main), "sess-1", self.dir, time.time())
        cache = facts["cache"]
        self.assertAlmostEqual(cache["hit_pct"], 50.0, msg="m1 counted once: 1000 read of 2000 input")
        self.assertEqual(cache["expires_at"], data.parse_ts("2026-11-27T10:05:00.000Z") + 3600,
                         "a read-only call refreshes the 1h cache the session writes")
        self.assertIsNone(facts["agents"])

    def test_incremental_reads_keep_offsets_and_skip_partial_lines(self):
        write_lines(self.main, [assistant("m1", usage(read=0, write=1000))])
        state = data.update_main({}, self.main)
        size = self.main.stat().st_size
        self.assertEqual(state["cursor"]["offset"], size)
        with open(self.main, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(assistant("m2", usage(inp=0, read=1000)))[:40])   # still being written
        state = data.update_main(state, self.main)
        self.assertEqual(state["cursor"]["offset"], size, "an unfinished line is left for the next render")
        self.assertEqual(len(state["recent"]), 1)
        with open(self.main, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(assistant("m2", usage(inp=0, read=1000)))[40:] + "\n")
        state = data.update_main(state, self.main)
        self.assertEqual([r[0] for r in state["recent"]], ["m1", "m2"])
        self.main.write_text("")   # rewritten (e.g. /clear): starts over
        self.assertEqual(data.update_main(state, self.main)["recent"], [])

    def test_a_replaced_file_with_a_new_inode_starts_over(self):
        write_lines(self.main, [assistant("m1", usage(read=0, write=1000)), assistant("m2", usage(read=10))])
        state = data.update_main({}, self.main)
        replacement = self.dir / "new.jsonl"
        write_lines(replacement, [assistant("n1", usage(read=0, write=1000)), assistant("n2", usage(read=10)),
                                  assistant("n3", usage(read=20))])
        os.replace(replacement, self.main)   # bigger file, new inode: an offset would land mid-record
        state = data.update_main(state, self.main)
        self.assertEqual([r[0] for r in state["recent"]], ["n1", "n2", "n3"])

    def test_agents_running_done_stalled_and_cost_without_double_counting(self):
        write_lines(self.main, [assistant("m1", usage())])
        u1, u2 = usage(inp=10, write=1000, out=5), usage(inp=10, write=1000, out=400)
        records = {
            "a1": [assistant("r1", u1), assistant("r1", u2), assistant("r2", usage(read=1000, out=20))],
            "a2": [assistant("x1", usage(out=30))],
            "a3": [assistant("y1", usage(out=30))],
        }
        for aid, lines in records.items():
            write_lines(self.agents / f"agent-{aid}.jsonl", lines)
        (self.agents / "agent-a1.meta.json").write_text(json.dumps({"agentType": "x", "finished": True}))
        (self.agents / "agent-a2.meta.json").write_text(json.dumps({"agentType": "x"}))
        (self.agents / "agent-a3.meta.json").write_text(json.dumps({"agentType": "x"}))
        old = time.time() - 3600
        os.utime(self.agents / "agent-a3.jsonl", (old, old))
        expected = sum(request_cost(MODEL, u) for u in (u2, usage(read=1000, out=20), usage(out=30), usage(out=30)))
        agents = data.transcript_facts(str(self.main), "sess-1", self.dir, time.time())["agents"]
        self.assertEqual((agents["running"], agents["done"], agents["stalled"]), (1, 1, 1))
        self.assertAlmostEqual(agents["cost"], expected)
        self.assertFalse(agents["partial"])
        # The next render reads only what was appended, including a later record of a counted response.
        u3 = usage(out=60)
        write_lines(self.agents / "agent-a2.jsonl", [assistant("x1", u3), assistant("x2", usage(out=1))], "a")
        agents = data.transcript_facts(str(self.main), "sess-1", self.dir, time.time())["agents"]
        more = request_cost(MODEL, u3) - request_cost(MODEL, usage(out=30)) + request_cost(MODEL, usage(out=1))
        self.assertAlmostEqual(agents["cost"], expected + more)

    def test_agent_reading_stops_at_the_time_budget_and_resumes(self):
        write_lines(self.main, [assistant("m1", usage())])
        for i in range(3):
            write_lines(self.agents / f"agent-a{i}.jsonl", [assistant(f"r{i}", usage(out=100))])
            (self.agents / f"agent-a{i}.meta.json").write_text(json.dumps({"finished": True}))
        states, first = data.agents_summary({}, self.dir / "sess-1", time.time(), budget_s=-1)
        self.assertTrue(first["partial"])
        self.assertEqual(first["cost"], 0)
        states, second = data.agents_summary(states, self.dir / "sess-1", time.time())
        self.assertFalse(second["partial"])
        self.assertAlmostEqual(second["cost"], 3 * request_cost(MODEL, usage(out=100)))

    def test_bad_inputs_give_no_facts(self):
        self.assertEqual(data.transcript_facts("", "s", self.dir, 0), {})
        self.assertEqual(data.transcript_facts(str(self.dir / "missing.jsonl"), "s", self.dir, 0), {})
        write_lines(self.main, ["not json", '{"type":"assistant","message":"x"}', json.dumps(assistant("m", 5))])
        facts = data.transcript_facts(str(self.main), "../../etc", self.dir, 0)
        self.assertIsNone(facts["cache"])
        self.assertFalse(any(p.name.startswith("session-") for p in self.dir.iterdir()),
                         "an unsafe session id is never used as a file name")


class BoundedReadTests(unittest.TestCase):
    """Large transcripts: bounded bytes per read, steady progress, progress kept if a render dies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.agents = self.dir / "sess" / "subagents"
        self.agents.mkdir(parents=True)
        self.patch = mock.patch.multiple(data, CHUNK_BYTES=64 * 1024, AGENT_FIRST_READ_MAX_BYTES=512 * 1024,
                                         MAIN_FIRST_READ_MAX_BYTES=128 * 1024, CHECKPOINT_EVERY_S=0)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def big_agent(self, name, megabytes, filler=2000):
        """MEGABYTES of transcript: an assistant call of 1 output token after every filler line."""
        path = self.agents / f"agent-{name}.jsonl"
        pad = json.dumps({"type": "user", "message": {"content": "x" * filler}})
        with open(path, "w", encoding="utf-8") as handle:
            i = 0
            while handle.tell() < megabytes * 1024 * 1024:
                handle.write(pad + "\n" + json.dumps(assistant(f"{name}-{i}", usage(inp=0, out=1))) + "\n")
                i += 1
        (self.agents / f"agent-{name}.meta.json").write_text(json.dumps({"finished": True}))
        return path

    def test_each_read_is_bounded_and_the_backlog_drains_across_calls(self):
        path = self.big_agent("a", 2)
        size = path.stat().st_size
        cursor, offsets = {}, []
        for _ in range(20):
            before = int(cursor.get("offset") or 0)
            _, cursor, info = data.read_chunk(path, cursor, data.AGENT_FIRST_READ_MAX_BYTES)
            if offsets:
                self.assertLessEqual(cursor["offset"] - before, data.CHUNK_BYTES)
            offsets.append(cursor["offset"])
            if not info["behind"]:
                break
        self.assertEqual(offsets[-1], size)
        self.assertGreaterEqual(offsets[0], size - data.AGENT_FIRST_READ_MAX_BYTES, "first read starts near the end")
        self.assertEqual(offsets, sorted(offsets))

    def test_first_read_is_capped_marked_partial_and_progress_survives_a_cancelled_render(self):
        self.big_agent("a", 2)
        self.big_agent("b", 2)
        saved = []
        states, summary = data.agents_summary({}, self.dir / "sess", time.time(), budget_s=60,
                                               checkpoint=lambda s: saved.append(json.loads(json.dumps(s))))
        self.assertTrue(summary["partial"], "the head of each 2 MiB file was skipped")
        self.assertFalse(any(s.get("behind") for s in states.values()))
        self.assertGreaterEqual(len(saved), 2 * data.AGENT_FIRST_READ_MAX_BYTES // data.CHUNK_BYTES - 2,
                                "a checkpoint after every chunk")
        first = saved[0]
        self.assertTrue(any(s.get("cursor") for s in first.values()), "progress is on disk after one chunk")
        # A render that died after its first checkpoint resumes from it instead of starting over.
        states2, _ = data.agents_summary(first, self.dir / "sess", time.time(), budget_s=60)
        self.assertAlmostEqual(sum(s["cost"] for s in states2.values()), sum(s["cost"] for s in states.values()))

    def test_time_budget_is_checked_between_chunks(self):
        self.big_agent("a", 2)
        clock = iter(i * 0.01 for i in range(10_000))   # every clock read costs 10ms
        with mock.patch.object(data.time, "monotonic", lambda: next(clock)):
            states, summary = data.agents_summary({}, self.dir / "sess", time.time(), budget_s=0.1)
        self.assertGreater(states["a"]["cursor"]["offset"], 0, "some chunks were read")
        self.assertTrue(states["a"]["behind"], "stopped mid-file when the budget ran out")
        self.assertTrue(summary["partial"])

    def test_a_line_longer_than_a_chunk_is_skipped_not_looped_on(self):
        path = self.agents / "agent-huge.jsonl"
        write_lines(path, [assistant("r0", usage(out=1)), {"type": "user", "message": {"content": "y" * 300_000}},
                           assistant("r1", usage(out=1))])
        state = {}
        for _ in range(10):
            state = data.update_agent(state, path)
            if not state["behind"]:
                break
        self.assertEqual(state["behind"], 0)
        self.assertAlmostEqual(state["cost"], 2 * request_cost(MODEL, usage(out=1)))

    def test_main_transcript_skips_a_long_backlog(self):
        main = self.dir / "sess.jsonl"
        write_lines(main, [assistant("old", usage(read=0, write=1000))])
        state = data.update_main({}, main)
        with open(main, "a", encoding="utf-8") as handle:
            for i in range(400):
                handle.write(json.dumps({"type": "user", "message": {"content": "z" * 2000}}) + "\n")
            handle.write(json.dumps(assistant("new", usage(inp=0, read=500))) + "\n")
        before = state["cursor"]["offset"]
        state = data.update_main(state, main)
        self.assertEqual(state["cursor"]["offset"], main.stat().st_size, "caught up in one bounded read")
        self.assertGreater(main.stat().st_size - before, data.MAIN_FIRST_READ_MAX_BYTES)
        self.assertEqual(state["recent"][-1][0], "new")


class WaitingSessionsTests(unittest.TestCase):
    def test_only_other_live_interactive_waiting_sessions_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            records = [
                {"pid": 101, "sessionId": "me", "status": "waiting", "name": "self"},
                {"pid": 102, "sessionId": "a", "status": "waiting", "name": "api@main", "entrypoint": "cli"},
                {"pid": 103, "sessionId": "b", "status": "waiting", "name": "dead"},
                {"pid": 104, "sessionId": "c", "status": "waiting", "name": "sdk", "entrypoint": "sdk-cli"},
                {"pid": 105, "sessionId": "d", "status": "idle", "name": "idle"},
                {"pid": True, "sessionId": "e", "status": "waiting"},
            ]
            for i, record in enumerate(records):
                (folder / f"{i}.json").write_text(json.dumps(record))
            (folder / "broken.json").write_text("{")
            names = data.waiting_sessions("me", folder, alive=lambda pid: pid != 103)
            self.assertEqual(names, ["api@main"])
        self.assertEqual(data.waiting_sessions("me", Path("/no/such/dir")), [])
        self.assertTrue(data.pid_alive(os.getpid()))


class ContextBridgeTests(unittest.TestCase):
    """The file gsd-context-monitor.js reads (see the comment above write_context_bridge)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "claude"
        self.out = Path(self.tmp.name) / "tmp"
        self.out.mkdir()
        self.env = {"TMPDIR": str(self.out) + "/"}

    def tearDown(self):
        self.tmp.cleanup()

    def install_monitor(self):
        (self.home / "hooks").mkdir(parents=True)
        (self.home / "hooks" / "gsd-context-monitor.js").write_text("// consumer\n")

    def write(self, session="0b9c-uuid", remaining=33.5):
        payload = {"session_id": session, "context_window": {"remaining_percentage": remaining}}
        return data.write_context_bridge(payload, self.env, self.home, now=1_800_000_123.9)

    def test_written_only_when_the_monitor_hook_is_installed(self):
        self.assertIsNone(self.write())
        self.assertEqual(list(self.out.iterdir()), [])
        self.install_monitor()
        path = self.write()
        self.assertEqual(path, self.out / "claude-ctx-0b9c-uuid.json")
        self.assertEqual(json.loads(path.read_text()), {"session_id": "0b9c-uuid", "remaining_percentage": 33.5,
                                                        "used_pct": 67, "timestamp": 1_800_000_123})
        self.assertEqual([p.name for p in self.out.iterdir()], [path.name], "no temp file left behind")

    def test_same_rounding_and_tmpdir_rules_as_node(self):
        self.assertEqual([data.js_round(x) for x in (66.5, 67.5, 0.5, 66.49)], [67, 68, 1, 66])
        self.assertEqual(data.node_tmpdir({}), "/tmp")
        self.assertEqual(data.node_tmpdir({"TMP": "/a/"}), "/a")
        self.assertEqual(data.node_tmpdir({"TMPDIR": "/b/", "TMP": "/a"}), "/b")

    def test_unsafe_ids_and_missing_values_write_nothing(self):
        self.install_monitor()
        for session, remaining in (("../x", 30), ("a/b", 30), ("a..b", 30), ("", 30), (None, 30),
                                   ("ok", None), ("ok", True), ("ok", "30")):
            self.assertIsNone(self.write(session, remaining), (session, remaining))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_a_planted_symlink_is_replaced_not_followed(self):
        self.install_monitor()
        victim = Path(self.tmp.name) / "victim.txt"
        victim.write_text("keep")
        (self.out / "claude-ctx-s1.json").symlink_to(victim)
        self.write("s1")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((self.out / "claude-ctx-s1.json").is_symlink())

    def test_unwritable_tmpdir_is_not_an_error(self):
        self.install_monitor()
        self.env = {"TMPDIR": str(Path(self.tmp.name) / "missing")}
        self.assertIsNone(self.write())


class UsageAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sent = []
        self.path = mock.patch.dict(os.environ, {"PATH": str(self.fake_osascript())})
        self.path.start()

    def tearDown(self):
        self.path.stop()
        self.tmp.cleanup()

    def fake_osascript(self):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "osascript").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "osascript").chmod(0o755)
        return bin_dir

    def alert(self, pct, resets_at=1_800_000_000, environ=None, platform="darwin"):
        return data.maybe_alert({"used_percentage": pct, "resets_at": resets_at}, self.dir,
                                environ or {}, platform, self.sent.append)

    def test_once_per_window_at_the_default_threshold(self):
        self.assertFalse(self.alert(59.9))
        self.assertTrue(self.alert(61))
        self.assertFalse(self.alert(75), "same window")
        self.assertFalse(self.alert(80, resets_at=1_800_000_090), "resets_at jitter is the same window")
        self.assertTrue(self.alert(65, resets_at=1_800_018_000), "next window")
        self.assertEqual(len(self.sent), 2)
        argv = self.sent[0]
        self.assertTrue(argv[0].endswith("osascript"))
        self.assertRegex(argv[-1], r"^5-hour plan usage at 61%, resets at \d\d:\d\d$")
        self.assertIn("item 1 of argv", " ".join(argv), "the message is an argument, never AppleScript source")

    def test_threshold_env_and_platform(self):
        self.assertFalse(self.alert(99, environ={"COCKPIT_USAGE_ALERT_PCT": "0"}))
        self.assertFalse(self.alert(50, environ={"COCKPIT_USAGE_ALERT_PCT": "80"}))
        self.assertTrue(self.alert(81, environ={"COCKPIT_USAGE_ALERT_PCT": "80"}))
        self.assertFalse(self.alert(99, resets_at=1_900_000_000, platform="linux"))
        self.assertEqual(data.alert_threshold({"COCKPIT_USAGE_ALERT_PCT": "junk"}), 60.0)
        self.assertFalse(self.alert(True), "booleans are not percentages")
        self.assertFalse(data.maybe_alert({"used_percentage": 99, "resets_at": 1}, None, {}, "darwin",
                                          self.sent.append), "no state dir, no alert")


if __name__ == "__main__":
    unittest.main()
