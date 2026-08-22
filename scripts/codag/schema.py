"""Validation gauntlet for tasks.yaml.

The planner is an LLM; this module is the deterministic gate that decides
whether its plan is safe to execute in parallel. The expensive failure mode
it exists to prevent: two executors, each in its own worktree, editing the
same file and colliding at merge time. Ownership must be provably disjoint
before a single executor is dispatched.

Errors block execution. Warnings are shown to the human but do not block.
"""

from __future__ import annotations

import re

SLICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")
ACCEPTANCE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,15}$")

STATUSES = ("pending", "claimed", "done", "blocked", "failed", "carried")
MODELS = ("opus", "sonnet", "haiku", "fable", "inherit")

REQUIRED_TOP = ("version", "run_id", "cycle", "goal", "slices")
REQUIRED_SLICE = ("id", "title", "owns", "acceptance", "tests")

MAX_ACCEPTANCE_PER_SLICE = 8
MAX_SLICES_PER_WAVE = 6


class Report:
    """Outcome of validating one plan."""

    def __init__(self):
        self.errors = []
        self.warnings = []

    @property
    def ok(self):
        return not self.errors

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    def text(self):
        lines = []
        for message in self.errors:
            lines.append("ERROR: {}".format(message))
        for message in self.warnings:
            lines.append("WARN:  {}".format(message))
        if not lines:
            lines.append("OK: plan is valid")
        return "\n".join(lines)

    def as_dict(self):
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


# --------------------------------------------------------------------------
# glob overlap
# --------------------------------------------------------------------------

_MAGIC = re.compile(r"[*?\[]")


def has_magic(pattern):
    return bool(_MAGIC.search(pattern))


def normalise(pattern):
    """Forward slashes, no leading ``./``, no trailing slash.

    A bare directory (``src/db/migrations/``) means everything under it.
    """
    text = str(pattern).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.endswith("/"):
        text += "**"
    return text


def to_regex(pattern):
    """Compile a glob to a regex where ``*`` stops at a path separator."""
    pattern = normalise(pattern)
    out = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def matches(pattern, path):
    return to_regex(pattern).match(normalise(path)) is not None


def literal_prefix(pattern):
    """The fixed leading path segments, up to the first wildcard."""
    pattern = normalise(pattern)
    match = _MAGIC.search(pattern)
    head = pattern if not match else pattern[: match.start()]
    if "/" in head:
        return head[: head.rindex("/") + 1]
    return ""


def literal_suffix(pattern):
    """The fixed trailing text, after the last wildcard."""
    pattern = normalise(pattern)
    last = -1
    for index, ch in enumerate(pattern):
        if ch in "*?[":
            last = index
    return pattern[last + 1 :] if last >= 0 else pattern


def overlaps(left, right):
    """Could these two patterns ever match the same path?

    Deliberately conservative: a false "yes" costs the planner one more
    revision, while a false "no" costs a corrupted parallel merge.
    """
    left = normalise(left)
    right = normalise(right)
    if left == right:
        return True

    left_magic = has_magic(left)
    right_magic = has_magic(right)
    if not left_magic and not right_magic:
        return False
    if not left_magic:
        return matches(right, left)
    if not right_magic:
        return matches(left, right)

    prefix_left = literal_prefix(left)
    prefix_right = literal_prefix(right)
    if not (prefix_left.startswith(prefix_right) or prefix_right.startswith(prefix_left)):
        return False

    # Same directory tree. Distinct fixed endings (".test.ts" vs ".spec.ts")
    # keep them apart; anything else is treated as a collision.
    suffix_left = literal_suffix(left)
    suffix_right = literal_suffix(right)
    if suffix_left and suffix_right:
        if not (suffix_left.endswith(suffix_right) or suffix_right.endswith(suffix_left)):
            return False
    return True


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------


def dependency_cycles(slices):
    """Every dependency cycle, each as an ordered list of slice ids."""
    graph = {s.get("id"): list(s.get("depends_on") or []) for s in slices if s.get("id")}
    cycles = []
    seen_paths = set()
    state = {}

    def visit(node, path):
        state[node] = "open"
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if state.get(dep) == "open":
                cycle = path[path.index(dep) :] + [dep]
                key = tuple(sorted(set(cycle)))
                if key not in seen_paths:
                    seen_paths.add(key)
                    cycles.append(cycle)
            elif state.get(dep) is None:
                visit(dep, path + [dep])
        state[node] = "closed"

    for node in graph:
        if state.get(node) is None:
            visit(node, [node])
    return cycles


