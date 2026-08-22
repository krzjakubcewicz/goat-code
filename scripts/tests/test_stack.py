"""Stack detection across fixture repos.

These assertions are the contract the executor agents rely on: if the
detected test command is wrong, every gate downstream is wrong.
"""

from __future__ import annotations

import json

import pytest

from codag import stack


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
