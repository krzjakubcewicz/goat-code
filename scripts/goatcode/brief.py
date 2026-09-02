"""Slice briefs: everything one executor needs, and nothing else.

Handing an agent a file path instead of pasted text keeps the
orchestrator's own context small - the brief never enters it. The brief is
the single source of requirements for its slice: exact values live here,
not in the dispatch prompt.
"""

from __future__ import annotations

import pathlib

from . import osenv, stack as stackmod, tasks

_TDD_RULES = """\
1. Write the failing test first. Run it. Watch it fail for the right reason.
2. Write the minimum code that makes it pass. Run it again.
3. Refactor only with the test green.
4. Commit. One commit per green test, message in caveman style
   (terse, lowercase, no filler - "add token expiry check", not
   "This commit adds a check for token expiry.").
Never write implementation before a failing test exists for it.

This is checked, not just asked for. When you report DONE, your commit
history is read: if a commit adds implementation before this slice has
touched any test, the report is refused. A commit adding a test and its
implementation together is fine. Opening with a fixture or a config file
is fine. Writing the whole feature and adding tests at the end is not."""

_OWNERSHIP_RULES = """\
- Create and edit files ONLY inside your owned paths listed above.
- Files under "shared paths" are append-only: add your entry, never
  reorder, rewrite or delete anyone else's.
- Never touch a path owned by another slice, even to fix an obvious bug
  there. Report it in your concerns instead - a parallel executor is
  editing that file right now and your change would be lost at merge.
- Do not run git merge, rebase, push, or switch branches. You are already
  on your branch in your own worktree."""

_EVIDENCE_STANDARD = """\
A criterion is met when a named test would fail if the behaviour were wrong.
Not when the code looks right, and not when a test near it passes. This is
the same standard the verifier judges by - see the Evidence standard in
`goat-code:goat-code-conventions` for the full version.

- **No test, not met.** A passing assertion is the evidence, not the code.
- **A test that asserts nothing is not a test.** Ask whether it could fail.
- **Exact values, literally.** The error string, status code or boundary the
  criterion names - not a paraphrase, not a type check, not "contains".
- **Exact counts.** `== 1` when the criterion says exactly one. `>= 1` passes
  while the behaviour is wrong.
- **Read back through the real surface.** For a criterion about what an
  endpoint returns, assert a second GET - not an in-memory object that never
  reached the database.
- **Drive the thing the criterion names.** A UI branch or event listener
  needs the component rendered and driven; a thorough test of the helper
  underneath does not cover the branch that calls it.
- **Placement, not just counts.** "Grouped under its exercise" means
  asserting the row is inside that group.
- **Every clause.** "null on the second call" needs a test for the second
  call. Four clauses need four assertions."""


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
    _section_method(add, run, profile, item)
    _section_report(add, run, slice_id, item)

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
    add("### The evidence bar")
    add("")
    add(_EVIDENCE_STANDARD)
    add("")
    ids = tasks.criterion_ids(item)
    if ids:
        add("When you report DONE you must name, for each of {}, the test".format(", ".join(ids)))
        add("`path:line` that would fail if the behaviour were wrong. If you cannot")
        add("name one, that criterion is not met - write the test.")
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


def _section_method(add, run, profile, item):
    add("## Method: test-driven, committed in small steps")
    add("")
    add(_TDD_RULES)
    add("")
    commands = (profile or {}).get("commands") or {}
    _section_test_commands(add, commands, item)
    for name in ("typecheck", "lint", "build"):
        if commands.get(name):
            add("Before you report DONE, {} must pass: `{}`".format(name, stackmod.command_text(commands[name])))
    add("")


def _section_test_commands(add, commands, item):
    """The loop command first, the whole suite once.

    Executors launched 909 test containers across the recorded runs, and one
    ran the identical full-suite command 25 times - because the brief named
    the suite and nothing else, so every red-green iteration paid for it.
    """
    suite = commands.get("test")
    per_file = _per_file_commands(commands.get("test_one"), item)
    if per_file:
        add("While you work, run only the test you are on:")
        add("")
        for line in per_file:
            add("    {}".format(line))
        add("")
        if suite:
            add("Run the whole suite once before you report: `{}`.".format(
                stackmod.command_text(suite)
            ))
            add("A green slice that breaks something else is not done.")
    elif suite:
        add("Run your tests with: `{}`".format(stackmod.command_text(suite)))


def _section_report(add, run, slice_id, item=None):
    add("## What to report")
    add("")
    add("Write your full report to:")
    add("")
    add("    {}".format(run.report_path(slice_id)))
    add("")
    ids = tasks.criterion_ids(item or {})
    if ids:
        add("A `DONE` carries one `--evidence` flag per criterion:")
        add("")
        for cid in ids:
            add("    --evidence {}=<path>:<line>".format(cid))
        add("")
        add("The path is resolved inside your worktree and the line must exist.")
        add("The exact command is in your dispatch.")
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


def _per_file_commands(template, item):
    """The per-file test command, once per test path the brief declares."""
    if not template:
        return []
    out = []
    for entry in item.get("tests") or []:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if not path:
            continue
        argv = [str(part).replace(stackmod.PATH_TOKEN, str(path)) for part in template]
        out.append(stackmod.command_text(argv))
    return out


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
