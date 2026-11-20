# goat-report Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gaps from the failed headless run in `goat-report.md` that are still open on `main` (0.5.1): Java stacks get no gates, users can't set gate commands, empty gates read as "no regressions", a failed run can't be reopened, and a stopped run forgets why it stopped.

**Architecture:** All changes are in the script layer (`scripts/goatcode/`) plus the docs that `test_plugin.py` keeps in sync. Stack detection gains a Java detector and a config overlay. Gate rendering and `init` say so when nothing can be checked. A new `reopen` CLI command reverses a `failed` phase. `machine._stop` persists its reason so later calls to `next` repeat it.

**Tech Stack:** Python 3.9+ stdlib only, pytest.

**Spec:** `goat-report.md` (repo root, untracked). It was written against plugin **0.4.0**.

### What the report found vs what `main` already fixes

| Report cause | State on `main` | This plan |
| --- | --- | --- |
| 1. A stale `merge.status: clean` skipped the merge | Fixed twice: `run.advance_cycle` pops `merge` (7dd543e, 2026-11-10), and `derive_phase` only verifies when `not evidence.unmerged` (555615e). The run used a cached 0.4.0, because the version wasn't bumped until 2026-11-12. | Task 1 adds an end-to-end test of the exact scenario |
| 2. `depends_on` does not carry code within a cycle | Fixed: `Evidence.dispatchable` waits until dependencies are merged, and `worktree.start_point` branches from the integration tip (555615e) | Task 1 corrects the stale `worktree.py` docstring |
| 3. No gates on a Maven repo | **Open**: `stack.detect` knows `java` as a language but has no detector, so every command is `null` | Tasks 2, 3, 4 |
| 4. `failed` is terminal, config is frozen, empty stop reason | **Open** | Tasks 5, 6 |
| 5. The guard refused the pipeline's own commands | Mostly fixed in 0.5.x: `guard.allowed_roots` admits the plugin root, `~/.claude/plugins`, the interpreter's directory, `temp_root` and every worktree | none. Rerun on 0.6.0 and read `guard.jsonl` |
| 6. Executors skipped Spotless/JaCoCo | Not a script-layer bug | Task 2's `test` gate is `mvn -B verify`, which runs those plugins |
| 7. The verifier found real defects | Not a bug | none |

**Not doing:**
- **Recommendation 1's ancestry check:** two independent fixes already cover it. Add it only if a stale merge record shows up again on ≥0.6.0.
- **Supporting a manual merge followed by a re-verify:** `reopen` plus a raised cap is the supported route.
- **Maven or Gradle wrapper (`mvnw`/`gradlew`) detection:** `osenv.resolve_exe` resolves relative names against the process cwd, not the gate's cwd. The `commands:` override from Task 3 covers wrappers.
- **The `sed` call on `tasks.yaml`:** the report doesn't know who issued it. There is nothing to act on.

## Global Constraints

- Stdlib only, Python 3.9+. No new imports outside the stdlib.
- `subprocess` may only be imported in `osenv.py`, and `shell=True` is never used.
- Commands are argv lists, never shell strings (see the `stack.py` docstring).
- `templates/config.yaml` must equal `run.DEFAULT_CONFIG` key for key and value for value (`test_config_template_parses_and_matches_the_defaults`).
- Every `goatcode.py <cmd>` in markdown must be a real subcommand, and every row in the conventions skill's CLI table must name a real command (`test_plugin.py`).
- Bump the version in both `scripts/goatcode/__init__.py` and `.claude-plugin/plugin.json`, from 0.5.1 to **0.6.0** (Task 7).
- Run tests with `py -m pytest` on this machine, because `python` here has no pytest. Never use `-n`/xdist.
- Work on branch `fix/goat-report-findings`, never on `main`.
- Commit subjects are lowercase declarative sentences explaining *why*, and end with the `Co-Authored-By` trailer.

## Review Focus

