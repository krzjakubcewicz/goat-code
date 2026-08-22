"""The CLI, including a full pipeline run with no LLM in the loop.

test_end_to_end_pipeline is the one that matters: it proves the
deterministic spine works on this OS regardless of model behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from codag import miniyaml, osenv, worktree  # noqa: E402
from codag.run import Run  # noqa: E402


def _load_cli():
    """Load scripts/codag.py by path.

    ``import codag`` finds the package next to it, which is what the entry
    point itself imports - so the script has to be loaded explicitly.
    """
    spec = importlib.util.spec_from_file_location("codag_cli", SCRIPTS / "codag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def invoke(capsys, *argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def invoke_json(capsys, *argv):
    code, out, err = invoke(capsys, *argv, "--json")
    return code, (json.loads(out) if out.strip() else None), err


@pytest.fixture
def node_repo(git_repo):
    """A repo with a detectable stack whose gates are fast and scriptable."""
    (git_repo / "package.json").write_text(
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
    (git_repo / "package-lock.json").write_text("{}", encoding="utf-8")
    (git_repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    scripts = git_repo / "scripts"
    scripts.mkdir()
    (scripts / "test.js").write_text("process.exit(0);\n", encoding="utf-8")
    osenv.git(["add", "-A"], cwd=git_repo, check=True)
    osenv.git(["commit", "-qm", "fixture project"], cwd=git_repo, check=True)
    return git_repo


def plan_for(run, slices):
    doc = {
        "version": 1,
        "run_id": run.run_id,
        "cycle": 1,
        "goal": "Land three slices in parallel.",
        "global_constraints": ["No new runtime dependencies"],
        "slices": slices,
    }
    miniyaml.dump(doc, run.tasks_path)
    return doc


def slice_spec(slice_id, owns, depends_on=None):
    return {
        "id": slice_id,
        "title": "Slice {}".format(slice_id),
        "intent": "Deliver {}".format(slice_id),
        "depends_on": depends_on or [],
        "owns": [owns],
        "acceptance": [{"id": "A1", "text": "{} exists".format(owns)}],
        "tests": ["tests/{}.test.js".format(slice_id)],
        "status": "pending",
    }


# -- init ------------------------------------------------------------------


def test_init_creates_a_run_and_detects_the_stack(capsys, node_repo):
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "add magic link", "--no-baseline"
    )
    assert code == 0
    assert payload["run_id"].endswith("add-magic-link")
    assert "typescript" in payload["stack_summary"]
    assert pathlib.Path(payload["spec"]).exists()
    assert pathlib.Path(payload["stack"]).exists()
    assert payload["base_commit"]


def test_init_hides_state_from_git_without_dirtying_the_tree(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    assert ".codag/" in (node_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_init_refuses_a_dirty_tree(capsys, node_repo):
    (node_repo / "wip.txt").write_text("x", encoding="utf-8")
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    assert code == cli.EXIT_USAGE
    assert "not clean" in err


def test_init_force_overrides_preflight(capsys, node_repo):
    (node_repo / "wip.txt").write_text("x", encoding="utf-8")
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline", "--force"
    )
    assert code == 0
    assert any("not clean" in w for w in payload["warnings"])


def test_init_outside_a_repo_is_a_clear_error(capsys, tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    code, _out, err = invoke(capsys, "--repo", str(outside), "init", "--prompt", "x")
    assert code == cli.EXIT_USAGE
    assert "git init" in err


def test_init_requires_a_prompt_or_spec(capsys, node_repo):
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "init", "--no-baseline")
    assert code == cli.EXIT_USAGE
    assert "--spec" in err


def test_init_from_a_spec_file_takes_its_title(capsys, node_repo, tmp_path):
    spec = tmp_path / "auth.md"
    spec.write_text("# Magic link sign in\n\n## Goal\n\nUsers sign in.\n", encoding="utf-8")
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "init", "--spec", str(spec), "--no-baseline"
    )
    assert code == 0
    assert payload["run_id"].endswith("magic-link-sign-in")
    assert "Users sign in." in pathlib.Path(payload["spec"]).read_text(encoding="utf-8")


def test_init_missing_spec_file_is_an_error(capsys, node_repo):
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "init", "--spec", "nope.md")
    assert code == cli.EXIT_USAGE
    assert "no spec file" in err


def test_init_captures_a_baseline_in_the_integration_worktree(capsys, node_repo):
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "init", "--prompt", "x")
    assert code == 0
    assert payload["integration_worktree"]
    assert pathlib.Path(payload["integration_worktree"]).exists()
    run = Run.load(node_repo)
    assert run.baseline_path.exists()


# -- plan validation -------------------------------------------------------


def test_plan_validate_passes_a_good_plan(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**")])
    code, out, _err = invoke(capsys, "--repo", str(node_repo), "plan", "validate")
    assert code == 0
    assert "OK" in out


def test_plan_validate_reports_an_ownership_collision(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/a/**")])
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "plan", "validate")
    assert code == cli.EXIT_FAIL
    assert any("same wave and both own" in e for e in payload["errors"])


def test_plan_validate_reports_bad_yaml_with_a_position(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    run.tasks_path.write_text("version: 1\nslices: &anchor\n", encoding="utf-8")
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "plan", "validate")
    assert code == cli.EXIT_FAIL
    assert "line 2" in payload["errors"][0]


def test_plan_show_renders_waves(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**", ["S1"])])
    _code, out, _err = invoke(capsys, "--repo", str(node_repo), "plan", "show")
    assert "wave 1" in out and "wave 2" in out


# -- waves and tasks -------------------------------------------------------


def test_wave_next_respects_the_parallel_cap(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S{}".format(i), "src/m{}/**".format(i)) for i in range(5)])
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "wave", "next")
    assert len(payload["ready"]) == 3
    assert len(payload["deferred"]) == 2


def test_wave_next_after_completion(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**", ["S1"])])
    invoke(capsys, "--repo", str(node_repo), "task", "status", "S1", "done")
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "wave", "next")
    assert payload["ready"] == ["S2"]


def test_task_set_and_show(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "task", "set", "S1", "model", "opus")
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "task", "show", "S1")
    assert payload["model"] == "opus"


def test_task_set_coerces_types(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "task", "set", "S1", "owns", '["src/z/**"]', "--type", "json")
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "task", "show", "S1")
    assert payload["owns"] == ["src/z/**"]


def test_task_on_an_unknown_slice_is_a_clear_error(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "task", "status", "S9", "done")
    assert code == cli.EXIT_USAGE
    assert "S9" in err


# -- worktrees and briefs --------------------------------------------------


def test_worktree_create_records_paths_in_the_plan(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**")])
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "worktree", "create", "S1", "S2", "--no-setup"
    )
    assert code == 0
    assert {c["slice"] for c in payload["created"]} == {"S1", "S2"}
    _code, item, _e = invoke_json(capsys, "--repo", str(node_repo), "task", "show", "S1")
    assert item["worktree"].endswith("S1")
    assert item["branch"].endswith("/S1")
    assert item["commits"]["base"] == run.base_commit


def test_brief_writes_one_file_per_slice(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "brief", "S1")
    text = pathlib.Path(payload["briefs"][0]).read_text(encoding="utf-8")
    assert "# Slice S1" in text
    assert "No new runtime dependencies" in text


# -- lifecycle -------------------------------------------------------------


def test_status_summarises_the_run(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    _code, out, _err = invoke(capsys, "--repo", str(node_repo), "status")
    assert run.run_id in out
    assert "phase:  grill" in out


def test_status_all_lists_runs(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "one", "--no-baseline")
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "two", "--no-baseline")
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "status", "--all")
    assert len(payload["runs"]) == 2


def test_ledger_appends_and_reads(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    invoke(capsys, "--repo", str(node_repo), "ledger", "slice S1 complete (commits a..b)")
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "ledger")
    assert payload["completed"] == ["S1"]


def test_resume_reports_what_to_trust(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**")])
    invoke(capsys, "--repo", str(node_repo), "task", "status", "S1", "done")
    invoke(capsys, "--repo", str(node_repo), "ledger", "slice S1 complete")
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "resume")
    assert payload["completed_slices"] == ["S1"]
    assert payload["ready"] == ["S2"]


def test_cycle_advances_and_carries_finished_work(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**")])
    invoke(capsys, "--repo", str(node_repo), "task", "status", "S1", "done")
    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "cycle")
    assert payload["cycle"] == 2
    assert payload["carried"] == ["S1"]
    assert pathlib.Path(payload["cycle_dir"]).name == "cycle-2"
    _code, item, _e = invoke_json(capsys, "--repo", str(node_repo), "task", "show", "S1")
    assert item["status"] == "carried"


def test_cycle_cap_stops_the_loop(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    run.state["cycle"] = 4
    run.save()
    code, _out, err = invoke(capsys, "--repo", str(node_repo), "cycle")
    assert code == cli.EXIT_USAGE
    assert "cycle cap reached" in err


def test_abort_cleans_up(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "--no-setup")
    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "abort")
    assert code == 0
    assert "S1" in payload["removed"]
    assert not worktree.path_for(run, "S1").exists()
    assert Run.load(node_repo, run.run_id).phase == "aborted"


# -- the full deterministic pipeline ---------------------------------------


def test_end_to_end_pipeline(capsys, node_repo):
    """init -> plan -> 3 worktrees -> commits -> merge -> gates -> finish.

    No LLM anywhere: this is the spine, and it must hold on every OS.
    """
    # A pre-existing failing gate, so we also prove inherited breakage does
    # not block the run.
    (node_repo / "scripts" / "lint.js").write_text("process.exit(1);\n", encoding="utf-8")
    package = json.loads((node_repo / "package.json").read_text(encoding="utf-8"))
    package["scripts"]["lint"] = "node scripts/lint.js"
    (node_repo / "package.json").write_text(json.dumps(package, indent=2), encoding="utf-8")
    osenv.git(["add", "-A"], cwd=node_repo, check=True)
    osenv.git(["commit", "-qm", "add a failing lint"], cwd=node_repo, check=True)

    code, init_payload, _err = invoke_json(capsys, "--repo", str(node_repo), "init", "--prompt", "three slices")
    assert code == 0
    run = Run.load(node_repo)
    baseline = osenv.read_json(run.baseline_path)
    assert baseline["gates"]["lint"]["status"] == "fail"
    assert baseline["gates"]["test"]["status"] == "pass"

    plan_for(
        run,
        [
            slice_spec("S1", "src/auth/**"),
            slice_spec("S2", "src/mail/**"),
            slice_spec("S3", "src/routes/**", ["S1", "S2"]),
        ],
    )
    assert invoke(capsys, "--repo", str(node_repo), "plan", "validate")[0] == 0

    _code, wave1, _e = invoke_json(capsys, "--repo", str(node_repo), "wave", "next")
    assert wave1["ready"] == ["S1", "S2"]

    for wave in (["S1", "S2"], ["S3"]):
        invoke(capsys, "--repo", str(node_repo), "worktree", "create", *wave, "--no-setup")
        invoke(capsys, "--repo", str(node_repo), "brief", *wave)
        for slice_id in wave:
            path = worktree.path_for(run, slice_id)
            assert run.brief_path(slice_id).exists()
            target = path / "src" / slice_id.lower() / "index.js"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("module.exports = '{}';\n".format(slice_id), encoding="utf-8")
            osenv.git(["add", "-A"], cwd=path, check=True)
            osenv.git(["commit", "-qm", "{}: add module".format(slice_id)], cwd=path, check=True)
            invoke(
                capsys,
                "--repo",
                str(node_repo),
                "task",
                "commits",
                slice_id,
                "--head",
                worktree.head_commit(path),
            )
            invoke(capsys, "--repo", str(node_repo), "task", "status", slice_id, "done")
            invoke(capsys, "--repo", str(node_repo), "ledger", "slice {} complete".format(slice_id))

    code, merge_payload, _err = invoke_json(capsys, "--repo", str(node_repo), "merge")
    assert code == 0
    assert merge_payload["status"] == "clean"
    assert merge_payload["merged"] == ["S1", "S2", "S3"]

    integration = pathlib.Path(merge_payload["worktree"])
    for slice_id in ("s1", "s2", "s3"):
        assert (integration / "src" / slice_id / "index.js").exists()

    code, verify_payload, _err = invoke_json(capsys, "--repo", str(node_repo), "verify-package")
    assert code == 0
    gates_report = osenv.read_json(verify_payload["gates"])
    assert "lint" in gates_report["pre_existing"], "inherited lint failure must not be blamed on the run"
    assert gates_report["regressions"] == []
    assert verify_payload["gates_blocking"] == []
    assert len(verify_payload["criteria"]) == 3
    review = pathlib.Path(verify_payload["review"]).read_text(encoding="utf-8")
    assert "src/s1/index.js" in review

    code, finish_payload, out = invoke_json(capsys, "--repo", str(node_repo), "finish")
    assert code == 0
    assert finish_payload["integration_branch"] == run.integration_branch
    for slice_id in ("S1", "S2", "S3"):
        assert not worktree.path_for(run, slice_id).exists()

    # The user's branch is untouched.
    assert osenv.git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=node_repo) == "main"
    assert osenv.git_out(["rev-parse", "HEAD"], cwd=node_repo) == run.base_commit
    assert osenv.git(["status", "--porcelain"], cwd=node_repo).out == ""

    # But the work is on the integration branch, ready to review.
    listed = osenv.git_out(["ls-tree", "-r", "--name-only", run.integration_branch], cwd=node_repo)
    assert "src/s1/index.js" in listed and "src/s3/index.js" in listed


def test_end_to_end_conflict_and_replan_cycle(capsys, node_repo):
    """A conflict stops the merge; after resolution the run continues."""
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "conflicting", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**")])

    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "S2", "--no-setup")
    for slice_id, text in (("S1", "auth"), ("S2", "mail")):
        path = worktree.path_for(run, slice_id)
        (path / "registry.js").write_text("module.exports = ['{}'];\n".format(text), encoding="utf-8")
        osenv.git(["add", "-A"], cwd=path, check=True)
        osenv.git(["commit", "-qm", "{}: registry".format(slice_id)], cwd=path, check=True)
        invoke(capsys, "--repo", str(node_repo), "task", "status", slice_id, "done")

    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "merge")
    assert code == cli.EXIT_FAIL
    assert payload["status"] == "conflict"
    assert payload["conflicts"] == ["registry.js"]
    assert "registry.js" in pathlib.Path(payload["report"]).read_text(encoding="utf-8")

    integration = pathlib.Path(payload["worktree"])
    (integration / "registry.js").write_text("module.exports = ['auth', 'mail'];\n", encoding="utf-8")

    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "merge", "--continue")
    assert code == 0
    assert payload["status"] == "clean"
    assert payload["merged"] == ["S1", "S2"]


def test_merge_continue_refuses_unresolved_markers(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "conflicting", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "S2", "--no-setup")
    for slice_id, text in (("S1", "auth"), ("S2", "mail")):
        path = worktree.path_for(run, slice_id)
        (path / "registry.js").write_text("module.exports = ['{}'];\n".format(text), encoding="utf-8")
        osenv.git(["add", "-A"], cwd=path, check=True)
        osenv.git(["commit", "-qm", "registry"], cwd=path, check=True)
        invoke(capsys, "--repo", str(node_repo), "task", "status", slice_id, "done")
    invoke(capsys, "--repo", str(node_repo), "merge")

    code, _out, err = invoke(capsys, "--repo", str(node_repo), "merge", "--continue")
    assert code == cli.EXIT_FAIL
    assert "conflict markers" in err


def test_gates_run_fails_on_a_regression(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x")
    run = Run.load(node_repo)
    integration = worktree.integration_path(run)
    (integration / "scripts" / "test.js").write_text("process.exit(1);\n", encoding="utf-8")

    code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "gates", "run")
    assert code == cli.EXIT_FAIL
    assert payload["regressions"] == ["test"]


def test_wave_next_names_the_model_for_each_slice(capsys, node_repo):
    """The orchestrator dispatches from this map, so it must be present."""
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    slices = [slice_spec("S1", "src/a/**"), slice_spec("S2", "src/b/**")]
    slices[1]["model"] = "opus"
    plan_for(run, slices)

    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "wave", "next")
    assert payload["models"] == {"S1": "haiku", "S2": "opus"}
    assert payload["escalated_model"] == "sonnet"


def test_wave_next_falls_back_to_the_configured_executor_model(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    run.state["config"]["models"]["executor"] = "sonnet"
    run.save()
    plan_for(run, [slice_spec("S1", "src/a/**")])

    _code, payload, _err = invoke_json(capsys, "--repo", str(node_repo), "wave", "next")
    assert payload["models"] == {"S1": "sonnet"}


def test_wave_next_text_output_shows_the_model(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    _code, out, _err = invoke(capsys, "--repo", str(node_repo), "wave", "next")
    assert out.strip() == "S1 (haiku)"


# -- the CLI must work from inside an executor's worktree ------------------


def test_cli_reaches_the_run_from_inside_a_slice_worktree(capsys, node_repo):
    """Executors self-report from their worktree; .codag/ lives in the main repo."""
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "--no-setup")

    inside = worktree.path_for(run, "S1")
    code, payload, _err = invoke_json(capsys, "--repo", str(inside), "status")
    assert code == 0
    assert payload["run_id"] == run.run_id


def test_task_status_can_be_set_from_inside_a_worktree(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "--no-setup")

    inside = worktree.path_for(run, "S1")
    code, _out, _err = invoke(capsys, "--repo", str(inside), "task", "status", "S1", "done")
    assert code == 0
    assert Run.load(node_repo).run_id == run.run_id
    _code, item, _e = invoke_json(capsys, "--repo", str(node_repo), "task", "show", "S1")
    assert item["status"] == "done"


def test_init_refuses_to_start_from_a_linked_worktree(capsys, node_repo):
    invoke(capsys, "--repo", str(node_repo), "init", "--prompt", "x", "--no-baseline")
    run = Run.load(node_repo)
    plan_for(run, [slice_spec("S1", "src/a/**")])
    invoke(capsys, "--repo", str(node_repo), "worktree", "create", "S1", "--no-setup")

    inside = worktree.path_for(run, "S1")
    code, _out, err = invoke(capsys, "--repo", str(inside), "init", "--prompt", "y", "--no-baseline")
    assert code == cli.EXIT_USAGE
    assert "linked worktree" in err
