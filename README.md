# claude-cockpit

[![CI](https://github.com/KartikeyMish/claude-cockpit/actions/workflows/ci.yml/badge.svg)](https://github.com/KartikeyMish/claude-cockpit/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/KartikeyMish/claude-cockpit)](https://github.com/KartikeyMish/claude-cockpit/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

You ask for outcomes; Claude acts as your second brain. It owns the task, routes each piece to the right specialist, keeps track of what's in flight, and only asks when a decision is genuinely yours.

Three parts, each usable on its own:

1. **`orchestrator` plugin**: five agents with model and effort matched to task difficulty, and a routing policy that makes every interactive session delegate to them by default. A live agent panel shows each running agent's tier, model, effort, token burn and progress. Includes the matching **Cockpit** colour theme.
2. **`ccx`**: an interactive cockpit for all your Claude sessions. It lists live sessions, shows the RAM each holds, launches new sessions and worktrees, and cleans up the ones you've forgotten.
3. **Ghostty profile**: a low-overhead terminal tuned for Claude Code (keys, notifications, clipboard, splits, a drop-down terminal).

```
 ◆ claude-cockpit  v1.5.0                                                    17:31:20

  claude RAM ██▋·····················  1.77 GB of 16.00 GB   iTerm2 122 MB
  ⚑ 4 sessions idle ≥ 6h hold 588 MB. Press g to reclaim.

  SESSIONS 7   +1 background (a)
     NAME                       DIR                STATE     IDLE      RAM
 ▸ ◐ api@feat/status            ~/orders-svc       busy         -   593 MB  ████████
   ○ web@main                   ~/web              idle        5m   426 MB  █████▊··
   ◌ billing@main               ~/billing-svc      idle    19h19m   214 MB  ██▉·····

  AGENT TIERS   the orchestrator routes work to the lowest tier that fits
    ◇ scout           sonnet · medium  lookups, maps, docs · read-only
    ▪ routine-worker  opus · medium    clear specs: configs, docs, tests
    ◆ senior-engineer opus · high      interacting parts, migrations, security
    ✦ debugger        opus · xhigh     flaky bugs, races, incidents
    ▲ architect       opus · max       hard-to-undo decisions · no edits

  ⏎ claude here   v team   w worktree   x close   g clean idle   a background   t tiers   d doctor   q quit
```

## Install

You need [Claude Code](https://code.claude.com/docs/en/setup) 2.1.319 or newer and `python3` 3.9 or newer on your PATH (macOS ships one with the Command Line Tools; `ccx` tells you if yours is too old). `ccx` and the plugin run on macOS and Linux; the Homebrew cask and the Ghostty profile are aimed at macOS.

The plugin, `ccx` and the Ghostty profile ship together as one version. The plugin comes from the release channel `KartikeyMish/claude-cockpit#stable`, a branch that only moves when a release is published, so you only ever get released versions.

### Homebrew (macOS, recommended)

```bash
brew install --cask kartikeymish/tap/claude-cockpit
ccx setup
```

The fully qualified name taps [KartikeyMish/homebrew-tap](https://github.com/KartikeyMish/homebrew-tap) and trusts only this cask. It's a cask rather than a formula, so it installs even when your Command Line Tools are out of date.

`ccx setup` does the rest and is safe to run again at any time:
- adds the `claude-cockpit` marketplace from the `#stable` channel and installs (or updates) `orchestrator@claude-cockpit` at user scope;
- copies the Ghostty profile to `~/.config/ghostty/cockpit.conf` and includes it from your Ghostty config (your own settings below the include still win);
- backs up any file it changes as `*.cockpit-backup-<timestamp>`.

If you added the marketplace earlier from a git URL or a local path, `ccx setup` re-points that entry to `#stable` instead of removing it, because removing a marketplace also deletes its plugins' saved data. Options: `--no-plugin`, `--no-ghostty`, `--source <owner/repo#ref or path>`.

Then start a new Claude session, or run `/clear` in an open one. Check everything with `ccx doctor`.

### Claude Code plugin only

No `ccx`, no profile, just the agents and the routing policy:

```bash
claude plugin marketplace add KartikeyMish/claude-cockpit#stable
claude plugin install orchestrator@claude-cockpit
```

Inside Claude Code the same works as `/plugin marketplace add KartikeyMish/claude-cockpit#stable`, then `/plugin install orchestrator@claude-cockpit`.

### From source

```bash
git clone https://github.com/KartikeyMish/claude-cockpit ~/claude-cockpit && cd ~/claude-cockpit
./install.sh                 # plugin (from #stable) + ccx linked into ~/.local/bin
./install.sh --with-ghostty  # also Ghostty (via Homebrew if missing) + the cockpit profile
./install.sh --local         # development: use this checkout as the plugin source
```

`ccx` runs straight from the checkout, so check out a release tag (`git checkout v1.5.0`) to match the plugin from `#stable`, or use `--local` to run both from the checkout. `install.sh` runs `ccx setup` for the plugin and profile, so both install paths share one implementation.

### For a whole team

Commit this to a project's `.claude/settings.json`. Everyone who trusts the folder is offered the plugin, from the release channel:

```json
{
  "extraKnownMarketplaces": {
    "claude-cockpit": {
      "source": { "source": "github", "repo": "KartikeyMish/claude-cockpit", "ref": "stable" }
    }
  },
  "enabledPlugins": { "orchestrator@claude-cockpit": true }
}
```

## Update

| Installed with | Update with |
|---|---|
| Homebrew | `brew upgrade --cask claude-cockpit`, then `ccx setup` (run them as two commands; see [Troubleshooting](#troubleshooting)) |
| Plugin only | `claude plugin marketplace update claude-cockpit`, then `claude plugin update orchestrator@claude-cockpit` |
| Source | `git pull` (or check out the new tag), then `./install.sh` |

After updating the plugin, restart running sessions (`ccx doctor` shows which). Claude Code reads a plugin's hook list once, when the process starts; `/clear` and compaction keep the old one. For a local checkout install, hook scripts are live and only hooks.json changes need a restart; for marketplace installs, any plugin update needs a restart. To restart a session, `exit`, then run `claude --resume <id>` in that directory. The dashboard and `ccx ls` mark those sessions with `⟳`.

**Auto-update.** Claude Code doesn't auto-update third-party marketplaces by default. To turn it on, run `/plugin`, open **Marketplaces**, pick **claude-cockpit** and enable auto-update. Because `#stable` only moves at release, auto-update only ever brings released versions. `ccx` itself updates through Homebrew (or `git pull`).

## Uninstall

**Homebrew:**

```bash
claude plugin uninstall orchestrator@claude-cockpit
claude plugin marketplace remove claude-cockpit   # also deletes the plugin's saved data
brew uninstall --cask claude-cockpit              # add --zap to delete ~/.config/ghostty/cockpit.conf too
```

`ccx setup` added two lines starting with `# claude-cockpit profile` to `~/.config/ghostty/config`. The include is optional, so it is harmless once the profile is gone, but you can delete both lines.

**Source:** `./uninstall.sh` removes the plugin, the marketplace, the `ccx` link and the Ghostty include line. It keeps the Ghostty app and your own configs; backups are left as `*.cockpit-backup-*`.

## The tiers

| Agent | Model · effort | Use it for |
|---|---|---|
| `orchestrator:scout` | Sonnet · medium · **read-only** | Lookups: where/what/which, usages, docs, mapping an area |
| `orchestrator:routine-worker` | Opus · medium | Clear specs: configs, boilerplate, docs, renames, deps, writing/running tests |
| `orchestrator:senior-engineer` | Opus · high | Interacting parts: protocols, concurrency, migrations, security, cross-service work, artifacts |
| `orchestrator:debugger` | Opus · xhigh | Intermittent bugs, races, leaks, incidents, regressions with unclear cause |
| `orchestrator:architect` | Opus · max · **read-only** | Decisions that are hard to undo, when you ask for one: boundaries, data models, API shape, vendors, migration plans |

The main session stays the **orchestrator** and your second brain: it plans, briefs, owns git state, reviews and verifies. **Agents do the work, short requests included.** A one-file lookup goes to scout and a one-line edit goes to routine-worker. Anything multi-step opens with a short **visible plan** where every step names a tier. Independent steps launch in parallel, and edits in the same repo still go to agents (one at a time, or isolated worktrees).

**Split by area (policy v1.6).** Work that spans 2+ repos, services or independent areas gets one agent per area, all launched in one message. Parallel edits run in `isolation: "worktree"` or touch disjoint files in the shared tree. One agent stays right when the parts need each other's results, and small parts (≤3 tool calls) fold into an agent that's launching anyway. When a step has to wait, the plan says why: a data dependency, a shared browser, one working tree, one git owner. Broad unknowns get 2–4 parallel scouts by area, but only when the search space really splits. While agents run, the orchestrator doesn't poll or narrate. It waits for the hand-backs and reacts once per batch.

**Agent reports.** Each tier ends with a report under its word cap and sends no interim messages unless it's blocked. Two optional lines tell the orchestrator what to do next:
- `ESCALATE: <tier>` re-routes the task upward, with the findings so far.
- `SPLIT: <area B> | <area C> …` is for an agent that finds its task spans independent areas. It finishes the first coherent area and lists the rest. The orchestrator then launches those areas in parallel, each with the findings so far.

Use the tiers rather than the built-in `Explore`, `general-purpose` or `fork` agents. Those skip the tiers and run on the main session's model: a read-only Explore run on Opus costs about twice what scout does. The exception is when no tier fits, such as a specialist reviewer you installed.

### Delegation guard

The policy asks; the guard enforces it. The plugin's `UserPromptSubmit` and `PreToolUse` hooks act on the **main thread only**. Calls made inside an agent carry an `agent_id` and pass through untouched.

| In the main thread | What happens |
|---|---|
| Edit / Write / NotebookEdit | Blocked, with a note to brief routine-worker or senior-engineer. Files under `~/.claude` (memory, plans, settings) are exempt. |
| Read / Grep / Glob / Bash / WebFetch / WebSearch | Blocked until an agent has been launched for the request. After each launch, 3 calls are allowed for checking the result. Builds, tests, deploys and remote commands (`make`, `npm`, `pytest`, `docker`, `aws`, `ssh`, …, also behind `env`, `time`, `timeout` or `nice`) are work, not checks, and are always blocked. |
| Agent / Task with `subagent_type` `Explore` or `general-purpose`, or with none (that runs general-purpose) | The first one per request is blocked, along with the rest of its parallel batch (2 s), with the tier to use instead: scout for read-only discovery, or routine-worker / senior-engineer / debugger for work. It doesn't count as delegating. A later retry in the same request goes through, for when no tier fits. Tier agents, `fork` (it carries the full context, which is sometimes the point) and agents you installed are never touched. |
| Shell commands made only of `git`/`gh` and `make release` / `make release-finish` (state you own; plus `cd`, and pipes into `head`, `grep` and the like), agent output files, MCP tools, questions to you | Never limited |
| Each prompt you send | Gets a one-line routing reminder (about 40 tokens) |

When you want it done in the main thread, say so: *"do it yourself"*, *"no agents needed"*, *"don't delegate"*. That request then runs inline. Complaints like "why were no agents spun up?" don't count as asking for inline work.

| Variable | Default | Effect |
|---|---|---|
| `COCKPIT_GUARD=off` | on | Keep the policy and reminder, drop enforcement |
| `COCKPIT_START_BUDGET` | `0` | Inline calls allowed before the first agent launch |
| `COCKPIT_VERIFY_BUDGET` | `3` | Inline calls allowed after each agent launch |
| `COCKPIT_ROUTING=off` | auto | Turns off policy, reminder and guard |

The guard fails open. If Python is missing or anything errors, the tool call goes through. It needs a Claude Code version that stamps hook calls with `prompt_id` and `agent_id` (2.1.319 does). On older versions it stands down rather than risk blocking an agent.

**Built-in cost guards:** agents can't spawn agents; every agent has a turn limit and a report word cap; scout and architect are read-only by allowlist; the policy is injected on startup, `/clear` and compaction, and on resume only when that conversation doesn't have the current version yet; headless and SDK sessions skip it.

**Fixed cost:** about 1.5k tokens per session (policy plus the five agent descriptions), plus about 40 tokens per prompt for the reminder. That's cached after the first request. The real cost driver is delegation itself: each agent starts a fresh context, which is why short requests cost a little more than they would inline. That's the trade for quality and a main context that stays small.

## Daily use

Just type **`ccx`**. The cockpit opens; press `⏎` to start Claude in the current folder, or `w` to start it in a fresh git worktree. Everything else is one key, shown at the bottom of the screen.

| Command | What it does |
|---|---|
| `ccx` | Open the interactive cockpit (falls back to `ccx ls` when piped) |
| `ccx "fix the flaky queue test"` | Start Claude here with that prompt, named `<repo>@<branch>` |
| `ccx wt fix-login` | Start Claude in a new git worktree, for parallel work without file clashes |
| `ccx ls` | Running sessions: state, idle time, RAM, context %, API-equivalent cost, saved by delegating |
| `ccx savings` | What delegation saved over the last 7 days (`--days N`, `--json`); see [Measuring savings](#measuring-savings) |
| `ccx team [name]` | Live team view of the agents inside a session |
| `ccx mem` | RAM by terminal, interactive sessions, SDK/background sessions |
| `ccx gc` | Close sessions idle ≥ 6h after confirming (`--idle 2`, `--dry-run`, `--yes`). They stay resumable with `claude --resume` |
| `ccx doctor` | Checks CLI, plugin (version and source in effect), agents, routing/guard switches, guard data dir, terminal, Ghostty config, idle sessions, and sessions still running old plugin hooks (`⟳`) with the exact restart command (`--json`; exits 1 when something needs action) |

**Team view.** Select a session and press `v` (or run `ccx team`) to watch its agents work together, live:

```
 ◆ claude-cockpit  › team · orders-svc@feat/status                               17:58:02
  ◐ orchestrator   opus·xhigh   3 agents · 2 running   context 421k/1.0M 42%   ≈$16.99 session · $0.19/turn

  AGENTS                          TASK                MODEL       TIME    CTX    COST TOOLS  NOW
  ├─ ⠼ ◆ senior-engineer          Add status column   opus·high   4:12    96k   $1.84    57  Bash npm test
  │  └─ ⠼ ◇ scout                 Find status readers sonnet·med  0:40    29k   $0.06    11  Grep status src/
  └─ ✓ ✦ debugger                 Duplicate jobs      opus·xhigh  6:03   140k   $3.10    88  done

  TIMELINE   last 7m, all agents on one clock
    senior-engineer ███████████████████████████████▶
    scout           ··········█████████▶
    debugger        ████████████████████····
  ACTIVITY
    17:57:58  ◇ scout          Grep status src/
    17:57:57  ◆ senior-engineer Bash npm test
```

`⏎` opens an agent's recent tool calls and latest report. It reads transcripts Claude already writes, so watching costs zero tokens.

**Context and cost.** Every session row shows its context fill against the model's real window, plus its spend so far at API prices, including its agents. At 70% you get a heads-up; at 85% a red alert tells you what each message now costs and suggests `/compact` or a fresh session. Sessions waiting on a permission prompt jump to the top and blink. On a subscription you aren't billed per token, but the API-equivalent figure is the honest way to compare sessions, agents and habits.

`ccx` passes any other arguments straight to `claude` (`ccx --model sonnet`, `ccx -c`). Use `ccx run …` if your prompt starts with a ccx word such as `ls`.

**Inside Claude:** while agents work, the panel under the prompt shows rows like
`⠼ ◆ senior-engineer  opus·high  ▃▅▅█▆ 18.2k  9% ctx  1:42  Writing migration 002_add_status.sql`.
For matching colours everywhere, run `/theme` and pick **Cockpit**.

**Ghostty keys:** `Cmd+D` / `Cmd+Shift+D` split · `Cmd+Opt+arrows` move between splits · `Cmd+Shift+Enter` zoom a split · `Ctrl+`` ` drop-down terminal from anywhere · `Shift+Enter` newline in Claude · Option shortcuts (`Opt+P` model picker) work.

### Steering the routing

- Say it directly: "do this yourself", "use the debugger", "no agents". Explicit instructions always win.
- `COCKPIT_ROUTING=off ccx`: one session without the policy. `export COCKPIT_ROUTING=off` turns it off everywhere. For one project, commit `{"env": {"COCKPIT_ROUTING": "off"}}` to its `.claude/settings.json`.
- `ccx doctor` warns if `CLAUDE_CODE_EFFORT_LEVEL` or `CLAUDE_CODE_SUBAGENT_MODEL`/`_EFFORT` is set, since those silently flatten every tier to one model or effort.
- SDK and headless sessions (IDE bridges, memory observers, scripts) are skipped by default so they don't pay the tokens. Use `COCKPIT_ROUTING=on` to include them.

### Cost

Effort scales token use. `scout` is the cheapest way to gather context. `architect` at max effort is the most expensive call in the set, so the orchestrator only runs it when you ask for a design decision or plan; otherwise it suggests it in one line. The main session does all the routing, so it doesn't need max effort itself: `high` is a good default for it.

### Measuring savings

Delegating isn't free: every agent starts a fresh context, and its brief and report pass through the main session. So the cockpit estimates, from transcripts already on disk, whether each session's agents saved money compared with the main session doing the same work itself.

- **`SAVED` column** (dashboard and `ccx ls`): the net estimate for that session so far.
- **Team view:** `saved ≈$X (Y%) · mix · ctx · overhead −$… · Nk kept out of main`, plus a warning line when the inline version would have overflowed the context window.
- **`ccx savings [--days N] [--json]`:** totals across every session from the last N days (default 7). It prints two lines, one with and one without the sessions that would have compacted, and then the top sessions.

| Marker | Meaning |
|---|---|
| `+$1.20` / `−$0.40` | Delegation saved / cost that much (it can be negative, and is reported as such) |
| `~` | Partial: an unknown model or a transcript too large to read whole; those runs are left out |
| `!` | Done inline, main would have outgrown its context window; treat the saving as an upper bound |
| `-` | No agents in this session yet |

**The counterfactual** is the same work done inline by the main session's model. For each agent run:

- **Model mix:** the agent's own API calls, token for token, repriced at the main model's rates. Positive when a cheaper tier did the work (a Sonnet scout under an Opus main).
- **Context:** this has two parts. *Work in context*: inline, every one of those calls would also have re-read the main session's whole context, not just the agent's small starting one. *Carry*: what the agent read and wrote would then stay in main, where every later main call re-reads it. Delegation leaves only the report behind. Carry stops at main's next compaction.
- **Delegation overhead** (subtracted): writing the brief; the brief, launch notice and report entering main's cache; and any main turn that exists only to react to an agent (waking up for a report, or acknowledging a background launch).

Sizes that aren't logged as tokens (brief, launch notice, report) are estimated at 2.5 characters per token. On real hand-backs the main prompt grew by one token per about 2.75 characters (the median), so the common chars/4 rule would understate them. Nested agents count inside the run that launched them.

**Choices that round against delegation:** an agent's first-call cache write is a delegation cost (inline, that context would already be cached); repriced cache writes keep the agent's 5-minute rate although the main session writes at the dearer 1-hour rate; carry is priced at the cache-read rate even where main's cache would have expired; a report that never reached main means no carry; and a nested agent's work counts only if its report to its parent is found.

**Limitations:**
- Compaction is flagged, not priced. In `!` sessions the inline version would have compacted and shed context, so their context saving is an upper bound. `ccx savings` shows totals without them for that reason.
- Agents you stop still count their work.
- `SendMessage` follow-ups from main to a running agent aren't counted.
- Transcripts from Claude Code 2.1.281–2.1.300 sometimes logged only a streaming snapshot of a response's output tokens (a few tokens instead of the final count). Where a later record has the final count, it is used. Where none does, that output stays undercounted, in both the actual and the estimated costs.
- Everything is an API-equivalent estimate from logs, not a bill.

**Parallelism.** `ccx savings` also prints a Parallelism block (and a `parallelism` object in `--json`), and the team view adds a line such as `parallel: max 3 · 41% overlap · 12m saved`. The definitions:

- **Agent activity:** an agent's transcript timestamps, split into intervals wherever it logged nothing for more than 15 minutes, so an agent resumed later isn't counted as busy while it waited. A still-running agent's last interval ends at its latest record. Its *duration* is the sum of its intervals.
- **Saved by overlap** = sum of agent durations − the union of their intervals: the wall-clock time parallel agents already saved. *Overlap share* = that saving ÷ the sum of durations. *Max concurrent* = the most agents active at one instant (intervals that merely touch don't count as overlapping). All three count top-level agents only: an agent launched by another agent runs while its parent waits, so counting both would invent overlap.
- **Request:** one prompt you typed, and everything up to the next one. *Agents per request* counts main's agent launches in requests that delegated at all. A *multi-step request* has at least 20 tool calls, main's and its agents' together; the block counts those that one agent did alone.
- **Sequential launches that looked independent (heuristic):** in one request, agent B was launched only after agent A had finished, B's brief has no code-like token (a path, identifier or id) that first appeared in A's report, and B touched no file A edited. Launched together, such a chain would have finished in the time of its longest agent; the estimate is the sum of durations minus the longest. On a hand-checked sample about 7 in 10 flagged pairs were truly independent. A dependency that leaves no shared path or id, a shared single resource (one browser, one working tree) and anything that isn't a consecutive pair are invisible to it.
- **Main calls over the guard budget (approximate):** counted only in sessions where the routing policy was injected. These are main-thread calls the guard would deny under its default rules: file edits outside `~/.claude`, and Read/Grep/Glob/Bash/WebFetch/WebSearch beyond none before the first agent launch of a prompt and 3 after each launch. Heavy shell commands (builds, tests, deploys, remote shells such as `make`, `npm`, `pytest`, `docker`, `ssh`) count whatever budget is left. Commands made only of git/gh and `make release`/`release-finish`, and reads of `~/.claude` or Claude's temp folder are free. The count mirrors those rules from the transcript. Like the guard, it lets a work call through when an Agent call follows it in the same response (the guard re-checks such a denial after 150 ms), and it resolves relative paths against the session's working directory. Where it still differs from the guard: it doesn't resolve symlinks, so a link into `~/.claude` or the temp folder counts as work; it takes `TMPDIR`/`CLAUDE_CODE_TMPDIR` from ccx's own environment rather than Claude's; and it can't see `COCKPIT_*` overrides or whether the guard was installed and on. The policy counts as injected only when its SessionStart hook output is in the transcript, not when a tool merely read the policy file. Read the count as "work the policy says belongs to agents", not as guard failures.
- **`~` partial sessions:** a transcript over 64 MB is read from its end, so its early requests, launches and activity are missing. Those sessions show `~` (in the team view, `parallel~:`), and `ccx savings` leaves them out of the parallelism totals and says how many it left out.

On an 80-column terminal the session table drops the `DIR` column to make room for `SAVED`; it returns at 87 columns.

## Rolling out to a team

1. Install from the release channel (Homebrew, or the project settings above), so everyone runs the same released version and updates arrive through `ccx setup`, `/plugin update` or auto-update.
2. Measure before and after rather than assuming a win: compare `/usage`, or OpenTelemetry cost and token metrics, alongside the outcomes people care about, such as PRs merged and rework.
3. Run `make eval` before every change to the agents or policy. It checks that each kind of task reaches the right tier, that short lookups and edits are delegated too, that two independent services get one agent each, launched together and without built-in agents, that "do it yourself" stays inline, and that migrations never reach the max-effort architect.
4. Keep opt-outs easy (above) and listen for "it delegated when it shouldn't have". `COCKPIT_START_BUDGET=1` gives the orchestrator one look before it delegates.

## Why Ghostty, and where the memory goes

Measured on a working Mac (Nov 2026): the terminal app is the small part. iTerm2 used about 100 MB. Each Claude session used 150–480 MB, about 2 GB across 7 sessions, and 4 of those had been idle for 17–19 hours. So the biggest win is `ccx gc`, not the terminal. Ghostty is still the best fit:

- It's native and GPU-rendered, and community measurements put it among the lightest terminals. Only Alacritty is consistently lower.
- It needs **zero setup** for Claude Code: Shift+Enter works natively, and Claude Code sends desktop notifications to Ghostty by default. Alacritty has neither tabs nor splits, so you would add tmux, which costs memory and needs passthrough config.

The profile avoids transparency, blur, shaders and cursor blink, and keeps the default 10 MB scrollback.

## Develop & verify

```bash
./install.sh --local   # plugin and ccx both run from this checkout
make test              # stdlib unittest suite: agents, guards, hook, status line, pricing, context, team view, dashboard, setup, release tooling + real pty runs
make compile           # byte-compile everything (PYTHON=/usr/bin/python3 make compile checks the 3.9 floor)
make validate          # Claude's validator on plugin + marketplace
make lint              # shellcheck + ruff, if installed
make eval              # routing evals (claude plugin eval), capped spend; use --ablation with-without to compare against no plugin
```

With `--local`, the plugin loads in place from the checkout: edits take effect at the next session start or `/reload-plugins`, with no version bump. Users only see a change once it is released, because Claude Code caches plugins per version.

Evals live in `plugins/orchestrator/evals/`: one case per tier on a 51-file fixture service, plus negative cases. A one-file question and a one-line edit must be delegated (and the edit must land). "Do it yourself" must stay inline, and a migration must never reach architect.

CI (GitHub Actions) runs the tests on macOS with Apple's `/usr/bin/python3` and the latest Python, and on Linux with Python 3.9 and the latest, plus shellcheck and `claude plugin validate`.

## Releasing

Don't bump versions by hand; the release does it. Add what changed under `## [Unreleased]` in `CHANGELOG.md` as you go, then, from an up-to-date `main` with a clean tree:

```bash
make release VERSION=1.6.0 DRY_RUN=1   # rehearse: runs every check and the tests, changes nothing
make release VERSION=1.6.0
```

`make release` (`scripts/release.py`, stdlib only):
1. checks you are on `main`, clean and in sync with `origin/main`, that the version is higher than the current one, that `[Unreleased]` has entries, that the tag is free, that `gh` is logged in and that the tap checkout is clean;
2. bumps the version in `plugin.json`, `marketplace.json` and `lib/cockpit/__init__.py` (and the routing-policy title for a new minor or major, so resumed sessions pick up the new policy), and turns `[Unreleased]` into `[1.6.0] - <today>`;
3. runs `make test` and `claude plugin validate`, and puts the files back if anything fails;
4. commits `chore: release v1.6.0` and makes an annotated tag;
5. asks, then pushes `main` and the tag; the tag starts the Release workflow, which builds `claude-cockpit-1.6.0.tar.gz` and its `.sha256` and publishes the GitHub Release with the CHANGELOG notes;
6. waits for that workflow and checks the Release and its assets;
7. fast-forwards `stable` to the tag, which is when plugin users get the release;
8. writes the new `version` and `sha256` into the Homebrew cask (checked against the tarball), then commits and pushes the tap. The tap checkout is `~/homebrew-tap` unless you set `TAP_DIR`.

Options (combine as needed):
- `DRY_RUN=1` prints the plan and changes nothing.
- `YES=1` pushes without asking; required when not running in a terminal.
- `NO_TAP=1` skips the Homebrew tap update.

If it stops before the push, the bumped files are put back, or, once committed, it prints the exact undo command. If it stops after the tag is pushed, `make release-finish VERSION=1.6.0` does what is left (waits for or checks the Release, fast-forwards `stable`, updates the tap) and skips steps already done, so it is safe to re-run.

`python3 scripts/release.py notes 1.6.0` prints one version's notes.

## Troubleshooting

- **Agents missing in `/agents`:** run `ccx doctor`, then `claude plugin list`. Start a new session after installing.
- **Inside tmux:** add `set -g allow-passthrough on`, `set -g focus-events on`, `set -s extended-keys on` and `set -as terminal-features 'xterm*:extkeys'` to `~/.tmux.conf` for Shift+Enter and notifications.
- **No notifications:** allow Ghostty under macOS System Settings → Notifications.
- **Drop-down terminal does nothing:** grant Ghostty Accessibility permission (System Settings → Privacy & Security).
- **`ccx` says it needs Python 3.9:** put a newer `python3` first on PATH: `xcode-select --install` provides one on macOS, or `brew install python`.
- **`brew upgrade` ends with "Your Command Line Tools are too outdated":** after upgrading casks, Homebrew may also try to rebuild `pkgconf` from source ("due to macOS version mismatch"), and that part needs current Command Line Tools. claude-cockpit itself is a cask and has already upgraded (check with `ccx --version`), but `brew upgrade` exits with an error, so run `ccx setup` as its own command.

## Layout

```
.claude-plugin/marketplace.json        marketplace (this repo)
plugins/orchestrator/
  .claude-plugin/plugin.json, icon.png
  agents/                              the five tiers
  hooks/hooks.json, session-start.sh   SessionStart → inject routing-policy.md
  hooks/guard.py                       delegation guard (UserPromptSubmit + PreToolUse)
  evals/                               routing evals
  settings.json, scripts/              live subagent status rows
  themes/cockpit.json                  Claude Code colour theme
bin/ccx, lib/cockpit/                  cockpit: core rules, cli, curses dashboard, `ccx setup` (installer.py), `ccx doctor` (doctor.py, doctor_cmd.py); Python 3.9+, stdlib
terminal/ghostty/cockpit.conf          Ghostty profile
scripts/release.py                     `make release`
.github/workflows/                     CI and the Release workflow
install.sh, uninstall.sh, Makefile, tests/, CHANGELOG.md
```

## License

[MIT](LICENSE) © 2026 Kartikey Mishra
