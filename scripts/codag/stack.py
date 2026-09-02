"""Stack detection: what is this project, and how do you build and test it?

This is how cod-ag's agents become specialists in whatever the target repo
uses. Detection is deterministic and cheap; the resulting ``stack.json`` is
injected into every agent prompt, so no agent has to rediscover that the
project uses pnpm and vitest.

Commands are stored as argv lists, never shell strings - see the cross-OS
contract in ``osenv``.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re

from . import osenv

#: Order matters: the first matching entry names the primary language.
_LANGUAGE_MARKERS = (
    ("javascript", ("package.json",)),
    ("python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")),
    ("go", ("go.mod",)),
    ("rust", ("Cargo.toml",)),
    ("ruby", ("Gemfile",)),
    ("php", ("composer.json",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    ("elixir", ("mix.exs",)),
)

_SPECIALIST_BY_FRAMEWORK = {
    "nextjs": "engineering-skills:senior-frontend",
    "react": "engineering-skills:senior-frontend",
    "vue": "engineering-skills:senior-frontend",
    "svelte": "engineering-skills:senior-frontend",
    "angular": "engineering-skills:senior-frontend",
    "express": "engineering-skills:senior-backend",
    "fastify": "engineering-skills:senior-backend",
    "nestjs": "engineering-skills:senior-backend",
    "fastapi": "engineering-skills:senior-backend",
    "django": "engineering-skills:senior-backend",
    "flask": "engineering-skills:senior-backend",
}

_SPECIALIST_BY_LANGUAGE = {
    "javascript": "engineering-skills:senior-fullstack",
    "typescript": "engineering-skills:senior-fullstack",
    "python": "engineering-skills:senior-backend",
    "go": "engineering-skills:senior-backend",
    "rust": "engineering-skills:senior-backend",
    "csharp": "engineering-skills:senior-backend",
    "java": "engineering-skills:senior-backend",
    "ruby": "engineering-skills:senior-backend",
    "php": "engineering-skills:senior-backend",
}

_JS_LOCKFILES = (
    ("pnpm", "pnpm-lock.yaml"),
    ("yarn", "yarn.lock"),
    ("bun", "bun.lockb"),
    ("npm", "package-lock.json"),
)

#: Unit-level runners. Ordered: the first match names ``test_framework``.
_JS_TEST_LIBS = ("vitest", "jest", "mocha", "ava", "node:test")

#: Stands in for the test file in a ``test_one`` command. The brief
#: substitutes it; only runners that genuinely take a path get one.
PATH_TOKEN = "{path}"

#: End-to-end runners, detected separately. A repo commonly has both a unit
#: runner and an E2E one, and reporting only the first would hide the E2E
#: runner the e2e agent needs.
_JS_E2E_LIBS = (
    ("playwright", ("@playwright/test", "playwright")),
    ("cypress", ("cypress",)),
    ("webdriverio", ("webdriverio", "@wdio/cli")),
    ("puppeteer", ("puppeteer",)),
    ("testcafe", ("testcafe",)),
)

#: Scripts that run an end-to-end suite, most specific first.
_JS_E2E_SCRIPTS = ("test:e2e", "e2e", "test:integration", "e2e:run")

_PY_TEST_LIBS = ("pytest", "unittest2", "nose2")

_PY_E2E_LIBS = ("playwright", "selenium", "splinter")


def detect(repo, project_dir=None, _siblings=True):
    """Inspect ``repo`` and return the stack profile as a plain dict.

    ``project_dir`` names the subdirectory holding the build system, for a
    repo where detection cannot tell on its own - a monorepo with several.
    It comes from ``project_dir`` in ``.codag/config.yaml``.
    """
    repo = pathlib.Path(repo)
    profile = {
        "detected_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "repo": str(repo),
        "languages": [],
        "frameworks": [],
        "package_manager": None,
        "monorepo": None,
        "test_framework": None,
        "e2e_framework": None,
        "commands": {
            "setup": None,
            "build": None,
            "typecheck": None,
            "lint": None,
            "test": None,
            # The same suite narrowed to one file. The red-green loop runs
            # this; the whole suite runs once before reporting.
            "test_one": None,
            "e2e": None,
        },
        "source_dirs": [],
        "test_dirs": [],
        "specialist_skills": [],
        "notes": [],
        "project_dir": None,
        "commands_cwd": None,
        "sibling_projects": [],
    }

    root = repo
    markers = _present_markers(repo)
    profile["root_markers"] = markers

    nested = _configured_project(repo, project_dir) or _nested_project(repo, markers)
    if nested is not None:
        repo, markers = _adopt_nested_project(repo, nested, profile)
        if _siblings:
            profile["sibling_projects"] = _sibling_projects(root, nested)

    if "package.json" in markers:
        _detect_javascript(repo, profile)
    if any(m in markers for m in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")):
        _detect_python(repo, profile)
    if "go.mod" in markers:
        _detect_go(repo, profile)
    if "Cargo.toml" in markers:
        _detect_rust(repo, profile)
    if _find_dotnet_projects(repo):
        _detect_dotnet(repo, profile)

    if not profile["languages"]:
        for language, files in _LANGUAGE_MARKERS:
            if any(f in markers for f in files):
                profile["languages"].append(language)
        if not profile["languages"]:
            profile["notes"].append(
                "no build system recognised here; if this run creates one it is "
                "re-detected once the run has built it, and the gates start "
                "working from that point"
            )

    _containerize(root, profile)

    profile["source_dirs"] = _existing(repo, ["src", "lib", "app", "pkg", "cmd", "internal"])
    profile["test_dirs"] = _existing(repo, ["tests", "test", "spec", "__tests__", "e2e"])
    profile["specialist_skills"] = _specialists(profile)
    return profile


# -- ecosystems ------------------------------------------------------------


def _detect_javascript(repo, profile):
    pkg = _read_json(repo / "package.json") or {}
    scripts = pkg.get("scripts") or {}
    deps = _all_deps(pkg)

    manager = "npm"
    for name, lockfile in _JS_LOCKFILES:
        if (repo / lockfile).exists():
            manager = name
            break
    else:
        declared = pkg.get("packageManager")
        if isinstance(declared, str) and "@" in declared:
            manager = declared.split("@", 1)[0]
        profile["notes"].append("no lockfile found; assuming {}".format(manager))
    profile["package_manager"] = manager

    languages = ["javascript"]
    if (repo / "tsconfig.json").exists() or "typescript" in deps:
        languages.insert(0, "typescript")
    profile["languages"] = _merge(profile["languages"], languages)

    for framework in ("next", "react", "vue", "svelte", "@angular/core", "express", "fastify", "@nestjs/core"):
        if framework in deps:
            profile["frameworks"] = _merge(profile["frameworks"], [_normalise_framework(framework)])

    for lib in _JS_TEST_LIBS:
        if lib in deps:
            profile["test_framework"] = lib
            break

    for name, packages in _JS_E2E_LIBS:
        if any(package in deps for package in packages):
            profile["e2e_framework"] = name
            break

    profile["commands"]["setup"] = _js_install(manager)
    for key, candidates in (
        ("build", ("build",)),
        ("typecheck", ("typecheck", "type-check", "tsc", "types")),
        ("lint", ("lint", "eslint")),
        ("test", ("test", "test:unit", "tests")),
    ):
        script = next((c for c in candidates if c in scripts), None)
        if script:
            profile["commands"][key] = _js_run(manager, script)

    if profile["commands"]["typecheck"] is None and "typescript" in languages:
        profile["commands"]["typecheck"] = _js_exec(manager, ["tsc", "--noEmit"])
    if profile["commands"]["test"] is None and profile["test_framework"]:
        profile["commands"]["test"] = _js_exec(manager, [profile["test_framework"], "run"])
    if profile["test_framework"] == "vitest":
        profile["commands"]["test_one"] = _js_exec(manager, ["vitest", "run", PATH_TOKEN])
    elif profile["test_framework"] == "jest":
        profile["commands"]["test_one"] = _js_exec(manager, ["jest", PATH_TOKEN])

    e2e_script = next((s for s in _JS_E2E_SCRIPTS if s in scripts), None)
    if e2e_script:
        profile["commands"]["e2e"] = _js_run(manager, e2e_script)
        profile["e2e_framework"] = profile["e2e_framework"] or e2e_script
    elif profile["e2e_framework"] == "playwright":
        profile["commands"]["e2e"] = _js_exec(manager, ["playwright", "test"])
    elif profile["e2e_framework"] == "cypress":
        profile["commands"]["e2e"] = _js_exec(manager, ["cypress", "run"])

    profile["monorepo"] = _detect_js_monorepo(repo, pkg, manager)


def _detect_js_monorepo(repo, pkg, manager):
    if (repo / "pnpm-workspace.yaml").exists():
        return {"kind": "pnpm-workspaces", "config": "pnpm-workspace.yaml"}
    if isinstance(pkg.get("workspaces"), (list, dict)):
        return {"kind": "{}-workspaces".format(manager), "config": "package.json"}
    for marker, kind in (("turbo.json", "turborepo"), ("nx.json", "nx"), ("lerna.json", "lerna")):
        if (repo / marker).exists():
            return {"kind": kind, "config": marker}
    return None


def _detect_python(repo, profile):
    profile["languages"] = _merge(profile["languages"], ["python"])
    pyproject = _read_text(repo / "pyproject.toml") or ""

    manager = None
    if (repo / "poetry.lock").exists() or "[tool.poetry]" in pyproject:
        manager = "poetry"
    elif (repo / "uv.lock").exists():
        manager = "uv"
    elif (repo / "Pipfile").exists():
        manager = "pipenv"
    elif (repo / "requirements.txt").exists():
        manager = "pip"
    profile["package_manager"] = profile["package_manager"] or manager

    for framework in ("fastapi", "django", "flask"):
        if framework in pyproject.lower() or _requirements_mention(repo, framework):
            profile["frameworks"] = _merge(profile["frameworks"], [framework])

    for lib in _PY_TEST_LIBS:
        if lib in pyproject.lower() or _requirements_mention(repo, lib) or (repo / "pytest.ini").exists():
            profile["test_framework"] = profile["test_framework"] or lib
            break

    for lib in _PY_E2E_LIBS:
        if lib in pyproject.lower() or _requirements_mention(repo, lib):
            profile["e2e_framework"] = profile["e2e_framework"] or lib
            break

    if manager == "poetry":
        profile["commands"]["setup"] = ["poetry", "install"]
        runner = ["poetry", "run"]
    elif manager == "uv":
        profile["commands"]["setup"] = ["uv", "sync"]
        runner = ["uv", "run"]
    elif manager == "pipenv":
        profile["commands"]["setup"] = ["pipenv", "install", "--dev"]
        runner = ["pipenv", "run"]
    elif manager == "pip":
        profile["commands"]["setup"] = ["python", "-m", "pip", "install", "-r", "requirements.txt"]
        runner = []
    else:
        runner = []

    test_cmd = ["pytest"] if (profile["test_framework"] or "pytest") == "pytest" else ["python", "-m", "unittest"]
    profile["commands"]["test"] = profile["commands"]["test"] or (runner + test_cmd)
    if test_cmd[0] == "pytest":
        profile["commands"]["test_one"] = runner + ["pytest", PATH_TOKEN]

    if "ruff" in pyproject or (repo / "ruff.toml").exists() or (repo / ".ruff.toml").exists():
        profile["commands"]["lint"] = runner + ["ruff", "check", "."]
    elif "flake8" in pyproject or (repo / ".flake8").exists():
        profile["commands"]["lint"] = runner + ["flake8"]

    if "mypy" in pyproject or (repo / "mypy.ini").exists():
        profile["commands"]["typecheck"] = runner + ["mypy", "."]
    elif "pyright" in pyproject:
        profile["commands"]["typecheck"] = runner + ["pyright"]


def _detect_go(repo, profile):
    profile["languages"] = _merge(profile["languages"], ["go"])
    profile["package_manager"] = profile["package_manager"] or "go modules"
    profile["commands"]["setup"] = profile["commands"]["setup"] or ["go", "mod", "download"]
    profile["commands"]["build"] = profile["commands"]["build"] or ["go", "build", "./..."]
    profile["commands"]["test"] = profile["commands"]["test"] or ["go", "test", "./..."]
    profile["commands"]["typecheck"] = profile["commands"]["typecheck"] or ["go", "vet", "./..."]
    if (repo / ".golangci.yml").exists() or (repo / ".golangci.yaml").exists():
        profile["commands"]["lint"] = ["golangci-lint", "run"]
    profile["test_framework"] = profile["test_framework"] or "go test"


def _detect_rust(repo, profile):
    profile["languages"] = _merge(profile["languages"], ["rust"])
    profile["package_manager"] = profile["package_manager"] or "cargo"
    profile["commands"]["setup"] = profile["commands"]["setup"] or ["cargo", "fetch"]
    profile["commands"]["build"] = profile["commands"]["build"] or ["cargo", "build"]
    profile["commands"]["test"] = profile["commands"]["test"] or ["cargo", "test"]
    profile["commands"]["typecheck"] = profile["commands"]["typecheck"] or ["cargo", "check"]
    profile["commands"]["lint"] = profile["commands"]["lint"] or ["cargo", "clippy", "--", "-D", "warnings"]
    profile["test_framework"] = profile["test_framework"] or "cargo test"
    cargo = _read_text(repo / "Cargo.toml") or ""
    if "[workspace]" in cargo:
        profile["monorepo"] = {"kind": "cargo-workspace", "config": "Cargo.toml"}


def _detect_dotnet(repo, profile):
    profile["languages"] = _merge(profile["languages"], ["csharp"])
    profile["package_manager"] = profile["package_manager"] or "nuget"
    profile["commands"]["setup"] = profile["commands"]["setup"] or ["dotnet", "restore"]
    profile["commands"]["build"] = profile["commands"]["build"] or ["dotnet", "build", "--nologo"]
    profile["commands"]["test"] = profile["commands"]["test"] or ["dotnet", "test", "--nologo"]
    profile["test_framework"] = profile["test_framework"] or "dotnet test"


# -- helpers ---------------------------------------------------------------


def _js_install(manager):
    return {
        "pnpm": ["pnpm", "install"],
        "yarn": ["yarn", "install"],
        "bun": ["bun", "install"],
    }.get(manager, ["npm", "install"])


def _js_run(manager, script):
    if manager == "npm":
        return ["npm", "run", script]
    return [manager, "run", script]


def _js_exec(manager, argv):
    return {
        "pnpm": ["pnpm", "exec"],
        "yarn": ["yarn"],
        "bun": ["bunx"],
    }.get(manager, ["npx", "--no-install"]) + list(argv)


def _normalise_framework(name):
    return {"next": "nextjs", "@angular/core": "angular", "@nestjs/core": "nestjs"}.get(name, name)


def _all_deps(pkg):
    deps = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            deps.update(section)
    return deps


_BUILD_MARKERS = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "composer.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "mix.exs",
)


_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

#: Emptying the entrypoint matters: an app image usually starts a server.
_COMPOSE_RUN = ("docker", "compose", "run", "--rm", "--entrypoint", "")

_CONTAINERISED_GATES = ("build", "typecheck", "lint", "test", "e2e")


def _containerize(root, profile):
    """Run the gates inside the compose service that owns the code.

    A project whose toolchain only exists in its image - the common shape
    once there is a Dockerfile - has nothing to run on the host: ``pytest``
    is simply not installed there. When a compose file defines a service
    built from the project directory, the same commands go through
    ``docker compose run`` instead, from the repo root where the compose
    file lives.
    """
    service = _compose_service(root, profile.get("project_dir"))
    if not service:
        return
    commands = profile["commands"]
    if not any(commands.get(name) for name in _CONTAINERISED_GATES):
        return
    for name in _CONTAINERISED_GATES:
        if commands.get(name):
            commands[name] = list(_COMPOSE_RUN) + [service] + list(commands[name])
    profile["commands"]["setup"] = None
    profile["commands_cwd"] = ""
    profile["notes"].append(
        "gates run in the '{}' compose service; the toolchain lives in the image".format(service)
    )


def _compose_service(root, project_dir):
    """Name the compose service whose build context is the project.

    Deliberately a line scan rather than a YAML parse: all it needs is the
    service name above a ``build:`` that points at the project directory,
    and a wrong guess here would silently run the gates in the wrong
    container.
    """
    text = ""
    for name in _COMPOSE_FILES:
        text = _read_text(pathlib.Path(root) / name) or ""
        if text:
            break
    if not text:
        return None

    wanted = {".", "./"}
    if project_dir:
        wanted = {project_dir, "./" + project_dir, project_dir + "/", "./" + project_dir + "/"}

    in_services = False
    service = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_services = stripped.startswith("services:")
            service = None
            continue
        if not in_services:
            continue
        if indent <= 2 and stripped.endswith(":"):
            service = stripped[:-1].strip()
            continue
        key, _, value = stripped.partition(":")
        if service and key.strip() in ("build", "context") and value.strip() in wanted:
            return service
    return None


def _sibling_projects(root, adopted):
    """The build systems beside the one the gates were pointed at.

    Gating one half of a monorepo and calling it a safety net is how a run
    ships with its other half untested: in the recorded runs the frontend
    suite was never gated once, and the verifier ran it by hand instead and
    said so in three separate verdicts.

    Each sibling is detected in full but never adopted, so the brief and the
    specialist skill still describe the primary project.
    """
    out = []
    for child in sorted(pathlib.Path(root).iterdir()):
        if child == adopted or not child.is_dir() or child.name.startswith("."):
            continue
        if not any((child / marker).exists() for marker in _BUILD_MARKERS):
            continue
        nested = detect(child, _siblings=False)
        out.append({"dir": child.name, "commands": nested.get("commands") or {}})
    return out


def _configured_project(repo, project_dir):
    """The explicitly configured project directory, if it exists."""
    if not project_dir:
        return None
    candidate = pathlib.Path(repo) / project_dir
    return candidate if candidate.is_dir() else None


def _nested_project(repo, markers):
    """The single child directory holding the build system, if the root has none.

    A repo that keeps its app under ``backend/`` or ``server/`` looks empty
    from the root: ``Makefile`` and ``docker-compose.yml`` name no language.
    Detecting one level down is what keeps the gates from reporting
    ``missing`` for such a layout. Ambiguity is left undetected on purpose -
    two candidates is a monorepo, which the caller must configure.
    """
    if any(m in markers for m in _BUILD_MARKERS):
        return None
    repo = pathlib.Path(repo)
    if not repo.is_dir():
        return None
    found = [
        child
        for child in sorted(repo.iterdir())
        if child.is_dir()
        and not child.name.startswith(".")
        and any((child / m).exists() for m in _BUILD_MARKERS)
    ]
    return found[0] if len(found) == 1 else None


def _adopt_nested_project(repo, nested, profile):
    """Re-root detection onto ``nested`` and record where the gates must run."""
    rel = nested.name
    profile["project_dir"] = rel
    profile["notes"].append(
        "build system lives in {}/, not the repo root; gates run there".format(rel)
    )
    return nested, _present_markers(nested)


def _present_markers(repo):
    names = [
        "package.json",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "go.mod",
        "Cargo.toml",
        "Gemfile",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "mix.exs",
        "tsconfig.json",
        "Makefile",
    ]
    return [n for n in names if (pathlib.Path(repo) / n).exists()]


def _find_dotnet_projects(repo):
    repo = pathlib.Path(repo)
    found = list(repo.glob("*.csproj")) + list(repo.glob("*.sln"))
    if found:
        return found
    for child in repo.iterdir() if repo.is_dir() else []:
        if child.is_dir() and not child.name.startswith("."):
            found.extend(child.glob("*.csproj"))
    return found


def _requirements_mention(repo, needle):
    text = _read_text(pathlib.Path(repo) / "requirements.txt") or ""
    return re.search(r"^\s*" + re.escape(needle) + r"\b", text, re.MULTILINE | re.IGNORECASE) is not None


def _existing(repo, names):
    return [n for n in names if (pathlib.Path(repo) / n).is_dir()]


def _merge(current, additions):
    out = list(current)
    for item in additions:
        if item not in out:
            out.append(item)
    return out


def _specialists(profile):
    out = []
    for framework in profile["frameworks"]:
        skill = _SPECIALIST_BY_FRAMEWORK.get(framework)
        if skill and skill not in out:
            out.append(skill)
    for language in profile["languages"]:
        skill = _SPECIALIST_BY_LANGUAGE.get(language)
        if skill and skill not in out:
            out.append(skill)
    if not out:
        out.append("engineering-skills:senior-fullstack")
    return out


def _read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(path):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def write(repo, path, project_dir=None):
    """Detect and persist the profile. Returns the profile."""
    profile = detect(repo, project_dir=project_dir)
    osenv.write_json(path, profile)
    return profile


def usable(profile):
    """Whether this profile can actually gate anything.

    The test command is the proxy: a stack with no way to run tests gives the
    pipeline no automated safety net at all, whatever else it detected.
    """
    return bool(((profile or {}).get("commands") or {}).get("test"))


def fill_gaps(where, path, project_dir=None):
    """Re-detect once the build system exists, and keep what was already there.

    ``init`` detects against the base commit, but a greenfield run's build
    system is the thing the run is about to create - so the first run of a new
    project detects nothing and gates nothing. Phase 1's own stack.json
    records the manual workaround in its notes: "detected at init against an
    empty repo; re-wired by the cycle-2 replanner once the stack existed".
    Phases 1 and 2 both ran to a verdict with every gate reporting `missing`.

    Only gaps are filled. A profile that can already gate is left untouched,
    and any command the stored profile carried survives re-detection, because
    a hand-tuned command is worth more than a freshly guessed one.
    """
    path = pathlib.Path(path)
    stored = osenv.read_json(path) if path.exists() else {}
    if usable(stored):
        return stored

    fresh = detect(where, project_dir=project_dir)
    if not usable(fresh):
        return stored or fresh

    for name, command in (stored.get("commands") or {}).items():
        if command:
            fresh.setdefault("commands", {})[name] = command
    fresh.setdefault("notes", []).append(
        "the build system did not exist at init; re-detected against {} once "
        "the run had created it".format(where)
    )
    osenv.write_json(path, fresh)
    return fresh


def summary_line(profile):
    """One-line description for an agent prompt."""
    languages = "/".join(profile["languages"]) or "unknown"
    frameworks = ", ".join(profile["frameworks"])
    parts = [languages]
    if frameworks:
        parts.append(frameworks)
    if profile.get("package_manager"):
        parts.append(profile["package_manager"])
    if profile.get("test_framework"):
        parts.append("tests via {}".format(profile["test_framework"]))
    return " | ".join(parts)


def command_text(argv):
    """Render an argv list for humans and agent prompts."""
    if not argv:
        return "(none detected)"
    return " ".join(argv)
