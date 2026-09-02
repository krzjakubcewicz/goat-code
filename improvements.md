# cod-ag: telemetry-driven improvements

Derived entirely from `telemetry/` — 11 real runs (8 completed, 2 aborted restarts, 1
stillborn) that built an eight-phase Django/DRF + React application, 177 subagent
transcripts, and the cross-run `progress.txt`. Every number below is reproducible from
that tree; each finding names its source path.

The pipeline works. Every completed run reached `done` with a passing verdict, no run
looped forever, no run claimed a success it did not have. What follows is about the tax
it pays to get there.

---

## 1. The headline: cycle 1 has never passed

Source: `telemetry/codag/runs/*/cycle-*/verdict.md`, `*/state.json`, `*/tasks.yaml`.

- 21 verdicts across 8 completed runs: **13 FAIL, 8 PASS**. Every run's cycle 1 failed.
- 5 of 8 runs consumed the full `max_cycles: 3` budget.
- 44 initial slices against **28 remedial slices — 39% of all executor dispatches are rework**.

And the rework is tiny. Diff size by cycle (`cycle-N/review.diff`):

| run | c1 | c2 | c3 | final-cycle delta |
|---|---|---|---|---|
| phase-1 foundation | 3191 | 3379 | 3442 | **+63** |
| phase-2 exercise library | 3557 | 3823 | 3923 | **+100** |
| phase-3 workout logging | 2901 | 3083 | — | **+182** |
| phase-4 PRs / percentages | 9004 | 9564 | 9589 | **+25** |
| phase-5 complexes | 7473 | 7972 | 8040 | **+68** |
| phase-6 coaching | 4858 | 4908 | — | **+50** |
| phase-7 analytics | 4971 | 5100 | — | **+129** |
| phase-8 notifications | — | 5955 | 6168 | **+213** |

A full replan → worktree → executor → merge → gates → opus-verifier lap buys, typically,
about two assertions.

---

## 2. Why cycle 1 fails: assertion strength, not broken code

The verdicts say it in their own words:

> "All three are test-only. No production code needs to move." — phase-5, cycle 2

> "Two missing assertions. Nothing needs redesigning, and no code needs deleting." — phase-7, cycle 1

> "Both gaps are test-only — no production code needs to change, and both are in one slice." — phase-8, cycle 2

> "Nothing else blocks. Everything outside those two pieces of test work is implemented, tested and green." — phase-4, cycle 2

The recurring defect shapes, across every run:

- `count() >= 1` where the criterion says *exactly one*
- `len(...) >= 2` where the criterion names the two entries literally
- asserting a serialized in-memory object instead of reading back through `GET`
- testing the helper (`enqueueMutation`, `drainQueue`) instead of rendering the component the criterion describes
- asserting counts where the criterion is about placement (`within()` the right group)
- `assert ids() == ids()` — a tie-break test that cannot fail

None of these are implementation defects. The code was right; the proof was not.

### The rubric exists, and the executor has never seen it

`agents/codag-verifier.md` carries a section titled **"How to judge a criterion"**:

- *"A criterion with no test is not met."*
- *"A test that asserts nothing is not a test."*
- *"Check exact values literally."*
- *"If the criterion says 'returns null on the second call', find the test for the second call."*

That text appears in exactly one file:

```
$ grep -c "How to judge" agents/codag-executor.md skills/cod-ag-conventions/SKILL.md agents/codag-verifier.md
agents/codag-executor.md:0
skills/cod-ag-conventions/SKILL.md:0
agents/codag-verifier.md:1
```

The executor's own pre-report checklist says only *"every acceptance criterion is
demonstrably satisfied"*. It is graded on a rubric it was never given. This is the
single highest-leverage gap in the system, and closing it costs no code.

---

## 3. The cross-run feedback loop does not close for this class

Source: `telemetry/codag/progress.txt` (8 entries, 43 KB).

Scoring each entry's learnings by theme:

| theme | run 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| test-only / assertion gap | 2 | 1 | 5 | 3 | 7 | 3 | 5 | 5 |
| gate / stack detection | 3 | 3 | 2 | 0 | 0 | 0 | 0 | 0 |
| cross-slice contract | 0 | 1 | 0 | 3 | 1 | 0 | 0 | 2 |

The assertion-gap learning is written down after **every single run**, and the failure
recurs after **every single run**. The gate/stack learning appears in runs 1–3 and then
stops — because that one was fixed *in code*, not in prose.

