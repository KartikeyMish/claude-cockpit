# Changelog

## Plugin 1.3.0 / policy v1.3 — 2026-11-07
- **Plan first, then fan out.** Any multi-step task (including "continue from the handoff") opens with a short visible plan: workstreams, tier, what runs in parallel, what stays inline and why, and how it will be verified. Independent workstreams launch together as agents; only work coupled to state the orchestrator holds (sequential git on one working tree, decisions needing the conversation) stays inline. Progress is reported against the plan.
- **Checkpoint:** after ~15 inline tool calls the orchestrator re-plans and hands remaining independent pieces to agents.
- **One owner per working tree** for git operations.
- **Resumed sessions get policy updates:** the hook now also runs on resume and injects the policy if that conversation hasn't received the current version (it checks the transcript for this version's SessionStart delivery, so mere mentions don't count and nothing is duplicated). Hook input is read with a 2s timeout.
- ccx: a conversation open in two terminals is counted once in spend totals and marked `⧉ same chat as <pid>`.

## 1.3.0 — 2026-11-07
- **Team view** (`v` in the dashboard, or `ccx team [name|pid]`): a live tree of the orchestrator and every agent it spawned (nested agents included), with model·effort, elapsed time, context, cost, tool calls and what each one is doing now. It adds a shared timeline that shows agents working in parallel, an activity feed across all agents, and per-agent details with the latest report. It reads transcripts Claude already writes, so watching costs zero tokens.
- **Context and cost per session**: the dashboard and `ccx ls` show each session's context fill (bar + %, against the model's real window) and its API-equivalent spend, including its agents. Alerts appear at 70% and 85% with the current cost per message; `ccx doctor` flags nearly-full sessions.
- **Sessions waiting for you** (permission prompts) jump to the top, blink, and get a header alert.
- Pricing table from platform.claude.com (cache writes 5m/1h, cache reads, fast mode, US-only multiplier, Haiku 5.5 long-prompt tier); usage is de-duplicated per API response (Claude logs one response as several records); `<synthetic>` placeholders are ignored.

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
