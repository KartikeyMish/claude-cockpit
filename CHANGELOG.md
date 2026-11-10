# Changelog

All notable changes to claude-cockpit. The `orchestrator` plugin, `ccx` and the Ghostty profile ship together under one version. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/). `make release` turns `[Unreleased]` into the next version, so add entries there as you go.

## [Unreleased]

## [1.5.0] - 2026-11-10
First public release, under the MIT license. v1.4 was never released on its own; its notes are folded in below.

### Releases and install
- **Homebrew cask:** `brew install --cask kartikeymish/tap/claude-cockpit` puts `ccx` on PATH. It's a cask, not a formula, so it installs on Macs whose Command Line Tools are out of date.
- **`ccx setup`:** the one command after `brew install`, and safe to re-run after `brew upgrade`. It adds the `claude-cockpit` marketplace from the `#stable` channel, installs or updates `orchestrator@claude-cockpit` at user scope, and copies the Ghostty profile into place (a copy, because Homebrew deletes the old versioned folder on upgrade). Any file it changes is backed up as `*.cockpit-backup-*`. When the marketplace already points somewhere else, it re-points the existing entry instead of removing it, so the plugin's saved data survives.
- **Release channel:** versioned GitHub Releases. The marketplace source `KartikeyMish/claude-cockpit#stable` only moves when a release is published, so plugin installs only ever see released versions.
- **`make release VERSION=x.y.z`** (`DRY_RUN=1` to rehearse): checks the tree, bumps every version field, dates this changelog, runs the tests and Claude's validator, then tags, pushes, waits for the release workflow, moves `stable` and updates the Homebrew tap. `scripts/release.py notes X.Y.Z` prints one version's notes.
- **GitHub Actions:** CI runs the tests on macOS (Apple's `/usr/bin/python3` and the latest Python) and Linux (3.9 and latest), plus shellcheck and `claude plugin validate`. Tag pushes build the release tarball and its `.sha256`.
- **One version everywhere:** `ccx --version` reports the release version (it still said 1.3.0).
- **Python 3.9 is the documented minimum**, the version macOS ships as `/usr/bin/python3`. `ccx` exits with a clear message on anything older.
- `install.sh` now defaults to the `#stable` channel. It used to derive the source from the clone's `git remote`, which resolves to Homebrew's own repository inside a Homebrew install. `--local` still registers the clone for development, and no longer removes the marketplace to re-point it.
- `ccx doctor` points at `ccx setup` instead of `./install.sh`.
- MIT license.

### ccx
- **Delegation savings:** a signed `SAVED` column in the dashboard and `ccx ls` (`--json` adds `saved_usd`, `saved_partial`, `would_compact`), a savings line in the team view header, and `ccx savings [--days N] [--json]` for totals across recent sessions, with and without sessions that would have compacted inline. It estimates what agents saved compared with the main session doing the same work itself: model mix, context, minus delegation overhead. The estimate can be negative and rounds every uncertain choice against delegation. See README "Measuring savings".
- **Output tokens no longer undercounted for transcripts from Claude Code 2.1.281–2.1.300.** Those builds sometimes logged a response's streaming snapshot (a few output tokens) before the final count under the same message id, and the first record won. Now the record with the most output tokens wins.
- Session table cells line up under their headers again (every cell sat one column to the left).
- On narrow terminals the session table drops `DIR` to fit `SAVED`; `DIR` shows at 87 columns and wider (it used to show from 80).

### Routing policy v1.5 (2026-11-08)
Field report: short requests were still done by the main agent. v1.4 allowed it: "tiny requests (≤ 3 tool calls) you just do", plus an 8-call inline budget. A policy is also only a request, so the model could reason past it.
- **Short requests go to agents too.** A one-file lookup goes to scout and a one-line edit to routine-worker. For a short request the first tool call is the Agent call; multi-step work still opens with a plan.
- **Delegation guard** (`hooks/guard.py`, `UserPromptSubmit` + `PreToolUse`, main thread only; subagent calls carry `agent_id` and pass through):
  - main-thread file edits are blocked, except under `~/.claude`;
  - work tools are blocked until an agent is launched for the prompt, with 3 checks allowed after each launch;
  - git/gh, agent output, MCP and other tools are never limited;
  - each prompt gets a one-line reminder.
  - Controls: `COCKPIT_GUARD`, `COCKPIT_START_BUDGET`, `COCKPIT_VERIFY_BUDGET`.
  - Fails open.
- **Hardened after an independent review:**
  - shell commands are split quote-aware: `git status && npm test` counts as work, while `git log | head`, `(cd x && git log)` and heredoc commit messages stay free;
  - a negation or "why" in the same sentence cancels an inline request;
  - the guard stands down when `prompt_id` is missing (older Claude Code);
  - a blocked call re-checks once after 150 ms in case an Agent call in the same message registers late;
  - the state file is never followed through a symlink;
  - relative paths resolve against the session cwd.
- **Explicit inline:** "do it yourself", "no agents needed" or "don't delegate" runs that request inline. Complaints and negations ("why no subagents?", "don't do it yourself") don't.
- **Evals:** `inline-trivial`/`inline-small-edit` became `route-short-lookup`/`route-short-edit` (they must delegate), and `inline-on-request` was added.

### Routing policy v1.4 (2026-11-07, not released separately)
Field report: a resumed session posted a plan but kept almost all work inline ("same working tree as the uncommitted port"), and still carried the v1.2 "inline by default" text in its history.
- **Agents do the work.** The orchestrator keeps only planning, briefs, conversation-bound decisions, git state changes, review and final verification. Investigation, edits, test triage, debugging and builds go to agents.
- **Same repo is not a reason to stay inline:** agents edit the shared tree one at a time, or in parallel with `isolation: "worktree"`. Only checkout, rebase, merge, stage and commit have a single owner.
- **Inline budget:** about 8 tool calls per request outside git and verification.
- **Every plan step names a tier**; `[inline]` only for git state, verification or a tiny step.
- **Supersedes earlier versions** explicitly, so a resumed conversation follows v1.4 only.

## [1.3.0] - 2026-11-07

### Routing policy v1.3
- **Plan first, then fan out.** Any multi-step task (including "continue from the handoff") opens with a short visible plan: workstreams, tier, what runs in parallel, what stays inline and why, and how it will be verified. Independent workstreams launch together as agents; only work coupled to state the orchestrator holds (sequential git on one working tree, decisions needing the conversation) stays inline. Progress is reported against the plan.
- **Checkpoint:** after ~15 inline tool calls the orchestrator re-plans and hands remaining independent pieces to agents.
- **One owner per working tree** for git operations.
- **Resumed sessions get policy updates:** the hook now also runs on resume and injects the policy if that conversation hasn't received the current version (it checks the transcript for this version's SessionStart delivery, so mere mentions don't count and nothing is duplicated). Hook input is read with a 2s timeout.

### ccx
- **Team view** (`v` in the dashboard, or `ccx team [name|pid]`): a live tree of the orchestrator and every agent it spawned (nested agents included), with model·effort, elapsed time, context, cost, tool calls and what each one is doing now. It adds a shared timeline that shows agents working in parallel, an activity feed across all agents, and per-agent details with the latest report. It reads transcripts Claude already writes, so watching costs zero tokens.
- **Context and cost per session**: the dashboard and `ccx ls` show each session's context fill (bar + %, against the model's real window) and its API-equivalent spend, including its agents. Alerts appear at 70% and 85% with the current cost per message; `ccx doctor` flags nearly-full sessions.
- **Sessions waiting for you** (permission prompts) jump to the top, blink, and get a header alert.
- Pricing table from platform.claude.com (cache writes 5m/1h, cache reads, fast mode, US-only multiplier, Haiku 5.5 long-prompt tier); usage is de-duplicated per API response (Claude logs one response as several records); `<synthetic>` placeholders are ignored.
- A conversation open in two terminals is counted once in spend totals and marked `⧉ same chat as <pid>`.

