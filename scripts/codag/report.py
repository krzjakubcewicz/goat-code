"""How agents put results back into the pipeline.

Agents call the CLI directly instead of returning a receipt for the
orchestrator to read and interpret. That removes the last place where the
orchestrator's model had to parse prose, and it lets the pipeline *check*
what an agent claims rather than take its word.
"""

from __future__ import annotations

import pathlib
import re

from . import ledger, miniyaml, osenv, tasks

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


def verify_done(run, doc, slice_id):
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

    status = osenv.git(["status", "--porcelain"], cwd=path)
    if status.out:
        changed = ", ".join(line[3:] for line in status.out.splitlines()[:5])
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

    return problems


def record_slice(run, slice_id, status, tests=None, concerns=None, reason=None, head=None, force=False):
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

    problems = []
    if status in FINISHED and not force:
        problems = verify_done(run, doc, slice_id)
        if problems:
            raise ReportError(
                "cannot accept {} for {}:\n  - ".format(status, slice_id) + "\n  - ".join(problems)
            )

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
    }


# --------------------------------------------------------------------------
# role reports
# --------------------------------------------------------------------------


def record_role(run, role, status, detail=None):
    """Record a non-slice agent's outcome (currently the synthesizer)."""
    role = (role or "").strip().lower()
    status = (status or "").strip().upper()
    if role != "synthesizer":
        raise ReportError("unknown role {!r}; only 'synthesizer' reports this way".format(role))
    if status not in ("CLEAN", "ESCALATE"):
        raise ReportError("synthesizer status must be CLEAN or ESCALATE, not {!r}".format(status))
    if status == "ESCALATE" and not detail:
        raise ReportError("ESCALATE needs --detail saying precisely what disagrees")

    run.state["synthesizer"] = {"status": status, "detail": detail}
    run.save()
    ledger.append(run, "synthesizer {}{}".format(status.lower(), " - " + detail if detail else ""))

    written = None
    if status == "ESCALATE":
        # Slices that contradict each other are a verification failure, not a
        # merge problem. Writing the verdict here keeps one path to replan.
        written = write_escalation_verdict(run, detail)
    return {"role": role, "status": status, "detail": detail, "verdict": str(written) if written else None}


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
