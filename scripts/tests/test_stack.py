"""Stack detection across fixture repos.

These assertions are the contract the executor agents rely on: if the
detected test command is wrong, every gate downstream is wrong.
"""

from __future__ import annotations

import json

import pytest

from codag import osenv, stack


def write(root, relpath, text):
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def package_json(**kwargs):
    return json.dumps(kwargs, indent=2)


# -- javascript / typescript ----------------------------------------------


def test_detects_nextjs_with_pnpm_and_vitest(tmp_path):
    write(
        tmp_path,
        "package.json",
        package_json(
            name="app",
            scripts={"build": "next build", "test": "vitest run", "lint": "next lint"},
            dependencies={"next": "15.0.0", "react": "19.0.0"},
            devDependencies={"typescript": "5.6.0", "vitest": "2.0.0"},
        ),
    )
    write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    write(tmp_path, "tsconfig.json", "{}")
    (tmp_path / "src").mkdir()

    profile = stack.detect(tmp_path)
    assert profile["languages"][0] == "typescript"
    assert "nextjs" in profile["frameworks"]
    assert profile["package_manager"] == "pnpm"
    assert profile["test_framework"] == "vitest"
    assert profile["commands"]["setup"] == ["pnpm", "install"]
    assert profile["commands"]["build"] == ["pnpm", "run", "build"]
    assert profile["commands"]["test"] == ["pnpm", "run", "test"]
    assert profile["commands"]["lint"] == ["pnpm", "run", "lint"]
    assert profile["source_dirs"] == ["src"]
    assert "engineering-skills:senior-frontend" in profile["specialist_skills"]


def test_typecheck_falls_back_to_tsc_when_no_script(tmp_path):
    write(tmp_path, "package.json", package_json(devDependencies={"typescript": "5.6.0"}))
    write(tmp_path, "package-lock.json", "{}")
    write(tmp_path, "tsconfig.json", "{}")
    profile = stack.detect(tmp_path)
    assert profile["package_manager"] == "npm"
    assert profile["commands"]["typecheck"] == ["npx", "--no-install", "tsc", "--noEmit"]


def test_prefers_the_typecheck_script_when_present(tmp_path):
    write(
        tmp_path,
        "package.json",
        package_json(scripts={"type-check": "tsc --noEmit"}, devDependencies={"typescript": "5"}),
    )
    write(tmp_path, "yarn.lock", "")
    profile = stack.detect(tmp_path)
    assert profile["package_manager"] == "yarn"
    assert profile["commands"]["typecheck"] == ["yarn", "run", "type-check"]


def test_express_backend_is_a_backend_specialist(tmp_path):
    write(tmp_path, "package.json", package_json(dependencies={"express": "4"}, scripts={"test": "jest"}))
    write(tmp_path, "package-lock.json", "{}")
    profile = stack.detect(tmp_path)
    assert "express" in profile["frameworks"]
    assert profile["specialist_skills"][0] == "engineering-skills:senior-backend"


def test_missing_lockfile_is_noted(tmp_path):
    write(tmp_path, "package.json", package_json(name="x"))
    profile = stack.detect(tmp_path)
    assert any("no lockfile" in note for note in profile["notes"])
    assert profile["package_manager"] == "npm"


def test_package_manager_field_wins_when_no_lockfile(tmp_path):
    write(tmp_path, "package.json", package_json(packageManager="pnpm@9.0.0"))
    profile = stack.detect(tmp_path)
    assert profile["package_manager"] == "pnpm"


@pytest.mark.parametrize(
    "marker,content,kind",
    [
        ("pnpm-workspace.yaml", "packages:\n  - apps/*\n", "pnpm-workspaces"),
        ("turbo.json", "{}", "turborepo"),
        ("nx.json", "{}", "nx"),
    ],
)
def test_detects_js_monorepos(tmp_path, marker, content, kind):
    write(tmp_path, "package.json", package_json(name="root"))
    write(tmp_path, marker, content)
    profile = stack.detect(tmp_path)
    assert profile["monorepo"]["kind"] == kind


def test_npm_workspaces_are_a_monorepo(tmp_path):
    write(tmp_path, "package.json", package_json(name="root", workspaces=["packages/*"]))
    write(tmp_path, "package-lock.json", "{}")
    assert stack.detect(tmp_path)["monorepo"]["kind"] == "npm-workspaces"


