"""How agents put results back into the pipeline.

Agents call the CLI directly instead of returning a receipt for the
orchestrator to read and interpret. That removes the last place where the
orchestrator's model had to parse prose, and it lets the pipeline *check*
what an agent claims rather than take its word.
"""

from __future__ import annotations

import pathlib
import re

from . import classify, diffpkg, gates, ledger, miniyaml, osenv, schema, stack as stackmod, tasks, tdd, workflow as workflowmod

SLICE_STATUSES = ("DONE", "DONE_WITH_CONCERNS", "NEEDS_CONTEXT", "BLOCKED")
FINISHED = ("DONE", "DONE_WITH_CONCERNS")

#: How a reported status maps onto the slice's state in tasks.yaml.
STATUS_TO_SLICE = {
    "DONE": "done",
    "DONE_WITH_CONCERNS": "done",
    "NEEDS_CONTEXT": "pending",
    "BLOCKED": "blocked",
}

VERDICT_RE = re.compile(r"^VERDICT:\s*(PASS|FAIL)\s*$", re.MULTILINE)

#: The three marks a verdict row can carry: met, unmet, unsure. Written as
#: escapes so the file stays ASCII on every editor and code page.
VERDICT_MARKS = (("✅", "pass"), ("❌", "fail"), ("⚠", "unsure"))


class ReportError(RuntimeError):
    """An agent's report was rejected, with a reason it can act on."""


# --------------------------------------------------------------------------
# slice reports
# --------------------------------------------------------------------------


#: An evidence value: a path inside the worktree and a line number in it.
EVIDENCE_RE = re.compile(r"^(?P<path>.+):(?P<line>\d+)$")


def evidence_findings(item, evidence, worktree_path):
    """Every acceptance criterion must name a test line that really exists.

    The failure this catches is the slice whose code is right and whose proof
    is missing - by far the most common reason a cycle is spent. Naming the
    line is cheap when the test exists and impossible when it does not, which
    is the whole point: it is the same question the verifier will ask, asked
    while the executor is still holding the context to answer it.
    """
    criteria = tasks.criterion_ids(item)
    if not criteria:
        return []
    evidence = evidence or {}

    problems = []
    missing = [cid for cid in criteria if not str(evidence.get(cid, "")).strip()]
    if missing:
        problems.append(
            "no --evidence for {}; give each criterion the test path:line that"
            " would fail if the behaviour were wrong".format(", ".join(missing))
        )

    unknown = sorted(set(evidence) - set(criteria))
    if unknown:
        problems.append(
            "--evidence names {}, which {} not an acceptance criterion of this"
            " slice (its criteria are {})".format(
                ", ".join(unknown), "are" if len(unknown) > 1 else "is", ", ".join(criteria)
            )
        )

    for cid in criteria:
        raw = str(evidence.get(cid, "")).strip()
        if not raw:
            continue
        match = EVIDENCE_RE.match(raw)
        if not match:
            problems.append(
                "evidence for {} is {!r}; it must be <test path>:<line>".format(cid, raw)
            )
            continue
        problems.extend(_evidence_target(cid, match, worktree_path))
    return problems


def _evidence_target(cid, match, worktree_path):
    """The named file and line have to exist in the worktree being reported."""
    target = pathlib.Path(worktree_path) / match.group("path")
    if not target.is_file():
        return ["evidence for {} names {}, which does not exist".format(cid, match.group("path"))]
    # ponytail: line-exists only. Whether that line holds a real assertion is
    # the verifier's judgement, not something a script should guess at.
    total = len(osenv.read_text(target).splitlines())
    line = int(match.group("line"))
    if line < 1 or line > total:
        return [
            "evidence for {} names {}:{}, but that file has {} lines".format(
                cid, match.group("path"), line, total
            )
        ]
    return []


