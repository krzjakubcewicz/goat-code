"""The CLI surface agents use to report: report, answer, approve, verdict.

Exercised through ``main`` rather than the modules, because the point is
that an agent can invoke these as written in its dispatch prompt.
"""

from __future__ import annotations

import pathlib

import pytest

from codag import osenv, worktree
from codag.run import Run
from tests.test_cli import cli, invoke, invoke_json, node_repo, plan_for, slice_spec  # noqa: F401


def start(capsys, repo, slices=None):
    invoke(capsys, "--repo", str(repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(repo)
    if slices is not None:
        plan_for(run, slices)
    return run


def working_slice(capsys, repo, slice_id="S1"):
    """A dispatched slice with real committed work, as an executor leaves it."""
    run = Run.load(repo)
    invoke(capsys, "--repo", str(repo), "worktree", "create", slice_id, "--no-setup")
    path = worktree.path_for(run, slice_id)
    target = path / "tests" / "{}.test.js".format(slice_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("// test\n", encoding="utf-8")
    osenv.git(["add", "-A"], cwd=path, check=True)
    osenv.git(["commit", "-qm", "work"], cwd=path, check=True)
    return path


def slice_with_tests(slice_id, owns):
    spec = slice_spec(slice_id, owns)
    spec["tests"] = ["tests/{}.test.js".format(slice_id)]
    return spec


# -- report ----------------------------------------------------------------


def test_report_accepts_a_real_done_from_inside_the_worktree(capsys, node_repo):
    start(capsys, node_repo, [slice_with_tests("S1", "src/a/**")])
    path = working_slice(capsys, node_repo)

    code, payload, _err = invoke_json(
        capsys, "--repo", str(path), "report", "--slice", "S1", "--status", "DONE", "--tests", "3 passed"
    )
    assert code == 0
    assert payload["slice_status"] == "done"

    _code, item, _e = invoke_json(capsys, "--repo", str(node_repo), "task", "show", "S1")
    assert item["status"] == "done"
    assert item["commits"]["head"]


def test_report_rejects_a_done_with_uncommitted_work(capsys, node_repo):
    start(capsys, node_repo, [slice_with_tests("S1", "src/a/**")])
    path = working_slice(capsys, node_repo)
    (path / "stray.js").write_text("uncommitted\n", encoding="utf-8")

    code, _out, err = invoke(capsys, "--repo", str(path), "report", "--slice", "S1", "--status", "DONE")
    assert code == cli.EXIT_USAGE
    assert "not clean" in err
    assert "stray.js" in err

    _code, item, _e = invoke_json(capsys, "--repo", str(node_repo), "task", "show", "S1")
    assert item["status"] == "pending", "a rejected report must not change the slice"


def test_report_rejects_a_done_with_no_commits(capsys, node_repo):
    run = start(capsys, node_repo, [slice_with_tests("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "--no-setup")
    path = worktree.path_for(run, "S1")

    code, _out, err = invoke(capsys, "--repo", str(path), "report", "--slice", "S1", "--status", "DONE")
    assert code == cli.EXIT_USAGE
    assert "base commit" in err


def test_report_blocked_needs_no_verification(capsys, node_repo):
    start(capsys, node_repo, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "--no-setup")

    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "report",
        "--slice", "S1", "--status", "BLOCKED", "--reason", "the API has no pagination",
    )
    assert code == 0
    assert payload["slice_status"] == "blocked"


def test_report_requires_a_slice_or_a_role(capsys, node_repo):
    start(capsys, node_repo)
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "report", "--status", "DONE")
    assert code == cli.EXIT_USAGE
    assert "--slice" in err


def test_report_synthesizer_clean(capsys, node_repo):
    start(capsys, node_repo)
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "report", "--role", "synthesizer", "--status", "CLEAN"
    )
    assert code == 0
    assert payload["status"] == "CLEAN"


def test_report_synthesizer_escalation_writes_a_failing_verdict(capsys, node_repo):
    start(capsys, node_repo)
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "report",
        "--role", "synthesizer", "--status", "ESCALATE", "--detail", "S1 and S2 disagree",
    )
    assert code == 0
    body = pathlib.Path(payload["verdict"]).read_text(encoding="utf-8")
    assert "S1 and S2 disagree" in body
    assert body.rstrip().endswith("VERDICT: FAIL")


# -- answer ----------------------------------------------------------------

QUESTION_FILE = "\n".join(
    [
        "round: 1",
        "questions:",
        "  - id: Q1",
        "    topic: scope",
        "    blocking: true",
        '    question: "Timer or next login?"',
        "    options:",
        '      - label: "15-minute timer"',
        '        detail: "Standard."',
        '    recommended: "15-minute timer"',
        "",
    ]
)


def with_questions(run):
    path = run.cycle_dir() / "questions-round-1.yaml"
    path.write_text(QUESTION_FILE, encoding="utf-8")
    return path


def test_answer_records_the_round_into_the_spec(capsys, node_repo):
    run = start(capsys, node_repo)
    with_questions(run)

    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "answer", "Q1=15-minute timer")
    assert code == 0
    assert payload["rounds_used"] == 1

    body = run.spec_path.read_text(encoding="utf-8")
    assert "## Clarifications (round 1)" in body
    assert "Timer or next login?" in body
    assert "**A:** 15-minute timer" in body


def test_answer_accepts_a_note(capsys, node_repo):
    run = start(capsys, node_repo)
    with_questions(run)
    invoke(
        capsys, "--repo", str(node_repo), "answer",
        "Q1=15-minute timer", "--note", "Q1=match the session cookie",
    )
    assert "match the session cookie" in run.spec_path.read_text(encoding="utf-8")


def test_answer_reports_a_malformed_pair(capsys, node_repo):
    run = start(capsys, node_repo)
    with_questions(run)
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "answer", "Q1")
    assert code == cli.EXIT_USAGE
    assert "QID=answer" in err


