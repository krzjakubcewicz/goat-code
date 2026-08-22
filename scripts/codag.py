#!/usr/bin/env python3
"""cod-ag command line - the deterministic half of the pipeline.

Everything mechanical lives behind this one entry point so the agents spend
their tokens on judgement instead of rediscovering git plumbing.

    python codag.py init --prompt "add magic-link login"
    python codag.py plan validate
    python codag.py wave next
    python codag.py worktree create S1
    python codag.py brief S1
    python codag.py merge
    python codag.py gates run
    python codag.py verify-package
    python codag.py finish

Run ``python codag.py --help`` for the full list. Every subcommand accepts
``--json`` for machine-readable output and ``--run <id>`` to target a run
other than the most recent one.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from codag import (  # noqa: E402
    brief as briefmod,
    diffpkg,
    dispatch as dispatchmod,
    gates as gatesmod,
    ledger as ledgermod,
    machine as machinemod,
    merge as mergemod,
    miniyaml,
    osenv,
    report as reportmod,
    run as runmod,
    schema,
    stack as stackmod,
    tasks as tasksmod,
    worktree as worktreemod,
)
from codag.run import Run, RunError  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


class CliError(RuntimeError):
    """A problem worth reporting to the caller without a traceback."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def emit(args, payload, text):
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    elif text:
        print(text)


def resolve_repo(args):
    """The main work tree, where .codag/ lives.

    Deliberately not ``repo_root``: executor agents run inside their own
    linked worktree, and every command they issue must still reach the run
    state in the main repository.
    """
    root = osenv.main_repo_root(getattr(args, "repo", None) or pathlib.Path.cwd())
    if root is None:
        raise CliError("not inside a git repository (run 'git init' first)")
    return root


def resolve_run(args):
    repo = resolve_repo(args)
    try:
        return Run.load(repo, getattr(args, "run", None))
    except RunError as exc:
        raise CliError(str(exc))


def load_plan(run):
    try:
        return tasksmod.load(run.tasks_path)
    except tasksmod.TaskError as exc:
        raise CliError(str(exc))
    except miniyaml.YamlError as exc:
        raise CliError("{} is not valid YAML: {}".format(run.tasks_path, exc))


def load_stack(run):
    path = pathlib.Path(run.stack_path)
    return osenv.read_json(path) if path.exists() else {}


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def cmd_init(args):
    repo, problems = runmod.preflight(getattr(args, "repo", None))
    if repo is None:
        raise CliError(problems[0])
    if problems and not args.force:
        raise CliError(
            "preflight failed:\n  - " + "\n  - ".join(problems) + "\n\nFix these, or pass --force."
        )

    spec_text = ""
    title = args.prompt or "feature"
    if args.spec:
        spec_path = pathlib.Path(args.spec)
        if not spec_path.exists():
            raise CliError("no spec file at {}".format(spec_path))
        spec_text = osenv.read_text(spec_path)
        title = _spec_title(spec_text) or spec_path.stem
    elif not args.prompt:
        raise CliError("pass --prompt \"...\" or --spec <file>")

    runmod.ensure_ignored(repo)
    gitignored = False
    if runmod.load_config(repo).get("manage_gitignore", True):
        gitignored = runmod.ensure_gitignore(repo)
    worktreemod.reap_orphans(repo)

    run = Run.create(repo, title, "spec" if args.spec else "chat", spec_text=spec_text)
    profile = stackmod.write(repo, run.stack_path)

    baseline = None
    integration = None
    if not args.no_baseline:
        integration, _branch = worktreemod.create_integration(run)
        setup = worktreemod.run_setup(run, integration, profile)
        if setup is not None and not setup.ok:
            run.state["setup_warning"] = setup.stderr.strip()[-2000:] or setup.stdout.strip()[-2000:]
            run.save()
        baseline = gatesmod.capture_baseline(run, integration, profile=profile)

    if args.kind:
        run.set_kind_override(args.kind)
    run.set_phase("grill")
    ledgermod.append(run, "run started ({} mode) at base {}".format(run.state["mode"], run.base_commit[:7]))

    payload = {
        "run_id": run.run_id,
        "run_dir": str(run.root),
        "spec": str(run.spec_path),
        "stack": str(run.stack_path),
        "tasks": str(run.tasks_path),
        "base_commit": run.base_commit,
        "base_branch": run.state["base_branch"],
        "integration_branch": run.integration_branch,
        "integration_worktree": str(integration) if integration else None,
        "stack_summary": stackmod.summary_line(profile),
        "specialist_skills": profile.get("specialist_skills", []),
        "baseline": (baseline or {}).get("summary"),
        "gitignore_updated": gitignored,
        "kind_override": run.kind_override,
        "warnings": problems,
    }

    lines = [
        "run {} ready".format(run.run_id),
        "  dir:    {}".format(run.root),
        "  spec:   {}".format(run.spec_path),
        "  stack:  {}".format(stackmod.summary_line(profile)),
        "  skills: {}".format(", ".join(profile.get("specialist_skills") or []) or "-"),
        "  base:   {} on {}".format(run.base_commit[:7], run.state["base_branch"]),
        "  branch: {}".format(run.integration_branch),
    ]
    if gitignored:
        lines.append("")
        lines.append(
            "added cod-ag's entries to .gitignore - an uncommitted change in "
            "your tree, left for you to review and commit"
        )
    if baseline:
        lines.append("")
        lines.append(gatesmod.render(baseline))
    if problems:
        lines.append("")
        lines.append("proceeding despite: " + "; ".join(problems))
    emit(args, payload, "\n".join(lines))
    return EXIT_OK


