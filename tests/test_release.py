"""Release tooling: version fields, CHANGELOG format, and scripts/release.py.

The release flow itself (fake Runner) is tested in test_release_flow.py.
"""

import json
import os
import re
import shutil
import subprocess
import sys
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


class VersionConsistencyTests(unittest.TestCase):
    """The shipped tree: one version everywhere, and notes for it."""

    def test_every_version_field_agrees(self):
        repo = Repo(ROOT)
        version = repo.current_version()
        self.assertEqual(repo.version_problems(version), [])
        sys.path.insert(0, str(ROOT / "lib"))
        from cockpit import VERSION
        self.assertEqual(VERSION, version)

    def test_changelog_has_notes_for_the_current_version(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(release.changelog_section(text, Repo(ROOT).current_version()))

    def test_changelog_is_in_release_format_newest_first(self):
        sections = release.changelog_sections((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertEqual(sections[0][0], "Unreleased")
        versions = [release.parse_semver(name) for name, _, _ in sections[1:]]
        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(len(versions), len(set(versions)))
        for name, day, _ in sections[1:]:
            with self.subTest(version=name):
                self.assertRegex(day or "", r"^\d{4}-\d{2}-\d{2}$")


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
