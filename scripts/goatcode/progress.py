"""The project's running log of what goat-code has done, and what it learnt.

Distinct from ``ledger.md``, which is per-run machine bookkeeping used for
crash recovery. This file is cross-run and written for a reader: one entry
per completed run, carrying the learnings a later run would otherwise have
to rediscover.

Appending is done here rather than by the agent, so "append, never replace"
is a property of the code instead of an instruction someone might not
follow.
"""

from __future__ import annotations

import datetime
import pathlib
import re

from . import osenv, run as runmod

FILENAME = "progress.txt"
SEPARATOR = "---"

HEADER = """\
# goat-code progress log

Appended to at the end of every run. Read the Learnings sections before
planning new work: they are what earlier runs found out the hard way.
"""

_ENTRY_RE = re.compile(r"^## ", re.MULTILINE)



def path_for(repo):
    return runmod.goatcode_dir(repo) / FILENAME


def read(repo):
    target = path_for(repo)
    return osenv.read_text(target) if target.exists() else ""


def entries(repo):
    """Existing entries, newest last, without the file header.

    Split on the ``## `` headings rather than on the separator: the first
    entry shares a chunk with the file header otherwise, and would be lost.
    """
    text = read(repo)
    start = text.find("\n## ")
    if start == -1:
        return [] if not text.startswith("## ") else [text.strip()]

    out = []
    for chunk in _ENTRY_RE.split(text[start + 1 :]):
        cleaned = chunk.strip()
        if cleaned.endswith(SEPARATOR):
            cleaned = cleaned[: -len(SEPARATOR)].strip()
        if cleaned:
            out.append("## " + cleaned)
    return out


def recent(repo, limit=5):
    found = entries(repo)
    return found[-limit:] if limit else found


#: Rules promoted out of the narrative, shown to every planner regardless of
#: age. A learning written into a run's entry is read once and then scrolls
#: out of reach; the log reached 43 KB over eight runs and the same
#: assertion-gap lesson was written in every one of them while the failure
#: recurred every time. Prose does not change behaviour - a standing rule the
#: planner must fold into `global_constraints` does.
CONSTRAINTS_FILE = "constraints.md"

CONSTRAINTS_HEADER = """\
# Standing constraints

Promoted out of the run log because they kept coming back. The planner folds
these into every plan's `global_constraints`. Add one when a learning has now
appeared twice - the third time should be prevented, not recorded again.
"""


def constraints_path(repo):
    return runmod.goatcode_dir(repo) / CONSTRAINTS_FILE


def constraints(repo):
    """The standing rules, in the order they were promoted."""
    target = constraints_path(repo)
    if not target.exists():
        return []
    out = []
    for line in osenv.read_text(target).splitlines():
        line = line.strip()
        if line.startswith("- "):
            out.append(line[2:].strip())
    return out


def add_constraint(repo, text):
    """Promote one rule. Appends; never rewrites, and never duplicates."""
    text = (text or "").strip()
    if not text:
        raise ValueError("refusing to add an empty constraint")
    existing = constraints(repo)
    if text in existing:
        return existing

    target = constraints_path(repo)
    body = osenv.read_text(target) if target.exists() else CONSTRAINTS_HEADER
    if not body.endswith("\n"):
        body += "\n"
    osenv.write_text(target, body + "- " + text + "\n")
    return existing + [text]


def planner_view(repo, limit=5):
    """What the planner is shown: every standing rule, the last few entries.

    Not the whole log. It grows without bound, it is read at the start of
    every run, and an old entry's narrative is worth far less than the rule
    that entry should have become.
    """
    parts = []
    rules = constraints(repo)
    if rules:
        parts.append(
            "## Standing constraints\n\nFold every one of these into the plan's "
            "`global_constraints`:\n\n" + "\n".join("- " + rule for rule in rules)
        )
    parts.extend(recent(repo, limit))
    return "\n\n".join(parts).strip()


def render_header(run, now=None):
    stamp = (now or datetime.datetime.now()).replace(microsecond=0)
    return "\n".join(
        [
            "## {} - {}".format(stamp.strftime("%Y-%m-%d %H:%M"), run.run_id),
            "Run: {}".format(run.root),
        ]
    )


def append(repo, run, body, now=None):
    """Add one entry. Never rewrites what is already there.

    Returns the entry text that was appended.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("refusing to append an empty progress entry")

    target = path_for(repo)
    existing = read(repo)
    if not existing.strip():
        existing = HEADER

    entry = "{}\n{}\n".format(render_header(run, now), body)
    if not existing.endswith("\n"):
        existing += "\n"
    osenv.write_text(target, "{}\n{}\n{}\n".format(existing, entry, SEPARATOR))
    return entry


def summary(repo):
    """One line for a status display."""
    found = entries(repo)
    if not found:
        return "no entries yet"
    return "{} entr{}, last: {}".format(
        len(found), "y" if len(found) == 1 else "ies", found[-1].splitlines()[0][3:]
    )


def template():
    """The shape an entry body should take, for the agent's prompt."""
    return "\n".join(
        [
            "- What was implemented",
            "  - one or two lines, in terms of what the project can now do",
            "- Files changed",
            "  - the paths that matter, grouped if there are many",
            "- **Learnings for future iterations:**",
            "  - patterns discovered (e.g. \"this codebase uses X for Y\")",
            "  - gotchas encountered (e.g. \"changing W means also updating Z\")",
            "  - useful context (e.g. \"the evaluation panel lives in component X\")",
        ]
    )
