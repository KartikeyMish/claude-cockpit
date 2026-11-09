#!/usr/bin/env python3
"""Delegation guard: the main thread orchestrates, agents do the work.

Wired to UserPromptSubmit and PreToolUse in hooks.json. It only ever acts on the
main thread: tool calls made inside a subagent carry ``agent_id`` and pass through.

UserPromptSubmit
    Adds a one-line routing reminder to each user prompt, and remembers whether
    the user explicitly asked for inline work ("do it yourself", "no agents needed").

PreToolUse (main thread only)
    * File edits (Edit, Write, MultiEdit, NotebookEdit) are denied: agents make
      changes. Files under ~/.claude (memory, plans, settings) are exempt.
    * Work tools (Read, Grep, Glob, Bash, PowerShell, WebFetch, WebSearch) are
      denied until an agent has been launched for the current prompt
      (COCKPIT_START_BUDGET, default 0). After each Agent launch a few are allowed
      for checking results (COCKPIT_VERIFY_BUDGET, default 3).
    * Never limited: shell commands made only of git/gh (plus cd/export and read-only pipe
      filters such as `| head`), reads under ~/.claude or Claude's temp dir (agent output),
      and every other tool (Agent, MCP tools, AskUserQuestion, ...).
    * Calls without ``prompt_id`` (older Claude Code) are never limited: those versions may
      not mark subagent calls with ``agent_id`` either, and blocking an agent would be worse.

Controls: COCKPIT_ROUTING=off disables policy, reminder and guard.
COCKPIT_GUARD=off keeps the policy and reminder but drops enforcement.
SDK/headless sessions are skipped unless COCKPIT_ROUTING (or EVAL_COCKPIT_ROUTING) is on.

Fail-open by design: any error means no output and exit 0, so the tool call proceeds.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from typing import Callable

try:
    import fcntl
except ImportError:  # Windows: run without locking rather than not at all
    fcntl = None

OFF = frozenset({"off", "0", "false", "no"})
ON = frozenset({"on", "1", "true", "yes"})
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
WORK_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash", "PowerShell", "WebFetch", "WebSearch"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
DEFAULT_START_BUDGET = 0
DEFAULT_VERIFY_BUDGET = 3
MAX_BUDGET = 50
STATE_MAX_AGE_S = 7 * 24 * 3600
RACE_RECHECK_S = 0.15  # an Agent launched in the same message may be checked a moment later

# Shell commands: free only when every segment is git/gh, navigation, or a read-only pipe filter.
FREE_COMMANDS = frozenset({"git", "gh"})
NEUTRAL_COMMANDS = frozenset({"cd", "pushd", "popd", "export", "true", ":"})
PIPE_FILTERS = frozenset({"head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "sort", "uniq", "cut",
                          "cat", "tr", "column", "jq", "less", "more", "sed", "awk"})
WRAPPERS = frozenset({"env", "command"})
SEPARATOR_CHARS = frozenset(";&|()\n")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FD_REDIRECT = re.compile(r"\d*[<>]&-?\d*|&>>?")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n")
# Fallback when the command can't be tokenised (unbalanced quotes): first word decides.
GIT_FIRST_WORD = re.compile(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)*(?:\S+/)?(?:git|gh)(?:\s|$)")

# Explicit requests for inline work, ignored when the same sentence carries a negation or a
# "why", so complaints ("why no subagents?") and refusals ("don't do it yourself") don't count.
INLINE_REQUEST = re.compile(
    r"\b(?:do|handle|fix|run|check|answer|edit|change|make) (?:it|this|that|them|these) yourself\b"
    r"|\b(?:don't|dont|do not) (?:delegate|spawn|use (?:any )?(?:sub)?agents?)\b"
    r"|\bno (?:sub)?agents? (?:needed|please|for this)\b",
    re.IGNORECASE)
NEGATION = re.compile(r"\b(?:not|never|no|don't|dont|didn't|didnt|doesn't|won't|wont|shouldn't|why)\b",
                      re.IGNORECASE)
PASTED = re.compile(r"<pasted_content\b.*?</pasted_content>", re.IGNORECASE | re.DOTALL)
NOTIFICATION_PREFIXES = ("<agent-message", "<task-notification")
CLAUDE_TMP_CHILD = re.compile(r"claude-\d+(?:/|$)")

TIERS_HINT = ("orchestrator:scout to find or read things, orchestrator:routine-worker to run "
              "commands, builds or tests or make clear-spec edits, orchestrator:senior-engineer "
              "for changes where parts interact, orchestrator:debugger for unexplained failures")
REMINDER = ("claude-cockpit: if this request needs any tool beyond git, open with the right tier "
            "agent(s) (several at once when independent); you keep conversation, git and checking "
            "results. Main-thread file edits are blocked.")
REMINDER_SOFT = ("claude-cockpit: if this request needs any tool beyond git, open with the right "
                 "tier agent(s); you keep conversation, git and checking results.")
REMINDER_INLINE = "claude-cockpit: the user asked for inline work on this request; do it yourself."
EDIT_REASON = ("claude-cockpit: file edits belong to agents, not the main thread. Brief "
               "orchestrator:routine-worker (clear spec) or orchestrator:senior-engineer (parts "
               "interact) with the file, the change and how to check it; launch independent edits "
               "together. Inline edits are allowed only if the user says \"do it yourself\".")
START_REASON = ("claude-cockpit: open this request with an agent instead of working inline. "
                f"Launch {TIERS_HINT}; several at once when independent. git and gh stay with you.")
VERIFY_REASON = ("claude-cockpit: you've used the {limit} checks allowed after the last agent "
                 f"launch. Hand further work to an agent: {TIERS_HINT}. git and gh stay with you.")

State = dict
Step = Callable[[State], "tuple[State, object]"]


def routing_on(env: "os._Environ[str] | dict[str, str]") -> bool:
    mode = (env.get("COCKPIT_ROUTING") or env.get("EVAL_COCKPIT_ROUTING") or "auto").lower()
    if mode in OFF:
        return False
    if mode in ON:
        return True
    return not env.get("CLAUDE_CODE_ENTRYPOINT", "cli").startswith("sdk")


def budget(env: "os._Environ[str] | dict[str, str]", name: str, default: int) -> int:
    try:
        return max(0, min(MAX_BUDGET, int(env.get(name, default))))
    except ValueError:
        return default


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class Places:
    """Where reads and writes are never limited: ~/.claude, and Claude's temp dir (agent output)."""

    def __init__(self, env: "os._Environ[str] | dict[str, str]", cwd: object) -> None:
        self.claude_home = os.path.realpath(os.path.expanduser("~/.claude"))
        self.cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd()
        roots = {"/tmp", "/private/tmp"} | {env[k] for k in ("CLAUDE_CODE_TMPDIR", "TMPDIR") if env.get(k)}
        self.tmp_roots = {os.path.realpath(r).rstrip(os.sep) for r in roots}

    def resolve(self, raw: str) -> str:
        return os.path.realpath(os.path.join(self.cwd, os.path.expanduser(raw)))

    def is_claude_home(self, raw: object) -> bool:
        return isinstance(raw, str) and bool(raw) and under(self.resolve(raw), self.claude_home)

    def is_free_read(self, raw: object) -> bool:
        if not isinstance(raw, str) or not raw:
            return False
        real = self.resolve(raw)
        return under(real, self.claude_home) or any(
            real.startswith(root + os.sep) and CLAUDE_TMP_CHILD.match(real[len(root) + 1:])
            for root in self.tmp_roots)


