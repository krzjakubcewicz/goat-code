"""Slice briefs: everything one executor needs, and nothing else.

Handing an agent a file path instead of pasted text keeps the
orchestrator's own context small - the brief never enters it. The brief is
the single source of requirements for its slice: exact values live here,
not in the dispatch prompt.
"""

from __future__ import annotations

import pathlib

from . import osenv, stack as stackmod

_TDD_RULES = """\
1. Write the failing test first. Run it. Watch it fail for the right reason.
2. Write the minimum code that makes it pass. Run it again.
3. Refactor only with the test green.
4. Commit. One commit per green test, message in caveman style
   (terse, lowercase, no filler - "add token expiry check", not
   "This commit adds a check for token expiry.").
Never write implementation before a failing test exists for it."""

_OWNERSHIP_RULES = """\
- Create and edit files ONLY inside your owned paths listed above.
- Files under "shared paths" are append-only: add your entry, never
  reorder, rewrite or delete anyone else's.
- Never touch a path owned by another slice, even to fix an obvious bug
  there. Report it in your concerns instead - a parallel executor is
  editing that file right now and your change would be lost at merge.
- Do not run git merge, rebase, push, or switch branches. You are already
  on your branch in your own worktree."""


def build(run, doc, slice_id, stack_profile=None):
    """Render the brief markdown for one slice."""
    item = _slice(doc, slice_id)
    profile = stack_profile
    if profile is None and pathlib.Path(run.stack_path).exists():
        profile = osenv.read_json(run.stack_path)
    profile = profile or {}

    lines = []
    add = lines.append

    add("# Slice {}: {}".format(slice_id, item.get("title", "")))
    add("")
    add("Run `{}`, cycle {}. This brief is your requirements.".format(run.run_id, run.cycle))
    add("Use the exact values written here; do not substitute your own.")
    add("")

    add("## Where this fits")
    add("")
    add("Project goal: {}".format(doc.get("goal", "")))
    if item.get("intent"):
        add("")
        add("Your slice delivers: {}".format(item["intent"]))
    add("")
    add(
        "This is a vertical slice. It must build, test and stand on its own - "
        "not a layer waiting for another slice to become useful."
    )
    add("")

    _section_stack(add, profile)
    _section_constraints(add, doc)
    _section_ownership(add, item)
    _section_interfaces(add, doc, item)
    _section_acceptance(add, item)
    _section_tests(add, item)
    _section_scope(add, item)
    _section_method(add, run, profile)
    _section_report(add, run, slice_id)

    return "\n".join(lines).rstrip() + "\n"


def write(run, doc, slice_id, stack_profile=None, path=None):
    """Render and persist the brief. Returns the path written."""
    target = pathlib.Path(path) if path else run.brief_path(slice_id)
    osenv.write_text(target, build(run, doc, slice_id, stack_profile))
    return target


# -- sections --------------------------------------------------------------


def _section_stack(add, profile):
    add("## Stack")
    add("")
    if not profile:
        add("Not detected. Read the repository and follow its existing conventions.")
        add("")
        return
    add("- Profile: {}".format(stackmod.summary_line(profile)))
    commands = profile.get("commands") or {}
    for name in ("setup", "build", "typecheck", "lint", "test"):
        if commands.get(name):
            add("- {}: `{}`".format(name.capitalize(), stackmod.command_text(commands[name])))
    skills = profile.get("specialist_skills") or []
    if skills:
        add("")
        add("Load the `{}` skill before you start; write code the way that".format(skills[0]))
        add("skill and this repository's existing patterns dictate.")
    if profile.get("monorepo"):
        add("")
        add("Monorepo ({}): run commands from the repository root unless the".format(profile["monorepo"].get("kind")))
        add("package you are touching defines its own scripts.")
    add("")


def _section_constraints(add, doc):
    constraints = doc.get("global_constraints") or []
    assumptions = doc.get("assumptions") or []
    if not constraints and not assumptions:
        return
    add("## Global constraints")
    add("")
    add("These bind every slice. Treat them as part of your requirements.")
    add("")
    for constraint in constraints:
        add("- {}".format(constraint))
    if assumptions:
        add("")
        add("Recorded assumptions (the user did not specify; do not contradict them):")
        for assumption in assumptions:
            add("- {}".format(assumption))
    add("")


