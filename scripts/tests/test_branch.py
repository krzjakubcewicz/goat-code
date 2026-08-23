"""The feature branch: where a run forks from, and what the branch is called.

Every branch a run creates - the baseline, each slice, the feature branch -
forks from one base, so the final diff is exactly base..feature: the thing
you would open a pull request with.
"""

from __future__ import annotations

import copy
import datetime

import pytest

from codag import miniyaml, osenv, run as runmod, worktree
from codag.run import Run, RunError
from tests.conftest import make_run
from tests.test_cli import cli, invoke, invoke_json  # noqa: F401

PLAN = {
    "version": 1,
    "run_id": "placeholder",
    "cycle": 1,
    "goal": "Users sign in with a magic link.",
    "kind": "feature",
    "kind_reason": "Adds a sign-in route.",
    "slices": [
        {
            "id": "S1",
            "title": "Token store",
            "intent": "Persist tokens.",
            "depends_on": [],
            "owns": ["src/auth/**"],
            "acceptance": [{"id": "A1", "text": "consumable once"}],
            "tests": ["tests/S1.test.js"],
            "status": "pending",
        }
    ],
}


def write_plan(run, **overrides):
    doc = copy.deepcopy(PLAN)
    doc["run_id"] = run.run_id
    doc.update(overrides)
    miniyaml.dump(doc, run.tasks_path)
    return doc


def commit(repo, name, message="work"):
    (repo / name).write_text("x\n", encoding="utf-8")
    osenv.git(["add", "-A"], cwd=repo, check=True)
    osenv.git(["commit", "-qm", message], cwd=repo, check=True)
    return osenv.git_out(["rev-parse", "HEAD"], cwd=repo)


def configure(repo, text):
    target = repo / ".codag" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# -- resolving the base ----------------------------------------------------


def test_main_is_found(git_repo):
    name, commit_sha = runmod.resolve_base_branch(git_repo)
    assert name == "main"
    assert commit_sha == osenv.git_out(["rev-parse", "main"], cwd=git_repo)


def test_master_is_found_when_there_is_no_main(git_repo):
    osenv.git(["branch", "-m", "main", "master"], cwd=git_repo, check=True)
    name, _sha = runmod.resolve_base_branch(git_repo)
    assert name == "master"


def test_main_wins_over_master(git_repo):
    osenv.git(["branch", "master", "main"], cwd=git_repo, check=True)
    name, _sha = runmod.resolve_base_branch(git_repo)
    assert name == "main"


def test_config_overrides_the_detection(git_repo):
    osenv.git(["branch", "develop", "main"], cwd=git_repo, check=True)
    name, _sha = runmod.resolve_base_branch(git_repo, "develop")
    assert name == "develop"


def test_a_configured_branch_that_does_not_exist_is_an_error(git_repo):
    with pytest.raises(RunError) as excinfo:
        runmod.resolve_base_branch(git_repo, "nope")
    assert "does not exist" in str(excinfo.value)


def test_no_base_branch_at_all_is_reported(git_repo):
    osenv.git(["branch", "-m", "main", "wip"], cwd=git_repo, check=True)
    name, _sha = runmod.resolve_base_branch(git_repo)
    assert name is None
    _root, problems = runmod.preflight(git_repo)
    assert any("no base branch found" in p for p in problems)


# -- the run forks from the base, not from HEAD ---------------------------


def test_the_run_bases_itself_on_the_base_branch(git_repo):
    base = osenv.git_out(["rev-parse", "main"], cwd=git_repo)
    osenv.git(["checkout", "-q", "-b", "my-wip"], cwd=git_repo, check=True)
    commit(git_repo, "wip.txt")

    run = Run.create(git_repo, "x", "chat")
    assert run.state["base_branch"] == "main"
    assert run.base_commit == base, "the run must fork from main, not from my-wip"
    assert run.state["current_branch"] == "my-wip"


def test_slice_worktrees_fork_from_the_base(git_repo):
    base = osenv.git_out(["rev-parse", "main"], cwd=git_repo)
    osenv.git(["checkout", "-q", "-b", "my-wip"], cwd=git_repo, check=True)
    commit(git_repo, "wip.txt")

    run = Run.create(git_repo, "x", "chat")
    write_plan(run)
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    assert osenv.git_out(["rev-parse", "HEAD"], cwd=path) == base
    assert not (path / "wip.txt").exists()


def test_divergence_is_reported(git_repo):
    osenv.git(["checkout", "-q", "-b", "my-wip"], cwd=git_repo, check=True)
    commit(git_repo, "one.txt", "first")
    commit(git_repo, "two.txt", "second")

    found = runmod.divergence(git_repo, "main")
    assert found["branch"] == "my-wip"
    assert len(found["commits"]) == 2
    assert any("second" in c for c in found["commits"])


def test_no_divergence_on_the_base_branch(git_repo):
    assert runmod.divergence(git_repo, "main") is None


def test_no_divergence_when_the_branch_is_level(git_repo):
    osenv.git(["checkout", "-q", "-b", "level"], cwd=git_repo, check=True)
    assert runmod.divergence(git_repo, "main") is None


# -- rendering the name ----------------------------------------------------


@pytest.mark.parametrize(
    "template,expected",
    [
        ("{kind}/{slug}", "feature/users-sign-in-with-a-magic-link"),
        ("codag/{slug}", "codag/users-sign-in-with-a-magic-link"),
        ("{kind}/{date}-{slug}", "feature/20260823-users-sign-in-with-a-magic-link"),
        ("{slug}", "users-sign-in-with-a-magic-link"),
    ],
)
def test_templates_render(git_repo, template, expected):
    configure(git_repo, 'branch_template: "{}"\n'.format(template))
    run = Run.create(git_repo, "x", "chat")
    doc = write_plan(run)
    when = datetime.datetime(2026, 8, 23, 1, 2, 3)
    assert run.proposed_branch(doc, now=when) == expected