def shell_segments(command: str) -> "list[tuple[bool, list[str]]] | None":
    """Split a shell command into (after_pipe, words) segments; None if it can't be tokenised."""
    text = FD_REDIRECT.sub(" ", command)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments, words, after_pipe = [], [], False
    for token in tokens:
        if token and set(token) <= SEPARATOR_CHARS:
            if words:
                segments.append((after_pipe, words))
            words, after_pipe = [], token.strip("()\n") in {"|", "|&"}
        else:
            words.append(token)
    if words:
        segments.append((after_pipe, words))
    return segments


def is_git_only(command: str) -> bool:
    segments = shell_segments(strip_heredoc_bodies(command))
    if segments is None:
        return bool(GIT_FIRST_WORD.match(command))
    saw_git = False
    for after_pipe, words in segments:
        rest = list(words)
        while rest and (ASSIGNMENT.match(rest[0]) or rest[0] in WRAPPERS):
            rest = rest[1:]
        if not rest or rest[0].startswith("#"):
            continue
        name = os.path.basename(rest[0])
        if name in FREE_COMMANDS:
            saw_git = True
        elif not (name in NEUTRAL_COMMANDS or (after_pipe and name in PIPE_FILTERS)):
            return False
    return saw_git


def strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies (data such as commit messages, not commands), keeping the `<<EOF` line."""
    out, pos = [], 0
    for match in HEREDOC.finditer(command):
        if match.start() < pos:
            continue
        end = re.compile(rf"^[ \t]*{re.escape(match.group(2))}[ \t]*$", re.MULTILINE).search(command, match.end())
        out.append(command[pos:match.end()])
        pos = match.end() if end is None else end.end()
    out.append(command[pos:])
    return "".join(out)


def is_free_work(tool: str, tool_input: dict, places: Places) -> bool:
    if tool in SHELL_TOOLS:
        return is_git_only(str(tool_input.get("command") or ""))
    if tool == "Read":
        return places.is_free_read(tool_input.get("file_path"))
    if tool in {"Grep", "Glob"}:
        return places.is_free_read(tool_input.get("path"))
    return False


def fresh_state(prompt_id: object) -> State:
    return {"prompt_id": prompt_id, "inline_ok": False, "delegated": False, "used": 0}


def current_state(state: State, prompt_id: object) -> State:
    """The state for this prompt: stale state from an earlier prompt starts over."""
    if prompt_id and state.get("prompt_id") != prompt_id:
        return fresh_state(prompt_id)
    return {**fresh_state(prompt_id), **state}


def state_file(data_dir: str, session_id: object) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:80] or "unknown"
    return os.path.join(data_dir, "guard", safe + ".json")


def with_state(path: str, step: Step) -> object:
    """Read-modify-write the session state under an exclusive lock (never through a symlink)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        raw = fh.read()
        try:
            loaded = json.loads(raw) if raw.strip() else {}
        except ValueError:
            loaded = {}
        new_state, result = step(loaded if isinstance(loaded, dict) else {})
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(new_state))
        return result


