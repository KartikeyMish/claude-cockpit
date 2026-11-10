#!/usr/bin/env python3
"""Cut a claude-cockpit release, finish one, or read the release data. Stdlib only.

  release.py cut X.Y.Z [--dry-run] [--yes] [--no-tap]   (make release)
  release.py finish X.Y.Z [--dry-run] [--no-tap]        (make release-finish)
  release.py notes X.Y.Z        print that version's CHANGELOG section
  release.py verify-tag vX.Y.Z  exit 1 unless the tag matches every version field
                                and the CHANGELOG has a section for it (release.yml)

`cut` runs, in order:
  1. preflight  on main, clean tree, HEAD == origin/main, VERSION is semver and
                higher than the current version, [Unreleased] has entries, the tag
                is free, origin's `stable` (if any) is an ancestor of HEAD, gh is
                logged in, the push can be confirmed (a terminal, or --yes), and
                the tap (TAP_DIR) is a clean checkout of its default branch that
                matches origin. The tap repository may be private;
  2. bump       plugin.json, marketplace.json, lib/cockpit/__init__.py and, for a
                new minor or major, the routing-policy title; [Unreleased] becomes
                [X.Y.Z] - today under a fresh empty [Unreleased];
  3. verify     `make test`, plus `claude plugin validate` when claude is on PATH;
  4. commit     `chore: release vX.Y.Z` and an annotated tag vX.Y.Z, each checked;
  5. publish    after you confirm: push main and the tag (atomically);
  6. release    wait for the Release workflow, check the Release and its assets;
  7. stable     fast-forward `stable` to the tag, only now, so plugin installs
                never see a version whose release failed;
  8. tap        set the cask's version and sha256 from the published .sha256 asset
                (checked against the tarball), commit and push the tap's branch.

Anything that stops steps 2-5 puts the tree back: the bumped files are restored
before the commit, and after it the exact undo command is printed. Once the tag is
pushed, an error names the steps left; `finish X.Y.Z` does them, skipping any that
are already done, so it is safe to re-run.

First release: when the version fields already equal X.Y.Z and the CHANGELOG
already has [X.Y.Z], the bump and its commit are skipped and the current commit
is tagged.

--dry-run runs every read-only check (and the tests), reports each problem
instead of stopping at the first, and prints the commands it would run.

Every git, gh, make and claude call goes through Runner, so tests can fake it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_files import (  # noqa: E402  (re-exported for tests and callers)
    CASK, SHA256, ReleaseError, Repo, asset_names, bump_cask, bump_init, bump_json, bump_policy_title, cask_state,
    changelog_section, changelog_sections, is_newer, major_minor, parse_semver, release_changelog, replace_one,
    unreleased_body,
)

ROOT = Path(__file__).resolve().parent.parent
REPO_SLUG = "KartikeyMish/claude-cockpit"
RELEASE_WORKFLOW = "release.yml"
RUN_APPEAR_TIMEOUT_S = 120
SYMREF = re.compile(r"^ref: refs/heads/(\S+)\s+HEAD$", re.M)

__all__ = [
    "CASK", "ReleaseError", "Repo", "Release", "Result", "Runner", "asset_names", "bump_cask", "bump_init",
    "bump_json", "bump_policy_title", "cask_state", "changelog_section", "changelog_sections", "is_newer",
    "major_minor", "parse_semver", "release_changelog", "replace_one", "unreleased_body", "main",
]


# ---------------------------------------------------------------- running commands

@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def first_word(self) -> str:
        words = self.out.split()
        return words[0] if self.ok and words else ""


class Runner:
    """The one place release.py runs git, gh, make and claude.

    mutate=True marks commands that change anything (local or remote); a dry run
    prints those instead of running them. Tests override `execute`.
    """

    def __init__(self, cwd: Path, dry_run: bool = False, echo: Callable[[str], None] = print):
        self.cwd, self.dry_run, self.echo = cwd, dry_run, echo

    def __call__(self, argv: Sequence[str], *, cwd: Path | None = None, mutate: bool = False,
                 stream: bool = False, timeout: float | None = 600) -> Result:
        where = cwd or self.cwd
        if mutate and self.dry_run:
            prefix = "" if where == self.cwd else f"(in {where}) "
            self.echo(f"  [dry-run] would run: {prefix}{shlex.join(argv)}")
            return Result(0)
        return self.execute(list(argv), where, stream, timeout)

    def execute(self, argv: list[str], cwd: Path, stream: bool, timeout: float | None) -> Result:
        try:
            if stream:
                return Result(subprocess.run(argv, cwd=cwd, timeout=timeout, check=False).returncode)
            out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            return Result(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{shlex.join(argv)} timed out")
        return Result(out.returncode, out.stdout, out.stderr)


def detail(result: Result) -> str:
    return (result.err.strip() or result.out.strip() or f"exit {result.code}").splitlines()[-1]


# ---------------------------------------------------------------- the release

@dataclass(frozen=True)
class Plan:
    """What preflight decided: "bump" (a normal release), "first" (tag as is) or "blocked"."""
    mode: str
    sha: str


@dataclass
class Progress:
    """What this run has changed so far, so an abort can say exactly how to undo it."""
    originals: dict = field(default_factory=dict)  # bumped files' contents before the bump
    staged: bool = False
    committed: bool = False
    tagged: bool = False
    pushed: bool = False
    released: bool = False
    stable: bool = False
    tap: bool = False


def prompt(question: str) -> str:
    return input(question)


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


@dataclass
class Release:
    repo: Repo
    version: str
    run: Runner
    tap: Path | None
    assume_yes: bool = False
    today: str = field(default_factory=lambda: date.today().isoformat())
    ask: Callable[[str], str] = prompt
    is_tty: Callable[[], bool] = stdin_is_tty
    echo: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep
    has_claude: bool = field(default_factory=lambda: shutil.which("claude") is not None)
    problems: list = field(default_factory=list)  # a dry run's findings
    progress: Progress = field(default_factory=Progress)
    tap_branch: str = ""

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def dry_run(self) -> bool:
        return self.run.dry_run

    def say(self, text: str) -> None:
        self.echo(f"==> {text}")

    def ok(self, text: str) -> None:
        self.echo(f"  ✓ {text}")

    def done(self, text: str) -> None:
        """Report a step that changed something; a dry run already printed what it would run."""
        if not self.dry_run:
            self.ok(text)

    def problem(self, text: str) -> None:
        """Stop a real release; a dry run records it and carries on."""
        if not self.dry_run:
            raise ReleaseError(text)
        self.problems.append(text)
        self.echo(f"  ✗ {text}")

    def git(self, *args: str, mutate: bool = False) -> Result:
        return self.run(["git", *args], mutate=mutate)

    def tap_git(self, *args: str, mutate: bool = False) -> Result:
        return self.git("-C", str(self.tap), *args, mutate=mutate)

    def must(self, result: Result, what: str) -> Result:
        if not result.ok:
            raise ReleaseError(f"{what} failed: {detail(result)}")
        return result

    def finish_command(self) -> str:
        no_tap = self.tap is None
        return (f"make release-finish VERSION={self.version}{' NO_TAP=1' if no_tap else ''}  "
                f"(or: python3 scripts/release.py finish {self.version}{' --no-tap' if no_tap else ''})")

    # -- 1. preflight

    def preflight(self) -> Plan:
        self.say(f"Preflight for {self.tag}")
        mode = self.check_version()
        sha = self.check_git()
        self.check_tag_is_free()
        self.check_stable(sha or "HEAD")
        self.check_gh()
        self.check_confirmable()
        self.check_tap(allow_ahead=False)
        return Plan(mode=mode, sha=sha)

    def check_version(self) -> str:
        try:
            parse_semver(self.version)
            current = self.repo.current_version()
        except ReleaseError as exc:
            self.problem(str(exc))
            return "blocked"
        for text in self.repo.version_problems(current):
            self.problem(text)
        changelog = self.repo.changelog.read_text(encoding="utf-8")
        if self.version == current:
            return self.check_first_release(changelog)
        if not is_newer(self.version, current):
            self.problem(f"{self.version} is not higher than the current version {current}")
            return "blocked"
        self.ok(f"{current} → {self.version}")
        if unreleased_body(changelog):
            self.ok("CHANGELOG [Unreleased] has entries")
        else:
            self.problem("CHANGELOG [Unreleased] has no entries; add what changed before releasing")
        if changelog_section(changelog, self.version) is not None:
            self.problem(f"CHANGELOG already has a [{self.version}] section")
        return "bump"

    def check_first_release(self, changelog: str) -> str:
        """VERSION is already in every field: tag the current commit if it is fully prepared."""
        if not changelog_section(changelog, self.version):
            self.problem(f"the version fields already say {self.version} but CHANGELOG has no [{self.version}] "
                         "notes; release a higher version")
            return "blocked"
        self.ok(f"versions already say {self.version} and CHANGELOG has [{self.version}]: "
                "tagging the current commit, no bump")
        if unreleased_body(changelog):
            self.problem(f"[Unreleased] has entries that {self.tag} would not include; "
                         "move them under the version or release a higher one")
        return "first"

    def check_git(self) -> str:
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").out.strip()
        if branch == "main":
            self.ok("on main")
        else:
            self.problem(f"on {branch or '?'}, not main")
        if self.git("status", "--porcelain").out.strip():
            self.problem("working tree is not clean (commit or stash first)")
        else:
            self.ok("working tree clean")
        head = self.git("rev-parse", "HEAD").out.strip()
        remote_sha = self.git("ls-remote", "--exit-code", "origin", "refs/heads/main").first_word
        if head and head == remote_sha:
            self.ok(f"HEAD {head[:7]} is origin/main")
        else:
            self.problem(f"HEAD {head[:7] or '?'} is not origin/main ({remote_sha[:7] or 'unreachable'}); "
                         "pull or push first")
        return head

    def check_tag_is_free(self) -> None:
        local = self.git("rev-parse", "-q", "--verify", f"refs/tags/{self.tag}").ok
        remote = self.git("ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{self.tag}").ok
        if local or remote:
            self.problem(f"tag {self.tag} already exists {'locally' if local else 'on origin'}"
                         + ("; if it is pushed, run: " + self.finish_command() if remote else ""))
        else:
            self.ok(f"tag {self.tag} is free")

    def check_stable(self, target: str) -> None:
        """origin's stable may be absent (the first release creates it); otherwise it must fast-forward."""
        remote = self.git("ls-remote", "--exit-code", "origin", "refs/heads/stable")
        if remote.code == 2:
            self.ok("origin has no stable branch yet; this release creates it")
            return
        stable = remote.first_word
        if not stable:
            self.problem(f"could not read origin's stable branch: {detail(remote)}")
            return
        if stable == target:
            self.ok(f"stable is already at {target[:7]}")
            return
        ancestry = self.git("merge-base", "--is-ancestor", stable, target)
        if ancestry.ok:
            self.ok(f"stable {stable[:7]} can fast-forward to {self.tag}")
        elif ancestry.code == 1:
            self.problem(f"origin's stable ({stable[:7]}) is not an ancestor of {target[:7]}, so it can't "
                         "fast-forward; fix stable before releasing")
        else:
            self.problem(f"stable's commit {stable[:7]} isn't in this clone: run `git fetch origin`, then retry")

    def check_gh(self) -> None:
        if self.run(["gh", "auth", "status"]).ok:
            self.ok("gh is logged in")
        else:
            self.problem("gh is not logged in: run `gh auth login`")

    def check_confirmable(self) -> None:
        if self.assume_yes:
            self.ok("--yes: the push won't ask")
        elif self.is_tty():
            self.ok("you'll be asked before anything is pushed")
        else:
            self.problem("stdin is not a terminal, so the push can't be confirmed: run from a terminal, "
                         "or pass --yes (YES=1 with make)")

    def check_tap(self, allow_ahead: bool) -> None:
        """A clean checkout of the tap's default branch, in step with origin. Private repositories are fine."""
        if self.tap is None:
            self.ok("tap update skipped (--no-tap / NO_TAP=1)")
            return
        if not (self.tap / CASK).is_file():
            self.problem(f"no {CASK} in TAP_DIR={self.tap} (set TAP_DIR, or pass NO_TAP=1)")
            return
        top = self.tap_git("rev-parse", "--show-toplevel")
        if not top.ok or os.path.realpath(top.out.strip()) != os.path.realpath(self.tap):
            self.problem(f"{self.tap} is not its own git checkout (clone the tap repository there)")
            return
        if self.tap_git("status", "--porcelain").out.strip():
            self.problem(f"{self.tap} has uncommitted changes")
            return
        symref = SYMREF.search(self.tap_git("ls-remote", "--symref", "origin", "HEAD").out)
        if not symref:
            self.problem(f"can't read the default branch of {self.tap}'s origin (is it pushed, and can you reach it?)")
            return
        self.tap_branch = symref.group(1)
        current = self.tap_git("rev-parse", "--abbrev-ref", "HEAD").out.strip()
        if current != self.tap_branch:
            self.problem(f"{self.tap} is on {current or '?'}, not its default branch {self.tap_branch}")
            return
        self.check_tap_sync(allow_ahead)

    def check_tap_sync(self, allow_ahead: bool) -> None:
        head = self.tap_git("rev-parse", "HEAD").out.strip()
        remote = self.tap_git("ls-remote", "--exit-code", "origin", f"refs/heads/{self.tap_branch}").first_word
        where = f"origin/{self.tap_branch}"
        if head and head == remote:
            self.ok(f"tap {self.tap} is clean and matches {where}")
        elif allow_ahead and remote and self.tap_git("merge-base", "--is-ancestor", remote, head).ok:
            self.ok(f"tap {self.tap} has unpushed commits on top of {where}; they will be pushed")
        else:
            self.problem(f"tap HEAD {head[:7] or '?'} is not {where} ({remote[:7] or 'unreachable'}); "
                         "pull or push the tap first")

    # -- 2-4. bump, verify, commit and tag

    def bump(self, plan: Plan) -> None:
        if plan.mode != "bump":
            return
        self.say("Bump")
        changes = self.repo.bumped_files(self.version, self.today)
        originals = {path: path.read_text(encoding="utf-8") for path in changes}
        for path, text in changes.items():
            rel = path.relative_to(self.repo.root)
            if self.dry_run:
                self.echo(f"  [dry-run] would update {rel}")
                continue
            self.progress.originals = {**self.progress.originals, path: originals[path]}
            path.write_text(text, encoding="utf-8")
            self.ok(f"updated {rel}")
        if self.dry_run:
            self.progress.originals = originals  # a dry run "bumps" for the commit step's printout only

    def verify(self) -> None:
        self.say("Verify")
        checks = [["make", "test"]]
        if self.has_claude:
            checks += [["claude", "plugin", "validate", "plugins/orchestrator"], ["claude", "plugin", "validate", "."]]
        else:
            self.ok("claude not on PATH: skipping `claude plugin validate`")
        for argv in checks:
            result = self.run(argv, timeout=900)
            if result.ok:
                self.ok(f"{shlex.join(argv)}: {summary(result)}")
                continue
            self.echo(tail(result.out + result.err, 40))
            self.problem(f"{shlex.join(argv)} failed")

    def rel_paths(self) -> list[str]:
        return [str(p.relative_to(self.repo.root)) for p in self.progress.originals]

    def commit_and_tag(self, plan: Plan) -> str:
        """Returns the commit the tag points at, after checking that it really does."""
        self.say("Commit and tag")
        sha = plan.sha
        if self.progress.originals:
            sha = self.commit(plan.sha)
        self.must(self.git("tag", "-a", self.tag, "-m", f"claude-cockpit {self.tag}", mutate=True), "git tag")
        if self.dry_run:
            return sha
        self.progress.tagged = True
        tagged = self.git("rev-parse", "--verify", "-q", f"refs/tags/{self.tag}^{{commit}}").out.strip()
        if tagged != sha:
            raise ReleaseError(f"{self.tag} points at {tagged[:7] or 'nothing'}, not the release commit {sha[:7]}")
        self.ok(f"{self.tag} → {sha[:7]}" + ("" if self.progress.committed else " (no bump commit)"))
        return sha

    def commit(self, before: str) -> str:
        self.progress.staged = not self.dry_run
        self.must(self.git("add", "--", *self.rel_paths(), mutate=True), "git add")
        self.must(self.git("commit", "-m", f"chore: release {self.tag}", mutate=True), "git commit")
        if self.dry_run:
            return before
        after = self.git("rev-parse", "HEAD").out.strip()
        if not after or after == before:
            raise ReleaseError(f"git commit did not create a commit (HEAD is still {before[:7]})")
        self.progress.committed = True
        return after

    # -- 5. publish

    def confirm_and_push(self) -> bool:
        self.say("Publish")
        refs = (["refs/heads/main"] if self.progress.originals else []) + [f"refs/tags/{self.tag}"]
        names = " and ".join(ref.split("/", 2)[2] for ref in refs)
        question = f"Push {names} to origin? This publishes the release. [y/N] "
        if self.dry_run:
            self.echo(f"  [dry-run] would ask: {question.strip()}")
        elif not self.assume_yes and self.ask(question).strip().lower() not in {"y", "yes"}:
            self.echo("  Nothing was pushed.")
            self.print_undo()
            return False
        pushed = self.git("push", "--atomic", "origin", *refs, mutate=True)
        if not pushed.ok:
            raise ReleaseError(f"push failed, nothing was published: {detail(pushed)}")
        self.progress.pushed = not self.dry_run
        return True

    # -- recovery

    def undo_command(self) -> str:
        steps = ([f"git tag -d {self.tag}"] if self.progress.tagged else []) + \
                (["git reset --hard HEAD~1"] if self.progress.committed else [])
        return " && ".join(steps)

    def print_undo(self) -> None:
        undo = self.undo_command()
        if undo:
            self.echo(f"  To undo the local release: {undo}")

    def restore_bump(self) -> None:
        """Put the bumped files back (and unstage them) when the run stops before the commit."""
        if self.dry_run or self.progress.committed or not self.progress.originals:
            return
        for path, text in self.progress.originals.items():
            path.write_text(text, encoding="utf-8")
        if self.progress.staged:
            self.git("reset", "-q", "--", *self.rel_paths(), mutate=True)
        self.echo("  restored the version bump; the tree is as it was")

    def remaining_steps(self) -> list[str]:
        steps = []
        if not self.progress.released:
            steps.append(f"wait for the {RELEASE_WORKFLOW} run and check the Release")
        if not self.progress.stable:
            steps.append(f"fast-forward stable to {self.tag}")
        if self.tap is not None and not self.progress.tap:
            steps.append("update the Homebrew cask")
        return steps

    def explain_remaining(self) -> None:
        steps = self.remaining_steps()
        if steps:
            self.echo(f"  {self.tag} is pushed. Still to do: " + "; ".join(steps) + ".")
            self.echo(f"  When the cause is fixed, run: {self.finish_command()}")

    # -- 6. wait for the Release workflow

    def find_run(self, sha: str) -> str:
        waited = 0
        while waited < RUN_APPEAR_TIMEOUT_S:
            listing = self.run(["gh", "run", "list", "--workflow", RELEASE_WORKFLOW, "--commit", sha,
                                "--event", "push", "--json", "databaseId,headBranch", "--limit", "10"])
            runs = json.loads(listing.out or "[]") if listing.ok else []
            ours = [run for run in runs if run.get("headBranch") == self.tag] or runs
            if ours:
                return str(ours[0]["databaseId"])
            self.sleep(5)
            waited += 5
        raise ReleaseError(f"no {RELEASE_WORKFLOW} run appeared for {self.tag}; check the Actions tab")

    def release_status(self) -> tuple[str, str]:
        """("", url) when the Release is complete, else (what is missing, "")."""
        view = self.run(["gh", "release", "view", self.tag, "--json", "url,assets,isDraft"])
        try:
            data = json.loads(view.out) if view.ok and view.out.strip() else {}
        except ValueError:
            data = {}
        if not data:
            return "the release", ""
        names = {asset.get("name") for asset in data.get("assets", [])}
        missing = [name for name in asset_names(self.version) if name not in names]
        if data.get("isDraft"):
            missing.append("publication (it is a draft)")
        return ", ".join(missing), ("" if missing else str(data.get("url")))

    def await_release(self, sha: str) -> None:
        self.say("Release workflow")
        if self.dry_run:
            self.echo(f"  [dry-run] would wait for the {RELEASE_WORKFLOW} run of {self.tag} (gh run watch), "
                      "unless the Release is already complete")
            self.echo(f"  [dry-run] would check the Release has {', '.join(asset_names(self.version))}")
            return
        missing, url = self.release_status()
        if not missing:
            self.ok(f"Release already published: {url}")
            self.progress.released = True
            return
        run_id = self.find_run(sha)
        if not self.run(["gh", "run", "watch", run_id, "--exit-status"], stream=True, timeout=3600).ok:
            raise ReleaseError(f"the release workflow failed (gh run view {run_id} --log-failed); "
                               "stable did not move, so plugin users are unaffected")
        missing, url = self.release_status()
        if missing:
            raise ReleaseError(f"Release {self.tag} is incomplete (missing: {missing})")
        self.ok(f"Release published: {url}")
        self.progress.released = True

    # -- 7. stable

    def move_stable(self, sha: str) -> None:
        self.say("stable channel")
        if self.git("ls-remote", "--exit-code", "origin", "refs/heads/stable").first_word == sha:
            self.ok(f"stable is already at {self.tag}")
            self.progress.stable = True
            return
        pushed = self.git("push", "origin", f"{sha or 'HEAD'}:refs/heads/stable", mutate=True)
        if not pushed.ok:
            raise ReleaseError(f"could not fast-forward stable to {self.tag}: {detail(pushed)}")
        self.done(f"stable → {self.tag}: plugin installs from {REPO_SLUG}#stable now get {self.version}")
        self.progress.stable = not self.dry_run

    # -- 8. Homebrew tap

    def published_sha256(self) -> str:
        tarball, checksum = asset_names(self.version)
        with tempfile.TemporaryDirectory() as tmp:
            got = self.run(["gh", "release", "download", self.tag, "--pattern", tarball,
                            "--pattern", checksum, "--dir", tmp])
            if not got.ok:
                raise ReleaseError(f"could not download the {self.tag} assets: {detail(got)}")
            listed = SHA256.search((Path(tmp) / checksum).read_text(encoding="utf-8"))
            actual = hashlib.sha256((Path(tmp) / tarball).read_bytes()).hexdigest()
        if not listed or listed.group(0) != actual:
            raise ReleaseError(f"{checksum} does not match {tarball}; not updating the tap")
        return actual

    def update_tap(self) -> None:
        if self.tap is None:
            return
        branch = self.tap_branch or "<default branch>"
        self.say(f"Homebrew tap ({self.tap}, {branch})")
        if self.dry_run:
            self.echo(f"  [dry-run] would set version {self.version} and the published sha256 in {CASK}")
            self.tap_git("add", str(CASK), mutate=True)
            self.tap_git("commit", "-m", f"claude-cockpit {self.version}", mutate=True)
            self.tap_git("push", "origin", f"HEAD:refs/heads/{branch}", mutate=True)
            return
        sha = self.published_sha256()
        cask = self.tap / CASK
        text = cask.read_text(encoding="utf-8")
        if cask_state(text) == (self.version, sha):
            self.ok(f"cask already says {self.version} ({sha[:12]}…)")
        else:
            cask.write_text(bump_cask(text, self.version, sha), encoding="utf-8")
            self.must(self.tap_git("add", str(CASK), mutate=True), f"git add in {self.tap}")
            self.must(self.tap_git("commit", "-m", f"claude-cockpit {self.version}", mutate=True),
                      f"git commit in {self.tap}")
            self.ok(f"cask set to {self.version} ({sha[:12]}…)")
        self.push_tap()
        self.progress.tap = True

    def push_tap(self) -> None:
        head = self.tap_git("rev-parse", "HEAD").out.strip()
        remote = self.tap_git("ls-remote", "--exit-code", "origin", f"refs/heads/{self.tap_branch}").first_word
        if head and head == remote:
            self.ok(f"tap origin/{self.tap_branch} is up to date")
            return
        self.must(self.tap_git("push", "origin", f"HEAD:refs/heads/{self.tap_branch}", mutate=True),
                  f"git push in {self.tap}")
        self.ok(f"pushed the tap to origin/{self.tap_branch}")

    # -- the whole release, and finishing one

    def cut(self) -> int:
        plan = self.preflight()
        if plan.mode == "blocked":
            return self.finish_dry_run()
        try:
            self.bump(plan)
            self.verify()
            sha = self.commit_and_tag(plan)
            if not self.confirm_and_push():
                return 1
        except BaseException:  # KeyboardInterrupt included: never leave a half-made release behind
            self.restore_bump()
            if not self.dry_run:
                self.print_undo()
            raise
        return self.publish(sha)

    def publish(self, sha: str) -> int:
        try:
            self.await_release(sha)
            self.move_stable(sha)
            self.update_tap()
        except (ReleaseError, KeyboardInterrupt):
            self.explain_remaining()
            raise
        if self.dry_run:
            return self.finish_dry_run()
        self.say(f"Released {self.tag}")
        return 0

    def finish(self) -> int:
        self.say(f"Finish {self.tag}")
        sha = self.check_pushed_tag()
        if sha:
            self.check_stable(sha)
        self.check_gh()
        self.check_tap(allow_ahead=True)
        if not sha:
            return self.finish_dry_run()
        self.progress.pushed = True
        return self.publish(sha)

    def check_pushed_tag(self) -> str:
        """The commit the pushed tag points at (peeled), or "" when the tag isn't on origin."""
        try:
            parse_semver(self.version)
        except ReleaseError as exc:
            self.problem(str(exc))
            return ""
        ref = f"refs/tags/{self.tag}"
        listing = self.git("ls-remote", "origin", ref, f"{ref}^{{}}")
        pairs = [line.split("\t", 1) for line in listing.out.splitlines() if "\t" in line]
        refs = {name.strip(): object_id.strip() for object_id, name in pairs}
        sha = refs.get(f"{ref}^{{}}") or refs.get(ref, "")  # an annotated tag peels to its commit
        if sha:
            self.ok(f"{self.tag} is on origin at {sha[:7]}")
        else:
            self.problem(f"{self.tag} is not on origin; cut the release first: make release VERSION={self.version}")
        return sha

    def finish_dry_run(self) -> int:
        if not self.dry_run:
            return 1
        self.say(f"Dry run: {len(self.problems)} problem(s) would stop a real run")
        for text in self.problems:
            self.echo(f"  ✗ {text}")
        return 1 if self.problems else 0


