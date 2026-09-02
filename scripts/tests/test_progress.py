"""The cross-run progress log.

Distinct from ledger.md, which is per-run machine bookkeeping. This one is
written for a reader, carries the learnings, and survives the run that wrote
it. The property that matters most: it appends, never replaces.
"""

from __future__ import annotations

import datetime

import pytest

from codag import progress
from codag.run import Run
from tests.conftest import make_run
from tests.test_cli import cli, invoke, invoke_json  # noqa: F401

BODY = "\n".join(
    [
        "- What was implemented",
        "  - magic-link sign in",
        "- Files changed",
        "  - src/auth/**",
        "- **Learnings for future iterations:**",
        "  - routes register in src/routes/index.ts, not by file convention",
    ]
)


@pytest.fixture
def run(git_repo):
    return Run.create(git_repo, "magic link", "chat")


# -- appending -------------------------------------------------------------


def test_the_first_entry_creates_the_file_with_a_header(git_repo, run):
    progress.append(git_repo, run, BODY)
    text = progress.read(git_repo)
    assert text.startswith("# cod-ag progress log")
    assert "magic-link sign in" in text
    assert progress.path_for(git_repo).name == "progress.txt"


def test_the_entry_carries_the_run_id_and_directory(git_repo, run):
    when = datetime.datetime(2026, 8, 23, 14, 5, 0)
    entry = progress.append(git_repo, run, BODY, now=when)
    assert entry.startswith("## 2026-08-23 14:05 - {}".format(run.run_id))
    assert str(run.root) in entry


def test_a_second_entry_is_appended_not_written_over(git_repo, run):
    progress.append(git_repo, run, BODY)
    second = Run.create(git_repo, "second feature", "chat")
    progress.append(git_repo, second, "- What was implemented\n  - the second thing")

    text = progress.read(git_repo)
    assert "magic-link sign in" in text, "the first entry must survive"
    assert "the second thing" in text
    assert text.count("# cod-ag progress log") == 1, "only one file header"


def test_many_entries_all_survive(git_repo):
    for index in range(5):
        run = Run.create(git_repo, "feature {}".format(index), "chat")
        progress.append(git_repo, run, "- entry {}".format(index))
    text = progress.read(git_repo)
    for index in range(5):
        assert "- entry {}".format(index) in text


def test_an_empty_body_is_refused(git_repo, run):
    with pytest.raises(ValueError):
        progress.append(git_repo, run, "   \n  ")


def test_entries_are_separated(git_repo, run):
    progress.append(git_repo, run, BODY)
    progress.append(git_repo, Run.create(git_repo, "b", "chat"), "- second")
    assert progress.read(git_repo).count("\n---\n") == 2


# -- reading ---------------------------------------------------------------


def test_no_file_reads_as_empty(git_repo):
    assert progress.read(git_repo) == ""
    assert progress.entries(git_repo) == []
    assert progress.summary(git_repo) == "no entries yet"


def test_entries_are_parsed_back_out(git_repo, run):
    progress.append(git_repo, run, BODY)
    progress.append(git_repo, Run.create(git_repo, "b", "chat"), "- second thing")
    found = progress.entries(git_repo)
    assert len(found) == 2
    assert found[0].startswith("## ")
    assert "second thing" in found[1]


def test_recent_returns_the_newest(git_repo):
    for index in range(4):
        progress.append(git_repo, Run.create(git_repo, "f{}".format(index), "chat"), "- e{}".format(index))
    found = progress.recent(git_repo, limit=2)
    assert len(found) == 2
    assert "- e3" in found[-1]


def test_recent_with_no_limit_returns_all(git_repo):
    for index in range(3):
        progress.append(git_repo, Run.create(git_repo, "f{}".format(index), "chat"), "- e{}".format(index))
    assert len(progress.recent(git_repo, limit=0)) == 3


def test_summary_names_the_latest(git_repo, run):
    progress.append(git_repo, run, BODY)
    assert run.run_id in progress.summary(git_repo)


def test_the_template_names_the_three_sections():
    text = progress.template()
    assert "What was implemented" in text
    assert "Files changed" in text
    assert "Learnings for future iterations" in text


# -- through the CLI -------------------------------------------------------


def start(capsys, repo):
    """A run ready to write progress into; not a test of `init`."""
    return make_run(repo, "magic link")


def test_append_from_a_body_file(capsys, node_repo):
    run = start(capsys, node_repo)
    body = run.cycle_dir() / "progress-entry.md"
    body.write_text(BODY, encoding="utf-8")

    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "progress", "append", "--body", str(body)
    )
    assert code == 0
    assert "magic-link sign in" in payload["entry"]
    assert "routes register in" in progress.read(node_repo)


def test_append_records_that_the_run_was_written_up(capsys, node_repo):
    run = start(capsys, node_repo)
    body = run.cycle_dir() / "progress-entry.md"
    body.write_text(BODY, encoding="utf-8")
    invoke(capsys, "--repo", str(node_repo), "progress", "append", "--body", str(body))
    assert Run.load(node_repo).state["scribe"]["status"] == "WRITTEN"


