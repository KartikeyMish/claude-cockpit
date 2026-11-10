"""scripts/release.py end to end, through a fake Runner.

No git, gh, make or claude command really runs and nothing is pushed. FakeRunner
keeps just enough state (HEAD, tags, origin's refs, the Release, the tap) to answer
like a healthy repository and GitHub, and each test breaks one thing.
"""

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release  # noqa: E402
from release import Release, ReleaseError, Repo, Result, Runner  # noqa: E402

SHA = "a" * 40        # HEAD == origin/main before the release
NEW = "c" * 40        # the release commit
TAG_OBJECT = "d" * 40
TAP_SHA = "e" * 40
TAP_NEW = "f" * 40
TARBALL = b"fake release tarball"
TARBALL_SHA = hashlib.sha256(TARBALL).hexdigest()
CASK_TEXT = '''cask "claude-cockpit" do
  version "1.5.0"
  # `make release` sets version and sha256 from the published .sha256 asset;
  # all zeros means "not released yet".
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
end
'''
CHANGELOG = """# Changelog

Intro.

## [Unreleased]
- **New thing.** It works.

### Details
- more

## [1.5.0] - 2026-11-10
First public release.

## [1.3.0] - 2026-11-07
- Older.
"""
NO_UNRELEASED = CHANGELOG.replace("- **New thing.** It works.\n\n### Details\n- more\n", "")


class Workspace:
    """A temporary copy of the version-bearing files plus a tap checkout."""

    def __init__(self, tmp, changelog=CHANGELOG):
        self.root = Path(tmp) / "repo"
        for rel in ("plugins/orchestrator/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
                    "lib/cockpit/__init__.py", "plugins/orchestrator/hooks/routing-policy.md"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, self.root / rel)
        (self.root / "CHANGELOG.md").write_text(changelog)
        self.tap = Path(tmp) / "tap"
        (self.tap / "Casks").mkdir(parents=True)
        self.cask.write_text(CASK_TEXT)
        self.repo = Repo(self.root)

    @property
    def cask(self):
        return self.tap / "Casks" / "claude-cockpit.rb"

    def snapshot(self):
        return {p: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}


