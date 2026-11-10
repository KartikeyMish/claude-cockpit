"""Stale-hook detection and the plugin facts `ccx doctor` reports. Pure where it can be.

Claude Code reads a plugin's hook LIST once, when the process starts. `/clear` and
compaction keep it (they re-run SessionStart, but never re-read hooks.json), so only a
new process, a fresh start or `claude --resume <id>`, loads new hook entries.

The rule:
  directory marketplace  root = <source.path>/<plugin source from its marketplace.json>;
                         installPath is ignored. Hook scripts are live (edits run at once),
                         so only a newer hooks file makes a process stale.
  any other source       root = installPath. A process is stale if it started before the
                         hooks file's mtime or the install's lastUpdated (an update installs
                         a new version dir; old processes keep the old one).
  hooks file             <root>/hooks/hooks.json, or the `hooks` path in <root>/.claude-plugin/plugin.json.
Evidence (Claude Code 2.1.322, isolated config): for a directory source, CLAUDE_PLUGIN_ROOT and
the hook list come from the checkout and cache-only edits never run; for github, CLAUDE_PLUGIN_ROOT is installPath.

Claude's registry files (installed_plugins.json, known_marketplaces.json) are an
internal format: every field is optional and anything malformed yields "unknown",
never an exception.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .core import Proc, Session, is_claude_proc

MARKETPLACE = "claude-cockpit"
PLUGIN_NAME = "orchestrator"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE}"
DEFAULT_PLUGIN_SUBDIR = "plugins/orchestrator"
DATA_DIR_NAME = f"{PLUGIN_NAME}-{MARKETPLACE}"   # Claude's ${CLAUDE_PLUGIN_DATA} for this plugin
# `ps etime` has 1 s resolution and ccx reads the clock after ps returns, so a derived
# start time can be up to ~1 s late. Inside this window after the change we can't tell.
START_SLACK_MS = 3000
PROBE_TTL_SECONDS = 10.0
OFF_VALUES = frozenset({"off", "0", "false", "no"})
POLICY_MARKER = re.compile(rb"claude-cockpit policy v([0-9][0-9A-Za-z.\-]*)\)")
SESSION_START_EVENT = re.compile(rb'"hookEvent"\s*:\s*"SessionStart"')

SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

CURRENT, STALE, UNKNOWN = "current", "stale", "unknown"
HOOKS_MARK = "⟳"
HOOKS_HINT = "⟳ restart to load current hooks: exit, then claude --resume <id> (ccx doctor lists them)"


def claude_home_from_env(env: Mapping[str, str]) -> Path:
    """Claude's config dir: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    return Path(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- install in effect

@dataclass(frozen=True)
class PluginInstall:
    """The orchestrator install whose hooks Claude runs. root is None when not installed."""
    root: Path | None
    source: str                      # directory | github | git | url | missing | unknown
    source_detail: str               # path, owner/repo#ref, or why it is missing
    version: str | None              # from the effective root's plugin.json
    registry_version: str | None     # what Claude's registry recorded
    enabled: bool | None             # settings.json enabledPlugins; None = not stated
    hooks_mtime_ms: int | None       # mtime of the hooks file in effect
    hooks_file: Path | None = None   # <root>/hooks/hooks.json, or plugin.json's `hooks` path
    updated_ms: int | None = None    # registry lastUpdated; marketplace (non-directory) installs only
    note: str = ""                   # how the root was guessed, when it had to be

    @property
    def hooks_json(self) -> Path | None:
        return self.hooks_file

    @property
    def policy_path(self) -> Path | None:
        return self.root / "hooks" / "routing-policy.md" if self.root else None

    @property
    def stale_after_ms(self) -> int | None:
        """Processes started before this run an older hook list (None: can't tell)."""
        if self.hooks_mtime_ms is None:
            return None
        return max(self.hooks_mtime_ms, self.updated_ms or 0)


def registry_entry(plugins_dir: Path) -> dict:
    """The user-scope registry entry for the orchestrator, else the first one listed."""
    entries = as_dict(as_dict(read_json(plugins_dir / "installed_plugins.json")).get("plugins")).get(PLUGIN_ID)
    if not isinstance(entries, list):
        return {}
    dicts = [e for e in entries if isinstance(e, dict)]
    return next((e for e in dicts if e.get("scope") == "user"), dicts[0] if dicts else {})


def marketplace_source(plugins_dir: Path) -> dict:
    return as_dict(as_dict(as_dict(read_json(plugins_dir / "known_marketplaces.json")).get(MARKETPLACE)).get("source"))


