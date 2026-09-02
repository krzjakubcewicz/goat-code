"""Deterministic integration of executor branches.

The synthesizer agent is expensive and prone to drifting into rewriting
code. So the machine does the whole mechanical part first - create the
integration branch, merge slice branches in dependency order, stop at the
first conflict - and only hands over when there is a genuine judgement call
to make. A clean merge never wakes the agent at all.

Merge state lives in ``state.json`` so a resumed run knows exactly which
branches already landed.
"""

from __future__ import annotations

import pathlib

from . import osenv, tasks, worktree


class MergeError(RuntimeError):
    """The merge could not proceed for a reason the agent cannot fix."""


def state_of(run):
    return run.state.get("merge") or {
        "status": "not-started",
        "merged": [],
        "pending": [],
        "conflicted": None,
        "conflicts": [],
    }


def _store(run, state):
    run.state["merge"] = state
    run.save()
    return state


def start(run, doc, reset=False):
    """Prepare the integration worktree and queue the branches to merge."""
    path, _branch = worktree.create_integration(run)
    order = tasks.merge_order(doc)
    if reset:
        osenv.git(["merge", "--abort"], cwd=path)
        osenv.git(["reset", "--hard", run.base_commit], cwd=path, check=True)
    state = {
        "status": "in-progress",
        "worktree": str(path),
        "merged": [],
        "pending": list(order),
        "conflicted": None,
        "conflicts": [],
    }
    return _store(run, state)


def run_merge(run, doc, reset=False):
    """Merge every finished slice branch, stopping at the first conflict.

    Returns the merge state. ``status`` is ``clean``, ``conflict`` or
    ``empty``.
    """
    state = state_of(run)
    if state.get("status") in ("not-started", "clean", "empty") or reset:
        state = start(run, doc, reset=reset)

    path = pathlib.Path(state["worktree"])
    if not state["pending"] and not state["merged"]:
        state["status"] = "empty"
        return _store(run, state)

    while state["pending"]:
        slice_id = state["pending"][0]
        branch = worktree.branch_name(run.run_id, slice_id)
        if not _has_commits(run.repo, run.base_commit, branch):
            state["pending"].pop(0)
            state["merged"].append(slice_id)
            continue

        result = osenv.git(
            ["merge", "--no-ff", "--no-edit", "-m", "goatcode: merge slice {}".format(slice_id), branch],
            cwd=path,
        )
        if result.ok:
            state["pending"].pop(0)
            state["merged"].append(slice_id)
            continue

        conflicts = unmerged_paths(path)
        if not conflicts:
            osenv.git(["merge", "--abort"], cwd=path)
            raise MergeError(
                "merging {} failed without conflicts: {}".format(
                    slice_id, result.stderr.strip() or result.stdout.strip()
                )
            )
        state["status"] = "conflict"
        state["conflicted"] = slice_id
        state["conflicts"] = conflicts
        _store(run, state)
        write_report(run, doc)
        return state

    state["status"] = "clean"
    state["conflicted"] = None
    state["conflicts"] = []
    _store(run, state)
    write_report(run, doc)
    return state


def resume(run, doc):
    """Commit a conflict the synthesizer resolved, then keep merging."""
    state = state_of(run)
    if state.get("status") != "conflict":
        return run_merge(run, doc)

    path = pathlib.Path(state["worktree"])

    # A file the synthesizer edited stays "unmerged" until it is staged, so
    # check the text for markers first, then stage, then trust git.
    marked = marker_paths(path, unmerged_paths(path))
    if marked:
        raise MergeError("these paths still have conflict markers: {}".format(", ".join(marked)))

    slice_id = state.get("conflicted")
    osenv.git(["add", "-A"], cwd=path, check=True)
    still_unmerged = unmerged_paths(path)
    if still_unmerged:
        raise MergeError(
            "git still reports these paths as unmerged: {}".format(", ".join(still_unmerged))
        )
    if _merge_in_progress(path):
        result = osenv.git(["commit", "--no-edit"], cwd=path)
        if not result.ok:
            raise MergeError("could not commit the resolved merge: {}".format(result.stderr.strip()))

    if state["pending"] and state["pending"][0] == slice_id:
        state["pending"].pop(0)
    state["merged"].append(slice_id)
    state["status"] = "in-progress"
    state["conflicted"] = None
    state["conflicts"] = []
    _store(run, state)
    return run_merge(run, doc)


def unmerged_paths(path):
    """Files git still considers conflicted."""
    result = osenv.git(["diff", "--name-only", "--diff-filter=U"], cwd=path)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def marker_paths(path, candidates=None):
    """Of ``candidates``, the files that still contain conflict markers."""
    root = pathlib.Path(path)
    out = []
    for relpath in candidates if candidates is not None else unmerged_paths(root):
        target = root / relpath
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<<<<<<<" in text or ">>>>>>>" in text:
            out.append(relpath)
    return out


def _merge_in_progress(path):
    return (pathlib.Path(path) / ".git").exists() and osenv.git(
        ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"], cwd=path
    ).ok


def _has_commits(repo, base, ref):
    result = osenv.git(["rev-list", "--count", "{}..{}".format(base, ref)], cwd=repo)
    if not result.ok:
        return False
    try:
        return int(result.out or "0") > 0
    except ValueError:
        return False


def write_report(run, doc, path=None):
    """Write ``merge-report.md``: what landed, what clashed, what the
    synthesizer changed and why."""
    state = state_of(run)
    target = pathlib.Path(path) if path else run.cycle_dir() / "merge-report.md"
    lines = []
    add = lines.append

    add("# Merge report - cycle {}".format(run.cycle))
    add("")
    add("Integration branch: `{}`".format(run.integration_branch))
    add("Base commit: `{}`".format(run.base_commit[:12]))
    add("Worktree: `{}`".format(state.get("worktree", "")))
    add("Status: **{}**".format(state.get("status", "unknown")))
    add("")

    add("## Branches merged")
    add("")
    if state.get("merged"):
        for slice_id in state["merged"]:
            title = _title(doc, slice_id)
            add("- `{}` - {}".format(worktree.branch_name(run.run_id, slice_id), title))
    else:
        add("_none yet_")
    add("")

    if state.get("pending"):
        add("## Still queued")
        add("")
        for slice_id in state["pending"]:
            add("- `{}` - {}".format(worktree.branch_name(run.run_id, slice_id), _title(doc, slice_id)))
        add("")

    if state.get("status") == "conflict":
        add("## Conflict")
        add("")
        add("Merging slice `{}` ({}) hit conflicts in:".format(state["conflicted"], _title(doc, state["conflicted"])))
        add("")
        for conflict in state.get("conflicts", []):
            add("- `{}`".format(conflict))
        add("")
        add("Resolve these in the integration worktree, then run `goatcode merge --continue`.")
        add("")

    add("## Synthesizer edits")
    add("")
    add("Every edit outside a conflict hunk must be listed here with a one-line")
    add("justification. The verifier reads this list and treats anything")
    add("unjustified as a scope violation.")
    add("")
    add("| File | Change | Why it was needed to make the merge work |")
    add("| --- | --- | --- |")
    add("| _none_ | | |")
    add("")

    osenv.write_text(target, "\n".join(lines) + "\n")
    return target


def _title(doc, slice_id):
    for item in doc.get("slices") or []:
        if isinstance(item, dict) and item.get("id") == slice_id:
            return item.get("title", "")
    return ""


def integration_head(run):
    state = state_of(run)
    path = state.get("worktree")
    if not path or not pathlib.Path(path).exists():
        return None
    return worktree.head_commit(path)
