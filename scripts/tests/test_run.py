"""Run directory lifecycle, config resolution, preflight and the ledger."""

from __future__ import annotations

import datetime

import pytest

from goatcode import ledger, osenv, run as runmod
from goatcode.run import Run, RunError


def make_run(repo, title="Add magic link login", mode="chat"):
    return Run.create(repo, title, mode)


# -- ids and slugs ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Add magic-link login", "add-magic-link-login"),
        ("  Spaces   everywhere  ", "spaces-everywhere"),
        ("UPPER/lower_MIX", "upper-lower-mix"),
        ("!!!", "run"),
        ("", "run"),
    ],
)
def test_slugify(text, expected):
    assert runmod.slugify(text) == expected


def test_slugify_truncates_without_trailing_hyphen():
    slug = runmod.slugify("a" * 20 + "-" + "b" * 40, limit=21)
    assert len(slug) <= 21
    assert not slug.endswith("-")


def test_run_ids_are_sortable_and_readable():
    early = runmod.new_run_id("feature", now=datetime.datetime(2026, 8, 22, 11, 49, 0))
    late = runmod.new_run_id("feature", now=datetime.datetime(2026, 8, 22, 11, 49, 1))
    assert early == "20260822-114900-feature"
    assert early < late


# -- preflight -------------------------------------------------------------


def test_preflight_accepts_a_clean_repo(git_repo):
    root, problems = runmod.preflight(git_repo)
    assert root == git_repo.resolve()
    assert problems == []