def test_answer_without_a_questions_file_is_a_clear_error(capsys, node_repo):
    start(capsys, node_repo)
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "answer", "Q1=x")
    assert code == cli.EXIT_USAGE
    assert "no questions file" in err


def test_answer_leaves_a_skipped_question_to_the_planner(capsys, node_repo):
    run = start(capsys, node_repo)
    with_questions(run)
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "answer")
    assert payload["unanswered"] == ["Q1"]
    assert "record it as an assumption" in run.spec_path.read_text(encoding="utf-8")


# -- approve ---------------------------------------------------------------


def test_approve_records_the_decision(capsys, node_repo):
    start(capsys, node_repo)
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "approve", "--yes")
    assert code == 0
    assert payload["approval"] == "approved"
    assert Run.load(node_repo).needs_approval() is False


def test_approve_revise_carries_the_feedback(capsys, node_repo):
    start(capsys, node_repo)
    _code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "approve", "--revise", "Split the CLI slice."
    )
    assert payload["feedback"] == "Split the CLI slice."
    assert Run.load(node_repo).approval == "revise"


def test_approve_abort_cleans_up_and_stops_the_run(capsys, node_repo):
    run = start(capsys, node_repo, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "--no-setup")

    code, _payload, _err = invoke_json(capsys, "--repo", str(node_repo), "approve", "--abort")
    assert code == 0
    assert Run.load(node_repo).phase == "aborted"
    assert not worktree.path_for(run, "S1").exists()


def test_approve_requires_a_decision(capsys, node_repo):
    start(capsys, node_repo)
    with pytest.raises(SystemExit):
        invoke(capsys, "--repo", str(node_repo), "approve")


# -- verdict ---------------------------------------------------------------


def test_verdict_pass_exits_zero(capsys, node_repo):
    run = start(capsys, node_repo)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "verdict")
    assert code == 0
    assert payload["verdict"] == "PASS"


def test_verdict_fail_exits_one(capsys, node_repo):
    run = start(capsys, node_repo)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: FAIL\n", encoding="utf-8")
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "verdict")
    assert code == cli.EXIT_FAIL
    assert payload["verdict"] == "FAIL"


def test_verdict_without_the_line_tells_the_verifier_what_to_add(capsys, node_repo):
    run = start(capsys, node_repo)
    (run.cycle_dir() / "verdict.md").write_text("# Verdict\n\nI forgot.\n", encoding="utf-8")
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "verdict")
    assert code == cli.EXIT_USAGE
    assert "final line" in err


def test_verdict_without_a_file_says_so(capsys, node_repo):
    start(capsys, node_repo)
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "verdict")
    assert code == cli.EXIT_USAGE
    assert "no verdict at" in err