class FakeRunner(Runner):
    """A healthy repository, origin and GitHub. `overrides` maps an argv prefix to a Result
    (or to a callable taking argv), checked longest prefix first."""

    def __init__(self, root, dry_run=False, overrides=None, tap=None, **state):
        self.output = []
        super().__init__(root, dry_run=dry_run, echo=self.output.append)
        self.calls = []
        self.overrides = overrides or {}
        self.tap = str(tap) if tap else None
        self.head = SHA
        self.tags = {}
        self.remote = {"refs/heads/main": SHA}
        self.release_ready = False
        self.tap_head = TAP_SHA
        self.tap_remote = TAP_SHA
        self.tap_branch = "main"
        self.tap_current = "main"
        for key, value in state.items():
            setattr(self, key, value)

    def execute(self, argv, cwd, stream, timeout):
        self.calls.append(argv)
        for prefix, answer in sorted(self.overrides.items(), key=lambda kv: -len(kv[0])):
            if tuple(argv[:len(prefix)]) == prefix:
                return answer(argv) if callable(answer) else answer
        if self.tap and argv[:3] == ["git", "-C", self.tap]:
            return self.tap_git(argv[3:])
        if argv[0] == "git":
            return self.repo_git(argv[1:])
        if argv[0] == "make":
            return Result(0, "", "Ran 12 tests in 1.0s\n\nOK\n")
        if argv[0] == "gh":
            return self.gh(argv[1:])
        return Result(0)

    def ref_line(self, ref):
        sha = self.remote.get(ref)
        return Result(0, f"{sha}\t{ref}\n") if sha else Result(2)

    def repo_git(self, args):
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return Result(0, "main\n")
        if args == ["rev-parse", "HEAD"]:
            return Result(0, self.head + "\n")
        if args[:3] == ["rev-parse", "-q", "--verify"]:
            return Result(0 if args[3].split("/")[-1] in self.tags else 1)
        if args[:3] == ["rev-parse", "--verify", "-q"]:
            commit = self.tags.get(args[3][len("refs/tags/"):-len("^{commit}")])
            return Result(0, commit + "\n") if commit else Result(1)
        if args[:2] == ["ls-remote", "--exit-code"]:
            return self.ref_line(args[-1])
        if args[:2] == ["ls-remote", "origin"]:
            ref = args[2]
            sha = self.remote.get(ref)
            return Result(0, f"{TAG_OBJECT}\t{ref}\n{sha}\t{ref}^{{}}\n" if sha else "")
        if args[0] == "commit":
            self.head = NEW
        elif args[:2] == ["tag", "-a"]:
            self.tags[args[2]] = self.head
        elif args[:2] == ["push", "--atomic"]:
            for ref in args[3:]:
                self.remote[ref] = self.head if ref.startswith("refs/heads/") else self.tags[ref.split("/")[-1]]
        elif args[:2] == ["push", "origin"]:
            sha, ref = args[2].split(":")
            self.remote[ref] = sha
        return Result(0)

    def tap_git(self, args):
        if args == ["rev-parse", "--show-toplevel"]:
            return Result(0, self.tap + "\n")
        if args == ["ls-remote", "--symref", "origin", "HEAD"]:
            return Result(0, f"ref: refs/heads/{self.tap_branch}\tHEAD\n{self.tap_remote}\tHEAD\n")
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return Result(0, self.tap_current + "\n")
        if args == ["rev-parse", "HEAD"]:
            return Result(0, self.tap_head + "\n")
        if args[:2] == ["ls-remote", "--exit-code"]:
            ok = args[-1] == f"refs/heads/{self.tap_branch}"
            return Result(0, f"{self.tap_remote}\t{args[-1]}\n") if ok else Result(2)
        if args[0] == "commit":
            self.tap_head = TAP_NEW
        elif args[0] == "push":
            self.tap_remote = self.tap_head
        return Result(0)

    def gh(self, args):
        if args[:2] == ["run", "list"]:
            return Result(0, json.dumps([{"databaseId": 42, "headBranch": "v1.6.0"}]))
        if args[:2] == ["run", "watch"]:
            self.release_ready = True
            return Result(0)
        if args[:2] == ["release", "view"]:
            if not self.release_ready:
                return Result(1, "", "release not found")
            version = args[2][1:]
            names = [f"claude-cockpit-{version}.tar.gz", f"claude-cockpit-{version}.tar.gz.sha256"]
            return Result(0, json.dumps({"url": "https://example/r", "isDraft": False,
                                         "assets": [{"name": n} for n in names]}))
        if args[:2] == ["release", "download"]:
            return self.download(args)
        return Result(0)

    def download(self, args):
        directory = Path(args[args.index("--dir") + 1])
        name = f"claude-cockpit-{args[2][1:]}.tar.gz"
        (directory / name).write_bytes(TARBALL)
        (directory / f"{name}.sha256").write_text(f"{TARBALL_SHA}  {name}\n")
        return Result(0)

    def ran(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]

    def text(self):
        return "\n".join(self.output)


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, version="1.6.0", dry_run=False, overrides=None, runner_class=None, state=None, **kw):
        tap = kw.get("tap", self.ws.tap)
        runner = (runner_class or FakeRunner)(self.ws.root, dry_run=dry_run, overrides=overrides, tap=tap,
                                              **(state or {}))
        options = {"tap": self.ws.tap, "assume_yes": True, "is_tty": lambda: True, **kw}
        rel = Release(repo=self.ws.repo, version=version, run=runner, today="2026-12-01",
                      echo=runner.output.append, sleep=lambda s: None, has_claude=False, **options)
        return rel, runner

    def assert_nothing_pushed(self, runner):
        self.assertFalse(runner.ran("git", "push"), runner.text())


