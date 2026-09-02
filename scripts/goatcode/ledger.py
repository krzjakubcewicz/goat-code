"""Append-only progress ledger.

Conversation memory does not survive compaction; this file does. The
orchestrator writes one line per completed step, and on resume it trusts
the ledger and ``git log`` over its own recollection. Never re-dispatch a
slice the ledger already marks complete.
"""

from __future__ import annotations

import datetime
import pathlib

from . import osenv


def append(run, line, now=None):
    """Add one timestamped entry. Returns the formatted line."""
    stamp = (now or datetime.datetime.now()).replace(microsecond=0).isoformat()
    entry = "- [{}] cycle {}: {}".format(stamp, run.cycle, line.strip())
    path = pathlib.Path(run.ledger_path)
    existing = osenv.read_text(path) if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    # A retried command must not double-log. One recorded run has "scribe
    # written" twice, seven seconds apart, which makes the recovery map claim
    # a step happened twice when it happened once.
    if _same_as_last(existing, entry):
        return entry
    osenv.write_text(path, existing + entry + "\n")
    return entry


def _same_as_last(existing, entry):
    """True when the last entry records the same thing, timestamp aside."""
    lines = [line for line in existing.splitlines() if line.startswith("- [")]
    if not lines:
        return False
    return _without_stamp(lines[-1]) == _without_stamp(entry)


def _without_stamp(line):
    return line.split("]", 1)[1].strip() if "]" in line else line.strip()


def entries(run):
    """Every ledger line, in order, without the bullet prefix."""
    path = pathlib.Path(run.ledger_path)
    if not path.exists():
        return []
    out = []
    for line in osenv.read_text(path).splitlines():
        line = line.strip()
        if line.startswith("- ["):
            out.append(line[2:])
    return out


def completed_slices(run):
    """Slice ids the ledger records as complete.

    The orchestrator's recovery map: anything named here is done, whatever
    the conversation appears to say.
    """
    done = set()
    for entry in entries(run):
        marker = "slice "
        if marker not in entry or "complete" not in entry:
            continue
        tail = entry.split(marker, 1)[1]
        slice_id = tail.split()[0].strip(":,")
        if slice_id:
            done.add(slice_id)
    return done
