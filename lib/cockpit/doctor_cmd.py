"""`ccx doctor [--json]`: check the cockpit setup end to end.

Each check returns Check records; the text and JSON views render the same records,
and the exit code is 1 when any check failed (something needs action), else 0.
Warnings (`!`) are advisory and keep the exit code at 0.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from . import VERSION
from .cli import PLUGIN_ID, color, run_quiet, settings_env, tier_overrides, usage_by_pid
from .core import (
    DEFAULT_IDLE_HOURS, CockpitError, Proc, Session, current_ms, duplicate_of, gc_candidates, human_mb, load_sessions,
    parse_hours, protected_pids, read_processes, short_path,
)
from .doctor import (
    STALE, UNKNOWN, HookedProcess, hooks_in_future, PluginInstall, claude_home_from_env, dir_writable, guard_data_dir,
    hooked_processes, installed_policy_version, resolve_install, switched_off, transcript_policy_version,
)
from .installer import ghostty_binary, ghostty_dir
from .pricing import context_level

OK, WARN, FAIL = "ok", "warn", "fail"
EXPECTED_AGENTS = 5
RESTART_HINT = "exit, then claude --resume <id> in that directory"


@dataclass(frozen=True)
class Check:
    name: str
    status: str                     # ok | warn | fail
    label: str
    hint: str = ""
    details: tuple[str, ...] = ()   # extra indented lines (e.g. one per stale session)


@dataclass(frozen=True)
class Context:
    """Everything the checks read, gathered once."""
    env: Mapping[str, str]
    claude_home: Path
    install: PluginInstall
    procs: Mapping[int, Proc]
    sessions: tuple[Session, ...]
    now_ms: int
    ps_error: str = ""
    hooked: tuple[HookedProcess, ...] = field(default=())


def build_context(env: Mapping[str, str] | None = None, run_ps: Callable[[], str] | None = None) -> Context:
    env = dict(os.environ) if env is None else env
    home = claude_home_from_env(env)
    install = resolve_install(home)
    try:
        procs, error = read_processes(run_ps), ""
    except CockpitError as exc:
        procs, error = {}, str(exc)
    now = current_ms()
    sessions = tuple(load_sessions(home / "sessions", procs, now))
    hooked = tuple(hooked_processes(procs, sessions, install.stale_after_ms, now))
    return Context(env=env, claude_home=home, install=install, procs=procs, sessions=sessions,
                   now_ms=now, ps_error=error, hooked=hooked)


def local_time(ms: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) if ms is not None else "?"


# ---------------------------------------------------------------- checks (plugin, hooks)

def check_install(ctx: Context) -> list[Check]:
    inst = ctx.install
    if inst.root is None:
        return [Check("plugin-in-effect", FAIL, "orchestrator plugin in effect: not installed", "run: ccx setup")]
    where = f"{inst.source} {short_path(inst.source_detail)}"
    hooks_name = short_path(str(inst.hooks_json))
    if inst.hooks_mtime_ms is None:
        return [Check("plugin-in-effect", FAIL, f"orchestrator {inst.version or '?'} from {where}: "
                      f"{hooks_name} missing", "run: ccx setup")]
    label = (f"orchestrator {inst.version or '?'} in effect from {where} · "
             f"{hooks_name} changed {local_time(inst.hooks_mtime_ms)}")
    if inst.updated_ms is not None:
        label += f" · updated {local_time(inst.updated_ms)}"
    rule = ("local checkout: hook scripts are live, only hooks.json changes need a restart"
            if inst.source == "directory" else "marketplace install: any plugin update needs a restart")
    checks = [Check("plugin-in-effect", OK, label, details=(rule,))]
    if inst.note:
        checks.append(Check("plugin-root", WARN, f"plugin dir guessed: {inst.note}",
                            "fix the checkout's .claude-plugin/marketplace.json"))
    if hooks_in_future(inst.stale_after_ms, ctx.now_ms):
        checks.append(Check("hooks-mtime", WARN, f"{hooks_name} or the install date is in the future "
                            f"({local_time(inst.stale_after_ms)}), so no session can be dated against it",
                            f"touch {hooks_name}" if inst.updated_ms is None else "check the system clock"))
    if inst.enabled is False:
        checks.append(Check("plugin-enabled", FAIL, "orchestrator plugin disabled in settings.json",
                            f"claude plugin enable {PLUGIN_ID}"))
    return checks


def check_hooks(ctx: Context) -> list[Check]:
    """Processes that started before hooks.json changed run an older guard (or none)."""
    if ctx.ps_error:
        return [Check("hooks-current", WARN, f"could not check running sessions' hooks: {ctx.ps_error}")]
    if ctx.install.stale_after_ms is None:
        return [Check("hooks-current", WARN, "could not check running sessions' hooks: no hooks.json in effect")]
    stale = [p for p in ctx.hooked if p.state == STALE]
    unknown = [p for p in ctx.hooked if p.state == UNKNOWN]
    checks = []
    if stale:
        dupes = duplicate_of(ctx.sessions)
        details = tuple(
            f"⟳ {p.name or 'pid ' + str(p.pid)} (pid {p.pid}, started {local_time(p.started_ms)}): "
            + (f"same chat as pid {dupes[p.pid]}: just exit, resume it once" if p.pid in dupes
               else f"exit, then {p.restart_command()}") for p in stale)
        checks.append(Check("hooks-current", FAIL,
                            f"{len(stale)} of {len(ctx.hooked)} claude sessions run an older hook list than the plugin in effect",
                            "restart each to load the current hooks; /clear and compaction don't", details))
    else:
        checks.append(Check("hooks-current", OK, f"{len(ctx.hooked)} claude sessions run the current hooks"))
    if unknown:
        pids = ", ".join(str(p.pid) for p in unknown)
        checks.append(Check("hooks-unknown", WARN, f"hooks age unknown for pid {pids}",
                            "started within seconds of the change, or ps gave no start time"))
    return checks


def check_policy(ctx: Context, projects_dir: Path | None = None) -> list[Check]:
    """Live sessions whose transcript holds an older routing policy than the one installed."""
    installed = installed_policy_version(ctx.install)
    if installed is None:
        return [Check("policy-version", WARN, "routing policy version unknown (routing-policy.md unreadable)")]
    projects = projects_dir or ctx.claude_home / "projects"
    behind = []
    for s in ctx.sessions:
        if s.is_sdk or not s.session_id:
            continue
        injected = transcript_policy_version(projects, s.session_id)
        if injected and injected != installed:
            behind.append(f"{s.name}: v{injected}")
    if behind:
        return [Check("policy-version", WARN, f"routing policy v{installed} installed; older in "
                      + ", ".join(behind), "exit, then claude --resume <id> in that directory")]
    return [Check("policy-version", OK, f"routing policy v{installed} installed and injected")]


def check_switches(ctx: Context) -> list[Check]:
    found = switched_off(ctx.env, settings_env(ctx.claude_home / "settings.json"))
    if found:
        return [Check("cockpit-switches", WARN, "routing/guard turned off: " + ", ".join(found),
                      "unset them to re-enable the routing policy and guard")]
    return [Check("cockpit-switches", OK, "routing policy and delegation guard switched on")]


def check_data_dir(ctx: Context) -> list[Check]:
    data = guard_data_dir(ctx.claude_home)
    writable = dir_writable(data)
    shown = short_path(str(data))
    if writable is None:
        return [Check("guard-data", WARN, f"guard data dir {shown} not created yet",
                      "the first session after install creates it")]
    if not writable:
        return [Check("guard-data", FAIL, f"guard data dir {shown} is not writable",
                      "the guard can't keep per-prompt state; fix its permissions")]
    return [Check("guard-data", OK, f"guard data dir writable ({shown})")]


# ---------------------------------------------------------------- checks (CLI, terminal, sessions)

def check_claude(_ctx: Context) -> list[Check]:
    path = shutil.which("claude")
    if not path:
        return [Check("claude-cli", FAIL, "claude CLI on PATH", "install from https://code.claude.com/docs/en/setup")]
    out = run_quiet([path, "--version"], 20)
    ok = bool(out and out.returncode == 0)
    return [Check("claude-cli", OK if ok else FAIL, f"claude CLI: {out.stdout.strip() if out else 'did not answer'}")]


def installed_plugin() -> dict | None:
    out = run_quiet(["claude", "plugin", "list", "--json"])
    try:
        plugins = json.loads(out.stdout) if out else []
    except ValueError:
        return None
    if not isinstance(plugins, list):
        return None
    return next((p for p in plugins if isinstance(p, dict) and p.get("id") == PLUGIN_ID), {})


def check_plugin(ctx: Context) -> list[Check]:
    if not shutil.which("claude"):
        return [Check("plugin", FAIL, "orchestrator plugin", "needs the claude CLI")]
    entry = installed_plugin()
    if entry is None:
        return [Check("plugin", WARN, "orchestrator plugin", "could not read `claude plugin list --json`")]
    if not entry:
        return [Check("plugin", FAIL, "orchestrator plugin installed", "run: ccx setup")]
    if not entry.get("enabled"):
        return [Check("plugin", FAIL, "orchestrator plugin enabled", f"claude plugin enable {PLUGIN_ID}")]
    root = ctx.install.root or (Path(entry["installPath"]) if entry.get("installPath") else None)
    agents = sorted(p.stem for p in root.glob("agents/*.md")) if root else []
    return [Check("plugin", OK if len(agents) == EXPECTED_AGENTS else FAIL,
                  f"orchestrator {entry.get('version')} enabled · agents: {', '.join(agents) or 'none'}")]


def check_overrides(ctx: Context) -> list[Check]:
    found = tier_overrides(dict(ctx.env), settings_env(ctx.claude_home / "settings.json"))
    if found:
        return [Check("tier-overrides", WARN, "tier models/efforts are overridden: " + ", ".join(found),
                      "unset these so each agent runs its own model and effort")]
    return [Check("tier-overrides", OK, "agents run their own model and effort (no global overrides)")]


def check_terminal(ctx: Context) -> list[Check]:
    term = ctx.env.get("TERM_PROGRAM", "unknown")
    in_tmux = bool(ctx.env.get("TMUX"))
    checks = [Check("terminal", WARN if in_tmux else OK, f"terminal: {term}" + (" (inside tmux)" if in_tmux else ""),
                    "tmux needs allow-passthrough + extended-keys; see README")]
    ghostty = ghostty_binary()
    if not ghostty:
        return checks + [Check("ghostty", WARN, "Ghostty installed", "brew install --cask ghostty, then: ccx setup")]
    profile = (ghostty_dir() / "cockpit.conf").exists()
    checks.append(Check("ghostty-profile", OK if profile else FAIL, "Ghostty cockpit profile", "run: ccx setup"))
    out = run_quiet([ghostty, "+validate-config"], 20)
    detail = " | ".join(((out.stdout + out.stderr).strip().splitlines() if out else ["no answer"])[:3])
    valid = OK if out and out.returncode == 0 else FAIL
    return checks + [Check("ghostty-config", valid, "Ghostty config valid", detail)]


def check_sessions(ctx: Context) -> list[Check]:
    if not (ctx.claude_home / "sessions").is_dir():
        return [Check("sessions", WARN, "session registry",
                      f"{ctx.claude_home / 'sessions'} missing; `ccx ls` will be empty")]
    sessions = [s for s in ctx.sessions if not s.is_sdk]
    idle = gc_candidates(sessions, ctx.now_ms, parse_hours(DEFAULT_IDLE_HOURS), protected_pids(dict(ctx.procs)))
    usage = usage_by_pid(sessions)
    checks = []
    full = [s for s in sessions if s.pid in usage and context_level(usage[s.pid].pct) == "critical"]
    if full:
        names = ", ".join(f"{s.name} ({usage[s.pid].pct:.0f}%)" for s in full)
        checks.append(Check("context", WARN, f"context nearly full: {names}",
                            "run /compact in those sessions or start fresh"))
    label = f"{len(sessions)} interactive sessions"
    if idle:
        held = human_mb(sum(s.tree_rss_kb for s in idle))
        return checks + [Check("sessions", WARN, f"{label}; {len(idle)} idle > {DEFAULT_IDLE_HOURS}h hold {held}",
                               "ccx gc")]
    return checks + [Check("sessions", OK, label)]


GATHER = (check_claude, check_plugin, check_install, check_hooks, check_policy, check_switches,
          check_data_dir, check_overrides, check_terminal, check_sessions)


def gather(ctx: Context, checks=GATHER) -> list[Check]:
    return [result for check in checks for result in check(ctx)]


# ---------------------------------------------------------------- views

def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


def render_text(checks: list[Check]) -> str:
    marks = {OK: color("✓", "32"), FAIL: color("✗", "31"), WARN: color("!", "33")}
    lines = [color(f"claude-cockpit doctor (ccx {VERSION})", "1")]
    for c in checks:
        hint = color(f"  → {c.hint}", "2") if c.hint and c.status != OK else ""
        lines.append(f"  {marks.get(c.status, '?')} {c.label}{hint}")
        lines.extend(f"      {line}" for line in c.details)
    return "\n".join(lines)


def process_json(p: HookedProcess) -> dict:
    return dict(asdict(p), started_at=local_time(p.started_ms) if p.started_ms is not None else None,
                restart=(f"exit, then {p.restart_command()}" if p.state == STALE else None))


def report_json(ctx: Context, checks: list[Check]) -> dict:
    inst = ctx.install
    return {
        "ccx_version": VERSION,
        "healthy": exit_code(checks) == 0,
        "plugin": {"version": inst.version, "registry_version": inst.registry_version, "source": inst.source,
                   "source_detail": inst.source_detail, "root": str(inst.root) if inst.root else None,
                   "enabled": inst.enabled, "hooks_json": str(inst.hooks_json) if inst.hooks_json else None,
                   "hooks_changed_ms": inst.hooks_mtime_ms, "updated_ms": inst.updated_ms,
                   "stale_after_ms": inst.stale_after_ms, "root_note": inst.note or None},
        "checks": [dict(asdict(c), details=list(c.details)) for c in checks],
        "processes": [process_json(p) for p in ctx.hooked],
        "summary": {state: sum(1 for p in ctx.hooked if p.state == state)
                    for state in ("current", STALE, UNKNOWN)},
    }


def cmd_doctor(args: list[str], context: Callable[[], Context] = build_context,
               checks=GATHER) -> int:
    unknown = [a for a in args if a != "--json"]
    if unknown:
        raise CockpitError(f"ccx doctor: unknown option {unknown[0]!r} (allowed: --json)")
    ctx = context()
    results = gather(ctx, checks)
    print(json.dumps(report_json(ctx, results), indent=2) if "--json" in args else render_text(results))
    return exit_code(results)


__all__ = ["Check", "Context", "build_context", "cmd_doctor", "exit_code", "render_text", "report_json"]
