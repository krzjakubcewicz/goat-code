---
name: cod-ag-conventions
description: Shared contracts for the cod-ag pipeline - run directory layout, the tasks.yaml dialect, agent status codes, report formats and the codag CLI. Read this when working as any cod-ag agent (planner, executor, synthesizer, verifier, replanner) or when the orchestrator needs the exact shape of an artifact.
---

# cod-ag Conventions

One place for the contracts every cod-ag agent depends on, so they are
defined once instead of drifting across five agent files.

## The pipeline

```
init -> grill -> plan -> validate -> approve -> execute (waves)
     -> synthesize -> verify -> DONE | replan -> execute ...
```

The **orchestrator is the main Claude Code thread**, not an agent: only the
main thread can spawn subagents and ask the user questions. Planner,
executor, synthesizer, verifier and replanner are subagents.

## The `codag` CLI

Everything mechanical is a script. Never do by hand what the CLI does.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/codag.py" <command> [--json]
```

| Command | Purpose |
| --- | --- |
| `init --prompt "..."` / `init --spec FILE` | preflight, run dir, stack detection, baseline gates |
| `plan validate` / `plan show` / `plan waves` | gate and inspect tasks.yaml |
| `wave next` | slice ids dispatchable right now, capped at the parallel limit |
| `worktree create S1 S2` / `worktree reap` | isolated checkouts |
| `brief S1` | write a slice's self-contained brief |
| `task status S1 done` / `task set S1 field value` / `task commits S1 --head SHA` | mutate the plan atomically |
| `merge` / `merge --continue` | integrate slice branches |
| `gates run` | build/typecheck/lint/test, classified against the baseline |
| `verify-package` | everything the verifier needs, in one call |
| `diffpkg` | one-file review package |
| `ledger "..."` | append to the durable progress ledger |
| `cycle` | advance to the next replan cycle |
| `status` / `resume` / `finish` / `abort` | lifecycle |

Exit codes: `0` success, `1` the pipeline is not in a good state (act on
it), `2` you called it wrong.

Add `--json` for machine-readable output; it works before or after the
subcommand.

## Run directory

Everything lives in the target repo under `.codag/`, hidden from git via
`.git/info/exclude` so the user's working tree is never dirtied.

```
.codag/runs/<run-id>/
  spec.md              the spec, plus appended "## Clarifications (round N)"
  stack.json           detected languages, frameworks, commands, specialists
  state.json           phase, cycle, base commit, branches, worktrees
  ledger.md            append-only progress; the recovery map
  baseline-gates.json  gate results at the base commit
  tasks.yaml           THE plan - every agent reads this
  cycle-N/
    briefs/S1.md   reports/S1.md
    merge-report.md   gates.json   review.diff   verdict.md
```

Worktrees are **outside** the repo, at `<tempdir>/codag/<8 hex>/<slice-id>`.

## tasks.yaml dialect

Parsed by a stdlib-only reader, so the supported subset is deliberately
small. Supported: block mappings and sequences, plain and quoted scalars,
`null`/`true`/`false`/numbers, block literals (`|`, `>`), single-line flow
collections (`[a, b]`, `{a: 1}`), `#` comments.

**Not supported, and rejected with a `line:col` error:** anchors (`&`),
aliases (`*`), tags (`!`), merge keys (`<<:`), multiple documents, tabs for
indentation. If a value starts with `*`, `&`, `!`, `-`, `[`, `{`, `#`, or
contains `: `, quote it.

