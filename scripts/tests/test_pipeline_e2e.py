"""Drive a whole cod-ag run through the state machine, with no LLM.

A fake agent does what a real one would - writes the files, commits the
work, runs the reporting command - and the loop is the shipped one,
``driver.Driver``, with the fake standing in for the ``claude`` backend.

So this proves two things at once: the pipeline's control flow is correct
independently of any model's behaviour, and the loop that runs a standalone
`codag run` is the loop these tests exercise.
"""

from __future__ import annotations

import io
import json

import pytest

from codag import agentcli, driver as drivermod, machine, miniyaml, osenv, tasks
from tests.test_cli import cli as cli_module  # noqa: F401
from codag.run import Run

MAX_STEPS = 60


def plan_document(run, slices, kind="feature"):
    return {
        "version": 1,
        "run_id": run.run_id,
        "cycle": run.cycle,
        "goal": "Greet a user by name from the CLI.",
        "kind": kind,
        "kind_reason": "Chosen by the test fixture.",
        "global_constraints": ["No new runtime dependencies"],
        "slices": slices,
    }


def slice_doc(slice_id, owns, depends_on=None, **extra):
    doc = {
        "id": slice_id,
        "title": "Slice {}".format(slice_id),
        "intent": "Deliver {}".format(slice_id),
        "depends_on": depends_on or [],
        "owns": [owns],
        "acceptance": [{"id": "A1", "text": "{} exists".format(slice_id)}],
        "tests": ["tests/{}.test.js".format(slice_id)],
        "status": "pending",
    }
    doc.update(extra)
    return doc


QUESTIONS = "\n".join(
    [
        "round: 1",
        "questions:",
        "  - id: Q1",
        "    topic: scope",
        "    blocking: true",
        '    question: "Greet by first name or full name?"',
        "    options:",
        '      - label: "First name"',
        '        detail: "Shorter."',
        '      - label: "Full name"',
        '        detail: "Formal."',
        '    recommended: "First name"',
        "",
    ]
)


