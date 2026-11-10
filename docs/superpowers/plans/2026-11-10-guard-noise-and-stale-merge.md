# Guard noise and stale merge record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a rejected interpreter's stderr and goat-code's own generated interpreter path from producing false guard refusals, and stop a stale `merge` record skipping `synthesize` in cycles 2+.

**Architecture:** Three independent fixes from `bug.md`. The hook shim auditions each interpreter before handing it the payload. `guard.allowed_roots` trusts the directory of the interpreter running the guard. `Run.advance_cycle` drops the previous cycle's merge record so `derive_phase` cannot read it as this cycle's.

**Tech Stack:** POSIX sh, Python 3.9+ stdlib, pytest.

**Spec:** `bug.md` (issues 1, 2 and the unrelated observation).

## Global Constraints

- Stdlib only, Python 3.9+; no `shell=True`; only `osenv` imports `subprocess` (tests may).
- Must pass on ubuntu/macos/windows. Run `python -m pytest scripts/tests -v`, never with `-n`.
- Never touch the user's repo state.
- Commit subjects and comments: lowercase, declarative, explain why.
- The audition must use `</dev/null`: the hook payload is on stdin exactly once and belongs to the guard.

## Review Focus

- A real interpreter whose `-c ''` succeeds must still receive the full payload on stdin (test: existing verdict assertions stay green).
- A candidate that passes the audition but exits something other than 0/2 must still fall through to the next name (existing status check stays).
- `allowed_roots` must not admit sibling directories of the interpreter dir, or `C:/Windows/system32/drivers/etc/hosts` stays refused.
- `sys.executable` empty or `None` (embedded Python) must not crash `allowed_roots`; guard fails open elsewhere, so skip it.
- Cycle 1 with no merge record, and a direct run that never merges, must behave as before.

---

### Task 1: Audition interpreters in the hook shim

**Files:**
- Modify: `hooks/repo_guard.sh`
- Test: `scripts/tests/test_guard.py::test_an_interpreter_that_is_not_python_does_not_take_down_the_hook`

**Interfaces:**
- Consumes: nothing.
- Produces: a hook whose stderr contains only the guard's own message.

- [ ] **Step 1: Write the failing test.** In the existing test, after the two existing return-code assertions, add `assert "no Python here" not in outside.stderr` and `assert "no Python here" not in inside.stderr`.
- [ ] **Step 2: Run it.** `python -m pytest scripts/tests/test_guard.py::test_an_interpreter_that_is_not_python_does_not_take_down_the_hook -v`. Expected: FAIL on the new assertion (skipped if no `sh`; then run in Git Bash).
- [ ] **Step 3: Implement.** In the loop, after the `command -v` line, add `"$interpreter" -c '' </dev/null >/dev/null 2>&1 || continue`. Extend the comment: the audition runs the cheapest program only a real interpreter completes, output discarded so a launcher's message never reaches the agent; stdin is `/dev/null` so the payload is left for the guard; the status check stays as a second line of defence.
- [ ] **Step 4: Run it.** Same command. Expected: PASS. Then `python -m pytest scripts/tests/test_guard.py -v`; expected all pass.
- [ ] **Step 5: Commit.** `git add hooks/repo_guard.sh scripts/tests/test_guard.py && git commit -m "keep a rejected interpreter's stderr out of the guard's verdict"`

### Task 2: Trust the guard's own interpreter directory

**Files:**
- Modify: `scripts/goatcode/guard.py:56-72` (`allowed_roots`)
- Test: `scripts/tests/test_guard.py`

