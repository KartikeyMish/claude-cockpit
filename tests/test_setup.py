"""`ccx setup` and install.sh against a fake `claude` CLI and a temporary HOME.

Nothing here touches the real ~/.claude or ~/.config: every run gets its own HOME,
no XDG_CONFIG_HOME/CLAUDE_CONFIG_DIR, and a PATH whose `claude` is tests/fake_claude.py.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from cockpit import installer  # noqa: E402
from cockpit.core import CockpitError  # noqa: E402

CCX = ROOT / "bin" / "ccx"
PROFILE = ROOT / "terminal" / "ghostty" / "cockpit.conf"
FAKE = ROOT / "tests" / "fake_claude.py"
STABLE = "KartikeyMish/claude-cockpit#stable"


class Sandbox:
    """A temporary HOME with a fake `claude` (and optionally a fake `git`) first on PATH."""

    def __init__(self, tmp, with_claude=True):
        self.home = Path(tmp) / "home"
        self.bin = Path(tmp) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.script("python3", f'exec "{sys.executable}" "$@"')  # install.sh runs the interpreter under test
        if with_claude:
            self.script("claude", f'exec "{sys.executable}" "{FAKE}" "$@"')
        self.env = {k: v for k, v in os.environ.items()
                    if k not in {"XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "NO_COLOR"}
                    and not k.startswith(("COCKPIT_", "FAKE_"))}
        self.env.update({"HOME": str(self.home), "PATH": os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])})

    def script(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def ccx(self, *args):
        return subprocess.run([sys.executable, str(CCX), *args], capture_output=True, text=True,
                              env=self.env, timeout=60, check=False)

    def calls(self):
        log = self.home / "fake-claude.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def state(self):
        return json.loads((self.home / "fake-claude-state.json").read_text())

    def write_state(self, markets=None, plugins=None):
        data = {"markets": markets or {}, "plugins": plugins or {}}
        (self.home / "fake-claude-state.json").write_text(json.dumps(data))

    @property
    def settings_path(self):
        return self.home / ".claude" / "settings.json"

    def write_settings(self, data):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(data, indent=2) + "\n")

    @property
    def ghostty(self):
        return self.home / ".config" / "ghostty"


def backups(directory, name):
    return sorted(directory.glob(f"{name}.cockpit-backup-*"))


class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_ok(self, *args):
        out = self.box.ccx("setup", *args)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def test_fresh_install_adds_stable_channel_installs_plugin_and_copies_profile(self):
        out = self.run_ok()
        calls = self.box.calls()
        self.assertIn(["plugin", "marketplace", "add", STABLE, "--scope", "user", "--json"], calls)
        self.assertIn(["plugin", "install", "orchestrator@claude-cockpit", "--scope", "user", "--json"], calls)
        profile = self.box.ghostty / "cockpit.conf"
        self.assertTrue(profile.is_file())
        self.assertFalse(profile.is_symlink(), "a link would break when Homebrew deletes the old version")
        self.assertEqual(profile.read_bytes(), PROFILE.read_bytes())
        config = (self.box.ghostty / "config").read_text()
        self.assertTrue(config.startswith(installer.INCLUDE_MARKER + "\n" + installer.INCLUDE_LINE + "\n"))
        self.assertIn("1.5.0 installed", out.stdout)

    def test_rerun_is_idempotent(self):
        self.run_ok()
        first_calls = len(self.box.calls())
        config_before = (self.box.ghostty / "config").read_text()
        out = self.run_ok()
        second = self.box.calls()[first_calls:]
        commands = [c[:3] for c in second]
        self.assertIn(["plugin", "marketplace", "update"], commands)
        self.assertIn(["plugin", "update", "orchestrator@claude-cockpit"], commands)
        self.assertNotIn(["plugin", "marketplace", "add"], commands)
        self.assertNotIn(["plugin", "install", "orchestrator@claude-cockpit"], commands)
        self.assertEqual((self.box.ghostty / "config").read_text(), config_before)
        self.assertEqual(backups(self.box.ghostty, "cockpit.conf") + backups(self.box.ghostty, "config"), [])
        self.assertIn("profile up to date", out.stdout)
        self.assertIn("already included", out.stdout)

    def test_existing_profile_and_config_are_backed_up_and_kept(self):
        self.box.ghostty.mkdir(parents=True)
        (self.box.ghostty / "cockpit.conf").write_text("font-size = 99\n")
        (self.box.ghostty / "config").write_text("theme = mine\n")
        self.run_ok("--no-plugin")
        (profile_backup,) = backups(self.box.ghostty, "cockpit.conf")
        self.assertEqual(profile_backup.read_text(), "font-size = 99\n")
        self.assertEqual((self.box.ghostty / "cockpit.conf").read_bytes(), PROFILE.read_bytes())
        (config_backup,) = backups(self.box.ghostty, "config")
        self.assertEqual(config_backup.read_text(), "theme = mine\n")
        config = (self.box.ghostty / "config").read_text()
        self.assertTrue(config.endswith("\ntheme = mine\n"), "the user's own settings stay, below the include")
        self.assertEqual(self.box.calls(), [], "--no-plugin never calls claude")

    def test_symlinked_profile_from_an_old_install_becomes_a_regular_file(self):
        old = Path(self.tmp.name) / "Caskroom" / "1.4.0" / "cockpit.conf"
        old.parent.mkdir(parents=True)
        old.write_text("old = profile\n")
        self.box.ghostty.mkdir(parents=True)
        (self.box.ghostty / "cockpit.conf").symlink_to(old)
        self.run_ok("--no-plugin")
        profile = self.box.ghostty / "cockpit.conf"
        self.assertFalse(profile.is_symlink())
        self.assertEqual(profile.read_bytes(), PROFILE.read_bytes())
        self.assertEqual(old.read_text(), "old = profile\n", "the link target is left alone")
        (saved,) = backups(self.box.ghostty, "cockpit.conf")
        self.assertEqual(saved.read_text(), "old = profile\n")

    def test_marketplace_declared_elsewhere_is_repointed_not_removed(self):
        clone = "/Users/someone/claude-cockpit"
        self.box.write_settings({"theme": "dark", "extraKnownMarketplaces": {
            "claude-cockpit": {"source": {"source": "directory", "path": clone}, "autoUpdate": True}}})
        self.box.write_state(markets={"claude-cockpit": {"source": "directory", "path": clone}},
                             plugins={"orchestrator@claude-cockpit": {"version": "1.4.0", "scope": "user",
                                                                      "enabled": False}})
        out = self.run_ok("--no-ghostty")
        settings = json.loads(self.box.settings_path.read_text())
        entry = settings["extraKnownMarketplaces"]["claude-cockpit"]
        self.assertEqual(entry["source"], {"source": "github", "repo": "KartikeyMish/claude-cockpit",
                                           "ref": "stable"})
        self.assertTrue(entry["autoUpdate"], "other keys of the entry (auto-update) survive")
        self.assertEqual(settings["theme"], "dark")
        (saved,) = backups(self.box.settings_path.parent, "settings.json")
        self.assertIn(clone, saved.read_text())
        calls = self.box.calls()
        self.assertNotIn(["plugin", "marketplace", "remove"], [c[:3] for c in calls])
        self.assertEqual(len([c for c in calls if c[:3] == ["plugin", "marketplace", "add"]]), 2)
        plugin = self.box.state()["plugins"]["orchestrator@claude-cockpit"]
        self.assertEqual((plugin["version"], plugin["enabled"]), ("1.5.0", True))
        self.assertIn("re-pointed marketplace claude-cockpit", out.stdout)
        self.assertIn("updated 1.4.0 → 1.5.0", out.stdout)

    def test_local_source_registers_the_checkout(self):
        self.run_ok("--no-ghostty", "--source", str(ROOT))
        self.assertIn(["plugin", "marketplace", "add", str(ROOT.resolve()), "--scope", "user", "--json"],
                      self.box.calls())

    def test_missing_claude_cli_fails_clearly(self):
        elsewhere = Path(self.tmp.name) / "no-claude"
        elsewhere.mkdir()
        box = Sandbox(elsewhere, with_claude=False)
        out = box.ccx("setup")
        self.assertEqual(out.returncode, 2)
        self.assertIn("`claude` CLI is not on PATH", out.stderr)
        self.assertIn("https://code.claude.com/docs/en/setup", out.stderr)
        self.assertFalse(box.ghostty.exists(), "nothing is half-installed")

    def test_bad_options_are_rejected(self):
        for args in (["--bogus"], ["--source"], ["--source", "/nonexistent/clone"]):
            with self.subTest(args=args):
                out = self.box.ccx("setup", *args)
                self.assertEqual(out.returncode, 2)
                self.assertIn("ccx setup", out.stderr)
        self.assertEqual(self.box.calls(), [])

    def test_version_flag_matches_the_release(self):
        out = self.box.ccx("--version")
        version = json.loads((ROOT / "plugins/orchestrator/.claude-plugin/plugin.json").read_text())["version"]
        self.assertEqual(out.stdout.strip(), f"ccx {version}")


class SourceSpecTests(unittest.TestCase):
    def test_github_shorthand_and_paths(self):
        self.assertEqual(installer.source_spec(STABLE),
                         {"source": "github", "repo": "KartikeyMish/claude-cockpit", "ref": "stable"})
        self.assertEqual(installer.source_spec("o/r"), {"source": "github", "repo": "o/r"})
        self.assertEqual(installer.source_spec("/x/y"), {"source": "directory", "path": "/x/y"})
        self.assertIsNone(installer.source_spec("https://example.com/m.git"))

    def test_same_source_compares_ref_and_resolved_paths(self):
        entry = {"name": "claude-cockpit", "source": "github", "repo": "KartikeyMish/claude-cockpit",
                 "ref": "main", "installLocation": "/x"}
        self.assertFalse(installer.same_source(entry, installer.source_spec(STABLE)))
        self.assertTrue(installer.same_source({**entry, "ref": "stable"}, installer.source_spec(STABLE)))
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            link.symlink_to(tmp)
            self.assertTrue(installer.same_source({"source": "directory", "path": str(link)},
                                                  {"source": "directory", "path": tmp}))

    def test_default_source_is_the_constant_stable_channel(self):
        self.assertEqual(installer.STABLE_SOURCE, STABLE)
        self.assertEqual(installer.parse_args([]).source, STABLE)

    def test_result_line_is_the_last_json_object(self):
        self.assertEqual(installer.last_json_object('Cloning…\n{"outcome": "ok"}\n'), {"outcome": "ok"})
        self.assertIsNone(installer.last_json_object("no json\n[1]\n"))

    def test_unreadable_settings_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{broken")
            with self.assertRaises(CockpitError):
                installer.repoint_declaration(path, {"source": "github", "repo": "o/r"}, "s", installer.Report())
            self.assertEqual(path.read_text(), "{broken")


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self.tmp.name)
        # A clone inside a Homebrew install would report Homebrew's own repository.
        self.box.script("git", 'echo "https://github.com/Homebrew/brew"')

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, *args):
        out = subprocess.run(["/bin/bash", str(ROOT / "install.sh"), *args], capture_output=True, text=True,
                             env={**self.box.env, "COCKPIT_BIN_DIR": str(self.box.home / "bin")},
                             timeout=120, check=False)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def test_script_never_derives_the_source_from_git_remote(self):
        text = (ROOT / "install.sh").read_text()
        self.assertNotIn("remote get-url", text)
        self.assertNotIn("git -C", text)

    def test_default_install_uses_the_stable_channel(self):
        self.install("--no-ccx")
        adds = [c for c in self.box.calls() if c[:3] == ["plugin", "marketplace", "add"]]
        self.assertEqual(adds, [["plugin", "marketplace", "add", STABLE, "--scope", "user", "--json"]])
        self.assertFalse(self.box.ghostty.exists(), "the profile is opt-in for clones (--with-ghostty)")

    def test_local_flag_registers_this_clone_and_links_ccx(self):
        self.install("--local")
        adds = [c for c in self.box.calls() if c[:3] == ["plugin", "marketplace", "add"]]
        self.assertEqual(adds[0][3], str(ROOT.resolve()))
        link = self.box.home / "bin" / "ccx"
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), ROOT / "bin" / "ccx")


@unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
class ShellcheckTests(unittest.TestCase):
    def test_install_scripts_pass_shellcheck(self):
        out = subprocess.run(["shellcheck", str(ROOT / "install.sh"), str(ROOT / "uninstall.sh")],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(out.returncode, 0, out.stdout)


if __name__ == "__main__":
    unittest.main()