1. **A misspelt key under `commands:` in `config.yaml` (e.g. `tests:`):** must be refused with an error that names the key. Ignoring it silently is the exact "edited the config, nothing changed" trap from the report. Tested in Task 3.
2. **A command written as a string (`test: mvn verify`):** must be refused with "list of arguments". A plain `.split()` would mangle paths that contain spaces. Tested in Task 3.
3. **`reopen --max-cycles` below the current cycle:** must be refused and must name the minimum. Otherwise the run fails again on the very next `next`. Tested in Task 5.
4. **`pom.xml` one level down (`backend/pom.xml`):** must be adopted as a nested project and get Maven gates. Tested in Task 2.
5. **A gate whose command is configured but missing from the host:** status is `missing` and must render as "nothing was checked", not "no regressions". Tested in Task 4.

---

### Task 1: Pin merge-before-verify across cycles end to end

**Files:**
- Test: `scripts/tests/test_pipeline_e2e.py` (add after `test_carried_slices_are_never_re_executed`)
- Modify: `scripts/goatcode/worktree.py:1-10` (module docstring)

**Interfaces:**
- Consumes: the existing `FakeAgent`, `slice_doc`, `make_driver` and `started` fixtures. `FakeAgent.replanner` adds remedial slice `R2`, which owns `src/fix2/**`.
- Produces: nothing new.

- [ ] **Step 1: Create the branch**

Run: `git switch -c fix/goat-report-findings`

- [ ] **Step 2: Write the pin test**

```python
def test_a_remedial_slice_is_merged_before_its_cycle_is_verified(started):
    """goat-report, cause 1: cycles 2 and 3 verified cycle 1's S1-only diff
    because the clean merge record survived the cycle boundary."""
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=1)
    make_driver(started, agent).loop()

    run = Run.load(started)
    assert (run.cycle_dir(2) / "merge-report.md").exists()
    first = (run.cycle_dir(1) / "review.diff").read_text(encoding="utf-8")
    second = (run.cycle_dir(2) / "review.diff").read_text(encoding="utf-8")
    assert "src/fix2/index.js" in second
    assert second != first
```

- [ ] **Step 3: Run it. It should pass, since this pins a fix already on `main`**

Run: `py -m pytest scripts/tests/test_pipeline_e2e.py::test_a_remedial_slice_is_merged_before_its_cycle_is_verified -v`
Expected: PASS

- [ ] **Step 4: Prove the test bites, then restore**

Temporarily make two changes: delete `self.state.pop("merge", None)` in `run.advance_cycle`, and change `and not evidence.unmerged` to nothing in `machine.derive_phase` (the `"verify"` branch, around line 224). Rerun the step 3 command and expect FAIL. Then restore both lines with `git checkout scripts/goatcode/run.py scripts/goatcode/machine.py`.

- [ ] **Step 5: Correct the `worktree.py` docstring**

Replace the sentence "Branches are created off the run's recorded base commit, never off HEAD..." with one that matches `start_point`: slice branches start at the integration tip, which in cycle 1 is the base commit, and never at the user's HEAD.

- [ ] **Step 6: Commit**

```bash
git add scripts/tests/test_pipeline_e2e.py scripts/goatcode/worktree.py
git commit -m "pin that a replan cycle merges its remedial slices before verifying them"
```

### Task 2: Detect Maven and Gradle projects

**Files:**
- Modify: `scripts/goatcode/stack.py`. Add `_detect_java` next to `_detect_dotnet`, and call it from `detect` after the `.NET` branch.
- Test: `scripts/tests/test_stack.py` (add after `test_detects_dotnet`)

**Interfaces:**
- Produces: `_detect_java(repo: pathlib.Path, profile: dict) -> None`. It writes these values, using `or` so an earlier detector wins, the same way `_detect_go` does:

