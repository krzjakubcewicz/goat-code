"""The state machine: what phase the evidence implies, and what to do in it.

These are the tests that replace trusting the orchestrator model to follow
eight steps of prose in order.
"""

from __future__ import annotations

import copy

import pytest

from goatcode import ledger, machine, merge, miniyaml, osenv, report, tasks, worktree
from goatcode.run import Run
from tests import conftest

PLAN = {
    "version": 1,
    "run_id": "placeholder",
    "cycle": 1,
    "goal": "Users sign in with a magic link.",
    "slices": [
        {
            "id": "S1",
            "title": "Token store",
            "intent": "Persist tokens.",
            "depends_on": [],
            "owns": ["src/auth/**"],
            "acceptance": [{"id": "A1", "text": "consumable once"}],
            "tests": ["tests/S1.test.js"],
            "status": "pending",
        },
        {
            "id": "S2",
            "title": "Mailer",
            "intent": "Send the link.",
            "depends_on": [],
            "owns": ["src/mail/**"],
            "acceptance": [{"id": "A1", "text": "one email"}],
            "tests": ["tests/S2.test.js"],
            "status": "pending",
        },
        {
            "id": "S3",
            "title": "Route",
            "intent": "Wire it up.",
            "depends_on": ["S1", "S2"],
            "owns": ["src/routes/**"],
            "acceptance": [{"id": "A1", "text": "route responds"}],
            "tests": ["tests/S3.test.js"],
            "status": "pending",
        },
    ],
}

QUESTIONS = "\n".join(
    [
        "round: 1",
        "questions:",
        "  - id: Q1",
        "    topic: scope",
        "    blocking: true",
        '    question: "Timer or next login?"',
        '    context: "src/auth/session.ts:88 uses a 30-day cookie."',
        "    options:",
        '      - label: "15-minute timer"',
        '        detail: "Standard."',
        '      - label: "Next login"',
        '        detail: "Simpler."',
        '    recommended: "15-minute timer"',
        "",
    ]
)


