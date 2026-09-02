"""Enforcing test-first from the slice's own git history.

Every executor is instructed to work test-first. These tests are what makes
the instruction cost something: a DONE whose history shows implementation
landing before any test is refused.
"""

from __future__ import annotations

import copy

import pytest

from codag import miniyaml, osenv, report, tasks, tdd, worktree
from codag.report import ReportError
from codag.run import Run

PLAN = {
    "version": 1,
    "run_id": "placeholder",
    "cycle": 1,
    "goal": "Users sign in with a magic link.",
    "slices": [
        {
            "id": "S1",
            "title": "Token store",
            "depends_on": [],
            "owns": ["src/auth/**"],
            "acceptance": [{"id": "A1", "text": "consumable once"}],
            "tests": [{"path": "tests/auth.test.js"}],
            "status": "pending",
        }
    ],
}

SLICE = PLAN["slices"][0]


@pytest.fixture
def run(git_repo):
    return Run.create(git_repo, "tdd", "chat")


@pytest.fixture
def plan(run):
    doc = copy.deepcopy(PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    return doc


@pytest.fixture
def slice_worktree(run, plan):
    path, _branch, _setup = worktree.create(run, "S1", setup=False)
    tasks.set_field(run.tasks_path, "S1", "worktree", str(path))
    tasks.record_commits(run.tasks_path, "S1", base=run.base_commit)
    return path


def commit(path, files, message):
    for relpath, text in files.items():
        target = path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    osenv.git(["add", "-A"], cwd=path, check=True)
    osenv.git(["commit", "-qm", message], cwd=path, check=True)
    return osenv.git_out(["rev-parse", "HEAD"], cwd=path)


# -- classifying paths -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/auth.test.js",
        "src/auth/token.test.ts",
        "src/auth/token.spec.ts",
        "src/auth/token_test.go",
        "src/auth/token-test.js",
        "tests/test_tokens.py",
        "test/helpers.js",
        "spec/models/user_spec.rb",
        "__tests__/token.js",
        "e2e/login.js",
    ],
)
def test_recognises_test_paths(path):
    assert tdd.is_test_path(path, SLICE) is True


@pytest.mark.parametrize(
    "path",
    ["src/auth/token.ts", "src/auth/index.js", "src/latest/thing.py", "lib/attestation.js"],
)
def test_does_not_mistake_source_for_tests(path):
    assert tdd.is_test_path(path, SLICE) is False


def test_a_declared_test_path_counts_however_it_is_named():
    item = {"tests": [{"path": "src/auth/checks.js"}], "owns": ["src/auth/**"]}
    assert tdd.is_test_path("src/auth/checks.js", item) is True


def test_the_stacks_test_dirs_are_honoured():
    profile = {"test_dirs": ["t"]}
    assert tdd.is_test_path("t/thing.js", SLICE, profile) is True
    assert tdd.is_test_path("t/thing.js", SLICE) is False


def test_windows_separators_are_handled():
    assert tdd.is_test_path("tests\\auth.test.js", SLICE) is True


# -- classifying implementation -------------------------------------------


def test_owned_source_is_implementation():
    assert tdd.is_implementation("src/auth/token.ts", SLICE) is True


def test_a_test_is_never_implementation():
    assert tdd.is_implementation("tests/auth.test.js", SLICE) is False


def test_files_outside_the_slice_are_not_its_implementation():
    assert tdd.is_implementation("src/mail/send.ts", SLICE) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/auth/fixtures.json",
        "src/auth/README.md",
        "src/auth/config.yaml",
        "src/auth/schema.sql",
    ],
)
def test_scaffolding_and_data_do_not_count_as_implementation(path):
    """A slice may honestly open with a fixture or a config file."""
    assert tdd.is_implementation(path, SLICE) is False


# -- reading the history ---------------------------------------------------


def test_test_first_history_is_clean(run, plan, slice_worktree):
    commit(slice_worktree, {"tests/auth.test.js": "// red\n"}, "add failing token test")
    head = commit(slice_worktree, {"src/auth/token.js": "module.exports = 1;\n"}, "make it pass")
    assert tdd.violations(slice_worktree, SLICE, run.base_commit, head) == []


def test_a_squashed_red_green_pair_is_clean(run, plan, slice_worktree):
    """Test and implementation in one commit is what a squashed pair looks like."""
    head = commit(
        slice_worktree,
        {"tests/auth.test.js": "// t\n", "src/auth/token.js": "x\n"},
        "add token with test",
    )
    assert tdd.violations(slice_worktree, SLICE, run.base_commit, head) == []


def test_implementation_first_is_a_violation(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "module.exports = 1;\n"}, "add token store")
    head = commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "add a test for it")

    found = tdd.violations(slice_worktree, SLICE, run.base_commit, head)
    assert len(found) == 1
    assert found[0]["subject"] == "add token store"
    assert found[0]["files"] == ["src/auth/token.js"]


def test_only_the_first_offence_is_reported(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/a.js": "a\n"}, "first")
    commit(slice_worktree, {"src/auth/b.js": "b\n"}, "second")
    head = commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "tests at last")
    found = tdd.violations(slice_worktree, SLICE, run.base_commit, head)
    assert len(found) == 1
    assert found[0]["subject"] == "first"


