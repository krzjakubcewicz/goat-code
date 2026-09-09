"""The repository boundary a live run is confined to.

The hook shell itself (``hooks/repo_guard.py``) is the one piece of goat-code
a test cannot drive end to end - it needs a real Claude Code session. So the
decision lives in ``guard`` and is tested here, and the shell around it is
kept as small as it can be.
"""

from __future__ import annotations

import json
import os

import pytest

from goatcode import guard, run as runmod
from tests.conftest import make_run


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