| marker | `package_manager` | `test_framework` | `build` | `test` |
| --- | --- | --- | --- | --- |
| `pom.xml` | `maven` | `maven` | `["mvn", "-B", "-DskipTests", "test-compile"]` | `["mvn", "-B", "verify"]` |
| `build.gradle` / `build.gradle.kts` (no `pom.xml`) | `gradle` | `gradle` | `["gradle", "assemble"]` | `["gradle", "check"]` |

  In both cases `languages` gains `"java"`. `setup`, `typecheck`, `lint` and `test_one` stay `None`. `verify` and `check` already run the lint and coverage plugins bound to them, which is why the report's Spotless failures were missed.

- [ ] **Step 1: Write the failing tests**

```python
def test_detects_maven(tmp_path):
    write(tmp_path, "pom.xml", "<project/>")
    profile = stack.detect(tmp_path)
    assert profile["languages"] == ["java"]
    assert profile["package_manager"] == "maven"
    assert profile["commands"]["build"] == ["mvn", "-B", "-DskipTests", "test-compile"]
    assert profile["commands"]["test"] == ["mvn", "-B", "verify"]
    assert stack.usable(profile)


def test_detects_gradle(tmp_path):
    write(tmp_path, "build.gradle.kts", "")
    profile = stack.detect(tmp_path)
    assert profile["languages"] == ["java"]
    assert profile["package_manager"] == "gradle"
    assert profile["commands"]["test"] == ["gradle", "check"]


def test_detects_a_maven_project_under_backend(tmp_path):
    write(tmp_path, "backend/pom.xml", "<project/>")
    profile = stack.detect(tmp_path)
    assert profile["project_dir"] == "backend"
    assert profile["commands"]["test"] == ["mvn", "-B", "verify"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `py -m pytest scripts/tests/test_stack.py -k "maven or gradle" -v`
Expected: 3 FAIL. `package_manager` is `None` and `commands["test"]` is `None`.

- [ ] **Step 3: Implement `_detect_java` and call it from `detect`**

In `detect`, add `if any(m in markers for m in ("pom.xml", "build.gradle", "build.gradle.kts")): _detect_java(repo, profile)`. Choose Maven when `pom.xml` exists in `repo`, otherwise Gradle.

- [ ] **Step 4: Run the stack suite**

Run: `py -m pytest scripts/tests/test_stack.py -v`
Expected: all PASS, including `test_all_commands_are_argv_lists`.

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/stack.py scripts/tests/test_stack.py
git commit -m "give maven and gradle projects gates instead of a java label and no commands"
```

### Task 3: Let `.goatcode/config.yaml` set gate commands

**Files:**
- Modify: `scripts/goatcode/stack.py`. Add the `COMMAND_NAMES` constant and a `commands` parameter on `detect` and `write`.
- Modify: `scripts/goatcode/run.py`. Add `"commands": {}` to `DEFAULT_CONFIG`, and validate it in `load_config`.
- Modify: `scripts/goatcode.py`. `cmd_init` (around line 368) passes `commands=run.config.get("commands")` to `stackmod.write`. `cmd_stack` detect (around line 478) passes `commands=runmod.load_config(repo).get("commands")`.
- Modify: `templates/config.yaml`. Add `commands: {}` with a comment and an example, placed after `project_dir`.
- Test: `scripts/tests/test_stack.py`, `scripts/tests/test_run.py`, `scripts/tests/test_cli.py`

**Interfaces:**
- Consumes: `_detect_java` from Task 2. The tests use its Maven values.
- Produces:
  - `stack.COMMAND_NAMES = ("setup", "build", "typecheck", "lint", "test", "test_one", "e2e")`
  - `stack.detect(repo, project_dir=None, commands=None, _siblings=True) -> dict`. Every non-`None` entry in `commands` replaces the detected argv. The overlay is applied after `_containerize`, so the user's command is taken verbatim. When at least one entry applies, a note is appended to `profile["notes"]`: `"commands from .goatcode/config.yaml: <names, comma-separated>"`.
  - `stack.write(repo, path, project_dir=None, commands=None) -> dict`, which passes `commands` through.
  - `run.load_config(repo)` raises `RunError` in two cases:
    - a key under `commands` that is not in `COMMAND_NAMES`: `"commands.<key> in <path> is not a gate; use one of setup, build, typecheck, lint, test, test_one, e2e"`
    - a value that is neither `None` nor a non-empty list of strings: `"commands.<key> in <path> must be a list of arguments, like [mvn, -B, verify]"`

  If importing `stack` into `run.py` creates an import cycle, define `COMMAND_NAMES` in `run.py` and import it into `stack.py` instead.
  - `fill_gaps` needs no change: commands stored in the profile already survive re-detection.

