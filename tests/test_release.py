"""Release tooling: version fields, CHANGELOG format, and scripts/release.py.

The release flow itself (fake Runner) is tested in test_release_flow.py.
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
sys.path.insert(0, str(ROOT / "scripts"))

import release  # noqa: E402
from release import ReleaseError, Repo  # noqa: E402

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


def release_consistency_problems(root):
    """What would make `verify-tag` or the version tests fail for ROOT's current version."""
    repo = Repo(root)
    version = repo.current_version()
    problems = list(repo.version_problems(version))
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not release.changelog_section(text, version):
        problems.append(f"CHANGELOG has no [{version}] notes")
    sections = release.changelog_sections(text)
    if not sections or sections[0][0] != "Unreleased":
        problems.append("CHANGELOG does not open with [Unreleased]")
    versions = [release.parse_semver(name) for name, _, _ in sections[1:]]
    if versions != sorted(versions, reverse=True) or len(versions) != len(set(versions)):
        problems.append(f"CHANGELOG versions are not unique and newest first: {versions}")
    problems += [f"[{name}] has no YYYY-MM-DD date" for name, day, _ in sections[1:]
                 if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or "")]
    return version, problems


class VersionConsistencyTests(unittest.TestCase):
    """The shipped tree: one version everywhere, and notes for it."""

    def test_every_version_field_agrees(self):
        version, problems = release_consistency_problems(ROOT)
        self.assertEqual(problems, [])
        sys.path.insert(0, str(ROOT / "lib"))
        from cockpit import VERSION
        self.assertEqual(VERSION, version)

    def test_changelog_is_in_release_format_newest_first(self):
        self.assertEqual(release.changelog_sections((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))[0][0],
                         "Unreleased")


# Suites that read version state; they must also pass on a tree `make release` has just bumped.
VERSION_SENSITIVE = ["test_release_flow", "test_release", "test_plugin"]
NESTED = "COCKPIT_BUMPED_TREE_TEST"


