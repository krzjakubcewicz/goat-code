"""The pipeline's control flow, as code rather than prose.

``next_action`` reads the run's state off disk and returns the single thing
to do next. The orchestrator model does not decide what comes next, when a
cap fires, which agent runs on which model, or what an agent is told - it
invokes the tool the action names and calls back.

Everything here is derived from files, so a whole run can be driven in a
test with no model involved.
"""

from __future__ import annotations

import pathlib

import sys

from . import debuglog, dispatch, merge, miniyaml, progress, report, schema, tasks

#: Actions the orchestrator knows how to perform.
ACTIONS = ("run", "dispatch", "ask", "escalate", "stop")


class Evidence:
    """Everything on disk that the phase depends on, read once."""

    def __init__(self, run):
        self.run = run
        self.tasks_path = pathlib.Path(run.tasks_path)
        self.doc = None
        self.parse_error = None
        self.validation = None

        if self.tasks_path.exists():
            try:
                self.doc = tasks.load(self.tasks_path)
            except (tasks.TaskError, miniyaml.YamlError) as exc:
                self.parse_error = str(exc)
            else:
                self.validation = schema.validate(self.doc)

        self.round = run.grill_rounds + 1
        self.questions = dispatch.questions_path(run, self.round)
        self.verdict = report.read_verdict(run)
        self.merge_state = merge.state_of(run)
        self.e2e = run.state.get("e2e") or {}
        self.progress = run.state.get("scribe") or {}

    @property
    def has_plan(self):
        """The planner has produced something - parseable or not."""
        return self.tasks_path.exists()

    @property
    def plan_valid(self):
        return self.validation is not None and self.validation.ok

    @property
    def plan_errors(self):
        if self.parse_error:
            return [self.parse_error]
        return list(self.validation.errors) if self.validation else []

    @property
    def ready(self):
        return tasks.ready(self.doc) if self.doc else []

    @property
    def blocked(self):
        if not self.doc:
            return []
        return [s["id"] for s in tasks.slices(self.doc) if s.get("status") == "blocked"]

    @property
    def unfinished(self):
        return tasks.remaining(self.doc) if self.doc else []


# --------------------------------------------------------------------------
# phase
# --------------------------------------------------------------------------


def derive_phase(run, evidence=None):
    """Which phase the evidence on disk says this run is in.

    Pure: no writes, no side effects. ``next_action`` persists the result so
    a stale or hand-edited ``state.json`` corrects itself.
    """
    evidence = evidence or Evidence(run)

    if run.phase in ("done", "failed", "aborted"):
        return run.phase

    if not evidence.has_plan:
        return "ask" if evidence.questions.exists() else "grill"

    if not evidence.plan_valid:
        return "plan"

    if run.approval == "revise":
        return "grill"

    if run.needs_approval():
        return "approve"

    if evidence.verdict == "PASS":
        # A feature earns an end-to-end test before the run is called done.
        # A bugfix does not: its slices already had to be written test-first.
        if run.wants_e2e(evidence.doc) and not evidence.e2e.get("status"):
            return "e2e"
        if evidence.e2e.get("status") == "FAILED":
            return "failed"
        # One entry per completed run, with the learnings a later run would
        # otherwise have to rediscover.
        if run.wants_progress() and not evidence.progress.get("status"):
            return "record"
        return "done"
    if evidence.verdict == "FAIL":
        return "replan"
    # Only owed a replan while the plan has nothing to execute. Once the
    # replanner has added remedial slices, the flag is stale and executing
    # them is what clears it.
    if run.state.get("awaiting_replan") and not evidence.ready:
        return "replan"

    if evidence.ready or evidence.blocked:
        return "execute"

    if evidence.merge_state.get("status") in ("clean", "empty"):
        return "verify"

    if evidence.unfinished and not evidence.ready:
        # Everything left is waiting on something that failed; there is no
        # more executing to do, so integrate what did land.
        return "synthesize"

    return "synthesize"


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------


def _action(run, kind, reason, message, **extra):
    payload = {
        "run_id": run.run_id,
        "phase": run.phase,
        "cycle": run.cycle,
        "action": kind,
        "reason": reason,
        "message": message,
        "commands": [],
        "dispatches": [],
        "ask": None,
    }
    payload.update(extra)
    return payload