class FakeAgent:
    """Performs, deterministically, what each real agent would do."""

    def __init__(
        self, slices, ask_first=True, fail_verdicts=0, block=None, conflict=False,
        kind="feature", e2e_status="PASS", subprocess=False,
    ):
        self.subprocess = subprocess
        self.repo = None  # set by make_driver
        self.slices = slices
        self.kind = kind
        self.e2e_status = e2e_status
        self.ask_first = ask_first
        self.fail_verdicts = fail_verdicts
        self.block = dict(block or {})
        self.conflict = conflict
        self.planner_rounds = 0
        self.verdicts = []
        self.dispatched = []

    def cli(self, run, *args, **kwargs):
        """The agent's own CLI calls, in whichever mode the test asked for."""
        runner = cli_subprocess if self.subprocess else cli
        return runner(run, *args, **kwargs)

    # -- the agents --------------------------------------------------------

    def planner(self, run, _dispatch):
        self.planner_rounds += 1
        if self.ask_first and self.planner_rounds == 1:
            (run.cycle_dir() / "questions-round-1.yaml").write_text(QUESTIONS, encoding="utf-8")
            return
        miniyaml.dump(plan_document(run, self.slices, self.kind), run.tasks_path)

    def executor(self, run, entry):
        slice_id = entry["slice"]
        remaining = self.block.get(slice_id, 0)
        if remaining:
            self.block[slice_id] = remaining - 1
            self.cli(run, "report", "--slice", slice_id, "--status", "BLOCKED", "--reason", "stuck")
            return

        doc = tasks.load(run.tasks_path)
        path = tasks.get(doc, slice_id)["worktree"]
        _write(path, "tests/{}.test.js".format(slice_id), "// test\n")
        body = "shared\n" if self.conflict else "module.exports = '{}';\n".format(slice_id)
        target = "shared.js" if self.conflict else "src/{}/index.js".format(slice_id.lower())
        _write(path, target, "{}{}".format(body, slice_id))
        osenv.git(["add", "-A"], cwd=path, check=True)
        osenv.git(["commit", "-qm", "{}: work".format(slice_id)], cwd=path, check=True)
        # A real executor names the test line proving each criterion; the fake
        # one has to as well, or it is not driving the loop that ships.
        args = ["report", "--slice", slice_id, "--status", "DONE", "--tests", "1 passed"]
        for cid in tasks.criterion_ids(tasks.get(doc, slice_id)):
            args += ["--evidence", "{}=tests/{}.test.js:1".format(cid, slice_id)]
        self.cli(run, *args)

    def synthesizer(self, run, _dispatch):
        state = run.state.get("merge") or {}
        worktree_path = state["worktree"]
        for conflict in state.get("conflicts") or []:
            _write(worktree_path, conflict, "resolved by the synthesizer\n")
        self.cli(run, "merge", "--continue", check=False)
        self.cli(run, "report", "--role", "synthesizer", "--status", "CLEAN")

    def verifier(self, run, _dispatch):
        verdict = "FAIL" if len(self.verdicts) < self.fail_verdicts else "PASS"
        self.verdicts.append(verdict)
        (run.cycle_dir() / "verdict.md").write_text(
            "# Verdict\n\nchecked everything\n\nVERDICT: {}\n".format(verdict), encoding="utf-8"
        )
        self.cli(run, "verdict", check=False)

    def e2e(self, run, _dispatch):
        state = run.state.get("merge") or {}
        worktree_path = state.get("worktree")
        if self.e2e_status == "PASS":
            _write(worktree_path, "e2e/journey.test.js", "// end to end\n")
            osenv.git(["add", "-A"], cwd=worktree_path, check=True)
            osenv.git(["commit", "-qm", "test: e2e journey"], cwd=worktree_path, check=True)
            self.cli(run, "report", "--role", "e2e", "--status", "PASS", "--tests", "1 passed")
        else:
            self.cli(run, "report", "--role", "e2e", "--status", self.e2e_status, "--detail", "no runner")

    def scribe(self, run, _dispatch):
        entry = run.cycle_dir() / "progress-entry.md"
        entry.write_text(
            "\n".join(
                [
                    "- What was implemented",
                    "  - the fixture feature",
                    "- Files changed",
                    "  - src/**",
                    "- **Learnings for future iterations:**",
                    "  - the fake agent wrote this",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.cli(run, "progress", "append", "--body", str(entry))

    def replanner(self, run, _dispatch):
        doc = tasks.load(run.tasks_path)
        for item in tasks.slices(doc):
            if item["status"] == "done":
                item["status"] = "carried"
        doc["cycle"] = run.cycle
        remedial = "R{}".format(run.cycle)
        if remedial not in [s["id"] for s in tasks.slices(doc)]:
            doc["slices"].append(slice_doc(remedial, "src/fix{}/**".format(run.cycle)))
        miniyaml.dump(doc, run.tasks_path)

    def __call__(self, entry, on_event=None):
        run = Run.load(self.repo)
        self.dispatched.append((entry["agent"], entry["slice"], entry["model"]))
        handler = {
            "codag-planner": self.planner,
            "codag-executor": self.executor,
            "codag-synthesizer": self.synthesizer,
            "codag-verifier": self.verifier,
            "codag-e2e": self.e2e,
            "codag-scribe": self.scribe,
            "codag-replanner": self.replanner,
        }[entry["agent"]]
        assert open(entry["prompt"], encoding="utf-8").read().strip(), "dispatch prompt is empty"
        handler(run, entry)
        return agentcli.Receipt(entry, ok=True, duration_ms=0)


def make_driver(repo, agent, cls=None):
    """The shipped loop, wired to a fake backend and a silent console."""
    agent.repo = repo
    console = drivermod.Console(stream=io.StringIO(), quiet=True)
    return (cls or drivermod.Driver)(
        repo,
        agent,
        console=console,
        prompter=drivermod.Prompter(console, yes=True),
        limit=MAX_STEPS,
    )


class SubprocessDriver(drivermod.Driver):
    """Runs every codag command as a real subprocess, exactly as rendered."""

    def cli(self, command):
        result = osenv.run(command)
        assert result.returncode in (0, 1), "{}\n{}".format(command, result.stderr)
        return result.returncode


def cli(run, *args, check=True):
    """Run a codag command the way the driver does.

    In-process by default: spawning the CLI costs about 1.3 s a call and the
    driver makes hundreds. ``subprocess=True`` runs the argv exactly as
    rendered - a couple of tests keep that path, so the command strings
    cod-ag writes into dispatch prompts are still proven to execute.
    """
    code = cli_module.main(machine.cli_argv(run, *args)[2:])
    if check:
        assert code in (0, 1), "codag {} exited {}".format(" ".join(str(a) for a in args), code)
    return code


def cli_subprocess(run, *args, check=True):
    command = machine.cli_argv(run, *args)
    result = osenv.run(command)
    if check:
        assert result.ok, "{}\n{}\n{}".format(" ".join(command), result.stdout, result.stderr)
    return result


def _write(root, relpath, text):
    import pathlib

    target = pathlib.Path(root) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


@pytest.fixture
def started(node_repo):
    """A chat-mode run, initialised, ready for the first `next`."""
    command = [
        __import__("sys").executable,
        str(machine.dispatch.CLI),
        "--repo",
        str(node_repo),
        "init",
        "--prompt",
        "greet a user",
        "--no-baseline",
    ]
    result = osenv.run(command)
    assert result.ok, result.stderr
    return node_repo


# -- the happy path --------------------------------------------------------


def test_a_whole_run_reaches_done_with_no_model(started):
    agent = FakeAgent(
        [
            slice_doc("S1", "src/s1/**"),
            slice_doc("S2", "src/s2/**"),
            slice_doc("S3", "src/s3/**", depends_on=["S1", "S2"]),
        ]
    )
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert final["message"].startswith("DONE")

    ordered = [p for i, p in enumerate(driver.phases) if i == 0 or p != driver.phases[i - 1]]
    assert ordered == [
        "grill",
        "ask",
        "grill",
        "approve",
        "execute",
        "synthesize",
        "verify",
        "e2e",
        "record",
        "done",
    ]


def test_the_waves_are_dispatched_in_parallel_batches(started):
    agent = FakeAgent(
        [
            slice_doc("S1", "src/s1/**"),
            slice_doc("S2", "src/s2/**"),
            slice_doc("S3", "src/s3/**", depends_on=["S1", "S2"]),
        ]
    )
    driver = make_driver(started, agent)
    driver.loop()
    assert driver.waves == [["S1", "S2"], ["S3"]], "wave 1 must go out as one batch"


def test_executors_run_on_the_model_the_plan_names(started):
    agent = FakeAgent(
        [slice_doc("S1", "src/s1/**", model="opus"), slice_doc("S2", "src/s2/**")]
    )
    make_driver(started, agent).loop()
    models = {s: m for a, s, m in agent.dispatched if a == "codag-executor"}
    assert models == {"S1": "opus", "S2": "haiku"}


def test_each_role_runs_on_its_own_model(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    make_driver(started, agent).loop()
    by_agent = {a: m for a, _s, m in agent.dispatched}
    assert by_agent["codag-planner"] == "opus"
    assert by_agent["codag-executor"] == "haiku"
    assert by_agent["codag-verifier"] == "opus"


def test_the_work_lands_on_the_integration_branch(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")])
    make_driver(started, agent).loop()

    run = Run.load(started)
    listed = osenv.git_out(["ls-tree", "-r", "--name-only", run.integration_branch], cwd=started)
    assert "src/s1/index.js" in listed
    assert "src/s2/index.js" in listed


def test_the_users_branch_is_untouched(started):
    """Same branch, same commit, and nothing in the tree but the .gitignore
    cod-ag wrote on the first run - which it never commits."""
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    base = osenv.git_out(["rev-parse", "HEAD"], cwd=started)
    make_driver(started, agent).loop()

    assert osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=started) == "main"
    assert osenv.git_out(["rev-parse", "HEAD"], cwd=started) == base
    assert osenv.git(["status", "--porcelain"], cwd=started).out == "?? .gitignore"


def test_the_answers_reach_the_spec(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    make_driver(started, agent).loop()
    body = Run.load(started).spec_path.read_text(encoding="utf-8")
    assert "## Clarifications (round 1)" in body
    assert "**A:** First name" in body


def test_a_precise_spec_skips_the_question_round(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], ask_first=False)
    driver = make_driver(started, agent)
    driver.loop()
    assert "ask" not in driver.phases
    assert agent.planner_rounds == 1


# -- the failure loop ------------------------------------------------------


def test_a_failing_verdict_drives_a_replan_cycle(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=1)
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert agent.verdicts == ["FAIL", "PASS"]
    assert "replan" in driver.phases
    assert Run.load(started).cycle == 2


def test_carried_slices_are_never_re_executed(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=1)
    driver = make_driver(started, agent)
    driver.loop()

    executed = [s for a, s, _m in agent.dispatched if a == "codag-executor"]
    assert executed.count("S1") == 1, "S1 passed in cycle 1 and must not run again"
    assert "R2" in executed, "the remedial slice must run"


def test_the_cycle_cap_stops_instead_of_looping(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=99)
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["action"] == "stop"
    assert final["outcome"] == "failed"
    assert "cycle cap of 3" in final["reason"]
    assert Run.load(started).cycle == 4


def test_a_blocked_slice_is_retried_once_then_the_run_moves_on(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], block={"S1": 99})
    driver = make_driver(started, agent)
    final = driver.loop()

    executed = [s for a, s, m in agent.dispatched if a == "codag-executor"]
    models = [m for a, _s, m in agent.dispatched if a == "codag-executor"]
    assert executed == ["S1", "S1"], "one retry, not an infinite loop"
    assert models == ["haiku", "sonnet"], "the retry escalates the model"
    assert final["action"] == "stop"


def test_a_slice_that_recovers_on_the_retry_completes(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], block={"S1": 1})
    driver = make_driver(started, agent)
    final = driver.loop()
    assert final["outcome"] == "done"


# -- conflicts -------------------------------------------------------------


def test_a_merge_conflict_wakes_the_synthesizer_and_the_run_finishes(started):
    agent = FakeAgent(
        [slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")], conflict=True
    )
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert any(a == "codag-synthesizer" for a, _s, _m in agent.dispatched)


def test_a_clean_merge_never_dispatches_the_synthesizer(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")])
    make_driver(started, agent).loop()
    assert not any(a == "codag-synthesizer" for a, _s, _m in agent.dispatched)


# -- the artifacts a human reads afterwards --------------------------------


def test_the_run_directory_holds_the_whole_record(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    make_driver(started, agent).loop()

    run = Run.load(started)
    cycle = run.cycle_dir(1)
    for path in (
        run.spec_path,
        run.tasks_path,
        run.stack_path,
        run.ledger_path,
        cycle / "verdict.md",
        cycle / "gates.json",
        cycle / "review.diff",
        cycle / "merge-report.md",
        cycle / "briefs" / "S1.md",
        cycle / "dispatch" / "S1.md",
    ):
        assert path.exists(), "missing {}".format(path)


def test_gate_results_are_recorded(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    make_driver(started, agent).loop()
    run = Run.load(started)
    report = json.loads((run.cycle_dir(1) / "gates.json").read_text(encoding="utf-8"))
    assert report["gates"]["test"]["status"] == "pass"


def test_resume_reports_the_true_phase_mid_run(started):
    """The defect that prompted all of this: phase used to be stuck at grill."""
    agent = FakeAgent([slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")])
    driver = make_driver(started, agent)

    seen = []
    for _ in range(6):
        run = Run.load(started)
        action = machine.next_action(run)
        seen.append(Run.load(started).phase)
        if action["action"] == "stop":
            break
        driver.perform(run, action)

    assert "grill" in seen
    assert seen[-1] != "grill", "phase must advance as the run progresses"
    assert Run.load(started).phase in ("execute", "approve", "synthesize", "verify")


# -- the end-to-end phase --------------------------------------------------


def test_a_feature_run_gets_an_e2e_test(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert "e2e" in driver.phases
    assert any(a == "codag-e2e" for a, _s, _m in agent.dispatched)

    run = Run.load(started)
    listed = osenv.git_out(["ls-tree", "-r", "--name-only", run.integration_branch], cwd=started)
    assert "e2e/journey.test.js" in listed


def test_the_e2e_agent_runs_on_sonnet(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    make_driver(started, agent).loop()
    models = {a: m for a, _s, m in agent.dispatched}
    assert models["codag-e2e"] == "sonnet"


def test_a_bugfix_run_skips_the_e2e_phase(started):
    """A bugfix's slices were already forced to be written test-first."""
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], kind="bugfix")
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert "e2e" not in driver.phases
    assert not any(a == "codag-e2e" for a, _s, _m in agent.dispatched)


def test_the_kind_override_beats_the_planners_classification(started):
    """The planner says feature; --kind bugfix at init wins."""
    run = Run.load(started)
    run.set_kind_override("bugfix")

    agent = FakeAgent([slice_doc("S1", "src/s1/**")], kind="feature")
    driver = make_driver(started, agent)
    driver.loop()
    assert "e2e" not in driver.phases


def test_a_failing_e2e_stops_the_run_without_replanning(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], e2e_status="FAILED")
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["action"] == "stop"
    assert final["outcome"] == "failed"
    assert Run.load(started).cycle == 1, "a failing e2e must not burn a replan cycle"


def test_a_skipped_e2e_still_finishes(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], e2e_status="SKIPPED")
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert Run.load(started).state["e2e"]["status"] == "SKIPPED"


def test_e2e_can_be_switched_off(started):
    config = started / ".codag" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("write_e2e_tests: false\n", encoding="utf-8")
    run = Run.load(started)
    run.state["config"]["write_e2e_tests"] = False
    run.save()

    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    driver = make_driver(started, agent)
    assert driver.loop()["outcome"] == "done"
    assert "e2e" not in driver.phases


def test_the_e2e_prompt_names_the_criteria_and_forbids_reading_the_diff(started):
    """The agent must assert the spec, not describe the implementation."""
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    driver = make_driver(started, agent)
    driver.loop()

    prompt = (Run.load(started).cycle_dir(1) / "dispatch" / "e2e.md").read_text(encoding="utf-8")
    assert "S1 exists" in prompt
    assert "Do not read the diff" in prompt
    assert "Test files only" in prompt


# -- the commands really do run as written ---------------------------------
#
# The rest of this file drives the CLI in-process, which is roughly a
# hundred times faster. These two keep the real subprocess path, so the
# command strings cod-ag renders into dispatch prompts - the ones a real
# agent copies and runs - are still proven to execute.


def test_a_whole_run_works_through_real_subprocesses(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], subprocess=True)
    driver = make_driver(started, agent, cls=SubprocessDriver)
    final = driver.loop()

    assert final["outcome"] == "done"
    run = Run.load(started)
    listed = osenv.git_out(["ls-tree", "-r", "--name-only", run.integration_branch], cwd=started)
    assert "src/s1/index.js" in listed


def test_the_rendered_report_command_runs_verbatim(started):
    """An executor copies this string out of its dispatch prompt."""
    run = Run.load(started)
    miniyaml.dump(plan_document(run, [slice_doc("S1", "src/s1/**")]), run.tasks_path)
    cli(run, "approve", "--yes")
    cli(run, "branch")
    cli(run, "worktree", "create", "S1", "--no-setup")

    doc = tasks.load(run.tasks_path)
    path = tasks.get(doc, "S1")["worktree"]
    _write(path, "tests/S1.test.js", "// test\n")
    _write(path, "src/s1/index.js", "module.exports = 1;\n")
    osenv.git(["add", "-A"], cwd=path, check=True)
    osenv.git(["commit", "-qm", "S1: work"], cwd=path, check=True)

    result = cli_subprocess(
        run, "report", "--slice", "S1", "--status", "DONE", "--tests", "1 passed",
        "--evidence", "A1=tests/S1.test.js:1",
    )
    assert result.ok, result.stderr
    stored = tasks.get(tasks.load(run.tasks_path), "S1")
    assert stored["status"] == "done"
    assert stored["report"]["evidence"] == {"A1": "tests/S1.test.js:1"}
