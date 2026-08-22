"""Agents reporting through the CLI, and the checks on what they claim.

The DONE verification is the point of this module: it turns "the agent said
it finished" into a fact the pipeline has confirmed.
"""

from __future__ import annotations

import copy

import pytest

from codag import ledger, miniyaml, osenv, report, tasks, worktree
from codag.report import ReportError
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
            "depends_on": [],
            "owns": ["src/auth/**"],
            "acceptance": [{"id": "A1", "text": "consumable once"}],
            "tests": [{"path": "tests/auth.test.js"}],
            "status": "pending",
        },
        {
            "id": "S2",
            "title": "Mailer",
            "depends_on": [],
            "owns": ["src/mail/**"],
            "acceptance": [{"id": "A1", "text": "one email"}],
            "tests": ["tests/mail.test.js"],
            "status": "pending",
        },
    ],
}


@pytest.fixture
def run(git_repo):
    return Run.create(git_repo, "reporting", "chat")


@pytest.fixture
def plan(run):
    doc = copy.deepcopy(PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    return doc


#: The test file each fixture slice's brief declares.
DECLARED_TEST = {"S1": "tests/auth.test.js", "S2": "tests/mail.test.js"}


def prepare(run, slice_id, commit=True, tests=True):
    """Create the worktree and, by default, do the work an executor would."""
    path, _branch, _setup = worktree.create(run, slice_id, setup=False)
    tasks.set_field(run.tasks_path, slice_id, "worktree", str(path))
    tasks.record_commits(run.tasks_path, slice_id, base=run.base_commit)

    if tests:
        target = path / DECLARED_TEST[slice_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// test\n", encoding="utf-8")
    else:
        # Something must exist to commit, or git refuses; make it a file the
        # brief does not ask for, so the missing-test check still fires.
        (path / "notes.txt").write_text("work in progress\n", encoding="utf-8")

    if commit:
        osenv.git(["add", "-A"], cwd=path, check=True)
        osenv.git(["commit", "-qm", "{}: work".format(slice_id)], cwd=path, check=True)
    return path


# -- accepting a real DONE -------------------------------------------------


def test_done_is_accepted_when_the_work_is_real(run, plan):
    path = prepare(run, "S1")
    result = report.record_slice(run, "S1", "DONE", tests="3 passed")

    assert result["status"] == "DONE"
    assert result["slice_status"] == "done"
    assert result["head"] == worktree.head_commit(path)
    assert tasks.get(tasks.load(run.tasks_path), "S1")["status"] == "done"


def test_done_records_the_head_commit(run, plan):
    path = prepare(run, "S1")
    report.record_slice(run, "S1", "DONE")
    commits = tasks.get(tasks.load(run.tasks_path), "S1")["commits"]
    assert commits["head"] == worktree.head_commit(path)
    assert commits["base"] == run.base_commit


def test_done_writes_a_ledger_entry(run, plan):
    prepare(run, "S1")
    report.record_slice(run, "S1", "DONE", tests="3 passed")
    assert ledger.completed_slices(run) == set()  # "done", not the word "complete"
    assert any("slice S1 done" in entry for entry in ledger.entries(run))


def test_done_with_concerns_still_counts_as_finished(run, plan):
    prepare(run, "S1")
    result = report.record_slice(run, "S1", "DONE_WITH_CONCERNS", concerns="file is getting large")
    assert result["slice_status"] == "done"
    stored = tasks.get(tasks.load(run.tasks_path), "S1")
    assert stored["report"]["concerns"] == "file is getting large"


def test_the_report_is_stored_on_the_slice(run, plan):
    prepare(run, "S1")
    report.record_slice(run, "S1", "DONE", tests="3 passed, 0 failed")
    stored = tasks.get(tasks.load(run.tasks_path), "S1")["report"]
    assert stored["status"] == "DONE"
    assert stored["tests"] == "3 passed, 0 failed"


# -- rejecting a DONE that is not true ------------------------------------


def test_done_is_rejected_when_the_worktree_is_dirty(run, plan):
    path = prepare(run, "S1")
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "stray.js").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE")
    assert "not clean" in str(excinfo.value)
    assert "stray.js" in str(excinfo.value)
    assert tasks.get(tasks.load(run.tasks_path), "S1")["status"] == "pending"


def test_done_is_rejected_when_nothing_was_committed(run, plan):
    prepare(run, "S1", commit=False, tests=False)
    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE")
    assert "still at the base commit" in str(excinfo.value)


def test_done_is_rejected_when_a_declared_test_is_missing(run, plan):
    prepare(run, "S1", tests=False)
    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE")
    assert "tests/auth.test.js" in str(excinfo.value)


def test_done_is_rejected_when_the_slice_never_ran(run, plan):
    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE")
    assert "never dispatched" in str(excinfo.value)


def test_rejection_names_every_problem_at_once(run, plan):
    path = prepare(run, "S1", commit=False, tests=False)
    (path / "junk.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE")
    message = str(excinfo.value)
    assert "not clean" in message
    assert "base commit" in message
    assert "tests/auth.test.js" in message


def test_force_overrides_the_checks(run, plan):
    prepare(run, "S1", commit=False, tests=False)
    result = report.record_slice(run, "S1", "DONE", force=True)
    assert result["slice_status"] == "done"


def test_unknown_status_is_rejected(run, plan):
    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "FINISHED")
    assert "must be one of" in str(excinfo.value)