def next_action(run, stack_profile=None):
    """The single next thing to do. Persists the derived phase."""
    evidence = Evidence(run)
    phase = derive_phase(run, evidence)
    if phase != run.phase:
        run.set_phase(phase)

    handler = {
        "grill": _grill,
        "ask": _ask,
        "plan": _plan,
        "approve": _approve,
        "execute": _execute,
        "synthesize": _synthesize,
        "verify": _verify,
        "e2e": _e2e,
        "record": _record,
        "replan": _replan,
    }.get(phase)

    if handler is None:
        return _log_action(_stop(run, evidence))
    return _log_action(handler(run, evidence, stack_profile))


def _entry(agent, model, prompt, slice_id=None, cwd=None):
    """One dispatch.

    ``cwd`` is where a driver that spawns its own agent processes must start
    this one. Worktrees live outside the repository, so an agent given only
    the repo cannot read or write its own slice. Claude Code subagents
    inherit the main thread's directory and are told the path in their
    brief, so the plugin ignores this field.
    """
    return {
        "agent": agent,
        "model": model,
        "slice": slice_id,
        "prompt": str(prompt),
        "cwd": str(cwd) if cwd else None,
    }


def _log_action(action):
    debuglog.log(
        "action",
        kind=action.get("action"),
        phase=action.get("phase"),
        cycle=action.get("cycle"),
        reason=action.get("reason"),
    )
    for entry in action.get("dispatches") or []:
        debuglog.log(
            "dispatch", agent=entry.get("agent"), model=entry.get("model"),
            slice=entry.get("slice"), prompt=entry.get("prompt"), cwd=entry.get("cwd"),
        )
    return action


# -- grill -----------------------------------------------------------------


def _grill(run, evidence, _stack):
    revision = None
    if run.approval == "revise":
        revision = run.state.get("approval_feedback")
        run.set_approval(None)

    forced = run.grill_exhausted() and not revision
    text = dispatch.planner(run, evidence.round, forced=forced, revision=revision)
    path = dispatch.write(run, "planner-round-{}".format(evidence.round), text)

    if revision:
        reason = "the user asked for plan changes"
    elif forced:
        reason = "grill round cap reached; the planner must produce a plan"
    else:
        reason = "round {} of {}".format(evidence.round, run.config.get("max_grill_rounds", 3))

    return _action(
        run,
        "dispatch",
        reason,
        "dispatch codag-planner ({})".format(_model(run, "planner")),
        dispatches=[_entry("codag-planner", _model(run, "planner"), path)],
    )


def _ask(run, evidence, _stack):
    try:
        questions = report.load_questions(evidence.questions)
    except report.ReportError as exc:
        return _action(run, "escalate", "unreadable questions file", str(exc))

    payload = []
    for question in questions.get("questions") or []:
        if not isinstance(question, dict):
            continue
        options = []
        for option in question.get("options") or []:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label", "")).strip()
            if label and label == str(question.get("recommended", "")).strip():
                label = "{} (Recommended)".format(label)
            options.append({"label": label, "description": str(option.get("detail", ""))})
        payload.append(
            {
                "id": str(question.get("id", "?")),
                "header": str(question.get("topic", "question"))[:12],
                "question": str(question.get("question", "")),
                "context": str(question.get("context", "")),
                "options": options,
                "blocking": bool(question.get("blocking", True)),
            }
        )

    record = dispatch.command(run, "answer", "Q1=<the label the user chose>")
    return _action(
        run,
        "ask",
        "round {} has {} question(s)".format(questions.get("round", evidence.round), len(payload)),
        "put {} question(s) to the user, then record the answers".format(len(payload)),
        ask={
            "round": questions.get("round", evidence.round),
            "questions": payload,
            "record": record,
            "record_note": (
                "One QID=answer pair per question, using the label the user picked. "
                "Add --note QID=text for anything they typed. Omit a question they skipped."
            ),
        },
    )


# -- plan ------------------------------------------------------------------


