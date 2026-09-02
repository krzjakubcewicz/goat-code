"""Gate execution and, critically, telling new breakage from inherited breakage."""

from __future__ import annotations

import sys

import pytest

from goatcode import gates, osenv
from goatcode.run import Run


def python_cmd(body):
    return [sys.executable, "-c", body]


PASS = python_cmd("print('ok')")
FAIL = python_cmd("import sys; print('boom'); sys.exit(1)")


@pytest.fixture
def run(git_repo):
    return Run.create(git_repo, "gates demo", "chat")


def profile(**commands):
    base = {"build": None, "typecheck": None, "lint": None, "test": None}
    base.update(commands)
    return {"commands": base}


# -- running ---------------------------------------------------------------


def test_runs_every_detected_gate(run, git_repo):
    report = gates.run_all(run, git_repo, profile=profile(build=PASS, test=PASS))
    assert report["gates"]["build"]["status"] == "pass"
    assert report["gates"]["test"]["status"] == "pass"
    assert report["summary"]["pass"] == 2
    assert report["ok"] is True


def test_missing_commands_are_marked_not_failed(run, git_repo):
    report = gates.run_all(run, git_repo, profile=profile(test=PASS))
    assert report["gates"]["lint"]["status"] == "missing"
    assert report["ok"] is True
    assert "no lint command detected" in report["gates"]["lint"]["note"]


def test_failure_is_recorded_with_output(run, git_repo):
    report = gates.run_all(run, git_repo, profile=profile(test=FAIL))
    gate = report["gates"]["test"]
    assert gate["status"] == "fail"
    assert gate["returncode"] == 1
    assert "boom" in gate["output_tail"]
    assert report["ok"] is False


def test_only_runs_the_requested_gates(run, git_repo):
    report = gates.run_all(run, git_repo, profile=profile(build=PASS, test=PASS), only=["test"])
    assert "build" not in report["gates"]
    assert "test" in report["gates"]


def test_output_is_truncated_to_a_bounded_tail(run, git_repo):
    noisy = python_cmd("print('\\n'.join(str(i) for i in range(1000)))")
    report = gates.run_all(run, git_repo, profile=profile(test=noisy))
    tail = report["gates"]["test"]["output_tail"]
    assert tail.startswith("... 800 earlier lines omitted ...")
    assert len(tail.splitlines()) == gates.TAIL_LINES + 1
    assert tail.rstrip().endswith("999")


def test_available_lists_only_real_commands():
    assert gates.available(profile(build=PASS, test=PASS)) == ["build", "test"]
    assert gates.available({}) == []


def test_report_records_the_ref(run, git_repo):
    report = gates.run_all(run, git_repo, profile=profile(test=PASS))
    assert report["ref"] == osenv.git_out(["rev-parse", "HEAD"], cwd=git_repo)


# -- baseline classification: the whole point ------------------------------


def test_a_pre_existing_failure_does_not_block(run, git_repo):
    gates.capture_baseline(run, git_repo, profile=profile(lint=FAIL, test=PASS))
    report = gates.run_and_classify(run, git_repo, profile=profile(lint=FAIL, test=PASS))

    assert report["pre_existing"] == ["lint"]
    assert report["regressions"] == []
    assert gates.passed(report) is True
    assert report["gates"]["lint"]["pre_existing"] is True


def test_a_new_failure_blocks(run, git_repo):
    gates.capture_baseline(run, git_repo, profile=profile(test=PASS))
    report = gates.run_and_classify(run, git_repo, profile=profile(test=FAIL))

    assert report["regressions"] == ["test"]
    assert gates.blocking(report) == ["test"]
    assert gates.passed(report) is False


def test_fixing_an_inherited_failure_is_credited(run, git_repo):
    gates.capture_baseline(run, git_repo, profile=profile(lint=FAIL))
    report = gates.run_and_classify(run, git_repo, profile=profile(lint=PASS))
    assert report["fixed"] == ["lint"]
    assert gates.passed(report) is True


def test_without_a_baseline_every_failure_blocks(run, git_repo):
    report = gates.run_and_classify(run, git_repo, profile=profile(test=FAIL))
    assert report["baseline"] is None
    assert report["regressions"] == ["test"]


def test_baseline_is_persisted_and_reloadable(run, git_repo):
    gates.capture_baseline(run, git_repo, profile=profile(test=PASS))
    assert run.baseline_path.exists()
    loaded = gates.load_baseline(run)
    assert loaded["is_baseline"] is True
    assert loaded["gates"]["test"]["status"] == "pass"


def test_load_baseline_is_none_when_absent(run):
    assert gates.load_baseline(run) is None


