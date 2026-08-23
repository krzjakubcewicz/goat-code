# Architecture

Why cod-ag is shaped the way it is. For how to *use* it, see the README;
for the artifact contracts, see `skills/cod-ag-conventions/SKILL.md`.

## The constraint that determines everything

**Subagents cannot reliably spawn subagents, and they cannot ask the user
questions.** Only the main Claude Code thread has the `Agent` tool and
`AskUserQuestion`.

So the orchestrator is not an agent file — it is a skill the main thread
follows. Everything that needs to fan out or talk to the user happens there.
The five agents are leaves.

This also explains the grill loop's shape. The planner is the one who knows
what is ambiguous, but it cannot ask. So it *returns* questions, the
orchestrator asks them, appends the answers to the spec, and re-dispatches.
The spec file — not the conversation — is the durable record, which is why
the answers are written to disk rather than passed in the next prompt.

## The orchestrator is a state machine

The pipeline's control flow lives in `machine.py`, not in the orchestrator
skill. `next_action` reads the run's files, derives which phase the evidence
implies, and returns exactly one action: run this command, dispatch these
agents on these models, put these questions to the user, or stop.

`derive_phase` is pure - no writes, no side effects - and `next_action`
persists whatever it returns. That means `state.json` is a cache of the
evidence rather than the source of truth: a stale or hand-edited phase
self-corrects on the next call, which is also what makes resuming after a
crash or a compaction trustworthy.

Everything that used to be prose the model was trusted to honour is now
code with a test:

| Was | Is |
| --- | --- |
| "maximum three rounds" | `grill_rounds` incremented by `answer`, checked by `grill_exhausted` |
| "two attempts, then stop" | `plan_fix_attempts`, `plan_fixes_exhausted` |
| "at cycle 4, stop" | `cycles_exhausted`, enforced in `_replan` |
| "re-dispatch on a stronger model, once" | `escalations` per slice, enforced in `_retry_blocked` |
| "dispatch all executors in one message" | one `dispatch` action carrying the whole wave |
| "gate in chat mode only" | `approval_gate` config, `gate_applies` |

Because the machine is a function of files, a whole run can be driven with
no model: `test_pipeline_e2e.py` loops on `next_action` with a fake agent
that writes the files and runs the reporting commands a real agent would.

The model still has to exist - only a model turn can invoke the `Agent` tool
or `AskUserQuestion` - but it no longer decides anything.

## Agents report through the CLI

An agent's result reaches `tasks.yaml` by the agent running a command, not
by the orchestrator reading its reply. Every dispatch prompt is rendered by
`dispatch.py` with the exact command baked in, interpreter and `--repo`
pinned so it works from an executor's own worktree in any shell.

This removed the last place the orchestrator model had to parse prose, and
it made the claims checkable. `report --status DONE` is refused when the
worktree is dirty, when HEAD has not moved from the slice's base, or when a
test file the brief declares does not exist. The failure it exists to catch
is the slice that reports DONE with uncommitted work and so contributes
nothing to the merge - previously invisible until verification, at the cost
of a whole cycle.

## Two halves: script and judgement

Every mechanical operation lives in `scripts/codag.py`. Agents call it
rather than running git themselves.

| Deterministic (scripts) | Judgement (agents) |
| --- | --- |
| preflight, run directory, state | what to ask the user |
| stack detection | how to split the work |
| plan validation, DAG, ownership overlap | how to implement a slice |
| worktree create/remove/reap | how to resolve a conflict |
| ordered branch merge | whether a criterion is met |
| gate execution and baseline comparison | what to change after a failure |
| review packages, briefs, ledger | |

The split is not just tidiness. Anything scripted is testable without a
model, runs identically on every OS, and costs nothing to repeat. The
end-to-end test drives the entire pipeline with no LLM at all — that test
passing is what proves the spine works on a given platform.

## One base for the whole run

A run forks from the **base branch** - config, else `origin/HEAD`, else
`main`, else `master` - not from wherever the user happens to be standing.
The baseline gates measure it, every slice branch forks from it, and the
feature branch is based on it, so the final diff is exactly `base..feature`:
the thing you would open a pull request with. Mixing bases would drag the
user's unrelated commits into that diff and measure the baseline against a
different tree from the one being tested.

Standing on a branch with commits the base lacks is common and not an error,
so `init` names those commits and proceeds. Finding that out before
executors start is the point.

The branch is named at the start of `execute`, not at `init`, because
`branch_template` can reference `{kind}` and `{slug}` - and `kind` is the
planner's call, which does not exist until the plan does. Until then the run
uses a provisional `codag/<run-id>/integration`, which `codag branch`
renames. `git branch -m` updates the integration worktree's HEAD for us, so
nothing has to be recreated.

## Making parallel execution safe

Three independent agents editing one repository is the whole value and the
whole risk. Four mechanisms contain it:

**Disjoint ownership, checked before dispatch.** Each slice declares `owns`
globs. `schema.overlaps` decides whether two glob patterns could ever match
the same path, and the validator rejects any plan where two slices in the
same wave overlap. The overlap test is deliberately conservative: a false
"these collide" costs one planner revision, a false "these are fine" costs a
corrupted merge. It compares literal prefixes to separate `src/auth/**` from
`src/mail/**`, and literal suffixes so `**/*.test.ts` and `**/*.spec.ts` are
correctly seen as disjoint.

**Separate worktrees off a fixed base.** Branches start at the commit
recorded at `init`, not at HEAD, so the run stays reproducible even if the
user moves their branch mid-flight.

**Waves.** Slices with dependencies wait. `wave next` only returns slices
whose dependencies are `done` or `carried`, so an interface is always
published before anything is written against it.

