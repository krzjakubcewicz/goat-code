"""The project's running log of what cod-ag has done, and what it learnt.

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
# cod-ag progress log

Appended to at the end of every run. Read the Learnings sections before
planning new work: they are what earlier runs found out the hard way.
"""

_ENTRY_RE = re.compile(r"^## ", re.MULTILINE)



def path_for(repo):
    return runmod.codag_dir(repo) / FILENAME


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
