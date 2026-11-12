"""The repository boundary a live run is confined to.

The hook shell itself (``hooks/repo_guard.py``) is the one piece of goat-code
a test cannot drive end to end - it needs a real Claude Code session. So the
decision lives in ``guard`` and is tested here, and the shell around it is
kept as small as it can be.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from goatcode import guard, run as runmod
from tests.conftest import make_run


@pytest.fixture(autouse=True)
def headless(monkeypatch):
    """Refusal is what most of this file is about, and only a headless run
    refuses outright; the tests about asking take the variable back out."""
    monkeypatch.setenv("GOATCODE_HEADLESS", "1")


@pytest.fixture
def elsewhere(tmp_path):
    """Another repository, beside the one under test."""
    other = tmp_path / "other-project"
    (other / "src").mkdir(parents=True)
    (other / "src" / "app.js").write_text("stolen\n", encoding="utf-8")
    return other


@pytest.fixture
def roots(git_repo):
    return guard.allowed_roots(git_repo, runmod.load_config(git_repo))


def read(path):
    return {"file_path": str(path)}


# -- what is inside ---------------------------------------------------------


def test_the_repository_itself_is_allowed(git_repo, roots):
    assert guard.verdict("Read", read(git_repo / "README.md"), roots) is None


def test_the_runs_worktrees_are_allowed(git_repo):
    """They live in the system temp directory, not under the repo."""
    run = make_run(git_repo)
    roots = guard.allowed_roots(git_repo, run.config, run.state)
    assert guard.verdict("Read", read(run.temp_root / "S1" / "src" / "a.js"), roots) is None


def test_goat_codes_own_files_are_allowed(git_repo, roots):
    """Agents are handed an absolute path to goatcode.py and told to run it."""
    cli = guard.PLUGIN_ROOT / "scripts" / "goatcode.py"
    assert cli.exists()
    assert guard.verdict("Read", read(cli), roots) is None


def test_installed_skills_are_allowed(git_repo, roots):
    """A skill's own instructions tell an agent to read its reference files."""
    skill = os.path.expanduser("~/.claude/plugins/cache/somewhere/SKILL.md")
    assert guard.verdict("Read", read(skill), roots) is None


# -- what is outside --------------------------------------------------------


def test_the_interpreter_directory_is_an_allowed_root(git_repo, roots):
    """Dispatch prompts name the interpreter by absolute path; refusing it
    refused goat-code's own commands, 52 times in one run."""
    assert guard.real(os.path.dirname(sys.executable)) in roots


def test_a_command_naming_the_interpreter_is_not_refused(git_repo, roots):
    command = '"{}" -V'.format(sys.executable)
    assert guard.verdict("Bash", {"command": command}, roots, cwd=git_repo) is None


def test_the_interpreter_directory_does_not_widen_containment(git_repo, roots, elsewhere):
    assert guard.verdict("Read", read(elsewhere / "src" / "app.js"), roots, cwd=git_repo)


def test_the_configured_python_directory_is_an_allowed_root(git_repo, tmp_path):
    """Rendered commands name the configured python, so refusing its
    directory would refuse goat-code's own commands again."""
    exe = tmp_path / "py313" / "python.exe"
    exe.parent.mkdir()
    roots = guard.allowed_roots(git_repo, {"python": str(exe)})
    assert guard.real(exe.parent) in roots


def test_a_bare_configured_python_name_admits_nothing(git_repo):
    """A name resolved off PATH puts no path in the command, so there is
    nothing to admit - and dirname("python3") would be the working directory."""
    assert guard.allowed_roots(git_repo, {"python": "python3"}) == guard.allowed_roots(git_repo)


