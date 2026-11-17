# Changelog

## 1.2.0 (2026-11-17)

### Chief: dashboard fixes

- Fixed: tag paths with spaces (#9).
- Fixed: an unknown `?run=` link shows a "Run not found" state (#1). One shared dashboard for every repo is kept.
- Fixed: polling never rebuilds the score while you scroll, drag or select (#3).
- Fixed: gauges and metrics sit in one row (#4).
- Cards are at most 360px wide and never squeezed below 260px; the programme scrolls sideways on small screens. The page is no longer capped at 1600px, and the staff, measures and sparkline stay crisp.
- Session badge and sibling runs, chat title, resume command, VS Code link, and read-only transcripts (#6, #7).
- Context size is shown per Claude agent; Codex agents show as not reported (#8).
- Every dashboard request checks its Host and header.
- `DASHBOARD_VERSION` is 4: a running dashboard from an earlier version is replaced.
- No model attribution on commits and PRs: a silent SessionStart hook sets Claude Code's `attribution` to empty strings in the user settings once per plugin version, only when the key is absent (backup at `settings.json.maestro-bak`; `MAESTRO_KEEP_ATTRIBUTION=1` opts out), and `git-check.py` warns about AI trailers in commits since the base.
- The stage is redesigned: each chief run is its own small stage with a proscenium and curtains, and up to 10 playing robot agents grouped by role. The chief conducts while agents work, rests when the run is idle and bows when it is done; the stage turns to face the audience on joins, finishes and phase changes.
- A prompt-cache badge on the run card and the score header shows whether the chief session's cache is hot (with the minutes left) or expired.
- On a wide screen a busy programme column lays its cards two or three across.

## 1.1.0 (2026-11-14)

### Maestro: one plugin for Claude Code and Codex

- The repo is now `vgozalov/maestro` (it was `vgozalov/skills`). GitHub redirects the old name, and `install.sh` still treats a `vgozalov/skills` line in a manifest as this repo, so existing manifests keep working.

- The repo now installs as the `maestro` plugin, with updates delivered by each tool's own marketplace. Claude Code: `/plugin marketplace add vgozalov/maestro`, then `/plugin install maestro@maestro` (turn on auto-update for the marketplace in `/plugin` → Marketplaces). Codex: `codex plugin marketplace add vgozalov/maestro`, then `codex plugin add maestro@maestro`.
- Plugin skills are namespaced: `/maestro:chief`, `maestro:parallel-build`, and the agent `maestro:chief`.
- In Claude Code the plugin registers the usage hook itself (`hooks/claude-code.json`), so `configure-hooks.py` is needed only for the optional auto-save patch. The plugin's hook runs only while a chief run is active (`usage-before-spawn.py --during-chief-run`), so ordinary sessions never wait on a usage read. `check-account-usage.py` reports `spawn_hook: registered` when the plugin carries it.
- Codex gets the skills and no hooks, which keeps the plugin eligible for OpenAI's directory.
- `npx skills add` and `install.sh` keep working; install one way, not both, or every skill appears twice.
- Tests: `tests/plugin.test.sh` keeps both manifests, both marketplaces and the newest CHANGELOG entry on one name and one version.

### Chief: one dashboard for every repo and worktree

- Fixed: chiefs running at once in two repos, or in several worktrees of one repo, could not share the dashboard. Runs were listed only from the checkout `serve` started in, and a second `serve` failed on port 8765 that was already taken, so the second run's `?run=` link opened a dashboard that did not know it.
- Each run now leaves a pointer in `~/.chief/runs/` (`CHIEF_HOME` overrides), and the dashboard lists every registered run on the machine, with a filter per repo, the repo on each card, and git warnings per checkout. Plan, progress and handoff files open from the run's own checkout.
- The chief starts the dashboard itself: `chief-run.py start` and every phase change except `done` start it in the background (in its own session, so it outlives the chief's shell) when none answers, and join it when one does. A chief dashboard older than the script, including the single-repo one from 1.0, is stopped and replaced; anything else on the port is left alone and reported. The output carries `dashboard` (this run's link) and `dashboard_server` (`running`, `started`, `replaced`, `failed` or `off`).
- A dashboard started this way exits after 6 hours with no page open (`serve --idle-hours`); the next run starts it again. Its log is `~/.chief/dashboard.log`. `CHIEF_DASHBOARD_PORT` sets the port, `CHIEF_DASHBOARD=off` turns the automatic start off, and `chief-run.py dashboard` starts or joins it by hand.
- `serve` started while a current dashboard is running prints its address and exits. `serve`, `board` and `dashboard` work outside a git repo.
- Run links use a key that is unique across repos (`<run id>@<repo hash>`, printed by `start` as `dashboard`); a bare run id still opens when only one repo has it.
- Runs that ended more than 14 days ago move to the archived view (`--days`, 0 turns that off). Pointers to removed worktrees are dropped. `chief-run.py register` adds a checkout's earlier runs; `phase` registers its run too.
- The board reads each main-session transcript once per change instead of on every poll for every run.

### Chief: the Concert Hall dashboard

- The dashboard is redrawn in the README's style. The stage at the top shows every live run as an ensemble: its chief as the conductor on a podium and each working agent as a brass robot under its own lamp, with the agent's latest tool call beneath it. The instrument is the role (violin research, clarinet plan and fix, cello implement, trumpet review, double bass git); brass is Claude, verdigris is ChatGPT. Working robots sway and send up notes; anyone with reduced motion turned on sees them still. Ensembles wrap onto a second row rather than shrinking past readability.
- Run cards are short: the title, a seven-bar staff for the phases with the plan's tasks as notes on it, the number of players, a count per role and the Claude/ChatGPT cost split. The agents themselves are in the run's score, which opens on click: time per phase as measures, plan tasks by wave with their area (from the top folders in each task's Owns paths), agents by section, and the selected agent's recent tool calls and reply.
- The header shows the spend of all runs, the last 24 hours with a sparkline, the burn rate over the last hour, and each account's 5-hour and weekly use as a needle dial with the 60% switch line.
- Everything is read from files already on disk, so watching the board costs no tokens: tool calls come from Claude Code's transcripts and from each Codex job's `events.jsonl`, task status from the plan, the build ledger's `Task N:` lines and the live agents, and an agent counts as blocked or failed from the first word of its reply (`DONE`, `BLOCKED`, `FAILED`, also after `Status:`). Roles match by prefix, so `implementer`, `reviewer`, `fixer` and `plan-review` land in their sections.
- Archive: finished runs can be archived one at a time from their score, all at once from the Done column, or with `chief-run.py archive <run>... | --done [--restore]`. Archived runs move to their own view and can be restored; their spend still counts in the all-runs total. An unfinished run with no agent working and no phase change for two hours shows as quiet, leaves the stage, and can be archived too. Run state lives on disk, so it survives a restart; a run whose session ended without `done` turns quiet instead of looking busy forever.
- The page stays self-contained: the robots come from `assets/generate.py` (`skills/chief/robots.svg`) and its three typefaces ship with the skill as small WOFF2 files under SIL OFL 1.1, so nothing is fetched from another host. The archive endpoint accepts only requests carrying the page's own header.

## 1.0.1 (2026-11-12)

- Fixed: `model-routing`'s `description` held an unquoted `: `, which is invalid YAML, so `npx skills add` and `npx skills update` refused the skill. The text is unchanged apart from a dash in place of that colon.
- Tests: the frontmatter check now fails an unquoted description containing `: ` or ` #`.

## 1.0.0 (2026-11-12)

First stable release. Every skill and the agent are self-contained: nothing here needs another plugin.

### The chief: one orchestrator for agent teams

- New `chief` skill and `chief` agent (were `conduct` and `conductor`). Start a run with `/chief <task>` in any session or `claude --agent chief`; it also answers to "orchestrate", "orchestrator", "manage agents", "team of agents" and "use parallel agents". The agent loads the skill, so the two never drift apart.
- Tasks come from anywhere: a description in chat, a file from a client or manager, or a GitHub or GitLab issue. Each run gets a folder under `.chief/runs/` (ignored by git) with the request saved verbatim.
- Every brief starts with a run tag, so Claude subagents and Codex jobs are found without bookkeeping: `chief-run.py` reads Claude Code's transcripts and the records `run-codex-job.py` now writes.
- Live dashboard (`chief-run.py serve`): runs in columns by phase, a chip per agent on either provider, both accounts' 5-hour and weekly use with the 60% switch line, git warnings, and per-run detail with each agent's reply and the run's task, plan, progress and handoff.
- Cost report (`chief-run.py summary`): one row per agent, a Claude subtotal, a ChatGPT subtotal and their sum labelled as an API-equivalent estimate, plus each account's window use from start to end. Prices from the official Anthropic and OpenAI pages in `model-routing/prices.json`, including Haiku 5.5 long-context rates.
- Pipeline adds a gate (the project's full checks before any reviewer runs) and a finish step (pull request, local merge, keep or discard, only on the user's answer).

### Routing and git hygiene

- The usage check runs at intake, each new request, each phase start, before each wave and after a usage-limit stop, so one account is no longer drained while the other sits idle. It reports whether the pre-spawn hook is registered.
- New `git-check.py`: auto-committing Stop hooks, skip-worktree files that differ from HEAD, stray `*-E`/`*.orig`/`*.bak` files and auto-save commits, with a `chief-run` marker that auto-save hooks can respect. Briefs forbid `sed -i` and ask workers to name stray files.
- New `configure-hooks.py` registers the usage hook and, with permission, makes auto-save hooks skip chief runs.

### Renames and independence

- `parallel-agent-development` is now `parallel-build`, rebased on the current upstream text word for word (its `references/` files, `plan-repo` and `-C DIR` support in the scripts), with our parallel waves, ownership checks, verification budget, pause and resume, and run tag kept as separate sections. `parallel-plans` is likewise the current upstream text plus the parallel layout, ownership and final verification task. Workspaces live under `.parallel-build/`.
- New in this repo, copied word for word with only names changed: `executing-plans`, `requesting-code-review`, `using-git-worktrees`, `finishing-a-development-branch`, `test-driven-development`, `systematic-debugging` and `verification-before-completion`, the skills the build hands off to. Nothing here needs another plugin.
- `parallel-plans` and `parallel-build` have descriptions of their own, so "parallel agents" requests reach them instead of similarly described skills from other plugins.
- `check-account-usage.py --conductor` is now `--orchestrator` (the old flag still works).

### Install

- New [INSTALL.md](./INSTALL.md): point any agent at it to install the skills, the agent and the hooks.