def verify_done(run, doc, slice_id, status="DONE", evidence=None, profile=None):
    """Cheaply falsifiable checks on a claimed DONE. Returns the problems.

    Not an attempt to judge the work - the verifier does that. This only
    catches claims that are provably untrue, most importantly the slice that
    reports DONE with uncommitted changes and so contributes nothing to the
    merge.
    """
    item = tasks.get(doc, slice_id)
    problems = []

    recorded = item.get("worktree")
    if not recorded:
        return ["no worktree recorded for {}; it was never dispatched".format(slice_id)]
    path = pathlib.Path(recorded)
    if not path.exists():
        return ["worktree {} does not exist".format(path)]

    # --untracked-files=all, so the agent is told "src/stray.js" rather than
    # a collapsed "src/" it then has to go looking through.
    tree = osenv.git(["status", "--porcelain", "--untracked-files=all"], cwd=path)
    if tree.out:
        changed = ", ".join(line[3:] for line in tree.out.splitlines()[:5])
        problems.append(
            "worktree is not clean - commit or discard these before reporting DONE: {}".format(changed)
        )

    head = osenv.git(["rev-parse", "HEAD"], cwd=path)
    base = (item.get("commits") or {}).get("base")
    if not head.ok:
        problems.append("cannot read HEAD in {}".format(path))
    elif base and head.out == base:
        problems.append("HEAD is still at the base commit; nothing was committed")

    for entry in item.get("tests") or []:
        relpath = entry.get("path") if isinstance(entry, dict) else entry
        if not relpath:
            continue
        if not (path / relpath).exists():
            problems.append("the brief requires a test at {} and it does not exist".format(relpath))

    # For both finished statuses: a concern explains how the work was done,
    # it does not excuse where. Another slice is writing those paths now.
    if base and head.ok:
        problems.extend(boundary_findings(item, path, base, profile))

    if status == "DONE":
        problems.extend(evidence_findings(item, evidence, path))
        problems.extend(tdd_findings(run, item, path, profile))
        # Last, and only on an otherwise sound report: it is the one check
        # here that costs real time.
        if not problems:
            problems.extend(test_findings(run, item, path, profile))

    return problems


def test_findings(run, item, worktree_path, profile=None):
    """Run the test files the brief declares. Returns the ones that fail.

    `--tests "7 passed"` is the executor's own sentence and was stored as
    written. The stack already knows how to run one test file, so the claim
    can be checked instead.
    """
    commands = (profile or {}).get("commands") or {}
    template = commands.get("test_one")
    if not template:
        return []
    # ponytail: only when commands run at the worktree root. with a
    # project_dir one level down, the declared paths and the command's cwd
    # disagree and a good slice would be refused; resolve the path against
    # project_dir when a nested stack needs this check.
    if (profile or {}).get("project_dir") or (profile or {}).get("commands_cwd"):
        return []

    problems = []
    timeout = run.config.get("gate_timeout_seconds", 1800)
    for entry in item.get("tests") or []:
        relpath = entry.get("path") if isinstance(entry, dict) else entry
        if not relpath or not (pathlib.Path(worktree_path) / relpath).exists():
            continue
        argv = [str(part).replace(stackmod.PATH_TOKEN, str(relpath)) for part in template]
        try:
            result = osenv.run(argv, cwd=worktree_path, timeout=timeout)
        except osenv.CommandError:
            # The runner is not on this host - it often lives only in the
            # project's container. Not the same thing as a failing test.
            return []
        if result.ok or result.returncode == 124:
            continue
        tail = "\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-15:])
        problems.append(
            "the declared test {} fails (`{}` exited {}):\n{}".format(
                relpath, stackmod.command_text(argv), result.returncode, _indent(tail)
            )
        )
    return problems


def _indent(text):
    return "\n".join("      " + line for line in text.splitlines())


def boundary_findings(item, worktree_path, base, profile=None):
    """Where the slice wrote, held against what the plan let it write.

    The ownership rules were validated in the plan before dispatch and were
    only a paragraph in the brief after it. Disjoint ownership is what makes
    a wave safe to run in parallel, so it is checked on the commits.
    """
    problems = []
    owns = [str(p) for p in item.get("owns") or []]
    shared = [str(p) for p in item.get("touches_shared") or []]

    def inside(relpath, patterns):
        return any(schema.matches(pattern, relpath) for pattern in patterns)

    changed = diffpkg.changed_files(worktree_path, base, "HEAD")
    outside = [
        relpath
        for relpath in changed
        if not inside(relpath, owns + shared) and not tdd.is_test_path(relpath, item, profile)
    ]
    if outside:
        problems.append(
            "these files are outside the paths this slice owns ({}): {}. Move the change"
            " inside your paths, or report BLOCKED and say what you needed".format(
                ", ".join(owns) or "none", ", ".join(outside[:8])
            )
        )

    rewritten = []
    for relpath in changed:
        if inside(relpath, owns) or not inside(relpath, shared):
            continue
        stat = osenv.git(["diff", "--numstat", "{}..HEAD".format(base), "--", relpath], cwd=worktree_path)
        removed = (stat.out.split() or ["0", "0"])[1]
        if removed not in ("0", "-"):
            rewritten.append(relpath)
    if rewritten:
        problems.append(
            "shared paths are append-only, and these had lines removed or rewritten: {}".format(
                ", ".join(rewritten)
            )
        )

    merges = osenv.git(["rev-list", "--merges", "{}..HEAD".format(base)], cwd=worktree_path)
    if merges.out:
        problems.append(
            "this branch contains a merge commit ({}); a slice never merges, rebases or"
            " pulls in another branch".format(merges.out.split()[0][:8])
        )

    branch = osenv.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path)
    if item.get("branch") and branch.ok and branch.out != item["branch"]:
        problems.append(
            "the worktree is on {}, not the slice's branch {}".format(branch.out, item["branch"])
        )
    return problems


