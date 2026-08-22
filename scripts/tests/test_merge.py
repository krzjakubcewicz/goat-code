"""Ordered merge into the integration branch, and review packages.

These tests build real conflicting branches: the merge machinery is the
part of the pipeline most likely to corrupt work if it is wrong.
"""

from __future__ import annotations

import copy

import pytest

from codag import diffpkg, merge, miniyaml, osenv, tasks, worktree
from codag.merge import MergeError
from codag.run import Run

PLAN = {
    "version": 1,
    "run_id": "placeholder",
    "cycle": 1,
    "goal": "Three slices land together.",
    "slices": [
        {
            "id": "S1",
            "title": "Auth",
            "depends_on": [],
            "owns": ["src/auth/**"],
            "acceptance": [{"id": "A1", "text": "x"}],
            "tests": ["tests/auth.test.ts"],
            "status": "done",
        },
        {
            "id": "S2",
            "title": "Mail",
            "depends_on": [],
            "owns": ["src/mail/**"],
            "acceptance": [{"id": "A1", "text": "x"}],
            "tests": ["tests/mail.test.ts"],
            "status": "done",
        },
        {
            "id": "S3",
            "title": "Route",
            "depends_on": ["S1", "S2"],
            "owns": ["src/routes/**"],
            "acceptance": [{"id": "A1", "text": "x"}],
            "tests": ["tests/route.test.ts"],
            "status": "done",
        },
    ],
}


@pytest.fixture
def run(git_repo):
    return Run.create(git_repo, "merge demo", "chat")


@pytest.fixture
def plan(run):
    doc = copy.deepcopy(PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    return doc


def work_in(run, slice_id, files, message="work"):
    """Create the slice worktree and commit ``{relpath: text}`` in it."""
    path, _branch, _setup = worktree.create(run, slice_id, setup=False)
    for relpath, text in files.items():
        target = path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    osenv.git(["add", "-A"], cwd=path, check=True)
    osenv.git(["commit", "-qm", "{}: {}".format(slice_id, message)], cwd=path, check=True)
    return path


# -- clean merges ----------------------------------------------------------


def test_clean_merge_lands_every_slice(run, plan):
    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    work_in(run, "S2", {"src/mail/send.ts": "mail\n"})
    work_in(run, "S3", {"src/routes/login.ts": "route\n"})

    state = merge.run_merge(run, plan)

    assert state["status"] == "clean"
    assert state["merged"] == ["S1", "S2", "S3"]
    integration = run.temp_root / worktree.INTEGRATION_DIR
    assert (integration / "src/auth/token.ts").exists()
    assert (integration / "src/mail/send.ts").exists()
    assert (integration / "src/routes/login.ts").exists()


def test_merge_order_follows_dependencies(run, plan):
    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    work_in(run, "S2", {"src/mail/send.ts": "mail\n"})
    work_in(run, "S3", {"src/routes/login.ts": "route\n"})
    merge.run_merge(run, plan)

    integration = run.temp_root / worktree.INTEGRATION_DIR
    subjects = osenv.git(["log", "--format=%s"], cwd=integration).stdout
    assert subjects.index("codag: merge slice S3") < subjects.index("codag: merge slice S1")


def test_merge_starts_from_the_base_commit(run, plan, git_repo):
    from tests.conftest import commit_file

    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    commit_file(git_repo, "unrelated.txt", "drift", "drift on main")
    tasks.set_status(run.tasks_path, "S2", "pending")
    tasks.set_status(run.tasks_path, "S3", "pending")
    plan = tasks.load(run.tasks_path)

    merge.run_merge(run, plan)
    integration = run.temp_root / worktree.INTEGRATION_DIR
    assert not (integration / "unrelated.txt").exists()


def test_slices_that_are_not_done_are_not_merged(run, plan):
    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    tasks.set_status(run.tasks_path, "S2", "failed")
    tasks.set_status(run.tasks_path, "S3", "pending")
    state = merge.run_merge(run, tasks.load(run.tasks_path))
    assert state["merged"] == ["S1"]


def test_a_slice_with_no_commits_is_skipped_not_failed(run, plan):
    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    worktree.create(run, "S2", setup=False)
    work_in(run, "S3", {"src/routes/login.ts": "route\n"})
    state = merge.run_merge(run, plan)
    assert state["status"] == "clean"
    assert state["merged"] == ["S1", "S2", "S3"]


def test_empty_plan_reports_empty(run, plan):
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "pending")
    state = merge.run_merge(run, tasks.load(run.tasks_path))
    assert state["status"] == "empty"