def test_opening_with_scaffolding_is_allowed(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/fixtures.json": "{}\n"}, "add fixtures")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "add failing test")
    head = commit(slice_worktree, {"src/auth/token.js": "x\n"}, "make it pass")
    assert tdd.violations(slice_worktree, SLICE, run.base_commit, head) == []


def test_touching_another_slices_files_is_not_this_slices_implementation(run, plan, slice_worktree):
    commit(slice_worktree, {"src/mail/send.js": "x\n"}, "append to a shared file")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "add failing test")
    head = commit(slice_worktree, {"src/auth/token.js": "x\n"}, "make it pass")
    assert tdd.violations(slice_worktree, SLICE, run.base_commit, head) == []


def test_later_refactor_commits_need_no_test(run, plan, slice_worktree):
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "add failing test")
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "make it pass")
    head = commit(slice_worktree, {"src/auth/token.js": "tidied\n"}, "refactor while green")
    assert tdd.violations(slice_worktree, SLICE, run.base_commit, head) == []


def test_an_empty_range_is_clean(run, plan, slice_worktree):
    assert tdd.violations(slice_worktree, SLICE, run.base_commit, run.base_commit) == []


def test_a_missing_base_is_clean(run, plan, slice_worktree):
    assert tdd.violations(slice_worktree, SLICE, None, "HEAD") == []


def test_check_renders_a_readable_finding(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "add token store")
    head = commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "test")
    item = dict(SLICE, commits={"base": run.base_commit, "head": head})

    messages = tdd.check(slice_worktree, item)
    assert len(messages) == 1
    assert "before any test" in messages[0]
    assert "src/auth/token.js" in messages[0]


# -- the gate in report ----------------------------------------------------


def test_report_refuses_a_done_that_was_not_test_first(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "add token store")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "add a test afterwards")

    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE", tests="1 passed")

    message = str(excinfo.value)
    assert "before any test" in message
    assert "add token store" in message
    assert tasks.get(tasks.load(run.tasks_path), "S1")["status"] == "pending"


def test_the_refusal_points_at_the_honest_remedy(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "impl")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "test")

    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE")
    message = str(excinfo.value)
    assert "cannot be un-written" in message
    assert "DONE_WITH_CONCERNS" in message


def test_report_accepts_a_done_that_was_test_first(run, plan, slice_worktree):
    commit(slice_worktree, {"tests/auth.test.js": "// red\n"}, "add failing test")
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "make it pass")

    result = report.record_slice(
        run, "S1", "DONE", tests="1 passed", evidence={"A1": "tests/auth.test.js:1"}
    )
    assert result["slice_status"] == "done"


def test_done_with_concerns_is_the_escape_hatch(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "add token store")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "add a test afterwards")

    result = report.record_slice(run, "S1", "DONE_WITH_CONCERNS", concerns="tests came second")
    assert result["slice_status"] == "done"


def test_the_escape_hatch_records_the_violation_anyway(run, plan, slice_worktree):
    """It cannot be hidden behind a vague concern string."""
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "add token store")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "test")

    report.record_slice(run, "S1", "DONE_WITH_CONCERNS", concerns="file is getting large")
    recorded = tasks.get(tasks.load(run.tasks_path), "S1")["report"]["concerns"]
    assert "file is getting large" in recorded
    assert "before any test" in recorded


def test_concerns_are_recorded_even_when_the_agent_gives_none(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "impl")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "test")

    report.record_slice(run, "S1", "DONE_WITH_CONCERNS")
    recorded = tasks.get(tasks.load(run.tasks_path), "S1")["report"]["concerns"]
    assert "before any test" in recorded


def test_blocked_is_unaffected(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "impl")
    result = report.record_slice(run, "S1", "BLOCKED", reason="stuck")
    assert result["slice_status"] == "blocked"


def test_force_bypasses_the_check(run, plan, slice_worktree):
    commit(slice_worktree, {"src/auth/token.js": "x\n"}, "impl")
    commit(slice_worktree, {"tests/auth.test.js": "// t\n"}, "test")
    assert report.record_slice(run, "S1", "DONE", force=True)["slice_status"] == "done"


def test_enforcement_can_be_switched_off(git_repo):
    config = git_repo / ".codag" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("enforce_tdd: false\n", encoding="utf-8")

    run = Run.create(git_repo, "tdd off", "chat")
    doc = copy.deepcopy(PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    path, _b, _s = worktree.create(run, "S1", setup=False)
    tasks.set_field(run.tasks_path, "S1", "worktree", str(path))
    tasks.record_commits(run.tasks_path, "S1", base=run.base_commit)

    commit(path, {"src/auth/token.js": "x\n"}, "impl first")
    commit(path, {"tests/auth.test.js": "// t\n"}, "test second")

    assert report.record_slice(
        run, "S1", "DONE", evidence={"A1": "tests/auth.test.js:1"}
    )["slice_status"] == "done"


def test_the_other_done_checks_still_apply_with_enforcement_off(git_repo):
    config = git_repo / ".codag" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("enforce_tdd: false\n", encoding="utf-8")

    run = Run.create(git_repo, "tdd off", "chat")
    doc = copy.deepcopy(PLAN)
    doc["run_id"] = run.run_id
    miniyaml.dump(doc, run.tasks_path)
    path, _b, _s = worktree.create(run, "S1", setup=False)
    tasks.set_field(run.tasks_path, "S1", "worktree", str(path))
    tasks.record_commits(run.tasks_path, "S1", base=run.base_commit)
    commit(path, {"src/auth/token.js": "x\n"}, "impl")

    with pytest.raises(ReportError) as excinfo:
        report.record_slice(run, "S1", "DONE")
    assert "tests/auth.test.js" in str(excinfo.value)