def tdd_findings(run, item, worktree_path, profile=None):
    """TDD violations in the slice's own commit range, if enforcement is on."""
    if not run.config.get("enforce_tdd", True):
        return []
    base = (item.get("commits") or {}).get("base")
    head = osenv.git(["rev-parse", "HEAD"], cwd=worktree_path)
    if not base or not head.ok:
        return []
    return [
        "{}. Write the failing test first"
        " (superpowers:test-driven-development)".format(finding)
        for finding in _tdd_messages(worktree_path, item, base, head.out, profile)
    ]


def _tdd_hint(problems):
    """Point at the honest remedy, since git history cannot be un-written."""
    if not any("before any test" in problem for problem in problems):
        return ""
    return (
        "\n\nHistory cannot be un-written. Add the missing tests, then report"
        "\nDONE_WITH_CONCERNS saying they were written after the implementation."
        "\nThe verifier will see it."
    )


def _tdd_messages(worktree_path, item, base, head, profile):
    return [
        "implementation landed before any test: commit {} ({}) added {} with no"
        " test touched yet in this slice".format(entry["short"], entry["subject"], ", ".join(entry["files"]))
        for entry in tdd.violations(worktree_path, item, base, head, profile)
    ]


def record_slice(
    run,
    slice_id,
    status,
    tests=None,
    concerns=None,
    reason=None,
    head=None,
    evidence=None,
    force=False,
    profile=None,
):
    """Record an executor's own report. Raises :class:`ReportError` if rejected."""
    status = (status or "").strip().upper()
    if status not in SLICE_STATUSES:
        raise ReportError(
            "status must be one of {}, not {!r}".format(", ".join(SLICE_STATUSES), status)
        )

    doc = tasks.load(run.tasks_path)
    try:
        item = tasks.get(doc, slice_id)
    except tasks.TaskError as exc:
        raise ReportError(str(exc))

    if item.get("status") in tasks.TERMINAL and status not in FINISHED:
        # The driver reports BLOCKED for any agent process that exits badly,
        # including one that hit its budget after its DONE was accepted.
        # That turned verified work into a retry, and then into a failure.
        raise ReportError(
            "{} is already {}; a finished slice cannot go back to {}".format(
                slice_id, item.get("status"), status
            )
        )

    if status in ("BLOCKED", "NEEDS_CONTEXT") and not (reason or "").strip():
        # The reason is the whole content of these two reports: it is what
        # the user is asked, and what the retry is told.
        raise ReportError("{} needs --reason: say what is missing or what stopped you".format(status))

    if status == "NEEDS_CONTEXT" and item.get("context"):
        # One question, one answer. Blocked has an end - a single retry on a
        # stronger model, then the run moves on - and asking again did not.
        status = "BLOCKED"
        reason = "still missing context after an answer: {}".format(reason)

    if status in FINISHED and not force:
        problems = verify_done(run, doc, slice_id, status=status, evidence=evidence, profile=profile)
        if problems:
            raise ReportError(
                "cannot accept {} for {}:\n  - ".format(status, slice_id)
                + "\n  - ".join(problems)
                + _tdd_hint(problems)
            )

    # An honest DONE_WITH_CONCERNS is the escape hatch from the TDD and
    # evidence checks, but the gap still goes on the record - it cannot be
    # hidden behind a vague concern string.
    if status == "DONE_WITH_CONCERNS" and not force and item.get("worktree"):
        worktree_path = pathlib.Path(item["worktree"])
        late = (
            evidence_findings(item, evidence, worktree_path)
            + tdd_findings(run, item, worktree_path, profile)
            + [finding.splitlines()[0] for finding in test_findings(run, item, worktree_path, profile)]
        )
        if late:
            concerns = "; ".join([c for c in [concerns] if c] + late)

    resolved_head = head
    if resolved_head is None and item.get("worktree"):
        probe = osenv.git(["rev-parse", "HEAD"], cwd=item["worktree"])
        resolved_head = probe.out if probe.ok else None

    def mutate(document):
        target = tasks.get(document, slice_id)
        target["status"] = STATUS_TO_SLICE[status]
        target["report"] = {
            "status": status,
            "tests": tests,
            "concerns": concerns,
            "reason": reason,
            # The verifier reads these as data: the executor's own claim about
            # which test proves which criterion, there to be checked.
            "evidence": dict(evidence) if evidence else None,
        }
        if resolved_head:
            commits = target.setdefault("commits", {"base": None, "head": None})
            commits["head"] = resolved_head
        return target["status"]

    slice_status = tasks.update(run.tasks_path, mutate)

    detail = tests or reason or concerns or ""
    ledger.append(run, "slice {} {}{}".format(slice_id, status.lower(), " - " + detail if detail else ""))

    return {
        "slice": slice_id,
        "status": status,
        "slice_status": slice_status,
        "head": resolved_head,
        "tests": tests,
        "concerns": concerns,
        "reason": reason,
        "evidence": dict(evidence) if evidence else None,
    }