def test_preflight_rejects_a_non_repo(tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    root, problems = runmod.preflight(outside)
    assert root is None
    assert "git init" in problems[0]


def test_preflight_rejects_a_dirty_tree(git_repo):
    (git_repo / "scratch.txt").write_text("wip", encoding="utf-8")
    _root, problems = runmod.preflight(git_repo)
    assert any("not clean" in p for p in problems)
    assert any("scratch.txt" in p for p in problems)


def test_preflight_ignores_the_goatcode_directory(git_repo):
    target = git_repo / ".goatcode" / "runs" / "x"
    target.mkdir(parents=True)
    (target / "state.json").write_text("{}", encoding="utf-8")
    _root, problems = runmod.preflight(git_repo)
    assert problems == []


def test_preflight_rejects_detached_head(git_repo):
    sha = osenv.git_out(["rev-parse", "HEAD"], cwd=git_repo)
    osenv.git(["checkout", "--quiet", "--detach", sha], cwd=git_repo, check=True)
    _root, problems = runmod.preflight(git_repo)
    assert any("detached" in p for p in problems)


def test_preflight_rejects_a_repo_with_no_commits(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    osenv.run(["git", "init", "-q", "-b", "main"], cwd=empty, check=True)
    _root, problems = runmod.preflight(empty)
    assert any("no commits" in p for p in problems)


# -- ignoring run state ----------------------------------------------------


def exclude_file(repo):
    return repo / ".git" / "info" / "exclude"


def test_ensure_ignored_writes_to_info_exclude(git_repo):
    assert runmod.ensure_ignored(git_repo) is True
    assert ".goatcode/" in exclude_file(git_repo).read_text(encoding="utf-8")


def test_ensure_ignored_does_not_touch_the_working_tree(git_repo):
    """The pipeline promises not to dirty the user's branch."""
    runmod.ensure_ignored(git_repo)
    assert osenv.git(["status", "--porcelain"], cwd=git_repo).out == ""
    assert not (git_repo / ".gitignore").exists()


def test_ensure_ignored_is_idempotent(git_repo):
    runmod.ensure_ignored(git_repo)
    assert runmod.ensure_ignored(git_repo) is False
    assert exclude_file(git_repo).read_text(encoding="utf-8").count(".goatcode/") == 1


def test_ensure_ignored_appends_without_eating_the_last_line(git_repo):
    osenv.write_text(exclude_file(git_repo), "*.tmp")
    runmod.ensure_ignored(git_repo)
    lines = exclude_file(git_repo).read_text(encoding="utf-8").splitlines()
    assert lines == ["*.tmp", ".goatcode/"]


def test_ensure_ignored_respects_an_existing_gitignore_entry(git_repo):
    (git_repo / ".gitignore").write_text(".goatcode/\n", encoding="utf-8")
    assert runmod.ensure_ignored(git_repo) is False


def test_run_state_is_invisible_to_git(git_repo):
    runmod.ensure_ignored(git_repo)
    make_run(git_repo)
    assert osenv.git(["status", "--porcelain"], cwd=git_repo).out == ""


# -- config ----------------------------------------------------------------


def test_config_defaults(git_repo):
    config = runmod.load_config(git_repo)
    assert config["parallel"] == 3
    assert config["max_cycles"] == 3
    assert config["models"]["executor"] == "haiku"
    assert config["models"]["planner"] == "opus"


def test_config_overrides_merge_deeply(git_repo):
    target = git_repo / ".goatcode" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("parallel: 5\nmodels:\n  executor: opus\n", encoding="utf-8")
    config = runmod.load_config(git_repo)
    assert config["parallel"] == 5
    assert config["models"]["executor"] == "opus"
    assert config["models"]["verifier"] == "opus"
    assert config["max_cycles"] == 3


def test_config_rejects_a_non_mapping(git_repo):
    target = git_repo / ".goatcode" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(RunError):
        runmod.load_config(git_repo)


# -- run directory ---------------------------------------------------------


def test_create_lays_out_the_run_directory(git_repo):
    run = make_run(git_repo)
    assert run.root.is_dir()
    assert run.state_path.exists()
    assert run.spec_path.exists()
    assert run.ledger_path.exists()
    assert (run.cycle_dir() / "briefs").is_dir()
    assert (run.cycle_dir() / "reports").is_dir()
    assert run.cycle_dir().name == "cycle-1"


def test_create_records_the_base_commit_and_branch(git_repo):
    run = make_run(git_repo)
    assert run.base_commit == osenv.git_out(["rev-parse", "HEAD"], cwd=git_repo)
    assert run.state["base_branch"] == "main"
    assert run.integration_branch == "goatcode/{}/integration".format(run.run_id)


def test_create_uses_a_short_temp_root(git_repo, temp_root):
    run = make_run(git_repo)
    assert run.temp_root.parent == osenv.temp_root()
    assert len(run.temp_root.name) == 8


def test_spec_text_is_stored_verbatim(git_repo):
    run = Run.create(git_repo, "auth", "spec", spec_text="# Spec\n\nbody\n")
    assert run.spec_path.read_text(encoding="utf-8") == "# Spec\n\nbody\n"


def test_chat_mode_writes_a_placeholder_spec(git_repo):
    run = make_run(git_repo, title="Add magic link login")
    body = run.spec_path.read_text(encoding="utf-8")
    assert "# Add magic link login" in body
    assert "## Goal" in body


def test_load_roundtrips_state(git_repo):
    run = make_run(git_repo)
    run.state["phase"] = "execute"
    run.save()
    again = Run.load(git_repo, run.run_id)
    assert again.phase == "execute"
    assert again.base_commit == run.base_commit


def test_load_without_an_id_takes_the_latest(git_repo):
    first = Run.create(git_repo, "one", "chat", now=datetime.datetime(2026, 8, 22, 10, 0, 0))
    second = Run.create(git_repo, "two", "chat", now=datetime.datetime(2026, 8, 22, 11, 0, 0))
    assert runmod.latest_run_id(git_repo) == second.run_id
    assert Run.load(git_repo).run_id != first.run_id


def test_load_reports_a_missing_run(git_repo):
    make_run(git_repo)
    with pytest.raises(RunError):
        Run.load(git_repo, "20200101-000000-nope")


def test_set_phase_rejects_an_unknown_phase(git_repo):
    run = make_run(git_repo)
    with pytest.raises(RunError):
        run.set_phase("dancing")


def test_set_phase_records_the_transition_in_the_ledger(git_repo):
    """Phase timing is the one thing the recorded runs could never answer.

    debug was off in all eleven of them, so no log.txt was ever written and
    there is no record of where a run's hours went. The ledger is not
    optional, so this is.
    """
    run = make_run(git_repo)
    run.set_phase("execute")
    assert any("phase init -> execute" in entry for entry in ledger.entries(run))


def test_set_phase_records_nothing_when_the_phase_is_unchanged(git_repo):
    run = make_run(git_repo)
    run.set_phase("execute")
    before = len(ledger.entries(run))
    run.set_phase("execute")
    assert len(ledger.entries(run)) == before


def test_advance_cycle_creates_the_next_directory(git_repo):
    run = make_run(git_repo)
    assert run.advance_cycle() == 2
    assert (run.root / "cycle-2" / "briefs").is_dir()
    assert run.cycle_dir().name == "cycle-2"


def test_cycles_exhausted_respects_the_cap(git_repo):
    run = make_run(git_repo)
    assert not run.cycles_exhausted()
    run.state["cycle"] = 4
    assert run.cycles_exhausted()


def test_worktree_bookkeeping(git_repo):
    run = make_run(git_repo)
    run.record_worktree("S1", "/tmp/goatcode/abc/S1")
    assert Run.load(git_repo, run.run_id).state["worktrees"]["S1"].endswith("S1")
    run.forget_worktree("S1")
    assert Run.load(git_repo, run.run_id).state["worktrees"] == {}


def test_append_spec_adds_a_clarification_section(git_repo):
    run = make_run(git_repo)
    run.append_spec("## Clarifications (round 1)\n\n- Q: TTL? A: 15 minutes.")
    body = run.spec_path.read_text(encoding="utf-8")
    assert body.count("## Clarifications (round 1)") == 1
    assert body.endswith("15 minutes.\n")


def test_list_runs_is_ordered(git_repo):
    Run.create(git_repo, "one", "chat", now=datetime.datetime(2026, 8, 22, 10, 0, 0))
    Run.create(git_repo, "two", "chat", now=datetime.datetime(2026, 8, 22, 11, 0, 0))
    summaries = runmod.list_runs(git_repo)
    assert [s["run_id"].split("-")[1] for s in summaries] == ["100000", "110000"]


def test_summary_shape(git_repo):
    summary = make_run(git_repo).summary()
    assert summary["phase"] == "init"
    assert summary["cycle"] == 1
    assert len(summary["base_commit"]) == 7


# -- ledger ----------------------------------------------------------------


def test_ledger_appends_in_order(git_repo):
    run = make_run(git_repo)
    ledger.append(run, "slice S1 complete (commits abc..def, review clean)")
    ledger.append(run, "slice S2 complete (commits def..123, review clean)")
    lines = ledger.entries(run)
    assert len(lines) == 2
    assert "S1" in lines[0] and "S2" in lines[1]


def test_ledger_records_the_cycle(git_repo):
    run = make_run(git_repo)
    run.advance_cycle()
    entry = ledger.append(run, "replanned after verifier FAIL")
    assert "cycle 2" in entry


def test_completed_slices_is_the_recovery_map(git_repo):
    run = make_run(git_repo)
    ledger.append(run, "slice S1 complete (commits abc..def)")
    ledger.append(run, "slice S2 blocked, escalating model")
    ledger.append(run, "slice S3 complete (commits 111..222)")
    assert ledger.completed_slices(run) == {"S1", "S3"}


def test_ledger_survives_a_reload(git_repo):
    run = make_run(git_repo)
    ledger.append(run, "slice S1 complete")
    assert ledger.completed_slices(Run.load(git_repo, run.run_id)) == {"S1"}


# -- counters: caps live in code now, not in prose -------------------------


def test_grill_rounds_start_at_zero_and_count_up(git_repo):
    run = make_run(git_repo)
    assert run.grill_rounds == 0
    assert run.bump_grill_round() == 1
    assert run.bump_grill_round() == 2
    assert Run.load(git_repo, run.run_id).grill_rounds == 2


def test_grill_cap_fires_at_the_configured_round(git_repo):
    run = make_run(git_repo)
    for _ in range(2):
        run.bump_grill_round()
    assert not run.grill_exhausted()
    run.bump_grill_round()
    assert run.grill_exhausted()


def test_grill_cap_respects_config(git_repo):
    target = git_repo / ".goatcode" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("max_grill_rounds: 1\n", encoding="utf-8")
    run = make_run(git_repo)
    run.bump_grill_round()
    assert run.grill_exhausted()


def test_plan_fix_attempts_are_capped(git_repo):
    run = make_run(git_repo)
    assert not run.plan_fixes_exhausted()
    run.bump_plan_fix()
    assert not run.plan_fixes_exhausted()
    run.bump_plan_fix()
    assert run.plan_fixes_exhausted()


def test_escalations_are_counted_per_slice(git_repo):
    run = make_run(git_repo)
    assert run.escalations("S1") == 0
    assert run.escalate("S1") == 1
    assert run.escalations("S1") == 1
    assert run.escalations("S2") == 0
    assert Run.load(git_repo, run.run_id).escalations("S1") == 1


# -- the approval gate -----------------------------------------------------


def test_chat_mode_gates_the_first_cycle(git_repo):
    run = Run.create(git_repo, "x", "chat")
    assert run.gate_applies() is True
    assert run.needs_approval() is True
    run.set_approval("approved")
    assert run.needs_approval() is False


def test_spec_mode_does_not_gate(git_repo):
    run = Run.create(git_repo, "x", "spec")
    assert run.gate_applies() is False
    assert run.needs_approval() is False


def test_replan_cycles_do_not_gate(git_repo):
    run = Run.create(git_repo, "x", "chat")
    run.set_approval("approved")
    run.advance_cycle()
    assert run.gate_applies() is False


def test_approval_gate_always(git_repo):
    target = git_repo / ".goatcode" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("approval_gate: always\n", encoding="utf-8")
    run = Run.create(git_repo, "x", "spec")
    assert run.gate_applies() is True
    run.set_approval("approved")
    run.advance_cycle()
    assert run.gate_applies() is True
    assert run.needs_approval() is True, "approval must not carry across cycles"


def test_approval_gate_never(git_repo):
    target = git_repo / ".goatcode" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("approval_gate: never\n", encoding="utf-8")
    run = Run.create(git_repo, "x", "chat")
    assert run.gate_applies() is False


def test_unknown_approval_decision_is_rejected(git_repo):
    run = make_run(git_repo)
    with pytest.raises(RunError):
        run.set_approval("maybe")


def test_advance_cycle_resets_per_cycle_counters(git_repo):
    run = make_run(git_repo)
    run.bump_plan_fix()
    run.set_approval("approved")
    run.bump_grill_round()
    run.advance_cycle()
    assert run.plan_fix_attempts == 0
    assert run.approval is None
    assert run.grill_rounds == 1, "grill rounds are not per-cycle; replans never grill"


def test_new_phases_are_accepted(git_repo):
    run = make_run(git_repo)
    for phase in ("ask", "replan"):
        run.set_phase(phase)
        assert Run.load(git_repo, run.run_id).phase == phase


def test_preflight_names_a_modified_tracked_file_correctly(git_repo):
    """porcelain status is column-aligned; the first entry must not lose its
    leading space and get mis-sliced."""
    (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    _root, problems = runmod.preflight(git_repo)
    assert any("README.md" in p for p in problems)


def test_preflight_names_several_dirty_paths(git_repo):
    (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    (git_repo / "extra.txt").write_text("new\n", encoding="utf-8")
    _root, problems = runmod.preflight(git_repo)
    message = " ".join(problems)
    assert "README.md" in message and "extra.txt" in message


def test_the_same_ledger_line_is_not_appended_twice_in_a_row(git_repo):
    """A recorded run logged 'scribe written' twice, seven seconds apart."""
    run = make_run(git_repo)
    ledger.append(run, "scribe written")
    ledger.append(run, "scribe written")
    assert [e for e in ledger.entries(run) if "scribe written" in e] == [
        e for e in ledger.entries(run) if "scribe written" in e
    ][:1]


def test_a_repeated_line_is_appended_again_once_something_else_intervenes(git_repo):
    run = make_run(git_repo)
    ledger.append(run, "slice S1 done")
    ledger.append(run, "slice S2 done")
    ledger.append(run, "slice S1 done")
    assert len([e for e in ledger.entries(run) if "slice S1 done" in e]) == 2


# -- classification ---------------------------------------------------------


def test_classify_is_a_phase(git_repo):
    run = make_run(git_repo)
    run.set_phase("classify")
    assert run.phase == "classify"


def test_classification_is_on_by_default(git_repo):
    assert make_run(git_repo).wants_classification() is True


def test_classification_can_be_switched_off(git_repo):
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "classifier:\n  enabled: false\n")
    run = Run.create(git_repo, "x", "chat")
    assert run.wants_classification() is False


def test_an_unclassified_run_behaves_as_it_does_today(git_repo):
    """The whole backward-compatibility guarantee, in one assertion."""
    run = make_run(git_repo)
    assert run.classification is None
    assert run.workflow == "PLANNED_DEVELOPMENT"


def test_a_recorded_classification_is_read_back(git_repo):
    run = make_run(git_repo)
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    reloaded = Run.load(git_repo)
    assert reloaded.classification["complexity"] == "SIMPLE"
    assert reloaded.workflow == "DIRECT_DEVELOPMENT"
