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
MARKDOWN = sorted(
    p for p in ROOT.rglob("*.md") if ".git" not in p.parts and "node_modules" not in p.parts
)


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "{} has no frontmatter".format(path.name)
    end = text.index("\n---\n", 3)
    block = text[4:end]
    fields = {}
    key = None
    for line in block.splitlines():
        match = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if match:
            key = match.group(1)
            fields[key] = match.group(2).strip()
        elif key and line.strip():
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields, text[end + 5 :]


def cli_module():
    spec = importlib.util.spec_from_file_location("codag_cli_struct", SCRIPTS / "codag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- manifests -------------------------------------------------------------


def test_plugin_manifest_is_valid():
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "cod-ag"
    assert data["description"]
    assert re.match(r"^\d+\.\d+\.\d+$", data["version"])


def test_marketplace_manifest_points_at_this_plugin():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert data["name"] == "cod-ag"
    assert [p["name"] for p in data["plugins"]] == ["cod-ag"]
    assert data["plugins"][0]["source"] == "./"


# -- agents ----------------------------------------------------------------


def test_every_agent_exists():
    assert {p.stem for p in AGENTS} == {
        "codag-planner",
        "codag-executor",
        "codag-synthesizer",
        "codag-verifier",
        "codag-replanner",
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
    assert "cod-ag-conventions" in path.read_text(encoding="utf-8")


def test_read_only_agents_cannot_write():
    """The verifier judges; if it could edit, nothing would be left to judge."""
    fields, _body = frontmatter(ROOT / "agents" / "codag-verifier.md")
    tools = fields["tools"]
    for forbidden in ("Edit", "Write", "NotebookEdit"):
        assert forbidden not in tools, "verifier must stay read-only"


def test_planner_and_replanner_cannot_edit_code():
    """They write tasks.yaml with Write; Edit would let them touch source."""
    for name in ("codag-planner", "codag-replanner"):
        fields, _body = frontmatter(ROOT / "agents" / (name + ".md"))
        assert "Edit" not in fields["tools"], "{} must not edit existing files".format(name)


# -- commands --------------------------------------------------------------


def test_every_command_exists():
    assert {p.stem for p in COMMANDS} == {
        "cod-ag",
        "cod-ag-spec",
        "cod-ag-status",
        "cod-ag-resume",
        "cod-ag-abort",
    }


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.stem)
def test_command_frontmatter(path):
    fields, body = frontmatter(path)
    assert fields.get("description")
    assert body.strip()


def test_entry_commands_invoke_the_orchestrator_skill():
    for name in ("cod-ag", "cod-ag-resume"):
        text = (ROOT / "commands" / (name + ".md")).read_text(encoding="utf-8")
        assert "cod-ag:cod-ag-orchestrator" in text


def test_abort_command_protects_committed_work():
    text = (ROOT / "commands" / "cod-ag-abort.md").read_text(encoding="utf-8")
    assert "confirm" in text.lower()
    assert "--delete-branches" in text


# -- skills ----------------------------------------------------------------


def test_every_skill_exists():
    assert {p.parent.name for p in SKILLS} == {"cod-ag-orchestrator", "cod-ag-conventions"}


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(path):
    fields, body = frontmatter(path)
    assert fields.get("name") == path.parent.name, "name must match the directory"
    assert len(fields.get("description", "")) > 40
    assert len(body.strip()) > 1000


def test_orchestrator_names_every_agent():
    text = (ROOT / "skills" / "cod-ag-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
    for path in AGENTS:
        assert path.stem in text, "orchestrator never dispatches {}".format(path.stem)


def test_orchestrator_states_the_parallel_dispatch_rule():
    """One message per wave is the whole point; it must be unmissable."""
    text = (ROOT / "skills" / "cod-ag-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
    assert "single message" in text
    assert "parallel" in text


# -- cross-references ------------------------------------------------------


def test_every_referenced_agent_exists():
    """No dispatch target may be a typo."""
    known = {p.stem for p in AGENTS}
    for path in MARKDOWN:
        for name in re.findall(r"\bcodag-[a-z]+\b", path.read_text(encoding="utf-8")):
            assert name in known, "{} references unknown agent {}".format(path.name, name)


def test_every_referenced_skill_exists():
    known = {p.parent.name for p in SKILLS}
    for path in MARKDOWN:
        for name in re.findall(r"cod-ag:([a-z-]+)", path.read_text(encoding="utf-8")):
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

    pattern = re.compile(r"codag\.py\"?\s+([a-z-]+)")
    for path in MARKDOWN:
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            assert name in known, "{} calls unknown command 'codag {}'".format(path.name, name)


def test_documented_cli_examples_are_real_commands():
    module = cli_module()
    parser = module.build_parser()
    top = next(a for a in parser._actions if a.dest == "command")
    documented = re.findall(
        r"^\| `([a-z-]+)[^`]*` \|",
        (ROOT / "skills" / "cod-ag-conventions" / "SKILL.md").read_text(encoding="utf-8"),
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
    from codag import miniyaml
    from codag.run import DEFAULT_CONFIG

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