def _spec_text(run):
    """The spec's on-disk text, or ``""`` if it has none yet.

    Missing is expected - a run may not have written a spec yet, and "" is
    the honest state for that. Unreadable (bad encoding, permissions,
    replaced by a directory) is not the same thing: it is raised, so a
    caller cannot mistake "the rules found nothing" for "the rules never
    got to look."
    """
    path = pathlib.Path(run.spec_path)
    if not path.exists():
        return ""
    return osenv.read_text(path)


def reassess_classification(run, doc):
    """Run the deterministic rules again, now that the plan names paths.

    At classify time only the request existed. A plan that claims
    `src/auth/**` is evidence the request never carried, so the same rules
    see it now. Escalate-only: this can cost a run its cheap path and can
    never buy one. Never raises: an unreadable spec floors the result at the
    fallback's own risk rather than stopping `next_action` cold.
    """
    current = run.classification
    if not current or not doc:
        return None

    paths = []
    for item in tasks.slices(doc):
        paths.extend(item.get("owns") or [])
        paths.extend(item.get("touches_shared") or [])

    try:
        spec = _spec_text(run)
        unreadable = False
    except (OSError, UnicodeDecodeError):
        spec = ""
        unreadable = True

    updated = classify.apply_rules(current, spec, paths)
    if unreadable:
        # An unreadable spec must not look like a clean bill of health from
        # the rules. Floor the result at the fallback's own risk - never a
        # downgrade, since apply_rules already guarantees `current`'s risk
        # is the floor for its own merge.
        updated["risk"] = classify.risk_at_least(updated["risk"], classify.FALLBACK["risk"])
        if updated["risk"] != current.get("risk"):
            overrides = list(updated.get("deterministic_overrides") or [])
            if "spec-unreadable" not in overrides:
                overrides.append("spec-unreadable")
            updated["deterministic_overrides"] = overrides
    if updated["risk"] == current.get("risk"):
        return None

    selected = workflowmod.select(updated)
    run.set_classification(updated, selected)
    ledger.append(
        run,
        "re-classified {}/{} -> {} after the plan claimed {}".format(
            updated["complexity"],
            updated["risk"],
            selected,
            ", ".join(updated.get("deterministic_overrides") or []),
        ),
    )
    return updated


def _fallback_advisory():
    """A copy of `classify.FALLBACK` with its own list objects.

    `dict(classify.FALLBACK)` is a shallow copy - the `_factors` lists would
    still be the module constant's own objects, shared across every fallback.
    """
    advisory = dict(classify.FALLBACK)
    advisory["risk_factors"] = list(advisory["risk_factors"])
    advisory["complexity_factors"] = list(advisory["complexity_factors"])
    return advisory