def _spec_title(text):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


# --------------------------------------------------------------------------
# stack
# --------------------------------------------------------------------------


def cmd_stack(args):
    repo = resolve_repo(args)
    if args.stack_command == "detect":
        try:
            run = Run.load(repo, getattr(args, "run", None))
            target = run.stack_path
        except RunError:
            target = None
        profile = stackmod.detect(repo)
        if target:
            osenv.write_json(target, profile)
        emit(args, profile, stackmod.summary_line(profile))
        return EXIT_OK
    if args.stack_command == "show":
        run = resolve_run(args)
        profile = load_stack(run)
        emit(args, profile, stackmod.summary_line(profile))
        return EXIT_OK
    raise CliError("unknown stack command")


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def cmd_plan(args):
    run = resolve_run(args)
    if args.plan_command == "validate":
        try:
            doc = load_plan(run)
        except CliError as exc:
            emit(args, {"ok": False, "errors": [str(exc)], "warnings": []}, "ERROR: {}".format(exc))
            return EXIT_FAIL
        report = schema.validate(doc)
        emit(args, report.as_dict(), report.text())
        return EXIT_OK if report.ok else EXIT_FAIL

    if args.plan_command == "show":
        doc = load_plan(run)
        emit(args, doc, tasksmod.render_table(doc))
        return EXIT_OK

    if args.plan_command == "waves":
        doc = load_plan(run)
        layout = tasksmod.waves(doc)
        text = "\n".join(
            "wave {}: {}".format(i + 1, ", ".join(group)) for i, group in enumerate(layout)
        )
        emit(args, {"waves": layout}, text or "(no waves; check for a dependency cycle)")
        return EXIT_OK

    raise CliError("unknown plan command")


# --------------------------------------------------------------------------
# waves and tasks
# --------------------------------------------------------------------------


def cmd_wave(args):
    run = resolve_run(args)
    doc = load_plan(run)
    if args.wave_command == "next":
        ready = tasksmod.ready(doc)
        limit = args.limit or run.config.get("parallel", 3)
        batch = ready[:limit]
        default_model = run.config.get("models", {}).get("executor", "haiku")
        payload = {
            "ready": batch,
            "deferred": ready[limit:],
            "remaining": tasksmod.remaining(doc),
            "counts": tasksmod.counts(doc),
            "parallel_limit": limit,
            # Which model to dispatch each executor on, so the orchestrator
            # does not have to look each slice up separately.
            "models": {
                slice_id: (tasksmod.get(doc, slice_id).get("model") or default_model)
                for slice_id in batch
            },
            "escalated_model": run.config.get("models", {}).get("executor_escalated", "sonnet"),
        }
        text = " ".join(
            "{} ({})".format(slice_id, payload["models"][slice_id]) for slice_id in batch
        )
        if not batch and payload["remaining"]:
            text = "(nothing ready; blocked on {})".format(", ".join(payload["remaining"]))
        elif not batch:
            text = "(all slices finished)"
        emit(args, payload, text)
        return EXIT_OK
    raise CliError("unknown wave command")