## [1.2.0] - 2026-11-07
Token and safety hardening from an independent product review.
- Agents can no longer spawn agents (`disallowedTools: Agent`, or an allowlist), so one task can't fan out into delegate → reviewer → reviewer chains.
- Every agent has `maxTurns` and a report word cap; architect is read-only by allowlist (no Bash/MCP).
- Routing policy: inline by default; delegate only for flooding output, >10 tool calls / 5+ files, parallel work, or a different tier. Architect runs when the user asks for a design decision. One escalation, then ask. At most one review pass.
- SessionStart hook no longer re-injects on resume/fork (`startup|clear|compact`).
- `ccx doctor` warns when `CLAUDE_CODE_EFFORT_LEVEL` / `CLAUDE_CODE_SUBAGENT_*` flatten every tier.
- Dashboard tier table is read from agent frontmatter; a test keeps the policy table in sync.
- Ghostty: `clipboard-read = ask` (programs can no longer read your clipboard silently).
- Evals: 51-file fixture, neutral prompts, editing allowed, negative cases (small edit stays inline and lands; migrations never reach architect).

## [1.1.0] - 2026-11-07
Versions 1.1.0 and 1.0.0 predate the public git history and have no tags.
- `ccx` opens an interactive dashboard: live sessions, RAM gauges, launch / worktree / close / clean-idle, tier panel.
- Live subagent status rows (tier glyph, model·effort, token sparkline, context %, elapsed, task label).
- Cockpit colour theme for Claude Code.
- "Second brain" routing: decide instead of asking, keep track of open loops, report outcomes plainly.
- Safety fixes from code review: strict `ccx gc` flags, pid-reuse detection via process start time, race-safe close, installer fixes for fresh Ghostty configs, symlinked dotfiles, and pipefail.

## [1.0.0] - 2026-11-07
- Five-tier orchestrator plugin, SessionStart routing policy, `ccx` CLI, Ghostty profile, installer, tests, routing evals.