def _plan(run, evidence, _stack):
    errors = evidence.plan_errors
    if run.plan_fixes_exhausted():
        return _stop(
            run,
            evidence,
            outcome="failed",
            reason="the planner could not produce a valid plan",
            details=errors,
        )

    run.bump_plan_fix()
    text = dispatch.planner(run, evidence.round, validator_errors=errors)
    path = dispatch.write(run, "planner-fix-{}".format(run.plan_fix_attempts), text)
    return _action(
        run,
        "dispatch",
        "plan does not validate ({} error(s))".format(len(errors)),
        "dispatch codag-planner to fix the plan (attempt {} of {})".format(
            run.plan_fix_attempts, run.config.get("max_plan_fix_attempts", 2)
        ),
        dispatches=[_entry("codag-planner", _model(run, "planner"), path)],
    )


def _approve(run, evidence, _stack):
    warnings = list(evidence.validation.warnings) if evidence.validation else []
    return _action(
        run,
        "ask",
        "chat-mode plan needs approval before any executor runs",
        "show the plan and ask the user to approve it",
        commands=[cli_argv(run, "plan", "show")],
        ask={
            "kind": "approval",
            "warnings": warnings,
            "assumptions": (evidence.doc or {}).get("assumptions") or [],
            "questions": [
                {
                    "id": "approve",
                    "header": "Plan",
                    "question": "Start building this plan?",
                    "options": [
                        {"label": "Approve", "description": "Run the executors on this plan."},
                        {"label": "Revise", "description": "Send it back to the planner with feedback."},
                        {"label": "Abort", "description": "Stop the run and clean up."},
                    ],
                    "blocking": True,
                }
            ],
            "record": "{}  |  {}  |  {}".format(
                dispatch.command(run, "approve", "--yes"),
                dispatch.command(run, "approve", "--revise", "<their feedback>"),
                dispatch.command(run, "approve", "--abort"),
            ),
        },
    )


# -- execute ---------------------------------------------------------------


def _execute(run, evidence, stack_profile):
    if run.state.get("awaiting_replan"):
        run.state["awaiting_replan"] = False
        run.save()

    # The branch gets its real name before a single line is written, now that
    # the plan exists and `kind` is known.
    if not run.feature_branch:
        return _action(
            run,
            "run",
            "the work needs a named branch before any code is written",
            "create the feature branch off {}".format(run.state.get("base_branch")),
            commands=[cli_argv(run, "branch")],
        )

    ready = evidence.ready
    if not ready:
        retried = _retry_blocked(run, evidence)
        if retried:
            evidence = Evidence(run)
            ready = evidence.ready
        else:
            _fail_stuck(run, evidence)
            return _synthesize(run, Evidence(run), stack_profile)

    limit = run.config.get("parallel", 3)
    batch = ready[:limit]

    missing = [s for s in batch if not _worktree_ready(evidence.doc, s)]
    if missing:
        return _action(
            run,
            "run",
            "{} slice(s) need a worktree and a brief".format(len(missing)),
            "prepare {}".format(", ".join(missing)),
            commands=[
                cli_argv(run, "worktree", "create", *missing),
                cli_argv(run, "brief", *missing),
            ],
        )

    dispatches = []
    for slice_id in batch:
        item = tasks.get(evidence.doc, slice_id)
        model = _executor_model(run, item, slice_id)
        text = dispatch.executor(run, evidence.doc, slice_id, stack_profile)
        path = dispatch.write(run, slice_id, text)
        dispatches.append(
            _entry("codag-executor", model, path, slice_id=slice_id, cwd=item.get("worktree"))
        )

    return _action(
        run,
        "dispatch",
        "wave of {} slice(s) ready".format(len(batch)),
        "dispatch all {} in ONE message so they run in parallel: {}".format(
            len(batch), ", ".join("{} ({})".format(d["slice"], d["model"]) for d in dispatches)
        ),
        dispatches=dispatches,
    )


def _worktree_ready(doc, slice_id):
    item = tasks.get(doc, slice_id)
    recorded = item.get("worktree")
    return bool(recorded) and pathlib.Path(recorded).exists()


