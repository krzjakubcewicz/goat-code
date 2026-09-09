#!/usr/bin/env python
"""PreToolUse hook: refuse a tool call that reaches outside the live run.

Reads Claude Code's hook payload on stdin. Exit 0 allows the call; exit 2
blocks it and hands stderr back to the agent as the reason. Both behaviours
are verified against a real dispatch, including from inside a subagent.

Two properties matter more than the check:

**It does nothing unless a run is live.** No ``.goatcode/``, no runs, or a
latest run that has finished - allow, immediately. The hook is loaded for a
whole session, so without this it would police the user's own work in any
repository goat-code has ever touched.

**It fails open.** Any exception, a state file it cannot parse, a package it
cannot import - allow. A guard that can take down every run it is guarding is
worse than no guard, and this is the one file in goat-code the test suite
cannot exercise end to end.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

ALLOW = 0
BLOCK = 2


def find_repo(cwd):
    """The repository holding the run, without spending a subprocess.

    ``GOATCODE_REPO`` is set by the standalone driver, which knows exactly.
    Otherwise walk up from the working directory looking for ``.goatcode/``,
    which is what a plugin subagent sees: it inherits the main thread's
    directory, and that is the repository.
    """
    named = os.environ.get("GOATCODE_REPO")
    if named and (pathlib.Path(named) / ".goatcode").is_dir():
        return pathlib.Path(named)

    here = pathlib.Path(cwd or os.getcwd()).resolve()
    for candidate in [here] + list(here.parents):
        if (candidate / ".goatcode" / "runs").is_dir():
            return candidate
    return None


def live_run(repo):
    """The state of the run in progress, or None when nothing is running."""
    from goatcode import run as runmod

    run_id = runmod.latest_run_id(repo)
    if not run_id:
        return None
    state = json.loads(
        (runmod.runs_dir(repo) / run_id / "state.json").read_text(encoding="utf-8")
    )
    if state.get("phase") in runmod.TERMINAL_PHASES:
        return None
    return state


def record(repo, state, entry):
    """Append one line to the run's guard log.

    Append-only and one line at a time, deliberately not `ledger.append`:
    that is read-modify-write with no lock, and a wave of executors tripping
    the guard would corrupt it.
    """
    from goatcode import guard, run as runmod

    path = runmod.runs_dir(repo) / state["run_id"] / guard.LOG_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def decide(payload):
    """``(exit code, message)`` for one hook payload."""
    from goatcode import guard, run as runmod

    cwd = payload.get("cwd") or os.getcwd()
    repo = find_repo(cwd)
    if repo is None:
        return ALLOW, ""

    config = runmod.load_config(repo)
    if not guard.enabled(config):
        return ALLOW, ""

    state = live_run(repo)
    if state is None:
        return ALLOW, ""

    roots = guard.allowed_roots(repo, config, state)
    reason = guard.verdict(payload.get("tool_name", ""), payload.get("tool_input"), roots, cwd)
    if reason is None:
        return ALLOW, ""

    record(repo, state, {
        "tool": payload.get("tool_name"),
        "input": {k: str(v)[:300] for k, v in (payload.get("tool_input") or {}).items()},
        "cwd": str(cwd),
        "session": str(payload.get("session_id", ""))[:8],
    })
    return BLOCK, reason


def main():
    try:
        payload = json.load(sys.stdin)
        code, message = decide(payload)
    except Exception:
        # Every failure is a pass. See the module docstring.
        return ALLOW
    if message:
        sys.stderr.write(message + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
