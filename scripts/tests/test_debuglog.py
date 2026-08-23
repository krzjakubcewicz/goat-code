"""The debug trace.

Off by default and off in the rest of the suite: an autouse fixture resets
it between tests so a stray CODAG_DEBUG in the environment cannot make the
other 700 tests write log files.
"""

from __future__ import annotations

import pathlib

import pytest

from codag import debuglog, osenv
from codag.run import Run
from tests.conftest import make_run
from tests.test_cli import cli, invoke, invoke_json  # noqa: F401


@pytest.fixture
def tracing(tmp_path, monkeypatch):
    monkeypatch.setenv("CODAG_DEBUG", "1")
    target = tmp_path / "trace"
    target.mkdir()
    debuglog.attach(target)
    return target


# -- the switch ------------------------------------------------------------


def test_off_by_default():
    assert debuglog.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_env_var_turns_it_on(monkeypatch, value):
    monkeypatch.setenv("CODAG_DEBUG", value)
    assert debuglog.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_other_env_values_leave_it_off(monkeypatch, value):
    """An empty CODAG_DEBUG in a shell profile must not enable it."""
    monkeypatch.setenv("CODAG_DEBUG", value)
    assert debuglog.enabled() is False


def test_config_turns_it_on():
    debuglog.configure(True)
    assert debuglog.enabled() is True


def test_the_env_var_overrides_config(monkeypatch):
    debuglog.configure(True)
    monkeypatch.setenv("CODAG_DEBUG", "0")
    assert debuglog.enabled() is False, "the environment is the override"


def test_nothing_is_written_when_it_is_off(tmp_path):
    debuglog.attach(tmp_path)
    debuglog.log("exec", argv=["git", "status"])
    assert not (tmp_path / "log.txt").exists()


# -- what a line looks like ------------------------------------------------


def test_a_line_carries_a_timestamp_pid_and_event(tracing):
    debuglog.log("exec", rc=0)
    line = debuglog.read(tracing)[0]
    assert line.startswith("20")
    assert "pid={}".format(__import__("os").getpid()) in line
    assert " exec " in line
    assert "rc=0" in line


def test_fields_with_spaces_are_quoted(tracing):
    debuglog.log("exec", argv=["git", "rev-parse", "HEAD"])
    assert 'argv="git rev-parse HEAD"' in debuglog.read(tracing)[0]


def test_none_fields_are_dropped(tracing):
    debuglog.log("action", kind="run", slice=None)
    line = debuglog.read(tracing)[0]
    assert "kind=run" in line
    assert "slice" not in line


def test_newlines_never_break_a_line(tracing):
    debuglog.log("exec", error="first\nsecond")
    assert len(debuglog.read(tracing)) == 1
    assert "first\\nsecond" in debuglog.read(tracing)[0]


def test_a_very_long_line_is_truncated(tracing):
    debuglog.log("exec", argv=["x" * 5000])
    line = debuglog.read(tracing)[0]
    assert len(line) <= debuglog.MAX_LINE
    assert line.endswith("...")


# -- appending -------------------------------------------------------------


def test_entries_append_never_replace(tracing):
    for index in range(5):
        debuglog.log("exec", n=index)
    lines = debuglog.read(tracing)
    assert len(lines) == 5
    assert "n=0" in lines[0] and "n=4" in lines[-1]


def test_events_before_attach_are_buffered_and_flushed(tmp_path, monkeypatch):
    """A failing init is exactly the trace you want, and it happens before
    the run directory exists."""
    monkeypatch.setenv("CODAG_DEBUG", "1")
    debuglog.log("cli", command="init")
    debuglog.log("exec", argv=["git", "status"])

    target = tmp_path / "run"
    debuglog.attach(target)
    lines = debuglog.read(target)
    assert len(lines) == 2
    assert "command=init" in lines[0]