class CutTests(FlowCase):
    def test_regular_release_bumps_tags_publishes_then_moves_stable_and_tap(self):
        rel, runner = self.make()
        self.assertEqual(rel.cut(), 0, runner.text())
        self.assertEqual(self.ws.repo.version_problems("1.6.0"), [])
        self.assertIn("## [Unreleased]\n\n## [1.6.0] - 2026-12-01\n", (self.ws.root / "CHANGELOG.md").read_text())
        self.assertEqual(runner.ran("git", "commit"), [["git", "commit", "-m", "chore: release v1.6.0"]],
                         "the release commit message is exactly this: no body, no attribution trailers")
        self.assertFalse([c for c in runner.calls if any("Co-Authored-By" in arg for arg in c)])
        self.assertEqual(runner.tags, {"v1.6.0": NEW}, "the tag is on the release commit")
        self.assertTrue(runner.ran("git", "rev-parse", "--verify", "-q", "refs/tags/v1.6.0^{commit}"))
        self.assertTrue(runner.ran("git", "push", "--atomic", "origin", "refs/heads/main", "refs/tags/v1.6.0"))
        order = [c[:3] for c in runner.calls if c[:3] in (["gh", "run", "watch"], ["git", "push", "origin"])]
        self.assertEqual(order, [["gh", "run", "watch"], ["git", "push", "origin"]],
                         "stable moves only after the release workflow succeeded")
        self.assertEqual(runner.remote["refs/heads/stable"], NEW)
        cask = self.ws.cask.read_text()
        self.assertIn('version "1.6.0"', cask)
        self.assertIn(f'sha256 "{TARBALL_SHA}"', cask, "the all-zero placeholder is replaced")
        self.assertIn("# all zeros means", cask, "comments are left alone")
        self.assertEqual(runner.ran("git", "-C", str(self.ws.tap), "push"),
                         [["git", "-C", str(self.ws.tap), "push", "origin", "HEAD:refs/heads/main"]])
        self.assertFalse(runner.ran("gh", "repo"), "a private tap is fine: its visibility is never checked")

    def test_first_release_tags_the_current_commit_without_a_bump(self):
        (self.ws.root / "CHANGELOG.md").write_text(NO_UNRELEASED)
        before = self.ws.snapshot()
        rel, runner = self.make(version="1.5.0")
        self.assertEqual(rel.cut(), 0, runner.text())
        self.assertEqual(self.ws.snapshot(), before, "no files change on the first release")
        self.assertFalse(runner.ran("git", "commit") or runner.ran("git", "add"))
        self.assertEqual(runner.tags, {"v1.5.0": SHA})
        self.assertTrue(runner.ran("git", "push", "--atomic", "origin", "refs/tags/v1.5.0"))
        self.assertFalse([c for c in runner.calls if "refs/heads/main" in c and "push" in c])
        self.assertEqual(runner.remote["refs/heads/stable"], SHA, "the first release creates stable")

    def test_first_release_with_unreleased_entries_is_refused(self):
        rel, runner = self.make(version="1.5.0")
        with self.assertRaisesRegex(ReleaseError, "would not include"):
            rel.cut()
        self.assertFalse(runner.ran("git", "tag"))

    def test_dry_run_changes_nothing_and_reports_every_problem(self):
        before = self.ws.snapshot()
        overrides = {("git", "status", "--porcelain"): Result(0, " M README.md\n"),
                     ("gh", "auth", "status"): Result(1)}
        rel, runner = self.make(dry_run=True, overrides=overrides)
        self.assertEqual(rel.cut(), 1)
        self.assertEqual(self.ws.snapshot(), before)
        self.assertEqual(self.ws.cask.read_text(), CASK_TEXT)
        mutating = [c for c in runner.calls if c[:2] in (["git", "commit"], ["git", "tag"], ["git", "push"])
                    or c[:3] in (["gh", "release", "create"], ["gh", "run", "watch"])
                    or (c[:2] == ["git", "-C"] and c[3] in ("add", "commit", "push"))]
        self.assertEqual(mutating, [], "a dry run executes nothing that changes state")
        text = runner.text()
        self.assertIn("[dry-run] would run: git push --atomic origin refs/heads/main refs/tags/v1.6.0", text)
        self.assertIn("[dry-run] would update CHANGELOG.md", text)
        self.assertIn(f"git -C {self.ws.tap} push origin HEAD:refs/heads/main", text)
        self.assertIn("working tree is not clean", text)
        self.assertIn("gh is not logged in", text)
        self.assertIn("2 problem(s)", text)
        self.assertTrue(runner.ran("make", "test"), "a dry run still runs the tests")

    def test_clean_dry_run_succeeds(self):
        rel, runner = self.make(dry_run=True)
        self.assertEqual(rel.cut(), 0, runner.text())

    def test_preflight_failures_stop_before_anything_changes(self):
        tap = str(self.ws.tap)
        cases = {
            "not main": ({("git", "rev-parse", "--abbrev-ref", "HEAD"): Result(0, "feature\n")}, {}),
            "not clean": ({("git", "status", "--porcelain"): Result(0, "?? x\n")}, {}),
            "not origin/main": ({("git", "ls-remote", "--exit-code", "origin", "refs/heads/main"):
                                 Result(0, "b" * 40 + "\trefs/heads/main\n")}, {}),
            "already exists": ({("git", "ls-remote", "--exit-code", "--tags"): Result(0, "x\trefs/tags/v1.6.0\n")},
                               {}),
            "not logged in": ({("gh", "auth", "status"): Result(1)}, {}),
            "not an ancestor": ({("git", "merge-base"): Result(1)}, {"remote": {
                "refs/heads/main": SHA, "refs/heads/stable": "9" * 40}}),
            "git fetch origin": ({("git", "merge-base"): Result(128, "", "fatal: bad object")}, {"remote": {
                "refs/heads/main": SHA, "refs/heads/stable": "9" * 40}}),
            "not its default branch": ({}, {"tap_current": "wip"}),
            "pull or push the tap": ({}, {"tap_head": TAP_NEW}),
            "default branch of": ({("git", "-C", tap, "ls-remote", "--symref"): Result(128, "", "denied")}, {}),
        }
        for phrase, (overrides, state) in cases.items():
            with self.subTest(case=phrase):
                before = self.ws.snapshot()
                rel, runner = self.make(overrides=overrides, state=state)
                with self.assertRaisesRegex(ReleaseError, phrase):
                    rel.cut()
                self.assertEqual(self.ws.snapshot(), before)
                self.assertFalse(runner.ran("make"), "preflight runs before the tests")
                self.assert_nothing_pushed(runner)

    def test_existing_stable_that_can_fast_forward_is_fine(self):
        rel, runner = self.make(state={"remote": {"refs/heads/main": SHA, "refs/heads/stable": "9" * 40}})
        self.assertEqual(rel.cut(), 0, runner.text())
        self.assertTrue(runner.ran("git", "merge-base", "--is-ancestor", "9" * 40, SHA))
        self.assertEqual(runner.remote["refs/heads/stable"], NEW)

    def test_without_a_terminal_and_without_yes_preflight_fails(self):
        rel, runner = self.make(assume_yes=False, is_tty=lambda: False)
        with self.assertRaisesRegex(ReleaseError, "not a terminal.*--yes"):
            rel.cut()
        self.assertFalse(runner.ran("make"), "caught before the bump, not after the tag")
        self.assertFalse(runner.ran("git", "tag"))

    def test_version_must_go_up_and_unreleased_must_have_entries(self):
        for version, phrase in (("1.4.0", "not higher"), ("1.5", "not a version")):
            with self.subTest(version=version):
                rel, _ = self.make(version=version)
                with self.assertRaisesRegex(ReleaseError, phrase):
                    rel.cut()
        (self.ws.root / "CHANGELOG.md").write_text(NO_UNRELEASED)
        rel, _ = self.make()
        with self.assertRaisesRegex(ReleaseError, "no entries"):
            rel.cut()

    def test_missing_or_dirty_tap_is_caught_in_preflight(self):
        rel, _ = self.make(overrides={("git", "-C", str(self.ws.tap), "status"): Result(0, " M Casks/x.rb\n")})
        with self.assertRaisesRegex(ReleaseError, "uncommitted"):
            rel.cut()
        inside_home_repo = {("git", "-C", str(self.ws.tap), "rev-parse", "--show-toplevel"):
                            Result(0, str(self.ws.tap.parent) + "\n")}
        rel, _ = self.make(overrides=inside_home_repo)
        with self.assertRaisesRegex(ReleaseError, "not its own git checkout"):
            rel.cut()
        shutil.rmtree(self.ws.tap)
        rel, _ = self.make()
        with self.assertRaisesRegex(ReleaseError, "TAP_DIR"):
            rel.cut()

    def test_no_tap_skips_the_tap(self):
        shutil.rmtree(self.ws.tap)
        rel, runner = self.make(tap=None)
        self.assertEqual(rel.cut(), 0, runner.text())
        self.assertFalse([c for c in runner.calls if "-C" in c])


