"""Structural checks on the plugin itself.

A typo in an agent name or a CLI command referenced from a skill fails at
run time, deep inside a pipeline, with a confusing message. These catch it
at commit time instead.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

AGENTS = sorted((ROOT / "agents").glob("*.md"))
COMMANDS = sorted((ROOT / "commands").glob("*.md"))
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))
#: Directories that are not part of the plugin: git internals, installed
#: dependencies, and everything `.gitignore` keeps out - local run telemetry,
#: caches, virtualenvs, a run directory left behind by a self-test. A stray
#: `.md` in any of them is not a plugin file and must not be cross-referenced
#: as one; copied run artifacts in particular are full of paths that look like
#: agent and command names.
EXCLUDED_DIRS = {
    ".git",
    ".goatcode",
    ".pytest_cache",
    ".venv",
    ".worktrees",
    "__pycache__",
    "node_modules",
    "telemetry",
}
MARKDOWN = sorted(p for p in ROOT.rglob("*.md") if not EXCLUDED_DIRS & set(p.parts))


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "{} has no frontmatter".format(path.name)
    end = text.index("\n---\n", 3)
    block = text[4:end]
    fields = {}
    key = None
    for line in block.splitlines():
        match = re.match(r"^([a-z][a-z_-]*):\s*(.*)$", line)
        if match:
            key = match.group(1)
            fields[key] = match.group(2).strip()
        elif key and line.strip():
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields, text[end + 5 :]


def cli_module():
    spec = importlib.util.spec_from_file_location("goatcode_cli_struct", SCRIPTS / "goatcode.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- manifests -------------------------------------------------------------


def test_plugin_manifest_is_valid():
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "goat-code"
    assert data["description"]
    assert re.match(r"^\d+\.\d+\.\d+$", data["version"])


def test_marketplace_manifest_points_at_this_plugin():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert data["name"] == "goat-code"
    assert [p["name"] for p in data["plugins"]] == ["goat-code"]
    assert data["plugins"][0]["source"] == "./"


# -- agents ----------------------------------------------------------------


def test_every_agent_exists():
    assert {p.stem for p in AGENTS} == {
        "goat-code-planner",
        "goat-code-executor",
        "goat-code-synthesizer",
        "goat-code-verifier",
        "goat-code-e2e",
        "goat-code-scribe",
        "goat-code-replanner",
    }


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_agent_frontmatter(path):
    fields, body = frontmatter(path)
    assert fields.get("name") == path.stem, "name must match the filename"
    assert len(fields.get("description", "")) > 40, "description drives dispatch; make it specific"
    assert fields.get("tools"), "tools must be declared explicitly"
    assert fields.get("model") in ("opus", "sonnet", "haiku", "fable", "inherit")
    assert len(body.strip()) > 500, "an agent needs a real instruction body"


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_agent_points_at_the_conventions_skill(path):
    """Contracts live in one place; each agent must send the reader there."""
    assert "goat-code-conventions" in path.read_text(encoding="utf-8")


def test_read_only_agents_cannot_write():
    """The verifier judges; if it could edit, nothing would be left to judge."""
    fields, _body = frontmatter(ROOT / "agents" / "goat-code-verifier.md")
    tools = fields["tools"]
    for forbidden in ("Edit", "Write", "NotebookEdit"):
        assert forbidden not in tools, "verifier must stay read-only"


def test_planner_and_replanner_cannot_edit_code():
    """They write tasks.yaml with Write; Edit would let them touch source."""
    for name in ("goat-code-planner", "goat-code-replanner"):
        fields, _body = frontmatter(ROOT / "agents" / (name + ".md"))
        assert "Edit" not in fields["tools"], "{} must not edit existing files".format(name)


# -- commands --------------------------------------------------------------


def test_every_command_exists():
    assert {p.stem for p in COMMANDS} == {
        "goat-code",
        "goat-code-spec",
        "goat-code-status",
        "goat-code-resume",
        "goat-code-abort",
    }


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.stem)
def test_command_frontmatter(path):
    fields, body = frontmatter(path)
    assert fields.get("description")
    assert body.strip()


def test_entry_commands_invoke_the_orchestrator_skill():
    for name in ("goat-code", "goat-code-resume"):
        text = (ROOT / "commands" / (name + ".md")).read_text(encoding="utf-8")
        assert "goat-code:goat-code-orchestrator" in text


def test_abort_command_protects_committed_work():
    text = (ROOT / "commands" / "goat-code-abort.md").read_text(encoding="utf-8")
    assert "confirm" in text.lower()
    assert "--delete-branches" in text


# -- skills ----------------------------------------------------------------


def test_every_skill_exists():
    assert {p.parent.name for p in SKILLS} == {"goat-code-orchestrator", "goat-code-conventions"}


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(path):
    fields, body = frontmatter(path)
    assert fields.get("name") == path.parent.name, "name must match the directory"
    assert len(fields.get("description", "")) > 40
    assert len(body.strip()) > 1000


def test_the_state_machine_dispatches_every_agent():
    """Who runs when lives in machine.py, not in the skill's prose."""
    text = (SCRIPTS / "goatcode" / "machine.py").read_text(encoding="utf-8")
    for path in AGENTS:
        assert path.stem in text, "the machine never dispatches {}".format(path.stem)