def _retry_blocked(run, evidence):
    """Give each blocked slice exactly one go on a stronger model."""
    retried = []
    for slice_id in evidence.blocked:
        if run.escalations(slice_id) == 0:
            run.escalate(slice_id)
            tasks.set_status(run.tasks_path, slice_id, "pending")
            retried.append(slice_id)
    return retried


def _fail_stuck(run, evidence):
    """Nothing can proceed: record why, so the replanner has the facts."""
    for item in tasks.slices(evidence.doc):
        if item.get("status") in ("pending", "claimed", "blocked"):
            tasks.set_status(run.tasks_path, item["id"], "failed")


def _executor_model(run, item, slice_id):
    if run.escalations(slice_id) > 0:
        return _model(run, "executor_escalated")
    return item.get("model") or _model(run, "executor")


# -- synthesize ------------------------------------------------------------


def _synthesize(run, evidence, _stack):
    state = evidence.merge_state
    status = state.get("status", "not-started")

    if status in ("not-started", "in-progress"):
        return _action(
            run,
            "run",
            "slice branches are not merged yet",
            "merge the finished slice branches",
            commands=[cli_argv(run, "merge")],
        )

    if status == "conflict":
        text = dispatch.synthesizer(run, evidence.doc, state)
        path = dispatch.write(run, "synthesizer", text)
        return _action(
            run,
            "dispatch",
            "merge conflict in {} file(s)".format(len(state.get("conflicts") or [])),
            "dispatch codag-synthesizer ({})".format(_model(run, "synthesizer")),
            dispatches=[
                _entry(
                    "codag-synthesizer",
                    _model(run, "synthesizer"),
                    path,
                    cwd=state.get("worktree"),
                )
            ],
        )

    return _verify(run, evidence, _stack)


# -- verify ----------------------------------------------------------------


def _verify(run, evidence, _stack):
    package_path = run.cycle_dir() / "gates.json"
    review_path = run.cycle_dir() / "review.diff"
    if not package_path.exists() or not review_path.exists():
        return _action(
            run,
            "run",
            "the verifier needs gates and a review package",
            "build the verify package",
            commands=[cli_argv(run, "verify-package")],
        )

    package = {
        "gates": str(package_path),
        "review": str(review_path),
        "tasks": str(run.tasks_path),
        "spec": str(run.spec_path),
        "merge_report": str(run.cycle_dir() / "merge-report.md"),
        "worktree": evidence.merge_state.get("worktree", ""),
        "criteria": _criteria(evidence.doc),
        "assumptions": (evidence.doc or {}).get("assumptions") or [],
    }
    text = dispatch.verifier(run, package)
    path = dispatch.write(run, "verifier", text)
    return _action(
        run,
        "dispatch",
        "integration is ready to judge",
        "dispatch codag-verifier ({})".format(_model(run, "verifier")),
        dispatches=[
            _entry("codag-verifier", _model(run, "verifier"), path, cwd=package["worktree"])
        ],
    )


def _criteria(doc):
    out = []
    for item in tasks.slices(doc or {}):
        for criterion in item.get("acceptance") or []:
            if isinstance(criterion, dict):
                out.append(
                    {
                        "slice": item.get("id"),
                        "id": criterion.get("id"),
                        "text": criterion.get("text"),
                        "status": item.get("status"),
                    }
                )
    return out


# -- end to end ------------------------------------------------------------


def _e2e(run, evidence, stack_profile):
    package = {
        "worktree": evidence.merge_state.get("worktree", ""),
        "criteria": _criteria(evidence.doc),
    }
    text = dispatch.e2e(run, evidence.doc, package, stack_profile)
    path = dispatch.write(run, "e2e", text)
    return _action(
        run,
        "dispatch",
        "the feature passed verification and has no end-to-end test yet",
        "dispatch codag-e2e ({})".format(_model(run, "e2e")),
        dispatches=[_entry("codag-e2e", _model(run, "e2e"), path, cwd=package["worktree"])],
    )


# -- the progress log ------------------------------------------------------