- [ ] **Step 1: Write the failing tests**

`test_stack.py`:
```python
def test_configured_commands_replace_the_detected_ones(tmp_path):
    write(tmp_path, "pom.xml", "<project/>")
    profile = stack.detect(tmp_path, commands={"test": ["mvn", "-B", "clean", "verify"], "lint": None})
    assert profile["commands"]["test"] == ["mvn", "-B", "clean", "verify"]
    assert profile["commands"]["build"] == ["mvn", "-B", "-DskipTests", "test-compile"]
    assert "commands from .goatcode/config.yaml: test" in profile["notes"]


def test_configured_commands_make_an_unknown_stack_usable(tmp_path):
    write(tmp_path, "notes.txt", "x")
    assert stack.usable(stack.detect(tmp_path, commands={"test": ["make", "test"]}))
```

`test_run.py`:
```python
def _config(git_repo, text):
    target = git_repo / ".goatcode" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_a_misspelt_gate_name_is_refused(git_repo):
    _config(git_repo, "commands:\n  tests: [mvn, verify]\n")
    with pytest.raises(RunError, match="commands.tests"):
        runmod.load_config(git_repo)


def test_a_gate_command_written_as_a_string_is_refused(git_repo):
    _config(git_repo, "commands:\n  test: mvn verify\n")
    with pytest.raises(RunError, match="list of arguments"):
        runmod.load_config(git_repo)
```

`test_cli.py`:
```python
def test_init_takes_gate_commands_from_the_config(capsys, git_repo):
    target = git_repo / ".goatcode" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("commands:\n  test: [make, test]\n", encoding="utf-8")
    code, payload, _err = invoke_json(
        capsys, "--repo", str(git_repo), "init", "--prompt", "add a thing", "--no-baseline"
    )
    assert code == 0
    profile = json.loads(pathlib.Path(payload["stack"]).read_text(encoding="utf-8"))
    assert profile["commands"]["test"] == ["make", "test"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `py -m pytest scripts/tests/test_stack.py scripts/tests/test_run.py scripts/tests/test_cli.py -k "configured_commands or gate_name or gate_command or gate_commands_from" -v`
Expected: FAIL. `detect()` raises `TypeError: unexpected keyword 'commands'`, and `load_config` raises nothing.

- [ ] **Step 3: Implement the interfaces above, and add `commands: {}` to the template**

The template comment says three things: the key overrides detected gate commands per gate; values are argv lists (`test: [mvn, -B, clean, verify]`); and this is how to point at a `./mvnw` or `./gradlew` wrapper.

- [ ] **Step 4: Run the touched suites plus the plugin sync tests**

Run: `py -m pytest scripts/tests/test_stack.py scripts/tests/test_run.py scripts/tests/test_cli.py scripts/tests/test_plugin.py -q`
Expected: exit 0, no `FAILED` lines.

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/stack.py scripts/goatcode/run.py scripts/goatcode.py templates/config.yaml scripts/tests/test_stack.py scripts/tests/test_run.py scripts/tests/test_cli.py
git commit -m "let the project config name its gate commands, and refuse ones it would silently ignore"
```

### Task 4: Say so when the gates checked nothing

**Files:**
- Modify: `scripts/goatcode/gates.py`, in `render` (around line 300)
- Modify: `scripts/goatcode.py`, in `cmd_init` right after `stackmod.write(...)`
- Test: `scripts/tests/test_gates.py`, `scripts/tests/test_cli.py`