def waves(slices):
    """Group slice ids into dependency waves. Wave 0 has no dependencies.

    Returns ``[]`` when the graph has a cycle - validate first.
    """
    graph = {s.get("id"): [d for d in (s.get("depends_on") or []) if d] for s in slices if s.get("id")}
    if dependency_cycles(slices):
        return []
    depth = {}

    def resolve(node, guard):
        if node in depth:
            return depth[node]
        if node in guard:
            return 0
        deps = [d for d in graph.get(node, []) if d in graph]
        depth[node] = 0 if not deps else 1 + max(resolve(d, guard | {node}) for d in deps)
        return depth[node]

    for node in graph:
        resolve(node, frozenset())

    grouped = {}
    for node, level in depth.items():
        grouped.setdefault(level, []).append(node)
    return [sorted(grouped[level]) for level in sorted(grouped)]


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def validate(doc):
    """Full gauntlet. Returns a :class:`Report`."""
    report = Report()
    if not isinstance(doc, dict):
        report.error("tasks.yaml must contain a mapping at the top level")
        return report

    for key in REQUIRED_TOP:
        if key not in doc:
            report.error("missing top-level key: {}".format(key))

    if "version" in doc and doc["version"] != 1:
        report.error("unsupported version {!r}; this build understands version 1".format(doc["version"]))
    if "goal" in doc and not _nonempty_str(doc.get("goal")):
        report.error("'goal' must be a non-empty sentence")
    for key in ("global_constraints", "assumptions"):
        if key in doc and doc[key] is not None and not isinstance(doc[key], list):
            report.error("'{}' must be a list".format(key))

    slices = doc.get("slices")
    if not isinstance(slices, list) or not slices:
        report.error("'slices' must be a non-empty list")
        return report

    valid = [s for s in slices if isinstance(s, dict)]
    for index, item in enumerate(slices):
        if not isinstance(item, dict):
            report.error("slice #{} is not a mapping".format(index + 1))

    _validate_ids(valid, report)
    for item in valid:
        _validate_slice(item, report)
    _validate_dependencies(valid, report)
    _validate_interfaces(valid, report)
    _validate_ownership(valid, report)
    return report


def _validate_ids(slices, report):
    seen = {}
    for item in slices:
        slice_id = item.get("id")
        if not _nonempty_str(slice_id):
            report.error("every slice needs a non-empty 'id'")
            continue
        if not SLICE_ID_RE.match(slice_id):
            report.error("slice id {!r} must start with a letter and use only letters, digits, . _ -".format(slice_id))
        seen[slice_id] = seen.get(slice_id, 0) + 1
    for slice_id, count in seen.items():
        if count > 1:
            report.error("duplicate slice id {!r} appears {} times".format(slice_id, count))


def _validate_slice(item, report):
    slice_id = item.get("id") or "<unnamed>"
    for key in REQUIRED_SLICE:
        if key not in item:
            report.error("{}: missing required key '{}'".format(slice_id, key))

    if "title" in item and not _nonempty_str(item.get("title")):
        report.error("{}: 'title' must be a non-empty string".format(slice_id))

    status = item.get("status", "pending")
    if status not in STATUSES:
        report.error("{}: status {!r} is not one of {}".format(slice_id, status, ", ".join(STATUSES)))

    model = item.get("model")
    if model is not None and model not in MODELS:
        report.error("{}: model {!r} is not one of {}".format(slice_id, model, ", ".join(MODELS)))

    for key in ("owns", "touches_shared", "interfaces", "uses_interfaces", "out_of_scope", "depends_on"):
        value = item.get(key)
        if value is not None and not isinstance(value, list):
            report.error("{}: '{}' must be a list".format(slice_id, key))

    owns = item.get("owns")
    if isinstance(owns, list):
        if not owns:
            report.error("{}: 'owns' must claim at least one path or glob".format(slice_id))
        for pattern in owns:
            if not _nonempty_str(pattern):
                report.error("{}: 'owns' entries must be non-empty strings".format(slice_id))

    _validate_acceptance(item, slice_id, report)
    _validate_tests(item, slice_id, report)

    if _nonempty_str(item.get("intent")) is False and "intent" in item:
        report.error("{}: 'intent' must be a non-empty string when present".format(slice_id))
    if "intent" not in item:
        report.warn("{}: no 'intent' - the executor loses the vertical-slice framing".format(slice_id))


def _validate_acceptance(item, slice_id, report):
    acceptance = item.get("acceptance")
    if not isinstance(acceptance, list):
        if "acceptance" in item:
            report.error("{}: 'acceptance' must be a list".format(slice_id))
        return
    if not acceptance:
        report.error("{}: needs at least one acceptance criterion; nothing can verify it otherwise".format(slice_id))
        return
    if len(acceptance) > MAX_ACCEPTANCE_PER_SLICE:
        report.warn(
            "{}: {} acceptance criteria (>{}) - the slice is probably too fat to ship on its own".format(
                slice_id, len(acceptance), MAX_ACCEPTANCE_PER_SLICE
            )
        )
    seen = set()
    for index, criterion in enumerate(acceptance):
        label = "{}.acceptance[{}]".format(slice_id, index)
        if not isinstance(criterion, dict):
            report.error("{}: each criterion must be a mapping with 'id' and 'text'".format(label))
            continue
        criterion_id = criterion.get("id")
        if not _nonempty_str(criterion_id) or not ACCEPTANCE_ID_RE.match(str(criterion_id)):
            report.error("{}: needs a short id like 'A1'".format(label))
        elif criterion_id in seen:
            report.error("{}: duplicate acceptance id {!r} within the slice".format(label, criterion_id))
        else:
            seen.add(criterion_id)
        if not _nonempty_str(criterion.get("text")):
            report.error("{}: needs 'text' stating a checkable assertion".format(label))


