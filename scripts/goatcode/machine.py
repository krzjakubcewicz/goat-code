"""The pipeline's control flow, as code rather than prose.

``next_action`` reads the run's state off disk and returns the single thing
to do next. The orchestrator model does not decide what comes next, when a
cap fires, which agent runs on which model, or what an agent is told - it
invokes the tool the action names and calls back.

Everything here is derived from files, so a whole run can be driven in a
test with no model involved.
"""

from __future__ import annotations

import hashlib
import pathlib

import sys

from . import debuglog, diffpkg, dispatch, gates, ledger, merge, miniyaml, osenv, progress, report, run as runmod, schema, tasks, workflow as workflowmod

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
    def merged(self):
        """Slices whose work is on the integration branch.

        Carried ones landed in an earlier cycle; the rest are whatever this
        cycle's merge has taken so far.
        """
        carried = [s["id"] for s in tasks.slices(self.doc or {}) if s.get("status") == "carried"]
        return set(self.merge_state.get("merged") or []) | set(carried)

    @property
    def dispatchable(self):
        """Ready slices whose dependencies are merged, not merely finished.

        A worktree starts at the integration tip. Slices were merged once
        per cycle, after every wave, so a second-wave executor was told its
        dependencies' interfaces "exist now" in a tree that held none of
        their code.
        """
        if not self.doc:
            return []
        merged = self.merged
        return [
            slice_id
            for slice_id in self.ready
            if all(dep in merged for dep in tasks.get(self.doc, slice_id).get("depends_on") or [])
        ]

    @property
    def unmerged(self):
        """Slices finished this cycle that the merge has not taken yet."""
        if not self.doc:
            return []
        taken = set(self.merge_state.get("merged") or [])
        return [
            s["id"] for s in tasks.slices(self.doc) if s.get("status") == "done" and s["id"] not in taken
        ]

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

    # Sizing the task comes before deciding how much pipeline it gets - but
    # only for a run that has not planned yet. A run with an approved plan
    # already earned its workflow; re-classifying it on resume (a legacy
    # in-flight run created before the classifier existed, for instance)
    # would route it down to a cheaper path and drop the verifier and
    # approval gate it was already going through.
    if run.wants_classification() and run.classification is None and not evidence.has_plan:
        return "classify"

    if not evidence.has_plan:
        # A direct run derives `grill` like any other; `_grill` is what
        # suppresses the questions. A questions file only counts while the
        # run is still owed a round: the cap used to live in the planner's
        # prompt alone, so one that asked anyway was simply given another.
        may_ask = workflowmod.wants_grill(run.workflow) and not run.grill_exhausted()
        return "ask" if may_ask and evidence.questions.exists() else "grill"

    if not evidence.plan_valid:
        return "plan"

    if run.approval == "revise":
        return "grill"

    if workflowmod.wants_gate(run.workflow) and run.needs_approval():
        return "approve"

    if not workflowmod.wants_verifier(run.workflow):
        if evidence.merge_state.get("status") in ("clean", "empty"):
            report_path = run.cycle_dir() / "gates.json"
            if report_path.exists():
                blocking = gates.blocking(osenv.read_json(report_path) or {})
                if blocking:
                    # No replan on a direct run: it was routed here because it
                    # was small, and grinding a cycle budget on it is the cost
                    # the routing exists to avoid.
                    return "failed"
                return _after_the_work_is_judged(run, evidence)

    if evidence.verdict == "PASS":
        return _after_the_work_is_judged(run, evidence)
    if evidence.verdict == "FAIL":
        return "replan"
    # Only owed a replan while the plan has nothing to execute. Once the
    # replanner has added remedial slices, the flag is stale and executing
    # them is what clears it.
    if run.state.get("awaiting_replan") and not evidence.ready:
        return "replan"

    if evidence.dispatchable:
        return "execute"
    if evidence.ready:
        # Ready, but waiting on a dependency that is finished and not yet
        # merged: land the wave before the next one branches off it.
        return "synthesize"
    if evidence.blocked:
        return "execute"

    if evidence.merge_state.get("status") in ("clean", "empty") and not evidence.unmerged:
        return "verify"

    if evidence.unfinished and not evidence.ready:
        # Everything left is waiting on something that failed; there is no
        # more executing to do, so integrate what did land.
        return "synthesize"

    return "synthesize"


