"""Append-only progress ledger.

Conversation memory does not survive compaction; this file does. The
orchestrator writes one line per completed step, and on resume it trusts
the ledger and ``git log`` over its own recollection. Which slices are
finished is not read from here: that is a status in tasks.yaml, which the
checked ``report`` command writes. Parsing it back out of these lines went
wrong once already - the parser looked for "complete" and the entries said
"done", so the recovery list was always empty.
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
    # Appended under a lock, not read-modify-written. Executors report
    # concurrently: rewriting the whole file drops whichever entry lands
    # second, and on Windows even append mode interleaves without the lock.
    with osenv.FileLock(path):
        existing = osenv.read_text(path) if path.exists() else ""
        # A retried command must not double-log. One recorded run has "scribe
        # written" twice, seven seconds apart, which makes the recovery map
        # claim a step happened twice when it happened once.
        if _same_as_last(existing, entry):
            return entry
        healed = "" if not existing or existing.endswith("\n") else "\n"
        osenv.append_text(path, healed + entry + "\n")
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
