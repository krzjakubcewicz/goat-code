"""tasks.yaml access: wave scheduling, merge order, and concurrency safety."""

from __future__ import annotations

import copy
import multiprocessing
import sys

import pytest

from goatcode import miniyaml, tasks
from goatcode.tasks import TaskError

PLAN = {
    "version": 1,
    "run_id": "20260822-114900-demo",
    "cycle": 1,
    "goal": "Users sign in with a magic link.",
    "slices": [
        {
            "id": "S1",
            "title": "Token store",
            "depends_on": [],
            "owns": ["src/auth/**"],
            "acceptance": [{"id": "A1", "text": "one"}],
            "tests": ["tests/auth.test.ts"],
            "status": "pending",
            "commits": {"base": None, "head": None},
        },
        {
            "id": "S2",
            "title": "Mailer",
            "depends_on": [],
            "owns": ["src/mail/**"],
            "acceptance": [{"id": "A1", "text": "one"}],
            "tests": ["tests/mail.test.ts"],
            "status": "pending",
        },
        {
            "id": "S3",
            "title": "Login route",
            "depends_on": ["S1", "S2"],
            "owns": ["src/routes/login/**"],
            "acceptance": [{"id": "A1", "text": "one"}],
            "tests": ["tests/login.test.ts"],
            "status": "pending",
        },
    ],
}


@pytest.fixture
def plan_file(tmp_path):
    path = tmp_path / "tasks.yaml"
    miniyaml.dump(copy.deepcopy(PLAN), path)
    return path


# -- reading ---------------------------------------------------------------


def test_load_and_ids(plan_file):
    doc = tasks.load(plan_file)
    assert tasks.ids(doc) == ["S1", "S2", "S3"]


def test_load_rejects_a_missing_file(tmp_path):
    with pytest.raises(TaskError):
        tasks.load(tmp_path / "nope.yaml")


def test_load_rejects_a_non_mapping(tmp_path):
    path = tmp_path / "tasks.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(TaskError):
        tasks.load(path)


def test_get_unknown_slice_raises(plan_file):
    with pytest.raises(TaskError):
        tasks.get(tasks.load(plan_file), "S9")


# -- scheduling ------------------------------------------------------------


def test_waves_group_independent_slices(plan_file):
    assert tasks.waves(tasks.load(plan_file)) == [["S1", "S2"], ["S3"]]


def test_ready_returns_the_whole_first_wave(plan_file):
    assert tasks.ready(tasks.load(plan_file)) == ["S1", "S2"]


def test_ready_withholds_a_slice_until_its_deps_finish(plan_file):
    tasks.set_status(plan_file, "S1", "done")
    assert tasks.ready(tasks.load(plan_file)) == ["S2"]
    tasks.set_status(plan_file, "S2", "done")
    assert tasks.ready(tasks.load(plan_file)) == ["S3"]


def test_carried_dependencies_count_as_finished(plan_file):
    tasks.set_status(plan_file, "S1", "carried")
    tasks.set_status(plan_file, "S2", "carried")
    assert tasks.ready(tasks.load(plan_file)) == ["S3"]


def test_a_failed_dependency_keeps_dependents_waiting(plan_file):
    tasks.set_status(plan_file, "S1", "failed")
    tasks.set_status(plan_file, "S2", "done")
    assert tasks.ready(tasks.load(plan_file)) == []
    assert tasks.blocked_on(tasks.load(plan_file), "S3") == ["S1"]


def test_claimed_slices_are_not_offered_again(plan_file):
    tasks.set_status(plan_file, "S1", "claimed")
    assert tasks.ready(tasks.load(plan_file)) == ["S2"]


def test_remaining_and_counts(plan_file):
    tasks.set_status(plan_file, "S1", "done")
    doc = tasks.load(plan_file)
    assert tasks.remaining(doc) == ["S2", "S3"]
    assert tasks.counts(doc)["done"] == 1
    assert tasks.counts(doc)["pending"] == 2


# -- mutation --------------------------------------------------------------


def test_set_status_persists_and_returns_the_previous(plan_file):
    assert tasks.set_status(plan_file, "S1", "done") == "pending"
    assert tasks.get(tasks.load(plan_file), "S1")["status"] == "done"


def test_set_status_rejects_an_unknown_status(plan_file):
    with pytest.raises(TaskError):
        tasks.set_status(plan_file, "S1", "vibing")


def test_set_field_writes_arbitrary_values(plan_file):
    tasks.set_field(plan_file, "S2", "worktree", "/tmp/goatcode/ab/S2")
    assert tasks.get(tasks.load(plan_file), "S2")["worktree"].endswith("S2")


def test_record_commits_merges_into_the_existing_mapping(plan_file):
    tasks.record_commits(plan_file, "S1", base="aaa")
    tasks.record_commits(plan_file, "S1", head="bbb")
    commits = tasks.get(tasks.load(plan_file), "S1")["commits"]
    assert commits == {"base": "aaa", "head": "bbb"}


