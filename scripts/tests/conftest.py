"""Shared pytest fixtures. Adds ``scripts/`` to sys.path so tests import
``codag`` the same way ``codag.py`` does, with no packaging step.

Repository fixtures are built **once per session** and copied per test.
Building one with real git costs about half a second; copying it costs about
twenty milliseconds, and around six hundred tests need one. Each test still
gets its own independent repository - only the way it is produced changes.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from codag import debuglog, osenv  # noqa: E402
from codag.run import Run  # noqa: E402


@pytest.fixture(autouse=True)
def no_stray_tracing(monkeypatch):
    """Debug tracing off unless a test asks for it.

    Without this, a CODAG_DEBUG exported in someone's shell would make every
    test in the suite write a log file.
    """
    monkeypatch.delenv("CODAG_DEBUG", raising=False)
    debuglog.detach()
    yield
    debuglog.detach()


@pytest.fixture(autouse=True)
def temp_root(tmp_path, monkeypatch):
    """Keep every test's worktrees inside tmp_path, never the real temp dir."""
    root = tmp_path / "codag-temp"
    root.mkdir()
    monkeypatch.setenv("CODAG_TEMP_ROOT", str(root))
    return root


# -- repository templates, built once ---------------------------------------


def _build_git_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    osenv.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    osenv.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    osenv.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    osenv.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    osenv.run(["git", "add", "-A"], cwd=repo, check=True)
    osenv.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _build_node_repo(repo):
    """A repo with a detectable stack whose gates are fast and scriptable."""
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "scripts": {"build": "node -e \"process.exit(0)\"", "test": "node scripts/test.js"},
                "devDependencies": {"typescript": "5.6.0"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text("{}", encoding="utf-8")
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    scripts = repo / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "test.js").write_text("process.exit(0);\n", encoding="utf-8")
    osenv.git(["add", "-A"], cwd=repo, check=True)
    osenv.git(["commit", "-qm", "fixture project"], cwd=repo, check=True)
    return repo


@pytest.fixture(scope="session")
def _git_template(tmp_path_factory):
    return _build_git_repo(tmp_path_factory.mktemp("template") / "git-repo")


@pytest.fixture(scope="session")
def _node_template(tmp_path_factory):
    return _build_node_repo(_build_git_repo(tmp_path_factory.mktemp("template") / "node-repo"))


@pytest.fixture
def git_repo(tmp_path, _git_template):
    """A tiny initialised git repo with one commit on ``main``."""
    target = tmp_path / "repo"
    shutil.copytree(_git_template, target)
    return target


@pytest.fixture
def node_repo(tmp_path, _node_template):
    """A repo with a detectable stack whose gates are fast and scriptable."""
    target = tmp_path / "repo"
    shutil.copytree(_node_template, target)
    return target


# -- a run, without paying for the CLI's init -------------------------------


def make_run(repo, title="fixture feature", mode="chat"):
    """A run directory and a detected stack, without the CLI's ``init``.

    For the many tests that need a run to exist but are not about ``init``
    itself. Skips preflight, orphan reaping, the integration worktree and the
    baseline gates - about 1.8 seconds a test. Tests whose subject *is*
    ``init`` still call the real thing.
    """
    from codag import run as runmod, stack as stackmod

    # Hiding .codag/ is part of "a run exists"; writing .gitignore is not -
    # that belongs to init, and init's own tests still cover it.
    runmod.ensure_ignored(repo)
    run = Run.create(repo, title, mode)
    stackmod.write(repo, run.stack_path)
    run.set_phase("grill")
    return run


@pytest.fixture
def ready_run(node_repo):
    """``make_run`` as a fixture, for tests that want it ready-made."""
    return make_run(node_repo)


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