def test_allowed_roots_survives_no_executable(git_repo, monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    assert guard.allowed_roots(git_repo)


def test_another_repository_is_refused(git_repo, roots, elsewhere):
    reason = guard.verdict("Read", read(elsewhere / "src" / "app.js"), roots)
    assert reason and "outside this run's repository" in reason
    assert "guard.extra_roots" in reason, "a refusal an agent cannot act on becomes a retry"


def test_walking_up_out_of_a_worktree_is_refused(git_repo, elsewhere):
    run = make_run(git_repo)
    roots = guard.allowed_roots(git_repo, run.config, run.state)
    cwd = run.temp_root / "S1"
    up = os.path.join("..", "..", "..", elsewhere.name, "src", "app.js")
    assert guard.verdict("Read", {"file_path": up}, roots, cwd=cwd) is not None


def test_a_symlink_out_of_the_repository_is_refused(git_repo, roots, elsewhere):
    """String prefixes are not containment; both sides get realpath'd."""
    link = git_repo / "vendor"
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not let the test process create symlinks")
    assert guard.verdict("Read", read(link / "src" / "app.js"), roots) is not None


def test_a_glob_rooted_outside_is_refused(git_repo, roots, elsewhere):
    outside = {"pattern": str(elsewhere / "**" / "*.js")}
    assert guard.verdict("Glob", outside, roots) is not None


def test_a_relative_glob_is_judged_by_where_it_runs(git_repo, roots):
    """`**/*.js` names no root, so the working directory is what it means."""
    assert guard.verdict("Glob", {"pattern": "**/*.js"}, roots, cwd=git_repo) is None


def test_grep_with_a_path_outside_is_refused(git_repo, roots, elsewhere):
    assert guard.verdict("Grep", {"pattern": "token", "path": str(elsewhere)}, roots) is not None


def test_writing_outside_is_refused(git_repo, roots, elsewhere):
    assert guard.verdict("Write", read(elsewhere / "new.js"), roots) is not None


# -- bash, within its known ceiling ----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat {}/src/app.js",
        "cd {} && git log --oneline",
        "git -C {} log --oneline",
        "grep -r token {}",
    ],
)
def test_a_shell_command_naming_another_repository_is_refused(git_repo, roots, elsewhere, command):
    rendered = command.format(elsewhere)
    assert guard.verdict("Bash", {"command": rendered}, roots) is not None


def test_an_ordinary_command_in_the_repository_is_allowed(git_repo, roots):
    assert guard.verdict("Bash", {"command": "npm test -- --watch=false"}, roots, cwd=git_repo) is None
    assert guard.verdict("Bash", {"command": "git status --porcelain"}, roots, cwd=git_repo) is None


# -- the escape hatch ------------------------------------------------------


def test_extra_roots_admits_exactly_what_it_names(git_repo, tmp_path, elsewhere):
    sibling = tmp_path / "design-system"
    sibling.mkdir()
    config = dict(runmod.load_config(git_repo))
    config["guard"] = {"enabled": True, "extra_roots": ["../design-system"]}
    roots = guard.allowed_roots(git_repo, config)

    assert guard.verdict("Read", read(sibling / "tokens.css"), roots) is None
    assert guard.verdict("Read", read(elsewhere / "src" / "app.js"), roots) is not None


def test_the_guard_can_be_turned_off(git_repo):
    assert guard.enabled(runmod.load_config(git_repo)) is True
    assert guard.enabled({"guard": {"enabled": False}}) is False


def test_a_config_that_says_nothing_leaves_the_guard_on():
    """Absent means on. A guard you have to remember to enable is not one."""
    assert guard.enabled({}) is True
    assert guard.enabled(None) is True


# -- the log ---------------------------------------------------------------


def test_violations_reads_back_what_the_hook_appended(git_repo):
    run = make_run(git_repo)
    path = run.root / guard.LOG_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": "Read", "cwd": "a"}) + "\n")
        handle.write(json.dumps({"tool": "Glob", "cwd": "b"}) + "\n")

    found = guard.violations(run.root)
    assert [entry["tool"] for entry in found] == ["Read", "Glob"]
    assert run.summary()["guard_violations"] == 2


def test_a_torn_line_does_not_break_the_report(git_repo):
    """The log is appended to by several executors at once. A half-written
    last line must not stop the run it is reporting about."""
    run = make_run(git_repo)
    (run.root / guard.LOG_NAME).write_text(
        json.dumps({"tool": "Read"}) + "\n" + '{"tool": "Gl', encoding="utf-8"
    )
    assert len(guard.violations(run.root)) == 1


def test_no_log_is_not_an_error(git_repo):
    run = make_run(git_repo)
    assert guard.violations(run.root) == []


# -- the hook shell --------------------------------------------------------
#
# `decide` is the only part of hooks/repo_guard.py worth testing here; `main`
# is a try/except around it. What matters is when it declines to act at all,
# and that it never blocks because of its own failure.


