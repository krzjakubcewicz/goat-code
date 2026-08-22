"""Gate execution and, critically, telling new breakage from inherited breakage."""

from __future__ import annotations

import sys

import pytest

from codag import gates, osenv
from codag.run import Run


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