**Prose learnings do not change behaviour. Code and schema changes do.** That is the
governing lesson of this telemetry, and it is what ranks the change list below.

The scribe writes genuinely good analysis — phase-8's entry correctly diagnoses both the
health-endpoint contradiction and the primitive-vs-feature testing gap in detail. It is
read by the planner and then not acted on, because nothing forces the action.

---

## 4. The cheap gate is not aimed at the expensive failure

Source: `telemetry/codag/runs/*/cycle-*/gates.json`, `*/stack.json`.

- Gates reported a genuine regression in **1 of 21 cycles (4.8%)** — phase-3 cycle 1.
  The verifier failed **13 of 21 (62%)**.
- `build` and `typecheck` are `null` in **all 11** `stack.json` files. The only gates ever
  detected are `test` and `lint`.
- The frontend half of the monorepo is never gated (`project_dir: backend`), so the
  verifier ran `npm test` by hand in phases 4, 6 and 7 and said so in the verdict:
  *"Fresh evidence I ran myself, because no gate covers the frontend."*
- Phase 1 and phase 2 both ran with `test` and `lint` reported **missing** — the pipeline
  had no automated safety net at all for two full runs. (Fixed on the current branch.)

The deterministic layer is fast, cheap and repeatable, and it is pointed at a failure mode
that almost never occurs, while the expensive opus judgement absorbs the one that always
does.

---

## 5. There is no incremental verification

The phase-8 cycle-3 verifier dispatch (`cycle-3/dispatch/verifier.md`) reads:

> "Read all of these … the whole integration diff … **52 criteria across 8 slices. Every
> one needs a verdict and evidence.**"

The delta since the cycle-2 verdict was 213 diff lines, from one remedial slice (S8) that
changed **zero production files**. 51 of the 52 criteria had already been marked ✅ against
unchanged code.

9 of 26 verifier dispatches are cycle 2 or later. Each is opus, median 98 turns and 6.5M
cache-read tokens, re-reading a 3k–9.6k-line diff from scratch.

---

## 6. Cost shape

Source: `telemetry/claude/projects/*/subagents/*.jsonl` (177 transcripts).

| role | n | wall h | median turns | median min | cache-read |
|---|---|---|---|---|---|
| executor | 90 | 16.9 | 183 | 9.2 | 1095M |
| verifier | 26 | 3.5 | 98 | 8.5 | 165M |
| e2e | 8 | 1.7 | 180 | 11.7 | 74M |
| planner | 23 | 2.0 | 71 | 4.2 | 40M |
| synthesizer | 8 | 0.9 | 142 | 6.9 | 32M |
| replanner | 13 | 1.1 | 82 | 4.3 | 31M |
| scribe | 8 | 0.4 | 54 | 2.1 | 11M |

**1.45B cache-read tokens, ~26.5 agent-hours.** Executors are 76% of it.

**Turn count is the cost driver, not tool latency.** Shell execution accounts for only
**6.9h of 26.5h (26%)**:

| shell category | n | median s | p90 s | total h |
|---|---|---|---|---|
| `docker compose run` (test/lint) | 909 | 7.4 | 33.4 | 3.66 |
| other shell | 2646 | 1.6 | 2.6 | 1.53 |
| git | 1267 | 1.7 | 2.4 | 0.69 |
| frontend tests | 254 | 3.9 | 6.5 | 0.30 |
| install | 28 | 14.0 | 18.6 | 0.11 |

909 container launches to run tests. One executor ran the **identical full-suite command
25 times**; four others 16–23 times. The brief names one Test command and it is the whole
suite, so the red-green loop pays for the whole suite on every iteration.

**Context discipline holds.** Orchestrator `Agent` receipts are capped at 1075 characters,
every time. The design goal in `ARCHITECTURE.md § Context discipline` is met — this is not
where the money goes, and it should not be touched.

---

## 7. Model policy is decided by the model, not the machine

`templates/config.yaml` sets `executor: haiku`, `executor_escalated: sonnet`, and
`escalations` is `{}` in **every** `state.json` — the escalation path never fired.

Yet the orchestrator's actual `Agent` calls were:

| subagent | model requested | n |
|---|---|---|
| codag-executor | sonnet | 62 |
| codag-executor | haiku | 25 |
| codag-executor | opus | 3 |
| codag-verifier | opus | 26 |
| codag-planner | opus | 23 |