def prune(guard_dir: str, now: float) -> None:
    try:
        for entry in os.scandir(guard_dir):
            if entry.is_file() and now - entry.stat().st_mtime > STATE_MAX_AGE_S:
                os.unlink(entry.path)
    except OSError:
        pass


def asks_for_inline(prompt: str) -> bool:
    """True when the user explicitly asks for main-thread work in their own words."""
    text = PASTED.sub(" ", prompt.replace("’", "'"))
    for match in INLINE_REQUEST.finditer(text):
        sentence_start = max(text.rfind(mark, 0, match.start()) for mark in ".!?\n") + 1
        if not NEGATION.search(text, sentence_start, match.start()):
            return True
    return False


def on_prompt(payload: dict, path: str, guard_on: bool) -> "dict | None":
    text = str(payload.get("prompt") or "")
    notification = text.lstrip().startswith(NOTIFICATION_PREFIXES)
    prompt_id = payload.get("prompt_id")

    def step(old: State) -> "tuple[State, object]":
        if notification:  # an agent reported back: work was already delegated
            new = {**fresh_state(prompt_id), "inline_ok": bool(old.get("inline_ok")), "delegated": True}
        else:
            new = {**fresh_state(prompt_id), "inline_ok": asks_for_inline(text)}
        return new, new

    state = with_state(path, step)
    prune(os.path.dirname(path), time.time())
    if notification:
        return None
    message = REMINDER_INLINE if state["inline_ok"] else (REMINDER if guard_on else REMINDER_SOFT)
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": message}}


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def on_tool(payload: dict, path: str, env: "os._Environ[str] | dict[str, str]") -> "dict | None":
    if payload.get("agent_id"):  # inside a subagent: that is where the work belongs
        return None
    prompt_id = payload.get("prompt_id")
    if not prompt_id:
        # Claude Code versions that don't stamp prompt_id may not stamp agent_id on subagent
        # calls either; enforcing there could block agents, so the guard stands down.
        return None
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    places = Places(env, payload.get("cwd"))

    if tool in AGENT_TOOLS:
        return with_state(path, lambda s: ({**current_state(s, prompt_id), "delegated": True, "used": 0}, None))
    if tool in EDIT_TOOLS:
        if places.is_claude_home(tool_input.get("file_path") or tool_input.get("notebook_path")):
            return None

        def edit_step(old: State) -> "tuple[State, object]":
            state = current_state(old, prompt_id)
            return state, (None if state["inline_ok"] else deny(EDIT_REASON))

        return with_state(path, edit_step)
    if tool not in WORK_TOOLS or is_free_work(tool, tool_input, places):
        return None

    start = budget(env, "COCKPIT_START_BUDGET", DEFAULT_START_BUDGET)
    verify = budget(env, "COCKPIT_VERIFY_BUDGET", DEFAULT_VERIFY_BUDGET)

    def step(old: State) -> "tuple[State, object]":
        state = current_state(old, prompt_id)
        if state["inline_ok"]:
            return state, None
        limit = verify if state["delegated"] else start
        if state["used"] < limit:
            return {**state, "used": state["used"] + 1}, None
        return state, deny(VERIFY_REASON.format(limit=limit) if state["delegated"] else START_REASON)

    result = with_state(path, step)
    if result == deny(START_REASON):
        time.sleep(RACE_RECHECK_S)  # an Agent call in the same message may not have registered yet
        result = with_state(path, step)
    return result


def main() -> int:
    env = os.environ
    if not routing_on(env):
        return 0
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        return 0
    guard_on = (env.get("COCKPIT_GUARD") or "on").lower() not in OFF
    data_dir = env.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        env.get("TMPDIR") or "/tmp", f"claude-cockpit-{getattr(os, 'getuid', lambda: 'user')()}")
    path = state_file(data_dir, payload.get("session_id"))
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        out = on_prompt(payload, path, guard_on)
    elif event == "PreToolUse" and guard_on:
        out = on_tool(payload, path, env)
    else:
        out = None
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never block a session because the guard broke
        sys.exit(0)