def _record(run, evidence, stack_profile):
    text = dispatch.scribe(run, evidence.doc, _criteria(evidence.doc), evidence.merge_state)
    path = dispatch.write(run, "scribe", text)
    return _action(
        run,
        "dispatch",
        "the run is finished and has not been written up yet",
        "dispatch codag-scribe ({})".format(_model(run, "scribe")),
        dispatches=[_entry("codag-scribe", _model(run, "scribe"), path)],
    )


# -- replan ----------------------------------------------------------------


def _replan(run, evidence, _stack):
    if run.cycles_exhausted():
        return _stop(
            run,
            evidence,
            outcome="failed",
            reason="cycle cap of {} reached with criteria still unmet".format(
                run.config.get("max_cycles", 3)
            ),
        )

    if not run.state.get("awaiting_replan"):
        return _action(
            run,
            "run",
            "verification failed; starting the next cycle",
            "carry finished slices forward and open cycle {}".format(run.cycle + 1),
            commands=[cli_argv(run, "cycle")],
        )

    text = dispatch.replanner(run, run.cycle - 1)
    path = dispatch.write(run, "replanner", text)
    return _action(
        run,
        "dispatch",
        "cycle {} needs a remedial plan".format(run.cycle),
        "dispatch codag-replanner ({})".format(_model(run, "replanner")),
        dispatches=[_entry("codag-replanner", _model(run, "replanner"), path)],
    )


# -- stop ------------------------------------------------------------------


def _stop(run, evidence, outcome=None, reason=None, details=None):
    outcome = outcome or run.phase
    if outcome not in ("done", "failed", "aborted"):
        outcome = "done"
    if run.phase != outcome:
        run.set_phase(outcome)

    payload = {
        "outcome": outcome,
        "integration_branch": run.integration_branch,
        "base_commit": run.base_commit,
        "base_branch": run.state.get("base_branch"),
        "details": details or [],
        "verdict": str(run.cycle_dir() / "verdict.md"),
    }

    if outcome == "done":
        message = "\n".join(
            [
                "DONE",
                "",
                "branch: {}".format(run.integration_branch),
                "review: git diff {}..{}".format(run.base_commit[:12], run.integration_branch),
                "merge:  git merge {}".format(run.integration_branch),
                "",
                "nothing was committed to your branch {}".format(run.state.get("base_branch")),
            ]
        )
        payload["finish"] = cli_argv(run, "finish")
    else:
        message = "\n".join(
            ["STOPPED ({})".format(outcome), "", reason or "", ""]
            + ["  - {}".format(d) for d in (details or [])]
        )

    return _action(run, "stop", reason or outcome, message, **payload)


# -- helpers ---------------------------------------------------------------


def _model(run, role):
    return run.config.get("models", {}).get(role, "sonnet")


def cli_argv(run, *args):
    """A runnable command, as a list, with repo and run pinned."""
    return [sys.executable, str(dispatch.CLI), "--repo", str(run.repo), "--run", run.run_id] + [
        str(a) for a in args
    ]


def render(action):
    """Human-readable form of an action."""
    lines = [
        "phase {} (cycle {}) - {}".format(action["phase"], action["cycle"], action["reason"]),
        "",
        action["message"],
    ]
    for command in action.get("commands") or []:
        lines.append("")
        lines.append("  run: {}".format(" ".join(_quote(c) for c in command)))
    for entry in action.get("dispatches") or []:
        lines.append("")
        lines.append(
            "  dispatch: {} on {}{}".format(
                entry["agent"], entry["model"], "  [{}]".format(entry["slice"]) if entry["slice"] else ""
            )
        )
        lines.append("            prompt: {}".format(entry["prompt"]))
    ask = action.get("ask")
    if ask:
        for question in ask.get("questions") or []:
            lines.append("")
            lines.append("  ask {}: {}".format(question["id"], question["question"]))
            for option in question.get("options") or []:
                lines.append("      - {}".format(option["label"]))
        lines.append("")
        lines.append("  record with: {}".format(ask.get("record", "")))
    return "\n".join(lines)


def _quote(value):
    return '"{}"'.format(value) if " " in str(value) else str(value)


__all__ = ["ACTIONS", "Evidence", "derive_phase", "next_action", "render"]
