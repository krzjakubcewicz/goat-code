"""Worktree lifecycle against a real git repository."""

from __future__ import annotations

import copy

import pytest

from codag import brief, miniyaml, osenv, worktree
from codag.run import Run
from codag.worktree import WorktreeError

PLAN = {
    "version": 1,
    "run_id": "placeholder",
    "cycle": 1,
    "goal": "Users sign in with a magic link.",
    "global_constraints": ["Node >= 20, no new runtime deps"],
    "slices": [
        {
            "id": "S1",
            "title": "Token store",
            "intent": "Persist single-use tokens.",
            "depends_on": [],
            "owns": ["src/auth/**", "tests/auth/**"],
            "touches_shared": ["src/db/migrations/"],
            "interfaces": ["createToken(email): Token"],
            "acceptance": [
                {"id": "A1", "text": "consumeToken returns the email once, then null."},
                {"id": "A2", "text": "A token older than 15 minutes returns null."},
            ],
            "tests": [{"path": "tests/auth/store.test.ts", "must_cover": ["single use", "expiry"]}],
            "out_of_scope": ["email delivery"],
            "status": "pending",
        },
        {
            "id": "S2",
            "title": "Mailer",
            "intent": "Send the link.",
            "depends_on": ["S1"],
            "uses_interfaces": ["createToken(email): Token"],
            "owns": ["src/mail/**"],
            "acceptance": [{"id": "A1", "text": "One email per request."}],
            "tests": ["tests/mail/send.test.ts"],
            "status": "pending",
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


# -- creation --------------------------------------------------------------


def test_create_makes_an_isolated_checkout(run):
    path, branch, _setup = worktree.create(run, "S1", setup=False)
    assert path.exists()
    assert (path / "README.md").exists()
    assert branch == "codag/{}/S1".format(run.run_id)
    assert osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path) == branch


def test_worktree_lives_outside_the_repository(run, git_repo):
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    assert git_repo not in path.parents


def test_worktree_paths_stay_short(run):
    """cod-ag's own contribution to the path must be tiny.

    Measured against the temp root, because the test harness's tmp_path is
    itself long. In production this is ``%TEMP%\\codag\\<8 hex>\\S1``.
    """
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    added = str(path)[len(str(osenv.temp_root().parent)) :]
    assert len(added) < 30, "cod-ag adds {} chars: {}".format(len(added), added)


def test_branch_starts_at_the_recorded_base_not_head(run, git_repo):
    from tests.conftest import commit_file

    base = run.base_commit
    commit_file(git_repo, "drift.txt", "moved on", "drift")
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    assert osenv.git_out(["rev-parse", "HEAD"], cwd=path) == base
    assert not (path / "drift.txt").exists()


def test_two_slices_get_independent_checkouts(run):
    first, _b1, _s1 = worktree.create(run, "S1", setup=False)
    second, _b2, _s2 = worktree.create(run, "S2", setup=False)
    assert first != second
    (first / "only-in-first.txt").write_text("x", encoding="utf-8")
    assert not (second / "only-in-first.txt").exists()


def test_create_is_idempotent(run):
    first, _b, _s = worktree.create(run, "S1", setup=False)
    again, _b2, _s2 = worktree.create(run, "S1", setup=False)
    assert first == again


def test_create_recovers_from_a_stale_directory(run):
    path = worktree.path_for(run, "S1")
    path.mkdir(parents=True)
    (path / "junk.txt").write_text("left over from a crash", encoding="utf-8")
    created, _branch, _setup = worktree.create(run, "S1", setup=False)
    assert created == path
    assert not (path / "junk.txt").exists()
    assert (path / "README.md").exists()


def test_create_reuses_an_existing_branch(run, git_repo):
    branch = worktree.branch_name(run.run_id, "S1")
    osenv.git(["branch", branch, run.base_commit], cwd=git_repo, check=True)
    path, made, _setup = worktree.create(run, "S1", setup=False)
    assert made == branch
    assert path.exists()


def test_state_records_the_worktree(run, git_repo):
    worktree.create(run, "S1", setup=False)
    assert "S1" in Run.load(git_repo, run.run_id).state["worktrees"]


# -- setup -----------------------------------------------------------------


def test_setup_runs_the_detected_command(run):
    osenv.write_json(run.stack_path, {"commands": {"setup": ["git", "--version"]}})
    _path, _branch, result = worktree.create(run, "S1", setup=True)
    assert result is not None and result.ok
    assert "git version" in result.stdout


def test_setup_is_skipped_when_the_stack_has_no_command(run):
    osenv.write_json(run.stack_path, {"commands": {"setup": None}})
    _path, _branch, result = worktree.create(run, "S1", setup=True)
    assert result is None


def test_setup_failure_is_reported_not_raised(run):
    osenv.write_json(run.stack_path, {"commands": {"setup": ["git", "not-a-command"]}})
    _path, _branch, result = worktree.create(run, "S1", setup=True)
    assert result is not None and not result.ok


# -- integration worktree --------------------------------------------------


def test_integration_worktree_uses_the_run_branch(run):
    path, branch = worktree.create_integration(run)
    assert branch == run.integration_branch
    assert osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path) == branch


def test_integration_worktree_is_idempotent(run):
    first, _b = worktree.create_integration(run)
    again, _b2 = worktree.create_integration(run)
    assert first == again


# -- removal ---------------------------------------------------------------


def test_remove_deletes_the_directory_but_keeps_the_branch(run, git_repo):
    path, branch, _setup = worktree.create(run, "S1", setup=False)
    assert worktree.remove(run, "S1")
    assert not path.exists()
    assert osenv.git(["rev-parse", "--verify", "refs/heads/" + branch], cwd=git_repo).ok
    assert "S1" not in Run.load(git_repo, run.run_id).state["worktrees"]


def test_remove_can_delete_the_branch_too(run, git_repo):
    _path, branch, _setup = worktree.create(run, "S1", setup=False)
    worktree.remove(run, "S1", delete_branch=True)
    assert not osenv.git(["rev-parse", "--verify", "refs/heads/" + branch], cwd=git_repo).ok


def test_remove_survives_a_read_only_file(run):
    import os
    import stat

    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    locked = path / "locked.txt"
    locked.write_text("x", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)
    assert worktree.remove(run, "S1")
    assert not path.exists()


def test_remove_is_idempotent(run):
    worktree.create(run, "S1", setup=False)
    assert worktree.remove(run, "S1")
    assert worktree.remove(run, "S1")


def test_reap_clears_slice_worktrees_and_keeps_integration(run):
    worktree.create(run, "S1", setup=False)
    worktree.create(run, "S2", setup=False)
    integration, _branch = worktree.create_integration(run)
    removed = worktree.reap(run)
    assert sorted(removed) == ["S1", "S2"]
    assert integration.exists()


def test_reap_everything_removes_the_temp_root(run):
    worktree.create(run, "S1", setup=False)
    worktree.create_integration(run)
    worktree.reap(run, keep_integration=False)
    assert not run.temp_root.exists()


def test_reap_orphans_prunes_a_vanished_worktree(run, git_repo):
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    osenv.rmtree_force(path)
    worktree.reap_orphans(git_repo)
    assert not any(p == path for p in worktree.existing(git_repo))


def test_orphan_reaping_lets_creation_succeed_again(run, git_repo):
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    osenv.rmtree_force(path)
    worktree.reap_orphans(git_repo)
    recreated, _b, _s = worktree.create(run, "S1", setup=False)
    assert recreated.exists()


# -- inspection ------------------------------------------------------------


def test_existing_lists_the_main_worktree_and_ours(run, git_repo):
    worktree.create(run, "S1", setup=False)
    listed = worktree.existing(git_repo)
    branches = set(listed.values())
    assert "main" in branches
    assert "codag/{}/S1".format(run.run_id) in branches


def test_commits_between_reports_the_range(run, git_repo):
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    base = worktree.head_commit(path)
    (path / "a.txt").write_text("a", encoding="utf-8")
    osenv.git(["add", "-A"], cwd=path, check=True)
    osenv.git(["commit", "-qm", "add a"], cwd=path, check=True)
    head = worktree.head_commit(path)
    commits = worktree.commits_between(git_repo, base, head)
    assert [subject for _sha, subject in commits] == ["add a"]


def test_is_dirty_detects_uncommitted_work(run):
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    assert not worktree.is_dirty(path)
    (path / "wip.txt").write_text("x", encoding="utf-8")
    assert worktree.is_dirty(path)


def test_create_reports_a_git_failure_clearly(run, monkeypatch):
    monkeypatch.setattr(
        worktree.osenv, "git", lambda *a, **k: osenv.Result(["git"], 1, "", "fatal: nope", 0.0)
    )
    with pytest.raises(WorktreeError) as excinfo:
        worktree.create(run, "S1", setup=False)
    assert "fatal: nope" in str(excinfo.value)


# -- briefs ----------------------------------------------------------------


def test_brief_contains_the_whole_contract(run, plan):
    osenv.write_json(
        run.stack_path,
        {
            "languages": ["typescript"],
            "frameworks": ["nextjs"],
            "package_manager": "pnpm",
            "test_framework": "vitest",
            "commands": {"test": ["pnpm", "run", "test"], "lint": ["pnpm", "run", "lint"]},
            "specialist_skills": ["engineering-skills:senior-frontend"],
        },
    )
    text = brief.build(run, plan, "S1")

    assert "# Slice S1: Token store" in text
    assert "Persist single-use tokens." in text
    assert "Node >= 20, no new runtime deps" in text
    assert "`src/auth/**`" in text
    assert "src/db/migrations/" in text
    assert "append-only" in text
    assert "createToken(email): Token" in text
    assert "**A1**: consumeToken returns the email once, then null." in text
    assert "**A2**" in text
    assert "tests/auth/store.test.ts" in text
    assert "must cover: single use" in text
    assert "email delivery" in text
    assert "failing test first" in text
    assert "pnpm run test" in text
    assert "engineering-skills:senior-frontend" in text
    assert "DONE_WITH_CONCERNS" in text
    assert str(run.report_path("S1")) in text


def test_brief_names_the_provider_of_a_consumed_interface(run, plan):
    text = brief.build(run, plan, "S2")
    assert "createToken(email): Token" in text
    assert "(from S1)" in text


def test_brief_states_the_vertical_slice_rule(run, plan):
    assert "stand on its own" in brief.build(run, plan, "S1")


def test_brief_forbids_touching_other_slices(run, plan):
    text = brief.build(run, plan, "S1")
    assert "Never touch a path owned by another slice" in text
    assert "Do not run git merge, rebase, push" in text


def test_brief_warns_against_scope_creep(run, plan):
    assert "scope creep" in brief.build(run, plan, "S1")


def test_brief_surfaces_assumptions(run, plan):
    plan["assumptions"] = ["Token TTL assumed 15 min."]
    assert "Token TTL assumed 15 min." in brief.build(run, plan, "S1")


def test_brief_without_a_stack_says_so(run, plan):
    text = brief.build(run, plan, "S1")
    assert "Not detected" in text


def test_write_persists_the_brief(run, plan):
    path = brief.write(run, plan, "S1")
    assert path == run.brief_path("S1")
    assert path.read_text(encoding="utf-8").startswith("# Slice S1")


def test_brief_for_an_unknown_slice_raises(run, plan):
    with pytest.raises(KeyError):
        brief.build(run, plan, "S9")


def test_brief_warns_that_test_first_is_checked(run, plan):
    """An unannounced gate is a trap; the brief must say the check exists."""
    text = brief.build(run, plan, "S1")
    assert "This is checked" in text
    assert "the report is refused" in text


# -- where a slice branch starts -------------------------------------------


def test_a_slice_branches_off_the_integration_tip(run, git_repo):
    """A remedial slice must see the code earlier slices already landed."""
    integration, _branch = worktree.create_integration(run)
    (integration / "shipped.txt").write_text("from cycle 1", encoding="utf-8")
    osenv.git(["add", "shipped.txt"], cwd=integration)
    osenv.git(["commit", "-m", "cycle 1 work"], cwd=integration)
    tip = osenv.git_out(["rev-parse", "HEAD"], cwd=integration)

    assert worktree.start_point(run) == tip
    path, _branch, _setup = worktree.create(run, "R1", setup=False)
    assert (path / "shipped.txt").exists()


def test_start_point_is_the_base_commit_before_anything_merges(run):
    worktree.create_integration(run)
    assert worktree.start_point(run) == run.base_commit
