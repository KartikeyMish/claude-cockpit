"""`ccx setup`: connect Claude Code to the claude-cockpit release channel and
install the Ghostty profile.

This is the one command to run after `brew install --cask claude-cockpit`, and the
plugin and profile half of install.sh. It is safe to re-run (after `brew upgrade`
too): each step checks before it changes anything, and a file it rewrites is
backed up first as <file>.cockpit-backup-<timestamp>.

  marketplace  `claude-cockpit` from KartikeyMish/claude-cockpit#stable (or --source)
  plugin       orchestrator@claude-cockpit installed or updated at user scope, enabled
  Ghostty      profile copied to <config>/ghostty/cockpit.conf and included from
               <config>/ghostty/config. It is a copy, never a link, because
               Homebrew deletes the old versioned folder on every upgrade.

The marketplace is never removed: `claude plugin marketplace remove` uninstalls its
plugins and deletes their saved data. When the marketplace is already declared
with another source, Claude refuses a plain re-add, so the declaration in Claude's
user settings is re-pointed and the add retried.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .core import CLAUDE_HOME, CockpitError

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "terminal" / "ghostty" / "cockpit.conf"
MARKETPLACE = "claude-cockpit"
PLUGIN_ID = f"orchestrator@{MARKETPLACE}"
# The release channel. A constant on purpose: deriving it from `git remote` inside a
# Homebrew install would find Homebrew's own repository.
STABLE_SOURCE = "KartikeyMish/claude-cockpit#stable"
CLAUDE_SETUP_URL = "https://code.claude.com/docs/en/setup"
GHOSTTY_APP_BIN = "/Applications/Ghostty.app/Contents/MacOS/ghostty"
# The same two lines install.sh always wrote; uninstall.sh removes them.
INCLUDE_MARKER = "# claude-cockpit profile (remove these two lines to opt out)"
INCLUDE_LINE = "config-file = ?cockpit.conf"
INCLUDE_RE = re.compile(r"^[ \t\r\f\v]*config-file[ \t]*=[ \t]*\??cockpit\.conf[ \t\r\f\v]*$", re.M)
GITHUB_SHORTHAND = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)(?:#([^\s#]+))?")
SOURCE_KEYS = ("source", "repo", "ref", "path", "url")
CLAUDE_TIMEOUT_S = 300  # adding a marketplace clones its repository
USAGE = """\
usage: ccx setup [--source SOURCE] [--no-plugin] [--no-ghostty]

  --source SOURCE  marketplace source (default: KartikeyMish/claude-cockpit#stable);
                   a path registers a local checkout, for development
  --no-plugin      skip the Claude Code marketplace and plugin
  --no-ghostty     skip the Ghostty profile"""


# ---------------------------------------------------------------- options

@dataclass(frozen=True)
class Options:
    source: str = STABLE_SOURCE
    plugin: bool = True
    ghostty: bool = True


def is_path_source(source: str) -> bool:
    return source.startswith(("/", ".", "~"))


def normalize_source(source: str) -> str:
    """Paths become absolute checkouts; anything else is passed to Claude as given."""
    if not source.strip():
        raise CockpitError(f"ccx setup: --source needs a value\n{USAGE}")
    if not is_path_source(source):
        return source
    path = Path(source).expanduser().resolve()
    if not (path / ".claude-plugin" / "marketplace.json").is_file():
        raise CockpitError(f"ccx setup: {path} is not a claude-cockpit checkout (no .claude-plugin/marketplace.json)")
    return str(path)


def parse_args(args: Sequence[str]) -> Options:
    source, plugin, ghostty = STABLE_SOURCE, True, True
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--no-plugin":
            plugin = False
        elif arg == "--no-ghostty":
            ghostty = False
        elif arg == "--source":
            source = rest.pop(0) if rest else ""
        elif arg.startswith("--source="):
            source = arg.split("=", 1)[1]
        else:
            raise CockpitError(f"ccx setup: unknown option {arg!r}\n{USAGE}")
    return Options(source=normalize_source(source), plugin=plugin, ghostty=ghostty)


# ---------------------------------------------------------------- sources

def source_spec(source: str) -> dict[str, str] | None:
    """How Claude records SOURCE in settings, for the forms ccx setup re-points."""
    if is_path_source(source):
        return {"source": "directory", "path": source}
    match = GITHUB_SHORTHAND.fullmatch(source)
    if not match:
        return None
    owner, repo, ref = match.groups()
    spec = {"source": "github", "repo": f"{owner}/{repo}"}
    return {**spec, "ref": ref} if ref else spec


def _comparable(entry: dict) -> tuple:
    values = {key: entry.get(key) for key in SOURCE_KEYS}
    if isinstance(values["path"], str):
        values["path"] = os.path.realpath(values["path"])
    return tuple(values[key] for key in SOURCE_KEYS)


def same_source(entry: dict, spec: dict | None) -> bool:
    return spec is not None and _comparable(entry) == _comparable(spec)


def describe(entry: dict) -> str:
    if entry.get("source") == "github":
        return entry.get("repo", "?") + (f"#{entry['ref']}" if entry.get("ref") else "")
    return str(entry.get("path") or entry.get("url") or entry.get("source") or "?")


# ---------------------------------------------------------------- output

def _paint(text: str, code: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


@dataclass(frozen=True)
class Report:
    emit: Callable[[str], None] = print

    def say(self, text: str) -> None:
        self.emit(f"{_paint('==>', '1')} {text}")

    def ok(self, text: str) -> None:
        self.emit(f"  {_paint('✓', '32')} {text}")

    def warn(self, text: str) -> None:
        self.emit(f"  {_paint('!', '33')} {text}")

    def note(self, text: str) -> None:
        self.emit(f"    {text}")


# ---------------------------------------------------------------- claude CLI

@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)


def last_json_object(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


@dataclass(frozen=True)
class Claude:
    exe: str
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def call(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return self.run([self.exe, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                            timeout=CLAUDE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CockpitError(f"`claude {' '.join(args)}` took longer than {CLAUDE_TIMEOUT_S}s") from exc
        except OSError as exc:
            raise CockpitError(f"could not run {self.exe}: {exc}") from exc

    def outcome(self, *args: str) -> Outcome:
        """Run a `--json` command and read its machine-readable result line."""
        out = self.call(*args)
        data = last_json_object(out.stdout) or {}
        message = str(data.get("message") or out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}")
        return Outcome(ok=out.returncode == 0 and data.get("outcome", "ok") == "ok", message=message, data=data)

    def listing(self, *args: str) -> list[dict]:
        out = self.call(*args)
        try:
            data = json.loads(out.stdout) if out.returncode == 0 else None
        except ValueError:
            data = None
        if not isinstance(data, list):
            detail = out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"
            raise CockpitError(f"could not read `claude {' '.join(args)}`: {detail}")
        return [item for item in data if isinstance(item, dict)]

    def version(self) -> str:
        out = self.call("--version")
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else "unknown version"


def require_claude() -> Claude:
    path = shutil.which("claude")
    if not path:
        raise CockpitError(f"the `claude` CLI is not on PATH. Install Claude Code first ({CLAUDE_SETUP_URL}), "
                           "then run `ccx setup` again.")
    return Claude(path)


# ---------------------------------------------------------------- files

def backup(path: Path, stamp: str) -> Path:
    """Copy PATH's content (through a symlink, like `cp -pL`) to PATH.cockpit-backup-STAMP."""
    dest = path.with_name(f"{path.name}.cockpit-backup-{stamp}")
    suffix = 1
    while dest.exists() or dest.is_symlink():
        dest = path.with_name(f"{path.name}.cockpit-backup-{stamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, dest)
    return dest


def write_through(path: Path, text: str) -> None:
    """Rewrite in place, so a symlinked dotfile keeps its link and its mode."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CockpitError(f"can't read {path} ({exc}); fix it, then run `ccx setup` again") from exc
    if not isinstance(data, dict):
        raise CockpitError(f"{path} is not a JSON object; fix it, then run `ccx setup` again")
    return data


def repoint_declaration(settings_path: Path, spec: dict, stamp: str, report: Report) -> dict | None:
    """Point the user-settings declaration of the marketplace at SPEC. Returns the old source."""
    data = read_settings(settings_path)
    markets = data.get("extraKnownMarketplaces")
    entry = markets.get(MARKETPLACE) if isinstance(markets, dict) else None
    if not isinstance(entry, dict):
        return None
    updated = {**data, "extraKnownMarketplaces": {**markets, MARKETPLACE: {**entry, "source": spec}}}
    report.warn(f"backed up {backup(settings_path, stamp)}")
    write_through(settings_path, json.dumps(updated, indent=2) + "\n")
    old = entry.get("source")
    return old if isinstance(old, dict) else {}


# ---------------------------------------------------------------- plugin

def find(items: list[dict], key: str, value: str) -> dict | None:
    return next((item for item in items if item.get(key) == value), None)


def refresh_marketplace(claude: Claude, current: dict, report: Report) -> None:
    result = claude.outcome("plugin", "marketplace", "update", MARKETPLACE, "--json")
    if result.ok:
        report.ok(f"marketplace {MARKETPLACE} refreshed from {describe(current)}")
    else:
        report.warn(f"could not refresh marketplace {MARKETPLACE} ({result.message}); using the copy on disk")


def add_marketplace(claude: Claude, source: str, settings_path: Path, stamp: str, report: Report) -> None:
    def add() -> Outcome:
        return claude.outcome("plugin", "marketplace", "add", source, "--scope", "user", "--json")

    result = add()
    spec = source_spec(source)
    if not result.ok and result.data.get("failureCode") == "declared" and spec is not None:
        old = repoint_declaration(settings_path, spec, stamp, report)
        if old is not None:
            report.ok(f"re-pointed marketplace {MARKETPLACE}: {describe(old)} → {describe(spec)}")
            result = add()
    if not result.ok:
        raise CockpitError(f"could not add marketplace {MARKETPLACE} from {source}: {result.message}")
    report.ok(f"marketplace {MARKETPLACE} → {source}")


def ensure_marketplace(claude: Claude, source: str, settings_path: Path, stamp: str, report: Report) -> None:
    current = find(claude.listing("plugin", "marketplace", "list", "--json"), "name", MARKETPLACE)
    if current is not None and same_source(current, source_spec(source)):
        refresh_marketplace(claude, current, report)
    else:
        add_marketplace(claude, source, settings_path, stamp, report)


def ensure_plugin(claude: Claude, report: Report) -> None:
    installed = find(claude.listing("plugin", "list", "--json"), "id", PLUGIN_ID)
    if installed is None:
        result = claude.outcome("plugin", "install", PLUGIN_ID, "--scope", "user", "--json")
        if not result.ok:
            raise CockpitError(f"could not install {PLUGIN_ID}: {result.message}")
    else:
        result = claude.outcome("plugin", "update", PLUGIN_ID, "--json")
        if not result.ok:
            raise CockpitError(f"could not update {PLUGIN_ID}: {result.message}")
        if installed.get("enabled") is False:
            enabled = claude.outcome("plugin", "enable", PLUGIN_ID, "--json")
            if not (enabled.ok or enabled.data.get("alreadyInGoalState")):
                raise CockpitError(f"could not enable {PLUGIN_ID}: {enabled.message}")
    final = find(claude.listing("plugin", "list", "--json"), "id", PLUGIN_ID) or {}
    state = "enabled" if final.get("enabled", True) else "disabled"
    report.ok(f"{PLUGIN_ID} {plugin_change(installed, result.data, final)}, {state} "
              f"({final.get('scope', 'user')} scope)")


def plugin_change(before: dict | None, result: dict, after: dict) -> str:
    if before is None:
        return f"{after.get('version', '?')} installed"
    old, new = result.get("oldVersion"), result.get("newVersion")
    if old and new and old != new:
        return f"updated {old} → {new} (restart Claude sessions to load it)"
    return f"{after.get('version', '?')} up to date"


# ---------------------------------------------------------------- Ghostty

def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def ghostty_dir() -> Path:
    return config_home() / "ghostty"


def ghostty_binary() -> str | None:
    return shutil.which("ghostty") or (GHOSTTY_APP_BIN if Path(GHOSTTY_APP_BIN).exists() else None)


def install_profile(source: Path, directory: Path, stamp: str, report: Report) -> Path:
    """Copy the profile into DIRECTORY as a regular file, backing up anything different."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "cockpit.conf"
    content = source.read_bytes()
    if target.is_symlink():
        if target.exists():
            report.warn(f"backed up {backup(target, stamp)}")
        target.unlink()  # e.g. a link into an older, deleted Homebrew version
    elif target.is_file() and target.read_bytes() == content:
        report.ok(f"profile up to date: {target}")
        return target
    elif target.exists():
        report.warn(f"backed up {backup(target, stamp)}")
    target.write_bytes(content)
    report.ok(f"profile → {target}")
    return target


def include_profile(config: Path, stamp: str, report: Report) -> None:
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if INCLUDE_RE.search(text):
        report.ok(f"already included from {config}")
        return
    if config.exists():
        report.warn(f"backed up {backup(config, stamp)}")
    write_through(config, f"{INCLUDE_MARKER}\n{INCLUDE_LINE}\n\n{text}")
    report.ok(f"included from {config} (your own settings below it still win)")


def check_ghostty(report: Report) -> None:
    exe = ghostty_binary()
    if not exe:
        report.warn("Ghostty isn't installed; the profile is ready for it: brew install --cask ghostty")
        return
    try:
        out = subprocess.run([exe, "+validate-config"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is not None and out.returncode == 0:
        report.ok("Ghostty config valid")
    else:
        report.warn("Ghostty reports config problems: run `ghostty +validate-config`")


# ---------------------------------------------------------------- entry

def setup(options: Options, report: Report, stamp: str) -> None:
    if options.plugin:
        report.say("Claude Code plugin")
        claude = require_claude()
        report.ok(f"claude {claude.version()}")
        ensure_marketplace(claude, options.source, CLAUDE_HOME / "settings.json", stamp, report)
        ensure_plugin(claude, report)
    if options.ghostty:
        report.say("Ghostty profile")
        directory = ghostty_dir()
        install_profile(PROFILE, directory, stamp, report)
        include_profile(directory / "config", stamp, report)
        check_ghostty(report)


def run_setup(args: Sequence[str]) -> int:
    if any(arg in {"-h", "--help"} for arg in args):
        print(USAGE)
        return 0
    options = parse_args(args)
    report = Report()
    try:
        setup(options, report, time.strftime("%Y%m%d-%H%M%S"))
    except OSError as exc:
        raise CockpitError(f"ccx setup: {exc}") from exc
    report.say("Done")
    if options.plugin:
        report.note("Open a new Claude session (or run /clear) to load the orchestrator, then run: ccx")
        report.note("Plugin updates: re-run `ccx setup`, or turn on auto-update in "
                    "/plugin → Marketplaces → claude-cockpit.")
    return 0