class AbortTests(FlowCase):
    """Whatever stops a release before the push leaves the tree as it was, or says how to undo it."""

    def test_failing_tests_restore_the_bump(self):
        before = self.ws.snapshot()
        rel, runner = self.make(overrides={("make", "test"): Result(1, "", "FAILED (failures=1)")})
        with self.assertRaisesRegex(ReleaseError, "make test failed"):
            rel.cut()
        self.assertEqual(self.ws.snapshot(), before)
        self.assertFalse(runner.ran("git", "commit"))
        self.assertIn("restored the version bump", runner.text())

    def test_interrupt_during_the_tests_restores_the_bump(self):
        def interrupt(argv):
            raise KeyboardInterrupt

        before = self.ws.snapshot()
        rel, runner = self.make(overrides={("make", "test"): interrupt})
        with self.assertRaises(KeyboardInterrupt):
            rel.cut()
        self.assertEqual(self.ws.snapshot(), before)
        self.assertFalse(runner.ran("git", "tag"))

    def test_failed_commit_restores_and_unstages_the_bump_and_never_tags(self):
        before = self.ws.snapshot()
        rel, runner = self.make(overrides={("git", "commit"): Result(1, "", "error: gpg failed to sign the data")})
        with self.assertRaisesRegex(ReleaseError, "git commit failed: error: gpg failed"):
            rel.cut()
        self.assertEqual(self.ws.snapshot(), before)
        (reset,) = runner.ran("git", "reset")
        self.assertEqual(reset[:4], ["git", "reset", "-q", "--"])
        self.assertIn("CHANGELOG.md", reset)
        self.assertFalse(runner.ran("git", "tag"))
        self.assert_nothing_pushed(runner)
        self.assertNotIn("To undo", runner.text(), "nothing was committed, so there is nothing to undo")

    def test_commit_that_does_not_move_head_is_caught(self):
        before = self.ws.snapshot()
        rel, runner = self.make(overrides={("git", "commit"): Result(0, "nothing to commit")})
        with self.assertRaisesRegex(ReleaseError, "did not create a commit"):
            rel.cut()
        self.assertEqual(self.ws.snapshot(), before)
        self.assertFalse(runner.ran("git", "tag"))
        self.assert_nothing_pushed(runner)

    def test_failed_tag_prints_the_undo_and_pushes_nothing(self):
        rel, runner = self.make(overrides={("git", "tag"): Result(128, "", "fatal: tag 'v1.6.0' already exists")})
        with self.assertRaisesRegex(ReleaseError, "git tag failed"):
            rel.cut()
        self.assert_nothing_pushed(runner)
        self.assertIn("To undo the local release: git reset --hard HEAD~1", runner.text())

    def test_tag_on_the_wrong_commit_is_caught(self):
        wrong = {("git", "rev-parse", "--verify", "-q"): Result(0, SHA + "\n")}
        rel, runner = self.make(overrides=wrong)
        with self.assertRaisesRegex(ReleaseError, "points at aaaaaaa, not the release commit ccccccc"):
            rel.cut()
        self.assert_nothing_pushed(runner)
        self.assertIn("To undo the local release: git tag -d v1.6.0 && git reset --hard HEAD~1", runner.text())

    def test_declined_confirmation_pushes_nothing(self):
        rel, runner = self.make(ask=lambda question: "n", assume_yes=False)
        self.assertEqual(rel.cut(), 1)
        self.assert_nothing_pushed(runner)
        self.assertIn("git tag -d v1.6.0 && git reset --hard HEAD~1", runner.text())

    def test_first_release_undo_only_drops_the_tag(self):
        (self.ws.root / "CHANGELOG.md").write_text(NO_UNRELEASED)
        rel, runner = self.make(version="1.5.0", ask=lambda question: "", assume_yes=False)
        self.assertEqual(rel.cut(), 1)
        self.assertIn("To undo the local release: git tag -d v1.5.0\n", runner.text() + "\n")

    def test_interrupt_at_the_prompt_prints_the_undo(self):
        def interrupt(question):
            raise KeyboardInterrupt

        rel, runner = self.make(ask=interrupt, assume_yes=False)
        with self.assertRaises(KeyboardInterrupt):
            rel.cut()
        self.assert_nothing_pushed(runner)
        self.assertIn("git tag -d v1.6.0 && git reset --hard HEAD~1", runner.text())

    def test_rejected_push_prints_the_undo(self):
        rejected = {("git", "push", "--atomic"): Result(1, "", "! [rejected] main -> main (fetch first)")}
        rel, runner = self.make(overrides=rejected)
        with self.assertRaisesRegex(ReleaseError, "nothing was published"):
            rel.cut()
        self.assertIn("git tag -d v1.6.0 && git reset --hard HEAD~1", runner.text())
        self.assertNotIn("release-finish", runner.text())