def _validate_tests(item, slice_id, report):
    tests = item.get("tests")
    if not isinstance(tests, list):
        if "tests" in item:
            report.error("{}: 'tests' must be a list".format(slice_id))
        return
    if not tests:
        report.error("{}: needs at least one test file path; executors work test-first".format(slice_id))
        return
    for index, entry in enumerate(tests):
        label = "{}.tests[{}]".format(slice_id, index)
        if isinstance(entry, str):
            if not entry.strip():
                report.error("{}: empty test path".format(label))
            continue
        if not isinstance(entry, dict):
            report.error("{}: must be a path string or a mapping with 'path'".format(label))
            continue
        if not _nonempty_str(entry.get("path")):
            report.error("{}: needs a non-empty 'path'".format(label))
        if "must_cover" in entry and not isinstance(entry["must_cover"], list):
            report.error("{}: 'must_cover' must be a list".format(label))


def _validate_dependencies(slices, report):
    known = {s.get("id") for s in slices if _nonempty_str(s.get("id"))}
    for item in slices:
        slice_id = item.get("id") or "<unnamed>"
        deps = item.get("depends_on")
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if not _nonempty_str(dep):
                report.error("{}: 'depends_on' entries must be slice ids".format(slice_id))
            elif dep == slice_id:
                report.error("{}: depends on itself".format(slice_id))
            elif dep not in known:
                report.error("{}: depends on unknown slice {!r}".format(slice_id, dep))

    for cycle in dependency_cycles(slices):
        report.error("dependency cycle: {}".format(" -> ".join(cycle)))


def _validate_interfaces(slices, report):
    """A slice may only consume interfaces its dependencies actually publish."""
    published = {}
    for item in slices:
        for interface in item.get("interfaces") or []:
            if _nonempty_str(interface):
                published.setdefault(str(interface).strip(), set()).add(item.get("id"))

    for item in slices:
        slice_id = item.get("id") or "<unnamed>"
        deps = set(item.get("depends_on") or [])
        for wanted in item.get("uses_interfaces") or []:
            if not _nonempty_str(wanted):
                report.error("{}: 'uses_interfaces' entries must be strings".format(slice_id))
                continue
            providers = published.get(str(wanted).strip())
            if not providers:
                report.error("{}: uses interface {!r} that no slice provides".format(slice_id, wanted))
            elif not providers & deps:
                report.error(
                    "{}: uses interface {!r} from {} but does not depend on it".format(
                        slice_id, wanted, "/".join(sorted(p for p in providers if p))
                    )
                )


def _validate_ownership(slices, report):
    """The rule that makes parallel execution safe."""
    layout = waves(slices)
    wave_of = {}
    for index, group in enumerate(layout):
        for slice_id in group:
            wave_of[slice_id] = index

    for index, group in enumerate(layout):
        if len(group) > MAX_SLICES_PER_WAVE:
            report.warn(
                "wave {} has {} slices (>{}) - consider splitting the wave".format(
                    index + 1, len(group), MAX_SLICES_PER_WAVE
                )
            )

    by_id = {s.get("id"): s for s in slices if _nonempty_str(s.get("id"))}
    ids = sorted(by_id)
    for i, left_id in enumerate(ids):
        for right_id in ids[i + 1 :]:
            left = by_id[left_id]
            right = by_id[right_id]
            clashes = _clashing(left.get("owns"), right.get("owns"))
            if not clashes:
                continue
            same_wave = wave_of.get(left_id) == wave_of.get(right_id)
            detail = "; ".join("{!r} vs {!r}".format(a, b) for a, b in clashes[:3])
            if same_wave:
                report.error(
                    "{} and {} run in the same wave and both own {}".format(left_id, right_id, detail)
                )
            else:
                report.warn(
                    "{} and {} own overlapping paths ({}); they are in different waves, "
                    "so the later one must expect the earlier one's changes".format(left_id, right_id, detail)
                )

    for slice_id, item in by_id.items():
        for shared in item.get("touches_shared") or []:
            for other_id, other in by_id.items():
                if other_id == slice_id:
                    continue
                if _clashing([shared], other.get("owns")):
                    report.error(
                        "{} lists {!r} as shared but {} owns it exclusively; "
                        "shared paths must not be owned".format(slice_id, shared, other_id)
                    )


def _clashing(left, right):
    if not isinstance(left, list) or not isinstance(right, list):
        return []
    out = []
    for a in left:
        for b in right:
            if _nonempty_str(a) and _nonempty_str(b) and overlaps(a, b):
                out.append((a, b))
    return out


def _nonempty_str(value):
    return isinstance(value, str) and bool(value.strip())
