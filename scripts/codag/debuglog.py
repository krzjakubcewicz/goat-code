"""Debug trace: everything cod-ag does, one line at a time.

Distinct from the two logs that are always on. ``ledger.md`` records
pipeline steps for crash recovery; ``progress.txt`` records what a run
achieved and learnt. This one records *what the tool did* - every command,
every subprocess, every phase change, every file written - and exists for
the moment a run behaves oddly and the ledger only says "slice S1 done".

Off unless asked for. ``CODAG_DEBUG=1`` for a one-off, ``debug: true`` in
config when you want it on for a while.

Deliberately imports nothing from the rest of ``codag``: ``osenv`` calls
into it, so a dependency back would be circular. Stdlib only.
"""

from __future__ import annotations

import collections
import datetime
import os
import pathlib

FILENAME = "log.txt"

#: Longest a single line may get. A rendered argv can carry a whole diff
#: path list; the trace is worth more when it stays scannable.
MAX_LINE = 2000

_TARGET = None
_ENABLED = None

#: Events logged before there is a run directory to write them to. Bounded,
#: because a process that never attaches must not grow forever.
_PENDING = collections.deque(maxlen=500)

#: Values of CODAG_DEBUG that mean "on". Anything else, including the empty
#: string, means off - so `CODAG_DEBUG=` in a shell profile does not
#: silently enable it.
_TRUTHY = {"1", "true", "yes", "on"}


def enabled():
    """Whether tracing is on. The environment wins over config."""
    from_env = os.environ.get("CODAG_DEBUG")
    if from_env is not None:
        return from_env.strip().lower() in _TRUTHY
    return bool(_ENABLED)


def configure(from_config=None):
    """Record the config setting. ``CODAG_DEBUG`` still overrides it."""
    global _ENABLED
    _ENABLED = bool(from_config)


def attach(run_dir):
    """Start writing into ``<run_dir>/log.txt``, flushing anything buffered.

    Events logged before the run directory existed - preflight, argument
    parsing - are held in memory and written here. That is exactly the part
    of a failing ``init`` you want to see.
    """
    global _TARGET
    if not enabled():
        _PENDING.clear()
        return
    _TARGET = pathlib.Path(run_dir) / FILENAME
    _TARGET.parent.mkdir(parents=True, exist_ok=True)
    while _PENDING:
        _append(_PENDING.popleft())


def detach():
    """Stop writing. For tests, and for a process that changes runs."""
    global _TARGET, _ENABLED
    _TARGET = None
    _ENABLED = None
    _PENDING.clear()


def target():
    return _TARGET


def log(event, **fields):
    """Record one action. Never raises - a broken log must not stop a run."""
    if not enabled():
        return
    try:
        line = _render(event, fields)
    except Exception:  # noqa: BLE001 - formatting must never break the tool
        return
    if _TARGET is None:
        _PENDING.append(line)
    else:
        _append(line)


def _render(event, fields):
    stamp = datetime.datetime.now().isoformat(timespec="milliseconds")
    parts = ["{} pid={} {}".format(stamp, os.getpid(), event)]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append("{}={}".format(key, _value(value)))
    line = " ".join(parts)
    if len(line) > MAX_LINE:
        line = line[: MAX_LINE - 3] + "..."
    return line


def _value(value):
    if isinstance(value, (list, tuple)):
        text = " ".join(str(v) for v in value)
    else:
        text = str(value)
    text = text.replace("\n", "\\n").replace("\r", "")
    return '"{}"'.format(text) if " " in text else text


def _append(line):
    """One line, one append.

    Opened in append mode so the OS keeps a small write atomic: executors
    run in parallel and each shells out to the CLI, so lines interleave -
    but they never tear. Each line carries its pid to be sorted back out.
    No lock: logging must not be able to deadlock or slow the pipeline.
    """
    try:
        with open(_TARGET, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read(run_dir):
    """The trace for a run, as a list of lines. Empty when there is none."""
    path = pathlib.Path(run_dir) / FILENAME
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
