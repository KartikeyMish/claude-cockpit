# claude-cockpit

You ask for outcomes; Claude acts as your second brain. It owns the task, routes each piece to the right specialist, keeps track of what's in flight, and only asks when a decision is genuinely yours.

Three parts, each usable on its own:

1. **`orchestrator` plugin**: five agents with model and effort matched to task difficulty, and a routing policy that makes every interactive session delegate to them by default. A live agent panel shows each running agent's tier, model, effort, token burn and progress. Includes the matching **Cockpit** colour theme.
2. **`ccx`**: an interactive cockpit for all your Claude sessions. It lists live sessions, shows the RAM each holds, launches new sessions and worktrees, and cleans up the ones you've forgotten.
3. **Ghostty profile**: a low-overhead terminal tuned for Claude Code (keys, notifications, clipboard, splits, a drop-down terminal).

```
 ◆ claude-cockpit  v1.1.0                                                    17:31:20

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

## The tiers

| Agent | Model · effort | Use it for |
|---|---|---|
| `orchestrator:scout` | Sonnet · medium · **read-only** | Lookups: where/what/which, usages, docs, mapping an area |
| `orchestrator:routine-worker` | Opus · medium | Clear specs: configs, boilerplate, docs, renames, deps, writing/running tests |
| `orchestrator:senior-engineer` | Opus · high | Interacting parts: protocols, concurrency, migrations, security, cross-service work, artifacts |
| `orchestrator:debugger` | Opus · xhigh | Intermittent bugs, races, leaks, incidents, regressions with unclear cause |
| `orchestrator:architect` | Opus · max · **read-only** | Decisions that are hard to undo, when you ask for one: boundaries, data models, API shape, vendors, migration plans |

The main session stays the **orchestrator** and your second brain. Tiny things (one known file, a few tool calls) it just does. Anything multi-step opens with a short **visible plan**: workstreams, the tier for each, what runs in parallel, what stays inline and why, and how it will be verified. Then independent workstreams fan out to agents at once. Work tied to state it holds (sequential git on one working tree) stays with one owner, and after ~15 inline tool calls it re-plans. It picks the lowest tier that fits, escalates once and then asks you, allows at most one review pass, and verifies agent output before reporting. It decides instead of asking, keeps track of open loops, and reports outcomes in plain language. The full policy is in [`plugins/orchestrator/hooks/routing-policy.md`](plugins/orchestrator/hooks/routing-policy.md).

**Built-in cost guards:** agents can't spawn agents; every agent has a turn limit and a report word cap; scout and architect are read-only by allowlist; the policy is injected on startup, `/clear` and compaction, and on resume only when that conversation doesn't have the current version yet; headless and SDK sessions skip it.

**Fixed cost:** about 1.5k tokens per session (policy plus the five agent descriptions). That's cached after the first request. The real cost driver is delegation itself: each agent starts a fresh context, which is why inline is the default.

## Install

```bash
git clone <this repo> ~/claude-cockpit && cd ~/claude-cockpit
./install.sh                 # plugin + ccx
./install.sh --with-ghostty  # also Ghostty + the cockpit profile (macOS/Homebrew)
```

Then start a new Claude session, or run `/clear` in an open one. Check everything with `ccx doctor`.

**Plugin only** (no clone), from inside Claude Code:

```
/plugin marketplace add <git URL of this repo>
/plugin install orchestrator@claude-cockpit
```

**Whole team, automatically.** Commit this to a project's `.claude/settings.json`; teammates are offered the plugin when they trust the folder:

```json
{
  "extraKnownMarketplaces": {
    "claude-cockpit": { "source": { "source": "git", "url": "<git URL of this repo>" } }
  },
  "enabledPlugins": { "orchestrator@claude-cockpit": true }
}
```

## Daily use

Just type **`ccx`**. The cockpit opens; press `⏎` to start Claude in the current folder, or `w` to start it in a fresh git worktree. Everything else is one key, shown at the bottom of the screen.

| Command | What it does |
|---|---|
| `ccx` | Open the interactive cockpit (falls back to `ccx ls` when piped) |
| `ccx "fix the flaky queue test"` | Start Claude here with that prompt, named `<repo>@<branch>` |
| `ccx wt fix-login` | Start Claude in a new git worktree, for parallel work without file clashes |
| `ccx ls` | Running sessions: state, idle time, RAM, context %, API-equivalent cost |
| `ccx team [name]` | Live team view of the agents inside a session |
| `ccx mem` | RAM by terminal, interactive sessions, SDK/background sessions |
| `ccx gc` | Close sessions idle ≥ 6h after confirming (`--idle 2`, `--dry-run`, `--yes`). They stay resumable with `claude --resume` |
| `ccx doctor` | Checks CLI, plugin, agents, routing, terminal, Ghostty config, idle sessions |

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

## Rolling out to a team

Treat it as a pilot, not a mandate:
1. Host the repo (git) and install from the remote so everyone gets updates through `/plugin update`.
2. Pilot with a handful of developers for two weeks alongside a control group. Compare `/usage`, or OpenTelemetry cost and token metrics, plus the outcomes people care about: PRs merged and rework.
3. Run `make eval` before every change to the agents or policy. It checks that each kind of task reaches the right tier, that small edits stay inline, and that migrations never reach the max-effort architect.
4. Keep opt-outs easy (above) and listen for "it delegated when it shouldn't have".

## Why Ghostty, and where the memory goes

Measured on a working Mac (Nov 2026): the terminal app is the small part. iTerm2 used about 100 MB. Each Claude session used 150–480 MB, about 2 GB across 7 sessions, and 4 of those had been idle for 17–19 hours. So the biggest win is `ccx gc`, not the terminal. Ghostty is still the best fit:

- It's native and GPU-rendered, and community measurements put it among the lightest terminals. Only Alacritty is consistently lower.
- It needs **zero setup** for Claude Code: Shift+Enter works natively, and Claude Code sends desktop notifications to Ghostty by default. Alacritty has neither tabs nor splits, so you would add tmux, which costs memory and needs passthrough config.

The profile avoids transparency, blur, shaders and cursor blink, and keeps the default 10 MB scrollback.

## Develop & verify

```bash
make test       # 103 stdlib tests: agents, guards, hook, status line, pricing, context, team view, dashboard + real pty runs
make validate   # Claude's validator on plugin + marketplace
make lint       # shellcheck + ruff, if installed
make eval       # routing evals (claude plugin eval), capped spend; use --ablation with-without to compare against no plugin
```

Evals live in `plugins/orchestrator/evals/`: one case per tier on a 51-file fixture service, plus negative cases. A trivial question and a small edit must stay inline, with the edit actually landing, and a migration must never reach architect.

When you change a tier, bump `version` in both `plugins/orchestrator/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (a test enforces they match), then run `./install.sh` or `/plugin update`.