class AfterPushTests(FlowCase):
    """Once the tag is pushed, an error names what is left and how to finish it."""

    def test_failed_workflow_leaves_stable_and_tap_alone_and_names_finish(self):
        rel, runner = self.make(overrides={("gh", "run", "watch"): Result(1)})
        with self.assertRaisesRegex(ReleaseError, "stable did not move"):
            rel.cut()
        self.assertNotIn("refs/heads/stable", runner.remote)
        self.assertEqual(self.ws.cask.read_text(), CASK_TEXT)
        text = runner.text()
        self.assertIn("v1.6.0 is pushed. Still to do: wait for the release.yml run", text)
        self.assertIn("fast-forward stable to v1.6.0; update the Homebrew cask.", text)
        self.assertIn("make release-finish VERSION=1.6.0", text)
        self.assertNotIn("To undo", text, "a pushed release is finished, not undone")

    def test_checksum_mismatch_keeps_the_tap_unchanged(self):
        class BadChecksum(FakeRunner):
            def download(self, args):
                super().download(args)
                directory = Path(args[args.index("--dir") + 1])
                (directory / "claude-cockpit-1.6.0.tar.gz.sha256").write_text("f" * 64 + "  x\n")
                return Result(0)

        rel, runner = self.make(runner_class=BadChecksum)
        with self.assertRaisesRegex(ReleaseError, "does not match"):
            rel.cut()
        self.assertEqual(self.ws.cask.read_text(), CASK_TEXT)
        self.assertEqual(runner.remote["refs/heads/stable"], NEW)
        self.assertIn("Still to do: update the Homebrew cask.", runner.text())

    def test_failed_tap_push_names_finish(self):
        rel, runner = self.make(overrides={("git", "-C", str(self.ws.tap), "push"): Result(1, "", "denied")})
        with self.assertRaisesRegex(ReleaseError, "git push in"):
            rel.cut()
        self.assertIn("make release-finish VERSION=1.6.0", runner.text())