def hook():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "hooks" / "repo_guard.py"
    spec = importlib.util.spec_from_file_location("repo_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(repo, tool="Read", **tool_input):
    return {"cwd": str(repo), "tool_name": tool, "tool_input": tool_input, "session_id": "abcd1234"}


def test_a_repository_with_no_run_is_not_policed(git_repo, elsewhere):
    """The hook is loaded for a whole session. Without this it would refuse
    the user's own reads in any repository goat-code has ever touched."""
    code, _message = hook().decide(payload(git_repo, file_path=str(elsewhere / "src" / "app.js")))
    assert code == 0


def test_a_finished_run_is_not_policed(git_repo, elsewhere):
    run = make_run(git_repo)
    run.set_phase("done")
    code, _message = hook().decide(payload(git_repo, file_path=str(elsewhere / "src" / "app.js")))
    assert code == 0


def test_a_live_run_refuses_and_records(git_repo, elsewhere):
    run = make_run(git_repo)
    run.set_phase("execute")
    target = elsewhere / "src" / "app.js"

    code, message = hook().decide(payload(git_repo, file_path=str(target)))
    assert code == 2
    assert "outside this run's repository" in message

    recorded = guard.violations(run.root)
    assert len(recorded) == 1
    assert recorded[0]["tool"] == "Read"
    assert str(target) in recorded[0]["input"]["file_path"]


def test_a_live_run_allows_the_repository(git_repo):
    run = make_run(git_repo)
    run.set_phase("execute")
    code, _message = hook().decide(payload(git_repo, file_path=str(git_repo / "README.md")))
    assert code == 0
    assert guard.violations(run.root) == [], "an allowed call records nothing"


def test_a_disabled_guard_allows_everything(git_repo, elsewhere):
    run = make_run(git_repo)
    run.set_phase("execute")
    (git_repo / ".goatcode" / "config.yaml").write_text(
        "guard:\n  enabled: false\n", encoding="utf-8"
    )
    code, _message = hook().decide(payload(git_repo, file_path=str(elsewhere / "src" / "app.js")))
    assert code == 0


def run_main(module, monkeypatch, text):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return module.main()


def test_an_unreadable_state_file_fails_open(git_repo, elsewhere, monkeypatch):
    """A guard that can take down every run it guards is worse than none."""
    run = make_run(git_repo)
    run.set_phase("execute")
    (run.root / "state.json").write_text("{not json", encoding="utf-8")

    call = json.dumps(payload(git_repo, file_path=str(elsewhere / "src" / "app.js")))
    assert run_main(hook(), monkeypatch, call) == 0


def test_a_payload_it_cannot_parse_fails_open(git_repo, monkeypatch):
    assert run_main(hook(), monkeypatch, "not json at all") == 0


def test_main_still_blocks_when_everything_works(git_repo, elsewhere, monkeypatch):
    run = make_run(git_repo)
    run.set_phase("execute")
    call = json.dumps(payload(git_repo, file_path=str(elsewhere / "src" / "app.js")))
    assert run_main(hook(), monkeypatch, call) == 2


def test_an_interpreter_that_is_not_python_does_not_take_down_the_hook(tmp_path, git_repo, elsewhere):
    """A name can be on PATH and not be a usable Python: the Microsoft Store
    app-execution alias, a pyenv shim with no version for this directory. Both
    print and exit non-zero, and Claude Code reports that as a failed hook on
    every tool call. `command -v` cannot tell them apart from a real
    interpreter, so the shell has to look at what the interpreter did.
    """
    import pathlib
    import shutil
    import subprocess

    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("needs a POSIX sh")

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text("#!/bin/sh\necho 'no Python here' >&2\nexit 9009\n", encoding="utf-8")
    stub.chmod(0o755)

    script = pathlib.Path(__file__).resolve().parents[2] / "hooks" / "repo_guard.sh"
    env = dict(os.environ, PATH=str(stub_dir) + os.pathsep + os.environ["PATH"])

    def hook_says(body):
        return subprocess.run(
            [sh, str(script)],
            input=json.dumps(body),
            capture_output=True,
            text=True,
            env=env,
        )

    run = make_run(git_repo)
    run.set_phase("execute")

    # The stub is first on PATH and cannot answer; the real interpreter behind
    # it still gets the payload, so the verdict is the guard's, not the stub's.
    outside = hook_says(payload(git_repo, file_path=str(elsewhere / "src" / "app.js")))
    assert outside.returncode == 2, outside.stderr
    assert "outside this run's repository" in outside.stderr
    assert "no Python here" not in outside.stderr

    inside = hook_says(payload(git_repo, file_path=str(git_repo / "README.md")))
    assert inside.returncode == 0, inside.stderr
    assert "no Python here" not in inside.stderr


def test_no_working_interpreter_stops_the_run(tmp_path):
    """A guard that cannot start used to allow everything. Nothing then says
    whether a call stays inside the repository, so the shim blocks instead."""
    import shutil
    import subprocess

    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("needs a POSIX sh")

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name in ("python3", "python", "py"):
        stub = stub_dir / name
        stub.write_text("#!/bin/sh\nexit 9009\n", encoding="utf-8")
        stub.chmod(0o755)

    import pathlib

    script = pathlib.Path(__file__).resolve().parents[2] / "hooks" / "repo_guard.sh"
    # sh's own directory stays on PATH for dirname; a real python there would
    # defeat the test, so skip rather than pass for the wrong reason.
    path = str(stub_dir) + os.pathsep + os.path.dirname(sh)
    if shutil.which("python3", path=os.path.dirname(sh)) or shutil.which("python", path=os.path.dirname(sh)):
        pytest.skip("a real python sits beside sh")
    result = subprocess.run(
        [sh, str(script)], input="{}", capture_output=True, text=True, env=dict(os.environ, PATH=path)
    )
    assert result.returncode == 2, result.stderr
    assert "no working Python" in result.stderr


def _shim(env_extra, body, path=None):
    """Run hooks/repo_guard.sh the way Claude Code does."""
    import pathlib
    import shutil
    import subprocess

    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("needs a POSIX sh")
    script = pathlib.Path(__file__).resolve().parents[2] / "hooks" / "repo_guard.sh"
    env = dict(os.environ, **env_extra)
    if path is not None:
        env["PATH"] = path
    return subprocess.run([sh, str(script)], input=json.dumps(body), capture_output=True, text=True, env=env)


def _write_python_setting(repo, value):
    config = repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("parallel: 3\npython: {}  # the one agents call\n".format(value), encoding="utf-8")


def test_the_hook_runs_the_configured_python(tmp_path, git_repo, elsewhere):
    """The setting in .goatcode/config.yaml beats the PATH search, so a user
    whose PATH python is wrong can still run guarded."""
    run = make_run(git_repo)
    run.set_phase("execute")

    marker = tmp_path / "used"
    wrapper = tmp_path / "bin" / "mypython"
    wrapper.parent.mkdir()
    real_python = sys.executable.replace(chr(92), "/")
    wrapper.write_text(
        '#!/bin/sh\necho used >> "{}"\nexec "{}" "$@"\n'.format(str(marker).replace(chr(92), "/"), real_python),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    _write_python_setting(git_repo, "'{}'".format(str(wrapper).replace(chr(92), "/")))

    result = _shim({"GOATCODE_REPO": str(git_repo)}, payload(git_repo, file_path=str(elsewhere / "src" / "app.js")))
    assert result.returncode == 2, result.stderr
    assert "outside this run's repository" in result.stderr
    assert marker.exists()


def test_a_configured_python_that_does_not_run_stops_the_run(tmp_path, git_repo):
    """A setting the user wrote is not second-guessed by falling back to PATH:
    that would hide the mistake. Stop, and name the file to fix."""
    _write_python_setting(git_repo, '"{}"'.format((tmp_path / "nope" / "python.exe").as_posix()))

    result = _shim({"GOATCODE_REPO": str(git_repo)}, payload(git_repo, file_path=str(git_repo / "README.md")))
    assert result.returncode == 2
    assert "config.yaml" in result.stderr
    assert "nope" in result.stderr


def test_a_null_python_setting_falls_back_to_the_path_search(git_repo):
    _write_python_setting(git_repo, "null")
    result = _shim({"GOATCODE_REPO": str(git_repo)}, payload(git_repo, file_path=str(git_repo / "README.md")))
    assert result.returncode == 0, result.stderr


# -- powershell is a shell too ----------------------------------------------


def test_a_powershell_command_naming_another_repository_is_refused(git_repo, roots, elsewhere):
    """Executors may use any tool, and used this one unguarded."""
    command = "Get-Content {}".format(elsewhere / "src" / "app.js")
    assert guard.verdict("PowerShell", {"command": command}, roots, cwd=git_repo) is not None
    assert guard.verdict("PowerShell", {"command": "git status"}, roots, cwd=git_repo) is None


# -- asking, and remembering the answer --------------------------------------


def test_a_dependency_cache_is_granted_whole(tmp_path):
    jar = tmp_path / ".m2" / "repository" / "org" / "x" / "x-1.0.jar"
    assert guard.grant_root(jar, home=tmp_path) == guard.real(tmp_path / ".m2")


def test_anything_else_is_granted_by_its_directory(tmp_path, elsewhere):
    assert guard.grant_root(elsewhere / "src" / "app.js", home=tmp_path / "home") == guard.real(elsewhere / "src")
    assert guard.grant_root(elsewhere / "src", home=tmp_path / "home") == guard.real(elsewhere / "src")


def test_one_file_in_the_home_directory_does_not_open_the_home_directory(tmp_path):
    secret = tmp_path / "notes.txt"
    assert guard.grant_root(secret, home=tmp_path) == guard.real(secret)


def test_an_absolute_extra_root_is_admitted(git_repo, elsewhere):
    """What an approval writes: an absolute path, not one relative to the repo."""
    roots = guard.allowed_roots(git_repo, {"guard": {"extra_roots": [str(elsewhere).replace(chr(92), "/")]}})
    assert guard.verdict("Read", read(elsewhere / "src" / "app.js"), roots) is None


def test_a_watched_session_is_asked_and_the_answer_remembered(git_repo, elsewhere, monkeypatch, capsys):
    """Refusing a build's read of its own dependency cache helped nobody.
    The hook asks; the tool running afterwards is the yes."""
    monkeypatch.delenv("GOATCODE_HEADLESS")
    run = make_run(git_repo)
    run.set_phase("execute")
    module = hook()
    target = elsewhere / "src" / "app.js"
    call = dict(payload(git_repo, file_path=str(target)), tool_use_id="toolu_1")

    assert run_main(module, monkeypatch, json.dumps(call)) == 0
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert guard.real(elsewhere / "src") in decision["permissionDecisionReason"]
    assert guard.violations(run.root) == [], "asked is not refused"
    assert runmod.load_config(git_repo)["guard"]["extra_roots"] == [], "nothing is granted by asking"

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(call)))
    assert module.main(["post"]) == 0

    assert not (git_repo / ".goatcode" / guard.PENDING_NAME).exists()
    code, _message = module.decide(call)
    assert code == 0, "approved once, not asked again"
    code, _message = module.decide(payload(git_repo, file_path=str(elsewhere / "other.js")))
    assert code == module.ASK, "and nothing wider than what was approved"