**Interfaces:**
- Consumes: `allowed_roots(repo, config=None, state=None) -> list[str]`.
- Produces: same signature; result now includes `real(os.path.dirname(sys.executable))` when `sys.executable` is truthy.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_interpreter_directory_is_an_allowed_root`: `assert guard.real(os.path.dirname(sys.executable)) in guard.allowed_roots(git_repo)`.
  - `test_a_command_naming_the_interpreter_is_not_refused`: verdict for a Bash payload whose command is `'"{}" -V'.format(sys.executable)`, run set to `execute`, is allow (reuse the helpers the neighbouring tests use for verdicts).
  - `test_the_interpreter_directory_does_not_widen_containment`: a path outside both the repo and the interpreter dir (`elsewhere`) is still refused.
  - `test_allowed_roots_survives_no_executable`: monkeypatch `sys.executable` to `""`; `allowed_roots(git_repo)` returns without raising.
- [ ] **Step 2: Run them.** `python -m pytest scripts/tests/test_guard.py -k "interpreter_directory or naming_the_interpreter or no_executable" -v`. Expected: first two FAIL, last two PASS or FAIL as written.
- [ ] **Step 3: Implement.** Append `os.path.dirname(sys.executable)` to `roots` next to `PLUGIN_ROOT`, guarded by `if sys.executable`; add `import sys` if missing. Comment: it is already trusted, being the interpreter running the guard, and dispatch prompts and `next.commands` name it by absolute path, so refusing it refuses goat-code's own commands.
- [ ] **Step 4: Run the file.** `python -m pytest scripts/tests/test_guard.py -v`. Expected: all pass.
- [ ] **Step 5: Commit.** `git commit -am "admit the interpreter's own directory so the commands goat-code renders are not refused"`

### Task 3: Reset the merge record when a cycle opens

**Files:**
- Modify: `scripts/goatcode/run.py:914` (`advance_cycle`)
- Test: `scripts/tests/test_run.py`, `scripts/tests/test_pipeline_e2e.py`

**Interfaces:**
- Consumes: `Run.advance_cycle() -> int`; `merge.state_of(run)` returns the `not-started` default when `state["merge"]` is absent.
- Produces: after `advance_cycle`, `merge.state_of(run)["status"] == "not-started"`.

- [ ] **Step 1: Write the failing tests.**
  - In `test_run.py`: `test_a_new_cycle_forgets_the_previous_merge`: set `run.state["merge"] = {"status": "clean", "pending": []}`, call `advance_cycle()`, `assert "merge" not in run.state`.
  - In `test_machine.py`: `test_after_executing_cycle_two_the_next_action_is_synthesize`: build evidence for cycle 2 with every slice finished and a stale clean merge from cycle 1 cleared by `advance_cycle`; assert `derive_phase` returns `synthesize`, not `verify`.
- [ ] **Step 2: Run them.** `python -m pytest scripts/tests/test_run.py scripts/tests/test_machine.py -k "forgets_the_previous_merge or cycle_two" -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** In `advance_cycle`, next to the other per-cycle resets, `self.state.pop("merge", None)`. Comment: the record describes the last cycle's integration; leaving it makes `derive_phase` see a clean merge and skip `synthesize`, so verify judges the previous cycle's ref.
- [ ] **Step 4: Run the whole suite.** `python -m pytest scripts/tests -v`. Expected: all pass, replan-cycle e2e tests included. If an e2e test relied on the stale record, fix the test's expectation, not the guard against it.
- [ ] **Step 5: Check the second symptom.** The verifier's "Files changed since then (0)" came from `diffpkg.previous_judgement`, which reads `_judged_ref` for both cycles; with the stale merge it compared the same ref twice. Add `test_the_delta_spans_both_cycles` in `test_dispatch.py` or `test_diffpkg` equivalent: two cycles with different integration refs yield a non-empty `changed_files`. If it passes without further change, the root cause was the stale merge; if not, report it and stop rather than guess.
- [ ] **Step 6: Commit.** `git commit -am "forget the last cycle's merge so synthesize runs before verify in cycles 2 and up"`

### Task 4: Docs note

**Files:**
- Modify: `templates/config.yaml:134-142` comment on `extra_roots`.

- [ ] **Step 1:** Add one sentence: the interpreter's directory is admitted automatically, so `extra_roots` is for other directories only.
- [ ] **Step 2:** `python -m pytest scripts/tests/test_plugin.py -v`. Expected: pass.
- [ ] **Step 3:** `git commit -am "say extra_roots no longer needs the interpreter directory"`

## Self-review

- Spec coverage: issue 1 → Task 1; issue 2 option 2 → Task 2 (option 1 and 3 deliberately skipped: option 2 keeps absolute paths working); unrelated observation → Task 3 steps 3-5.
- Known gap: the hook may pick a different interpreter than the one that rendered the commands (e.g. `python` vs a venv). Task 2 trusts only the hook's own. If that bites, record `sys.executable` in run state at `init` and admit it too.
- Names consistent: `allowed_roots`, `advance_cycle`, `state_of`, `previous_judgement`.