`ARCHITECTURE.md` states *"the model still has to exist … but it no longer decides
anything."* Model choice is a live exception: 65 of 90 executors ran escalated without a
single escalation being recorded, so the escalation accounting is blind and the cost model
is wrong by roughly 3×.

---

## 8. There is no timing telemetry

`debug: false` in all 11 runs → **0 `log.txt` files** anywhere in the tree.

`ledger.md` timestamps are the only phase data, and they are contaminated by human idle —
one gap is 107 hours (the user walked away for days). One pattern does survive the noise:
the leg from *last slice done* to *e2e pass / next cycle* takes 4.0–5.0h in six of eight
runs, while the verifier subagent itself is a 9-minute job. Where the rest of that time
goes is currently unknowable.

Every improvement below is unmeasurable until this is fixed.

---

## 9. Two small defects

**`scripts/codag/dispatch.py:339`** renders assumptions with `"- {}".format(assumption)`.
When an assumption is a mapping, the verifier dispatch receives a raw Python dict repr:

```
- {'Cycle 2 verdict, S3/A2 and S3/A4': 'the offline production code is complete and …'}
```

**Duplicate ledger append.** Phase-8's `ledger.md` records `cycle 3: scribe written` twice,
7 seconds apart — a missing idempotency guard on the append.

---

# The change list

Ranked by measured impact × cost to build. All have landed; each names the files it
touched and the tests that hold it.

### 1. Give the executor the verifier's rubric — ✅ **landed**

Extract "How to judge a criterion" from `agents/codag-verifier.md` into a named
**Evidence standard** section in `skills/cod-ag-conventions/SKILL.md`. Have both
`agents/codag-executor.md` and `agents/codag-verifier.md` cite that one section, and render
it into every brief via `scripts/codag/brief.py`. Add one line to the executor's *Before you
report DONE* list:

> For each acceptance criterion, name the test `path:line` that would fail if the
> behaviour were wrong. If you cannot name one, the criterion is not met.

Targets the cause of 13 of 13 failures.

**Landed.** `skills/cod-ag-conventions/SKILL.md` now carries an **Evidence standard**
section — the verifier's rules plus the specific defect shapes the telemetry showed (exact
counts, read back through the real surface, drive the thing the criterion names, placement
not counts, every clause). `agents/codag-executor.md` and `agents/codag-verifier.md` both
cite it, and `scripts/codag/brief.py` renders it into every brief as **The evidence bar**
alongside the slice's own criterion ids.
`test_plugin.py::test_the_evidence_standard_is_defined_once_and_cited_by_both_sides` fails
if either side stops citing it.

### 2. Make evidence a `report` precondition, not an instruction — ✅ **landed**

`report` takes one `--evidence <criterion-id>=<path>:<line>` per acceptance criterion, and
refuses a `DONE` that is missing one, names a file that does not exist, names a line past
the end of that file, or names a criterion the slice does not have. This is the
choke-point pattern the codebase already uses for clean-worktree, HEAD-moved and
test-file-exists — and `ARCHITECTURE.md` already argues for it: *"an instruction to never
overwrite a file is exactly the kind of instruction that eventually gets ignored."* The
same is true of an instruction to test properly.

The pairs are stored on the slice as `report.evidence` in `tasks.yaml`, so the verifier
reads the executor's claim as data and checks it rather than rediscovering it — which is
also what item 4 needs. A `DONE_WITH_CONCERNS` still goes through, with the missing
evidence folded into the concerns rather than hidden, mirroring how the TDD check already
behaves.