def summary(result: Result) -> str:
    lines = [line for line in (result.err + result.out).splitlines() if line.strip()]
    ran = [line for line in lines if line.startswith("Ran ")]
    status = [line for line in lines if line.startswith(("OK", "FAILED"))]
    if ran:
        return " ".join([ran[-1], *status[-1:]])
    passed = [line for line in lines if "passed" in line.lower()]
    return passed[-1].strip("✔ ").strip() if passed else "ok"


def tail(text: str, count: int) -> str:
    return "\n".join(text.splitlines()[-count:])


# ---------------------------------------------------------------- commands

def cmd_notes(repo: Repo, version: str) -> int:
    notes = changelog_section(repo.changelog.read_text(encoding="utf-8"), version)
    if not notes:
        print(f"CHANGELOG has no notes for [{version}]", file=sys.stderr)
        return 1
    print(notes)
    return 0


def cmd_verify_tag(repo: Repo, tag: str) -> int:
    version = tag[1:] if tag.startswith("v") else ""
    try:
        parse_semver(version)
    except ReleaseError:
        print(f"{tag!r} is not a release tag like v1.2.3", file=sys.stderr)
        return 1
    problems = repo.version_problems(version)
    if not changelog_section(repo.changelog.read_text(encoding="utf-8"), version):
        problems.append(f"CHANGELOG.md has no [{version}] section")
    for text in problems:
        print(f"✗ {text}", file=sys.stderr)
    if not problems:
        print(f"✓ {tag} matches every version field and has CHANGELOG notes")
    return 1 if problems else 0