def cmd_task(args):
    run = resolve_run(args)
    if args.task_command == "set":
        value = _coerce(args.value, args.type)
        try:
            previous = tasksmod.set_field(run.tasks_path, args.slice, args.field, value)
        except tasksmod.TaskError as exc:
            raise CliError(str(exc))
        emit(
            args,
            {"slice": args.slice, "field": args.field, "value": value, "previous": previous},
            "{}.{} = {}".format(args.slice, args.field, value),
        )
        return EXIT_OK

    if args.task_command == "status":
        try:
            previous = tasksmod.set_status(run.tasks_path, args.slice, args.value)
        except tasksmod.TaskError as exc:
            raise CliError(str(exc))
        emit(
            args,
            {"slice": args.slice, "status": args.value, "previous": previous},
            "{}: {} -> {}".format(args.slice, previous, args.value),
        )
        return EXIT_OK

    if args.task_command == "commits":
        commits = tasksmod.record_commits(run.tasks_path, args.slice, base=args.base, head=args.head)
        emit(args, {"slice": args.slice, "commits": commits}, "{}: {}".format(args.slice, commits))
        return EXIT_OK

    if args.task_command == "show":
        doc = load_plan(run)
        try:
            item = tasksmod.get(doc, args.slice)
        except tasksmod.TaskError as exc:
            raise CliError(str(exc))
        emit(args, item, miniyaml.dumps(item))
        return EXIT_OK

    raise CliError("unknown task command")


def _coerce(text, kind):
    if kind == "str":
        return text
    if kind == "int":
        return int(text)
    if kind == "bool":
        return text.strip().lower() in ("1", "true", "yes", "on")
    if kind == "json":
        return json.loads(text)
    if text.lower() in ("null", "none", "~"):
        return None
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        return text


# --------------------------------------------------------------------------
# worktrees and briefs
# --------------------------------------------------------------------------


def cmd_worktree(args):
    run = resolve_run(args)
    if args.worktree_command == "create":
        doc = load_plan(run)
        profile = load_stack(run)
        created = []
        for slice_id in args.slices:
            try:
                tasksmod.get(doc, slice_id)
            except tasksmod.TaskError as exc:
                raise CliError(str(exc))
            path, branch, setup = worktreemod.create(
                run, slice_id, setup=not args.no_setup, stack_profile=profile
            )
            tasksmod.set_field(run.tasks_path, slice_id, "worktree", str(path))
            tasksmod.set_field(run.tasks_path, slice_id, "branch", branch)
            tasksmod.record_commits(run.tasks_path, slice_id, base=run.base_commit)
            created.append(
                {
                    "slice": slice_id,
                    "path": str(path),
                    "branch": branch,
                    "setup_ok": None if setup is None else setup.ok,
                    "setup_output": "" if setup is None else setup.stderr.strip()[-1000:],
                }
            )
        text = "\n".join("{}  {}  {}".format(c["slice"], c["branch"], c["path"]) for c in created)
        failed = [c for c in created if c["setup_ok"] is False]
        if failed:
            text += "\n\nsetup failed in: {}".format(", ".join(c["slice"] for c in failed))
        emit(args, {"created": created}, text)
        return EXIT_FAIL if failed and args.strict_setup else EXIT_OK

    if args.worktree_command == "integration":
        path, branch = worktreemod.create_integration(run)
        emit(args, {"path": str(path), "branch": branch}, "{}  {}".format(branch, path))
        return EXIT_OK

    if args.worktree_command == "rm":
        removed = [
            slice_id
            for slice_id in args.slices
            if worktreemod.remove(run, slice_id, delete_branch=args.delete_branch)
        ]
        emit(args, {"removed": removed}, "removed: {}".format(", ".join(removed) or "-"))
        return EXIT_OK

    if args.worktree_command == "reap":
        removed = worktreemod.reap(
            run, keep_integration=not args.all, delete_branches=args.delete_branch
        )
        orphans = worktreemod.reap_orphans(run.repo)
        emit(
            args,
            {"removed": removed, "orphans": orphans},
            "removed: {}".format(", ".join(removed) or "-"),
        )
        return EXIT_OK

    if args.worktree_command == "list":
        listed = {str(p): b for p, b in worktreemod.existing(run.repo).items()}
        text = "\n".join("{:<40} {}".format(b or "-", p) for p, b in listed.items())
        emit(args, listed, text)
        return EXIT_OK

    raise CliError("unknown worktree command")


def cmd_brief(args):
    run = resolve_run(args)
    doc = load_plan(run)
    profile = load_stack(run)
    written = []
    for slice_id in args.slices:
        try:
            path = briefmod.write(run, doc, slice_id, profile)
        except KeyError as exc:
            raise CliError(str(exc))
        written.append(str(path))
    emit(args, {"briefs": written}, "\n".join(written))
    return EXIT_OK


