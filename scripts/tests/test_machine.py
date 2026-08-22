"""The state machine: what phase the evidence implies, and what to do in it.

These are the tests that replace trusting the orchestrator model to follow
eight steps of prose in order.
"""

from __future__ import annotations

import copy

import pytest

from codag import machine, merge, miniyaml, osenv, report, tasks, worktree
from codag.run import Run

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
    created = Run.create(git_repo, "magic link", "chat")
    created.set_phase("grill")
    return created


def write_plan(run, doc=None):
    doc = copy.deepcopy(doc or PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    return doc


def approved(run):
    run.set_approval("approved")
    return run


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
    report.record_slice(run, slice_id, "DONE", tests="1 passed")
    return path


# -- phase derivation ------------------------------------------------------


def test_a_fresh_run_is_grilling(run):
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
    run = Run.create(git_repo, "x", "spec")
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


def test_a_passing_verdict_is_done(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
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
    assert action["dispatches"][0]["agent"] == "codag-planner"
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
    assert action["dispatches"][0]["agent"] == "codag-planner"
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
    assert action["dispatches"][0]["agent"] == "codag-planner"
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
    assert all(d["agent"] == "codag-executor" for d in action["dispatches"])
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

    assert action["action"] != "dispatch" or action["dispatches"][0]["agent"] != "codag-executor"
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
    assert action["dispatches"][0]["agent"] == "codag-synthesizer"
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
    assert action["dispatches"][0]["agent"] == "codag-verifier"
    assert action["dispatches"][0]["model"] == "opus"
    text = open(action["dispatches"][0]["prompt"], encoding="utf-8").read()
    assert "3 across 3 slices" in text


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
    assert action["dispatches"][0]["agent"] == "codag-replanner"
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
    assert action["dispatches"][0]["agent"] == "codag-replanner"

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
    machine.next_action(run)
    assert machine.next_action(run)["outcome"] == "done"


# -- rendering -------------------------------------------------------------


def test_render_shows_a_dispatch_readably(run):
    text = machine.render(machine.next_action(run))
    assert "phase grill" in text
    assert "dispatch: codag-planner on opus" in text
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
