# Pipeline walkthrough

One run, start to finish. Useful when debugging a run or driving the CLI by
hand.

The orchestrator does exactly two things: it starts the run, then it loops.

```bash
python scripts/codag.py init --prompt "add magic-link login"

# then, until it says stop:
python scripts/codag.py next --json
```

Each `next` returns one action — `run`, `dispatch`, `ask`, `escalate` or
`stop` — which the orchestrator performs before calling `next` again.
Everything below describes what the machine decides at each phase, and the
artifacts that come out.

## `init`

- Preflight: git repo, at least one commit, clean tree, attached HEAD, not a
  linked worktree, and a base branch it can find. Refuses otherwise;
  `--force` proceeds and records the warnings.
- Resolves the **base branch** - `base_branch` from config, else
  `origin/HEAD`'s target, else `main`, else `master` - and records its tip as
  the run's base commit. Everything the run creates forks from there, so the
  final diff is exactly `base..feature`. If you are standing on another
  branch with commits the base lacks, it says so and names them; it does not
  block.
- Hides `.codag/` from git: always in `.git/info/exclude`, and — unless
  `manage_gitignore: false` — in the project's `.gitignore`, creating that
  file if absent. The `.gitignore` change is left uncommitted for you to
  review; cod-ag does not commit to your branch. Preflight recognises its
  own block, so a later run is not blocked by it.
- Prunes orphan worktrees left by an earlier crashed run.
- Creates `.codag/runs/<run-id>/` and records the base commit and branch.
- Detects the stack into `stack.json`.
- Creates the integration worktree, installs dependencies, and runs the gates
  to produce `baseline-gates.json`.

Phase becomes `grill`.

## `grill` → dispatch the planner

The machine renders `cycle-N/dispatch/planner-round-R.md` and dispatches
`codag-planner` on `opus`. The planner either writes
`cycle-N/questions-round-R.yaml` and returns `QUESTIONS`, or writes
`tasks.yaml` and returns `PLAN`.

At the round cap (`max_grill_rounds`, default 3) the prompt tells the planner
it **must** produce a plan and record anything unresolved as an assumption.

## `ask` → put the questions to the user

The action carries an `AskUserQuestion`-shaped payload built from the
planner's YAML, with its recommendation already marked. The orchestrator asks
and records:

```bash
python scripts/codag.py answer Q1="15-minute timer" Q2=Both --note Q1="match the cookie"
```

That appends the Q&A verbatim to `spec.md` under `## Clarifications (round
N)` and increments the round counter. The spec file, not the conversation, is
the durable record — a later cycle re-reads the same file.

Spec-mode runs are grilled too: a spec file is a starting point, not a
contract.

## `plan` → validate

Every `next` re-validates `tasks.yaml`. On failure the planner is
re-dispatched with the exact error list and told to change nothing else,
capped by `max_plan_fix_attempts`. Past the cap the run stops rather than
grinding.

## `approve` → the gate

Applies when `approval_gate` is `chat` (default) and this is a chat-mode
run's first cycle. The action carries the plan table command, the validator
warnings and any recorded assumptions.

```bash
python scripts/codag.py approve --yes
python scripts/codag.py approve --revise "Split the CLI slice in two."
python scripts/codag.py approve --abort
```

`--revise` sends the plan back to the planner with the feedback. `--abort`
reaps the worktrees and ends the run.

## `execute` → the branch, then waves

Before a single line is written, the run's branch gets its real name:

```bash
python scripts/codag.py branch
```

The name comes from `branch_template` (default `{kind}/{slug}`), so a
feature run lands on `feature/magic-link-login` and a bugfix on
`bugfix/token-expiry`. This happens here rather than at `init` because the
name depends on `kind` and the goal, which only exist once the plan does. A
name already in use gets a `-2` suffix. Your working tree is not switched to
it - cod-ag has never moved your HEAD.

Then a `run` action prepares the wave:

```bash
python scripts/codag.py worktree create S1 S2
python scripts/codag.py brief S1 S2
```

Then a single `dispatch` action carrying the whole wave — every executor goes
out in one message, which is what makes them concurrent. Each gets its own
rendered prompt naming its brief, the interfaces its dependencies published,
and the command to report with.

Each executor works test-first in its own worktree, commits per green test,
and records its own result:

