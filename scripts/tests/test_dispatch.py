"""Rendered dispatch prompts.

The point of rendering these in Python is that an agent's instructions no
longer depend on the orchestrator model. So the assertions here are about
what must always be present, and what must never be inlined.
"""

from __future__ import annotations

import copy
import sys

import pytest

from goatcode import dispatch, miniyaml
from goatcode.run import Run

PLAN = {
    "version": 1,
    "run_id": "placeholder",
    "cycle": 1,
    "goal": "Users sign in with a magic link.",
    "global_constraints": ["Node >= 20"],
    "slices": [
        {
            "id": "S1",
            "title": "Token store",
            "intent": "Persist single-use tokens.",
            "depends_on": [],
            "owns": ["src/auth/**"],
            "interfaces": ["createToken(email): Token"],
            "acceptance": [{"id": "A1", "text": "consumable once"}],
            "tests": ["tests/auth.test.ts"],
            "status": "pending",
            "worktree": "/tmp/goatcode/ab/S1",
        },
        {
            "id": "S2",
            "title": "Mailer",
            "intent": "Send the link.",
            "depends_on": ["S1"],
            "uses_interfaces": ["createToken(email): Token"],
            "owns": ["src/mail/**"],
            "acceptance": [{"id": "A1", "text": "one email"}],
            "tests": ["tests/mail.test.ts"],
            "status": "pending",
            "notes": "Reuse the transport in src/mail/transport.ts:12.",
        },
    ],
}


@pytest.fixture
def run(git_repo):
    return Run.create(git_repo, "magic link", "chat")