# --------------------------------------------------------------------------
# merge, gates, verify
# --------------------------------------------------------------------------


def cmd_merge(args):
    run = resolve_run(args)
    doc = load_plan(run)
    try:
        if args.continue_merge:
            state = mergemod.resume(run, doc)
        else:
            state = mergemod.run_merge(run, doc, reset=args.reset)
    except mergemod.MergeError as exc:
        # Operational state, not a usage mistake: exit 1 so the orchestrator
        # treats it the same as any other "merge is not done yet".
        sys.stderr.write("codag: {}\n".format(exc))
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "status": "conflict", "error": str(exc)}, indent=2))
        return EXIT_FAIL

    report = run.cycle_dir() / "merge-report.md"
    payload = dict(state)
    payload["report"] = str(report)
    payload["head"] = mergemod.integration_head(run)

    lines = ["merge {}".format(state["status"])]
    lines.append("  merged:  {}".format(", ".join(state.get("merged") or []) or "-"))
    if state.get("status") == "conflict":
        lines.append("  slice:   {}".format(state.get("conflicted")))
        lines.append("  files:   {}".format(", ".join(state.get("conflicts") or [])))
        lines.append("  worktree:{}".format(state.get("worktree")))
    lines.append("  report:  {}".format(report))
    emit(args, payload, "\n".join(lines))
    return EXIT_FAIL if state.get("status") == "conflict" else EXIT_OK


def cmd_gates(args):
    run = resolve_run(args)
    profile = load_stack(run)
    where = args.cwd or mergemod.state_of(run).get("worktree") or worktreemod.integration_path(run)
    if not pathlib.Path(where).exists():
        raise CliError("nothing to test at {} (create the integration worktree first)".format(where))

    if args.gates_command == "baseline":
        report = gatesmod.capture_baseline(run, where, profile=profile)
        emit(args, report, gatesmod.render(report))
        return EXIT_OK

    if args.gates_command == "run":
        report = gatesmod.run_and_classify(run, where, profile=profile, only=args.only)
        emit(args, report, gatesmod.render(report) + "\n\nwrote {}".format(report["path"]))
        return EXIT_OK if gatesmod.passed(report) else EXIT_FAIL

    raise CliError("unknown gates command")


def cmd_diffpkg(args):
    run = resolve_run(args)
    where = mergemod.state_of(run).get("worktree") or run.repo
    base = args.base or run.base_commit
    head = args.head or mergemod.integration_head(run) or run.integration_branch
    out = pathlib.Path(args.out) if args.out else run.cycle_dir() / "review.diff"
    try:
        path = diffpkg.write(run.repo, base, head, out=out, cwd=where)
    except ValueError as exc:
        raise CliError(str(exc))
    payload = {
        "path": str(path),
        "base": base,
        "head": head,
        "files": diffpkg.changed_files(run.repo, base, head, cwd=where),
    }
    emit(args, payload, str(path))
    return EXIT_OK


def cmd_verify_package(args):
    """Everything the verifier agent needs, in one call."""
    run = resolve_run(args)
    doc = load_plan(run)
    profile = load_stack(run)
    where = mergemod.state_of(run).get("worktree") or worktreemod.integration_path(run)
    if not pathlib.Path(where).exists():
        raise CliError("no integration worktree; run 'codag merge' first")

    report = gatesmod.run_and_classify(run, where, profile=profile)
    head = mergemod.integration_head(run)
    review = diffpkg.write(run.repo, run.base_commit, head, out=run.cycle_dir() / "review.diff", cwd=where)

    criteria = []
    for item in tasksmod.slices(doc):
        for criterion in item.get("acceptance") or []:
            if isinstance(criterion, dict):
                criteria.append(
                    {
                        "slice": item.get("id"),
                        "id": criterion.get("id"),
                        "text": criterion.get("text"),
                        "status": item.get("status"),
                    }
                )

    payload = {
        "run_id": run.run_id,
        "cycle": run.cycle,
        "worktree": str(where),
        "base": run.base_commit,
        "head": head,
        "gates": report["path"],
        "gates_blocking": gatesmod.blocking(report),
        "review": str(review),
        "merge_report": str(run.cycle_dir() / "merge-report.md"),
        "spec": str(run.spec_path),
        "tasks": str(run.tasks_path),
        "criteria": criteria,
        "assumptions": doc.get("assumptions") or [],
    }
    lines = [
        "verify package for {} cycle {}".format(run.run_id, run.cycle),
        "  gates:   {}".format(payload["gates"]),
        "  review:  {}".format(payload["review"]),
        "  merge:   {}".format(payload["merge_report"]),
        "  spec:    {}".format(payload["spec"]),
        "  tasks:   {}".format(payload["tasks"]),
        "  criteria:{} across {} slices".format(len(criteria), len(tasksmod.slices(doc))),
        "",
        gatesmod.render(report),
    ]
    emit(args, payload, "\n".join(lines))
    return EXIT_OK


# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------


def cmd_next(args):
    """The single next thing to do. The orchestrator loops on this."""
    run = resolve_run(args)
    action = machinemod.next_action(run, stack_profile=load_stack(run))
    emit(args, action, machinemod.render(action))
    if action["action"] == "stop":
        return EXIT_OK if action.get("outcome") == "done" else EXIT_FAIL
    if action["action"] == "escalate":
        return EXIT_FAIL
    return EXIT_OK


# --------------------------------------------------------------------------
# agent reports
# --------------------------------------------------------------------------


def cmd_report(args):
    """How an agent puts its result back into the run."""
    run = resolve_run(args)
    try:
        if args.role:
            result = reportmod.record_role(
                run, args.role, args.status, detail=args.detail, tests=args.tests
            )
            text = "{} {}".format(args.role, result["status"].lower())
            if result.get("verdict"):
                text += "\nwrote {}".format(result["verdict"])
        else:
            if not args.slice:
                raise reportmod.ReportError("pass --slice <id> or --role synthesizer")
            result = reportmod.record_slice(
                run,
                args.slice,
                args.status,
                tests=args.tests,
                concerns=args.concerns,
                reason=args.reason,
                head=args.head,
                force=args.force,
                profile=load_stack(run),
            )
            text = "{}: {} (slice now {})".format(
                result["slice"], result["status"], result["slice_status"]
            )
    except reportmod.ReportError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            sys.stderr.write("codag: {}\n".format(exc))
        return EXIT_USAGE
    emit(args, result, text)
    return EXIT_OK


