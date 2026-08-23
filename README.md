# cod-ag

A multi-agent feature pipeline for Claude Code. You describe a feature; it
interrogates you until the requirements are unambiguous, splits the work
into independently-shippable vertical slices, builds them in parallel in
isolated git worktrees, merges them, verifies the result against your
acceptance criteria, and either says `DONE` or replans and tries again.

It never commits to the branch you are on.

```
/cod-ag "add magic-link login"
/cod-ag --spec docs/specs/auth.md
```

## How it works

```
        you
         │  request or spec
         ▼
   ┌─────────────────────────────────────────────┐
   │ orchestrator  (the main Claude Code thread) │
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

The orchestrator is the main thread rather than an agent, because only the
main thread can spawn subagents and ask you questions. Planner, executor,
synthesizer, verifier and replanner are real subagents with isolated
context.

**The orchestrator does not decide any of this.** `codag next` reads the run
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
- **No commits to your branch.** The first run adds `.codag/` and
  `.worktrees/` to your `.gitignore` (creating it if absent) and leaves that
  change uncommitted for you to review. Set `manage_gitignore: false` to
  keep the entry local to `.git/info/exclude` instead.

## Install

```
/plugin marketplace add /path/to/cod-ag
/plugin install cod-ag@cod-ag
```

Requires **Python 3.9+** and **git**. No pip install — the script layer is
stdlib-only, including its YAML reader. Works on Windows, macOS and Linux;
CI runs the test suite on all three.

## Commands

| Command | What it does |
| --- | --- |
| `/cod-ag "<request>"` | run the pipeline from a chat request |
| `/cod-ag --spec <file>` | run it from a markdown spec |
| `/cod-ag-spec [name]` | scaffold a spec file to fill in |
| `/cod-ag-status [--all]` | phase, cycle, slice states, recent ledger |
| `/cod-ag-resume` | pick up an interrupted run from its ledger |
| `/cod-ag-abort` | stop a run and clean up its worktrees |

## What you get back

On success:

```
DONE

branch: feature/magic-link-login
review: git diff a1b2c3d..feature/magic-link-login
merge:  git merge feature/magic-link-login

nothing was committed to your branch main
```

Each completed run also appends to `.codag/progress.txt` - what was built,
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
from the `model:` frontmatter on `/cod-ag` and `/cod-ag-resume`, not from
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

Optional. Copy `templates/config.yaml` to `.codag/config.yaml` in your
project to change the parallelism, the cycle cap, the grill rounds, timeouts,
the branch naming or the model per role.

## Run state

Everything lives in `.codag/` in the target repo, hidden from git via
`.git/info/exclude`, and listed in your `.gitignore` on the first run:

```
.codag/runs/<run-id>/
  spec.md  stack.json  state.json  ledger.md  baseline-gates.json
  tasks.yaml
  cycle-1/
    briefs/  reports/  merge-report.md  gates.json  review.diff  verdict.md
```

Worktrees live outside the repo, at `<tempdir>/codag/<hash>/<slice>` — short
paths, so a deep `node_modules` cannot hit the Windows path limit, and a
failed cleanup cannot litter your project.

## The script layer

Everything mechanical is a script, so the agents spend their tokens on
judgement instead of git plumbing. You can drive the whole pipeline by hand:

```bash
python scripts/codag.py init --prompt "add magic-link login"
python scripts/codag.py next          # what to do, and what to do after that
```

`next` is the whole pipeline. The individual steps it orchestrates are all
callable directly too:

```bash
python scripts/codag.py plan validate
python scripts/codag.py worktree create S1 S2
python scripts/codag.py brief S1 S2
python scripts/codag.py report --slice S1 --status DONE --tests "7 passed"
python scripts/codag.py merge
python scripts/codag.py verify-package
python scripts/codag.py finish
```

`python scripts/codag.py --help` lists everything. Add `--json` for
machine-readable output.

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
