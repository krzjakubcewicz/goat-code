# Pipeline walkthrough

One run, start to finish, with the artifacts each step produces. Useful when
debugging a run or driving the CLI by hand.

## 0. `init`

```bash
python scripts/codag.py init --prompt "add magic-link login"
```

- Preflight: git repo, at least one commit, clean tree, attached HEAD.
  Refuses otherwise; `--force` proceeds and records the warnings.
- Adds `.codag/` to `.git/info/exclude`.
- Prunes orphan worktrees left by any earlier crashed run.
- Creates `.codag/runs/<run-id>/`, records the base commit and branch.
- Detects the stack into `stack.json`.
- Creates the integration worktree, installs dependencies, and runs the
  gates to produce `baseline-gates.json`.

Phase becomes `grill`.

## 1. Grill

The orchestrator dispatches `codag-planner`, which returns either
`QUESTIONS` or `PLAN`.

Each question round: the orchestrator asks via `AskUserQuestion`, appends
the answers to `spec.md` under `## Clarifications (round N)`, and
re-dispatches. Maximum three rounds; anything unresolved becomes an entry in
the plan's `assumptions`, which the verifier surfaces at the end.

Spec-mode runs are grilled too — a spec file is a starting point, not a
contract.

## 2. Plan and validate

The planner writes `tasks.yaml`.

```bash
python scripts/codag.py plan validate
python scripts/codag.py plan show
```

Validation failure sends the planner back with the exact error list, twice
at most. See `cod-ag-conventions` for every rule.

## 3. Approve

Chat mode only. The slice table, the warnings and the assumptions go to the
user: approve, revise, or abort. Replan cycles skip this.

## 4. Execute

```bash
python scripts/codag.py wave next            # -> S1 S2
python scripts/codag.py worktree create S1 S2
python scripts/codag.py brief S1 S2
```

All executors in a wave are dispatched **in one message**, which is what
makes them concurrent. Each gets a brief path, the interfaces earlier slices
published, and a report path.

Each executor works test-first in its own worktree, commits per green test,
and returns a receipt:

```
STATUS: DONE
COMMITS: a1b2c3d..e4f5g6h (4 commits)
TESTS: 7 passed, 0 failed
```

The orchestrator records the result and appends to the ledger:

```bash
python scripts/codag.py task commits S1 --head e4f5g6h
python scripts/codag.py task status S1 done
python scripts/codag.py ledger "slice S1 complete (commits a1b2c3d..e4f5g6h)"
```

Then the next wave, until nothing is ready.

### When an executor does not return DONE

| Status | Response |
| --- | --- |
| `DONE_WITH_CONCERNS` | read them; act if correctness or scope, note if observation |
| `NEEDS_CONTEXT` | supply exactly what is missing, re-dispatch |
| `BLOCKED` | change something — more context, the escalated model, a smaller slice — then retry, or let the replanner take it |

A failed slice does not stop the wave. Its dependents simply never become
ready, and the replanner picks it up.

## 5. Synthesize

```bash
python scripts/codag.py merge
```

Creates `codag/<run-id>/integration` from the base commit and merges each
finished slice branch in dependency order.

- `clean` — no agent is dispatched at all.
- `conflict` — `merge-report.md` names the slice and the files. The
  synthesizer resolves them, runs `merge --continue`, and repeats. It logs
  every non-conflict edit in the report's justification table.

## 6. Verify

```bash
python scripts/codag.py verify-package
```

Runs the gates in the integration worktree, classifies failures against the
baseline, writes `review.diff`, and returns every path the verifier needs.

`codag-verifier` reads them and writes `verdict.md`: a per-criterion table
with evidence, gate results with pre-existing failures called out, scope
violations, carried assumptions, and a final `VERDICT: PASS` or
`VERDICT: FAIL`.

## 7a. PASS

```bash
python scripts/codag.py finish
```

Removes the slice worktrees, keeps the integration branch, prints the review
and merge commands. Your branch is untouched — merging is your decision.

## 7b. FAIL

```bash
python scripts/codag.py cycle
```

Marks satisfied slices `carried`, snapshots the old plan, creates
`cycle-N+1/`. Refuses past `max_cycles`.

`codag-replanner` diagnoses root cause and writes a plan containing only
remedial slices. Back to step 4, skipping the grill and the approval gate.

At the cap the run stops and reports what is still unmet. It does not loop
forever and it does not claim success.

## Resuming

```bash
python scripts/codag.py resume
```

Prints the phase, which slices are complete according to the ledger, what is
ready and the merge state. Trust it over recollection.

## Aborting

```bash
python scripts/codag.py abort
```

Removes every worktree including the integration one. Branches survive
unless you pass `--delete-branches`, so committed work is recoverable.
