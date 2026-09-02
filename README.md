# goat-code

A multi-agent feature pipeline. You describe a feature; it
interrogates you until the requirements are unambiguous, splits the work
into independently-shippable vertical slices, builds them in parallel in
isolated git worktrees, merges them, verifies the result against your
acceptance criteria, and either says `DONE` or replans and tries again.

It never commits to the branch you are on.

Run it two ways. In Claude Code:

```
/goat-code "add magic-link login"
/goat-code --spec docs/specs/auth.md
```

Or in a terminal, with no session at all:

```bash
python scripts/goatcode.py run --prompt "add magic-link login"
python scripts/goatcode.py run --spec docs/specs/auth.md --yes
```

Both drive the same state machine and the same agents. See
[Standalone](#standalone) for what the second one needs.

## How it works

```
        you
         │  request or spec
         ▼
   ┌─────────────────────────────────────────────┐
   │ orchestrator                                │
   │ the main Claude Code thread, or `goatcode run` │
   └─────────────────────────────────────────────┘
         │
         ├─ grill ────── planner ⇄ you        up to 3 question rounds
         ├─ plan ─────── tasks.yaml           validated: DAG, disjoint ownership
         ├─ approve ──── you                  chat mode only
         │
         ├─ execute ──── executor  executor  executor      parallel, one
         │               worktree  worktree  worktree      worktree each
         │
         ├─ synthesize ─ deterministic merge, then the synthesizer
         │               only if there is a real conflict
         │
         ├─ verify ───── gates + every acceptance criterion
         │
         ├─ e2e ──────── one test proving the finished feature
         │               (features only; a bugfix skips it)
         │
         └─ DONE  or  replanner ──▶ execute again  (max 3 cycles)
```

In the plugin the orchestrator is the main thread rather than an agent,
because only the main thread can spawn subagents and ask you questions.
Planner, executor, synthesizer, verifier and replanner are real subagents
with isolated context. Standalone, `goatcode run` is the orchestrator and each
agent is its own headless `claude` process.

**The orchestrator does not decide any of this.** `goatcode next` reads the run
state off disk and returns one action - run this command, dispatch these
agents on these models, ask this, or stop - and the orchestrator performs it
and calls `next` again. Phases, caps, retry policy and the exact text each
agent receives are all in tested Python, so a whole run can be driven with
no model at all. That is what the end-to-end test does.

## What makes it safe to run in parallel

- **Exclusive file ownership.** The planner assigns each slice a disjoint
  set of path globs, and a validator refuses any plan where two slices in
  the same wave could touch the same file. Merges are near-conflict-free by
  construction, not by luck.
- **Isolated worktrees.** Each executor gets its own checkout, branched off
  the commit the run started from, in the OS temp directory. Your working
  tree is never involved.
- **One base, one feature branch.** Every run forks from your base branch -
  `main`, `master`, or whatever you configure - never from wherever you
  happen to be standing. Before any code is written the work gets a properly
  named branch, `feature/magic-link-login` by default, and everything lands
  there. The final diff is exactly `base..feature`: the thing you would open
  a pull request with. Nothing is merged into your branch, your HEAD is never
  moved, and nothing is ever pushed.
- **A baseline.** Gates run once at the start, so a lint error that was
  already there is never blamed on the run — and never blocks it forever.
- **A durable ledger.** Progress is on disk, so a crash or a context
  compaction cannot cause finished slices to be rebuilt.
- **Checked reports.** Agents record results by running the CLI, and a slice
  claiming `DONE` is rejected unless its worktree is clean, its HEAD has
  moved, and the tests its brief declares exist.
- **No commits to your branch.** The first run adds `.goatcode/` and
  `.worktrees/` to your `.gitignore` (creating it if absent) and leaves that
  change uncommitted for you to review. Set `manage_gitignore: false` to
  keep the entry local to `.git/info/exclude` instead.

## Install

```
/plugin marketplace add krzjakubcewicz/goat-code
/plugin install goat-code@goat-code
```

`/plugin marketplace add /path/to/goat-code` instead, for a local checkout.

To install it for a repository rather than for yourself, commit this to that
repo's `.claude/settings.json` - anyone who opens it gets the plugin after
one trust prompt:

```json
{
  "extraKnownMarketplaces": {
    "goat-code": {
      "source": { "source": "github", "repo": "krzjakubcewicz/goat-code" }
    }
  },
  "enabledPlugins": { "goat-code@goat-code": true }
}
```

A local checkout works there too - `{ "source": "local", "path": "/path/to/goat-code" }` -
but the path is machine-specific, so put that one in the gitignored
`.claude/settings.local.json`.

Requires **Python 3.9+** and **git**. No pip install — the script layer is
stdlib-only, including its YAML reader. Works on Windows, macOS and Linux;
CI runs the test suite on all three.

## Commands

| Command | What it does |
| --- | --- |
| `/goat-code "<request>"` | run the pipeline from a chat request |
| `/goat-code --spec <file>` | run it from a markdown spec |
| `/goat-code-spec [name]` | scaffold a spec file to fill in |
| `/goat-code-status [--all]` | phase, cycle, slice states, recent ledger |
| `/goat-code-resume` | pick up an interrupted run from its ledger |
| `/goat-code-abort` | stop a run and clean up its worktrees |

## Standalone

The pipeline does not need a Claude Code session. `goatcode run` is the
orchestrator itself: it reads the same actions from the same state machine
and spawns each agent as a headless `claude` process.

```bash
python scripts/goatcode.py run --prompt "add magic-link login"
python scripts/goatcode.py run --spec docs/specs/auth.md --yes
python scripts/goatcode.py run --resume        # after an interruption
```

Needs the [Claude Code CLI](https://claude.com/claude-code) on `PATH` and a
working login - the same one you already use. Nothing else; still no pip
install.

| Flag | |
| --- | --- |
| `--yes` | take every recommended answer and approve the plan. For CI. |
| `--max-cost N` | dollar cap per agent dispatch |
| `--quiet` | receipts only, no per-tool output |
| `--claude-bin` | the executable, if it is not on `PATH` |
| `--claude-arg=--safe-mode` | any extra argument for `claude`, repeatable |

Without `--yes` it asks the grill questions and the approval gate in the
terminal. With no terminal and no `--yes` it stops rather than blocking on a
read that will never return.

```
[00:12] execute    wave of 2 slice(s) ready
    S1         Write test/auth/magic-link.test.js
    S2         Bash npm test -- session
    S1         Edit src/auth/session.js
  ok   S1 251s $0.31
  ok   S2 228s $0.27
```

**Permissions.** Agents run headless, so nobody can answer a permission
prompt; a Bash call that needs one is denied and reported back. If you want
agents running commands unattended, set `permission_mode: bypassPermissions`
in `.goatcode/config.yaml`. That is not the default on purpose.

**Your settings come along.** A dispatched `claude` inherits your `~/.claude`
configuration, which is how the `superpowers` and `ponytail` skills the
agents use resolve at all - and also means your own hooks and `CLAUDE.md`
reach every agent. `--claude-arg=--safe-mode` turns all of it off, including
those skills.

## What you get back

On success:

```
DONE

branch: feature/magic-link-login
review: git diff a1b2c3d..feature/magic-link-login
merge:  git merge feature/magic-link-login

nothing was committed to your branch main
```

Each completed run also appends to `.goatcode/progress.txt` - what was built,
what changed, and the learnings a later run would otherwise rediscover. The
planner reads those entries before planning the next piece of work.

The run directory holds the whole record: the spec with your clarifications
appended, the plan, each slice's brief and report, the merge report, the
gate results and the verdict with per-criterion evidence.

## Models

Each role runs on the cheapest model that can do its job. Judgement work
gets the expensive models; mechanical work does not.

| Role | Model | Why |
| --- | --- | --- |
| orchestrator | haiku | follows a fixed script; the CLI does the thinking |
| planner | opus | interrogation and decomposition are the highest-leverage step |
| executor | haiku | works from a complete brief, test-first, in one slice |
| executor (after BLOCKED) | sonnet | one step up when haiku could not finish |
| synthesizer | sonnet | reconciles conflicts, but within a very narrow brief |
| e2e | sonnet | writes one test proving the finished feature from outside |
| scribe | sonnet | writes up the run and its learnings in the progress log |
| verifier | opus | the only gate on whether the run is actually done |
| replanner | opus | diagnosing root cause is the hardest judgement in the loop |

The orchestrator is the main thread rather than an agent, so its model comes
from the `model:` frontmatter on `/goat-code` and `/goat-code-resume`, not from
`config.yaml`.

The planner also picks a model per slice, so a slice that genuinely needs
more than haiku can say so in the plan.

## Branch naming

The branch is created once the plan is approved - before any code - and is
named from `branch_template`, which defaults to `{kind}/{slug}`:

```
feature/magic-link-login
bugfix/token-expiry-off-by-one
```

Available placeholders: `{kind}` (feature or bugfix, from the plan),
`{slug}` (from the goal), `{run_id}`, `{date}`, `{time}`, `{user}`. A name
already in use gets a `-2` suffix.

`base_branch` controls what it forks from; `null` auto-detects
`origin/HEAD`, then `main`, then `master`.

## Configuration

Optional. Copy `templates/config.yaml` to `.goatcode/config.yaml` in your
project to change the parallelism, the cycle cap, the grill rounds, timeouts,
the branch naming or the model per role.

## Run state

Everything lives in `.goatcode/` in the target repo, hidden from git via
`.git/info/exclude`, and listed in your `.gitignore` on the first run:

```
.goatcode/runs/<run-id>/
  spec.md  stack.json  state.json  ledger.md  baseline-gates.json
  tasks.yaml
  cycle-1/
    briefs/  reports/  merge-report.md  gates.json  review.diff  verdict.md
```

Set `debug: true` in the config, or `GOATCODE_DEBUG=1` in the environment, and
each run also gets a `log.txt` next to its state: every command, subprocess,
phase change, dispatch and file write, timestamped and appended. Off by
default; the environment variable wins over the config either way.

Worktrees live outside the repo, at `<tempdir>/goatcode/<hash>/<slice>` — short
paths, so a deep `node_modules` cannot hit the Windows path limit, and a
failed cleanup cannot litter your project.

## The script layer

Everything mechanical is a script, so the agents spend their tokens on
judgement instead of git plumbing. You can drive the whole pipeline by hand:

```bash
python scripts/goatcode.py init --prompt "add magic-link login"
python scripts/goatcode.py next          # what to do, and what to do after that
```

`next` is the whole pipeline. The individual steps it orchestrates are all
callable directly too:

```bash
python scripts/goatcode.py plan validate
python scripts/goatcode.py worktree create S1 S2
python scripts/goatcode.py brief S1 S2
python scripts/goatcode.py report --slice S1 --status DONE --tests "7 passed"
python scripts/goatcode.py merge
python scripts/goatcode.py verify-package
python scripts/goatcode.py finish
```

`python scripts/goatcode.py --help` lists everything. Add `--json` for
machine-readable output.

## Skill dependencies

goat-code's agents load these from other plugins. Install them alongside it:

| plugin | skills used |
| --- | --- |
| `superpowers` | `brainstorming`, `writing-plans`, `test-driven-development`, `systematic-debugging`, `verification-before-completion` |
| `ponytail` | `ponytail`, `ponytail-review` |
| `engineering-skills` | the `senior-*` specialist chosen at runtime from `stack.json` |

The list is pinned by a test, so adding a dependency is a deliberate act.
Anything else an agent needs is written into the agent itself - goat-code runs
on machines that do not have whatever happens to be in your `~/.claude`.

## Built on

[superpowers](https://github.com/anthropics/claude-plugins-official) for
TDD, planning, parallel dispatch and verification discipline;
`engineering-skills` for per-stack specialists; `caveman` for terse
agent-to-agent reports; `ponytail` for keeping executors from gold-plating.

## Development

```bash
python -m pytest scripts/tests -v
```

`scripts/tests/test_pipeline_e2e.py` drives whole runs through the state
machine with a fake agent and no LLM: the happy path, parallel wave
batching, a failing verdict driving a replan cycle, carried slices never
re-executing, the cycle cap stopping rather than looping, a blocked slice
retried exactly once on a stronger model, and a merge conflict waking the
synthesizer. That suite passing is what proves the pipeline on each OS.