```yaml
version: 1
run_id: 20260822-114900-magic-link
cycle: 1
goal: Users sign in with a magic link emailed to them.
global_constraints:          # bind every slice; copied verbatim from the spec
  - "Node >= 20, no new runtime deps"
assumptions:                 # unresolved after grilling; the verifier surfaces these
  - "Token TTL assumed 15 min (not specified)."
slices:
  - id: S1                   # letters, digits, . _ - ; unique
    title: Magic-link token store
    intent: Persist single-use, expiring login tokens.
    depends_on: []           # [] means wave 1
    owns:                    # EXCLUSIVE globs - no two slices in a wave may overlap
      - "src/auth/tokens/**"
      - "tests/auth/tokens/**"
    touches_shared:          # append-only; the synthesizer reconciles these
      - "src/db/migrations/"
    interfaces:              # signatures this slice publishes; other slices code to them
      - "createToken(email: string): Promise<Token>"
    uses_interfaces:         # must be published by a slice in depends_on
      - "sendMail(to: string, body: string): Promise<void>"
    acceptance:              # checkable assertions, at least one
      - id: A1
        text: "consumeToken returns the email on first call and null on second."
    tests:                   # at least one
      - path: "tests/auth/tokens/store.test.ts"
        must_cover: ["single use", "expiry boundary"]
    out_of_scope: ["email delivery", "UI"]
    model: sonnet            # opus | sonnet | haiku | fable | inherit
    status: pending          # pending | claimed | done | blocked | failed | carried
    branch: null             # filled in by `worktree create`
    worktree: null
    commits: {base: null, head: null}
```

### What `plan validate` rejects

- missing top-level keys, unsupported `version`, empty `goal`
- duplicate, missing or malformed slice ids
- `depends_on` naming an unknown slice, itself, or forming a cycle
- **two slices in the same wave whose `owns` globs can match the same path**
- a slice with zero acceptance criteria or zero test paths
- an acceptance criterion missing `id` or `text`
- `uses_interfaces` naming something no slice publishes, or published by a
  slice this one does not depend on
- a `touches_shared` path that another slice claims in `owns`

Warnings (non-blocking): more than 8 acceptance criteria in a slice, more
than 6 slices in a wave, cross-wave ownership overlap, a missing `intent`.

## Slice discipline

These four rules are what make the parallel executor wave safe.

1. **Vertical, not horizontal.** A slice cuts through data, logic and
   surface to deliver one user-visible capability, and it builds and tests
   green on its own. "Add the database layer" is not a slice.
2. **Exclusive ownership.** Each slice owns a disjoint set of paths.
   Anything genuinely shared goes in `touches_shared` and is append-only.
3. **Fixed interfaces.** A slice publishes signatures before its dependents
   are written; those names and shapes cannot drift afterwards.
4. **Checkable acceptance.** Each criterion is an assertion someone can
   confirm from the diff and the tests. "Handles errors well" is not one.

## Agent status codes

Executors return exactly one:

| Status | Meaning | Orchestrator's move |
| --- | --- | --- |
| `DONE` | criteria met, tests green, work committed | verify and mark done |
| `DONE_WITH_CONCERNS` | finished, but something needs a human's eye | read the concerns, then proceed |
| `NEEDS_CONTEXT` | missing information, named precisely | supply it, re-dispatch |
| `BLOCKED` | cannot finish, reason named precisely | escalate the model once, then re-plan |

Never re-dispatch an unchanged `BLOCKED` task to the same model. Something
has to change: more context, a stronger model, or a smaller task.

## Report contract

Agents write detail to **files** and return only a short summary. Anything
returned in the message stays in the orchestrator's context for the rest of
the session; anything written to a file does not.

An executor returns:

```
STATUS: DONE
COMMITS: a1b2c3d..e4f5g6h (4 commits)
TESTS: 7 passed, 0 failed (pnpm run test)
CONCERNS: none
REPORT: .codag/runs/<id>/cycle-1/reports/S1.md
```

## Writing style

Agent-to-agent **reports, receipts and status lines are caveman-terse**:
drop articles and filler, fragments are fine, lead with the answer. That is
where the token savings are.

**Write normally** — full, precise prose — for: code, commit messages'
meaning, acceptance criteria, interface signatures, questions to the user,
and anything in `tasks.yaml`. Compression there costs correctness.

Commit messages use caveman style: `add token expiry check`, not
`This commit adds a check for token expiry.`

## Skills to use

- `superpowers:test-driven-development` — executors, always
- `superpowers:writing-plans` — planner, for task right-sizing
- `superpowers:brainstorming` and `grilling` — planner, for the question rounds
- `superpowers:systematic-debugging` — replanner, failure to root cause
- `superpowers:verification-before-completion` — verifier
- `ponytail:ponytail` (lite) — executors and the synthesizer, against gold-plating
- the `engineering-skills:senior-*` skill named in `stack.json` — executors
