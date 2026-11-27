"""`ccx setup` step: point Claude Code's `statusLine` at `ccx statusline`.

The status line lives in the user's settings.json, not in the plugin: the docs
document `statusLine` only for settings files (a plugin settings.json is not
documented to carry it), and `${CLAUDE_PLUGIN_ROOT}` changes on every update.
The command names ccx by a path that survives upgrades: the PATH entry (for
Homebrew, its stable bin/ link, never the versioned Caskroom folder it resolves
to), else this checkout's bin/ccx.

  unset             install the cockpit status line
  cockpit-owned     refresh it (same command, current refresh interval)
  someone else's    keep it and print a hint, unless --statusline forces it;
                    settings.json is backed up before any rewrite
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path

from .installer import REPO_ROOT, Report, backup, read_settings

REFRESH_INTERVAL_S = 15   # countdowns and other sessions' state change while this one is idle
OWNED_RE = re.compile(r"""(?:^|[\s/'"])ccx['"]?\s+statusline\s*$""")
STABLE_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
FORCE_HINT = "run `ccx setup --statusline` to replace it (settings.json is backed up first)"


def is_owned(entry: object) -> bool:
    """A statusLine entry that runs `ccx statusline`, whatever path it names ccx by."""
    return (isinstance(entry, dict) and isinstance(entry.get("command"), str)
            and OWNED_RE.search(entry["command"].strip()) is not None)


def ccx_path(repo_root: Path = REPO_ROOT, environ=os.environ) -> str:
    """The first ccx on PATH (or in a usual bin dir) that is this install, unresolved."""
    ours = os.path.realpath(repo_root / "bin" / "ccx")
    candidates = [shutil.which("ccx", path=environ.get("PATH"))]
    if environ.get("COCKPIT_BIN_DIR"):
        candidates.append(os.path.join(environ["COCKPIT_BIN_DIR"], "ccx"))
    candidates += [os.path.join(os.path.expanduser(d), "ccx") for d in STABLE_BIN_DIRS]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.path.realpath(candidate) == ours:
            return os.path.abspath(candidate)
    return str(repo_root / "bin" / "ccx")


def statusline_command(path: str) -> str:
    """Same convention as the plugin's scripts: `python3 -I -S` skips site-packages and
    PYTHON* variables, which is both safer and ~10ms faster per render."""
    return f"python3 -I -S {shlex.quote(path)} statusline"


def desired(current: object, command: str) -> dict:
    """Our entry; padding and other display keys of an entry we own are kept."""
    keep = {k: v for k, v in current.items() if k not in {"type", "command", "refreshInterval"}} \
        if is_owned(current) else {}
    return {"type": "command", "command": command, "refreshInterval": REFRESH_INTERVAL_S, **keep}


def write_json_atomic(path: Path, data: dict) -> None:
    """Write beside the real file (through a symlink) and rename over it, keeping its mode."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_statusline(settings_path: Path, force: bool, stamp: str, report: Report,
                      command: str | None = None) -> str:
    """Install, refresh or keep the status line. Returns what happened:
    installed | updated | current | kept | replaced."""
    command = command or statusline_command(ccx_path())
    data = read_settings(settings_path)   # unreadable settings stop here, untouched
    current = data.get("statusLine")
    owned = is_owned(current)
    if current is not None and not owned and not force:
        shown = current.get("command") if isinstance(current, dict) else current
        report.warn(f"kept your status line ({shown}); {FORCE_HINT}")
        return "kept"
    entry = desired(current, command)
    if current == entry:
        report.ok(f"status line up to date: {command}")
        return "current"
    if settings_path.exists():
        report.warn(f"backed up {backup(settings_path, stamp)}")
    write_json_atomic(settings_path, {**data, "statusLine": entry})
    outcome = "updated" if owned else "replaced" if current is not None else "installed"
    report.ok(f"status line {outcome}: {command} (refresh every {REFRESH_INTERVAL_S}s)")
    return outcome
