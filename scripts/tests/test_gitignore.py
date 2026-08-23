"""Managing the project's .gitignore on the first run.

cod-ag writes its entries there and deliberately leaves the change
uncommitted - it does not commit to your branch. The interesting part is
that preflight must then tolerate its own edit, or the next run would fail
the clean-tree check on a file cod-ag dirtied itself.
"""

from __future__ import annotations

import pytest

from codag import osenv, run as runmod
from codag.run import Run
from tests.test_cli import cli, invoke, invoke_json  # noqa: F401


def gitignore(repo):
    return repo / ".gitignore"


def commit_all(repo, message="chore: ignore cod-ag"):
    osenv.git(["add", "-A"], cwd=repo, check=True)
    osenv.git(["commit", "-qm", message], cwd=repo, check=True)


# -- writing the block -----------------------------------------------------


def test_creates_gitignore_when_absent(git_repo):
    assert not gitignore(git_repo).exists()
    assert runmod.ensure_gitignore(git_repo) is True

    body = gitignore(git_repo).read_text(encoding="utf-8")
    assert body == runmod.gitignore_block()
    assert ".codag/" in body
    assert ".worktrees/" in body


def test_appends_to_an_existing_gitignore_without_disturbing_it(git_repo):
    gitignore(git_repo).write_text("node_modules\ndist\n", encoding="utf-8")
    assert runmod.ensure_gitignore(git_repo) is True

    lines = gitignore(git_repo).read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["node_modules", "dist"]
    assert lines[2] == ""
    assert lines[3] == runmod.GITIGNORE_HEADER
    assert set(lines[4:]) == set(runmod.GITIGNORE_ENTRIES)


def test_appends_when_the_file_has_no_trailing_newline(git_repo):
    gitignore(git_repo).write_text("node_modules", encoding="utf-8")
    runmod.ensure_gitignore(git_repo)
    lines = gitignore(git_repo).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "node_modules"
    assert ".codag/" in lines


def test_is_idempotent(git_repo):
    runmod.ensure_gitignore(git_repo)
    assert runmod.ensure_gitignore(git_repo) is False
    body = gitignore(git_repo).read_text(encoding="utf-8")
    assert body.count(".codag/") == 1
    assert body.count(runmod.GITIGNORE_HEADER) == 1


@pytest.mark.parametrize("existing", [".codag/\n.worktrees/\n", ".codag\n.worktrees\n"])
def test_respects_entries_the_user_already_added(git_repo, existing):
    gitignore(git_repo).write_text(existing, encoding="utf-8")
    assert runmod.ensure_gitignore(git_repo) is False
    assert runmod.GITIGNORE_HEADER not in gitignore(git_repo).read_text(encoding="utf-8")


def test_adds_the_block_when_only_some_entries_are_present(git_repo):
    gitignore(git_repo).write_text(".codag/\n", encoding="utf-8")
    assert runmod.ensure_gitignore(git_repo) is True
    assert ".worktrees/" in gitignore(git_repo).read_text(encoding="utf-8")


def test_the_file_is_written_with_lf_endings(git_repo):
    gitignore(git_repo).write_text("node_modules\n", encoding="utf-8")
    runmod.ensure_gitignore(git_repo)
    assert b"\r" not in gitignore(git_repo).read_bytes()


# -- stripping the block back out -----------------------------------------


@pytest.mark.parametrize(
    "before",
    ["", "node_modules\n", "node_modules\ndist\n", "# comment\n\nnode_modules\n"],
)
def test_stripping_the_block_restores_the_original(git_repo, before):
    if before:
        gitignore(git_repo).write_text(before, encoding="utf-8")
    runmod.ensure_gitignore(git_repo)
    after = gitignore(git_repo).read_text(encoding="utf-8")
    assert runmod.strip_gitignore_block(after) == before


def test_stripping_text_without_our_block_changes_nothing():
    assert runmod.strip_gitignore_block("node_modules\n") == "node_modules\n"


def test_stripping_leaves_entries_the_user_added_after_our_block(git_repo):
    runmod.ensure_gitignore(git_repo)
    body = gitignore(git_repo).read_text(encoding="utf-8") + "coverage/\n"
    assert runmod.strip_gitignore_block(body) == "coverage/\n"


# -- preflight tolerance ---------------------------------------------------