# -- python ----------------------------------------------------------------


def test_detects_poetry_fastapi_pytest(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        "\n".join(
            [
                "[tool.poetry]",
                'name = "api"',
                "[tool.poetry.dependencies]",
                'fastapi = "^0.115"',
                "[tool.poetry.group.dev.dependencies]",
                'pytest = "^8"',
                'mypy = "^1"',
                "[tool.ruff]",
                "line-length = 100",
            ]
        ),
    )
    write(tmp_path, "poetry.lock", "")
    (tmp_path / "tests").mkdir()

    profile = stack.detect(tmp_path)
    assert profile["languages"] == ["python"]
    assert "fastapi" in profile["frameworks"]
    assert profile["package_manager"] == "poetry"
    assert profile["commands"]["setup"] == ["poetry", "install"]
    assert profile["commands"]["test"] == ["poetry", "run", "pytest"]
    assert profile["commands"]["lint"] == ["poetry", "run", "ruff", "check", "."]
    assert profile["commands"]["typecheck"] == ["poetry", "run", "mypy", "."]
    assert profile["test_dirs"] == ["tests"]
    assert profile["specialist_skills"] == ["engineering-skills:senior-backend"]


def test_detects_plain_pip_django(tmp_path):
    write(tmp_path, "requirements.txt", "Django==5.1\npytest==8.3\n")
    profile = stack.detect(tmp_path)
    assert profile["package_manager"] == "pip"
    assert "django" in profile["frameworks"]
    assert profile["commands"]["setup"][:4] == ["python", "-m", "pip", "install"]
    assert profile["commands"]["test"] == ["pytest"]