def test_a_denied_request_grants_nothing(git_repo, elsewhere, monkeypatch):
    """A different call finishing must not be read as this one's approval."""
    monkeypatch.delenv("GOATCODE_HEADLESS")
    run = make_run(git_repo)
    run.set_phase("execute")
    module = hook()
    module.decide(dict(payload(git_repo, file_path=str(elsewhere / "src" / "app.js")), tool_use_id="toolu_1"))

    module.approved(dict(payload(git_repo, file_path=str(git_repo / "README.md")), tool_use_id="toolu_2"))
    assert runmod.load_config(git_repo)["guard"]["extra_roots"] == []


def test_the_post_shim_leaves_early_when_nothing_was_asked(tmp_path, git_repo):
    """PostToolUse fires on every call; starting Python for each would be
    felt. No request waiting means no interpreter - proven with none on PATH."""
    import shutil

    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("needs a POSIX sh")
    make_run(git_repo)
    result = _shim_args(["post"], {"GOATCODE_REPO": str(git_repo)}, {}, path=os.path.dirname(sh))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def _shim_args(args, env_extra, body, path=None):
    import pathlib
    import shutil
    import subprocess

    script = pathlib.Path(__file__).resolve().parents[2] / "hooks" / "repo_guard.sh"
    env = dict(os.environ, **env_extra)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [shutil.which("sh"), str(script)] + args, input=json.dumps(body), capture_output=True, text=True, env=env
    )
