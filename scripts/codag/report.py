"""How agents put results back into the pipeline.

Agents call the CLI directly instead of returning a receipt for the
orchestrator to read and interpret. That removes the last place where the
orchestrator's model had to parse prose, and it lets the pipeline *check*
what an agent claims rather than take its word.
"""

from __future__ import annotations

import pathlib
import re

from . import ledger, miniyaml, osenv, tasks, tdd

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

    if status == "DONE":
        problems.extend(evidence_findings(item, evidence, path))
        problems.extend(tdd_findings(run, item, path, profile))

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
        late = evidence_findings(item, evidence, worktree_path) + tdd_findings(
            run, item, worktree_path, profile
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


def read_verdict(run, cycle=None):
    """The verdict the verifier wrote, or None if it has not written one."""
    path = run.cycle_dir(cycle) / "verdict.md"
    if not path.exists():
        return None
    matches = VERDICT_RE.findall(osenv.read_text(path))
    return matches[-1] if matches else None


def require_verdict(run):
    path = run.cycle_dir() / "verdict.md"
    if not path.exists():
        raise ReportError("no verdict at {}; write it before running this".format(path))
    result = read_verdict(run)
    if result is None:
        raise ReportError(
            "{} has no 'VERDICT: PASS' or 'VERDICT: FAIL' line; add one as the final line".format(path)
        )
    return result