**Interfaces:**
- Consumes: the existing `gates.nothing_ran(report)` and `stack.usable(profile)`.
- Produces:
  - `gates.render`: when `nothing_ran(report)` is true, the line `"no regressions"` is replaced by exactly `"nothing ran - no gate command was detected or runnable, so nothing was checked; set commands in .goatcode/config.yaml"`.
  - `cmd_init`: when `not stackmod.usable(profile)`, it appends exactly `"no test command detected - the gates check nothing until there is one; set commands.test in .goatcode/config.yaml"` to `problems`. That list is already emitted as `payload["warnings"]` and printed under "proceeding despite:".

- [ ] **Step 1: Write the failing tests**

`test_gates.py`:
```python
def test_render_says_nothing_was_checked_when_no_gate_ran(run, git_repo):
    report = gates.run_and_classify(run, git_repo, profile=profile())
    text = gates.render(report)
    assert "no regressions" not in text
    assert "nothing was checked" in text


def test_a_gate_tool_missing_from_the_host_is_not_an_all_clear(run, git_repo):
    report = gates.run_and_classify(run, git_repo, profile=profile(test=["goatcode-no-such-tool"]))
    assert "nothing was checked" in gates.render(report)
```

`test_cli.py`:
```python
def test_init_warns_when_no_test_command_was_detected(capsys, git_repo):
    code, payload, _err = invoke_json(
        capsys, "--repo", str(git_repo), "init", "--prompt", "add a thing", "--no-baseline"
    )
    assert code == 0
    assert any("no test command detected" in w for w in payload["warnings"])


def test_init_does_not_warn_about_gates_on_a_detected_stack(capsys, node_repo):
    _code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "add a thing", "--no-baseline"
    )
    assert not any("no test command detected" in w for w in payload["warnings"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `py -m pytest scripts/tests/test_gates.py scripts/tests/test_cli.py -k "nothing_was_checked or all_clear or no_test_command or warn_about_gates" -v`
Expected: 3 FAIL, and `test_init_does_not_warn_about_gates_on_a_detected_stack` PASSES.

- [ ] **Step 3: Implement both changes in `render` and `cmd_init`**

- [ ] **Step 4: Run the gates and CLI suites**

Run: `py -m pytest scripts/tests/test_gates.py scripts/tests/test_cli.py -q`
Expected: exit 0. `test_render_says_so_when_all_is_well` still passes, because its `test=PASS` gate ran.

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/gates.py scripts/goatcode.py scripts/tests/test_gates.py scripts/tests/test_cli.py
git commit -m "stop reporting no regressions when no gate ran, and warn at init when none can"
```

### Task 5: `reopen` gives a failed run more cycles

**Files:**
- Modify: `scripts/goatcode.py`. Add `cmd_reopen` next to `cmd_resume`, and a `reopen` subparser next to `resume` in `build_parser`.
- Modify: `skills/goat-code-conventions/SKILL.md`, adding a CLI table row after the `cycle` row.
- Modify: `docs/PIPELINE.md`, in the "Resuming" section.
- Modify: `templates/config.yaml`, extending the comment on `max_cycles`.
- Test: `scripts/tests/test_cli.py` (after `test_cycle_cap_stops_the_loop`)

**Interfaces:**
- Produces: `goatcode reopen [--max-cycles N]` (`N` is an int). It runs these checks in order. Each refusal raises `CliError`, which exits with `EXIT_USAGE`.
  1. If `run.phase != "failed"`, it refuses with `"only a failed run can be reopened; {run_id} is {phase}"`.
  2. When `--max-cycles` is given and `N < run.cycle`, or when it is omitted and `run.cycles_exhausted()`, it refuses with `"cycle cap of {cap} is spent at cycle {cycle}; pass --max-cycles {cycle} or more"`.
  3. Otherwise it:
     - sets `run.state["config"]["max_cycles"] = N` (if `"config"` is missing from state, it starts from a deep copy of `DEFAULT_CONFIG`);
     - pops `dispatch_attempts` and `repeated_run` from state, so an agent that hit the dispatch cap gets fresh attempts;
     - calls `run.set_phase("init")`, after which the next `next` re-derives the real phase;
     - appends `"run reopened (max_cycles {N})"` to the ledger;
     - emits `{"run_id", "max_cycles", "cycle"}` with the text `"reopened {run_id} at cycle {cycle}, cap {N}; run next"`.

  A run that failed for a reason that is still on disk (for example an e2e `FAILED`) re-derives `failed` on the next `next`. That is correct, and no special case is needed.

