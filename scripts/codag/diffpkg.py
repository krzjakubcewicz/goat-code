"""Review packages: one file a reviewer reads in a single call.

A reviewer agent that runs its own git commands burns turns and drags the
whole diff through the orchestrator's context on the way. Instead we write
the commit list, the stat summary and the full diff to one file and hand
over the path.
"""

from __future__ import annotations

import pathlib

from . import osenv

CONTEXT_LINES = 10


def build(repo, base, head, cwd=None):
    """Render the review package text for ``base..head``."""
    where = cwd or repo
    for ref in (base, head):
        if not osenv.git(["rev-parse", "--verify", "--quiet", ref], cwd=where).ok:
            raise ValueError("unknown git ref: {}".format(ref))

    span = "{}..{}".format(base, head)
    commits = osenv.git(["log", "--reverse", "--format=%h %s", span], cwd=where).stdout
    stat = osenv.git(["diff", "--stat", span], cwd=where).stdout
    diff = osenv.git(["diff", "--text", "-U{}".format(CONTEXT_LINES), span], cwd=where).stdout

    parts = [
        "# Review package",
        "",
        "Range: `{}`".format(span),
        "",
        "## Commits",
        "",
        "```",
        commits.rstrip() or "(no commits in range)",
        "```",
        "",
        "## Files changed",
        "",
        "```",
        stat.rstrip() or "(no changes)",
        "```",
        "",
        "## Diff",
        "",
        "```diff",
        diff.rstrip() or "(empty diff)",
        "```",
        "",
    ]
    return "\n".join(parts)


def write(repo, base, head, out=None, cwd=None):
    """Write the package and return its path.

    The filename carries the range, so a re-review after fixes lands in a
    distinct file instead of silently overwriting the old evidence.
    """
    where = cwd or repo
    text = build(repo, base, head, cwd=where)
    if out is None:
        short_base = osenv.git(["rev-parse", "--short", base], cwd=where).out
        short_head = osenv.git(["rev-parse", "--short", head], cwd=where).out
        out = pathlib.Path(repo) / "review-{}..{}.diff".format(short_base, short_head)
    out = pathlib.Path(out)
    osenv.write_text(out, text)
    return out


def is_empty(repo, base, head, cwd=None):
    """True when nothing changed between the two refs."""
    where = cwd or repo
    result = osenv.git(["diff", "--quiet", "{}..{}".format(base, head)], cwd=where)
    return result.returncode == 0


def changed_files(repo, base, head, cwd=None):
    where = cwd or repo
    result = osenv.git(["diff", "--name-only", "{}..{}".format(base, head)], cwd=where)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


#: What a remedial cycle adds to the verifier's package when there is a
#: previous ruling to narrow it against. Empty on cycle 1, and whenever the
#: evidence to narrow safely is not there.
NOTHING_JUDGED_YET = {
    "previous_verdict": None,
    "previous_ref": None,
    "changed_files": [],
    "unchanged_slices": [],
}


def previous_judgement(run, doc):
    """The previous cycle's verdict, and which slices it still covers.

    Each cycle's ``gates.json`` records the ref it judged, so the delta
    between two cycles is exactly ``previous ref .. this ref``. A slice
    owning none of those paths sits against byte-identical code, and the
    verifier can carry its own earlier ruling forward instead of re-deriving
    it from a diff thousands of lines long.

    Every branch falls back to judging everything: narrowing wrongly would
    hide a regression, while not narrowing only costs what the pipeline
    already pays today.
    """
    from . import tasks

    if run.cycle < 2:
        return dict(NOTHING_JUDGED_YET)

    verdict_path = run.cycle_dir(run.cycle - 1) / "verdict.md"
    if not verdict_path.exists():
        return dict(NOTHING_JUDGED_YET)

    before = _judged_ref(run, run.cycle - 1)
    now = _judged_ref(run, run.cycle)
    if not before or not now:
        return dict(NOTHING_JUDGED_YET)

    changed = changed_files(run.repo, before, now)
    return {
        "previous_verdict": str(verdict_path),
        "previous_ref": before,
        "changed_files": changed,
        "unchanged_slices": tasks.unchanged_slices(doc, changed),
    }


def _judged_ref(run, cycle):
    """The integration commit a cycle's gates ran against."""
    path = run.cycle_dir(cycle) / "gates.json"
    if not path.exists():
        return None
    return (osenv.read_json(path) or {}).get("ref")