def test_detects_uv(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    write(tmp_path, "uv.lock", "")
    profile = stack.detect(tmp_path)
    assert profile["package_manager"] == "uv"
    assert profile["commands"]["setup"] == ["uv", "sync"]
    assert profile["commands"]["test"] == ["uv", "run", "pytest"]


# -- other ecosystems ------------------------------------------------------


def test_detects_go(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n\ngo 1.23\n")
    profile = stack.detect(tmp_path)
    assert profile["languages"] == ["go"]
    assert profile["commands"]["build"] == ["go", "build", "./..."]
    assert profile["commands"]["test"] == ["go", "test", "./..."]
    assert profile["commands"]["typecheck"] == ["go", "vet", "./..."]


def test_detects_rust_workspace(tmp_path):
    write(tmp_path, "Cargo.toml", "[workspace]\nmembers = ['a']\n")
    profile = stack.detect(tmp_path)
    assert profile["languages"] == ["rust"]
    assert profile["commands"]["test"] == ["cargo", "test"]
    assert profile["monorepo"]["kind"] == "cargo-workspace"


def test_detects_dotnet(tmp_path):
    write(tmp_path, "Api/Api.csproj", "<Project Sdk='Microsoft.NET.Sdk' />")
    profile = stack.detect(tmp_path)
    assert profile["languages"] == ["csharp"]
    assert profile["commands"]["test"] == ["dotnet", "test", "--nologo"]


def test_polyglot_repo_reports_both(tmp_path):
    write(tmp_path, "package.json", package_json(scripts={"build": "vite build"}))
    write(tmp_path, "package-lock.json", "{}")
    write(tmp_path, "pyproject.toml", "[project]\nname='api'\n")
    profile = stack.detect(tmp_path)
    assert set(profile["languages"]) >= {"javascript", "python"}


def test_unknown_stack_is_reported_not_guessed(tmp_path):
    write(tmp_path, "notes.txt", "hello")
    profile = stack.detect(tmp_path)
    assert profile["languages"] == []
    assert any("no build system recognised" in note for note in profile["notes"])
    assert profile["specialist_skills"] == ["engineering-skills:senior-fullstack"]


# -- output ----------------------------------------------------------------


def test_write_persists_json(tmp_path):
    write(tmp_path, "go.mod", "module x\n")
    target = tmp_path / "out" / "stack.json"
    profile = stack.write(tmp_path, target)
    assert json.loads(target.read_text(encoding="utf-8"))["languages"] == profile["languages"]


def test_all_commands_are_argv_lists(tmp_path):
    write(tmp_path, "package.json", package_json(scripts={"build": "x", "test": "y", "lint": "z"}))
    write(tmp_path, "pnpm-lock.yaml", "")
    profile = stack.detect(tmp_path)
    for name, command in profile["commands"].items():
        assert command is None or isinstance(command, list), name
        if command:
            assert all(isinstance(part, str) for part in command), name


def test_summary_line_is_prompt_sized(tmp_path):
    write(tmp_path, "package.json", package_json(dependencies={"next": "15"}, devDependencies={"vitest": "2"}))
    write(tmp_path, "pnpm-lock.yaml", "")
    line = stack.summary_line(stack.detect(tmp_path))
    assert "nextjs" in line and "pnpm" in line and "vitest" in line
    assert "\n" not in line


def test_command_text_renders_argv():
    assert stack.command_text(["pnpm", "run", "test"]) == "pnpm run test"
    assert stack.command_text(None) == "(none detected)"


# -- end-to-end runners, detected independently ---------------------------


def test_a_repo_with_both_runners_reports_both(tmp_path):
    """The old code reported only vitest, hiding the runner the e2e agent needs."""
    write(
        tmp_path,
        "package.json",
        package_json(devDependencies={"vitest": "2.0.0", "@playwright/test": "1.47.0"}),
    )
    write(tmp_path, "pnpm-lock.yaml", "")
    profile = stack.detect(tmp_path)
    assert profile["test_framework"] == "vitest"
    assert profile["e2e_framework"] == "playwright"
    assert profile["commands"]["e2e"] == ["pnpm", "exec", "playwright", "test"]


def test_cypress_is_detected(tmp_path):
    write(tmp_path, "package.json", package_json(devDependencies={"cypress": "13"}))
    write(tmp_path, "package-lock.json", "{}")
    profile = stack.detect(tmp_path)
    assert profile["e2e_framework"] == "cypress"
    assert profile["commands"]["e2e"] == ["npx", "--no-install", "cypress", "run"]


def test_a_test_e2e_script_wins_over_the_inferred_command(tmp_path):
    write(
        tmp_path,
        "package.json",
        package_json(
            scripts={"test:e2e": "playwright test --project=ci"},
            devDependencies={"@playwright/test": "1.47.0"},
        ),
    )
    write(tmp_path, "pnpm-lock.yaml", "")
    profile = stack.detect(tmp_path)
    assert profile["commands"]["e2e"] == ["pnpm", "run", "test:e2e"]
    assert profile["e2e_framework"] == "playwright"


def test_an_e2e_script_alone_is_enough(tmp_path):
    write(tmp_path, "package.json", package_json(scripts={"e2e": "node e2e/run.js"}))
    write(tmp_path, "package-lock.json", "{}")
    profile = stack.detect(tmp_path)
    assert profile["e2e_framework"] == "e2e"
    assert profile["commands"]["e2e"] == ["npm", "run", "e2e"]


def test_no_e2e_runner_is_reported_as_none(tmp_path):
    write(tmp_path, "package.json", package_json(devDependencies={"vitest": "2.0.0"}))
    write(tmp_path, "package-lock.json", "{}")
    profile = stack.detect(tmp_path)
    assert profile["e2e_framework"] is None
    assert profile["commands"]["e2e"] is None


def test_python_e2e_libraries_are_detected(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['playwright']\n")
    profile = stack.detect(tmp_path)
    assert profile["e2e_framework"] == "playwright"


# -- projects that live one level down ------------------------------------


def test_detects_a_python_app_under_backend(tmp_path):
    write(tmp_path, "Makefile", "test:\n\tpytest\n")
    write(
        tmp_path,
        "backend/pyproject.toml",
        "[project]\nname='api'\ndependencies=['django','pytest','ruff']\n",
    )
    profile = stack.detect(tmp_path)
    assert profile["project_dir"] == "backend"
    assert profile["languages"] == ["python"]
    assert "django" in profile["frameworks"]
    assert profile["commands"]["test"] == ["pytest"]
    assert profile["commands"]["lint"] == ["ruff", "check", "."]


def test_a_root_build_system_wins_over_a_child(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")
    write(tmp_path, "backend/package.json", package_json(name="nested"))
    profile = stack.detect(tmp_path)
    assert profile["project_dir"] is None
    assert profile["languages"] == ["python"]


def test_two_candidate_children_stay_undetected(tmp_path):
    write(tmp_path, "backend/pyproject.toml", "[project]\nname='api'\n")
    write(tmp_path, "frontend/package.json", package_json(name="web"))
    profile = stack.detect(tmp_path)
    assert profile["project_dir"] is None
    assert profile["commands"]["test"] is None


# -- toolchains that only exist in a container ----------------------------


COMPOSE = """services:
  postgres:
    image: postgres:16
  api:
    build: ./backend
    ports:
      - "8000:8000"
"""


def test_gates_go_through_the_compose_service_that_builds_the_project(tmp_path):
    write(tmp_path, "docker-compose.yml", COMPOSE)
    write(
        tmp_path,
        "backend/pyproject.toml",
        "[project]\nname='api'\ndependencies=['django','pytest','ruff']\n",
    )
    profile = stack.detect(tmp_path)
    assert profile["project_dir"] == "backend"
    assert profile["commands_cwd"] == ""
    assert profile["commands"]["test"] == [
        "docker", "compose", "run", "--rm", "--entrypoint", "", "api", "pytest",
    ]
    assert profile["commands"]["lint"][:7] == [
        "docker", "compose", "run", "--rm", "--entrypoint", "", "api",
    ]


def test_a_compose_service_building_the_root_is_used_too(tmp_path):
    write(tmp_path, "docker-compose.yml", "services:\n  web:\n    build: .\n")
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")
    profile = stack.detect(tmp_path)
    assert profile["commands"]["test"][:7] == [
        "docker", "compose", "run", "--rm", "--entrypoint", "", "web",
    ]


def test_a_compose_file_with_no_build_leaves_the_commands_alone(tmp_path):
    write(tmp_path, "docker-compose.yml", "services:\n  db:\n    image: postgres:16\n")
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")
    profile = stack.detect(tmp_path)
    assert profile["commands"]["test"] == ["pytest"]
    assert profile["commands_cwd"] is None


def test_a_configured_project_dir_settles_an_ambiguous_repo(tmp_path):
    """Two build systems: detection cannot guess, so config decides."""
    write(tmp_path, "backend/pyproject.toml", "[project]\nname='api'\ndependencies=['pytest']\n")
    write(tmp_path, "frontend/package.json", package_json(name="web"))

    assert stack.detect(tmp_path)["project_dir"] is None

    profile = stack.detect(tmp_path, project_dir="backend")
    assert profile["project_dir"] == "backend"
    assert profile["languages"] == ["python"]
    assert profile["commands"]["test"] == ["pytest"]


def test_a_configured_project_dir_that_does_not_exist_is_ignored(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")
    profile = stack.detect(tmp_path, project_dir="nope")
    assert profile["project_dir"] is None
    assert profile["commands"]["test"] == ["pytest"]


# -- the other half of a monorepo ------------------------------------------
#
# In the recorded runs the frontend suite was never gated, so the verifier ran
# `npm test` by hand and said so in three verdicts. Gating one half of a
# monorepo and calling that a safety net is how a run ships with an untested
# half.


def test_the_other_build_system_is_recorded_as_a_sibling(tmp_path):
    write(tmp_path, "backend/pyproject.toml", "[project]\nname='api'\ndependencies=['pytest']\n")
    write(tmp_path, "frontend/package.json", package_json(name="web", scripts={"test": "vitest run"}))

    profile = stack.detect(tmp_path, project_dir="backend")
    siblings = profile["sibling_projects"]
    assert [s["dir"] for s in siblings] == ["frontend"]
    assert siblings[0]["commands"]["test"] == ["npm", "run", "test"]


def test_a_single_project_repo_has_no_siblings(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")
    assert stack.detect(tmp_path)["sibling_projects"] == []


def test_a_nested_project_with_nothing_beside_it_has_no_siblings(tmp_path):
    write(tmp_path, "backend/pyproject.toml", "[project]\nname='api'\ndependencies=['pytest']\n")
    profile = stack.detect(tmp_path)
    assert profile["project_dir"] == "backend"
    assert profile["sibling_projects"] == []


# -- running one file instead of the whole suite ---------------------------
#
# Across the recorded runs executors launched 909 test containers; one ran the
# identical full-suite command 25 times. The brief named one Test command and
# it was always the whole suite, so every red-green iteration paid for it.


@pytest.mark.parametrize(
    "marker,content,expected",
    [
        ("pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n", ["pytest", "{path}"]),
        ("package.json", None, ["npx", "--no-install", "vitest", "run", "{path}"]),
    ],
)
def test_a_single_file_test_command_is_detected(tmp_path, marker, content, expected):
    if marker == "package.json":
        write(tmp_path, marker, package_json(name="x", scripts={"test": "vitest run"}, devDependencies={"vitest": "2.0.0"}))
    else:
        write(tmp_path, marker, content)
    assert stack.detect(tmp_path)["commands"]["test_one"] == expected


def test_the_single_file_command_is_absent_when_the_runner_is_unknown(tmp_path):
    write(tmp_path, "Makefile", "test:\n\techo hi\n")
    assert stack.detect(tmp_path)["commands"].get("test_one") is None


# -- a stack that did not exist at init ------------------------------------
#
# init detects against the base commit, but a greenfield run's build system is
# the thing the run is about to create. Phase 1's stack.json says so in its own
# notes: "detected at init against an empty repo; re-wired by the cycle-2
# replanner once the stack existed". Phases 1 and 2 ran with no gates at all.


def stored(tmp_path, profile):
    target = tmp_path / "stack.json"
    osenv.write_json(target, profile)
    return target


def test_gaps_are_filled_once_the_build_system_exists(tmp_path):
    empty = stack.detect(tmp_path)
    assert empty["commands"]["test"] is None
    path = stored(tmp_path, empty)

    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")
    profile = stack.fill_gaps(tmp_path, path)

    assert profile["commands"]["test"] == ["pytest"]
    assert osenv.read_json(path)["commands"]["test"] == ["pytest"]


def test_a_usable_stack_is_left_exactly_as_it_is(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")
    tuned = stack.detect(tmp_path)
    tuned["commands"]["test"] = ["make", "test"]
    path = stored(tmp_path, tuned)

    profile = stack.fill_gaps(tmp_path, path)
    assert profile["commands"]["test"] == ["make", "test"]


def test_a_command_the_stored_profile_already_had_is_never_lost(tmp_path):
    empty = stack.detect(tmp_path)
    empty["commands"]["lint"] = ["make", "lint"]
    path = stored(tmp_path, empty)

    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest','ruff']\n")
    profile = stack.fill_gaps(tmp_path, path)

    assert profile["commands"]["test"] == ["pytest"]
    assert profile["commands"]["lint"] == ["make", "lint"], "a tuned command must survive"


def test_filling_records_why_the_profile_changed(tmp_path):
    path = stored(tmp_path, stack.detect(tmp_path))
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['pytest']\n")

    profile = stack.fill_gaps(tmp_path, path)
    assert any("did not exist at init" in note for note in profile["notes"]), profile["notes"]


def test_still_nothing_to_detect_leaves_the_file_alone(tmp_path):
    empty = stack.detect(tmp_path)
    path = stored(tmp_path, empty)

    profile = stack.fill_gaps(tmp_path, path)
    assert profile["commands"]["test"] is None
    assert osenv.read_json(path)["commands"]["test"] is None


def test_a_build_system_that_appeared_one_level_down_is_found(tmp_path):
    path = stored(tmp_path, stack.detect(tmp_path))
    write(tmp_path, "backend/pyproject.toml", "[project]\nname='api'\ndependencies=['pytest']\n")

    profile = stack.fill_gaps(tmp_path, path)
    assert profile["project_dir"] == "backend"
    assert profile["commands"]["test"] == ["pytest"]


def test_an_undetectable_repo_says_the_stack_will_be_re_detected(tmp_path):
    """Not "agents must infer": on a greenfield run the answer arrives later."""
    profile = stack.detect(tmp_path)
    assert any("re-detected once the run has built it" in n for n in profile["notes"]), profile["notes"]