class FinishTests(FlowCase):
    """`release.py finish X.Y.Z` against a tag that is already on origin."""

    def pushed(self, **extra):
        return {"head": NEW, "tags": {"v1.6.0": NEW},
                "remote": {"refs/heads/main": NEW, "refs/tags/v1.6.0": NEW}, **extra}

    def test_tag_must_be_on_origin(self):
        rel, runner = self.make()
        with self.assertRaisesRegex(ReleaseError, "not on origin; cut the release first"):
            rel.finish()
        self.assert_nothing_pushed(runner)

    def test_finish_does_every_remaining_step(self):
        rel, runner = self.make(state=self.pushed())
        self.assertEqual(rel.finish(), 0, runner.text())
        self.assertTrue(runner.ran("gh", "run", "watch"))
        self.assertEqual(runner.remote["refs/heads/stable"], NEW, "stable goes to the peeled tag commit")
        self.assertIn(f'sha256 "{TARBALL_SHA}"', self.ws.cask.read_text())
        self.assertEqual(runner.tap_remote, TAP_NEW)
        self.assertFalse(runner.ran("git", "commit") or runner.ran("git", "tag"))

    def test_finish_skips_steps_already_done(self):
        self.ws.cask.write_text(CASK_TEXT.replace('"1.5.0"', '"1.6.0"').replace("0" * 64, TARBALL_SHA))
        state = self.pushed(release_ready=True)
        state["remote"] = {**state["remote"], "refs/heads/stable": NEW}
        rel, runner = self.make(state=state)
        self.assertEqual(rel.finish(), 0, runner.text())
        self.assertFalse(runner.ran("gh", "run", "watch"))
        self.assert_nothing_pushed(runner)
        self.assertFalse([c for c in runner.calls if c[:2] == ["git", "-C"] and c[3] in ("add", "commit")])
        text = runner.text()
        for phrase in ("Release already published", "stable is already at v1.6.0", "cask already says 1.6.0",
                       "is up to date"):
            self.assertIn(phrase, text)

    def test_finish_pushes_a_tap_commit_left_unpushed(self):
        self.ws.cask.write_text(CASK_TEXT.replace('"1.5.0"', '"1.6.0"').replace("0" * 64, TARBALL_SHA))
        state = self.pushed(release_ready=True, tap_head=TAP_NEW)
        state["remote"] = {**state["remote"], "refs/heads/stable": NEW}
        rel, runner = self.make(state=state)
        self.assertEqual(rel.finish(), 0, runner.text())
        self.assertEqual(runner.ran("git", "-C", str(self.ws.tap), "push"),
                         [["git", "-C", str(self.ws.tap), "push", "origin", "HEAD:refs/heads/main"]])
        self.assertFalse(runner.ran("git", "-C", str(self.ws.tap), "commit"))

    def test_finish_refuses_a_stable_that_cannot_fast_forward(self):
        state = self.pushed()
        state["remote"] = {**state["remote"], "refs/heads/stable": "9" * 40}
        rel, runner = self.make(state=state, overrides={("git", "merge-base"): Result(1)})
        with self.assertRaisesRegex(ReleaseError, "not an ancestor"):
            rel.finish()
        self.assertFalse(runner.ran("gh", "run", "watch"))
        self.assert_nothing_pushed(runner)

    def test_finish_dry_run_prints_and_changes_nothing(self):
        rel, runner = self.make(dry_run=True, state=self.pushed())
        self.assertEqual(rel.finish(), 0, runner.text())
        self.assertFalse(runner.ran("gh", "run", "watch"))
        self.assertEqual(self.ws.cask.read_text(), CASK_TEXT)
        self.assertIn(f"would run: git push origin {NEW}:refs/heads/stable", runner.text())


class CommandLineTests(unittest.TestCase):
    def test_notes_and_verify_tag(self):
        quiet = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            ws = Workspace(tmp)
            self.assertEqual(release.main(["verify-tag", "v1.5.0"], repo=ws.repo), 0)
            self.assertEqual(release.main(["verify-tag", "v1.6.0"], repo=ws.repo), 1)
            self.assertEqual(release.main(["verify-tag", "1.5.0"], repo=ws.repo), 1)
            self.assertEqual(release.main(["verify-tag", "v1.5.0; rm -rf /"], repo=ws.repo), 1)
            self.assertEqual(release.main(["notes", "9.9.9"], repo=ws.repo), 1)


if __name__ == "__main__":
    unittest.main()