def cmd_answer(args):
    """Record the user's answers to a grill round."""
    run = resolve_run(args)
    path = (
        pathlib.Path(args.questions)
        if args.questions
        else dispatchmod.questions_path(run, run.grill_rounds + 1)
    )
    try:
        answers = reportmod.parse_pairs(args.pairs)
        notes = reportmod.parse_pairs(args.note)
        if args.file:
            loaded = miniyaml.load(args.file) or {}
            if not isinstance(loaded, dict):
                raise reportmod.ReportError("{} must contain a mapping of QID to answer".format(args.file))
            merged = {str(k): str(v) for k, v in loaded.items()}
            merged.update(answers)
            answers = merged
        result = reportmod.record_answers(run, path, answers, notes=notes, free_text=args.free)
    except (reportmod.ReportError, miniyaml.YamlError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            sys.stderr.write("codag: {}\n".format(exc))
        return EXIT_USAGE

    text = "round {} recorded ({} answered, {} left to the planner)".format(
        result["round"], result["answered"], len(result["unanswered"])
    )
    emit(args, result, text)
    return EXIT_OK


def cmd_approve(args):
    run = resolve_run(args)
    decision = "approved" if args.yes else "aborted" if args.abort else "revise"
    try:
        result = reportmod.record_approval(run, decision, feedback=args.revise)
    except reportmod.ReportError as exc:
        raise CliError(str(exc))
    if decision == "aborted":
        worktreemod.reap(run, keep_integration=False)
        run.set_phase("aborted")
    emit(args, result, "plan {}".format(decision))
    return EXIT_OK


def cmd_verdict(args):
    run = resolve_run(args)
    try:
        result = reportmod.require_verdict(run)
    except reportmod.ReportError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            sys.stderr.write("codag: {}\n".format(exc))
        return EXIT_USAGE

    payload = {
        "verdict": result,
        "cycle": run.cycle,
        "path": str(run.cycle_dir() / "verdict.md"),
    }
    emit(args, payload, "VERDICT: {}".format(result))
    return EXIT_OK if result == "PASS" else EXIT_FAIL


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def cmd_status(args):
    repo = resolve_repo(args)
    if args.all:
        summaries = runmod.list_runs(repo)
        text = "\n".join(
            "{:<34} {:<10} cycle {}/{}".format(s["run_id"], s["phase"], s["cycle"], s["max_cycles"])
            for s in summaries
        )
        emit(args, {"runs": summaries}, text or "(no runs yet)")
        return EXIT_OK

    run = resolve_run(args)
    summary = run.summary()
    lines = [
        "run {}".format(run.run_id),
        "  phase:  {}".format(summary["phase"]),
        "  cycle:  {}/{}".format(summary["cycle"], summary["max_cycles"]),
        "  mode:   {}".format(summary["mode"]),
        "  base:   {} on {}".format(summary["base_commit"], summary["base_branch"]),
        "  branch: {}".format(summary["integration_branch"]),
    ]
    if pathlib.Path(run.tasks_path).exists():
        doc = load_plan(run)
        summary["counts"] = tasksmod.counts(doc)
        summary["ready"] = tasksmod.ready(doc)
        lines.append("  slices: {}".format(_counts_text(summary["counts"])))
        lines.append("  ready:  {}".format(", ".join(summary["ready"]) or "-"))
    summary["ledger"] = ledgermod.entries(run)[-5:]
    if summary["ledger"]:
        lines.append("")
        lines.append("recent:")
        lines.extend("  {}".format(entry) for entry in summary["ledger"])
    emit(args, summary, "\n".join(lines))
    return EXIT_OK


def _counts_text(counts):
    return ", ".join("{} {}".format(v, k) for k, v in counts.items() if v)


def cmd_ledger(args):
    run = resolve_run(args)
    if args.text:
        entry = ledgermod.append(run, args.text)
        emit(args, {"entry": entry}, entry)
        return EXIT_OK
    listed = ledgermod.entries(run)
    emit(args, {"entries": listed, "completed": sorted(ledgermod.completed_slices(run))}, "\n".join(listed))
    return EXIT_OK


def cmd_cycle(args):
    run = resolve_run(args)
    if run.cycles_exhausted():
        raise CliError(
            "cycle cap reached ({} of {}); stop and report the unmet criteria".format(
                run.cycle, run.config.get("max_cycles", 3)
            )
        )
    doc = load_plan(run)
    carried = tasksmod.update(run.tasks_path, lambda d: tasksmod.carry_forward(d, set(tasksmod.ids(d))))
    number = run.advance_cycle()
    run.state["awaiting_replan"] = True
    run.save()
    previous = run.root / "cycle-{}".format(number - 1) / "tasks-snapshot.yaml"
    miniyaml.dump(doc, previous)
    ledgermod.append(run, "advanced to cycle {} (carried {})".format(number, ", ".join(carried) or "-"))
    emit(
        args,
        {"cycle": number, "carried": carried, "cycle_dir": str(run.cycle_dir()), "snapshot": str(previous)},
        "cycle {}  carried: {}".format(number, ", ".join(carried) or "-"),
    )
    return EXIT_OK


def cmd_finish(args):
    run = resolve_run(args)
    removed = worktreemod.reap(run, keep_integration=not args.reap_integration)
    run.set_phase("done")
    ledgermod.append(run, "run complete on {}".format(run.integration_branch))
    payload = {
        "run_id": run.run_id,
        "integration_branch": run.integration_branch,
        "base_commit": run.base_commit,
        "base_branch": run.state["base_branch"],
        "removed_worktrees": removed,
        "review_command": "git diff {}..{}".format(run.base_commit[:12], run.integration_branch),
    }
    lines = [
        "DONE",
        "",
        "branch: {}".format(run.integration_branch),
        "review: {}".format(payload["review_command"]),
        "merge:  git merge {}".format(run.integration_branch),
        "",
        "nothing was committed to your branch {}".format(run.state["base_branch"]),
    ]
    emit(args, payload, "\n".join(lines))
    return EXIT_OK


def cmd_abort(args):
    run = resolve_run(args)
    removed = worktreemod.reap(run, keep_integration=False, delete_branches=args.delete_branches)
    worktreemod.reap_orphans(run.repo)
    run.set_phase("aborted")
    ledgermod.append(run, "run aborted by request")
    emit(
        args,
        {"run_id": run.run_id, "removed": removed, "branches_deleted": bool(args.delete_branches)},
        "aborted {}; removed {} worktree(s)".format(run.run_id, len(removed)),
    )
    return EXIT_OK


def cmd_resume(args):
    run = resolve_run(args)
    payload = run.summary()
    payload["completed_slices"] = sorted(ledgermod.completed_slices(run))
    payload["ledger"] = ledgermod.entries(run)
    if pathlib.Path(run.tasks_path).exists():
        doc = load_plan(run)
        payload["counts"] = tasksmod.counts(doc)
        payload["ready"] = tasksmod.ready(doc)
        payload["remaining"] = tasksmod.remaining(doc)
    payload["merge"] = mergemod.state_of(run)
    lines = [
        "resuming {} at phase '{}', cycle {}".format(run.run_id, run.phase, run.cycle),
        "",
        "trust this and git log over your recollection:",
        "  completed: {}".format(", ".join(payload["completed_slices"]) or "-"),
        "  ready:     {}".format(", ".join(payload.get("ready") or []) or "-"),
        "  remaining: {}".format(", ".join(payload.get("remaining") or []) or "-"),
        "  merge:     {}".format(payload["merge"].get("status")),
    ]
    emit(args, payload, "\n".join(lines))
    return EXIT_OK


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def _global_options(parser, suppress=False):
    """The three flags every subcommand accepts, in any position.

    Repeating them on each subparser with ``SUPPRESS`` defaults means
    ``codag --json plan validate`` and ``codag plan validate --json`` both
    work; without SUPPRESS the subparser's default would clobber the
    value parsed at the top level.
    """
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--repo", default=default, help="path inside the target repository (default: cwd)")
    parser.add_argument("--run", default=default, help="run id (default: the most recent run)")
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="machine-readable output",
    )
    return parser