# -- conflicts -------------------------------------------------------------


def conflicting(run):
    """Two slices editing the same shared file: a real merge conflict."""
    work_in(run, "S1", {"src/shared/registry.ts": "export const items = ['auth'];\n"})
    work_in(run, "S2", {"src/shared/registry.ts": "export const items = ['mail'];\n"})


def test_conflict_stops_the_merge_and_names_the_files(run, plan):
    conflicting(run)
    tasks.set_status(run.tasks_path, "S3", "pending")
    state = merge.run_merge(run, tasks.load(run.tasks_path))

    assert state["status"] == "conflict"
    assert state["conflicted"] == "S2"
    assert state["conflicts"] == ["src/shared/registry.ts"]
    assert state["merged"] == ["S1"]


def test_conflict_is_recorded_in_the_report(run, plan):
    conflicting(run)
    tasks.set_status(run.tasks_path, "S3", "pending")
    merge.run_merge(run, tasks.load(run.tasks_path))

    text = (run.cycle_dir() / "merge-report.md").read_text(encoding="utf-8")
    assert "Status: **conflict**" in text
    assert "src/shared/registry.ts" in text
    assert "codag merge --continue" in text
    assert "Synthesizer edits" in text


def test_merge_state_survives_a_reload(run, plan, git_repo):
    conflicting(run)
    tasks.set_status(run.tasks_path, "S3", "pending")
    merge.run_merge(run, tasks.load(run.tasks_path))

    reloaded = Run.load(git_repo, run.run_id)
    assert merge.state_of(reloaded)["conflicted"] == "S2"


def test_resume_after_the_synthesizer_resolves(run, plan):
    conflicting(run)
    tasks.set_status(run.tasks_path, "S3", "pending")
    doc = tasks.load(run.tasks_path)
    merge.run_merge(run, doc)

    integration = run.temp_root / worktree.INTEGRATION_DIR
    (integration / "src/shared/registry.ts").write_text(
        "export const items = ['auth', 'mail'];\n", encoding="utf-8"
    )
    state = merge.resume(run, doc)

    assert state["status"] == "clean"
    assert state["merged"] == ["S1", "S2"]
    assert "auth" in (integration / "src/shared/registry.ts").read_text(encoding="utf-8")
    assert "mail" in (integration / "src/shared/registry.ts").read_text(encoding="utf-8")


def test_resume_refuses_while_markers_remain(run, plan):
    conflicting(run)
    tasks.set_status(run.tasks_path, "S3", "pending")
    doc = tasks.load(run.tasks_path)
    merge.run_merge(run, doc)

    with pytest.raises(MergeError) as excinfo:
        merge.resume(run, doc)
    assert "conflict markers" in str(excinfo.value)


def test_resume_continues_through_a_later_slice(run, plan):
    work_in(run, "S1", {"src/shared/registry.ts": "auth\n"})
    work_in(run, "S2", {"src/shared/registry.ts": "mail\n"})
    work_in(run, "S3", {"src/routes/login.ts": "route\n"})
    doc = tasks.load(run.tasks_path)
    merge.run_merge(run, doc)

    integration = run.temp_root / worktree.INTEGRATION_DIR
    (integration / "src/shared/registry.ts").write_text("auth+mail\n", encoding="utf-8")
    state = merge.resume(run, doc)

    assert state["status"] == "clean"
    assert state["merged"] == ["S1", "S2", "S3"]
    assert (integration / "src/routes/login.ts").exists()