- [ ] **Step 1: Write the failing tests**

```python
def _failed_at_the_cap(node_repo):
    run = make_run(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    tasksmod.set_status(run.tasks_path, "S1", "carried")
    run.state["cycle"] = 4
    run.state["awaiting_replan"] = True
    run.state["dispatch_attempts"] = {"4:goat-code-replanner:-:opus:abc": 3}
    run.save()
    run.set_phase("failed")
    return run


def test_reopen_raises_the_cap_and_the_run_replans(capsys, node_repo):
    run = _failed_at_the_cap(node_repo)
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "reopen", "--max-cycles", "6")
    assert code == 0
    assert payload["max_cycles"] == 6
    run = Run.load(node_repo, run.run_id)
    assert run.config["max_cycles"] == 6
    assert not run.state.get("dispatch_attempts")
    assert machine.derive_phase(run) == "replan"


def test_reopen_with_the_cap_spent_names_the_minimum(capsys, node_repo):
    _failed_at_the_cap(node_repo)
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "reopen")
    assert code == cli.EXIT_USAGE
    assert "--max-cycles 4 or more" in err


def test_reopen_below_the_current_cycle_is_refused(capsys, node_repo):
    _failed_at_the_cap(node_repo)
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "reopen", "--max-cycles", "3")
    assert code == cli.EXIT_USAGE
    assert "--max-cycles 4 or more" in err


def test_only_a_failed_run_can_be_reopened(capsys, node_repo):
    run = make_run(node_repo)
    run.set_phase("done")
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "reopen", "--max-cycles", "6")
    assert code == cli.EXIT_USAGE
    assert "only a failed run" in err
```

Add `machine` and `tasks as tasksmod` to `test_cli.py`'s `from goatcode import ...` line if they are not imported already.

- [ ] **Step 2: Run them to verify they fail**

Run: `py -m pytest scripts/tests/test_cli.py -k reopen -v`
Expected: 4 FAIL with argparse `invalid choice: 'reopen'`.

- [ ] **Step 3: Implement `cmd_reopen` and its subparser**

- [ ] **Step 4: Write the docs**
  - Conventions table row: `` | `reopen --max-cycles N` | give a failed run more cycles; the next `next` picks up where it stopped | ``
  - `docs/PIPELINE.md` "Resuming": one paragraph. `failed` is terminal for `next`, and `python scripts/goatcode.py reopen --max-cycles N` is the supported way back. Editing `max_cycles` in `config.yaml` does not reach a run that has already started, because each run snapshots its config at `init`.
  - `templates/config.yaml`, on `max_cycles`: add "Copied into each run when it starts: editing it here does not reach a run already under way; `goatcode reopen --max-cycles N` does."

- [ ] **Step 5: Run the CLI and plugin suites**