def _section_ownership(add, item):
    add("## Files you own")
    add("")
    for pattern in item.get("owns") or []:
        add("- `{}`".format(pattern))
    shared = item.get("touches_shared") or []
    if shared:
        add("")
        add("Shared paths (append-only, other slices write here too):")
        for pattern in shared:
            add("- `{}`".format(pattern))
    add("")
    add(_OWNERSHIP_RULES)
    add("")


def _section_interfaces(add, doc, item):
    provides = item.get("interfaces") or []
    uses = item.get("uses_interfaces") or []
    if not provides and not uses:
        return
    add("## Interfaces")
    add("")
    if provides:
        add("You must publish exactly these signatures - other slices are being")
        add("written against them right now, so the names and shapes are fixed:")
        add("")
        for interface in provides:
            add("- `{}`".format(interface))
    if uses:
        providers = _providers(doc)
        add("")
        add("You consume these, already built by the slices you depend on:")
        add("")
        for interface in uses:
            owner = providers.get(str(interface).strip())
            suffix = " (from {})".format(owner) if owner else ""
            add("- `{}`{}".format(interface, suffix))
    add("")


def _section_acceptance(add, item):
    add("## Acceptance criteria")
    add("")
    add("A verifier will check each of these against the merged diff. Every one")
    add("must be demonstrably true when you finish.")
    add("")
    for criterion in item.get("acceptance") or []:
        if isinstance(criterion, dict):
            add("- **{}**: {}".format(criterion.get("id", "?"), criterion.get("text", "")))
        else:
            add("- {}".format(criterion))
    add("")


def _section_tests(add, item):
    tests = item.get("tests") or []
    if not tests:
        return
    add("## Tests to write")
    add("")
    for entry in tests:
        if isinstance(entry, dict):
            add("- `{}`".format(entry.get("path", "")))
            for topic in entry.get("must_cover") or []:
                add("  - must cover: {}".format(topic))
        else:
            add("- `{}`".format(entry))
    add("")


def _section_scope(add, item):
    out_of_scope = item.get("out_of_scope") or []
    add("## Out of scope")
    add("")
    if out_of_scope:
        for entry in out_of_scope:
            add("- {}".format(entry))
        add("")
    add("Build what the acceptance criteria require and stop. No extra flags,")
    add("no speculative abstractions, no adjacent improvements. Anything not")
    add("required above is scope creep and the verifier will flag it.")
    add("")


def _section_method(add, run, profile):
    add("## Method: test-driven, committed in small steps")
    add("")
    add(_TDD_RULES)
    add("")
    commands = (profile or {}).get("commands") or {}
    if commands.get("test"):
        add("Run your tests with: `{}`".format(stackmod.command_text(commands["test"])))
    for name in ("typecheck", "lint", "build"):
        if commands.get(name):
            add("Before you report DONE, {} must pass: `{}`".format(name, stackmod.command_text(commands[name])))
    add("")


def _section_report(add, run, slice_id):
    add("## What to report")
    add("")
    add("Write your full report to:")
    add("")
    add("    {}".format(run.report_path(slice_id)))
    add("")
    add("Return to the orchestrator ONLY: a status line, the commit range, a")
    add("one-line test summary, and any concerns. Everything else goes in the")
    add("report file.")
    add("")
    add("Status is exactly one of:")
    add("")
    add("- `DONE` - all acceptance criteria met, tests green, work committed")
    add("- `DONE_WITH_CONCERNS` - finished, but something needs a human's eye")
    add("- `NEEDS_CONTEXT` - you are missing information; say precisely what")
    add("- `BLOCKED` - you cannot finish; say precisely why")
    add("")
    add("Do not report DONE with failing tests or uncommitted changes.")


def _providers(doc):
    out = {}
    for item in doc.get("slices") or []:
        if not isinstance(item, dict):
            continue
        for interface in item.get("interfaces") or []:
            out[str(interface).strip()] = item.get("id")
    return out


def _slice(doc, slice_id):
    for item in doc.get("slices") or []:
        if isinstance(item, dict) and item.get("id") == slice_id:
            return item
    raise KeyError("no slice {!r} in the plan".format(slice_id))