COMMON = _global_options(argparse.ArgumentParser(add_help=False), suppress=True)


def build_parser():
    parser = _global_options(
        argparse.ArgumentParser(prog="codag", description=__doc__.splitlines()[0])
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(group, name, **kwargs):
        kwargs.setdefault("parents", [COMMON])
        return group.add_parser(name, **kwargs)

    p = add(sub, "init", help="start a run: preflight, run dir, stack, baseline gates")
    p.add_argument("--prompt", help="feature request, for chat mode")
    p.add_argument("--spec", help="path to a markdown spec file")
    p.add_argument(
        "--kind",
        choices=runmod.KINDS,
        help="override the planner's classification; a bugfix skips the end-to-end phase",
    )
    p.add_argument("--force", action="store_true", help="proceed despite preflight problems")
    p.add_argument("--no-baseline", action="store_true", help="skip the baseline gate run")
    p.set_defaults(func=cmd_init)

    p = add(sub, "stack", help="stack detection")
    s = p.add_subparsers(dest="stack_command", required=True)
    add(s, "detect").set_defaults(func=cmd_stack)
    add(s, "show").set_defaults(func=cmd_stack)

    p = add(sub, "plan", help="validate and inspect tasks.yaml")
    s = p.add_subparsers(dest="plan_command", required=True)
    add(s, "validate").set_defaults(func=cmd_plan)
    add(s, "show").set_defaults(func=cmd_plan)
    add(s, "waves").set_defaults(func=cmd_plan)

    p = add(sub, "wave", help="which slices are dispatchable now")
    s = p.add_subparsers(dest="wave_command", required=True)
    nxt = add(s, "next")
    nxt.add_argument("--limit", type=int, help="override the parallel cap")
    nxt.set_defaults(func=cmd_wave)

    p = add(sub, "task", help="read and mutate one slice")
    s = p.add_subparsers(dest="task_command", required=True)
    setter = add(s, "set")
    setter.add_argument("slice")
    setter.add_argument("field")
    setter.add_argument("value")
    setter.add_argument("--type", choices=("auto", "str", "int", "bool", "json"), default="auto")
    setter.set_defaults(func=cmd_task)
    status = add(s, "status")
    status.add_argument("slice")
    status.add_argument("value", choices=schema.STATUSES)
    status.set_defaults(func=cmd_task)
    commits = add(s, "commits")
    commits.add_argument("slice")
    commits.add_argument("--base")
    commits.add_argument("--head")
    commits.set_defaults(func=cmd_task)
    show = add(s, "show")
    show.add_argument("slice")
    show.set_defaults(func=cmd_task)

    p = add(sub, "worktree", help="isolated checkouts for executors")
    s = p.add_subparsers(dest="worktree_command", required=True)
    create = add(s, "create")
    create.add_argument("slices", nargs="+")
    create.add_argument("--no-setup", action="store_true", help="skip dependency install")
    create.add_argument("--strict-setup", action="store_true", help="fail if setup fails")
    create.set_defaults(func=cmd_worktree)
    add(s, "integration").set_defaults(func=cmd_worktree)
    remove = add(s, "rm")
    remove.add_argument("slices", nargs="+")
    remove.add_argument("--delete-branch", action="store_true")
    remove.set_defaults(func=cmd_worktree)
    reap = add(s, "reap")
    reap.add_argument("--all", action="store_true", help="include the integration worktree")
    reap.add_argument("--delete-branch", action="store_true")
    reap.set_defaults(func=cmd_worktree)
    add(s, "list").set_defaults(func=cmd_worktree)

    p = add(sub, "brief", help="write self-contained slice briefs")
    p.add_argument("slices", nargs="+")
    p.set_defaults(func=cmd_brief)

    p = add(sub, "merge", help="merge slice branches into the integration branch")
    p.add_argument("--continue", dest="continue_merge", action="store_true", help="after resolving conflicts")
    p.add_argument("--reset", action="store_true", help="restart the integration branch from base")
    p.set_defaults(func=cmd_merge)

    p = add(sub, "gates", help="build / typecheck / lint / test")
    s = p.add_subparsers(dest="gates_command", required=True)
    runner = add(s, "run")
    runner.add_argument("--only", nargs="+", choices=gatesmod.GATE_ORDER)
    runner.add_argument("--cwd", help="where to run (default: the integration worktree)")
    runner.set_defaults(func=cmd_gates)
    base = add(s, "baseline")
    base.add_argument("--cwd")
    base.set_defaults(func=cmd_gates)

    p = add(sub, "diffpkg", help="one-file review package")
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--out")
    p.set_defaults(func=cmd_diffpkg)

    p = add(sub, "verify-package", help="gates + diff + criteria for the verifier")
    p.set_defaults(func=cmd_verify_package)

    p = add(sub, "next", help="the single next action; the orchestrator loops on this")
    p.set_defaults(func=cmd_next)

    p = add(sub, "report", help="how an agent records its result")
    p.add_argument("--slice", help="slice id, for an executor")
    p.add_argument(
        "--role", choices=tuple(sorted(reportmod.ROLE_STATUSES)), help="for a non-slice agent"
    )
    p.add_argument("--status", required=True, help="DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED | CLEAN | ESCALATE")
    p.add_argument("--tests", help="one-line test summary")
    p.add_argument("--concerns", help="what needs a human's eye")
    p.add_argument("--reason", help="why you are blocked or need context")
    p.add_argument("--detail", help="what disagrees, for an ESCALATE")
    p.add_argument("--head", help="override the commit sha (default: your worktree's HEAD)")
    p.add_argument("--force", action="store_true", help="skip the DONE checks (use only with a reason)")
    p.set_defaults(func=cmd_report)

    p = add(sub, "answer", help="record the user's answers to a grill round")
    p.add_argument("pairs", nargs="*", metavar="QID=ANSWER")
    p.add_argument("--note", action="append", metavar="QID=TEXT", help="extra note on one answer")
    p.add_argument("--free", help="unstructured extra context from the user")
    p.add_argument("--file", help="YAML mapping of QID to answer, instead of pairs")
    p.add_argument("--questions", help="questions file (default: the current round's)")
    p.set_defaults(func=cmd_answer)

    p = add(sub, "approve", help="record the plan approval gate")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--yes", action="store_true", help="approve and start executing")
    group.add_argument("--revise", metavar="FEEDBACK", help="send the plan back with this feedback")
    group.add_argument("--abort", action="store_true", help="stop the run and clean up")
    p.set_defaults(func=cmd_approve)

    p = add(sub, "verdict", help="read the verifier's PASS/FAIL back")
    p.set_defaults(func=cmd_verdict)

    p = add(sub, "status", help="what is this run doing")
    p.add_argument("--all", action="store_true", help="list every run")
    p.set_defaults(func=cmd_status)

    p = add(sub, "ledger", help="read or append the durable progress ledger")
    p.add_argument("text", nargs="?", help="entry to append; omit to read")
    p.set_defaults(func=cmd_ledger)

    p = add(sub, "cycle", help="advance to the next replan cycle")
    p.set_defaults(func=cmd_cycle)

    p = add(sub, "finish", help="conclude a passing run")
    p.add_argument("--reap-integration", action="store_true", help="also remove the integration worktree")
    p.set_defaults(func=cmd_finish)

    p = add(sub, "abort", help="stop a run and clean up")
    p.add_argument("--delete-branches", action="store_true")
    p.set_defaults(func=cmd_abort)

    p = add(sub, "resume", help="what to trust after a crash or compaction")
    p.set_defaults(func=cmd_resume)

    return parser


def main(argv=None):
    osenv.require_python()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            sys.stderr.write("codag: {}\n".format(exc))
        return EXIT_USAGE
    except (RunError, tasksmod.TaskError, worktreemod.WorktreeError, mergemod.MergeError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            sys.stderr.write("codag: {}\n".format(exc))
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