```bash
python scripts/codag.py report --slice S1 --status DONE --tests "7 passed, 0 failed"
```

A `DONE` is checked before it is accepted: clean worktree, HEAD moved from
the slice's base, and every declared test file present. A rejection lists all
the problems and changes nothing.

**A blocked slice** is retried exactly once, on `models.executor_escalated`.
If it blocks again it is marked failed and the run moves on; its dependents
never become ready, and the replanner picks it up.

## `synthesize` → merge

```bash
python scripts/codag.py merge
```

Creates `codag/<run-id>/integration` from the base commit and merges each
finished slice branch in dependency order.

- **clean** — no agent is dispatched at all.
- **conflict** — `codag-synthesizer` is dispatched on `sonnet` with the
  conflicted files. It resolves, runs `merge --continue`, repeats, then
  reports `CLEAN`. If the slices genuinely contradict each other it reports
  `ESCALATE`, which writes a failing verdict and sends the run to replan.

## `verify`

```bash
python scripts/codag.py verify-package
```

Runs the gates in the integration worktree, classifies failures against the
baseline, writes `review.diff`, and assembles every path the verifier needs.
`codag-verifier` reads them and writes `verdict.md`: a per-criterion table
with evidence, gate results with pre-existing failures called out, scope
violations, carried assumptions, and a final `VERDICT: PASS` or
`VERDICT: FAIL`. Then it runs `codag verdict`, which reads that line back.

## `e2e`

Only for a `kind: feature` run, and only after the verdict passed.
`codag-e2e` is dispatched into the integration worktree with the spec and
every acceptance criterion, and is told **not** to read the diff: a test
written from the implementation only restates it. It writes one test through
the real user-visible path, runs it, commits it with a `test:` prefix, then:

```bash
python scripts/codag.py report --role e2e --status PASS --tests "3 passed"
```

`SKIPPED` (with `--detail`) when nothing available can reach the feature -
the run still finishes, with the reason surfaced. `FAILED` (with `--detail`)
when the feature is genuinely broken end to end: the run stops there. That
deliberately does **not** replan, because a brand-new E2E test that fails is
more often the test's fault than the feature's, and a flaky one would eat
the cycle budget.

A `kind: bugfix` run skips this phase entirely. Its slices already had to be
written test-first, and that is the right level for a fix.

## `record`

The last thing before `done`, on every completed run - feature or bugfix.
`codag-scribe` reads the run's artifacts and appends one entry to
`.codag/progress.txt`:

```
## 2026-08-23 14:05 - 20260823-140012-magic-link
Run: .codag/runs/20260823-140012-magic-link
- What was implemented
- Files changed
- **Learnings for future iterations:**
  - routes register in src/routes/index.ts, not by file convention
---
```

The first two sections are brief; git already records what changed. The
learnings are the reason the file exists, and they are drawn from what
actually happened - a `DONE_WITH_CONCERNS`, a failed verdict, a path two
slices both needed. A run that took two cycles has more to teach than one
that went straight through.

The planner is shown these entries at the start of the next run, which is
what closes the loop.

Appending is done by `codag progress append`, not by the agent, so "append,
never replace" is a property of the code. The log lives under `.codag/`, so
it is git-ignored along with the rest of the run state.

## `done`

The `stop` action carries the message and the `finish` command, which removes
the slice worktrees and keeps the integration branch.

```
DONE

branch: codag/20260822-114900-magic-link/integration
review: git diff a1b2c3d..codag/20260822-114900-magic-link/integration
merge:  git merge codag/20260822-114900-magic-link/integration

nothing was committed to your branch main
```

Merging is your decision, always.

## `replan`

A failing verdict runs `codag cycle`: satisfied slices become `carried`, the
old plan is snapshotted, `cycle-N+1/` is created. Then `codag-replanner` is
dispatched to diagnose root cause and write a plan containing only remedial
slices. Back to `execute`, skipping the grill and the gate.

Past `max_cycles` the run stops and reports what is still unmet. It does not
loop forever and it does not claim success.

## Resuming

Just run `next`. The phase is derived from what is on disk, so it is correct
regardless of what the orchestrator remembers.

```bash
python scripts/codag.py resume --json   # the same picture, for a human
```

## Aborting

```bash
python scripts/codag.py abort
```

Removes every worktree including the integration one. Branches survive unless
you pass `--delete-branches`, so committed work stays recoverable.
