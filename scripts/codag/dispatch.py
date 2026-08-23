"""Render the prompt for each agent dispatch to a file.

The orchestrator's only job at dispatch time is
``Agent(subagent_type=..., model=..., prompt="Read <path>")``. Nothing about
what an agent is told depends on the orchestrator's model, and no brief,
diff or report is ever inlined into a prompt the orchestrator has to hold in
context.

Every prompt ends with the literal command the agent runs to report back, so
results reach ``tasks.yaml`` through the CLI instead of through prose the
orchestrator would have to parse.
"""

from __future__ import annotations

import pathlib
import sys

from . import _scribe_prompt, osenv, progress

#: Absolute path to the CLI, resolved from this file so a rendered command
#: is correct regardless of the agent's working directory or PATH.
CLI = pathlib.Path(__file__).resolve().parent.parent / "codag.py"


def command(run, *args):
    """Render a runnable ``codag`` invocation with everything pinned.

    The interpreter is the one running right now, and ``--repo`` is
    explicit, so the command works from an executor's worktree, from any
    shell, on any platform.
    """
    parts = ['"{}"'.format(sys.executable), '"{}"'.format(CLI), "--repo", '"{}"'.format(run.repo)]
    parts += ["--run", run.run_id]
    parts += [_quote(str(a)) for a in args]
    return " ".join(parts)


def _quote(value):
    return '"{}"'.format(value) if " " in value else value


def dispatch_dir(run):
    target = run.cycle_dir() / "dispatch"
    target.mkdir(parents=True, exist_ok=True)
    return target


def write(run, name, text):
    """Persist a rendered prompt. Returns its path."""
    path = dispatch_dir(run) / "{}.md".format(name)
    osenv.write_text(path, text.rstrip() + "\n")
    return path


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------


def planner(run, round_no, forced=False, validator_errors=None, revision=None):
    lines = []
    add = lines.append

    add("# Planner dispatch - round {}".format(round_no))
    add("")
    add("Run `{}`, cycle {}.".format(run.run_id, run.cycle))
    add("")
    add("## Your inputs")
    add("")
    add("- Spec (read the `## Clarifications` sections; those are already answered): `{}`".format(run.spec_path))
    add("- Detected stack: `{}`".format(run.stack_path))
    if progress.entries(run.repo):
        add("- What earlier runs learnt about this codebase - read this before")
        add("  exploring, it will save you time and stop you asking a question")
        add("  someone already answered:")
        add("")
        add("        {}".format(command(run, "progress", "show")))
    add("")

    if validator_errors:
        add("## The plan you wrote does not validate")
        add("")
        add("Fix exactly these, change nothing else, and rewrite `{}`:".format(run.tasks_path))
        add("")
        for error in validator_errors:
            add("- {}".format(error))
        add("")
        add("Attempt {} of {}.".format(run.plan_fix_attempts + 1, run.config.get("max_plan_fix_attempts", 2)))
        add("")
        add("Return `PLAN` when the validator passes.")
        return "\n".join(lines)

    if revision:
        add("## The user asked for changes to your plan")
        add("")
        add(revision)
        add("")
        add("Rewrite `{}` accordingly and return `PLAN`.".format(run.tasks_path))
        return "\n".join(lines)

    add("## What to do")
    add("")
    if forced:
        add("**This is the final round. You must return `PLAN`.** Record anything")
        add("still unresolved in the plan's `assumptions:` list with the value you")
        add("chose, rather than asking about it.")
    else:
        add("Grill first. If anything material is ambiguous, return `QUESTIONS`;")
        add("otherwise write the plan and return `PLAN`. Rounds used so far: {} of {}.".format(
            run.grill_rounds, run.config.get("max_grill_rounds", 3)
        ))
    add("")
    add("### If you have questions")
    add("")
    add("Write them to this exact path, then return the single line `QUESTIONS`:")
    add("")
    add("    {}".format(questions_path(run, round_no)))
    add("")
    add("Do **not** write `tasks.yaml` in the same round as questions.")
    add("")
    add("Format (at most 8, most-blocking first):")
    add("")
    add("```yaml")
    add("round: {}".format(round_no))
    add("questions:")
    add("  - id: Q1")
    add("    topic: scope            # scope | edges | architecture | non-functional")
    add("    blocking: true")
    add('    question: "One sentence, ending in a question mark."')
    add('    context: "What you found in the repo that makes this a real choice."')
    add("    options:")
    add('      - label: "Short label, 1-5 words"')
    add('        detail: "What this means and what it costs."')
    add('      - label: "The other one"')
    add('        detail: "Trade-off."')
    add('    recommended: "Short label, 1-5 words"')
    add("```")
    add("")
    add("`recommended` must match one option's `label` exactly. Two to four")
    add("options per question.")
    add("")
    add("### If you are ready to plan")
    add("")
    add("Write the plan to `{}` and return the single line `PLAN`.".format(run.tasks_path))
    add("")
    add("Check it yourself first:")
    add("")
    add("    {}".format(command(run, "plan", "validate")))
    return "\n".join(lines)