Run: `py -m pytest scripts/tests/test_cli.py scripts/tests/test_plugin.py -q`
Expected: exit 0. `test_documented_cli_examples_are_real_commands` and `test_every_referenced_cli_command_exists` both pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode.py scripts/tests/test_cli.py skills/goat-code-conventions/SKILL.md docs/PIPELINE.md templates/config.yaml
git commit -m "add reopen, because a run that spent its cycle cap had no supported way back"
```

### Task 6: A stopped run keeps saying why

**Files:**
- Modify: `scripts/goatcode/machine.py`, in `_stop` (around line 976) and in the cycle-cap branch of `_replan` (around line 943)
- Modify: `scripts/goatcode.py`, where `cmd_reopen` from Task 5 also pops `stop_reason` and `stop_details`
- Test: `scripts/tests/test_pipeline_e2e.py` (after `test_the_cycle_cap_stops_instead_of_looping`), `scripts/tests/test_cli.py`

**Interfaces:**
- Consumes: `cmd_reopen` and `_failed_at_the_cap` from Task 5.
- Produces:
  - `_stop`: when `outcome != "done"` and a `reason` is given, it stores `run.state["stop_reason"] = reason` and `run.state["stop_details"] = details or []`, then saves. When `reason` is `None`, it reads both back from state. The first stop and every later `next` therefore print the same message.
  - `_replan` cap stop:
    - `reason` is `"cycle cap of {cap} reached with criteria still unmet; goatcode reopen --max-cycles N gives it more"`. It keeps the `"cycle cap of {cap}"` prefix that `test_the_cycle_cap_stops_instead_of_looping` asserts.
    - `details` is `["last verdict: {}".format(run.cycle_dir(run.cycle - 1) / "verdict.md")]`.

- [ ] **Step 1: Write the failing tests**

`test_pipeline_e2e.py`:
```python
def test_a_stopped_run_keeps_saying_why(started):
    """goat-report: the headless run printed `STOPPED (failed)` with an empty
    reason, because only the first stop was handed one."""
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=99)
    make_driver(started, agent).loop()

    again = machine.next_action(Run.load(started))
    assert again["action"] == "stop"
    assert "cycle cap of 3" in again["message"]
    assert "reopen --max-cycles" in again["message"]
    assert "cycle-3" in again["message"] and "verdict.md" in again["message"]
```

`test_cli.py`:
```python
def test_reopen_forgets_why_the_run_stopped(capsys, node_repo):
    run = _failed_at_the_cap(node_repo)
    run.state["stop_reason"] = "cycle cap of 3 reached"
    run.save()
    invoke(capsys, "--repo", str(node_repo), "reopen", "--max-cycles", "6")
    assert "stop_reason" not in Run.load(node_repo, run.run_id).state
```

- [ ] **Step 2: Run them to verify they fail**

Run: `py -m pytest scripts/tests/test_pipeline_e2e.py::test_a_stopped_run_keeps_saying_why scripts/tests/test_cli.py::test_reopen_forgets_why_the_run_stopped -v`
Expected: both FAIL. The second message is `"STOPPED (failed)\n\n\n"`, and `stop_reason` is still in state.

- [ ] **Step 3: Implement the `_stop` persistence, the `_replan` reason and details, and the pop in `cmd_reopen`**

- [ ] **Step 4: Run the machine, e2e and CLI suites**

Run: `py -m pytest scripts/tests/test_machine.py scripts/tests/test_pipeline_e2e.py scripts/tests/test_cli.py -q`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/machine.py scripts/goatcode.py scripts/tests/test_pipeline_e2e.py scripts/tests/test_cli.py
git commit -m "keep a failed run's stop reason, so every later next says why and where to look"
```

### Task 7: Ship it as 0.6.0

**Files:**
- Modify: `scripts/goatcode/__init__.py` (`__version__`)
- Modify: `.claude-plugin/plugin.json` (`"version"`)

- [ ] **Step 1: Bump both from `0.5.1` to `0.6.0`**

- [ ] **Step 2: Run the full suite**

Run: `py -m pytest scripts/tests -q; echo exit=$?`
Expected: `exit=0`, and `grep -c ^FAILED` on the output is 0. `test_the_script_layer_reports_the_plugin_version` passes.

- [ ] **Step 3: Commit**

```bash
git add scripts/goatcode/__init__.py .claude-plugin/plugin.json
git commit -m "bump to 0.6.0 so installed plugins pick up the goat-report fixes"
```

- [ ] **Step 4: Hand back the rerun steps for the original repo (no code)**
  1. Reinstall the plugin so the cache holds 0.6.0.
  2. Rerun the spec.
  3. Check that the run's `stack.json` shows `mvn -B verify`.
  4. Check that `guard.jsonl` has no refusals of `python.exe`, the plugin cache or worktree paths.