def _after_the_work_is_judged(run, evidence):
    """What is left once the gates (direct run) or the verifier (PASS) have
    cleared this cycle's work.

    Shared by both tails so an e2e failure is terminal either way - a direct
    run reaching this point on a red e2e must not be waved through to "done"
    just because it skipped the verifier.
    """
    # A feature earns an end-to-end test before the run is called done. A
    # bugfix does not: its slices already had to be written test-first.
    if run.wants_e2e(evidence.doc) and not evidence.e2e.get("status"):
        return "e2e"
    if evidence.e2e.get("status") == "FAILED":
        return "failed"
    # One entry per completed run, with the learnings a later run would
    # otherwise have to rediscover.
    if run.wants_progress() and not evidence.progress.get("status"):
        return "record"
    return "done"


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
    # Escalate before any phase is decided, so the decision sees the new
    # workflow. Doing it afterwards let a phase computed under the old
    # workflow be persisted first - and if that phase was terminal, the
    # re-derive short-circuited on it and the escalation was silently lost.
    if run.phase not in runmod.TERMINAL_PHASES and evidence.plan_valid:
        report.reassess_classification(run, evidence.doc)
    phase = derive_phase(run, evidence)
    if phase != run.phase:
        run.set_phase(phase)

    handler = {
        "classify": _classify,
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
        return _log_action(run, _stop(run, evidence))

    action = handler(run, evidence, stack_profile)
    if action["action"] == "run":
        return _log_action(run, _unless_stuck(run, evidence, action))
    if run.state.pop("repeated_run", None):
        run.save()
    spent = [entry for entry in action.get("dispatches") or [] if _spent(run, entry)]
    if spent:
        return _give_up_on(run, evidence, spent, stack_profile)
    for entry in action.get("dispatches") or []:
        run.bump_dispatch(_dispatch_key(run, entry))
    return _log_action(run, action)


# -- the dispatch cap --------------------------------------------------------
#
# The machine only moves when an agent runs its reporting command. An agent
# that exits without doing so leaves the disk unchanged, so the same action
# came back with the same prompt, for as long as anyone kept calling `next`.
# The cap is on handing an agent the *same thing* again: the key includes the
# prompt's text, so a new grill round, a new conflict or a stronger model is a
# fresh attempt rather than a repeat.


def _dispatch_key(run, entry):
    try:
        text = osenv.read_text(entry["prompt"])
    except OSError:
        text = str(entry.get("prompt"))
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return "{}:{}:{}:{}:{}".format(
        run.cycle, entry.get("agent"), entry.get("slice") or "-", entry.get("model"), digest
    )


def _dispatch_cap(run):
    return run.config.get("max_dispatch_attempts", 3)


def _spent(run, entry):
    return run.dispatch_attempts(_dispatch_key(run, entry)) >= _dispatch_cap(run)


def _unless_stuck(run, evidence, action):
    """Stop when the same commands come back with nothing in between.

    `worktree create`, `merge` and `branch` exit 1 on their own errors, and
    1 is an answer to both front-ends. The disk is unchanged, so the same
    `run` action returned, and since no agent was involved neither the
    dispatch cap nor the driver's dead-round guard could see it. Only back
    to back counts: the same command after anything else is progress.
    """
    commands = [[str(part) for part in command] for command in action.get("commands") or []]
    last = run.state.get("repeated_run") or {}
    count = last.get("count", 0) + 1 if last.get("commands") == commands else 1
    run.state["repeated_run"] = {"commands": commands, "count": count}
    run.save()
    if count <= _dispatch_cap(run):
        return action
    # The subcommand and its arguments, without the interpreter, script path
    # and the --repo/--run pins every command carries.
    shown = "; ".join(" ".join(command[6:]) for command in commands)
    return _stop(
        run,
        evidence,
        outcome="failed",
        reason="`{}` was run {} times in a row and the run did not move".format(shown, count - 1),
    )


def _give_up_on(run, evidence, spent, stack_profile):
    """What happens instead of dispatching an agent that has had its goes."""
    cap = _dispatch_cap(run)
    slices = [entry["slice"] for entry in spent if entry.get("slice")]
    if slices:
        # Blocked, which the machine already knows how to retry once on a
        # stronger model and then move past.
        for slice_id in slices:
            report.record_slice(
                run,
                slice_id,
                "BLOCKED",
                reason="dispatched {} times without reporting a result".format(cap),
            )
        return next_action(run, stack_profile)

    agent = spent[0].get("agent")
    if agent == "goat-code-classifier":
        # Sizing is advisory, so a silent classifier costs the run the safe
        # middle workflow rather than the run itself.
        why = "the classifier was dispatched {} times and recorded nothing".format(cap)
        return _log_action(
            run,
            _action(
                run,
                "run",
                why,
                "record the fallback classification",
                commands=[cli_argv(run, "classify", "--fallback", why)],
            ),
        )

    return _log_action(
        run,
        _stop(
            run,
            evidence,
            outcome="failed",
            reason="{} was dispatched {} times and never recorded a result".format(agent, cap),
        ),
    )


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


def _log_action(run, action):
    """Trace the action, and put every dispatch on the durable record.

    The ledger rather than the debug trace, because debug was never on in any
    recorded run: `escalations` read empty in all eleven while 65 of 90
    executors ran on a model the config never asked for. Nothing wrote the
    choice down, so nothing could notice.
    """
    for entry in action.get("dispatches") or []:
        target = " {}".format(entry["slice"]) if entry.get("slice") else ""
        ledger.append(
            run, "dispatch {}{} on {}".format(entry.get("agent"), target, entry.get("model"))
        )
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


# -- classify ----------------------------------------------------------------


def _classify(run, _evidence, _stack):
    text = dispatch.classifier(run)
    path = dispatch.write(run, "classifier", text)
    return _action(
        run,
        "dispatch",
        "size the task before deciding how much pipeline it gets",
        "dispatch goat-code-classifier ({})".format(_model(run, "classifier")),
        dispatches=[_entry("goat-code-classifier", _model(run, "classifier"), path)],
    )


# -- grill -----------------------------------------------------------------


def _grill(run, evidence, _stack):
    revision = None
    if run.approval == "revise":
        revision = run.state.get("approval_feedback")
        run.set_approval(None)

    # A direct run is never invited to ask: `forced` already renders "This is
    # the final round. You must return `PLAN`. Record anything still
    # unresolved in the plan's `assumptions:` list."
    forced = not revision and (
        run.grill_exhausted() or not workflowmod.wants_grill(run.workflow)
    )
    text = dispatch.planner(run, evidence.round, forced=forced, revision=revision)
    path = dispatch.write(run, "planner-round-{}".format(evidence.round), text)

    if revision:
        reason = "the user asked for plan changes"
    elif not workflowmod.wants_grill(run.workflow):
        reason = "direct workflow: plan without a round of questions"
    elif forced:
        reason = "grill round cap reached; the planner must produce a plan"
    else:
        reason = "round {} of {}".format(evidence.round, run.config.get("max_grill_rounds", 3))

    return _action(
        run,
        "dispatch",
        reason,
        "dispatch goat-code-planner ({})".format(_model(run, "planner")),
        dispatches=[_entry("goat-code-planner", _model(run, "planner"), path)],
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
        "dispatch goat-code-planner to fix the plan (attempt {} of {})".format(
            run.plan_fix_attempts, run.config.get("max_plan_fix_attempts", 2)
        ),
        dispatches=[_entry("goat-code-planner", _model(run, "planner"), path)],
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

    ready = evidence.dispatchable
    if not ready:
        retried = _retry_blocked(run, evidence)
        if retried:
            evidence = Evidence(run)
            ready = evidence.dispatchable
        if not ready and evidence.ready:
            # The retry is waiting on a merge like any other ready slice.
            return _synthesize(run, evidence, stack_profile)
        if not ready:
            _fail_stuck(run, evidence)
            return _synthesize(run, Evidence(run), stack_profile)

    limit = run.config.get("parallel", 3)
    batch = ready[:limit]

    waiting = [s for s in batch if report.awaits_context(tasks.get(evidence.doc, s))]
    if waiting:
        return _ask_context(run, evidence, waiting[0])

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
            _entry("goat-code-executor", model, path, slice_id=slice_id, cwd=item.get("worktree"))
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


def _ask_context(run, evidence, slice_id):
    """Put a slice's question to the user before it is dispatched again.

    NEEDS_CONTEXT used to set the slice back to pending and nothing more, so
    the next dispatch was the same prompt and the same question. One slice
    at a time: the answer to the first often settles the second.
    """
    item = tasks.get(evidence.doc, slice_id)
    question = (item.get("report") or {}).get("reason") or "(no question recorded)"
    return _action(
        run,
        "ask",
        "slice {} cannot continue without an answer".format(slice_id),
        "put the executor's question to the user, then record the answer",
        ask={
            "kind": "context",
            "slice": slice_id,
            "questions": [
                {
                    "id": slice_id,
                    "header": "Context",
                    "question": "Slice {} ({}) asks: {}".format(
                        slice_id, item.get("title", ""), question
                    ),
                    "options": [
                        {
                            "label": "Use your judgement",
                            "description": "The executor picks the most reasonable reading and reports it as a concern.",
                        },
                        {
                            "label": "Skip this slice",
                            "description": "Record the slice as blocked and carry on without it.",
                        },
                    ],
                    "blocking": True,
                }
            ],
            "record": dispatch.command(
                run, "context", "--slice", slice_id, "--answer", "<what the user said>"
            ),
            "give_up": dispatch.command(
                run, "report", "--slice", slice_id, "--status", "BLOCKED",
                "--reason", "the user chose to skip this slice",
            ),
            "record_note": (
                "Pass what the user typed, or the label they picked, as --answer. "
                "For 'Skip this slice' run `give_up` instead."
            ),
        },
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

    # A clean merge with finished slices it has not taken is the merge of an
    # earlier wave; run it again and it picks up the rest.
    if status in ("not-started", "in-progress") or (status != "conflict" and evidence.unmerged):
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
            "dispatch goat-code-synthesizer ({})".format(_model(run, "synthesizer")),
            dispatches=[
                _entry(
                    "goat-code-synthesizer",
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
    package["weak_assertions"] = (osenv.read_json(package_path) or {}).get("weak_assertions") or []
    package.update(diffpkg.previous_judgement(run, evidence.doc))
    text = dispatch.verifier(run, package)
    path = dispatch.write(run, "verifier", text)
    return _action(
        run,
        "dispatch",
        "integration is ready to judge",
        "dispatch goat-code-verifier ({})".format(_model(run, "verifier")),
        dispatches=[
            _entry("goat-code-verifier", _model(run, "verifier"), path, cwd=package["worktree"])
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
        "dispatch goat-code-e2e ({})".format(_model(run, "e2e")),
        dispatches=[_entry("goat-code-e2e", _model(run, "e2e"), path, cwd=package["worktree"])],
    )


# -- the progress log ------------------------------------------------------


def _record(run, evidence, stack_profile):
    text = dispatch.scribe(run, evidence.doc, _criteria(evidence.doc), evidence.merge_state)
    path = dispatch.write(run, "scribe", text)
    return _action(
        run,
        "dispatch",
        "the run is finished and has not been written up yet",
        "dispatch goat-code-scribe ({})".format(_model(run, "scribe")),
        dispatches=[_entry("goat-code-scribe", _model(run, "scribe"), path)],
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
        "dispatch goat-code-replanner ({})".format(_model(run, "replanner")),
        dispatches=[_entry("goat-code-replanner", _model(run, "replanner"), path)],
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
        lines = [
            "DONE",
            "",
            "branch: {}".format(run.integration_branch),
            "review: git diff {}..{}".format(run.base_commit[:12], run.integration_branch),
            "merge:  git merge {}".format(run.integration_branch),
            "",
            "nothing was committed to your branch {}".format(run.state.get("base_branch")),
        ]
        if workflowmod.wants_approval(run.workflow):
            classification = run.classification or {}
            reasons = list(classification.get("deterministic_overrides") or [])
            for factor in classification.get("risk_factors") or []:
                if factor not in reasons:
                    reasons.append(factor)
            lines.append("")
            lines.append(
                "This run was classified HIGH_RISK ({}). Review the diff before you"
                " merge it - the pipeline asks for your sign-off rather than"
                " assuming it.".format(", ".join(reasons) or "risk rules")
            )
        message = "\n".join(lines)
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
    return [dispatch.interpreter(run), str(dispatch.CLI), "--repo", str(run.repo), "--run", run.run_id] + [
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