def record_classification(run, payload=None, reason=None):
    """Record how a run was sized, and which workflow that earns it.

    Never raises. A classifier that timed out, returned prose, or invented an
    enum must not stop the run - it costs the run its cheaper path, which is
    the safe direction to fail in. Same for a spec.md that cannot be read:
    the fallback classification stands in, so the model's own claim never
    goes unchallenged against an empty haystack.
    """
    fallback = reason
    if payload is None:
        advisory = _fallback_advisory()
        fallback = fallback or "no classification produced"
    else:
        try:
            advisory = classify.parse(payload)
        except classify.ClassifyError as exc:
            advisory = _fallback_advisory()
            fallback = "invalid classification: {}".format(
                ", ".join(exc.fields) if exc.fields else "not a JSON object"
            )

    try:
        spec = _spec_text(run)
    except (OSError, UnicodeDecodeError) as exc:
        advisory = _fallback_advisory()
        fallback = "spec could not be read: {}".format(exc)
        spec = ""
    final = classify.apply_rules(advisory, spec, [])
    # The spec's audit fields: which rules and which model produced this, so a
    # routing decision stays explicable after either has changed.
    final["classifier_version"] = classify.VERSION
    # Match machine._model(run, "classifier") exactly: `models.classifier` is
    # the key dispatch actually reads. `classifier.model` used to exist too
    # and could disagree with it, which made the audit record lie about what
    # ran.
    final["model"] = run.config.get("models", {}).get("classifier", "sonnet")
    final["fallback_reason"] = fallback
    selected = workflowmod.select(final)
    run.set_classification(final, selected)

    detail = "classified {}/{} -> {}".format(final["complexity"], final["risk"], selected)
    overrides = final.get("deterministic_overrides") or []
    if overrides:
        detail += "; deterministic override: {}".format(", ".join(overrides))
    if fallback:
        detail += "; fallback: {}".format(fallback)
    ledger.append(run, detail)

    return {"classification": final, "workflow": selected, "fallback": fallback}


# --------------------------------------------------------------------------
# role reports
# --------------------------------------------------------------------------


#: Statuses each non-slice role may report.
ROLE_STATUSES = {
    "synthesizer": ("CLEAN", "ESCALATE"),
    "e2e": ("PASS", "SKIPPED", "FAILED"),
    "scribe": ("WRITTEN", "SKIPPED"),
}

#: Statuses that must say why.
NEEDS_DETAIL = {
    ("synthesizer", "ESCALATE"),
    ("e2e", "SKIPPED"),
    ("e2e", "FAILED"),
    ("scribe", "SKIPPED"),
}


def record_role(run, role, status, detail=None, tests=None):
    """Record a non-slice agent's outcome: the synthesizer or the e2e agent."""
    role = (role or "").strip().lower()
    status = (status or "").strip().upper()
    if role not in ROLE_STATUSES:
        raise ReportError(
            "unknown role {!r}; expected one of {}".format(role, ", ".join(sorted(ROLE_STATUSES)))
        )
    allowed = ROLE_STATUSES[role]
    if status not in allowed:
        raise ReportError("{} status must be one of {}, not {!r}".format(role, ", ".join(allowed), status))
    if (role, status) in NEEDS_DETAIL and not detail:
        raise ReportError("{} {} needs --detail saying why".format(role, status))

    run.state[role] = {"status": status, "detail": detail, "tests": tests}
    run.save()
    ledger.append(run, "{} {}{}".format(role, status.lower(), " - " + detail if detail else ""))

    written = None
    if role == "synthesizer" and status == "ESCALATE":
        # Slices that contradict each other are a verification failure, not a
        # merge problem. Writing the verdict here keeps one path to replan.
        written = write_escalation_verdict(run, detail)
    return {
        "role": role,
        "status": status,
        "detail": detail,
        "tests": tests,
        "verdict": str(written) if written else None,
    }


def write_escalation_verdict(run, detail):
    path = run.cycle_dir() / "verdict.md"
    text = "\n".join(
        [
            "# Verdict - cycle {}".format(run.cycle),
            "",
            "The synthesizer escalated: the slices contradict each other and",
            "reconciling them would mean deciding which one is right.",
            "",
            "## What disagrees",
            "",
            str(detail),
            "",
            "## What must change",
            "",
            "1. Decide which slice's behaviour is correct and re-plan the other.",
            "",
            "VERDICT: FAIL",
            "",
        ]
    )
    osenv.write_text(path, text)
    return path


