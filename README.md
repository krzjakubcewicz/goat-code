# cod-ag

A multi-agent feature pipeline for Claude Code. You describe a feature; it
interrogates you until the requirements are unambiguous, splits the work
into independently-shippable vertical slices, builds them in parallel in
isolated git worktrees, merges them, verifies the result against your
acceptance criteria, and either says `DONE` or replans and tries again.

It never touches the branch you are on.

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
         └─ DONE  or  replanner ──▶ execute again  (max 3 cycles)
```

The orchestrator is the main thread rather than an agent, because only the
main thread can spawn subagents and ask you questions. Planner, executor,
synthesizer, verifier and replanner are real subagents with isolated
context.

## What makes it safe to run in parallel

- **Exclusive file ownership.** The planner assigns each slice a disjoint
  set of path globs, and a validator refuses any plan where two slices in
  the same wave could touch the same file. Merges are near-conflict-free by
  construction, not by luck.
- **Isolated worktrees.** Each executor gets its own checkout, branched off
  the commit the run started from, in the OS temp directory. Your working
  tree is never involved.
- **An integration branch.** Everything lands on `codag/<run-id>/integration`
  for you to review. Nothing is merged into your branch and nothing is ever
  pushed.
- **A baseline.** Gates run once at the start, so a lint error that was
  already there is never blamed on the run — and never blocks it forever.
- **A durable ledger.** Progress is on disk, so a crash or a context
  compaction cannot cause finished slices to be rebuilt.

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

branch: codag/20260822-114900-magic-link/integration
review: git diff a1b2c3d..codag/20260822-114900-magic-link/integration
merge:  git merge codag/20260822-114900-magic-link/integration

your branch main was not touched
```

The run directory holds the whole record: the spec with your clarifications
appended, the plan, each slice's brief and report, the merge report, the
gate results and the verdict with per-criterion evidence.

## Configuration

Optional. Copy `templates/config.yaml` to `.codag/config.yaml` in your
project to change the parallelism, the cycle cap, the grill rounds, timeouts
or the model per role.

## Run state

Everything lives in `.codag/` in the target repo, hidden from git via
`.git/info/exclude` so your working tree stays clean:

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
python scripts/codag.py plan validate
python scripts/codag.py wave next
python scripts/codag.py worktree create S1 S2
python scripts/codag.py brief S1 S2
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

The suite includes an end-to-end pipeline run with no LLM in the loop —
init, plan validation, three worktrees, real commits, merge, gates and
finish — which is what proves the deterministic spine on each OS.
