"""A stand-in for the `claude` CLI, for tests of `ccx setup` and install.sh.

It logs every call (one JSON argv per line in $FAKE_CLAUDE_LOG) and keeps
marketplace and plugin state the way Claude Code 2.1.321 does, as observed:

* `plugin marketplace add SRC` declares the marketplace in user settings
  (extraKnownMarketplaces) and records it; re-adding the same source is fine.
* Adding a *different* source while the settings declaration names another one
  fails with failureCode "declared" (the real CLI does exactly this).
* `--json` commands print one result line on stdout; failures also print a ✘ line
  on stderr and exit 1.

Not collected by unittest (the file name doesn't start with "test").
"""

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ["HOME"])
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
SETTINGS = CLAUDE_DIR / "settings.json"
STATE = Path(os.environ.get("FAKE_CLAUDE_STATE") or HOME / "fake-claude-state.json")
LOG = Path(os.environ.get("FAKE_CLAUDE_LOG") or HOME / "fake-claude.log")
NAME = "claude-cockpit"
PLUGIN = "orchestrator@claude-cockpit"
AVAILABLE = os.environ.get("FAKE_PLUGIN_VERSION", "1.5.0")


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def spec_for(source):
    if source.startswith(("/", ".", "~")):
        return {"source": "directory", "path": source}
    repo, _, ref = source.partition("#")
    return {"source": "github", "repo": repo, **({"ref": ref} if ref else {})}


def result(command, ok, message, **extra):
    print(json.dumps({"command": command, "outcome": "ok" if ok else "failed", "message": message, **extra}))
    if not ok:
        print(f"✘ Failed: {message}", file=sys.stderr)
        sys.exit(1)


def marketplace(args, state):
    action = args[0]
    if action == "list":
        print(json.dumps([{"name": n, **spec, "installLocation": "/fake"} for n, spec in state["markets"].items()]))
    elif action == "add":
        spec = spec_for(args[1])
        settings = load(SETTINGS, {})
        declared = settings.get("extraKnownMarketplaces", {}).get(NAME)
        if declared and declared.get("source") != spec:
            result("marketplace-add", False, "its source doesn't match its extraKnownMarketplaces entry",
                   failureCode="declared")
        markets = settings.get("extraKnownMarketplaces", {})
        entry = {**(declared or {}), "source": spec}
        save(SETTINGS, {**settings, "extraKnownMarketplaces": {**markets, NAME: entry}})
        save(STATE, {**state, "markets": {**state["markets"], NAME: spec}})
        result("marketplace-add", True, f"Successfully added marketplace: {NAME}", marketplace=NAME)
    elif action == "update":
        if args[1] not in state["markets"]:
            result("marketplace-update", False, f"Marketplace '{args[1]}' not found", failureCode="not_configured")
        result("marketplace-update", True, f"Successfully updated marketplace: {args[1]}")
    elif action == "remove":
        save(STATE, {**state, "markets": {}, "plugins": {}})
        print("removed (and deleted plugin data)")
    else:
        sys.exit(f"fake claude: unsupported marketplace command {args}")


def plugin(args, state):
    action, plugins = args[0], state["plugins"]
    if action == "list":
        print(json.dumps([{"id": pid, **info, "installPath": "/fake"} for pid, info in plugins.items()]))
    elif action == "install":
        if NAME not in state["markets"]:
            result("install", False, "marketplace not found", failureCode="not_found")
        if args[1] not in plugins:
            info = {"version": AVAILABLE, "scope": "user", "enabled": True}
            save(STATE, {**state, "plugins": {**plugins, args[1]: info}})
        result("install", True, f"Successfully installed plugin: {args[1]}")
    elif action == "update":
        old = plugins[args[1]]["version"]
        save(STATE, {**state, "plugins": {**plugins, args[1]: {**plugins[args[1]], "version": AVAILABLE}}})
        result("update", True, "updated", updateOutcome="up_to_date" if old == AVAILABLE else "updated",
               oldVersion=old, newVersion=AVAILABLE)
    elif action == "enable":
        if plugins[args[1]]["enabled"]:
            result("enable", False, "already enabled", failureCode="already_in_goal_state", alreadyInGoalState=True)
        save(STATE, {**state, "plugins": {**plugins, args[1]: {**plugins[args[1]], "enabled": True}}})
        result("enable", True, "enabled")
    elif action == "marketplace":
        marketplace(args[1:], state)
    else:
        sys.exit(f"fake claude: unsupported plugin command {args}")


def main(argv):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        handle.write(json.dumps(argv) + "\n")
    if argv == ["--version"]:
        print("2.1.321 (Claude Code)")
        return
    state = {"markets": {}, "plugins": {}, **load(STATE, {})}
    if argv[:1] == ["plugin"]:
        plugin(argv[1:], state)
    else:
        sys.exit(f"fake claude: unsupported command {argv}")


if __name__ == "__main__":
    main(sys.argv[1:])
