---
name: cod-ag-orchestrator
description: >
  Runs the cod-ag feature pipeline end to end - grill the user, plan into
  vertical slices, dispatch parallel executors in isolated worktrees,
  synthesize, verify, and either report DONE or replan. Use when the user
  asks to implement a feature with cod-ag, invokes /cod-ag, or asks to
  resume or continue a cod-ag run.
---

# cod-ag Orchestrator

**You are the orchestrator.** Not a subagent — you, the main thread. Only
you can spawn agents and ask the user questions, so the loop lives here.

Announce once at the start: "Running the cod-ag pipeline." Then work.

Read `cod-ag:cod-ag-conventions` before step 1 — it holds the artifact
shapes, status codes and CLI you will use throughout.

Throughout: `CODAG="${CLAUDE_PLUGIN_ROOT}/scripts/codag.py"` and every call
is `python "$CODAG" <command>`. Use `--json` when you need to read a field.

## Narration

One short line between steps. The ledger and the tool results carry the
record; a running commentary just burns the user's attention. Do not paste
file contents, diffs, or agent reports into your own messages — hand over
paths.

## Step 1 — Preflight

```bash
python "$CODAG" init --prompt "<the user's request>"        # chat mode
python "$CODAG" init --spec <path>                          # spec mode
```

This runs preflight, creates the run directory, detects the stack, creates
the integration worktree and captures baseline gates. It refuses on a dirty
tree, a detached HEAD, or a repo with no commits — relay the message, do not
work around it. `--force` exists but tell the user what they are overriding.

Record from the output: run id, spec path, stack path, tasks path, base
commit, integration branch.

## Step 2 — Grill (both chat and spec mode)

Dispatch `codag-planner` with: the spec path, the stack path, the tasks path
to write, and the round number. Not the spec's contents — the path.

The planner returns **QUESTIONS** or **PLAN**.

On QUESTIONS:

1. Convert them to `AskUserQuestion`, at most 4 per call, blocking ones
   first. Use the planner's `options` as the options and mark its
   `recommended` one "(Recommended)".
2. Append the answers to the spec, verbatim:
   ```
   ## Clarifications (round N)

   **Q1 (scope):** <question>
   **A:** <what the user chose, including any note they added>
   ```
   Write it with Edit/Write to the spec path — never re-dispatch answers
   only in the prompt, or they vanish on the next cycle.
3. Re-dispatch the planner with the round number incremented.

**Cap: three rounds.** On round three tell the planner it must return PLAN
and record anything unresolved as an assumption. Replan cycles skip grilling
entirely.

If the user declines to answer or picks "Other" with a redirect, take that
as their decision and pass it through.

## Step 3 — Validate

```bash
python "$CODAG" plan validate
```

Exit 0 means proceed. On failure, re-dispatch the planner with the exact
error list and the instruction to fix only what is listed. **Two attempts**,
then stop and show the user the errors — a planner that cannot satisfy the
validator twice is a signal the request itself is unclear.

Warnings do not block. Show them at the approval gate.

## Step 4 — Approve

```bash
python "$CODAG" plan show
```

Show the user the slice table plus any validator warnings and recorded
assumptions, then `AskUserQuestion`: approve / revise / abort.

- **revise** — take their feedback, re-dispatch the planner with it, return
  to step 3.
- **abort** — `python "$CODAG" abort` and stop.

Skip this gate on replan cycles; the user already approved the goal.

## Step 5 — Execute the waves

Loop until no slices remain:

```bash
python "$CODAG" wave next --json
```

For the returned batch:

```bash
python "$CODAG" worktree create S1 S2 S3
python "$CODAG" brief S1 S2 S3
```

Then **dispatch one `codag-executor` per slice, all in a single message** — that is
what makes them parallel; one per message runs them in sequence and wastes
the whole design. Use each slice's `model` field.

Each dispatch contains only:

1. One line on where this slice fits in the overall feature.
2. The brief path, introduced as "read this first — it is your
   requirements, with the exact values to use verbatim".
3. Interfaces and decisions from **earlier** slices that the brief could not
   know.
4. Your resolution of any ambiguity you spotted in the brief.
5. The report path and the report contract.

Never paste the brief's text, the plan, or previous slices' summaries into a
dispatch. A fresh executor needs its task and the interfaces it touches —
nothing else.

### Handling what comes back

| Status | Do |
| --- | --- |
| `DONE` | record commits, mark done, ledger |
| `DONE_WITH_CONCERNS` | read the concerns. Correctness or scope → address before proceeding. Observation → note it for the verifier and proceed. |
| `NEEDS_CONTEXT` | supply exactly what is missing, re-dispatch the same slice |
| `BLOCKED` | see below |

On `BLOCKED`, something must change before you retry. In order: give more
context; re-dispatch on `opus`; split the slice; escalate to the user. Never
re-dispatch an unchanged blocked task to the same model.

After each slice:

```bash
python "$CODAG" task commits S1 --head <head sha>
python "$CODAG" task status S1 done
python "$CODAG" ledger "slice S1 complete (commits a1b2c3d..e4f5g6h, tests green)"
```

If a slice ends `failed`, keep going with the rest of the wave. Its
dependents will not become ready, and the replanner will pick it up.

Do not pause to check in between waves. Execute the whole plan.

## Step 6 — Synthesize

```bash
python "$CODAG" merge
```

Exit 0 and `status: clean` means **do not dispatch the synthesizer at all**
— there is nothing for it to judge. Go to step 7.

On `status: conflict`, dispatch `codag-synthesizer` with: the integration
worktree path, the merge report path, and the ids of the slices in conflict.
It resolves, runs `merge --continue`, and repeats until clean.

If it returns `ESCALATE`, the slices genuinely contradict each other. Treat
that as a verification failure and go to step 8 with its explanation.

## Step 7 — Verify

```bash
python "$CODAG" verify-package --json
```

This runs the gates, writes the review package, and returns every path the
verifier needs. Dispatch `codag-verifier` with those paths and the verdict
path to write. Hand it paths, never contents.

## Step 8 — Branch

**PASS:**

```bash
python "$CODAG" finish
```

Report to the user: `DONE`, the integration branch, what was built (one
short paragraph), the review command, any assumptions the verifier
surfaced, and any pre-existing failures the run did not cause. State plainly
that their branch was not touched.

**FAIL:**

```bash
python "$CODAG" cycle
```

This carries finished slices forward and creates the next cycle directory.
It refuses past the cap. Then dispatch `codag-replanner` with the verdict,
the old plan, the gates and the diff. Validate its plan (step 3) and return
to step 5 — skipping the grill and the approval gate.

**At the cap** (`cycle` exits non-zero): stop. Report which criteria are
still unmet, what the last verdict said, where the integration branch is,
and what you would try next. Do not silently loop, and do not declare
success.

## Resuming

After a crash, an interruption, or a context compaction:

```bash
python "$CODAG" resume --json
```

Trust its output and `git log` over your own recollection. Anything the
ledger marks complete **is** complete — re-dispatching finished slices is
the most expensive mistake available to you. Pick up at the reported phase.

## Never

- Dispatch executors one per message when they could run in parallel.
- Paste briefs, diffs, plans or reports into a dispatch or your own output.
- Skip the validator, or the grill, or the approval gate in chat mode.
- Let a subagent edit `tasks.yaml`; you own it, through the CLI.
- Report DONE when the verifier said FAIL, or when the cap was hit.
- Merge the integration branch into the user's branch. That is their call.