@pytest.fixture
def run(git_repo):
    """A run classified as needing the full pipeline.

    Every test on this fixture asserts grill, approve or verify behaviour,
    which is what PLANNED_DEVELOPMENT means. Saying so explicitly is more
    honest than depending on "nothing has classified it yet", and it keeps
    these tests describing the pipeline they were written for now that
    classification exists.
    """
    created = Run.create(git_repo, "magic link", "chat")
    created.set_classification(
        {"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT"
    )
    created.set_phase("grill")
    return created


@pytest.fixture
def unclassified_run(git_repo):
    """A run before the classifier has spoken - what `init` leaves behind."""
    created = Run.create(git_repo, "magic link", "chat")
    created.set_phase("grill")
    return created


def write_plan(run, doc=None):
    doc = copy.deepcopy(doc or PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    return doc


def approved(run, branched=True):
    """Approve the plan. By the time executors run the branch exists too."""
    run.set_approval("approved")
    if branched:
        run.adopt_branch("feature/magic-link")
    return run


def e2e_passed(run):
    """A feature run only reaches done once the E2E agent has reported."""
    run.state["e2e"] = {"status": "PASS", "detail": None, "tests": "1 passed"}
    run.save()
    return run


def recorded(run):
    """...and once the run has been written up in the progress log."""
    run.state["scribe"] = {"status": "WRITTEN", "detail": None, "tests": None}
    run.save()
    return run


def finished(run):
    return recorded(e2e_passed(run))


def finish_slice(run, slice_id):
    """Do what an executor does: worktree, commit, report DONE."""
    path, _branch, _setup = worktree.create(run, slice_id, setup=False)
    tasks.set_field(run.tasks_path, slice_id, "worktree", str(path))
    tasks.record_commits(run.tasks_path, slice_id, base=run.base_commit)
    target = path / "tests" / "{}.test.js".format(slice_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("// test\n", encoding="utf-8")
    src = path / "src" / slice_id.lower() / "index.js"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("module.exports = 1;\n", encoding="utf-8")
    osenv.git(["add", "-A"], cwd=path, check=True)
    osenv.git(["commit", "-qm", "{}: work".format(slice_id)], cwd=path, check=True)
    doc = tasks.load(run.tasks_path)
    evidence = {
        cid: "tests/{}.test.js:1".format(slice_id)
        for cid in tasks.criterion_ids(tasks.get(doc, slice_id))
    }
    report.record_slice(run, slice_id, "DONE", tests="1 passed", evidence=evidence)
    return path


# -- phase derivation ------------------------------------------------------


def test_a_classified_run_starts_by_grilling(run):
    assert machine.derive_phase(run) == "grill"


def test_questions_on_disk_mean_ask(run):
    (run.cycle_dir() / "questions-round-1.yaml").write_text(QUESTIONS, encoding="utf-8")
    assert machine.derive_phase(run) == "ask"


def test_an_answered_round_goes_back_to_grill(run):
    path = run.cycle_dir() / "questions-round-1.yaml"
    path.write_text(QUESTIONS, encoding="utf-8")
    report.record_answers(run, path, {"Q1": "15-minute timer"})
    assert machine.derive_phase(run) == "grill", "round 2's questions do not exist yet"


def test_an_invalid_plan_means_plan(run):
    doc = copy.deepcopy(PLAN)
    doc["slices"][1]["owns"] = ["src/auth/**"]  # collides with S1 in the same wave
    write_plan(run, doc)
    assert machine.derive_phase(run) == "plan"


def test_unparseable_yaml_means_plan(run):
    run.tasks_path.write_text("version: 1\nslices: &anchor\n", encoding="utf-8")
    assert machine.derive_phase(run) == "plan"


def test_a_valid_plan_in_chat_mode_needs_approval(run):
    write_plan(run)
    assert machine.derive_phase(run) == "approve"


def test_spec_mode_skips_straight_to_execute(git_repo):
    """Spec mode waives the approval gate, not the classification: the spec
    file is exactly what the classifier reads."""
    run = Run.create(git_repo, "x", "spec")
    run.set_classification(
        {"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT"
    )
    write_plan(run)
    assert machine.derive_phase(run) == "execute"


def test_revise_sends_the_plan_back_to_the_planner(run):
    write_plan(run)
    run.set_approval("revise")
    assert machine.derive_phase(run) == "grill"


def test_an_approved_plan_executes(run):
    write_plan(run)
    approved(run)
    assert machine.derive_phase(run) == "execute"


def test_a_blocked_slice_keeps_the_run_executing(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2"):
        tasks.set_status(run.tasks_path, slice_id, "blocked")
    assert machine.derive_phase(run) == "execute"


def test_all_slices_finished_moves_to_synthesize(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    assert machine.derive_phase(run) == "synthesize"


def test_a_clean_merge_moves_to_verify(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "merged": [], "pending": [], "worktree": "w"}
    run.save()
    assert machine.derive_phase(run) == "verify"


def test_a_passing_verdict_on_a_feature_goes_to_e2e(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    assert machine.derive_phase(run) == "e2e"


def test_a_passing_verdict_is_done_once_e2e_has_reported(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)
    assert machine.derive_phase(run) == "done"


def test_a_failing_verdict_replans(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: FAIL\n", encoding="utf-8")
    assert machine.derive_phase(run) == "replan"


def test_awaiting_replan_holds_while_nothing_is_ready(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "carried")
    run.state["awaiting_replan"] = True
    run.save()
    assert machine.derive_phase(run) == "replan"


def test_awaiting_replan_yields_once_the_replanner_delivers(run):
    """The flag is stale the moment there is remedial work to execute."""
    write_plan(run)
    approved(run)
    run.state["awaiting_replan"] = True
    run.save()
    assert machine.derive_phase(run) == "execute"


@pytest.mark.parametrize("terminal", ["done", "failed", "aborted"])
def test_terminal_phases_are_sticky(run, terminal):
    run.set_phase(terminal)
    assert machine.derive_phase(run) == terminal


def test_a_stale_phase_self_corrects(run):
    """state.json is a cache of the evidence, never the source of truth."""
    write_plan(run)
    approved(run)
    run.set_phase("grill")
    action = machine.next_action(run)
    assert action["phase"] == "execute"
    assert Run.load(run.repo, run.run_id).phase == "execute"


# -- grill actions ---------------------------------------------------------


def test_grill_dispatches_the_planner_on_opus(run):
    action = machine.next_action(run)
    assert action["action"] == "dispatch"
    assert action["dispatches"][0]["agent"] == "goat-code-planner"
    assert action["dispatches"][0]["model"] == "opus"
    assert "round 1 of 3" in action["reason"]


def test_the_planner_prompt_is_written_to_disk(run):
    action = machine.next_action(run)
    prompt = action["dispatches"][0]["prompt"]
    assert prompt.endswith("planner-round-1.md")
    assert str(run.spec_path) in open(prompt, encoding="utf-8").read()


def test_the_final_grill_round_forces_a_plan(run):
    for _ in range(3):
        run.bump_grill_round()
    action = machine.next_action(run)
    assert "cap reached" in action["reason"]
    assert "must return `PLAN`" in open(action["dispatches"][0]["prompt"], encoding="utf-8").read()


def test_revise_carries_the_feedback_and_clears_the_flag(run):
    write_plan(run)
    run.set_approval("revise")
    run.state["approval_feedback"] = "Split the CLI slice."
    run.save()

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-planner"
    assert "Split the CLI slice." in open(action["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert Run.load(run.repo, run.run_id).approval is None


# -- ask actions -----------------------------------------------------------


def test_ask_builds_an_askuserquestion_payload(run):
    (run.cycle_dir() / "questions-round-1.yaml").write_text(QUESTIONS, encoding="utf-8")
    action = machine.next_action(run)

    assert action["action"] == "ask"
    question = action["ask"]["questions"][0]
    assert question["id"] == "Q1"
    assert question["header"] == "scope"
    assert question["question"] == "Timer or next login?"
    assert [o["label"] for o in question["options"]] == ["15-minute timer (Recommended)", "Next login"]
    assert "session.ts:88" in question["context"]


def test_ask_tells_the_orchestrator_how_to_record(run):
    (run.cycle_dir() / "questions-round-1.yaml").write_text(QUESTIONS, encoding="utf-8")
    action = machine.next_action(run)
    assert "answer" in action["ask"]["record"]
    assert "QID=answer" in action["ask"]["record_note"]


def test_an_unreadable_questions_file_escalates(run):
    (run.cycle_dir() / "questions-round-1.yaml").write_text("nope: true\n", encoding="utf-8")
    action = machine.next_action(run)
    assert action["action"] == "escalate"


# -- plan actions ----------------------------------------------------------


def test_an_invalid_plan_goes_back_with_the_errors(run):
    doc = copy.deepcopy(PLAN)
    doc["slices"][1]["owns"] = ["src/auth/**"]
    write_plan(run, doc)

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-planner"
    text = open(action["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert "same wave and both own" in text
    assert "change nothing else" in text


def test_the_plan_fix_cap_stops_the_run(run):
    doc = copy.deepcopy(PLAN)
    doc["slices"][1]["owns"] = ["src/auth/**"]
    write_plan(run, doc)

    assert machine.next_action(run)["action"] == "dispatch"
    assert machine.next_action(run)["action"] == "dispatch"
    action = machine.next_action(run)

    assert action["action"] == "stop"
    assert action["outcome"] == "failed"
    assert "could not produce a valid plan" in action["reason"]
    assert any("same wave" in d for d in action["details"])


# -- approve actions -------------------------------------------------------


def test_approve_asks_the_user_and_offers_the_plan_table(run):
    write_plan(run)
    action = machine.next_action(run)

    assert action["action"] == "ask"
    assert action["ask"]["kind"] == "approval"
    assert [o["label"] for o in action["ask"]["questions"][0]["options"]] == ["Approve", "Revise", "Abort"]
    assert action["commands"][0][-2:] == ["plan", "show"]
    assert "--yes" in action["ask"]["record"]


def test_approve_surfaces_warnings_and_assumptions(run):
    doc = copy.deepcopy(PLAN)
    doc["assumptions"] = ["Token TTL assumed 15 min."]
    del doc["slices"][0]["intent"]
    write_plan(run, doc)

    action = machine.next_action(run)
    assert action["ask"]["assumptions"] == ["Token TTL assumed 15 min."]
    assert any("intent" in w for w in action["ask"]["warnings"])


# -- execute actions -------------------------------------------------------


def test_execute_first_prepares_worktrees_and_briefs(run):
    write_plan(run)
    approved(run)
    action = machine.next_action(run)

    assert action["action"] == "run"
    assert ["worktree", "create", "S1", "S2"] == action["commands"][0][-4:]
    assert ["brief", "S1", "S2"] == action["commands"][1][-3:]


def test_execute_dispatches_the_whole_wave_in_one_action(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2"):
        path, _b, _s = worktree.create(run, slice_id, setup=False)
        tasks.set_field(run.tasks_path, slice_id, "worktree", str(path))

    action = machine.next_action(run)
    assert action["action"] == "dispatch"
    assert [d["slice"] for d in action["dispatches"]] == ["S1", "S2"]
    assert all(d["agent"] == "goat-code-executor" for d in action["dispatches"])
    assert all(d["model"] == "haiku" for d in action["dispatches"])
    assert "ONE message" in action["message"]


def test_execute_respects_the_parallel_cap(run):
    write_plan(run)
    approved(run)
    run.state["config"]["parallel"] = 1
    run.save()
    action = machine.next_action(run)
    assert action["commands"][0][-2:] == ["create", "S1"]


def test_a_slice_model_overrides_the_default(run):
    doc = copy.deepcopy(PLAN)
    doc["slices"][0]["model"] = "opus"
    write_plan(run, doc)
    approved(run)
    for slice_id in ("S1", "S2"):
        path, _b, _s = worktree.create(run, slice_id, setup=False)
        tasks.set_field(run.tasks_path, slice_id, "worktree", str(path))

    models = {d["slice"]: d["model"] for d in machine.next_action(run)["dispatches"]}
    assert models == {"S1": "opus", "S2": "haiku"}


def test_the_second_wave_waits_for_the_first(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2"):
        finish_slice(run, slice_id)

    action = machine.next_action(run)
    assert action["action"] == "run"
    assert action["commands"][0][-2:] == ["create", "S3"]


def test_a_blocked_slice_is_retried_once_on_a_stronger_model(run):
    write_plan(run)
    approved(run)
    path, _b, _s = worktree.create(run, "S1", setup=False)
    tasks.set_field(run.tasks_path, "S1", "worktree", str(path))
    tasks.set_status(run.tasks_path, "S1", "blocked")
    tasks.set_status(run.tasks_path, "S2", "done")
    tasks.set_status(run.tasks_path, "S3", "done")

    action = machine.next_action(run)
    assert [d["slice"] for d in action["dispatches"]] == ["S1"]
    assert action["dispatches"][0]["model"] == "sonnet", "escalated, not the default haiku"
    assert Run.load(run.repo, run.run_id).escalations("S1") == 1


def test_a_slice_blocked_twice_is_failed_not_retried_forever(run):
    write_plan(run)
    approved(run)
    path, _b, _s = worktree.create(run, "S1", setup=False)
    tasks.set_field(run.tasks_path, "S1", "worktree", str(path))
    tasks.set_status(run.tasks_path, "S2", "done")
    tasks.set_status(run.tasks_path, "S3", "done")

    tasks.set_status(run.tasks_path, "S1", "blocked")
    machine.next_action(run)  # first escalation
    tasks.set_status(run.tasks_path, "S1", "blocked")
    action = machine.next_action(run)

    assert action["action"] != "dispatch" or action["dispatches"][0]["agent"] != "goat-code-executor"
    assert tasks.get(tasks.load(run.tasks_path), "S1")["status"] == "failed"


# -- synthesize and verify -------------------------------------------------


def test_synthesize_runs_the_merge_first(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")

    action = machine.next_action(run)
    assert action["action"] == "run"
    assert action["commands"][0][-1] == "merge"


def test_a_conflict_dispatches_the_synthesizer_on_sonnet(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {
        "status": "conflict",
        "worktree": "w",
        "merged": ["S1"],
        "pending": ["S2"],
        "conflicted": "S2",
        "conflicts": ["src/shared/registry.ts"],
    }
    run.save()

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-synthesizer"
    assert action["dispatches"][0]["model"] == "sonnet"
    assert "src/shared/registry.ts" in open(action["dispatches"][0]["prompt"], encoding="utf-8").read()


def test_a_clean_merge_never_wakes_the_synthesizer(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": ["S1"], "pending": []}
    run.save()

    action = machine.next_action(run)
    assert action["action"] == "run"
    assert action["commands"][0][-1] == "verify-package"


def test_verify_dispatches_the_verifier_once_the_package_exists(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-verifier"
    assert action["dispatches"][0]["model"] == "opus"
    text = open(action["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert "3 across 3 slices" in text


def test_a_remedial_cycle_narrows_the_verifier_to_what_moved(run, git_repo):
    """The most expensive thing the pipeline repeats, and mostly needlessly."""
    write_plan(run)
    approved(run)
    first = conftest.commit_file(git_repo, "src/s1/index.js", "one", "s1")
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}, "ref": first})
    osenv.write_text(run.cycle_dir() / "verdict.md", "VERDICT: FAIL" + "\n")

    run.advance_cycle()
    run.set_approval("approved")  # cycle 2 of a high-risk run gates again
    second = conftest.commit_file(git_repo, "src/s2/index.js", "two", "s2")
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": str(git_repo), "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}, "ref": second})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")

    action = machine.next_action(run)
    text = open(action["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert "already judged" in text
    assert "src/s2/index.js" in text
    assert "S1" in text


# -- replan ----------------------------------------------------------------


def test_a_failing_verdict_opens_the_next_cycle(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: FAIL\n", encoding="utf-8")

    action = machine.next_action(run)
    assert action["action"] == "run"
    assert action["commands"][0][-1] == "cycle"


def test_after_cycle_the_replanner_is_dispatched(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "carried")
    run.state["awaiting_replan"] = True
    run.save()

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-replanner"
    assert action["dispatches"][0]["model"] == "opus"


def test_the_cycle_cap_stops_the_run(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "carried")
    run.state["cycle"] = 4
    run.state["awaiting_replan"] = True
    run.save()

    action = machine.next_action(run)
    assert action["action"] == "stop"
    assert action["outcome"] == "failed"
    assert "cycle cap of 3" in action["reason"]


def test_the_replanned_plan_resumes_executing(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "carried")
    run.state["awaiting_replan"] = True
    run.save()

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-replanner"

    # The replanner adds a remedial slice; the run goes straight back to work.
    doc = tasks.load(run.tasks_path)
    doc["slices"].append(
        {
            "id": "R2",
            "title": "Fix it",
            "intent": "Remedy the failure.",
            "depends_on": [],
            "owns": ["src/fix/**"],
            "acceptance": [{"id": "A1", "text": "fixed"}],
            "tests": ["tests/R2.test.js"],
            "status": "pending",
        }
    )
    miniyaml.dump(doc, run.tasks_path)

    action = machine.next_action(run)
    assert action["phase"] == "execute"
    assert Run.load(run.repo, run.run_id).state["awaiting_replan"] is False


# -- stop ------------------------------------------------------------------


def test_done_reports_the_branch_and_leaves_the_user_branch_alone(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)

    action = machine.next_action(run)
    assert action["action"] == "stop"
    assert action["outcome"] == "done"
    assert action["message"].startswith("DONE")
    assert run.integration_branch in action["message"]
    assert "nothing was committed to your branch" in action["message"]
    assert action["finish"][-1] == "finish"


def test_stop_is_idempotent(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)
    machine.next_action(run)
    assert machine.next_action(run)["outcome"] == "done"


# -- rendering -------------------------------------------------------------


def test_render_shows_a_dispatch_readably(run):
    text = machine.render(machine.next_action(run))
    assert "phase grill" in text
    assert "dispatch: goat-code-planner on opus" in text
    assert "prompt:" in text


def test_render_shows_commands(run):
    write_plan(run)
    approved(run)
    text = machine.render(machine.next_action(run))
    assert "run:" in text
    assert "worktree create S1 S2" in text


def test_render_shows_questions(run):
    (run.cycle_dir() / "questions-round-1.yaml").write_text(QUESTIONS, encoding="utf-8")
    text = machine.render(machine.next_action(run))
    assert "ask Q1: Timer or next login?" in text
    assert "15-minute timer (Recommended)" in text
    assert "record with:" in text


# -- the merge state is real, not just a dict -----------------------------


def test_synthesize_reads_the_real_merge_state(run):
    """End-to-end through merge.py rather than a hand-written state dict."""
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        finish_slice(run, slice_id)

    action = machine.next_action(run)
    assert action["commands"][0][-1] == "merge"

    merge.run_merge(run, tasks.load(run.tasks_path))
    action = machine.next_action(run)
    assert action["action"] == "run"
    assert action["commands"][0][-1] == "verify-package"


# -- the feature branch ----------------------------------------------------


def test_execute_names_the_branch_before_any_code(run):
    write_plan(run)
    approved(run, branched=False)

    action = machine.next_action(run)
    assert action["action"] == "run"
    assert action["commands"][0][-1] == "branch"
    assert "before any code" in action["reason"]


def test_the_branch_step_comes_before_the_worktrees(run):
    write_plan(run)
    approved(run, branched=False)
    assert machine.next_action(run)["commands"][0][-1] == "branch"

    run.adopt_branch("feature/magic-link")
    assert machine.next_action(run)["commands"][0][-4:] == ["worktree", "create", "S1", "S2"]


def test_the_branch_step_happens_once(run):
    write_plan(run)
    approved(run)
    action = machine.next_action(run)
    assert action["commands"][0][-1] != "branch"


# -- the progress log ------------------------------------------------------


def test_a_finished_run_is_written_up_before_it_is_done(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    e2e_passed(run)

    assert machine.derive_phase(run) == "record"
    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-scribe"
    assert action["dispatches"][0]["model"] == "sonnet"


def test_recording_can_be_switched_off(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    e2e_passed(run)
    run.state["config"]["write_progress"] = False
    run.save()
    assert machine.derive_phase(run) == "done"


def test_a_bugfix_is_still_written_up(run):
    """It skips the E2E phase, not the log - its learnings matter as much."""
    write_plan(run, dict(PLAN, kind="bugfix", kind_reason="restores behaviour"))
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    assert machine.derive_phase(run) == "record"


def test_the_scribe_prompt_points_at_the_run_artifacts(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    e2e_passed(run)

    prompt = open(machine.next_action(run)["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert str(run.spec_path) in prompt
    assert str(run.tasks_path) in prompt
    assert str(run.cycle_dir() / "verdict.md") in prompt
    assert "progress append" in prompt
    assert "Learnings" in prompt or "learnings" in prompt


def test_a_dispatch_records_which_model_the_machine_chose(run):
    """`escalations` read empty in every recorded run while 65 of 90 executors
    ran on a model the config never asked for. Nothing wrote down the choice,
    so nothing could notice."""
    action = machine.next_action(run)
    assert action["action"] == "dispatch"

    entries = ledger.entries(run)
    assert any("dispatch goat-code-planner on opus" in entry for entry in entries), entries


def test_an_executor_dispatch_records_its_slice_and_model(run):
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2"):
        path, _b, _s = worktree.create(run, slice_id, setup=False)
        tasks.set_field(run.tasks_path, slice_id, "worktree", str(path))
    machine.next_action(run)

    entries = ledger.entries(run)
    assert any("dispatch goat-code-executor S1 on haiku" in entry for entry in entries), entries


# -- classification ---------------------------------------------------------


def test_a_fresh_run_classifies_before_it_grills(unclassified_run):
    assert machine.derive_phase(unclassified_run) == "classify"


def test_classification_dispatches_the_classifier_on_the_cheap_model(unclassified_run):
    action = machine.next_action(unclassified_run)
    assert action["action"] == "dispatch"
    assert action["dispatches"][0]["agent"] == "goat-code-classifier"
    assert action["dispatches"][0]["model"] == "haiku"


def test_a_classified_run_moves_on_to_grill(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT")
    assert machine.derive_phase(run) == "grill"


def test_a_direct_workflow_skips_the_questions_not_the_planner(run):
    """CORRECTED by controller ruling F1 - see the note at the end of this brief.

    A direct run still derives `grill`; what it skips is being invited to ask.
    The planner is dispatched with the `forced` prompt, which already says
    "you must return PLAN, record anything unresolved as an assumption".
    """
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    assert machine.derive_phase(run) == "grill"

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-planner"
    prompt = open(action["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert "final round" in prompt, "a direct run must not be invited to ask questions"


def test_a_planned_workflow_is_still_invited_to_ask(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT")
    action = machine.next_action(run)
    prompt = open(action["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert "Grill first" in prompt


def test_a_direct_workflow_needs_no_approval(run):
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(run)
    assert machine.derive_phase(run) == "execute", "no approval gate on a direct run"


def test_a_planned_workflow_still_gates(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT")
    write_plan(run)
    assert machine.derive_phase(run) == "approve"


def test_classification_switched_off_behaves_exactly_as_before(git_repo):
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "classifier:\n  enabled: false\n")
    created = Run.create(git_repo, "magic link", "chat")
    created.set_phase("grill")
    assert machine.derive_phase(created) == "grill"


def test_a_direct_workflow_is_done_on_green_gates_without_a_verifier(run):
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}, "regressions": []})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")
    finished(run)

    assert machine.derive_phase(run) == "done", "gates alone decide a direct run"


def test_a_direct_workflow_fails_rather_than_replanning_on_a_red_gate(run):
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}, "regressions": ["test"]})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")

    assert machine.derive_phase(run) == "failed"


def test_a_planned_workflow_still_dispatches_the_verifier(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT")
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-verifier"


# -- reassessing risk once the plan exists ----------------------------------


def test_a_plan_that_reaches_into_sensitive_paths_escalates_the_run(run):
    """The second deterministic pass: the spec was innocent, the plan is not."""
    run.set_classification(
        {"complexity": "SIMPLE", "risk": "LOW", "deterministic_overrides": []},
        "DIRECT_DEVELOPMENT",
    )
    doc = copy.deepcopy(PLAN)
    doc["slices"][0]["owns"] = ["src/auth/**"]
    write_plan(run, doc)

    machine.next_action(run)
    reloaded = Run.load(run.repo)
    assert reloaded.classification["risk"] == "HIGH"
    assert reloaded.workflow == "HIGH_RISK_DEVELOPMENT"


def test_the_second_pass_never_lowers_a_run(run):
    run.set_classification(
        {"complexity": "COMPLEX", "risk": "CRITICAL", "deterministic_overrides": []},
        "HIGH_RISK_DEVELOPMENT",
    )
    write_plan(run)

    machine.next_action(run)
    reloaded = Run.load(run.repo)
    assert reloaded.workflow == "HIGH_RISK_DEVELOPMENT"


def test_the_escalation_is_recorded_in_the_ledger(run):
    run.set_classification(
        {"complexity": "SIMPLE", "risk": "LOW", "deterministic_overrides": []},
        "DIRECT_DEVELOPMENT",
    )
    doc = copy.deepcopy(PLAN)
    doc["slices"][0]["owns"] = [".github/workflows/**"]
    write_plan(run, doc)

    machine.next_action(run)
    assert any("re-classified" in e for e in ledger.entries(run))


def test_an_escalation_is_not_lost_when_the_run_would_otherwise_be_done(run, git_repo):
    """The ordering hazard: a phase computed under the old workflow must not
    be persisted before the escalation, because a terminal one is
    unreachable afterwards - derive_phase short-circuits on it."""
    run.set_classification(
        {"complexity": "SIMPLE", "risk": "LOW", "deterministic_overrides": []},
        "DIRECT_DEVELOPMENT",
    )
    doc = copy.deepcopy(PLAN)
    doc["kind"] = "bugfix"
    doc["kind_reason"] = "no e2e, so a green run would reach done in one call"
    for item in doc["slices"]:
        item["status"] = "done"
    doc["slices"][0]["owns"] = ["src/auth/**"]
    write_plan(run, doc)
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.state["scribe"] = {"status": "WRITTEN"}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}, "regressions": []})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")

    machine.next_action(run)

    reloaded = Run.load(git_repo)
    assert reloaded.workflow == "HIGH_RISK_DEVELOPMENT"
    assert reloaded.phase != "done", "a run needing a verifier must not report finished"


def test_a_plan_that_only_touches_shared_a_sensitive_path_still_escalates(run):
    """`touches_shared` is read alongside `owns`; a glob living only there
    must still trip the rules."""
    run.set_classification(
        {"complexity": "SIMPLE", "risk": "LOW", "deterministic_overrides": []},
        "DIRECT_DEVELOPMENT",
    )
    doc = copy.deepcopy(PLAN)
    doc["slices"][0]["touches_shared"] = ["src/auth/**"]
    write_plan(run, doc)

    machine.next_action(run)
    reloaded = Run.load(run.repo)
    assert reloaded.classification["risk"] == "HIGH"
    assert reloaded.workflow == "HIGH_RISK_DEVELOPMENT"


def test_a_high_risk_run_is_gated_even_when_the_config_says_never(git_repo):
    """Deterministic policy outranks a config that waives review."""
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "approval_gate: never\n")
    created = Run.create(git_repo, "auth change", "chat")
    created.set_classification({"complexity": "SIMPLE", "risk": "HIGH"}, "HIGH_RISK_DEVELOPMENT")
    write_plan(created)

    assert machine.derive_phase(created) == "approve"


def test_a_direct_run_is_still_ungated_when_the_config_says_never(git_repo):
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "approval_gate: never\n")
    created = Run.create(git_repo, "label change", "chat")
    created.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(created)

    assert machine.derive_phase(created) == "execute"


def test_a_high_risk_run_asks_for_sign_off_when_it_stops(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "HIGH"}, "HIGH_RISK_DEVELOPMENT")
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)

    action = machine.next_action(run)
    assert action["action"] == "stop"
    assert "sign-off" in action["message"].lower()
    assert "HIGH_RISK" in action["message"]


def test_an_ordinary_run_stops_without_asking_for_sign_off(run):
    """The shared PLAN fixture owns `src/auth/**`, which the second
    deterministic pass escalates - so an "ordinary" run needs a plan that
    touches nothing the rules care about."""
    doc = copy.deepcopy(PLAN)
    for index, item in enumerate(doc["slices"]):
        item["owns"] = ["src/feature{}/**".format(index)]
    write_plan(run, doc)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)

    action = machine.next_action(run)
    assert action["action"] == "stop"
    assert "sign-off" not in action["message"].lower()


def test_a_run_escalated_after_the_first_cycle_is_still_gated(git_repo):
    """The second deterministic pass can raise a run to high risk on a replan.
    If the gate only ever considered cycle 1, those remedial slices - the ones
    that touch what the rules flagged - would run unreviewed."""
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "approval_gate: never\n")
    created = Run.create(git_repo, "x", "chat")
    created.set_classification(
        {"complexity": "COMPLEX", "risk": "LOW", "deterministic_overrides": []},
        "PLANNED_DEVELOPMENT",
    )
    assert created.needs_approval() is False, "ordinary work honours approval_gate: never"

    created.advance_cycle()
    created.set_classification(
        {"complexity": "COMPLEX", "risk": "HIGH", "deterministic_overrides": ["authentication"]},
        "HIGH_RISK_DEVELOPMENT",
    )
    assert created.needs_approval() is True


def test_the_sign_off_names_a_rules_only_escalation(run):
    """A rules-driven escalation records its reasons in `deterministic_overrides`,
    not `risk_factors` - the message must read the field that is actually set."""
    run.set_classification(
        {
            "complexity": "COMPLEX",
            "risk": "HIGH",
            "risk_factors": [],
            "deterministic_overrides": ["authentication"],
        },
        "HIGH_RISK_DEVELOPMENT",
    )
    doc = copy.deepcopy(PLAN)
    for index, item in enumerate(doc["slices"]):
        item["owns"] = ["src/feature{}/**".format(index)]
    write_plan(run, doc)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)

    action = machine.next_action(run)
    assert action["action"] == "stop"
    assert "authentication" in action["message"]