def test_unknown_slice_is_rejected(run, plan):
    with pytest.raises(ReportError):
        report.record_slice(run, "S9", "BLOCKED", reason="x")


# -- the honest failure paths skip the checks ------------------------------


def test_blocked_is_recorded_without_verification(run, plan):
    prepare(run, "S1", commit=False, tests=False)
    result = report.record_slice(run, "S1", "BLOCKED", reason="the API has no pagination")
    assert result["slice_status"] == "blocked"
    stored = tasks.get(tasks.load(run.tasks_path), "S1")
    assert stored["status"] == "blocked"
    assert stored["report"]["reason"] == "the API has no pagination"


def test_needs_context_leaves_the_slice_dispatchable(run, plan):
    prepare(run, "S1", commit=False, tests=False)
    report.record_slice(run, "S1", "NEEDS_CONTEXT", reason="which mail transport?")
    assert tasks.get(tasks.load(run.tasks_path), "S1")["status"] == "pending"
    assert "S1" in tasks.ready(tasks.load(run.tasks_path))


# -- the synthesizer -------------------------------------------------------


def test_synthesizer_clean_is_recorded(run, plan):
    result = report.record_role(run, "synthesizer", "CLEAN")
    assert result["status"] == "CLEAN"
    assert Run.load(run.repo, run.run_id).state["synthesizer"]["status"] == "CLEAN"


def test_synthesizer_escalation_writes_a_failing_verdict(run, plan):
    result = report.record_role(
        run, "synthesizer", "ESCALATE", detail="S1 returns null, S2 expects a throw"
    )
    verdict = run.cycle_dir() / "verdict.md"
    assert verdict.exists()
    assert result["verdict"] == str(verdict)
    body = verdict.read_text(encoding="utf-8")
    assert "S1 returns null, S2 expects a throw" in body
    assert body.rstrip().endswith("VERDICT: FAIL")
    assert report.read_verdict(run) == "FAIL"


def test_escalation_requires_a_detail(run, plan):
    with pytest.raises(ReportError) as excinfo:
        report.record_role(run, "synthesizer", "ESCALATE")
    assert "--detail" in str(excinfo.value)


def test_unknown_role_is_rejected(run, plan):
    with pytest.raises(ReportError):
        report.record_role(run, "executor", "CLEAN")


def test_unknown_synthesizer_status_is_rejected(run, plan):
    with pytest.raises(ReportError):
        report.record_role(run, "synthesizer", "DONE")


# -- grill answers ---------------------------------------------------------


QUESTIONS = """\
round: 1
questions:
  - id: Q1
    topic: scope
    blocking: true
    question: "Should the link expire on a timer?"
    options:
      - label: "15-minute timer"
        detail: "Standard."
    recommended: "15-minute timer"
  - id: Q2
    topic: edges
    blocking: false
    question: "What happens on a reused link?"
    options:
      - label: "404"
        detail: "Simple."
    recommended: "404"
"""


@pytest.fixture
def questions(run):
    path = run.cycle_dir() / "questions-round-1.yaml"
    path.write_text(QUESTIONS, encoding="utf-8")
    return path


def test_answers_are_appended_to_the_spec_verbatim(run, questions):
    result = report.record_answers(run, questions, {"Q1": "15-minute timer", "Q2": "404"})
    body = run.spec_path.read_text(encoding="utf-8")

    assert "## Clarifications (round 1)" in body
    assert "Should the link expire on a timer?" in body
    assert "**A:** 15-minute timer" in body
    assert "**A:** 404" in body
    assert result["answered"] == 2
    assert result["unanswered"] == []


def test_answering_counts_the_round(run, questions):
    assert run.grill_rounds == 0
    result = report.record_answers(run, questions, {"Q1": "a", "Q2": "b"})
    assert result["rounds_used"] == 1
    assert Run.load(run.repo, run.run_id).grill_rounds == 1