def test_append_inline(capsys, node_repo):
    start(capsys, node_repo)
    code, _payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "progress", "append", "--text", "- did a thing"
    )
    assert code == 0
    assert "- did a thing" in progress.read(node_repo)


def test_append_reports_a_missing_body_file(capsys, node_repo):
    start(capsys, node_repo)
    code, _out, err = invoke(
        capsys, "--repo", str(node_repo), "progress", "append", "--body", "nope.md"
    )
    assert code == cli.EXIT_USAGE
    assert "no entry body" in err


def test_show_is_empty_before_anything_is_written(capsys, node_repo):
    start(capsys, node_repo)
    _code, out, _err = invoke(capsys, "--repo", str(node_repo), "progress", "show")
    assert "no entries yet" in out


def test_show_returns_the_entries(capsys, node_repo):
    start(capsys, node_repo)
    invoke(capsys, "--repo", str(node_repo), "progress", "append", "--text", "- first")
    invoke(capsys, "--repo", str(node_repo), "progress", "append", "--text", "- second")

    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "progress", "show")
    assert payload["count"] == 2
    assert "- second" in payload["entries"][-1]


def test_show_respects_the_limit(capsys, node_repo):
    start(capsys, node_repo)
    for index in range(4):
        invoke(capsys, "--repo", str(node_repo), "progress", "append", "--text", "- e{}".format(index))
    _code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "progress", "show", "--limit", "2"
    )
    assert payload["count"] == 2


def test_the_log_is_hidden_from_git(capsys, node_repo):
    """It lives under .codag/, which cod-ag already excludes."""
    start(capsys, node_repo)
    invoke(capsys, "--repo", str(node_repo), "progress", "append", "--text", "- a thing")

    from codag import osenv

    assert progress.path_for(node_repo).exists()
    assert osenv.git(["status", "--porcelain"], cwd=node_repo).out == ""


# -- what the planner actually reads ---------------------------------------
#
# The log reached 43 KB over eight runs, all of it read at the start of every
# run, and the same assertion-gap learning was written in every entry while
# the failure recurred every time. Narrative for a human; standing rules for
# the planner.


def bodies(repo, run, count):
    for index in range(count):
        progress.append(repo, run, "- Run {}\n- **Learnings:**\n  - thing {}".format(index, index))


def test_the_planner_sees_only_the_most_recent_entries(git_repo, run):
    bodies(git_repo, run, 8)
    view = progress.planner_view(git_repo, limit=3)
    assert "Run 7" in view
    assert "Run 5" in view
    assert "Run 4" not in view


def test_standing_constraints_are_always_shown_however_old(git_repo, run):
    progress.add_constraint(git_repo, "Audit rows come from service functions, never signals.")
    bodies(git_repo, run, 8)

    view = progress.planner_view(git_repo, limit=2)
    assert "Audit rows come from service functions" in view
    assert "Run 7" in view
    assert "Run 4" not in view


def test_a_constraint_is_appended_not_replaced(git_repo, run):
    progress.add_constraint(git_repo, "First rule.")
    progress.add_constraint(git_repo, "Second rule.")
    listed = progress.constraints(git_repo)
    assert listed == ["First rule.", "Second rule."]


def test_the_same_constraint_is_not_added_twice(git_repo, run):
    progress.add_constraint(git_repo, "Only once.")
    progress.add_constraint(git_repo, "Only once.")
    assert progress.constraints(git_repo) == ["Only once."]


def test_constraints_survive_a_later_entry_being_appended(git_repo, run):
    progress.add_constraint(git_repo, "Survives.")
    progress.append(git_repo, run, BODY)
    assert progress.constraints(git_repo) == ["Survives."]
    assert "magic-link sign in" in progress.read(git_repo)


def test_no_constraints_and_no_entries_reads_as_empty(git_repo):
    assert progress.planner_view(git_repo) == ""


def test_progress_show_gives_the_planner_view_by_default(capsys, git_repo, run):
    progress.add_constraint(git_repo, "Weights stay Decimal.")
    bodies(git_repo, run, 8)
    _code, out, _err = invoke(capsys, "--repo", str(git_repo), "progress", "show")
    assert "Weights stay Decimal." in out
    assert "Run 7" in out
    assert "Run 1" not in out


def test_progress_show_all_still_prints_the_whole_log(capsys, git_repo, run):
    bodies(git_repo, run, 8)
    _code, out, _err = invoke(capsys, "--repo", str(git_repo), "progress", "show", "--all")
    assert "Run 1" in out


def test_progress_promote_records_a_standing_constraint(capsys, git_repo, run):
    code, _out, _err = invoke(
        capsys, "--repo", str(git_repo), "progress", "promote", "Audit rows come from services."
    )
    assert code == 0
    assert progress.constraints(git_repo) == ["Audit rows come from services."]