## Troubleshooting

- **Agents missing in `/agents`:** run `ccx doctor`, then `claude plugin list`. Start a new session after installing.
- **Inside tmux:** add `set -g allow-passthrough on`, `set -g focus-events on`, `set -s extended-keys on` and `set -as terminal-features 'xterm*:extkeys'` to `~/.tmux.conf` for Shift+Enter and notifications.
- **No notifications:** allow Ghostty under macOS System Settings → Notifications.
- **Drop-down terminal does nothing:** grant Ghostty Accessibility permission (System Settings → Privacy & Security).

## Uninstall

`./uninstall.sh` removes the plugin, the marketplace, the `ccx` link and the Ghostty include line. It keeps the Ghostty app and your own configs; backups are left as `*.cockpit-backup-*`.

## Layout

```
.claude-plugin/marketplace.json        marketplace (this repo)
plugins/orchestrator/
  .claude-plugin/plugin.json, icon.png
  agents/                              the five tiers
  hooks/hooks.json, session-start.sh   SessionStart → inject routing-policy.md
  evals/                               routing evals
  settings.json, scripts/              live subagent status rows
  themes/cockpit.json                  Claude Code colour theme
bin/ccx, lib/cockpit/                  cockpit: core rules, cli, curses dashboard (Python 3.9+, stdlib)
terminal/ghostty/cockpit.conf          Ghostty profile
install.sh, uninstall.sh, Makefile, tests/
```