def test_run_and_classify_writes_into_the_cycle_directory(run, git_repo):
    report = gates.run_and_classify(run, git_repo, profile=profile(test=PASS))
    assert report["path"] == str(run.cycle_dir() / "gates.json")
    assert (run.cycle_dir() / "gates.json").exists()


def test_second_cycle_writes_its_own_gates_file(run, git_repo):
    gates.run_and_classify(run, git_repo, profile=profile(test=PASS))
    run.advance_cycle()
    gates.run_and_classify(run, git_repo, profile=profile(test=PASS))
    assert (run.root / "cycle-1" / "gates.json").exists()
    assert (run.root / "cycle-2" / "gates.json").exists()


# -- rendering -------------------------------------------------------------


def test_render_marks_pre_existing_failures_clearly(run, git_repo):
    gates.capture_baseline(run, git_repo, profile=profile(lint=FAIL, test=PASS))
    report = gates.run_and_classify(run, git_repo, profile=profile(lint=FAIL, test=FAIL))
    text = gates.render(report)

    assert "FAIL  lint" in text
    assert "pre-existing, not caused by this run" in text
    assert "regressions caused by this run: test" in text
    assert "failing before this run too: lint" in text


def test_render_says_so_when_all_is_well(run, git_repo):
    report = gates.run_and_classify(run, git_repo, profile=profile(test=PASS))
    assert "no regressions" in gates.render(report)


def test_timeout_is_reported_as_a_failure(run, git_repo):
    run.state["config"]["gate_timeout_seconds"] = 1
    report = gates.run_all(run, git_repo, profile=profile(test=python_cmd("import time; time.sleep(30)")))
    assert report["gates"]["test"]["status"] == "fail"
    assert report["gates"]["test"]["returncode"] == 124


def test_gates_run_inside_the_detected_project_dir(git_repo, run):
    (git_repo / "backend").mkdir()
    marker = python_cmd(
        "import pathlib; print(pathlib.Path.cwd().name)"
    )
    spec = profile(test=marker)
    spec["project_dir"] = "backend"
    report = gates.run_all(run, git_repo, profile=spec)
    assert report["gates"]["test"]["status"] == "pass"
    assert "backend" in report["gates"]["test"]["output_tail"]


def test_a_missing_project_dir_falls_back_to_the_worktree_root(git_repo, run):
    spec = profile(test=PASS)
    spec["project_dir"] = "nope"
    report = gates.run_all(run, git_repo, profile=spec)
    assert report["gates"]["test"]["status"] == "pass"


def test_a_tool_missing_from_this_host_is_reported_not_raised(git_repo, run):
    """The toolchain often lives only in the project's image."""
    report = gates.run_all(run, git_repo, profile=profile(test=["definitely-not-installed-xyz"]))
    assert report["gates"]["test"]["status"] == "missing"
    assert "definitely-not-installed-xyz" in report["gates"]["test"]["note"]


def test_commands_cwd_overrides_project_dir(git_repo, run):
    (git_repo / "backend").mkdir()
    spec = profile(test=python_cmd("import pathlib; print(pathlib.Path.cwd().name)"))
    spec["project_dir"] = "backend"
    spec["commands_cwd"] = ""
    report = gates.run_all(run, git_repo, profile=spec)
    assert "backend" not in report["gates"]["test"]["output_tail"]


# -- weak assertions -------------------------------------------------------
#
# Across eleven recorded runs the gates found a real regression in 1 of 21
# cycles while the verifier failed 13 of 21, every time on assertion strength.
# The cheap deterministic layer was pointed at the wrong failure. This is not
# blocking - it is a lead for the executor, replanner and verifier, who all
# read gates.json.


def test_a_count_assertion_that_cannot_discriminate_is_flagged(tmp_path):
    target = tmp_path / "test_thing.py"
    target.write_text("def test_x():\n    assert AuditLog.objects.count() >= 1\n", encoding="utf-8")
    found = gates.weak_assertions(tmp_path, ["test_thing.py"])
    assert found[0]["path"] == "test_thing.py"
    assert found[0]["line"] == 2
    assert "count" in found[0]["reason"]


def test_a_length_assertion_with_an_inequality_is_flagged(tmp_path):
    target = tmp_path / "test_thing.py"
    target.write_text("def test_x():\n    assert len(prs_updated) >= 2\n", encoding="utf-8")
    assert gates.weak_assertions(tmp_path, ["test_thing.py"])