def test_the_buffer_does_not_grow_without_bound(monkeypatch):
    monkeypatch.setenv("CODAG_DEBUG", "1")
    for index in range(700):
        debuglog.log("exec", n=index)
    assert len(debuglog._PENDING) <= 500


def test_an_unwritable_target_does_not_raise(tmp_path, monkeypatch):
    """A broken log must never take the pipeline down with it."""
    monkeypatch.setenv("CODAG_DEBUG", "1")
    debuglog.attach(tmp_path)
    monkeypatch.setattr(debuglog, "_TARGET", tmp_path / "nope" / "deep" / "log.txt")
    debuglog.log("exec", rc=0)


def test_read_is_empty_when_there_is_no_log(tmp_path):
    assert debuglog.read(tmp_path) == []


# -- what actually gets traced --------------------------------------------


def test_subprocesses_are_traced(tracing):
    osenv.run(["git", "--version"])
    lines = debuglog.read(tracing)
    assert any("exec" in line and "git" in line and "rc=0" in line for line in lines)
    assert any("ms=" in line for line in lines)


def test_a_failed_subprocess_is_traced(tracing):
    """The trace must not simply stop where the failure was."""
    with pytest.raises(osenv.CommandError):
        osenv.run(["definitely-not-a-real-binary-xyz"])
    assert any("rc=127" in line for line in debuglog.read(tracing))


def test_a_nonzero_exit_is_traced(tracing):
    import sys as _sys

    osenv.run([_sys.executable, "-c", "import sys; sys.exit(3)"])
    assert any("rc=3" in line for line in debuglog.read(tracing))


def test_file_writes_are_traced(tracing, tmp_path):
    osenv.write_text(tmp_path / "thing.txt", "hello")
    assert any("write" in line and "bytes=5" in line for line in debuglog.read(tracing))


def test_phase_transitions_are_traced(git_repo, tracing):
    run = make_run(git_repo)
    run.set_phase("execute")
    assert any("phase" in line and "now=execute" in line for line in debuglog.read(tracing))


def test_an_unchanged_phase_is_not_traced(git_repo, tracing):
    run = make_run(git_repo)
    run.set_phase("execute")
    before = len([line for line in debuglog.read(tracing) if " phase " in line])
    run.set_phase("execute")
    after = len([line for line in debuglog.read(tracing) if " phase " in line])
    assert after == before


# -- through the CLI -------------------------------------------------------


def test_the_cli_writes_into_the_run_directory(capsys, node_repo, monkeypatch):
    monkeypatch.setenv("CODAG_DEBUG", "1")
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline"
    )
    assert code == 0

    run = Run.load(node_repo)
    assert payload["debug_log"] == str(run.root / "log.txt")
    lines = debuglog.read(run.root)
    assert any("cli" in line and "command=init" in line for line in lines)
    assert any(" exec " in line for line in lines), "git calls during init are traced"


def test_the_command_and_its_exit_code_are_both_traced(capsys, node_repo, monkeypatch):
    monkeypatch.setenv("CODAG_DEBUG", "1")
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    debuglog.detach()

    monkeypatch.setenv("CODAG_DEBUG", "1")
    invoke(capsys, "--repo", str(node_repo), "status")
    lines = debuglog.read(run.root)
    assert any("cli-done" in line and "rc=0" in line for line in lines)


def test_nothing_is_written_without_the_switch(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    assert not (run.root / "log.txt").exists()
    assert debuglog.read(run.root) == []


def test_config_alone_enables_it(capsys, node_repo):
    config = node_repo / ".codag" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("debug: true\n", encoding="utf-8")

    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    assert (run.root / "log.txt").exists()


def test_the_trace_lives_with_the_run_it_describes(capsys, node_repo, monkeypatch):
    monkeypatch.setenv("CODAG_DEBUG", "1")
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    assert (run.root / "log.txt").parent == run.root
    assert not (node_repo / ".codag" / "log.txt").exists()