def questions_path(run, round_no):
    return run.cycle_dir() / "questions-round-{}.yaml".format(round_no)


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------


def executor(run, doc, slice_id, stack=None):
    item = _slice(doc, slice_id)
    lines = []
    add = lines.append

    add("# Executor dispatch - slice {}".format(slice_id))
    add("")
    add("Feature: {}".format(doc.get("goal", "")))
    add("Your slice: {}".format(item.get("intent") or item.get("title", "")))
    add("")
    add("## Read this first - it is your requirements")
    add("")
    add("    {}".format(run.brief_path(slice_id)))
    add("")
    add("Use the exact values it names. Work only inside the paths it lists as")
    add("owned, in the worktree below - you are already on the right branch.")
    add("")
    add("    {}".format(item.get("worktree") or "(run 'worktree create' first)"))
    add("")

    inherited = _inherited_interfaces(doc, item)
    if inherited:
        add("## Already built by the slices you depend on")
        add("")
        add("These exist now; code against them exactly as written:")
        add("")
        for slice_ref, interface in inherited:
            add("- `{}`  (from {})".format(interface, slice_ref))
        add("")

    notes = item.get("notes")
    if notes:
        add("## Notes from the planner")
        add("")
        add(str(notes))
        add("")

    add("## Report")
    add("")
    add("Write your full report to:")
    add("")
    add("    {}".format(run.report_path(slice_id)))
    add("")
    add("Then run **exactly this**, from anywhere:")
    add("")
    add("    {}".format(
        command(run, "report", "--slice", slice_id, "--status", "DONE", "--tests", "<one line>")
    ))
    add("")
    add("It refuses a `DONE` you have not earned: it checks your worktree is")
    add("clean, that HEAD has moved, and that the test files the brief names")
    add("exist. If it rejects you, fix what it names and run it again.")
    add("")
    add("If you cannot finish, report the truth instead:")
    add("")
    add("    {}".format(
        command(run, "report", "--slice", slice_id, "--status", "BLOCKED", "--reason", "<why>")
    ))
    add("")
    add("Valid statuses: `DONE`, `DONE_WITH_CONCERNS` (add `--concerns`),")
    add("`NEEDS_CONTEXT` (add `--reason`), `BLOCKED` (add `--reason`).")
    add("")
    add("Return to the orchestrator only the status line. Nothing else - the")
    add("report file and the CLI carry everything that matters.")
    return "\n".join(lines)


def _inherited_interfaces(doc, item):
    published = {}
    for other in doc.get("slices") or []:
        if not isinstance(other, dict):
            continue
        for interface in other.get("interfaces") or []:
            published[str(interface).strip()] = other.get("id")

    out = []
    wanted = [str(i).strip() for i in (item.get("uses_interfaces") or [])]
    for interface in wanted:
        out.append((published.get(interface, "?"), interface))
    if not wanted:
        for dep in item.get("depends_on") or []:
            for other in doc.get("slices") or []:
                if isinstance(other, dict) and other.get("id") == dep:
                    for interface in other.get("interfaces") or []:
                        out.append((dep, str(interface).strip()))
    return out


# --------------------------------------------------------------------------
# synthesizer, verifier, replanner
# --------------------------------------------------------------------------


def synthesizer(run, doc, merge_state):
    lines = []
    add = lines.append

    add("# Synthesizer dispatch - cycle {}".format(run.cycle))
    add("")
    add("The mechanical merge stopped. Make the slices work together; do not")
    add("write features.")
    add("")
    add("## Where")
    add("")
    add("    {}".format(merge_state.get("worktree", "")))
    add("")
    add("Integration branch `{}`, based on `{}`.".format(run.integration_branch, run.base_commit[:12]))
    add("")
    add("## What stopped")
    add("")
    add("Slice `{}` conflicts in:".format(merge_state.get("conflicted")))
    add("")
    for conflict in merge_state.get("conflicts") or []:
        add("- `{}`".format(conflict))
    add("")
    add("Already merged: {}".format(", ".join(merge_state.get("merged") or []) or "nothing"))
    add("Still queued: {}".format(", ".join(merge_state.get("pending") or []) or "nothing"))
    add("")
    add("## Read")
    add("")
    add("- Merge report (fill in its justification table): `{}`".format(
        run.cycle_dir() / "merge-report.md"
    ))
    add("- The plan, for what each slice was meant to do: `{}`".format(run.tasks_path))
    add("")
    add("## Method")
    add("")
    add("Resolve so **both** slices' intent survives, remove every conflict")
    add("marker, then continue the merge:")
    add("")
    add("    {}".format(command(run, "merge", "--continue")))
    add("")
    add("Repeat until it reports `clean`. Then report:")
    add("")
    add("    {}".format(command(run, "report", "--role", "synthesizer", "--status", "CLEAN")))
    add("")
    add("If the slices genuinely contradict each other - if making them work")
    add("together means deciding which one is right - do not decide. Report:")
    add("")
    add("    {}".format(
        command(run, "report", "--role", "synthesizer", "--status", "ESCALATE", "--detail", "<what disagrees>")
    ))
    return "\n".join(lines)