Files: `scripts/codag/report.py` (`evidence_findings`), `tasks.py` (`criterion_ids`),
`codag.py` (`--evidence`), `dispatch.py` and `brief.py` (render the exact command with the
slice's real ids). Seven new tests in `test_report.py` cover the gate; the end-to-end fake
executor now names evidence like a real one.

### 3. Instrument before optimising — ✅ **landed**

Nothing below could be shown to have worked without this.

`Run.set_phase` now appends `phase X -> Y` to `ledger.md` on every transition,
unconditionally — not behind `debug`, which was never switched on in any of the eleven
recorded runs. `codag stats` reads that ledger back into per-phase durations, cycle count,
first-cycle versus remedial slice counts, per-cycle verdicts and gate outcomes. It derives
everything from artifacts the pipeline always writes, so it works on runs that finished
long before it existed.

Files: `scripts/codag/stats.py` (new), `run.py`, `ledger.py` (`now=` for testability),
`codag.py`. Tests: `test_stats.py` (10), plus two in `test_run.py` for the transition
lines and the no-op guard.

### 4. Incremental verification — ✅ **landed**

Each cycle's `gates.json` records the ref it judged, so the delta between two cycles is
exactly `previous ref .. this ref`. `diffpkg.previous_judgement` computes that, and
`tasks.unchanged_slices` names the slices owning none of those paths — their code is
byte-identical to what the verifier already ruled on.

The verifier's dispatch then gains a **What changed since your last verdict** section: its
previous verdict's path, the files that moved, and the slices to carry forward verbatim.
It re-judges everything else, plus every ❌ and ⚠️ wherever they live. Carrying forward is
not skipping — each criterion still appears in the table with a verdict and evidence.

It never claims a criterion passed, only that its code did not change. Every branch falls
back to judging everything: narrowing wrongly would hide a regression, not narrowing only
costs what the pipeline already pays.

Files: `scripts/codag/diffpkg.py`, `tasks.py`, `dispatch.py`, `machine.py`, `codag.py`,
`agents/codag-verifier.md`. Tests across `test_tasks.py`, `test_dispatch.py`,
`test_machine.py`, `test_cli_reporting.py`.

### 5. Aim the gates at the real failure — ✅ **landed**

**(a) Both halves of a monorepo.** `stack.detect` records `sibling_projects` — the build
systems beside the one the gates were pointed at, each detected in full. `gates.run_all`
runs their gates too, keyed `test [frontend]`, classified against the baseline like any
other. A sibling directory absent from a worktree is skipped rather than reported missing,
so it never reads as a regression.

**(b) A weak-assertion scan.** `gates.weak_assertions` reads the test files this run
changed and flags `count()`/`len()` compared with an inequality, both sides of an equality
being the same expression, and test bodies with no assertion at all. It lands in
`gates.json` as `weak_assertions` and reaches the verifier as **leads, not findings** —
explicitly told a regex cannot judge whether an assertion proves its criterion, and to say
nothing about the ones that are fine. Never blocking.

Files: `scripts/codag/gates.py`, `stack.py`, `dispatch.py`, `codag.py`, `machine.py`.
Tests: 12 in `test_gates.py`, 3 in `test_stack.py`, 2 in `test_dispatch.py`.

### 6. Cross-slice contract check at plan validation — ✅ **landed**

Slices declare `changes_contract:` — the shapes they *redefine* for everyone, as opposed
to the new ones `interfaces` publishes. Two slices declaring the same contract in the same
wave is now a validation **error**: they cannot both define it, and one must depend on the
other. Across waves it is a warning, because the later slice can be written against what
the earlier one published and the planner saying so is what makes that deliberate.

The conventions skill tells the planner to grep the whole tree for existing assertions on
a shape before declaring it — including tests no diff will touch, which is exactly what
the phase-8 write-off turned on.

Files: `scripts/codag/schema.py`, `skills/cod-ag-conventions/SKILL.md`. Tests: 6 in
`test_schema.py`.

### 7. Stop the full-suite loop inside executors — ✅ **landed**

`stack.detect` emits `commands.test_one` — the suite narrowed to one file, with a `{path}`
token — for pytest, vitest and jest. `brief.py` renders it once per test path the slice
declares, so the red-green loop runs one file, and names the whole suite once before
reporting, because a green slice that breaks something else is not done. Runners that take
no path get the suite, as before.

Files: `scripts/codag/stack.py`, `brief.py`. Tests: 3 in `test_stack.py`, 2 in
`test_cli.py`.

### 8. Enforce the dispatch's `model` — ✅ **landed**

`machine._log_action` — the one choke point every dispatch passes through — now writes
`dispatch <agent> <slice> on <model>` to the ledger rather than only to the debug trace
that was never on. The choice is on the durable record, so a substitution is visible
instead of silent.

`skills/cod-ag-orchestrator/SKILL.md` states that each entry's `model` is used verbatim:
not a suggestion, not a default to improve on, and a slice that genuinely needs more is
what `BLOCKED` is for.

Files: `scripts/codag/machine.py`, `skills/cod-ag-orchestrator/SKILL.md`. Tests: 2 in
`test_machine.py`.

### 9. Promote recurring learnings out of prose — ✅ **landed**

`.codag/constraints.md` holds rules promoted out of the narrative. `progress show` now
gives the **planner's view** — every standing constraint plus the last five entries — and
`--all` the whole file. `progress promote "<rule>"` appends one, never duplicating.

The scribe's dispatch carries the rule that makes it work: *a learning you are writing for
the second time is a rule, not a note.* If a run hit something an earlier entry already
warned about, writing it again is pointless — that entry was read and it did not change
the outcome. Promote it instead, at most two per run, phrased as a rule that binds a plan
rather than a story about this run.

Files: `scripts/codag/progress.py`, `dispatch.py`, `codag.py`,
`skills/cod-ag-conventions/SKILL.md`. Tests: 9 in `test_progress.py`, 1 in
`test_dispatch.py`.

### 10. Two small fixes — ✅ **landed**

- `dispatch._assumption_text` renders a mapping assumption as `key: value` prose. The
  verifier no longer receives a raw Python dict repr.
- `ledger.append` refuses a line identical to the one before it, timestamp aside. The
  recovery map stops claiming a step happened twice when it happened once.

Tests: 2 in `test_dispatch.py`, 2 in `test_run.py`.

### 11. Fill the stack once the run has built it — ✅ **landed**

`init` detects the stack against the base commit, but the first run of a new project is
building the thing that would be detected. Phase 1's own `stack.json` records the manual
workaround in its notes:

> "detected at init against an empty repo; re-wired by the cycle-2 replanner once the
> stack existed"

Phases 1 and 2 both reached a verdict with every gate reporting `missing`, which is what
the phase-1 verdict meant by *"detection has to find make test and make lint, or this
pipeline has no automated safety net at all."*

`stack.fill_gaps(where, path)` re-detects and persists, but only into gaps: a profile that
can already gate is untouched, and any command the stored profile carried survives, so a
hand-tuned command is never replaced by a guessed one. Two call sites, both where the new
code first exists:

- `codag brief` — a slice's worktree starts at the integration tip, so by wave 2 the build
  system wave 1 created is there. Wave 2 stops guessing.
- `codag verify-package` — the integration worktree, which is the tree the gates run in.
  Whatever the waves built, the gates now see.

A repo where nothing is detected also stops claiming `agents must infer commands from the
repo` and says the stack will be re-detected once the run has built it.

Files: `scripts/codag/stack.py`, `codag.py`. Tests: 7 in `test_stack.py`, 4 in
`test_cli_reporting.py`.

## Already fixed on the current branch

Not re-proposed here. `fix/stack-detection-and-remedial-worktree-base` carries:

- `508e42f` — detect a build system that lives one level down
- `53ef308` — start a slice branch at the integration tip, not the base commit
  (this is the phase-2 `R3 blocked` write-off: three remedial worktrees rooted at the
  pre-cycle-1 base commit, with every dependency's code absent)
- `6ba34ea` — run the gates where the toolchain actually lives

Phase-1 and phase-2 verdicts both demanded these explicitly. The telemetry confirms the
symptom stops after run 3.

---

## How to reproduce these numbers

```bash
# verdict tally
grep -l "VERDICT: FAIL" telemetry/codag/runs/*/cycle-*/verdict.md | wc -l   # 13
grep -l "VERDICT: PASS" telemetry/codag/runs/*/cycle-*/verdict.md | wc -l   # 8

# the rubric gap
grep -c "How to judge" agents/codag-executor.md skills/cod-ag-conventions/SKILL.md agents/codag-verifier.md

# gates detected
python -c "import json,glob;[print(f, json.load(open(f)).get('commands')) for f in glob.glob('telemetry/codag/runs/*/stack.json')]"

# no debug trace was ever written
find telemetry/codag/runs -name log.txt | wc -l   # 0
```

Per-role token and turn figures come from the `usage` blocks and timestamps in
`telemetry/claude/projects/*/subagents/*.jsonl`, keyed by `agentType` in the sibling
`.meta.json`.

> `telemetry/` is gitignored (`.gitignore:10`) and must stay that way — the transcripts
> carry full prompts, verbatim project file contents and the account email.
>
> Its presence on disk also broke `test_plugin.py`, whose `MARKDOWN` glob walked the whole
> tree and read copied run artifacts as plugin files. That glob now skips every
> gitignored directory, so a local telemetry dump, a `.venv` or a leftover `.codag/`
> cannot be cross-referenced as plugin source.