def test_a_bugfix_gets_the_bugfix_prefix(git_repo):
    run = Run.create(git_repo, "x", "chat")
    doc = write_plan(run, kind="bugfix", goal="Token expiry is off by one")
    assert run.proposed_branch(doc) == "bugfix/token-expiry-is-off-by-one"


def test_the_kind_override_reaches_the_branch_name(git_repo):
    run = Run.create(git_repo, "x", "chat")
    run.set_kind_override("bugfix")
    doc = write_plan(run, kind="feature")
    assert run.proposed_branch(doc).startswith("bugfix/")


def test_an_unknown_placeholder_is_a_clear_error(git_repo):
    configure(git_repo, 'branch_template: "{ticket}/{slug}"\n')
    run = Run.create(git_repo, "x", "chat")
    doc = write_plan(run)
    with pytest.raises(RunError) as excinfo:
        run.proposed_branch(doc)
    assert "unknown placeholder" in str(excinfo.value)
    assert "slug" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("feature/a b c", "feature/a-b-c"),
        ("feature//double", "feature/double"),
        ("feature/dot..dot", "feature/dot.dot"),
        ("/leading/", "leading"),
        ("feature/x.lock", "feature/x"),
        ("feature/we?rd*chars", "feature/we-rd-chars"),
        ("", "codag-run"),
    ],
)
def test_names_are_made_git_safe(raw, expected):
    assert runmod.sanitise_branch(raw) == expected


def test_a_taken_name_gets_a_suffix(git_repo):
    osenv.git(["branch", "feature/taken", "main"], cwd=git_repo, check=True)
    assert runmod.unique_branch(git_repo, "feature/taken") == "feature/taken-2"


def test_a_free_name_is_used_as_is(git_repo):
    assert runmod.unique_branch(git_repo, "feature/free") == "feature/free"


# -- creating it through the CLI ------------------------------------------


def start(capsys, repo, **plan_overrides):
    """A run with a plan. The `init` path is exercised by its own tests below."""
    run = make_run(repo, "magic link")
    write_plan(run, **plan_overrides)
    return run


def test_branch_creates_and_records_the_name(capsys, node_repo):
    start(capsys, node_repo)
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "branch")

    assert code == 0
    assert payload["branch"] == "feature/users-sign-in-with-a-magic-link"
    assert payload["created"] is True
    assert osenv.git(
        ["rev-parse", "--verify", "refs/heads/" + payload["branch"]], cwd=node_repo
    ).ok


def test_the_branch_forks_from_the_base(capsys, node_repo):
    run = start(capsys, node_repo)
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "branch")
    assert osenv.git_out(["rev-parse", payload["branch"]], cwd=node_repo) == run.base_commit


def test_the_branch_becomes_the_merge_target(capsys, node_repo):
    start(capsys, node_repo)
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "branch")
    reloaded = Run.load(node_repo)
    assert reloaded.feature_branch == payload["branch"]
    assert reloaded.integration_branch == payload["branch"]


def test_the_old_provisional_branch_is_gone(capsys, node_repo):
    run = start(capsys, node_repo)
    provisional = run.integration_branch
    invoke(capsys, "--repo", str(node_repo), "branch")
    assert not osenv.git(["rev-parse", "--verify", "refs/heads/" + provisional], cwd=node_repo).ok


def test_the_integration_worktree_follows_the_rename(capsys, node_repo):
    run = start(capsys, node_repo)
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "branch")
    path = worktree.integration_path(run)
    assert osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path) == payload["branch"]


def test_branch_is_idempotent(capsys, node_repo):
    start(capsys, node_repo)
    _code, first, _err = invoke_json(capsys, "--repo", str(node_repo), "branch")
    _code, second, _err = invoke_json(capsys, "--repo", str(node_repo), "branch")
    assert second["created"] is False
    assert second["branch"] == first["branch"]


def test_an_explicit_name_wins(capsys, node_repo):
    start(capsys, node_repo)
    _code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "branch", "--name", "release/hotfix"
    )
    assert payload["branch"] == "release/hotfix"


def test_a_collision_is_reported(capsys, node_repo):
    start(capsys, node_repo)
    osenv.git(
        ["branch", "feature/users-sign-in-with-a-magic-link", "main"], cwd=node_repo, check=True
    )
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "branch")
    assert payload["collided"] is True
    assert payload["branch"].endswith("-2")


def test_the_users_branch_is_never_checked_out(capsys, node_repo):
    start(capsys, node_repo)
    before = osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=node_repo)
    invoke(capsys, "--repo", str(node_repo), "branch")
    assert osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=node_repo) == before


def test_init_warns_about_commits_the_base_does_not_have(capsys, node_repo):
    osenv.git(["checkout", "-q", "-b", "my-wip"], cwd=node_repo, check=True)
    commit(node_repo, "wip.txt", "unpushed work")

    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline"
    )
    assert code == 0, "a diverged branch warns; it does not block"
    assert payload["divergence"]["branch"] == "my-wip"
    assert any("unpushed work" in c for c in payload["divergence"]["commits"])


def test_the_divergence_warning_is_visible_in_the_output(capsys, node_repo):
    osenv.git(["checkout", "-q", "-b", "my-wip"], cwd=node_repo, check=True)
    commit(node_repo, "wip.txt", "unpushed work")
    _code, out, _err = invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    assert "my-wip" in out
    assert "NOT included in this run" in out
    assert "unpushed work" in out
