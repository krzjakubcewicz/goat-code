"""The cross-OS primitives. These are the tests that matter most in the
Windows / macOS / Linux CI matrix.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import sys
import threading

import pytest

from codag import osenv


def test_run_captures_output_without_a_shell():
    result = osenv.run([sys.executable, "-c", "print('hi')"])
    assert result.ok
    assert result.out == "hi"
    assert result.returncode == 0


def test_run_reports_failure_without_raising_by_default():
    result = osenv.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert not result.ok
    assert result.returncode == 3


def test_run_check_raises_command_error():
    with pytest.raises(osenv.CommandError) as excinfo:
        osenv.run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"], check=True)
    assert "boom" in str(excinfo.value)
    assert excinfo.value.returncode == 1


def test_missing_executable_is_a_command_error():
    with pytest.raises(osenv.CommandError) as excinfo:
        osenv.run(["definitely-not-a-real-binary-xyz"], check=True)
    assert excinfo.value.returncode == 127


def test_shell_metacharacters_are_never_interpreted(tmp_path):
    """Proof there is no shell: the string is an argument, not a command."""
    marker = tmp_path / "should-not-exist"
    result = osenv.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "a && touch {}".format(marker)])
    assert result.out == "a && touch {}".format(marker)
    assert not marker.exists()


def test_run_decodes_non_ascii():
    result = osenv.run([sys.executable, "-c", "print('caf\\u00e9')"])
    assert "café" in result.stdout


def test_timeout_returns_124(tmp_path):
    result = osenv.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_git_and_repo_root(git_repo):
    assert osenv.git(["status", "--porcelain"], cwd=git_repo).ok
    assert osenv.repo_root(git_repo) == git_repo.resolve()


def test_repo_root_is_none_outside_a_repo(tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert osenv.repo_root(outside) is None


def test_temp_root_honours_override(temp_root):
    root = osenv.temp_root()
    assert root.exists()
    assert root.parent == temp_root


def test_temp_root_paths_stay_short():
    """Windows caps paths near 260 chars; the run slug keeps us far below."""
    slug = osenv.run_slug("20260822-114900-a-very-long-feature-name-that-goes-on")
    assert len(slug) == 8
    assert slug == osenv.run_slug("20260822-114900-a-very-long-feature-name-that-goes-on")
    assert slug != osenv.run_slug("20260822-114901-other")


def test_rmtree_force_removes_read_only_files(tmp_path):
    tree = tmp_path / "tree" / "nested"
    tree.mkdir(parents=True)
    locked = tree / "readonly.txt"
    locked.write_text("x", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)
    assert osenv.rmtree_force(tmp_path / "tree")
    assert not (tmp_path / "tree").exists()


def test_rmtree_force_is_idempotent(tmp_path):
    assert osenv.rmtree_force(tmp_path / "never-existed")


def test_write_text_is_atomic_and_lf(tmp_path):
    target = tmp_path / "deep" / "file.txt"
    osenv.write_text(target, "line one\nline two\n")
    assert target.read_bytes() == b"line one\nline two\n"
    assert not list(tmp_path.glob("**/*.tmp*"))


def test_write_json_roundtrip(tmp_path):
    target = tmp_path / "data.json"
    osenv.write_json(target, {"b": 1, "a": [1, 2]})
    assert osenv.read_json(target) == {"b": 1, "a": [1, 2]}


def test_file_lock_is_exclusive(tmp_path):
    target = tmp_path / "tasks.yaml"
    target.write_text("x", encoding="utf-8")
    order = []

    with osenv.FileLock(target, timeout=5):
        order.append("outer-in")

        def contender():
            with osenv.FileLock(target, timeout=5):
                order.append("inner-in")

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=0.5)
        assert order == ["outer-in"], "second holder entered while the lock was held"
        order.append("outer-out")

    thread.join(timeout=5)
    assert order == ["outer-in", "outer-out", "inner-in"]


def test_file_lock_times_out(tmp_path):
    target = tmp_path / "tasks.yaml"
    with osenv.FileLock(target, timeout=5):
        with pytest.raises(TimeoutError):
            with osenv.FileLock(target, timeout=0.2):
                pass


def test_file_lock_releases_on_exception(tmp_path):
    target = tmp_path / "tasks.yaml"
    with pytest.raises(ValueError):
        with osenv.FileLock(target, timeout=5):
            raise ValueError("boom")
    with osenv.FileLock(target, timeout=1):
        pass


def test_no_module_uses_shell_true():
    """A guardrail: enabling the shell would break the cross-OS contract.

    Occurrences inside ``double backticks`` are prose, not code.
    """
    package = pathlib.Path(osenv.__file__).parent
    pattern = re.compile(r"(?<!``)shell\s*=\s*True")
    for module in package.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert not pattern.search(text), "{} enables the shell".format(module.name)


def test_only_osenv_calls_subprocess_directly():
    """Everything else must go through osenv.run so the env stays uniform."""
    package = pathlib.Path(osenv.__file__).parent
    for module in package.glob("*.py"):
        if module.name == "osenv.py":
            continue
        text = module.read_text(encoding="utf-8")
        assert "import subprocess" not in text, "{} imports subprocess".format(module.name)