def directory_plugin_root(checkout: Path) -> tuple[Path, str]:
    """(<checkout>/<plugin source> from the checkout's marketplace.json, note). Falls back to
    plugins/orchestrator, with a note saying so, only when marketplace.json doesn't say."""
    manifest_path = checkout / ".claude-plugin" / "marketplace.json"
    manifest = as_dict(read_json(manifest_path))
    plugins = manifest.get("plugins") if isinstance(manifest.get("plugins"), list) else []
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME and isinstance(plugin.get("source"), str):
            return (checkout / plugin["source"]).resolve(), ""
    why = "has no orchestrator entry" if manifest else "is unreadable"
    return checkout / DEFAULT_PLUGIN_SUBDIR, f"{manifest_path} {why}; assumed {DEFAULT_PLUGIN_SUBDIR}"


def describe_source(source: dict) -> str:
    kind = source.get("source")
    if kind == "github":
        return str(source.get("repo", "?")) + (f"#{source['ref']}" if source.get("ref") else "")
    return str(source.get("path") or source.get("url") or kind or "?")


def mtime_ms(path: Path | None, stat: Callable[[str], os.stat_result] = os.stat) -> int | None:
    if path is None:
        return None
    try:
        return int(stat(str(path)).st_mtime * 1000)
    except OSError:
        return None


def iso_ms(value: object) -> int | None:
    """Registry timestamps such as 2026-11-08T11:51:19.772Z -> epoch ms."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def enabled_flag(claude_home: Path) -> bool | None:
    value = as_dict(as_dict(read_json(claude_home / "settings.json")).get("enabledPlugins")).get(PLUGIN_ID)
    return value if isinstance(value, bool) else None


def effective_root(source: dict, entry: dict) -> tuple[Path | None, str]:
    """(plugin dir Claude runs, note). The single place that decides it:
    a directory marketplace runs from its checkout (installPath ignored); anything else from installPath."""
    if source.get("source") == "directory" and isinstance(source.get("path"), str):
        return directory_plugin_root(Path(os.path.expanduser(source["path"])))
    if isinstance(entry.get("installPath"), str):
        return Path(entry["installPath"]), ""
    return None, ""


def hooks_file(root: Path, manifest: dict) -> Path:
    """plugin.json's `hooks` (a path, or the hooks inline in plugin.json), else <root>/hooks/hooks.json."""
    declared = manifest.get("hooks")
    if isinstance(declared, list):
        declared = next((d for d in declared if isinstance(d, str)), None)
    if isinstance(declared, str) and declared.strip():
        return (root / declared).resolve()
    if isinstance(declared, dict):
        return root / ".claude-plugin" / "plugin.json"
    return root / "hooks" / "hooks.json"


def resolve_install(claude_home: Path, stat: Callable[[str], os.stat_result] = os.stat) -> PluginInstall:
    plugins_dir = claude_home / "plugins"
    entry, source = registry_entry(plugins_dir), marketplace_source(plugins_dir)
    root, note = effective_root(source, entry)
    if not entry and not source:
        kind, detail = "missing", f"no {PLUGIN_ID} in {plugins_dir}"
    else:
        kind, detail = str(source.get("source") or "unknown"), describe_source(source) if source else "?"
    manifest = as_dict(read_json(root / ".claude-plugin" / "plugin.json")) if root else {}
    version = manifest.get("version") if isinstance(manifest.get("version"), str) else None
    hooks = hooks_file(root, manifest) if root else None
    registry_version = entry.get("version") if isinstance(entry.get("version"), str) else None
    # A marketplace update installs a new version dir: processes from before it keep the old one.
    updated = None if kind == "directory" else iso_ms(entry.get("lastUpdated"))
    return PluginInstall(root=root, source=kind, source_detail=detail, version=version,
                         registry_version=registry_version, enabled=enabled_flag(claude_home),
                         hooks_mtime_ms=mtime_ms(hooks, stat), hooks_file=hooks, updated_ms=updated, note=note)


class HooksProbe:
    """Cheap, cached stale-after time for the dashboard, which refreshes every 1.5 s."""

    def __init__(self, claude_home: Path, ttl_s: float = PROBE_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 resolve: Callable[[Path], PluginInstall] = resolve_install) -> None:
        self.claude_home, self.ttl_s, self.clock, self.resolve = claude_home, ttl_s, clock, resolve
        self._at: float | None = None
        self._install: PluginInstall | None = None

    def install(self) -> PluginInstall:
        now = self.clock()
        if self._install is None or self._at is None or now - self._at >= self.ttl_s:
            self._install, self._at = self.resolve(self.claude_home), now
        return self._install

    def stale_after_ms(self) -> int | None:
        return self.install().stale_after_ms


# ---------------------------------------------------------------- processes

@dataclass(frozen=True)
class HookedProcess:
    pid: int
    started_ms: int | None
    state: str                      # current | stale | unknown
    session_id: str | None
    name: str | None
    cwd: str | None

    def restart_command(self) -> str:
        """What the user runs in that session's terminal, after typing `exit`. A session id that
        isn't a UUID (the registry is just a file) is never pasted into the command: the picker opens."""
        sid = self.session_id if self.session_id and SESSION_ID.fullmatch(self.session_id) else None
        resume = f"claude --resume {sid}" if sid else "claude --resume"
        return f"cd {shell_quote(self.cwd)} && {resume}" if self.cwd and self.cwd != "?" else resume