def test_reset_restarts_the_integration_branch(run, plan):
    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    tasks.set_status(run.tasks_path, "S2", "pending")
    tasks.set_status(run.tasks_path, "S3", "pending")
    merge.run_merge(run, tasks.load(run.tasks_path))

    integration = run.temp_root / worktree.INTEGRATION_DIR
    assert (integration / "src/auth/token.ts").exists()

    tasks.set_status(run.tasks_path, "S1", "pending")
    state = merge.run_merge(run, tasks.load(run.tasks_path), reset=True)
    assert state["status"] == "empty"
    assert not (integration / "src/auth/token.ts").exists()


def test_clean_report_lists_what_landed(run, plan):
    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    work_in(run, "S2", {"src/mail/send.ts": "mail\n"})
    work_in(run, "S3", {"src/routes/login.ts": "route\n"})
    merge.run_merge(run, plan)

    text = (run.cycle_dir() / "merge-report.md").read_text(encoding="utf-8")
    assert "Status: **clean**" in text
    assert "Auth" in text and "Mail" in text and "Route" in text


def test_integration_head_is_reported(run, plan):
    work_in(run, "S1", {"src/auth/token.ts": "auth\n"})
    tasks.set_status(run.tasks_path, "S2", "pending")
    tasks.set_status(run.tasks_path, "S3", "pending")
    merge.run_merge(run, tasks.load(run.tasks_path))
    assert len(merge.integration_head(run)) == 40


# -- review packages -------------------------------------------------------


def test_review_package_contains_commits_stat_and_diff(run, plan, git_repo):
    work_in(run, "S1", {"src/auth/token.ts": "export const token = 1;\n"}, message="add token")
    tasks.set_status(run.tasks_path, "S2", "pending")
    tasks.set_status(run.tasks_path, "S3", "pending")
    merge.run_merge(run, tasks.load(run.tasks_path))

    integration = run.temp_root / worktree.INTEGRATION_DIR
    head = worktree.head_commit(integration)
    text = diffpkg.build(git_repo, run.base_commit, head)

    assert "## Commits" in text
    assert "S1: add token" in text
    assert "## Files changed" in text
    assert "src/auth/token.ts" in text
    assert "## Diff" in text
    assert "+export const token = 1;" in text


def test_review_package_filename_carries_the_range(run, plan, git_repo, tmp_path):
    work_in(run, "S1", {"a.txt": "a\n"})
    tasks.set_status(run.tasks_path, "S2", "pending")
    tasks.set_status(run.tasks_path, "S3", "pending")
    merge.run_merge(run, tasks.load(run.tasks_path))

    head = worktree.head_commit(run.temp_root / worktree.INTEGRATION_DIR)
    path = diffpkg.write(tmp_path, run.base_commit, head, cwd=git_repo)
    assert path.name.startswith("review-")
    assert ".." in path.name
    assert path.read_text(encoding="utf-8").startswith("# Review package")


def test_review_package_rejects_an_unknown_ref(git_repo):
    with pytest.raises(ValueError):
        diffpkg.build(git_repo, "HEAD", "nope-not-a-ref")


def test_is_empty_and_changed_files(run, plan, git_repo):
    assert diffpkg.is_empty(git_repo, run.base_commit, run.base_commit)
    work_in(run, "S1", {"src/auth/token.ts": "x\n"})
    tasks.set_status(run.tasks_path, "S2", "pending")
    tasks.set_status(run.tasks_path, "S3", "pending")
    merge.run_merge(run, tasks.load(run.tasks_path))

    head = worktree.head_commit(run.temp_root / worktree.INTEGRATION_DIR)
    assert not diffpkg.is_empty(git_repo, run.base_commit, head)
    assert diffpkg.changed_files(git_repo, run.base_commit, head) == ["src/auth/token.ts"]
