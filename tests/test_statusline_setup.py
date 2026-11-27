"""`ccx setup` status line step: install, keep, force, refresh, on a temporary HOME.

Runs use --no-plugin --no-ghostty so only Claude's user settings.json is touched;
the Sandbox (test_setup.py) never lets a run reach the real ~/.claude.
"""

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from cockpit import installer  # noqa: E402
from cockpit import statusline_setup as sls  # noqa: E402
from cockpit.core import CockpitError  # noqa: E402
from test_setup import Sandbox, backups  # noqa: E402

OURS = f"python3 -I -S {shlex.quote(str(ROOT / 'bin' / 'ccx'))} statusline"
FOREIGN = {"type": "command", "command": "~/.claude/statusline-usage-alert.sh", "refreshInterval": 60}


class SetupStatuslineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self.tmp.name, with_claude=False)

    def tearDown(self):
        self.tmp.cleanup()

    def setup_run(self, *args):
        out = self.box.ccx("setup", "--no-plugin", "--no-ghostty", *args)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def settings(self):
        return json.loads(self.box.settings_path.read_text())

    def saved(self):
        return backups(self.box.settings_path.parent, "settings.json")

    def test_fresh_home_gets_the_cockpit_status_line(self):
        out = self.setup_run()
        self.assertEqual(self.settings()["statusLine"],
                         {"type": "command", "command": OURS, "refreshInterval": sls.REFRESH_INTERVAL_S})
        self.assertIn("status line installed", out.stdout)
        self.assertEqual(self.saved(), [], "nothing to back up")

    def test_unset_status_line_installed_other_keys_kept_and_rerun_is_idempotent(self):
        self.box.write_settings({"theme": "dark", "env": {"A": "1"}, "permissions": {"allow": ["Bash(ls)"]}})
        self.setup_run()
        settings = self.settings()
        self.assertEqual((settings["theme"], settings["env"], settings["permissions"]),
                         ("dark", {"A": "1"}, {"allow": ["Bash(ls)"]}))
        self.assertEqual(len(self.saved()), 1)
        before = self.box.settings_path.read_text()
        out = self.setup_run()
        self.assertIn("status line up to date", out.stdout)
        self.assertEqual(self.box.settings_path.read_text(), before)
        self.assertEqual(len(self.saved()), 1, "no rewrite, no new backup")

    def test_someone_elses_status_line_is_kept_with_a_hint(self):
        self.box.write_settings({"statusLine": FOREIGN})
        before = self.box.settings_path.read_text()
        out = self.setup_run()
        self.assertEqual(self.box.settings_path.read_text(), before)
        self.assertEqual(self.saved(), [])
        self.assertIn("kept your status line", out.stdout)
        self.assertIn("ccx setup --statusline", out.stdout)

    def test_force_replaces_it_after_a_backup(self):
        self.box.write_settings({"statusLine": FOREIGN, "model": "opus"})
        out = self.setup_run("--statusline")
        settings = self.settings()
        self.assertEqual(settings["statusLine"]["command"], OURS)
        self.assertEqual(settings["model"], "opus")
        (saved,) = self.saved()
        self.assertEqual(json.loads(saved.read_text())["statusLine"], FOREIGN)
        self.assertIn("status line replaced", out.stdout)

    def test_owned_entry_with_an_old_path_is_refreshed_keeping_display_keys(self):
        old = {"type": "command", "command": "/opt/homebrew/Caskroom/claude-cockpit/1.6.0/bin/ccx statusline",
               "refreshInterval": 60, "padding": 1}
        self.box.write_settings({"statusLine": old})
        out = self.setup_run()
        self.assertEqual(self.settings()["statusLine"],
                         {"type": "command", "command": OURS, "refreshInterval": sls.REFRESH_INTERVAL_S, "padding": 1})
        self.assertIn("status line updated", out.stdout)
        self.assertEqual(len(self.saved()), 1)

    def test_no_statusline_flag_and_contradictory_flags(self):
        self.box.write_settings({"theme": "dark"})
        self.setup_run("--no-statusline")
        self.assertNotIn("statusLine", self.settings())
        out = self.box.ccx("setup", "--no-plugin", "--no-ghostty", "--statusline", "--no-statusline")
        self.assertEqual(out.returncode, 2)
        self.assertIn("contradict", out.stderr)

    def test_unreadable_settings_are_left_alone_and_setup_still_succeeds(self):
        self.box.settings_path.parent.mkdir(parents=True)
        self.box.settings_path.write_text("{ broken")
        out = self.setup_run()
        self.assertEqual(self.box.settings_path.read_text(), "{ broken")
        self.assertIn("status line not changed", out.stdout)

    def test_symlinked_settings_keep_their_link(self):
        real = self.box.home / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"theme": "light"}))
        os.chmod(real, 0o640)
        self.box.settings_path.parent.mkdir(parents=True)
        self.box.settings_path.symlink_to(real)
        self.setup_run()
        self.assertTrue(self.box.settings_path.is_symlink())
        self.assertEqual(json.loads(real.read_text())["statusLine"]["command"], OURS)
        self.assertEqual(real.stat().st_mode & 0o777, 0o640)
        self.assertEqual(sorted(p.name for p in real.parent.iterdir()), ["claude-settings.json"],
                         "no temp file left behind")


