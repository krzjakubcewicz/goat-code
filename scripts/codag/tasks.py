"""Read and mutate tasks.yaml safely.

Several executor agents run at once, in separate processes and separate
worktrees, all reporting status into one file. Every mutation therefore
goes through :func:`update`, which takes a lock, re-reads from disk,
applies the change and writes atomically. Nothing here caches the document
across a mutation.
"""

from __future__ import annotations

import pathlib

from . import miniyaml, osenv, schema

TERMINAL = ("done", "carried")


class TaskError(RuntimeError):
    """The plan does not contain what the caller asked for."""


def load(path):
    """Read tasks.yaml. Raises :class:`TaskError` if it is absent."""
    path = pathlib.Path(path)
    if not path.exists():
        raise TaskError("no plan at {}".format(path))
    doc = miniyaml.load(path)
    if not isinstance(doc, dict):
        raise TaskError("{} does not contain a mapping".format(path))
    return doc


def save(path, doc):
    miniyaml.dump(doc, path)


def slices(doc):
    return [s for s in (doc.get("slices") or []) if isinstance(s, dict)]


def get(doc, slice_id):
    for item in slices(doc):
        if item.get("id") == slice_id:
            return item
    raise TaskError("no slice {!r} in the plan".format(slice_id))


def ids(doc):
    return [s.get("id") for s in slices(doc) if s.get("id")]


def update(path, mutate):
    """Apply ``mutate(doc)`` under a lock and write the result atomically.

    ``mutate`` receives the freshly-read document and may return a value,
    which :func:`update` passes back to the caller.
    """
    path = pathlib.Path(path)
    with osenv.FileLock(path):
        doc = load(path)
        result = mutate(doc)
        save(path, doc)
    return result


def set_field(path, slice_id, field, value):
    """Set one field on one slice. Returns the previous value."""

    def mutate(doc):
        item = get(doc, slice_id)
        previous = item.get(field)
        item[field] = value
        return previous

    return update(path, mutate)


def set_status(path, slice_id, status):
    if status not in schema.STATUSES:
        raise TaskError("status {!r} is not one of {}".format(status, ", ".join(schema.STATUSES)))
    return set_field(path, slice_id, "status", status)


def record_commits(path, slice_id, base=None, head=None):
    """Store the commit range an executor produced."""

    def mutate(doc):
        item = get(doc, slice_id)
        commits = item.get("commits")
        if not isinstance(commits, dict):
            commits = {"base": None, "head": None}
            item["commits"] = commits
        if base is not None:
            commits["base"] = base
        if head is not None:
            commits["head"] = head
        return dict(commits)

    return update(path, mutate)


def claim(path, slice_id):
    """Move a slice from pending to claimed. Returns False if already taken."""

    def mutate(doc):
        item = get(doc, slice_id)
        if item.get("status", "pending") != "pending":
            return False
        item["status"] = "claimed"
        return True

    return update(path, mutate)


def waves(doc):
    """Slice ids grouped into dependency waves."""
    return schema.waves(slices(doc))


def ready(doc):
    """Pending slices whose dependencies have all finished.

    This is what the orchestrator dispatches next, in one parallel batch.
    """
    by_id = {s.get("id"): s for s in slices(doc) if s.get("id")}
    out = []
    for item in slices(doc):
        if item.get("status", "pending") != "pending":
            continue
        deps = item.get("depends_on") or []
        if all(by_id.get(d, {}).get("status") in TERMINAL for d in deps if d in by_id):
            out.append(item.get("id"))
    return out


def blocked_on(doc, slice_id):
    """Dependencies of ``slice_id`` that are not finished yet."""
    item = get(doc, slice_id)
    by_id = {s.get("id"): s for s in slices(doc) if s.get("id")}
    return [d for d in (item.get("depends_on") or []) if by_id.get(d, {}).get("status") not in TERMINAL]


def remaining(doc):
    """Slice ids still to be executed, in any state other than finished."""
    return [s.get("id") for s in slices(doc) if s.get("status", "pending") not in TERMINAL]


def counts(doc):
    out = {status: 0 for status in schema.STATUSES}
    for item in slices(doc):
        status = item.get("status", "pending")
        out[status] = out.get(status, 0) + 1
    return out


def merge_order(doc):
    """Slice ids in the order the synthesizer should merge their branches.

    Dependency order, so a slice's prerequisites are always already in the
    integration branch when it lands.
    """
    order = []
    for wave in waves(doc):
        for slice_id in wave:
            item = get(doc, slice_id)
            if item.get("status") == "done":
                order.append(slice_id)
    return order


def carry_forward(doc, keep_ids):
    """Mark finished slices as carried so the next cycle never redoes them."""
    changed = []
    for item in slices(doc):
        if item.get("id") in keep_ids and item.get("status") == "done":
            item["status"] = "carried"
            changed.append(item.get("id"))
    return changed


def render_table(doc):
    """Human-readable plan summary for the approval gate."""
    layout = waves(doc)
    lines = []
    lines.append("goal: {}".format(doc.get("goal", "")))
    lines.append("run:  {}  cycle {}".format(doc.get("run_id", "?"), doc.get("cycle", "?")))
    constraints = doc.get("global_constraints") or []
    if constraints:
        lines.append("")
        lines.append("global constraints:")
        for constraint in constraints:
            lines.append("  - {}".format(constraint))
    assumptions = doc.get("assumptions") or []
    if assumptions:
        lines.append("")
        lines.append("assumptions (unresolved after grilling):")
        for assumption in assumptions:
            lines.append("  ! {}".format(assumption))

    if not layout:
        lines.append("")
        lines.append("(dependency graph has a cycle; run 'plan validate')")
        return "\n".join(lines)

    for index, group in enumerate(layout):
        lines.append("")
        lines.append("wave {} - {} slice(s) run in parallel".format(index + 1, len(group)))
        for slice_id in group:
            item = get(doc, slice_id)
            lines.append(
                "  {:<6} {:<9} {}".format(slice_id, item.get("status", "pending"), item.get("title", ""))
            )
            if item.get("intent"):
                lines.append("         intent: {}".format(item["intent"]))
            lines.append("         owns:   {}".format(", ".join(item.get("owns") or []) or "-"))
            criteria = item.get("acceptance") or []
            lines.append(
                "         checks: {}".format(
                    "; ".join(
                        "{} {}".format(c.get("id"), c.get("text"))
                        for c in criteria
                        if isinstance(c, dict)
                    )
                    or "-"
                )
            )
            deps = item.get("depends_on") or []
            if deps:
                lines.append("         after:  {}".format(", ".join(deps)))
    return "\n".join(lines)
