#!/usr/bin/env python
"""PreToolUse hook: stop a tool call that reaches outside the live run.

Reads Claude Code's hook payload on stdin. Exit 0 allows the call; exit 2
blocks it and hands stderr back to the agent as the reason. Both behaviours
are verified against a real dispatch, including from inside a subagent.

Where someone is watching, a call outside is asked about rather than
refused: exit 0 with a ``permissionDecision: ask`` on stdout. Run as
``repo_guard.py post`` from PostToolUse, it records what was approved - the
tool running at all is the only sign of the answer a hook ever gets.

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
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

ALLOW = 0
BLOCK = 2
#: Not an exit status: `main` turns it into exit 0 plus a JSON decision.
ASK = "ask"


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
        # A sandboxed run: the session stands in the sandbox and the clone
        # holding the run is one level down.
        if (candidate / "repo" / ".goatcode" / "runs").is_dir():
            return candidate / "repo"
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


def call_key(payload):
    """What ties a PostToolUse payload back to the PreToolUse one.

    ``tool_use_id`` when Claude Code sends it; the call itself otherwise.
    """
    return str(
        payload.get("tool_use_id")
        or json.dumps([payload.get("tool_name"), payload.get("tool_input")], sort_keys=True)
    )


def pending_path(repo):
    from goatcode import guard, run as runmod

    return runmod.goatcode_dir(repo) / guard.PENDING_NAME


def decide(payload):
    """``(verdict, message)`` for one PreToolUse payload.

    Someone watching the session is asked; a headless run, where nobody can
    answer, is refused as it always was. A hook cannot see the answer, so the
    request is parked for :func:`approved` to find if the tool then runs.
    """
    from goatcode import guard, osenv, run as runmod

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
    tool, tool_input = payload.get("tool_name", ""), payload.get("tool_input")
    path = guard.outside(tool, tool_input, roots, cwd)
    if path is None:
        return ALLOW, ""

    if not os.environ.get("GOATCODE_HEADLESS"):
        root = guard.grant_root(path)
        target = pending_path(repo)
        with osenv.FileLock(target):
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"key": call_key(payload), "root": root, "at": time.time()}) + "\n")
        return ASK, guard.ask_reason(path, root)

    record(repo, state, {
        "tool": tool,
        "input": {k: str(v)[:300] for k, v in (tool_input or {}).items()},
        "cwd": str(cwd),
        "session": str(payload.get("session_id", ""))[:8],
    })
    return BLOCK, guard.verdict(tool, tool_input, roots, cwd)


def approved(payload):
    """PostToolUse: a call that was asked about ran, so the user said yes.

    Writes the approved root into the config and forgets the request, along
    with any old enough that the answer must have been no.
    """
    from goatcode import guard, osenv, run as runmod

    repo = find_repo(payload.get("cwd") or os.getcwd())
    if repo is None:
        return
    target = pending_path(repo)
    if not target.exists():
        return

    key, now, granted, kept = call_key(payload), time.time(), [], []
    with osenv.FileLock(target):
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("key") == key:
                granted.append(entry["root"])
            elif now - entry.get("at", 0) < guard.PENDING_SECONDS:
                kept.append(line)
        if kept:
            target.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            target.unlink()
    for root in granted:
        runmod.add_extra_root(repo, root)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        payload = json.load(sys.stdin)
        if argv[:1] == ["post"]:
            approved(payload)
            return ALLOW
        code, message = decide(payload)
    except Exception:
        # Every failure is a pass. See the module docstring.
        return ALLOW
    if code == ASK:
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": message,
        }}, sys.stdout)
        return ALLOW
    if message:
        sys.stderr.write(message + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
