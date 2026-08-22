"""Drive a whole cod-ag run through the state machine, with no LLM.

A fake agent does what a real one would - writes the files, commits the
work, runs the reporting command - and the loop is exactly the one the
orchestrator skill describes: call ``next``, do what it says, repeat.

If this passes, the pipeline's control flow is correct independently of any
model's behaviour. That is the whole point of making the orchestrator
deterministic.
"""

from __future__ import annotations

import json

import pytest

from codag import machine, miniyaml, osenv, tasks
from codag.run import Run
from tests.test_cli import node_repo  # noqa: F401

MAX_STEPS = 60


def plan_document(run, slices):
    return {
        "version": 1,
        "run_id": run.run_id,
        "cycle": run.cycle,
        "goal": "Greet a user by name from the CLI.",
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

    def __init__(self, slices, ask_first=True, fail_verdicts=0, block=None, conflict=False):
        self.slices = slices
        self.ask_first = ask_first
        self.fail_verdicts = fail_verdicts
        self.block = dict(block or {})
        self.conflict = conflict
        self.planner_rounds = 0
        self.verdicts = []
        self.dispatched = []

    # -- the agents --------------------------------------------------------

    def planner(self, run, _dispatch):
        self.planner_rounds += 1
        if self.ask_first and self.planner_rounds == 1:
            (run.cycle_dir() / "questions-round-1.yaml").write_text(QUESTIONS, encoding="utf-8")
            return
        miniyaml.dump(plan_document(run, self.slices), run.tasks_path)

    def executor(self, run, entry):
        slice_id = entry["slice"]
        remaining = self.block.get(slice_id, 0)
        if remaining:
            self.block[slice_id] = remaining - 1
            cli(run, "report", "--slice", slice_id, "--status", "BLOCKED", "--reason", "stuck")
            return

        doc = tasks.load(run.tasks_path)
        path = tasks.get(doc, slice_id)["worktree"]
        _write(path, "tests/{}.test.js".format(slice_id), "// test\n")
        body = "shared\n" if self.conflict else "module.exports = '{}';\n".format(slice_id)
        target = "shared.js" if self.conflict else "src/{}/index.js".format(slice_id.lower())
        _write(path, target, "{}{}".format(body, slice_id))
        osenv.git(["add", "-A"], cwd=path, check=True)
        osenv.git(["commit", "-qm", "{}: work".format(slice_id)], cwd=path, check=True)
        cli(run, "report", "--slice", slice_id, "--status", "DONE", "--tests", "1 passed")

    def synthesizer(self, run, _dispatch):
        state = run.state.get("merge") or {}
        worktree_path = state["worktree"]
        for conflict in state.get("conflicts") or []:
            _write(worktree_path, conflict, "resolved by the synthesizer\n")
        cli(run, "merge", "--continue", check=False)
        cli(run, "report", "--role", "synthesizer", "--status", "CLEAN")

    def verifier(self, run, _dispatch):
        verdict = "FAIL" if len(self.verdicts) < self.fail_verdicts else "PASS"
        self.verdicts.append(verdict)
        (run.cycle_dir() / "verdict.md").write_text(
            "# Verdict\n\nchecked everything\n\nVERDICT: {}\n".format(verdict), encoding="utf-8"
        )
        cli(run, "verdict", check=False)

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

    def __call__(self, run, entry):
        self.dispatched.append((entry["agent"], entry["slice"], entry["model"]))
        handler = {
            "codag-planner": self.planner,
            "codag-executor": self.executor,
            "codag-synthesizer": self.synthesizer,
            "codag-verifier": self.verifier,
            "codag-replanner": self.replanner,
        }[entry["agent"]]
        assert open(entry["prompt"], encoding="utf-8").read().strip(), "dispatch prompt is empty"
        handler(run, entry)


class Driver:
    """The orchestrator loop, with a fake model."""

    def __init__(self, repo, agent):
        self.repo = repo
        self.agent = agent
        self.phases = []
        self.waves = []

    def loop(self, limit=MAX_STEPS):
        for _ in range(limit):
            run = Run.load(self.repo)
            action = machine.next_action(run)
            self.phases.append(action["phase"])

            if action["action"] == "stop":
                return action
            if action["action"] == "escalate":
                raise AssertionError("escalated: {}".format(action["message"]))
            self.perform(run, action)
        raise AssertionError("did not terminate in {} steps: {}".format(limit, self.phases))

    def perform(self, run, action):
        if action["action"] == "run":
            for command in action["commands"]:
                result = osenv.run(command)
                assert result.returncode in (0, 1), "{}\n{}".format(command, result.stderr)
            return

        if action["action"] == "dispatch":
            batch = [d["slice"] for d in action["dispatches"] if d["slice"]]
            if batch:
                self.waves.append(batch)
            for entry in action["dispatches"]:
                self.agent(Run.load(self.repo), entry)
            return

        if action["action"] == "ask":
            if action["ask"].get("kind") == "approval":
                cli(run, "approve", "--yes")
                return
            answers = [
                "{}={}".format(q["id"], _recommended(q))
                for q in action["ask"]["questions"]
            ]
            cli(run, "answer", *answers)
            return

        raise AssertionError("unknown action {!r}".format(action["action"]))


def _recommended(question):
    for option in question["options"]:
        if "(Recommended)" in option["label"]:
            return option["label"].replace(" (Recommended)", "")
    return question["options"][0]["label"]


def cli(run, *args, check=True):
    command = machine._argv(run, *args)
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
    driver = Driver(started, agent)
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
    driver = Driver(started, agent)
    driver.loop()
    assert driver.waves == [["S1", "S2"], ["S3"]], "wave 1 must go out as one batch"


def test_executors_run_on_the_model_the_plan_names(started):
    agent = FakeAgent(
        [slice_doc("S1", "src/s1/**", model="opus"), slice_doc("S2", "src/s2/**")]
    )
    Driver(started, agent).loop()
    models = {s: m for a, s, m in agent.dispatched if a == "codag-executor"}
    assert models == {"S1": "opus", "S2": "haiku"}


def test_each_role_runs_on_its_own_model(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    Driver(started, agent).loop()
    by_agent = {a: m for a, _s, m in agent.dispatched}
    assert by_agent["codag-planner"] == "opus"
    assert by_agent["codag-executor"] == "haiku"
    assert by_agent["codag-verifier"] == "opus"


def test_the_work_lands_on_the_integration_branch(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")])
    Driver(started, agent).loop()

    run = Run.load(started)
    listed = osenv.git_out(["ls-tree", "-r", "--name-only", run.integration_branch], cwd=started)
    assert "src/s1/index.js" in listed
    assert "src/s2/index.js" in listed


def test_the_users_branch_is_untouched(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    base = osenv.git_out(["rev-parse", "HEAD"], cwd=started)
    Driver(started, agent).loop()

    assert osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=started) == "main"
    assert osenv.git_out(["rev-parse", "HEAD"], cwd=started) == base
    assert osenv.git(["status", "--porcelain"], cwd=started).out == ""


def test_the_answers_reach_the_spec(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    Driver(started, agent).loop()
    body = Run.load(started).spec_path.read_text(encoding="utf-8")
    assert "## Clarifications (round 1)" in body
    assert "**A:** First name" in body


def test_a_precise_spec_skips_the_question_round(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], ask_first=False)
    driver = Driver(started, agent)
    driver.loop()
    assert "ask" not in driver.phases
    assert agent.planner_rounds == 1


# -- the failure loop ------------------------------------------------------


def test_a_failing_verdict_drives_a_replan_cycle(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=1)
    driver = Driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert agent.verdicts == ["FAIL", "PASS"]
    assert "replan" in driver.phases
    assert Run.load(started).cycle == 2


def test_carried_slices_are_never_re_executed(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=1)
    driver = Driver(started, agent)
    driver.loop()

    executed = [s for a, s, _m in agent.dispatched if a == "codag-executor"]
    assert executed.count("S1") == 1, "S1 passed in cycle 1 and must not run again"
    assert "R2" in executed, "the remedial slice must run"


def test_the_cycle_cap_stops_instead_of_looping(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], fail_verdicts=99)
    driver = Driver(started, agent)
    final = driver.loop()

    assert final["action"] == "stop"
    assert final["outcome"] == "failed"
    assert "cycle cap of 3" in final["reason"]
    assert Run.load(started).cycle == 4


def test_a_blocked_slice_is_retried_once_then_the_run_moves_on(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], block={"S1": 99})
    driver = Driver(started, agent)
    final = driver.loop()

    executed = [s for a, s, m in agent.dispatched if a == "codag-executor"]
    models = [m for a, _s, m in agent.dispatched if a == "codag-executor"]
    assert executed == ["S1", "S1"], "one retry, not an infinite loop"
    assert models == ["haiku", "sonnet"], "the retry escalates the model"
    assert final["action"] == "stop"


def test_a_slice_that_recovers_on_the_retry_completes(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], block={"S1": 1})
    driver = Driver(started, agent)
    final = driver.loop()
    assert final["outcome"] == "done"


# -- conflicts -------------------------------------------------------------


def test_a_merge_conflict_wakes_the_synthesizer_and_the_run_finishes(started):
    agent = FakeAgent(
        [slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")], conflict=True
    )
    driver = Driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    assert any(a == "codag-synthesizer" for a, _s, _m in agent.dispatched)


def test_a_clean_merge_never_dispatches_the_synthesizer(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")])
    Driver(started, agent).loop()
    assert not any(a == "codag-synthesizer" for a, _s, _m in agent.dispatched)


# -- the artifacts a human reads afterwards --------------------------------


def test_the_run_directory_holds_the_whole_record(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")])
    Driver(started, agent).loop()

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
    Driver(started, agent).loop()
    run = Run.load(started)
    report = json.loads((run.cycle_dir(1) / "gates.json").read_text(encoding="utf-8"))
    assert report["gates"]["test"]["status"] == "pass"


def test_resume_reports_the_true_phase_mid_run(started):
    """The defect that prompted all of this: phase used to be stuck at grill."""
    agent = FakeAgent([slice_doc("S1", "src/s1/**"), slice_doc("S2", "src/s2/**")])
    driver = Driver(started, agent)

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
