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
  `.git/info/exclude`, not `.gitignore`, so `init` cannot dirty the branch
  the pipeline promised not to touch.

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
