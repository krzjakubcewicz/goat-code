"""`init --clone`: an empty directory in, and nothing of the run outside it.

The point of a sandbox is what does *not* happen - no worktree under the
system temp dir, no scratch file there either - so most of these assert on
where things landed rather than on what was printed.
"""

from __future__ import annotations

import os
import sys

import pytest

from goatcode import guard, machine, osenv, run as runmod
from goatcode.run import Run
from tests.conftest import commit_file, make_run
from tests.test_cli import invoke, invoke_json
from tests.test_guard import hook


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An empty directory to stand in, with the temp variables pinned.

    The CLI runs in-process here and a sandbox rewrites ``os.environ``;
    setting each name through monkeypatch first is what puts it back.
    """
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, os.environ.get(name, str(tmp_path)))
    target = tmp_path / "box"
    target.mkdir()
    monkeypatch.chdir(target)
    return target


@pytest.fixture
def source(git_repo):
    """A repository with history, so a shallow clone has something to leave behind."""
    commit_file(git_repo, "src/a.txt", "a\n", "feat: a")
    return git_repo


def start(capsys, source, *extra):
    # A file URL, not a path: git ignores --depth for a plain local path.
    return invoke_json(capsys, "init", "--clone", source.as_uri(), "--prompt", "add a thing", *extra)


def under(path, parent):
    return guard.inside(path, [guard.real(parent)])


def test_a_cloned_run_keeps_everything_in_the_directory(capsys, box, source):
    code, payload, err = start(capsys, source)
    assert code == 0, err

    repo = box / "repo"
    assert osenv.git_out(["rev-parse", "--is-shallow-repository"], cwd=repo) == "true"
    assert runmod.load_config(repo)["sandbox"] is True
    # The clone is what the user pushes from; init must not have dirtied it.
    assert osenv.git(["status", "--porcelain"], cwd=repo).stdout == ""

    run = Run.load(repo)
    assert under(run.temp_root, box)
    assert under(payload["integration_worktree"], box)

    seen = osenv.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"], check=True
    ).out
    assert guard.real(seen) == guard.real(box / "tmp")


def test_a_directory_that_is_not_empty_is_refused(capsys, box, source):
    (box / "notes.txt").write_text("mine\n", encoding="utf-8")

    code, _out, err = invoke(capsys, "init", "--clone", source.as_uri(), "--prompt", "x", "--no-baseline")
    assert code != 0
    assert "notes.txt" in err
    assert not (box / "repo").exists()

    code, _payload, err = start(capsys, source, "--no-baseline", "--force")
    assert code == 0, err


def test_a_missing_spec_is_caught_before_the_clone(capsys, box, source):
    code, _out, err = invoke(capsys, "init", "--clone", source.as_uri(), "--spec", "nope.md")
    assert code != 0
    assert "nope.md" in err
    assert not (box / "repo").exists()


def test_the_spec_may_sit_in_the_sandbox(capsys, box, source):
    (box / "spec.md").write_text("# Magic link\n\nBuild it.\n", encoding="utf-8")
    code, payload, err = invoke_json(
        capsys, "init", "--clone", source.as_uri(), "--spec", "spec.md", "--no-baseline"
    )
    assert code == 0, err
    assert "Build it." in osenv.read_text(payload["spec"])


def test_later_commands_find_the_run_from_the_sandbox(capsys, box, source):
    started = start(capsys, source, "--no-baseline")[1]
    code, payload, err = invoke_json(capsys, "next")
    assert code == 0, err
    assert payload["action"]
    assert Run.load(box / "repo").run_id == started["run_id"]


def test_the_guard_admits_the_sandbox_and_finds_its_run(capsys, box, source, tmp_path):
    start(capsys, source, "--no-baseline")
    repo = box / "repo"
    run = Run.load(repo)

    roots = guard.allowed_roots(repo, runmod.load_config(repo), run.state)
    assert guard.inside(box / "tmp" / "scratch.txt", roots)
    assert not guard.inside(tmp_path / "elsewhere" / "app.js", roots)

    assert guard.real(hook().find_repo(box)) == guard.real(repo)


def test_only_a_sandboxed_run_is_told_how_to_push(capsys, box, source):
    assert machine.push_hint(make_run(source)) == []

    start(capsys, source, "--no-baseline")
    run = Run.load(box / "repo")
    (hint,) = machine.push_hint(run)
    assert "push -u origin {}".format(run.integration_branch) in hint