@pytest.fixture
def plan(run):
    doc = copy.deepcopy(PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    return doc


# -- the rendered command --------------------------------------------------


def test_command_pins_interpreter_repo_and_run(run):
    text = dispatch.command(run, "report", "--slice", "S1")
    assert sys.executable in text
    assert "goatcode.py" in text
    assert str(run.repo) in text
    assert run.run_id in text
    assert "report --slice S1" in text


def test_command_survives_spaces_in_paths(run):
    text = dispatch.command(run, "report", "--tests", "7 passed, 0 failed")
    assert '"7 passed, 0 failed"' in text


def test_command_uses_an_absolute_cli_path():
    assert dispatch.CLI.is_absolute()
    assert dispatch.CLI.exists()


# -- executor --------------------------------------------------------------


def test_executor_prompt_points_at_the_brief_and_does_not_inline_it(run, plan):
    text = dispatch.executor(run, plan, "S1")
    assert str(run.brief_path("S1")) in text
    # The brief's own content must not be duplicated into the prompt.
    assert "src/auth/**" not in text
    assert "consumable once" not in text


def test_executor_prompt_carries_the_report_command(run, plan):
    text = dispatch.executor(run, plan, "S1")
    assert "report --slice S1 --status DONE" in text
    assert "--status BLOCKED" in text
    assert str(run.report_path("S1")) in text


def test_executor_prompt_demands_evidence_per_criterion(run, plan):
    """The command an executor copies out has a slot for every criterion."""
    text = dispatch.executor(run, plan, "S1")
    assert "--evidence A1=<path>:<line>" in text
    assert "One `--evidence` per acceptance criterion (A1)" in text


def test_executor_prompt_warns_that_done_is_checked(run, plan):
    text = dispatch.executor(run, plan, "S1")
    assert "refuses a `DONE` you have not earned" in text


def test_executor_prompt_names_the_worktree(run, plan):
    assert "/tmp/goatcode/ab/S1" in dispatch.executor(run, plan, "S1")


def test_executor_prompt_lists_inherited_interfaces(run, plan):
    text = dispatch.executor(run, plan, "S2")
    assert "createToken(email): Token" in text
    assert "(from S1)" in text


def test_executor_prompt_omits_the_interface_section_when_there_is_none(run, plan):
    assert "Already built by" not in dispatch.executor(run, plan, "S1")


def test_executor_prompt_includes_planner_notes(run, plan):
    text = dispatch.executor(run, plan, "S2")
    assert "src/mail/transport.ts:12" in text


def test_executor_prompt_for_an_unknown_slice_raises(run, plan):
    with pytest.raises(KeyError):
        dispatch.executor(run, plan, "S9")


# -- planner ---------------------------------------------------------------


def test_planner_prompt_offers_both_outcomes(run):
    text = dispatch.planner(run, 1)
    assert str(run.spec_path) in text
    assert str(run.stack_path) in text
    assert str(dispatch.questions_path(run, 1)) in text
    assert str(run.tasks_path) in text
    assert "QUESTIONS" in text and "PLAN" in text


def test_planner_prompt_reports_the_round_budget(run):
    assert "0 of 3" in dispatch.planner(run, 1)
    run.bump_grill_round()
    assert "1 of 3" in dispatch.planner(run, 2)


def test_forced_final_round_demands_a_plan(run):
    text = dispatch.planner(run, 3, forced=True)
    assert "final round" in text
    assert "must return `PLAN`" in text
    assert "assumptions:" in text


def test_planner_fix_prompt_lists_only_the_validator_errors(run):
    text = dispatch.planner(run, 1, validator_errors=["S1 and S2 both own src/a/**"])
    assert "S1 and S2 both own src/a/**" in text
    assert "change nothing else" in text
    assert "Attempt 1 of 2" in text
    assert "QUESTIONS" not in text, "a fix round must not reopen grilling"


def test_planner_revision_prompt_carries_the_user_feedback(run):
    text = dispatch.planner(run, 1, revision="Split the CLI slice in two.")
    assert "Split the CLI slice in two." in text
    assert "QUESTIONS" not in text


def test_questions_path_is_per_round(run):
    assert dispatch.questions_path(run, 1) != dispatch.questions_path(run, 2)
    assert dispatch.questions_path(run, 1).parent == run.cycle_dir()


# -- synthesizer -----------------------------------------------------------


def test_synthesizer_prompt_names_the_conflict_and_the_continue_command(run, plan):
    state = {
        "worktree": "/tmp/goatcode/ab/_integration",
        "merged": ["S1"],
        "pending": ["S2"],
        "conflicted": "S2",
        "conflicts": ["src/shared/registry.ts"],
    }
    text = dispatch.synthesizer(run, plan, state)
    assert "src/shared/registry.ts" in text
    assert "/tmp/goatcode/ab/_integration" in text
    assert "merge --continue" in text
    assert "--role synthesizer --status CLEAN" in text
    assert "ESCALATE" in text


def test_synthesizer_prompt_forbids_deciding_between_slices(run, plan):
    state = {"worktree": "w", "merged": [], "pending": [], "conflicted": "S1", "conflicts": ["a"]}
    text = dispatch.synthesizer(run, plan, state)
    assert "do not decide" in text
    assert "do not" in text and "write features" in text


# -- verifier --------------------------------------------------------------


def package_for(run):
    return {
        "gates": str(run.cycle_dir() / "gates.json"),
        "review": str(run.cycle_dir() / "review.diff"),
        "tasks": str(run.tasks_path),
        "spec": str(run.spec_path),
        "merge_report": str(run.cycle_dir() / "merge-report.md"),
        "worktree": "/tmp/goatcode/ab/_integration",
        "criteria": [{"slice": "S1", "id": "A1", "text": "x"}, {"slice": "S2", "id": "A1", "text": "y"}],
        "assumptions": ["Token TTL assumed 15 min."],
    }


def test_verifier_prompt_lists_every_input_path(run):
    package = package_for(run)
    text = dispatch.verifier(run, package)
    for key in ("gates", "review", "tasks", "spec", "merge_report", "worktree"):
        assert package[key] in text


def test_verifier_prompt_counts_the_criteria(run):
    assert "2 across 2 slices" in dispatch.verifier(run, package_for(run))


def test_verifier_prompt_surfaces_assumptions(run):
    assert "Token TTL assumed 15 min." in dispatch.verifier(run, package_for(run))


def test_verifier_prompt_demands_the_verdict_line_and_command(run):
    text = dispatch.verifier(run, package_for(run))
    assert "VERDICT: PASS" in text and "VERDICT: FAIL" in text
    assert str(run.cycle_dir() / "verdict.md") in text
    assert text.rstrip().endswith("orchestrator.")
    assert " verdict" in text


def test_verifier_prompt_says_to_fix_nothing(run):
    assert "Fix nothing" in dispatch.verifier(run, package_for(run))


# -- replanner -------------------------------------------------------------


def test_replanner_prompt_points_at_the_previous_cycle(run):
    run.advance_cycle()
    text = dispatch.replanner(run, 1)
    assert str(run.cycle_dir(1) / "verdict.md") in text
    assert str(run.cycle_dir(1) / "review.diff") in text
    assert str(run.cycle_dir(1) / "reports") in text
    assert str(run.tasks_path) in text


def test_replanner_prompt_protects_carried_slices(run):
    run.advance_cycle()
    text = dispatch.replanner(run, 1)
    assert "carried" in text
    assert "nothing re-executes them" in text


# -- persistence -----------------------------------------------------------


def test_write_persists_into_the_cycle_dispatch_directory(run, plan):
    path = dispatch.write(run, "S1", dispatch.executor(run, plan, "S1"))
    assert path == run.cycle_dir() / "dispatch" / "S1.md"
    assert path.read_text(encoding="utf-8").startswith("# Executor dispatch")


def test_write_creates_the_directory_for_a_later_cycle(run, plan):
    run.advance_cycle()
    path = dispatch.write(run, "S1", "hello")
    assert path.parent == run.cycle_dir(2) / "dispatch"
    assert path.exists()


# -- incremental verification ----------------------------------------------
#
# The recorded cycle-3 verifier dispatch ordered a re-judge of all 52 criteria
# across a 6168-line diff for a 213-line delta whose slice changed no
# production file. Nine of twenty-six verifier dispatches were remedial, each
# on opus.


def test_the_first_cycle_asks_for_a_full_judgement(run):
    package = package_for(run)
    text = dispatch.verifier(run, package)
    assert "2 across 2 slices. Every one needs a verdict" in text
    assert "already judged" not in text


def test_a_later_cycle_points_at_the_previous_verdict(run):
    package = package_for(run)
    package["previous_verdict"] = str(run.root / "cycle-1" / "verdict.md")
    package["previous_ref"] = "abc1234"
    package["changed_files"] = ["src/s2/index.js"]
    package["unchanged_slices"] = ["S1"]

    text = dispatch.verifier(run, package)
    assert package["previous_verdict"] in text
    assert "S1" in text
    assert "src/s2/index.js" in text


def test_a_later_cycle_says_the_unchanged_code_was_already_judged(run):
    package = package_for(run)
    package["previous_verdict"] = str(run.root / "cycle-1" / "verdict.md")
    package["previous_ref"] = "abc1234"
    package["changed_files"] = ["src/s2/index.js"]
    package["unchanged_slices"] = ["S1"]

    text = dispatch.verifier(run, package)
    assert "already judged" in text
    assert "carry" in text.lower()


def test_a_later_cycle_that_changed_everything_asks_for_a_full_judgement(run):
    package = package_for(run)
    package["previous_verdict"] = str(run.root / "cycle-1" / "verdict.md")
    package["changed_files"] = ["src/s1/index.js", "src/s2/index.js"]
    package["unchanged_slices"] = []

    text = dispatch.verifier(run, package)
    assert "already judged" not in text


def test_the_verifier_is_pointed_at_weak_assertions_without_being_told_to_fail(run):
    package = package_for(run)
    package["weak_assertions"] = [
        {"path": "tests/test_audit.py", "line": 12, "reason": "count/length compared with an inequality", "source": "assert rows.count() >= 1"}
    ]
    text = dispatch.verifier(run, package)
    assert "tests/test_audit.py:12" in text
    assert "leads, not findings" in text


def test_no_weak_assertion_section_when_the_scan_found_nothing(run):
    text = dispatch.verifier(run, package_for(run))
    assert "leads, not findings" not in text


def test_a_mapping_assumption_renders_as_prose_not_a_python_dict(run):
    """Seen verbatim in a recorded cycle-3 dispatch: the verifier was handed
    `{'Cycle 2 verdict, S3/A2': 'the offline production code is complete...'}`."""
    package = package_for(run)
    package["assumptions"] = [{"Cycle 2 verdict": "S5's readiness body is correct."}]
    text = dispatch.verifier(run, package)
    assert "Cycle 2 verdict: S5's readiness body is correct." in text
    assert "{'" not in text


def test_a_multi_key_mapping_assumption_renders_every_pair(run):
    package = package_for(run)
    package["assumptions"] = [{"one": "first", "two": "second"}]
    text = dispatch.verifier(run, package)
    assert "one: first" in text
    assert "two: second" in text


def test_the_scribe_is_told_to_promote_a_learning_that_recurred(run, plan):
    """The assertion-gap lesson was written in all eight recorded entries and
    the failure recurred all eight times. Writing it again is not the move."""
    text = dispatch.scribe(run, plan, [])
    assert "progress promote" in text
    assert "second time" in text


def test_the_planner_is_shown_the_capped_view_not_the_whole_log(run):
    from goatcode import progress as progressmod

    progressmod.append(run.repo, run, "- What was implemented\n  - a thing")
    text = dispatch.planner(run, 1)
    assert "progress show" in text
    assert "progress show --all" not in text


# -- classifier -------------------------------------------------------------


def test_the_classifier_prompt_names_its_inputs_and_output(run):
    text = dispatch.classifier(run)
    assert str(run.spec_path) in text
    assert str(run.root / "classification.json") in text
    assert "classify" in text


def test_the_classifier_prompt_carries_the_exact_schema(run):
    text = dispatch.classifier(run)
    for field in ("complexity", "risk", "riskFactors", "complexityFactors", "reasoning"):
        assert field in text
    assert "SIMPLE" in text and "CRITICAL" in text


def test_the_classifier_prompt_does_not_inline_the_repository(run):
    """Cost control: the classifier reads metadata, not the codebase."""
    text = dispatch.classifier(run)
    assert "do not read the whole repository" in text.lower()


def test_the_classifier_prompt_says_it_is_advisory(run):
    text = dispatch.classifier(run)
    assert "advisory" in text.lower()
