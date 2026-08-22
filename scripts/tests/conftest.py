"""Shared pytest fixtures. Adds ``scripts/`` to sys.path so tests import
``codag`` the same way ``codag.py`` does, with no packaging step.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from codag import osenv  # noqa: E402


@pytest.fixture(autouse=True)
def temp_root(tmp_path, monkeypatch):
    """Keep every test's worktrees inside tmp_path, never the real temp dir."""
    root = tmp_path / "codag-temp"
    root.mkdir()
    monkeypatch.setenv("CODAG_TEMP_ROOT", str(root))
    return root


@pytest.fixture
def git_repo(tmp_path):
    """A tiny initialised git repo with one commit on ``main``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    osenv.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    osenv.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    osenv.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    osenv.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    osenv.run(["git", "add", "-A"], cwd=repo, check=True)
    osenv.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def commit_file(repo, relpath, text, message):
    """Write a file inside ``repo`` and commit it. Returns the commit sha."""
    target = pathlib.Path(repo) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    osenv.run(["git", "add", "-A"], cwd=repo, check=True)
    osenv.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return osenv.git_out(["rev-parse", "HEAD"], cwd=repo)


@pytest.fixture
def env_no_ci(monkeypatch):
    """Some assertions depend on git not inheriting a CI author identity."""
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    return os.environ