# --------------------------------------------------------------------------
# grill answers
# --------------------------------------------------------------------------


def load_questions(path):
    path = pathlib.Path(path)
    if not path.exists():
        raise ReportError("no questions file at {}".format(path))
    doc = miniyaml.load(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("questions"), list):
        raise ReportError("{} must be a mapping with a 'questions' list".format(path))
    return doc


def record_answers(run, questions_path, answers, notes=None, free_text=None):
    """Append the user's answers to the spec and count the round.

    The spec file, not the conversation, is the durable record: the planner
    is re-dispatched with a path, and a later cycle re-reads the same file.
    """
    doc = load_questions(questions_path)
    round_no = doc.get("round", run.grill_rounds + 1)
    notes = notes or {}

    lines = ["## Clarifications (round {})".format(round_no), ""]
    unanswered = []
    for question in doc["questions"]:
        if not isinstance(question, dict):
            continue
        qid = str(question.get("id", "?"))
        answer = answers.get(qid)
        lines.append("**{} ({}):** {}".format(qid, question.get("topic", "general"), question.get("question", "")))
        if answer:
            lines.append("")
            lines.append("**A:** {}".format(answer))
            if notes.get(qid):
                lines.append("")
                lines.append("**Note:** {}".format(notes[qid]))
        else:
            unanswered.append(qid)
            lines.append("")
            lines.append("**A:** _(not answered; use your recommendation and record it as an assumption)_")
        lines.append("")

    if free_text:
        lines.append("**Additional context from the user:** {}".format(free_text))
        lines.append("")

    run.append_spec("\n".join(lines))
    total = run.bump_grill_round()
    ledger.append(
        run,
        "grill round {} answered ({} of {} questions)".format(
            round_no, len(doc["questions"]) - len(unanswered), len(doc["questions"])
        ),
    )
    return {
        "round": round_no,
        "rounds_used": total,
        "answered": len(doc["questions"]) - len(unanswered),
        "unanswered": unanswered,
        "spec": str(run.spec_path),
    }


def parse_pairs(items):
    """``["Q1=Both", "Q2=15-minute timer"]`` -> dict. Rejects a missing ``=``."""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ReportError("expected QID=answer, got {!r}".format(item))
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ReportError("empty question id in {!r}".format(item))
        out[key] = value.strip()
    return out


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


def awaits_context(item):
    """Whether this slice asked a question nobody has answered yet."""
    reported = item.get("report") or {}
    return reported.get("status") == "NEEDS_CONTEXT" and not reported.get("answered")


def record_context(run, slice_id, answer):
    """Keep the user's answer to a slice's question, on the slice.

    On the run's record rather than in one dispatch's command line, so it
    survives a crash and reaches the executor through its prompt like
    everything else it is told.
    """
    answer = (answer or "").strip()
    if not answer:
        raise ReportError("--answer is empty; report the slice BLOCKED if there is nothing to tell it")

    def mutate(document):
        target = tasks.get(document, slice_id)
        if not awaits_context(target):
            raise ReportError("{} has no unanswered question".format(slice_id))
        target["report"]["answered"] = True
        target.setdefault("context", []).append(
            {"question": target["report"].get("reason"), "answer": answer}
        )
        return list(target["context"])

    try:
        recorded = tasks.update(run.tasks_path, mutate)
    except tasks.TaskError as exc:
        raise ReportError(str(exc))
    ledger.append(run, "slice {} context answered".format(slice_id))
    return {"slice": slice_id, "context": recorded}


# --------------------------------------------------------------------------
# approval and verdict
# --------------------------------------------------------------------------


def record_approval(run, decision, feedback=None):
    if decision == "revise" and not feedback:
        raise ReportError("--revise needs the feedback to send back to the planner")
    run.set_approval(decision)
    if feedback:
        run.state["approval_feedback"] = feedback
        run.save()
    ledger.append(run, "plan {}{}".format(decision, ": " + feedback if feedback else ""))
    return {"approval": decision, "feedback": feedback}


