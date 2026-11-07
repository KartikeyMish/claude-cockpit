#!/usr/bin/env bash
# SessionStart hook: injects the orchestrator routing policy as session context.
#
# Plain stdout from a SessionStart hook is added to the model's context.
#   startup, clear, compact  always inject (fresh or reset context)
#   resume                   inject only if this conversation hasn't already received
#                            *this version* of the policy, so a resumed session picks
#                            up policy updates without getting a duplicate copy
#
# Controls (environment):
#   COCKPIT_ROUTING=off   never inject (per shell or per session)
#   COCKPIT_ROUTING=on    inject even for SDK/headless sessions
#   EVAL_COCKPIT_ROUTING  same values; fallback used by `claude plugin eval`
#                         (it only forwards EVAL_* variables)
#   default               inject for interactive CLI sessions only; SDK
#                         sessions (memory observers, IDE bridges, scripts)
#                         are skipped so they don't pay the extra tokens.
#
# This hook must never block or fail a session: every path exits 0, stdin is
# read with a timeout, and only the policy is printed to stdout.

set -u

# The plugin's subagentStatusLine only runs once the plugin data dir exists.
if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
  mkdir -p "$CLAUDE_PLUGIN_DATA" 2>/dev/null || true
fi

mode="${COCKPIT_ROUTING:-${EVAL_COCKPIT_ROUTING:-auto}}"
entrypoint="${CLAUDE_CODE_ENTRYPOINT:-cli}"

case "$mode" in
  off | 0 | false | no) exit 0 ;;
  on | 1 | true | yes) ;;
  *)
    case "$entrypoint" in
      sdk*) exit 0 ;;
    esac
    ;;
esac

policy="${CLAUDE_PLUGIN_ROOT:-}/hooks/routing-policy.md"
if [ ! -r "$policy" ]; then
  echo "orchestrator: routing policy not found at $policy; skipping injection" >&2
  exit 0
fi

# Hook input JSON: {"source": "startup|resume|clear|compact", "transcript_path": "...", ...}
input=""
IFS= read -r -d '' -t 2 input 2>/dev/null || true
json_field() {
  printf '%s' "$input" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -n 1
}
source="$(json_field source)"
transcript="$(json_field transcript_path)"

if [ "$source" = "resume" ] && [ -n "$transcript" ] && [ -r "$transcript" ]; then
  marker="$(head -n 1 "$policy")"   # e.g. "# Orchestrator routing (claude-cockpit policy v1.3)"
  # Already delivered by this hook (not merely mentioned in conversation)? Then skip.
  if grep -F -- "$marker" "$transcript" 2>/dev/null \
     | grep -qE '"hookEvent"[[:space:]]*:[[:space:]]*"SessionStart"'; then
    exit 0
  fi
fi

cat "$policy" 2>/dev/null || true
exit 0
