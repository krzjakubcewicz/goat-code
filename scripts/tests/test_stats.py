"""Where a run's time actually went.

The recorded runs could not answer this: debug was off in all eleven, so no
log.txt was ever written, and the ledger's timestamps were the only evidence.
`goatcode stats` reads the ledger - which is always written - and turns it into
the per-phase picture every other improvement has to be measured against.
"""

from __future__ import annotations

import datetime

import pytest

from goatcode import ledger, osenv, stats
from goatcode.run import Run
from tests.test_cli import invoke_json  # noqa: F401


@pytest.fixture
def run(git_repo):
    return Run.create(git_repo, "magic link", "chat")


def _verdict(run, result):
    osenv.write_text(run.cycle_dir() / "verdict.md", "VERDICT: " + result + "\n")


def at(run, minute, line):
    """Append a ledger entry at a fixed minute past a fixed hour."""
    stamp = datetime.datetime(2026, 9, 2, 10, minute, 0)
    ledger.append(run, line, now=stamp)


# -- phase durations -------------------------------------------------------


def test_phase_durations_come_from_the_transitions_in_the_ledger(run):
    at(run, 0, "phase init -> grill")
    at(run, 10, "phase grill -> execute")
    at(run, 40, "phase execute -> verify")

    report = stats.collect(run)
    phases = {entry["phase"]: entry["seconds"] for entry in report["phases"]}
    assert phases["grill"] == 600
    assert phases["execute"] == 1800


def test_the_phase_still_running_has_no_duration_yet(run):
    at(run, 0, "phase init -> grill")
    at(run, 10, "phase grill -> execute")

    report = stats.collect(run)
    last = report["phases"][-1]
    assert last["phase"] == "execute"
    assert last["seconds"] is None


def test_the_same_phase_entered_twice_sums_its_time(run):
    at(run, 0, "phase init -> execute")
    at(run, 5, "phase execute -> verify")
    at(run, 10, "phase verify -> execute")
    at(run, 25, "phase execute -> done")

    report = stats.collect(run)
    assert report["totals"]["execute"] == 1200


def test_a_ledger_with_no_transitions_reports_no_phases(run):
    at(run, 0, "slice S1 done - 3 passed")
    report = stats.collect(run)
    assert report["phases"] == []
    assert report["elapsed_seconds"] == 0


# -- what the run cost -----------------------------------------------------


def test_remedial_slices_are_counted_apart_from_the_first_cycle(run):
    at(run, 0, "slice S1 done - 3 passed")
    at(run, 5, "slice S2 done - 4 passed")
    run.advance_cycle()
    at(run, 20, "slice R1 done - 1 passed")

    report = stats.collect(run)
    assert report["cycles"] == 2
    assert report["slices"]["first_cycle"] == 2
    assert report["slices"]["remedial"] == 1


def test_verdicts_are_read_from_each_cycles_verdict_file(run):
    """The verdict never reaches the ledger; verdict.md is the record."""
    _verdict(run, "FAIL")
    run.advance_cycle()
    _verdict(run, "PASS")

    assert stats.collect(run)["verdicts"] == ["FAIL", "PASS"]


def test_a_cycle_with_no_verdict_yet_reads_as_none(run):
    assert stats.collect(run)["verdicts"] == [None]


def test_gate_outcomes_are_read_from_each_cycles_gates_file(run):
    osenv.write_json(run.cycle_dir() / "gates.json", {"regressions": ["test"], "pre_existing": ["lint"]})
    report = stats.collect(run)
    assert report["gates"] == [{"cycle": 1, "regressions": ["test"], "pre_existing": ["lint"]}]


# -- the CLI ---------------------------------------------------------------


def test_stats_json_carries_the_phase_table(capsys, git_repo, run):
    at(run, 0, "phase init -> execute")
    at(run, 15, "phase execute -> done")

    code, payload, _err = invoke_json(capsys, "--repo", str(git_repo), "stats")
    assert code == 0
    assert payload["totals"]["execute"] == 900


def test_stats_renders_a_readable_table(capsys, git_repo, run):
    at(run, 0, "phase init -> execute")
    at(run, 15, "phase execute -> done")

    code, out, _err = _invoke_text(capsys, "--repo", str(git_repo), "stats")
    assert code == 0
    assert "execute" in out
    assert "15m" in out


def _invoke_text(capsys, *args):
    from tests.test_cli import invoke

    return invoke(capsys, *args)