def test_an_exact_count_is_not_flagged(tmp_path):
    target = tmp_path / "test_thing.py"
    target.write_text("def test_x():\n    assert AuditLog.objects.count() == 1\n", encoding="utf-8")
    assert gates.weak_assertions(tmp_path, ["test_thing.py"]) == []


def test_a_test_function_with_no_assertion_at_all_is_flagged(tmp_path):
    target = tmp_path / "test_thing.py"
    target.write_text("def test_x():\n    do_the_thing()\n", encoding="utf-8")
    found = gates.weak_assertions(tmp_path, ["test_thing.py"])
    assert any("no assertion" in entry["reason"] for entry in found)


def test_a_self_comparison_is_flagged(tmp_path):
    target = tmp_path / "test_thing.py"
    target.write_text("def test_x():\n    assert ids() == ids()\n", encoding="utf-8")
    found = gates.weak_assertions(tmp_path, ["test_thing.py"])
    assert any("cannot fail" in entry["reason"] for entry in found)


def test_only_test_files_are_scanned(tmp_path):
    target = tmp_path / "service.py"
    target.write_text("def check():\n    assert rows.count() >= 1\n", encoding="utf-8")
    assert gates.weak_assertions(tmp_path, ["service.py"]) == []


def test_a_missing_file_is_skipped_rather_than_raising(tmp_path):
    assert gates.weak_assertions(tmp_path, ["tests/gone.py"]) == []


def test_the_report_carries_weak_assertions_for_the_changed_tests(run, git_repo):
    target = git_repo / "tests" / "test_thing.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def test_x():\n    assert rows.count() >= 1\n", encoding="utf-8")

    report = gates.run_and_classify(
        run, git_repo, profile=profile(test=PASS), changed=["tests/test_thing.py"]
    )
    assert report["weak_assertions"][0]["path"] == "tests/test_thing.py"


def test_weak_assertions_never_block_the_run(run, git_repo):
    target = git_repo / "tests" / "test_thing.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def test_x():\n    assert rows.count() >= 1\n", encoding="utf-8")

    report = gates.run_and_classify(
        run, git_repo, profile=profile(test=PASS), changed=["tests/test_thing.py"]
    )
    assert gates.blocking(report) == []
    assert gates.passed(report) is True


def test_the_report_has_an_empty_list_when_nothing_changed(run, git_repo):
    report = gates.run_and_classify(run, git_repo, profile=profile(test=PASS))
    assert report["weak_assertions"] == []


# -- gating both halves of a monorepo --------------------------------------


def test_a_siblings_gates_run_too_and_are_named_by_its_directory(run, git_repo):
    (git_repo / "frontend").mkdir()
    stack_profile = profile(test=PASS)
    stack_profile["sibling_projects"] = [{"dir": "frontend", "commands": {"test": PASS, "lint": PASS}}]

    report = gates.run_all(run, git_repo, profile=stack_profile)
    assert report["gates"]["test [frontend]"]["status"] == "pass"
    assert report["gates"]["lint [frontend]"]["status"] == "pass"


def test_a_failing_sibling_gate_fails_the_report(run, git_repo):
    (git_repo / "frontend").mkdir()
    stack_profile = profile(test=PASS)
    stack_profile["sibling_projects"] = [{"dir": "frontend", "commands": {"test": FAIL}}]

    report = gates.run_all(run, git_repo, profile=stack_profile)
    assert report["ok"] is False
    assert report["gates"]["test [frontend]"]["status"] == "fail"


def test_a_sibling_failing_at_the_baseline_too_is_not_a_regression(run, git_repo):
    (git_repo / "frontend").mkdir()
    stack_profile = profile(test=PASS)
    stack_profile["sibling_projects"] = [{"dir": "frontend", "commands": {"test": FAIL}}]

    gates.capture_baseline(run, git_repo, profile=stack_profile)
    report = gates.run_and_classify(run, git_repo, profile=stack_profile)
    assert report["regressions"] == []
    assert "test [frontend]" in report["pre_existing"]


def test_a_sibling_directory_that_is_gone_is_skipped(run, git_repo):
    stack_profile = profile(test=PASS)
    stack_profile["sibling_projects"] = [{"dir": "frontend", "commands": {"test": PASS}}]

    report = gates.run_all(run, git_repo, profile=stack_profile)
    assert "test [frontend]" not in report["gates"]


def test_the_rendered_summary_names_the_sibling_gates(run, git_repo):
    (git_repo / "frontend").mkdir()
    stack_profile = profile(test=PASS)
    stack_profile["sibling_projects"] = [{"dir": "frontend", "commands": {"test": PASS}}]

    report = gates.run_all(run, git_repo, profile=stack_profile)
    assert "test [frontend]" in gates.render(report)