**Append-only shared paths.** Some files genuinely have to be touched by
several slices — a route registry, a migrations directory. Those go in
`touches_shared`, executors may only append, and the synthesizer reconciles
what lands.

## The baseline

A repository with one pre-existing lint error would otherwise fail
verification on every cycle, forever, for a fault the pipeline did not
cause. So `init` runs the gates once against the base commit and stores the
result. `gates.classify` then splits every later failure into `regressions`
(this run caused it — blocking) and `pre_existing` (inherited — reported,
not blocking).

The baseline runs in the integration worktree, which is created at `init`
for exactly this reason: it starts at the base commit, which is what a
baseline needs, and it is the same worktree the merge later targets — so the
dependency install is paid once instead of twice.

## Why the E2E test is written last, and from the spec

Every slice proves itself in isolation and the verifier judges the merged
diff, but until the `e2e` phase nothing drives the finished feature the way
a user would.

Writing that test after the code is the only option - it cannot exist before
the feature does - and it carries a specific hazard: a test written by
reading the implementation restates the implementation, passes forever, and
will happily assert a bug in detail. So the E2E agent is handed the spec and
the acceptance criteria and told explicitly not to read the diff to decide
what to assert. It reads code only to find the entry point.

It may touch test files only. The feature has just earned a passing verdict;
changing production code now would invalidate that judgement behind the
verifier's back. If the test can only pass by altering the implementation,
that is the finding, and the agent reports `FAILED`.

`FAILED` stops the run rather than triggering a replan. A brand-new
end-to-end test that fails is far more often the test's fault - a selector,
a wait, a fixture - than the feature's, and letting it into the replan loop
would spend cycles rewriting working code.

A `kind: bugfix` run skips the phase. Its slices already had to be written
test-first, enforced from git history, and a second layer for a one-line fix
is the overhead the classification exists to avoid.

## Why the synthesizer is last-resort

Merging is mechanical until it isn't. `merge.run_merge` creates the
integration branch and merges every slice branch in dependency order,
stopping at the first conflict. A clean merge never wakes an agent at all.

When it does stop, the synthesizer has the narrowest brief in the system:
resolve the conflict, wire things together, justify every edit outside a
conflict hunk in a table the verifier reads. The failure mode being defended
against is an agent that "helpfully" rewrites a slice while integrating it —
which silently destroys work that already passed its own tests.

`merge --continue` refuses while conflict markers remain, and refuses again
if git still reports unmerged paths after staging. Both checks exist because
the natural agent behaviour — edit the file, declare victory — leaves the
merge uncommitted.

## Cross-OS guarantees

Not best-effort. Enforced in code and in CI (`windows`, `macos`, `ubuntu` ×
Python 3.9 and 3.13):

- **Stdlib only.** `miniyaml` is a hand-written reader/writer for the
  documented tasks.yaml subset, so there is no pip install anywhere.
  Unsupported YAML is rejected with a `line:col` error rather than
  mishandled — the planner gets a precise message and fixes it.
- **No shell, ever.** Every subprocess call takes an argument list with
  `shell=False`. A test asserts no module enables the shell and that only
  `osenv` imports `subprocess`.
- **Executable resolution.** On Windows `npm`, `pnpm` and `yarn` are `.CMD`
  shims, and `CreateProcess` cannot launch a bare `npm`. `osenv.run`
  resolves `argv[0]` through `shutil.which`, which honours `PATHEXT`.
- **Short worktree paths.** `<tempdir>/codag/<8 hex>/<slice>` adds under 30
  characters, so a deep `node_modules` stays clear of the 260-character
  limit. `core.longpaths=true` is set on Windows as a second line of
  defence.
- **Force cleanup.** `rmtree_force` clears the read-only bit and retries
  with backoff, because antivirus and editor handles routinely hold files
  open for a moment on Windows.
- **Locking without platform APIs.** `FileLock` uses atomic directory
  creation rather than `fcntl` or `msvcrt`, so concurrent executors can
  update `tasks.yaml` from separate processes on any filesystem.
- **Nothing in the working tree.** Run state is hidden via
  both `.git/info/exclude` and the project's `.gitignore`; the first hides
  it immediately and locally, the second is what a team sees. `init` writes
  the `.gitignore` entry but never commits it, and preflight recognises its
  own block so the next run is not blocked by the change
  the pipeline promised not to touch.

## Two logs, for two readers

`ledger.md` is per-run machine bookkeeping: one line per step, used to
recover after a crash or a compaction. `progress.txt` is cross-run and
written for a reader - one entry per completed run.

They exist separately because they answer different questions. The ledger
answers "what has already happened in this run, so I do not redo it". The
progress log answers "what does this codebase do that surprised the last
person who touched it".

Only the second is worth a model's time, which is why an agent writes it and
a script writes the ledger. The append itself is scripted either way: an
instruction to never overwrite a file is exactly the kind of instruction
that eventually gets ignored.

## Context discipline

Everything pasted into a dispatch, and everything an agent prints back,
stays in the orchestrator's context for the rest of the session and is
re-read on every later turn. So artifacts move as **files**: briefs, reports,
review packages, gate results, verdicts. Agents return short receipts and
write detail to disk. `verify-package` exists so the verifier can be handed
one set of paths instead of the orchestrator assembling and quoting them.

## Recovery

`state.json` and `ledger.md` are the recovery map. The expensive failure
this prevents is an orchestrator that loses its place after a compaction and
re-dispatches an entire completed wave. `codag resume` prints what is
actually done; the orchestrator is instructed to trust it and `git log` over
its own recollection.