def shell_quote(path: str) -> str:
    home = str(Path.home())
    shown = "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
    if re.fullmatch(r"[~A-Za-z0-9_./@%+=:,-]+", shown):
        return shown
    return "'" + path.replace("'", "'\\''") + "'"


def hooks_in_future(hooks_ms: int | None, now_ms: int, slack_ms: int = START_SLACK_MS) -> bool:
    """A hooks.json dated ahead of the clock (cp -p, rsync, clock skew) can't date anything."""
    return hooks_ms is not None and hooks_ms > now_ms + slack_ms


def classify(started_ms: int | None, hooks_ms: int | None, now_ms: int, slack_ms: int = START_SLACK_MS) -> str:
    """stale only when the process certainly started before hooks.json last changed."""
    if started_ms is None or hooks_ms is None or hooks_in_future(hooks_ms, now_ms, slack_ms):
        return UNKNOWN
    if started_ms < hooks_ms:
        return STALE
    return CURRENT if started_ms >= hooks_ms + slack_ms else UNKNOWN


def is_sdk_child(proc: Proc, session: Session | None) -> bool:
    """SDK/IDE/background children: they don't take user prompts, so the guard is moot."""
    return "stream-json" in proc.args or bool(session and session.is_sdk)


def hooked_processes(procs: Mapping[int, Proc], sessions: Iterable[Session], hooks_ms: int | None,
                     now_ms: int) -> list[HookedProcess]:
    """Interactive claude sessions from the registry, with their processes' hooks age.

    Only processes Claude registered as a session count: `claude -p`, `claude mcp serve` and
    other helpers take no prompts the guard would see, and have no conversation to resume."""
    found = []
    for session in sorted(sessions, key=lambda s: s.pid):
        proc = procs.get(session.pid)
        if proc is None or not is_claude_proc(proc) or is_sdk_child(proc, session):
            continue
        started = proc.started_ms(now_ms)
        found.append(HookedProcess(
            pid=proc.pid, started_ms=started, state=classify(started, hooks_ms, now_ms),
            session_id=session.session_id, name=session.name, cwd=session.cwd,
        ))
    return found


def hooks_states(procs: Mapping[int, Proc], sessions: Iterable[Session], hooks_ms: int | None,
                 now_ms: int) -> dict[int, str]:
    """pid -> current | stale | unknown, for registered interactive sessions only."""
    return {p.pid: p.state for p in hooked_processes(procs, sessions, hooks_ms, now_ms)}


def stale_pids(procs: Mapping[int, Proc], sessions: Iterable[Session], hooks_ms: int | None,
               now_ms: int) -> frozenset[int]:
    return frozenset(p.pid for p in hooked_processes(procs, sessions, hooks_ms, now_ms) if p.state == STALE)


# ---------------------------------------------------------------- policy, data dir, env

def policy_version(text: bytes) -> str | None:
    """Version in a routing-policy title line such as `(claude-cockpit policy v1.5)`."""
    match = POLICY_MARKER.search(text)
    return match.group(1).decode("ascii", "replace") if match else None


def installed_policy_version(install: PluginInstall) -> str | None:
    try:
        with open(str(install.policy_path), "rb") as handle:
            return policy_version(handle.readline())
    except (OSError, TypeError):
        return None


def injected_policy_version(lines: Iterable[bytes]) -> str | None:
    """The last policy version a SessionStart hook delivered into this transcript."""
    latest = None
    for line in lines:
        if b"claude-cockpit policy v" in line and SESSION_START_EVENT.search(line):
            latest = policy_version(line) or latest
    return latest


def transcript_policy_version(projects_dir: Path, session_id: str) -> str | None:
    for path in projects_dir.glob(f"*/{session_id}.jsonl"):
        try:
            with path.open("rb") as handle:
                return injected_policy_version(handle)
        except OSError:
            return None
    return None


def guard_data_dir(claude_home: Path) -> Path:
    return claude_home / "plugins" / "data" / DATA_DIR_NAME


def dir_writable(path: Path) -> bool | None:
    """True/False for an existing directory; None when it doesn't exist yet (first session creates it)."""
    if not path.is_dir():
        return None
    return os.access(str(path), os.W_OK | os.X_OK)


def switched_off(env: Mapping[str, str], settings: Mapping[str, str],
                 names: tuple[str, ...] = ("COCKPIT_ROUTING", "COCKPIT_GUARD")) -> list[str]:
    """`NAME=value (where)` for each cockpit switch set to off in this shell or settings.json env."""
    found = []
    for name in names:
        for where, values in (("environment", env), ("settings.json env", settings)):
            value = values.get(name)
            if value is not None and str(value).lower() in OFF_VALUES:
                found.append(f"{name}={value} ({where})")
    return found