@unittest.skipIf(os.environ.get(NESTED), "already running inside a bumped copy")
class BumpedTreeTests(unittest.TestCase):
    """Regression for the 1.6.0 release: `make release` bumps the real files, then runs
    `make test`. A copy of this repo, bumped the same way, must stay consistent and keep
    the version-sensitive suites green, for a patch, a minor and a major release."""

    def bumped_copy(self, tmp, target):
        copy = Path(tmp) / "repo"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", "results", "*.pyc"))
        changelog = copy / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        if not release.unreleased_body(text):  # right after a release [Unreleased] is empty
            text = text.replace("## [Unreleased]\n", "## [Unreleased]\n- Regression fixture entry.\n", 1)
            changelog.write_text(text, encoding="utf-8")
        for path, new_text in Repo(copy).bumped_files(target, "2099-01-01").items():
            path.write_text(new_text, encoding="utf-8")
        return copy

    def test_a_bumped_copy_stays_consistent_and_its_release_tests_pass(self):
        major, minor, patch = release.parse_semver(Repo(ROOT).current_version())
        for target in (f"{major}.{minor}.{patch + 1}", f"{major}.{minor + 1}.0", f"{major + 1}.0.0"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                copy = self.bumped_copy(tmp, target)
                version, problems = release_consistency_problems(copy)
                self.assertEqual((version, problems), (target, []))
                self.assertEqual(release.unreleased_body((copy / "CHANGELOG.md").read_text(encoding="utf-8")), "")
                out = subprocess.run([sys.executable, "-m", "unittest", *VERSION_SENSITIVE], cwd=copy / "tests",
                                     env={**os.environ, NESTED: "1"}, capture_output=True, text=True,
                                     timeout=300, check=False)
                self.assertEqual(out.returncode, 0, f"on a tree bumped to {target}:\n{out.stderr[-3000:]}")


class PureFunctionTests(unittest.TestCase):
    def test_semver_parsing_and_comparison(self):
        self.assertEqual(release.parse_semver("1.10.0"), (1, 10, 0))
        for bad in ("1.2", "v1.2.3", "01.2.3", "1.2.3-rc1", "1.2.3.4", ""):
            with self.subTest(version=bad), self.assertRaises(ReleaseError):
                release.parse_semver(bad)
        self.assertTrue(release.is_newer("1.10.0", "1.9.9"))
        self.assertTrue(release.is_newer("2.0.0", "1.99.99"))
        self.assertFalse(release.is_newer("1.5.0", "1.5.0"))
        self.assertFalse(release.is_newer("1.4.9", "1.5.0"))

    def test_release_changelog_dates_unreleased_and_opens_a_fresh_one(self):
        out = release.release_changelog(CHANGELOG, "1.6.0", "2026-12-01")
        self.assertIn("## [Unreleased]\n\n## [1.6.0] - 2026-12-01\n- **New thing.** It works.", out)
        self.assertEqual(release.unreleased_body(out), "")
        self.assertEqual(release.changelog_section(out, "1.5.0"), "First public release.")
        self.assertEqual(out.replace("## [Unreleased]\n\n## [1.6.0] - 2026-12-01\n", "## [Unreleased]\n"), CHANGELOG)

    def test_release_changelog_refuses_empty_or_duplicate(self):
        empty = CHANGELOG.replace("- **New thing.** It works.\n\n### Details\n- more\n", "")
        with self.assertRaisesRegex(ReleaseError, "no entries"):
            release.release_changelog(empty, "1.6.0", "2026-12-01")
        with self.assertRaisesRegex(ReleaseError, "already has"):
            release.release_changelog(CHANGELOG, "1.5.0", "2026-12-01")

    def test_notes_include_subsections_and_stop_at_the_next_version(self):
        self.assertEqual(release.changelog_section(CHANGELOG, "Unreleased"),
                         "- **New thing.** It works.\n\n### Details\n- more")
        self.assertEqual(release.changelog_section(CHANGELOG, "1.3.0"), "- Older.")
        self.assertIsNone(release.changelog_section(CHANGELOG, "1.4.0"))

    def test_old_heading_styles_are_rejected(self):
        with self.assertRaisesRegex(ReleaseError, "not in release format"):
            release.changelog_sections("## Plugin 1.5.0 / policy v1.5 — 2026-11-08\n- x\n")
        with self.assertRaisesRegex(ReleaseError, "not in release format"):
            release.changelog_sections("## 1.2.0 — 2026-11-07\n")

    def test_json_bump_changes_only_the_version(self):
        text = '{\n  "name": "x",\n  "owner": { "name": "K" },\n  "version": "1.5.0"\n}\n'
        self.assertEqual(release.bump_json(text, "1.6.0", "f"), text.replace("1.5.0", "1.6.0"))
        with self.assertRaisesRegex(ReleaseError, "exactly one"):
            release.bump_json('{"version": "1", "a": {"version": "2"}}', "3.0.0", "f")

    def test_init_and_policy_bumps(self):
        self.assertEqual(release.bump_init('"""x"""\n\nVERSION = "1.5.0"\n', "1.6.0"), '"""x"""\n\nVERSION = "1.6.0"\n')
        title = "# Orchestrator routing (claude-cockpit policy v1.5)\n\nbody v1.5\n"
        self.assertEqual(release.bump_policy_title(title, "2.0.1"),
                         "# Orchestrator routing (claude-cockpit policy v2.0)\n\nbody v1.5\n")

    def test_cask_bump_replaces_the_placeholder_once(self):
        cask = 'cask "c" do\n  version "1.5.0"\n  # the sha256 below\n  sha256 "' + "0" * 64 + '"\nend\n'
        out = release.bump_cask(cask, "1.6.0", "ab" * 32)
        self.assertEqual(out, cask.replace("1.5.0", "1.6.0").replace("0" * 64, "ab" * 32))
        self.assertEqual(release.cask_state(out), ("1.6.0", "ab" * 32))
        self.assertEqual(release.cask_state(cask), ("1.5.0", "0" * 64))
        with self.assertRaisesRegex(ReleaseError, "not a sha256"):
            release.bump_cask(cask, "1.6.0", "xyz")
        sha_line = '  sha256 "' + "0" * 64 + '"\n'
        with self.assertRaisesRegex(ReleaseError, "exactly one cask sha256"):
            release.bump_cask(cask.replace(sha_line, sha_line * 2), "1.6.0", "ab" * 32)


class CommandLineTests(unittest.TestCase):
    def test_notes_prints_the_section(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "release.py"), "notes", "1.2.0"],
                             capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("Token and safety hardening"))
        self.assertNotIn("## [", out.stdout)

    @unittest.skipUnless(shutil.which("make"), "make not installed")
    def test_make_targets_pass_the_flags(self):
        # `make release DRY_RUN=1` runs these tests through make, which exports its variables.
        env = {k: v for k, v in os.environ.items()
               if k not in {"DRY_RUN", "VERSION", "NO_TAP", "YES", "MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}

        def plan(*args):
            return subprocess.run(["make", "-n", *args], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=30, check=False).stdout

        self.assertRegex(plan("release", "VERSION=1.6.0", "DRY_RUN=1"), r"scripts/release\.py cut 1\.6\.0 --dry-run")
        for flags in ((), ("DRY_RUN=0",)):
            out = plan("release", "VERSION=1.6.0", *flags)
            self.assertNotIn("--dry-run", out)
            self.assertNotIn("--no-tap", out)
            self.assertNotIn("--yes", out)
        self.assertRegex(plan("release", "VERSION=1.6.0", "NO_TAP=1", "YES=1"), r"cut 1\.6\.0 +--no-tap --yes")
        self.assertRegex(plan("release-finish", "VERSION=1.6.0", "DRY_RUN=1", "NO_TAP=1"),
                         r"scripts/release\.py finish 1\.6\.0 --dry-run --no-tap")
        self.assertNotIn("--yes", plan("release-finish", "VERSION=1.6.0", "YES=1"))


class DocsTests(unittest.TestCase):
    def test_readme_names_the_real_channel_and_cask(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in ("KartikeyMish/claude-cockpit#stable", "brew install --cask kartikeymish/tap/claude-cockpit",
                       "ccx setup", "make release VERSION="):
            self.assertTrue(phrase in readme, f"README should mention {phrase}")
        self.assertIsNone(re.search(r"\bpilot\b|colleague", readme, re.I))

    def test_license_is_mit_everywhere(self):
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text())
        self.assertIn("Kartikey Mishra", (ROOT / "LICENSE").read_text())
        manifest = json.loads((ROOT / "plugins/orchestrator/.claude-plugin/plugin.json").read_text())
        self.assertEqual(manifest["license"], "MIT")


if __name__ == "__main__":
    unittest.main()
