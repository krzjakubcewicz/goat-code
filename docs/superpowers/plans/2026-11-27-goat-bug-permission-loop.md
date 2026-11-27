# Headless Permission Loop Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone `goatcode run` under the default `permission_mode: acceptEdits` lets executors run commands, and a run whose agents cannot talk to the machine stops at the first dispatch instead of looping until the money runs out.

**Architecture:** The root cause is in `agentcli.build_argv`: `--allowedTools` is Claude Code's *pre-approval* list (`--tools` is the restriction), so every agent whose frontmatter lists `Bash` already runs commands unattended, while the executor (`tools: ["*"]`) gets nothing and is denied every Bash call. Probed on 2026-11-27 with `claude --print --permission-mode acceptEdits`: `python -c "print(6*7)"` came back `DENIED` without `--allowedTools Bash` and `42` with it. On top of that fix: the driver stops when an agent's own `goatcode` call is refused, reads its own settings live from `config.yaml`, skips an escalation that changes no model, and the machine stops after two remedial cycles that merged nothing.

**Tech Stack:** Python 3.9+ stdlib, pytest. No new dependencies.

**Spec:** `goat-bug.md` (repo root, uncommitted - it quotes another project's run; do not commit it).

## Global Constraints

- Branch `fix/goat-bug-permission-loop` off `main` **after PR #16 is merged**: this builds on `reopen`, `machine.why_it_failed` and `load_config` raising `RunError` for unparseable YAML, all from #16.
- Stdlib only, Python 3.9+. Only `osenv` imports `subprocess`. No `shell=True`.
- Markdown and code stay in sync (`test_plugin.py`); every `goatcode.py <cmd>` named in markdown must be a real subcommand.
- Version bumped once, in `scripts/goatcode/__init__.py` and `.claude-plugin/plugin.json` together: `0.6.0` -> `0.6.1`.
- LF line endings. Write Python edits with the Edit tool, not shell heredocs (heredocs mangle `\n` here).
- Prose (comments, docstrings, commits) explains *why*, lowercase declarative, as the rest of the repo does.
- Run tests with `py -m pytest`, never with `-n`.

## Decisions already made (with the user)

- `tools: ["*"]` agents get `--allowedTools Bash`, the same pre-approval every other agent already has. The repo guard stays the boundary. README and template text is corrected to say what `acceptEdits` really allows.
- No `NEEDS_HUMAN` verdict (report fix 4). The no-progress breaker (Task 5) covers environmental loops of any cause without a new agent contract.
- Report fix 3 (a status-file side channel for a refused `report`) is not built: Task 2 makes a refused report stop the run with its reason, which is the outcome fix 3 wanted, and a second reporting channel would skip `report.py`'s verification.
- A refused `goatcode` call raises `DriverError` (phase left as it is) rather than stopping with `outcome=failed` as the report suggests: the run is not at fault, and `goatcode run --resume` continues it once the permission is fixed, with no `reopen` needed.
- `reopen` does not reload driver settings (report fix 2): it dispatches nothing, and Task 3 makes every `goatcode run` start, `--resume` included, read them from the file.

## Review Focus

1. A permission-denial entry without `tool_input`, from a non-Bash tool, or with a non-string `command`: `refused_cli_calls` returns `[]` and nothing raises. Tested in Task 2.
2. A `goatcode` call refused inside a compound command (`cd backend && py scripts/goatcode.py report ...`) still counts as a refused report. Tested in Task 2.
3. A `config.yaml` that does not parse when `goatcode run --resume` starts: `live_settings` raises `RunError` (printed as `goatcode: ...`, exit 1), not a traceback. Tested in Task 3.
4. A cycle with no `gates.json` (it stopped before verify) never counts as a stalled cycle. Tested in Task 5.
5. A run reopened after the breaker stopped it is not stopped again by the same historical cycles. Tested in Task 5.

---

### Task 1: executors get Bash pre-approved like every other agent

**Files:**
- Modify: `scripts/goatcode/agentcli.py` (`build_argv`, the `tools` block near line 135)
- Modify: `README.md` (the **Permissions.** paragraph near line 231), `templates/config.yaml` (`permission_mode` comment near line 131), `scripts/goatcode/run.py` (`DEFAULT_CONFIG` comment above `"permission_mode"`)
- Test: `scripts/tests/test_agentcli.py`

**Interfaces:**
- Produces: `build_argv` emits `--allowedTools Bash` for an agent whose frontmatter `tools` contains `"*"`; a listed `tools` is emitted exactly as today.

- [ ] **Step 1: Replace `test_a_wildcard_tool_list_means_no_restriction` with the failing test**

```python
def test_a_wildcard_agent_gets_bash_pre_approved():
    """--allowedTools pre-approves rather than restricts, so the executor -
    tools: [*] - was the one agent that could run no command under acceptEdits."""
    argv = agentcli.build_argv(entry("goat-code-executor"), "C:/repo")
    assert argv_value(argv, "--allowedTools") == "Bash"
```

- [ ] **Step 2: Run it**

Run: `py -m pytest scripts/tests/test_agentcli.py -v`
Expected: FAIL on `test_a_wildcard_agent_gets_bash_pre_approved` (no `--allowedTools` in argv); the rest pass.

- [ ] **Step 3: Implement in `build_argv`**

`"*"` in `tools` emits `["--allowedTools", "Bash"]`; a list without `"*"` is unchanged. The comment says why: `--allowedTools` is the pre-approval list (`--tools` restricts), Edit and Write are already accepted under `acceptEdits`, and Read/Grep/Glob never prompt, so Bash is the only gap.

- [ ] **Step 4: Correct the docs**

The README **Permissions.** paragraph becomes:

> **Permissions.** Agents run headless, so nobody can answer a permission prompt. Under the default `permission_mode: acceptEdits` every agent may edit files and run Bash - executors need it for their tests and every agent for its `goatcode` calls - and the repo guard keeps those calls inside the repository. A tool that would still prompt (WebFetch, an MCP server) is denied and reported. `bypassPermissions` lifts that last ceiling.

Update the template's `acceptEdits` line and the `DEFAULT_CONFIG` comment to match: acceptEdits = edits and Bash pre-approved, other prompting tools denied; bypassPermissions = nothing prompts.

- [ ] **Step 5: Run the agentcli and plugin tests**

Run: `py -m pytest scripts/tests/test_agentcli.py scripts/tests/test_plugin.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/agentcli.py scripts/goatcode/run.py scripts/tests/test_agentcli.py README.md templates/config.yaml
git commit -m "pre-approve bash for executors, the one agent acceptEdits left unable to run a command"
```

---

### Task 2: a refused `goatcode` call stops the run

**Files:**
- Modify: `scripts/goatcode/driver.py` (new module-level `refused_cli_calls`; `Driver.do_dispatch`)
- Test: `scripts/tests/test_driver.py`, `scripts/tests/test_pipeline_e2e.py`

**Interfaces:**
- Produces: `refused_cli_calls(receipt) -> list[str]`: the `command` strings of `receipt.denials` entries whose `tool_name == "Bash"` and whose `tool_input["command"]` is a string containing `"goatcode.py"`. Anything malformed is skipped, never raised on.
- `Driver.do_dispatch` raises `DriverError` after the whole wave's receipts are printed and recorded, when any receipt has a refused CLI call.

- [ ] **Step 1: Write the failing unit tests in `test_driver.py`**

```python
def denied(command):
    return {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": command}}


def test_a_refused_report_is_found_inside_a_compound_command():
    r = receipt(denials=[denied("cd backend && py scripts/goatcode.py --repo . report --slice S1 --status DONE")])
    assert drivermod.refused_cli_calls(r) == ["cd backend && py scripts/goatcode.py --repo . report --slice S1 --status DONE"]


def test_a_refused_test_run_is_not_a_refused_report():
    assert drivermod.refused_cli_calls(receipt(denials=[denied("pytest -q")])) == []


def test_a_malformed_denial_is_ignored():
    odd = [{"tool_name": "Bash"}, {"tool_name": "WebFetch", "tool_input": {"url": "goatcode.py"}},
           {"tool_name": "Bash", "tool_input": {"command": None}}]
    assert drivermod.refused_cli_calls(receipt(denials=odd)) == []
```

(`receipt(...)` is the helper `test_driver.py` already uses for `Console.receipt` tests.)

- [ ] **Step 2: Write the failing end-to-end test in `test_pipeline_e2e.py`**

A `FakeAgent` subclass whose `executor` does nothing and whose `__call__` returns `agentcli.Receipt(entry, ok=True, duration_ms=0, denials=[{"tool_name": "Bash", "tool_input": {"command": "python goatcode.py report --slice S1 --status DONE"}}])` for executor entries, after appending `(entry["agent"], entry["slice"], entry["model"])` to `self.dispatched` as the base class does (delegating to `FakeAgent.__call__` for every other agent).

```python
def test_a_refused_report_stops_the_run_at_the_first_dispatch(started):
    """goat-bug: executors could not run `goatcode report`, the machine read
    silence, and nine cycles re-dispatched them until a $550 key ran dry."""
    agent = RefusedReportAgent([slice_doc("S1", "src/s1/**")])
    with pytest.raises(drivermod.DriverError) as excinfo:
        make_driver(started, agent).loop()
    message = str(excinfo.value)
    assert "S1" in message and "permission_mode" in message and "run --resume" in message
    assert [a for a, _s, _m in agent.dispatched].count("goat-code-executor") == 1
```

- [ ] **Step 3: Run them**

Run: `py -m pytest scripts/tests/test_driver.py scripts/tests/test_pipeline_e2e.py -k "refused or malformed" -v`
Expected: the three unit tests FAIL with `AttributeError: ... refused_cli_calls`; the e2e test FAILS because no `DriverError` is raised (the loop runs to the dispatch cap).

- [ ] **Step 4: Implement `refused_cli_calls` and the stop in `do_dispatch`**

The check runs after the receipt loop and before `_guard_against_spinning`. Message, exact:

`"{label} could not run its goatcode command - permission denied: {command[:160]}. Nothing it did can be recorded, so the run is stopping rather than dispatching it again. Allow it (permission_mode: bypassPermissions in .goatcode/config.yaml, or an allow rule in your Claude settings), then run `goatcode run --resume`."`

`label` is `receipt.label`; `command` is the first refused call of the first such receipt.

- [ ] **Step 5: Run the driver and pipeline suites**

Run: `py -m pytest scripts/tests/test_driver.py scripts/tests/test_pipeline_e2e.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/driver.py scripts/tests/test_driver.py scripts/tests/test_pipeline_e2e.py
git commit -m "stop the run when an agent's goatcode call is refused, instead of reading it as silence"
```

---

### Task 3: the driver reads its own settings live

**Files:**
- Modify: `scripts/goatcode/driver.py` (new `LIVE_KEYS`, `live_settings`), `scripts/goatcode.py` (`cmd_run`, where `config = dict(run.config)` is built)
- Modify: `templates/config.yaml` (the `permission_mode` comment), `docs/PIPELINE.md` (the "Resuming" paragraph about the config snapshot)
- Test: `scripts/tests/test_driver.py`

**Interfaces:**
- Produces: `LIVE_KEYS = ("permission_mode", "max_cost_usd", "agent_timeout_minutes", "claude_bin")` and `live_settings(run) -> dict`: a copy of `run.config` with each `LIVE_KEYS` value taken from `runmod.load_config(run.repo)`. Every other key stays the snapshot's. `RunError` from `load_config` propagates.

- [ ] **Step 1: Write the failing tests**

```python
def _config_file(run, text):
    path = run.repo / ".goatcode" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_driver_takes_its_own_settings_from_the_file(run):
    """goat-bug: bypassPermissions in config.yaml never reached a resumed run."""
    _config_file(run, "permission_mode: bypassPermissions\nmax_cycles: 9\n")
    settings = drivermod.live_settings(run)
    assert settings["permission_mode"] == "bypassPermissions"
    assert settings["max_cycles"] == run.config["max_cycles"], "pipeline policy stays the run's own"


def test_a_config_that_does_not_parse_stops_a_resume_with_its_reason(run):
    _config_file(run, "permission_mode: [bypass\n")
    with pytest.raises(RunError, match="config.yaml"):
        drivermod.live_settings(run)
```

(`run` is the `conftest` fixture; import `RunError` from `goatcode.run`.)

- [ ] **Step 2: Run them**

Run: `py -m pytest scripts/tests/test_driver.py -k "settings or resume" -v`
Expected: FAIL with `AttributeError: ... live_settings`.

- [ ] **Step 3: Implement `live_settings`, and use it in `cmd_run`**

`cmd_run` builds `config = drivermod.live_settings(run)`, then applies `--max-cost` / `--claude-bin` / `--claude-arg` on top as today. When `config["permission_mode"]` differs from `run.config.get("permission_mode")`, print once: `"permission_mode: {live} from .goatcode/config.yaml (this run started with {snapshot})"`.

- [ ] **Step 4: Update the docs**

The template's `permission_mode` comment gains: "Read each time `goatcode run` starts, `--resume` included, so an edit reaches a run already under way." The PIPELINE "Resuming" paragraph gains one sentence after the snapshot one: the standalone driver's own settings (`permission_mode`, `max_cost_usd`, `agent_timeout_minutes`, `claude_bin`) are the exception, read from the file every time `goatcode run` starts.

- [ ] **Step 5: Run the driver, cli and plugin tests**

Run: `py -m pytest scripts/tests/test_driver.py scripts/tests/test_cli.py scripts/tests/test_plugin.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/driver.py scripts/goatcode.py scripts/tests/test_driver.py templates/config.yaml docs/PIPELINE.md
git commit -m "read the driver's own settings from config.yaml on every start, resume included"
```

---

### Task 4: no escalation that changes no model

**Files:**
- Modify: `scripts/goatcode/machine.py` (`_retry_blocked`)
- Test: `scripts/tests/test_machine.py`

**Interfaces:**
- Consumes: `_executor_model(run, item, slice_id)`, `_model(run, "executor_escalated")`, `tasks.get(doc, slice_id)`.
- Produces: `_retry_blocked` skips a blocked slice whose current executor model already equals `executor_escalated`. The slice stays blocked, and `_fail_stuck` takes it as for any slice with no retry left.

- [ ] **Step 1: Write the failing test**

```python
def test_a_blocked_slice_already_on_the_stronger_model_is_not_retried(run):
    """goat-bug: the retry went to sonnet on slices already on sonnet; only the
    prompt changed, so the dispatch cap reset and each slice ran six times."""
    write_plan(run)
    approved(run)
    doc = miniyaml.load(run.tasks_path)
    for item in doc["slices"]:
        item["model"] = run.config["models"]["executor_escalated"]
    miniyaml.dump(doc, run.tasks_path)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "blocked")
    machine.next_action(run)
    assert run.escalations("S1") == 0
    assert tasks.get(miniyaml.load(run.tasks_path), "S1")["status"] != "pending"
```

(Match the model-key lookup to how `_model` reads it if `run.config["models"]` is not its shape; import `miniyaml` if the file does not already.)

- [ ] **Step 2: Run it**

Run: `py -m pytest scripts/tests/test_machine.py -k already_on_the_stronger -v`
Expected: FAIL: `escalations("S1") == 1` and S1 back at `pending`.

- [ ] **Step 3: Implement the skip in `_retry_blocked`**

- [ ] **Step 4: Run the machine and pipeline suites**

Run: `py -m pytest scripts/tests/test_machine.py scripts/tests/test_pipeline_e2e.py -v`
Expected: PASS. `test_a_blocked_slice_is_retried_once_then_the_run_moves_on` (haiku -> sonnet) is unchanged.

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/machine.py scripts/tests/test_machine.py
git commit -m "skip a blocked slice's retry when the stronger model is the one it already had"
```

---

### Task 5: stop after two remedial cycles that merged nothing

**Files:**
- Modify: `scripts/goatcode/diffpkg.py` (rename `_judged_ref` to `judged_ref`, both call sites), `scripts/goatcode/machine.py` (`_replan`, new `_stalled_ref`), `scripts/goatcode.py` (`cmd_reopen`)
- Modify: `docs/PIPELINE.md` (the replan / cycle-cap section: one paragraph on the breaker), `scripts/goatcode/__init__.py` and `.claude-plugin/plugin.json` (version `0.6.1`)
- Test: `scripts/tests/test_machine.py`, `scripts/tests/test_cli.py`

**Interfaces:**
- Produces: `diffpkg.judged_ref(run, cycle) -> str | None` (same behaviour, public).
- Produces: `machine._stalled_ref(run) -> str | None`: with `c = run.cycle`, the ref when `judged_ref` of cycles `c-2`, `c-1` and `c` are all non-None and equal, and `c - 2 >= max(1, run.state.get("stall_floor", 1))`; else `None`.
- Produces: `cmd_reopen` sets `run.state["stall_floor"] = run.cycle` with the other resets.
- `_replan`, in the `not awaiting_replan` branch and before returning the `cycle` command: when `_stalled_ref(run)` is set, return `_stop(run, evidence, outcome="failed", reason=..., details=[...])` with:
  - reason: `"cycles {c-2}-{c} were all verified at {ref[:12]}: nothing merged in two remedial cycles, so another replan would judge the same tree; goatcode reopen gives it more once the cause is fixed"`
  - details: `["slice reports: {cycle_dir(c)/'reports'}", "last verdict: {cycle_dir(c)/'verdict.md'}"]`

- [ ] **Step 1: Write the failing tests in `test_machine.py`**

```python
def _cycles_verified_at(run, refs):
    """A planned run whose cycles 1..len(refs) verified at these refs; None = no gates.json."""
    write_plan(run)
    approved(run)
    run.state["config"]["max_cycles"] = 9
    run.state["cycle"] = len(refs)
    run.save()
    for cycle, ref in enumerate(refs, 1):
        run.cycle_dir(cycle).mkdir(parents=True, exist_ok=True)
        if ref:
            osenv.write_json(run.cycle_dir(cycle) / "gates.json", {"ref": ref, "gates": {}})
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: FAIL\n", encoding="utf-8")


def test_two_remedial_cycles_that_merged_nothing_stop_the_run(run):
    """goat-bug: nine cycles verified the same integration HEAD."""
    _cycles_verified_at(run, ["906dcdf" * 6, "906dcdf" * 6, "906dcdf" * 6])
    action = machine.next_action(run)
    assert action["action"] == "stop" and action["outcome"] == "failed"
    assert "cycles 1-3" in action["reason"] and "906dcdf906dc" in action["reason"]
    assert "reports" in action["message"]


def test_one_empty_cycle_is_not_a_stall(run):
    _cycles_verified_at(run, ["a" * 40, "b" * 40, "b" * 40])
    assert machine.next_action(run)["action"] == "run"


def test_a_cycle_that_never_reached_verify_is_not_a_stall(run):
    _cycles_verified_at(run, [None, "b" * 40, "b" * 40])
    assert machine.next_action(run)["action"] == "run"


def test_cycles_before_a_reopen_do_not_count(run):
    _cycles_verified_at(run, ["b" * 40, "b" * 40, "b" * 40])
    run.state["stall_floor"] = 2
    run.save()
    assert machine.next_action(run)["action"] == "run"
```

And in `test_cli.py`, extend `test_reopen_raises_the_cap_and_the_run_replans` (or add a sibling test using `_failed_at_the_cap`) to assert `Run.load(...).state["stall_floor"] == 4` after reopen.

- [ ] **Step 2: Run them**

Run: `py -m pytest scripts/tests/test_machine.py scripts/tests/test_cli.py -k "stall or merged_nothing or never_reached or before_a_reopen or reopen" -v`
Expected: `test_two_remedial_cycles_that_merged_nothing_stop_the_run` FAILS (action is `run`), the reopen assertion FAILS (`KeyError: 'stall_floor'`), and the other three pass already (they pin that the breaker does not over-fire).

- [ ] **Step 3: Implement the rename, `_stalled_ref`, the `_replan` stop and the `reopen` floor**

- [ ] **Step 4: Document it and bump the version**

PIPELINE: one paragraph beside the cycle-cap text, saying the run stops when three consecutive cycles verify the same integration commit, because remedial slices that merge nothing leave the verifier judging the same tree, and `reopen` starts the count again. Version `0.6.1` in both files.

- [ ] **Step 5: Run the full suite**

Run: `py -m pytest scripts/tests -q`
Expected: PASS, 0 failed. Specifically `test_the_cycle_cap_stops_instead_of_looping` still ends on the cycle cap: the fake replanner adds a committing remedial slice each cycle, so refs differ. If it ends on the breaker instead, the fake's remedial slice merged nothing; fix the fixture, not the breaker.

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/diffpkg.py scripts/goatcode/machine.py scripts/goatcode.py scripts/tests/test_machine.py scripts/tests/test_cli.py docs/PIPELINE.md scripts/goatcode/__init__.py .claude-plugin/plugin.json
git commit -m "stop after two remedial cycles that merged nothing, and bump to 0.6.1"
```