def test_an_unanswered_question_is_marked_for_the_planner(run, questions):
    result = report.record_answers(run, questions, {"Q1": "15-minute timer"})
    assert result["unanswered"] == ["Q2"]
    body = run.spec_path.read_text(encoding="utf-8")
    assert "record it as an assumption" in body


def test_notes_are_recorded_next_to_the_answer(run, questions):
    report.record_answers(run, questions, {"Q1": "15-minute timer"}, notes={"Q1": "match the session cookie"})
    assert "**Note:** match the session cookie" in run.spec_path.read_text(encoding="utf-8")


def test_free_text_is_appended(run, questions):
    report.record_answers(run, questions, {"Q1": "a"}, free_text="Ship behind a flag.")
    assert "Ship behind a flag." in run.spec_path.read_text(encoding="utf-8")


def test_two_rounds_accumulate_in_the_spec(run, questions):
    report.record_answers(run, questions, {"Q1": "a", "Q2": "b"})
    second = run.cycle_dir() / "questions-round-2.yaml"
    second.write_text(QUESTIONS.replace("round: 1", "round: 2"), encoding="utf-8")
    report.record_answers(run, second, {"Q1": "c", "Q2": "d"})

    body = run.spec_path.read_text(encoding="utf-8")
    assert "## Clarifications (round 1)" in body
    assert "## Clarifications (round 2)" in body
    assert Run.load(run.repo, run.run_id).grill_rounds == 2


def test_missing_questions_file_is_an_error(run):
    with pytest.raises(ReportError) as excinfo:
        report.record_answers(run, run.cycle_dir() / "nope.yaml", {})
    assert "no questions file" in str(excinfo.value)


def test_malformed_questions_file_is_an_error(run):
    path = run.cycle_dir() / "questions-round-1.yaml"
    path.write_text("just: a mapping\n", encoding="utf-8")
    with pytest.raises(ReportError) as excinfo:
        report.record_answers(run, path, {})
    assert "'questions' list" in str(excinfo.value)


@pytest.mark.parametrize(
    "pairs,expected",
    [
        (["Q1=Both"], {"Q1": "Both"}),
        (["Q1=15-minute timer", "Q2=404"], {"Q1": "15-minute timer", "Q2": "404"}),
        (["Q1=an answer with = inside"], {"Q1": "an answer with = inside"}),
        ([], {}),
    ],
)
def test_parse_pairs(pairs, expected):
    assert report.parse_pairs(pairs) == expected


@pytest.mark.parametrize("bad", ["Q1", "=answer"])
def test_parse_pairs_rejects_malformed_input(bad):
    with pytest.raises(ReportError):
        report.parse_pairs([bad])


# -- approval and verdict --------------------------------------------------


def test_approval_is_recorded(run):
    report.record_approval(run, "approved")
    assert Run.load(run.repo, run.run_id).approval == "approved"


def test_revise_requires_feedback(run):
    with pytest.raises(ReportError):
        report.record_approval(run, "revise")


def test_revise_stores_the_feedback(run):
    report.record_approval(run, "revise", feedback="Split the CLI slice.")
    assert Run.load(run.repo, run.run_id).state["approval_feedback"] == "Split the CLI slice."


@pytest.mark.parametrize("line,expected", [("VERDICT: PASS", "PASS"), ("VERDICT: FAIL", "FAIL")])
def test_verdict_is_read_from_the_final_line(run, line, expected):
    (run.cycle_dir() / "verdict.md").write_text("# Verdict\n\nbody\n\n{}\n".format(line), encoding="utf-8")
    assert report.read_verdict(run) == expected


def test_the_last_verdict_line_wins(run):
    (run.cycle_dir() / "verdict.md").write_text(
        "VERDICT: FAIL\n\nthen it was fixed\n\nVERDICT: PASS\n", encoding="utf-8"
    )
    assert report.read_verdict(run) == "PASS"


def test_no_verdict_file_reads_as_none(run):
    assert report.read_verdict(run) is None


def test_require_verdict_explains_a_missing_file(run):
    with pytest.raises(ReportError) as excinfo:
        report.require_verdict(run)
    assert "no verdict at" in str(excinfo.value)


def test_require_verdict_explains_a_missing_line(run):
    (run.cycle_dir() / "verdict.md").write_text("# Verdict\n\nI forgot the line.\n", encoding="utf-8")
    with pytest.raises(ReportError) as excinfo:
        report.require_verdict(run)
    assert "final line" in str(excinfo.value)


def test_verdicts_are_per_cycle(run):
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: FAIL\n", encoding="utf-8")
    run.advance_cycle()
    assert report.read_verdict(run) is None
    assert report.read_verdict(run, cycle=1) == "FAIL"
