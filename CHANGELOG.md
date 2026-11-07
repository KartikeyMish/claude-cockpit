# Changelog

## 1.2.0 — 2026-11-07
Token and safety hardening from an independent product review.
- Agents can no longer spawn agents (`disallowedTools: Agent`, or an allowlist), so one task can't fan out into delegate → reviewer → reviewer chains.
- Every agent has `maxTurns` and a report word cap; architect is read-only by allowlist (no Bash/MCP).
- Routing policy: inline by default; delegate only for flooding output, >10 tool calls / 5+ files, parallel work, or a different tier. Architect runs when the user asks for a design decision. One escalation, then ask. At most one review pass.
- SessionStart hook no longer re-injects on resume/fork (`startup|clear|compact`).
- `ccx doctor` warns when `CLAUDE_CODE_EFFORT_LEVEL` / `CLAUDE_CODE_SUBAGENT_*` flatten every tier.
- Dashboard tier table is read from agent frontmatter; a test keeps the policy table in sync.
- Ghostty: `clipboard-read = ask` (programs can no longer read your clipboard silently).
- Evals: 51-file fixture, neutral prompts, editing allowed, negative cases (small edit stays inline and lands; migrations never reach architect).

## 1.1.0 — 2026-11-07
- `ccx` opens an interactive dashboard: live sessions, RAM gauges, launch / worktree / close / clean-idle, tier panel.
- Live subagent status rows (tier glyph, model·effort, token sparkline, context %, elapsed, task label).
- Cockpit colour theme for Claude Code.
- "Second brain" routing: decide instead of asking, keep track of open loops, report outcomes plainly.
- Safety fixes from code review: strict `ccx gc` flags, pid-reuse detection via process start time, race-safe close, installer fixes for fresh Ghostty configs, symlinked dotfiles, and pipefail.

## 1.0.0 — 2026-11-07
- Five-tier orchestrator plugin, SessionStart routing policy, `ccx` CLI, Ghostty profile, installer, tests, routing evals.