class CombinedSetupTests(unittest.TestCase):
    """The default flow: marketplace re-pointed and status line installed in one run."""

    def test_both_rewrites_land_and_each_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Sandbox(tmp)
            clone = "/Users/someone/claude-cockpit"
            box.write_settings({"theme": "dark", "extraKnownMarketplaces": {
                "claude-cockpit": {"source": {"source": "directory", "path": clone}}}})
            box.write_state(markets={"claude-cockpit": {"source": "directory", "path": clone}})
            out = box.ccx("setup", "--no-ghostty")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            settings = json.loads(box.settings_path.read_text())
            self.assertEqual(settings["extraKnownMarketplaces"]["claude-cockpit"]["source"]["repo"],
                             "KartikeyMish/claude-cockpit")
            self.assertTrue(sls.is_owned(settings["statusLine"]))
            self.assertEqual(settings["theme"], "dark")
            saved = backups(box.settings_path.parent, "settings.json")
            self.assertEqual(len(saved), 2)
            self.assertIn(clone, saved[0].read_text(), "the first backup is the untouched original")


class CommandPathTests(unittest.TestCase):
    def test_a_stable_link_on_path_wins_over_the_resolved_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "ccx"
            link.symlink_to(ROOT / "bin" / "ccx")
            self.assertEqual(sls.ccx_path(ROOT, {"PATH": tmp}), str(link))
            other = Path(tmp) / "other"
            other.mkdir()
            (other / "ccx").write_text("#!/bin/sh\n")
            (other / "ccx").chmod(0o755)
            self.assertNotEqual(sls.ccx_path(ROOT, {"PATH": str(other)}), str(other / "ccx"),
                                "a different ccx on PATH is not ours")

    def test_ownership_is_recognised_by_pattern(self):
        for command in ("ccx statusline", "/opt/homebrew/bin/ccx statusline", "'/a b/ccx' statusline",
                        "python3 -I -S /opt/homebrew/bin/ccx statusline"):
            self.assertTrue(sls.is_owned({"command": command}), command)
        for command in ("~/.claude/statusline.sh", "ccx statusline | tee x", "myccx statusline"):
            self.assertFalse(sls.is_owned({"command": command}), command)
        self.assertFalse(sls.is_owned("ccx statusline"))

    def test_parse_args(self):
        self.assertTrue(installer.parse_args(["--statusline"]).force_statusline)
        self.assertFalse(installer.parse_args(["--no-statusline"]).statusline)
        with self.assertRaises(CockpitError):
            installer.parse_args(["--statusline", "--no-statusline"])


if __name__ == "__main__":
    unittest.main()