def test_orchestrator_states_the_parallel_dispatch_rule():
    """One message per wave is the whole point; it must be unmissable."""
    text = (ROOT / "skills" / "goat-code-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
    assert "single message" in text
    assert "parallel" in text


def test_orchestrator_defers_to_the_state_machine():
    """The skill must not re-describe the pipeline it no longer owns."""
    text = (ROOT / "skills" / "goat-code-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
    assert "next" in text
    assert "do **not** decide what happens next" in text
    assert "## Step 1" not in text, "the eight-step prose is now machine.py"


def test_the_evidence_standard_is_defined_once_and_cited_by_both_sides():
    """The executor is graded on the verifier's bar, so it is given that bar.

    A rubric that lives only in the verifier is a rubric the work is judged
    against and never built to - which is what a whole cycle used to be spent
    discovering.
    """
    conventions = (ROOT / "skills" / "goat-code-conventions" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Evidence standard" in conventions

    for name in ("goat-code-executor", "goat-code-verifier"):
        text = (ROOT / "agents" / "{}.md".format(name)).read_text(encoding="utf-8")
        assert "Evidence standard" in text, "{} does not cite the shared standard".format(name)


# -- cross-references ------------------------------------------------------


def test_every_referenced_name_exists():
    """No dispatch target, skill or command reference may be a typo.

    One prefix now covers agents, skills and commands, so a reference is
    checked against the union of the three: a typo resolves to none of them.
    Matching on the prefix alone - which worked while agents were `codag-` and
    skills were `cod-ag-` - would now silently match nothing at all.
    """
    known = (
        {p.stem for p in AGENTS}
        | {p.parent.name for p in SKILLS}
        | {p.stem for p in COMMANDS}
    )
    pattern = re.compile(r"\bgoat-code-[a-z0-9]+(?:-[a-z0-9]+)*")
    for path in MARKDOWN:
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            assert name in known, "{} references unknown name {}".format(path.name, name)


def test_every_referenced_skill_exists():
    known = {p.parent.name for p in SKILLS}
    for path in MARKDOWN:
        for name in re.findall(r"goat-code:([a-z-]+)", path.read_text(encoding="utf-8")):
            assert name in known, "{} references unknown skill {}".format(path.name, name)


def test_every_referenced_cli_command_exists():
    """A skill telling an agent to run a command that does not exist is a
    failure deep inside a pipeline run."""
    module = cli_module()
    parser = module.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    known = set()
    for action in actions:
        known.update(action.choices or {})
    known.update(("--json", "--repo", "--run", "--help"))

    pattern = re.compile(r"goatcode\.py\"?\s+([a-z-]+)")
    for path in MARKDOWN:
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            assert name in known, "{} calls unknown command 'goatcode {}'".format(path.name, name)


def test_documented_cli_examples_are_real_commands():
    module = cli_module()
    parser = module.build_parser()
    top = next(a for a in parser._actions if a.dest == "command")
    documented = re.findall(
        r"^\| `([a-z-]+)[^`]*` \|",
        (ROOT / "skills" / "goat-code-conventions" / "SKILL.md").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert documented, "the conventions skill should list the CLI"
    for name in documented:
        assert name in top.choices, "conventions documents nonexistent command {}".format(name)


# -- templates -------------------------------------------------------------


def test_spec_template_has_the_sections_the_planner_grills_on():
    text = (ROOT / "templates" / "spec.md").read_text(encoding="utf-8")
    for heading in ("## Goal", "## Requirements", "## Acceptance criteria", "## Out of scope", "## Constraints"):
        assert heading in text


def test_config_template_parses_and_matches_the_defaults():
    import sys

    sys.path.insert(0, str(SCRIPTS))
    from goatcode import miniyaml
    from goatcode.run import DEFAULT_CONFIG

    config = miniyaml.load(ROOT / "templates" / "config.yaml")
    assert set(config) == set(DEFAULT_CONFIG), "template drifted from the real defaults"
    assert set(config["models"]) == set(DEFAULT_CONFIG["models"])
    for key, value in config.items():
        assert value == DEFAULT_CONFIG[key], "template's {} does not match the default".format(key)


# -- CI --------------------------------------------------------------------


def test_ci_covers_all_three_platforms():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for platform in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert platform in text, "the cross-OS guarantee needs {} in CI".format(platform)
    assert '"3.9"' in text, "3.9 is the documented floor"


# -- model assignment ------------------------------------------------------

EXPECTED_MODELS = {
    "goat-code-planner": "opus",
    "goat-code-executor": "haiku",
    "goat-code-synthesizer": "sonnet",
    "goat-code-e2e": "sonnet",
    "goat-code-scribe": "sonnet",
    "goat-code-verifier": "opus",
    "goat-code-replanner": "opus",
}


@pytest.mark.parametrize("name,model", sorted(EXPECTED_MODELS.items()))
def test_agent_runs_on_the_intended_model(name, model):
    fields, _body = frontmatter(ROOT / "agents" / (name + ".md"))
    assert fields["model"] == model


def test_config_defaults_agree_with_the_agent_files():
    """Two places name a model per role; they must not drift apart."""
    import sys

    sys.path.insert(0, str(SCRIPTS))
    from goatcode.run import DEFAULT_CONFIG

    models = DEFAULT_CONFIG["models"]
    for name, model in EXPECTED_MODELS.items():
        role = name.replace("goat-code-", "")
        assert models[role] == model, "{} frontmatter says {}, config says {}".format(
            name, model, models[role]
        )


def test_blocked_executors_escalate_to_a_stronger_model():
    import sys

    sys.path.insert(0, str(SCRIPTS))
    from goatcode.run import DEFAULT_CONFIG

    ladder = ["haiku", "sonnet", "opus"]
    models = DEFAULT_CONFIG["models"]
    assert ladder.index(models["executor_escalated"]) > ladder.index(models["executor"])


def test_orchestrator_commands_pin_their_model():
    """The orchestrator is the main thread, so its model lives here."""
    for name in ("goat-code", "goat-code-resume"):
        fields, _body = frontmatter(ROOT / "commands" / (name + ".md"))
        assert fields.get("model") == "haiku", "{} does not pin a model".format(name)


def test_readme_model_table_matches_reality():
    import sys

    sys.path.insert(0, str(SCRIPTS))
    from goatcode.run import DEFAULT_CONFIG

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| ([a-zA-Z0-9 ()]+) \| (opus|sonnet|haiku) \|", text, re.MULTILINE))
    assert rows.get("orchestrator") == "haiku"
    for role, model in DEFAULT_CONFIG["models"].items():
        if role == "executor_escalated":
            continue
        assert rows.get(role) == model, "README says {} for {}, config says {}".format(
            rows.get(role), role, model
        )


# -- the skills each agent is told to load --------------------------------

EXPECTED_SKILLS = {
    "goat-code-executor": ["superpowers:test-driven-development", "ponytail:ponytail"],
    "goat-code-planner": ["superpowers:writing-plans", "superpowers:brainstorming"],
    "goat-code-verifier": ["superpowers:verification-before-completion", "ponytail:ponytail-review"],
    "goat-code-replanner": ["superpowers:systematic-debugging"],
}


@pytest.mark.parametrize("name,skills", sorted(EXPECTED_SKILLS.items()))
def test_agents_are_pointed_at_their_skills(name, skills):
    """A skill an agent is meant to use, silently dropped, is invisible."""
    text = (ROOT / "agents" / (name + ".md")).read_text(encoding="utf-8")
    for skill in skills:
        assert skill in text, "{} no longer names {}".format(name, skill)


def test_the_conventions_skill_lists_them_all():
    text = (ROOT / "skills" / "goat-code-conventions" / "SKILL.md").read_text(encoding="utf-8")
    for skills in EXPECTED_SKILLS.values():
        for skill in skills:
            assert skill in text, "conventions does not list {}".format(skill)


#: Every skill goat-code's agents load from another plugin. Kept explicit so
#: adding a dependency is a deliberate act - goat-code is installed on machines
#: that do not have whatever happens to be in one developer's ~/.claude.
EXTERNAL_SKILLS = {
    "superpowers:brainstorming",
    "superpowers:systematic-debugging",
    "superpowers:test-driven-development",
    "superpowers:verification-before-completion",
    "superpowers:writing-plans",
    "ponytail:ponytail",
    "ponytail:ponytail-review",
}


def test_agents_depend_on_exactly_the_declared_skills():
    """A new dependency has to be added here on purpose.

    Note what this does *not* catch: a bare, un-namespaced skill name from
    someone's personal ~/.claude/skills. Namespacing is the convention that
    makes a dependency visible; keep to it.
    """
    found = set()
    for path in AGENTS:
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r"`((?:superpowers|ponytail|grilling)[a-z:-]*)`", text):
            found.add(name)

    # engineering-skills is resolved at runtime from stack.json, not named.
    unexpected = found - EXTERNAL_SKILLS
    assert not unexpected, "undeclared skill dependency: {}".format(sorted(unexpected))

    unused = EXTERNAL_SKILLS - found
    assert not unused, "declared but no agent loads it: {}".format(sorted(unused))