def written_verdict(run, cycle=None):
    """The line the verifier wrote, before anything checks it."""
    path = run.cycle_dir(cycle) / "verdict.md"
    if not path.exists():
        return None
    # The last non-blank line, not the last match: a verdict quoted in the
    # body, or one followed by second thoughts, is not the ruling.
    lines = [line for line in osenv.read_text(path).splitlines() if line.strip()]
    match = VERDICT_RE.match(lines[-1].strip()) if lines else None
    return match.group(1) if match else None


def parse_verdict(run, cycle=None):
    """The verifier's criteria table as rows, or [] when there is none.

    Any table row whose fourth cell carries one of the three marks counts;
    the header and the separator carry none, so they fall out on their own.
    """
    path = run.cycle_dir(cycle) / "verdict.md"
    if not path.exists():
        return []
    rows = []
    for line in osenv.read_text(path).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        mark = next((name for sign, name in VERDICT_MARKS if sign in cells[3]), None)
        if mark is None:
            continue
        # An evidence cell may itself hold a pipe; everything past the mark is evidence.
        rows.append({"slice": cells[0], "id": cells[1], "mark": mark, "evidence": " | ".join(cells[4:]).strip()})
    return rows


def _table_problems(run, cycle):
    """Where the verifier's own table contradicts the PASS written under it."""
    try:
        doc = tasks.load(run.tasks_path)
    except (OSError, tasks.TaskError, miniyaml.YamlError):
        # No readable plan, nothing to hold the table against.
        return []
    wanted = [
        (item.get("id"), criterion.get("id"))
        for item in tasks.slices(doc)
        for criterion in item.get("acceptance") or []
        if isinstance(criterion, dict)
    ]
    rows = parse_verdict(run, cycle)
    ruled = {(row["slice"], row["id"]) for row in rows}
    name = "{}/{}".format

    problems = []
    unmet = [name(row["slice"], row["id"]) for row in rows if row["mark"] != "pass"]
    if unmet:
        problems.append("criteria the table does not mark met: {}".format(", ".join(unmet)))
    missing = [name(*key) for key in wanted if key not in ruled]
    if missing:
        problems.append("criteria with no row in the table: {}".format(", ".join(missing)))
    unknown = sorted(name(*key) for key in ruled - set(wanted))
    if unknown:
        problems.append("rows for criteria the plan does not have: {}".format(", ".join(unknown)))
    bare = [name(row["slice"], row["id"]) for row in rows if row["mark"] == "pass" and not row["evidence"]]
    if bare:
        problems.append("criteria marked met with no evidence: {}".format(", ".join(bare)))
    return problems


def verdict_problems(run, cycle=None):
    """Why a PASS cannot stand, from evidence the verifier does not own.

    The gates are the pipeline's own measurement, and the table is the
    verifier's own ruling per criterion. A PASS written over a regression or
    over an unmet row used to carry the run to done, because only the
    verdict line was ever read.
    """
    problems = []
    path = run.cycle_dir(cycle) / "gates.json"
    if path.exists():
        regressions = gates.blocking(osenv.read_json(path) or {})
        if regressions:
            problems.append("gates regressed in this run: {}".format(", ".join(regressions)))
    return problems + _table_problems(run, cycle)


def read_verdict(run, cycle=None):
    """The verdict this cycle stands on, or None if none is written.

    A PASS the evidence contradicts reads as FAIL, here rather than in each
    caller, so the machine and the CLI cannot disagree about it.
    """
    result = written_verdict(run, cycle)
    if result == "PASS" and verdict_problems(run, cycle):
        return "FAIL"
    return result


def require_verdict(run):
    path = run.cycle_dir() / "verdict.md"
    if not path.exists():
        raise ReportError("no verdict at {}; write it before running this".format(path))
    result = written_verdict(run)
    if result is None:
        raise ReportError(
            "{} has no 'VERDICT: PASS' or 'VERDICT: FAIL' line; add one as the final line".format(path)
        )
    problems = verdict_problems(run) if result == "PASS" else []
    if problems:
        # Refused rather than quietly read as FAIL: the verifier is still in
        # its session, and a FAIL it writes itself carries the "what must
        # change" list the replanner works from. If it walks away instead,
        # read_verdict already treats this file as a FAIL.
        raise ReportError(
            "VERDICT: PASS cannot stand:\n  - {}\n"
            "correct the table if it is wrong, otherwise write VERDICT: FAIL "
            "with what must change, then run this again".format("\n  - ".join(problems))
        )
    return result