def make_release(repo: Repo, args: argparse.Namespace) -> Release:
    tap_dir = None if args.no_tap else Path(os.path.expanduser(os.environ.get("TAP_DIR") or "~/homebrew-tap"))
    runner = Runner(repo.root, dry_run=args.dry_run)
    return Release(repo=repo, version=args.version, run=runner, tap=tap_dir, assume_yes=getattr(args, "yes", False))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="release.py", description=__doc__.split("\n\n")[0])
    sub = root.add_subparsers(dest="command", required=True)
    cut = sub.add_parser("cut", help="cut a release (make release)")
    cut.add_argument("version", help="X.Y.Z")
    cut.add_argument("--yes", action="store_true", help="push without asking")
    finish = sub.add_parser("finish", help="finish a pushed release: Release, stable, tap (make release-finish)")
    finish.add_argument("version", help="X.Y.Z")
    for command in (cut, finish):
        command.add_argument("--dry-run", action="store_true", help="check and print, change nothing")
        command.add_argument("--no-tap", action="store_true", help="don't update the Homebrew tap")
    notes = sub.add_parser("notes", help="print a version's CHANGELOG section")
    notes.add_argument("version")
    verify = sub.add_parser("verify-tag", help="check a tag against the version fields and CHANGELOG")
    verify.add_argument("tag")
    return root


def main(argv: Sequence[str] | None = None, repo: Repo = Repo(ROOT)) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "notes":
            return cmd_notes(repo, args.version)
        if args.command == "verify-tag":
            return cmd_verify_tag(repo, args.tag)
        release = make_release(repo, args)
        return release.cut() if args.command == "cut" else release.finish()
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("release: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