def test_record_commits_creates_the_mapping_when_absent(plan_file):
    tasks.record_commits(plan_file, "S2", base="aaa", head="bbb")
    assert tasks.get(tasks.load(plan_file), "S2")["commits"] == {"base": "aaa", "head": "bbb"}


def test_claim_is_exclusive(plan_file):
    assert tasks.claim(plan_file, "S1") is True
    assert tasks.claim(plan_file, "S1") is False


def test_mutation_preserves_the_rest_of_the_document(plan_file):
    tasks.set_status(plan_file, "S1", "done")
    doc = tasks.load(plan_file)
    assert doc["goal"] == PLAN["goal"]
    assert doc["run_id"] == PLAN["run_id"]
    assert len(doc["slices"]) == 3
    assert doc["slices"][2]["depends_on"] == ["S1", "S2"]


def test_update_returns_the_mutator_result(plan_file):
    assert tasks.update(plan_file, lambda doc: doc["goal"]) == PLAN["goal"]


# -- merge order and carry-forward ----------------------------------------


def test_merge_order_follows_dependencies(plan_file):
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(plan_file, slice_id, "done")
    assert tasks.merge_order(tasks.load(plan_file)) == ["S1", "S2", "S3"]


def test_merge_order_skips_unfinished_slices(plan_file):
    tasks.set_status(plan_file, "S1", "done")
    assert tasks.merge_order(tasks.load(plan_file)) == ["S1"]


def test_carry_forward_marks_finished_slices(plan_file):
    tasks.set_status(plan_file, "S1", "done")
    doc = tasks.load(plan_file)
    assert tasks.carry_forward(doc, {"S1", "S2"}) == ["S1"]
    assert tasks.get(doc, "S1")["status"] == "carried"
    assert tasks.get(doc, "S2")["status"] == "pending"


# -- concurrency -----------------------------------------------------------


def _worker(path, slice_id):
    sys.path.insert(0, str(path.parents[2] / "scripts"))
    from goatcode import tasks as worker_tasks

    worker_tasks.set_status(path, slice_id, "done")


def test_parallel_writers_do_not_lose_updates(plan_file):
    """Three executors reporting at once must all be recorded."""
    procs = [
        multiprocessing.Process(target=_worker, args=(plan_file, slice_id))
        for slice_id in ("S1", "S2", "S3")
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0

    doc = tasks.load(plan_file)
    assert [s["status"] for s in doc["slices"]] == ["done", "done", "done"]


def test_sequential_writes_are_all_visible(plan_file):
    for index, slice_id in enumerate(("S1", "S2", "S3")):
        tasks.set_field(plan_file, slice_id, "branch", "goatcode/run/{}".format(index))
    doc = tasks.load(plan_file)
    assert [s["branch"] for s in doc["slices"]] == ["goatcode/run/0", "goatcode/run/1", "goatcode/run/2"]


# -- rendering -------------------------------------------------------------


def test_render_table_shows_waves_and_checks(plan_file):
    text = tasks.render_table(tasks.load(plan_file))
    assert "wave 1 - 2 slice(s) run in parallel" in text
    assert "wave 2 - 1 slice(s) run in parallel" in text
    assert "S3" in text and "after:  S1, S2" in text
    assert "src/auth/**" in text


def test_render_table_surfaces_assumptions(plan_file):
    doc = tasks.load(plan_file)
    doc["assumptions"] = ["Token TTL assumed 15 min."]
    assert "! Token TTL assumed 15 min." in tasks.render_table(doc)


def test_render_table_reports_a_cycle_instead_of_crashing(plan_file):
    doc = tasks.load(plan_file)
    doc["slices"][0]["depends_on"] = ["S3"]
    assert "cycle" in tasks.render_table(doc)


# -- what a later cycle still has to judge ---------------------------------
#
# A remedial cycle changes a handful of lines and the verifier is then told to
# re-judge every criterion against the whole diff again. It only needs to
# re-judge what actually moved; the rest was judged against identical code.


def test_a_slice_owning_none_of_the_changed_files_is_unchanged():
    doc = copy.deepcopy(PLAN)
    assert tasks.unchanged_slices(doc, ["src/mail/send.ts"]) == ["S1", "S3"]


def test_a_slice_owning_a_changed_file_is_not_unchanged():
    doc = copy.deepcopy(PLAN)
    assert "S1" not in tasks.unchanged_slices(doc, ["src/auth/tokens.ts"])


def test_a_shared_path_a_slice_touches_also_makes_it_changed():
    doc = copy.deepcopy(PLAN)
    tasks.get(doc, "S1")["touches_shared"] = ["src/db/migrations/"]
    assert "S1" not in tasks.unchanged_slices(doc, ["src/db/migrations/003_tokens.sql"])


def test_no_changed_files_leaves_every_slice_unchanged():
    doc = copy.deepcopy(PLAN)
    assert tasks.unchanged_slices(doc, []) == ["S1", "S2", "S3"]