def test_preflight_tolerates_our_own_edit(git_repo):
    """Without this the second run would fail on cod-ag's own change."""
    gitignore(git_repo).write_text("node_modules\n", encoding="utf-8")
    commit_all(git_repo)
    runmod.ensure_gitignore(git_repo)

    assert osenv.git(["status", "--porcelain"], cwd=git_repo).out, "the tree really is modified"
    _root, problems = runmod.preflight(git_repo)
    assert problems == []


def test_preflight_tolerates_a_gitignore_we_created(git_repo):
    runmod.ensure_gitignore(git_repo)
    _root, problems = runmod.preflight(git_repo)
    assert problems == []


def test_preflight_still_objects_to_a_real_gitignore_edit(git_repo):
    gitignore(git_repo).write_text("node_modules\n", encoding="utf-8")
    commit_all(git_repo)
    runmod.ensure_gitignore(git_repo)

    body = gitignore(git_repo).read_text(encoding="utf-8")
    gitignore(git_repo).write_text(body + "secrets.env\n", encoding="utf-8")

    _root, problems = runmod.preflight(git_repo)
    assert any(".gitignore" in p for p in problems)


def test_preflight_objects_to_a_gitignore_edit_without_our_block(git_repo):
    gitignore(git_repo).write_text("node_modules\n", encoding="utf-8")
    commit_all(git_repo)
    gitignore(git_repo).write_text("node_modules\nsecrets.env\n", encoding="utf-8")

    _root, problems = runmod.preflight(git_repo)
    assert any(".gitignore" in p for p in problems)


def test_preflight_is_clean_once_the_block_is_committed(git_repo):
    runmod.ensure_gitignore(git_repo)
    commit_all(git_repo)
    _root, problems = runmod.preflight(git_repo)
    assert problems == []


# -- through the CLI -------------------------------------------------------


def test_init_writes_the_gitignore_and_says_so(capsys, node_repo):
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline"
    )
    assert code == 0
    assert payload["gitignore_updated"] is True
    body = (node_repo / ".gitignore").read_text(encoding="utf-8")
    assert ".codag/" in body


def test_init_mentions_the_uncommitted_change_in_its_output(capsys, node_repo):
    _code, out, _err = invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    assert "uncommitted change" in out


def test_init_also_writes_info_exclude(capsys, node_repo):
    """Belt and braces: the state stays hidden even if .gitignore is reverted."""
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    exclude = node_repo / ".git" / "info" / "exclude"
    assert ".codag/" in exclude.read_text(encoding="utf-8")

    (node_repo / ".gitignore").unlink()
    assert osenv.git(["status", "--porcelain"], cwd=node_repo).out == ""


def test_a_second_run_is_not_blocked_by_the_first_runs_edit(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "one", "--no-baseline")
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "two", "--no-baseline"
    )
    assert code == 0, "the second run must not trip over cod-ag's own .gitignore edit"
    assert payload["gitignore_updated"] is False


def test_init_never_commits(capsys, node_repo):
    before = osenv.git_out(["rev-parse", "HEAD"], cwd=node_repo)
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    assert osenv.git_out(["rev-parse", "HEAD"], cwd=node_repo) == before
    assert osenv.git(["status", "--porcelain"], cwd=node_repo).out.strip() == "?? .gitignore"


def test_the_run_base_commit_is_unaffected(capsys, node_repo):
    before = osenv.git_out(["rev-parse", "HEAD"], cwd=node_repo)
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    assert Run.load(node_repo).base_commit == before


# -- the opt-out -----------------------------------------------------------


def test_manage_gitignore_false_leaves_the_file_alone(capsys, node_repo):
    config = node_repo / ".codag" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("manage_gitignore: false\n", encoding="utf-8")

    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline"
    )
    assert code == 0
    assert payload["gitignore_updated"] is False
    assert not (node_repo / ".gitignore").exists()


def test_the_opt_out_still_hides_the_state(capsys, node_repo):
    config = node_repo / ".codag" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("manage_gitignore: false\n", encoding="utf-8")
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")

    assert ".codag/" in (node_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert osenv.git(["status", "--porcelain"], cwd=node_repo).out == ""


def test_manage_gitignore_defaults_on(git_repo):
    assert runmod.load_config(git_repo)["manage_gitignore"] is True