def verifier(run, package):
    lines = []
    add = lines.append

    add("# Verifier dispatch - cycle {}".format(run.cycle))
    add("")
    add("Judge whether this run is done. Fix nothing.")
    add("")
    add("## Read all of these")
    add("")
    add("- Gate results (already classified against the baseline): `{}`".format(package["gates"]))
    add("- The whole integration diff: `{}`".format(package["review"]))
    add("- The plan and its acceptance criteria: `{}`".format(package["tasks"]))
    add("- The spec, including the user's clarifications: `{}`".format(package["spec"]))
    add("- The merge report and its justification table: `{}`".format(package["merge_report"]))
    add("")
    add("Integration worktree, if you need a file's full context:")
    add("")
    add("    {}".format(package["worktree"]))
    add("")
    add("## Criteria to judge")
    add("")
    add("{} across {} slices. Every one needs a verdict and evidence.".format(
        len(package.get("criteria") or []), len({c["slice"] for c in package.get("criteria") or []})
    ))
    assumptions = package.get("assumptions") or []
    if assumptions:
        add("")
        add("Restate these recorded assumptions in your verdict so the human sees")
        add("what was decided on their behalf:")
        add("")
        for assumption in assumptions:
            add("- {}".format(assumption))
    add("")
    add("## Write your verdict to")
    add("")
    add("    {}".format(run.cycle_dir() / "verdict.md"))
    add("")
    add("Its final line must be exactly `VERDICT: PASS` or `VERDICT: FAIL`.")
    add("")
    add("Then run:")
    add("")
    add("    {}".format(command(run, "verdict")))
    add("")
    add("which reads that line back and moves the run on. Return only the")
    add("verdict line to the orchestrator.")
    return "\n".join(lines)


def e2e(run, doc, package, profile=None):
    """Prompt for the agent that proves the finished feature from outside."""
    profile = profile or {}
    commands = profile.get("commands") or {}
    lines = []
    add = lines.append

    add("# End-to-end dispatch - cycle {}".format(run.cycle))
    add("")
    add("The feature is built, merged and has passed verification. Prove it")
    add("works end to end, the way a user would exercise it.")
    add("")
    add("## Where")
    add("")
    add("    {}".format(package.get("worktree", "")))
    add("")
    add("Every slice is already merged there, on `{}`.".format(run.integration_branch))
    add("")

    add("## What the test must demonstrate")
    add("")
    add("The acceptance criteria, and nothing else. Read:")
    add("")
    add("- The spec, including the user's clarifications: `{}`".format(run.spec_path))
    add("- The plan's criteria: `{}`".format(run.tasks_path))
    add("")
    for criterion in package.get("criteria") or []:
        add("- **{} {}**: {}".format(criterion.get("slice"), criterion.get("id"), criterion.get("text")))
    add("")
    add("**Do not read the diff to decide what to assert.** A test written")
    add("from the implementation only restates it, and passes even when the")
    add("feature is wrong. Assert what was asked for; if the code does not do")
    add("it, that is the finding.")
    add("")

    add("## How to write it")
    add("")
    if profile.get("e2e_framework"):
        add("This project uses **{}**. Use it.".format(profile["e2e_framework"]))
        if commands.get("e2e"):
            add("")
            add("    {}".format(_render(commands["e2e"])))
    else:
        add("No end-to-end runner is installed. **Do not add one** - the plan's")
        add("no-new-dependency constraints still bind. Write the highest-level")
        add("test the existing runner can reach: drive the CLI, call the HTTP")
        add("handler, exercise the exported entry point. One test that goes")
        add("through the real path beats three that mock it.")
    if commands.get("test"):
        add("")
        add("Existing test command: `{}`".format(_render(commands["test"])))
    add("")
    add("One test, or a small suite, covering the feature's user-visible path.")
    add("Not a unit test per slice - those exist already and were enforced.")
    add("")
    add("**Test files only.** Touching production code would invalidate the")
    add("verdict this feature just earned. If the only way to make the test")
    add("pass is to change the implementation, stop and report FAILED.")
    add("")
    add("Run it. A test that has never executed is a guess. Then commit on the")
    add("integration branch with a `test:` prefix.")
    add("")

    add("## Report")
    add("")
    add("Green:")
    add("")
    add("    {}".format(command(run, "report", "--role", "e2e", "--status", "PASS", "--tests", "<one line>")))
    add("")
    add("Nothing here can reach the feature (a GUI with no runner, say):")
    add("")
    add("    {}".format(command(run, "report", "--role", "e2e", "--status", "SKIPPED", "--detail", "<why>")))
    add("")
    add("The feature is genuinely broken end to end - only after you have")
    add("satisfied yourself the fault is not in your own test:")
    add("")
    add("    {}".format(command(run, "report", "--role", "e2e", "--status", "FAILED", "--detail", "<criterion and what happened>")))
    add("")
    add("A FAILED ends the run, so be sure. Debug your test first.")
    return "\n".join(lines)


def scribe(run, doc, criteria, merge_state=None):
    """Prompt for the agent that writes the run up in the progress log."""
    lines = []
    add = lines.append
    entry_path = run.cycle_dir() / "progress-entry.md"

    add("# Progress dispatch - {}".format(run.run_id))
    add("")
    add(_scribe_prompt.INTRO)

    add("## What this run did")
    add("")
    add("Goal: {}".format((doc or {}).get("goal", "")))
    add("Kind: {}".format(run.kind(doc)))
    add("Branch: `{}`, off `{}`".format(run.integration_branch, run.state.get("base_branch")))
    add("Cycles: {}".format(run.cycle))
    add("")

    add("## Read these before writing")
    add("")
    add("- The spec, with the user's clarifications: `{}`".format(run.spec_path))
    add("- The plan and its outcomes: `{}`".format(run.tasks_path))
    add("- What each executor reported: `{}`".format(run.cycle_dir() / "reports"))
    add("- The verdict: `{}`".format(run.cycle_dir() / "verdict.md"))
    add("- The merge report: `{}`".format(run.cycle_dir() / "merge-report.md"))
    add("- The ledger, for the order things happened in: `{}`".format(run.ledger_path))
    if run.cycle > 1:
        add("- Earlier cycles under `{}` - a cycle that failed is where the".format(run.root))
        add("  most useful learnings usually are")
    add("")
    add("Existing entries, so you do not repeat a learning already recorded:")
    add("")
    add("    {}".format(command(run, "progress", "show")))
    add("")

    add("## What to write")
    add("")
    add("Write the body of the entry - no heading, no date, no separator; the")
    add("command adds those - to:")
    add("")
    add("    {}".format(entry_path))
    add("")
    add("Shape:")
    add("")
    add("```")
    add(progress.template())
    add("```")
    add("")
    add(_scribe_prompt.LEARNINGS)
    add("")

    add("## Then record it")
    add("")
    add("    {}".format(command(run, "progress", "append", "--body", str(entry_path))))
    add("")
    add("It appends; it never rewrites what is already in the file. If there is")
    add("genuinely nothing worth recording:")
    add("")
    add("    {}".format(command(run, "report", "--role", "scribe", "--status", "SKIPPED", "--detail", "<why>")))
    add("")
    add("Return one line.")
    return "\n".join(lines)


def _render(argv):
    return " ".join(str(part) for part in argv) if argv else "(none detected)"


def replanner(run, previous_cycle):
    lines = []
    add = lines.append

    add("# Replanner dispatch - cycle {}".format(run.cycle))
    add("")
    add("The previous cycle failed verification. Diagnose the root cause, then")
    add("write the plan that fixes it.")
    add("")
    add("## Read")
    add("")
    previous = run.cycle_dir(previous_cycle)
    add("- The verdict: `{}`".format(previous / "verdict.md"))
    add("- The gate results: `{}`".format(previous / "gates.json"))
    add("- What was actually built: `{}`".format(previous / "review.diff"))
    add("- The executors' own reports: `{}`".format(previous / "reports"))
    add("- The plan that produced it: `{}`".format(run.tasks_path))
    add("")
    add("## Write")
    add("")
    add("Rewrite `{}` for cycle {}.".format(run.tasks_path, run.cycle))
    add("")
    add("Slices already marked `carried` passed and are merged; leave them")
    add("exactly as they are so nothing re-executes them. Add only remedial")
    add("slices, `status: pending`, obeying the same ownership and acceptance")
    add("rules as any plan.")
    add("")
    add("Validate before you return:")
    add("")
    add("    {}".format(command(run, "plan", "validate")))
    add("")
    add("Return the single line `REPLAN`.")
    return "\n".join(lines)


def _slice(doc, slice_id):
    for item in doc.get("slices") or []:
        if isinstance(item, dict) and item.get("id") == slice_id:
            return item
    raise KeyError("no slice {!r} in the plan".format(slice_id))
