"""Keep a live run inside the repository it was started in.

A report came back of agents reading and globbing files in *other*
repositories. Nothing stopped it: the spawn only ever widens what an agent
may touch - a worktree lives outside the repo, so the repo has to be added
back - and plugin subagents inherit whatever the main thread is allowed.

This module is the decision, and only the decision. It reads no stdin, holds
no process state and writes nothing; ``hooks/repo_guard.py`` is the shell
that feeds it a tool call and acts on the answer. Split that way because a
PreToolUse hook is the one piece of goat-code that cannot be exercised by the
test suite, so as little as possible lives in it.

The boundary is not "the repository". Agents legitimately work outside it:
worktrees are in the system temp directory, ``goatcode.py`` is in the plugin
cache, and the skills the agents are told to load resolve from
``~/.claude/plugins`` and tell them to read their own reference files. So the
allowed set is enumerated rather than assumed.
"""

from __future__ import annotations

import json
import os
import pathlib
import re

#: goat-code's own installation - the CLI the agents run, the agent
#: definitions. Resolved from this file, so it is right wherever the plugin
#: was installed. Same trick as ``dispatch.CLI``.
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: Tool inputs naming a single path, by the key they use.
PATH_KEYS = ("file_path", "notebook_path", "path")

#: ``git -C <path>`` and ``cd <path>``, the two ways a shell command moves
#: somewhere else without naming an absolute path.
_BASH_ELSEWHERE = re.compile(r"(?:^|[;&|]\s*)cd\s+(\S+)|-C\s+(\S+)")

#: A token that looks like it names a place rather than a flag or a value:
#: POSIX absolute, Windows drive-absolute, or an explicit walk upward.
_PATHLIKE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\.\.[\\/])")


def real(path):
    """``realpath`` as a string, with symlinks resolved and case left alone.

    Both sides of every comparison go through this. A prefix match on the
    literal strings would be defeated by a symlink inside the repository
    pointing at another one, which is exactly the shape containment has to
    survive.
    """
    return os.path.realpath(str(path))


def allowed_roots(repo, config=None, state=None):
    """Every directory tree a live run may legitimately reach into."""
    config = config or {}
    roots = [repo, PLUGIN_ROOT, pathlib.Path.home() / ".claude" / "plugins"]

    if state:
        # One entry covers every worktree of the run, the integration one
        # included, because they are all created under it. The recorded
        # paths go in too, in case a future layout stops being true.
        if state.get("temp_root"):
            roots.append(state["temp_root"])
        roots.extend((state.get("worktrees") or {}).values())

    for extra in (config.get("guard") or {}).get("extra_roots") or []:
        # Relative to the repository, which is the only stable thing a
        # config file can be written against.
        roots.append(pathlib.Path(repo) / str(extra))

    return sorted({real(root) for root in roots if root})


def inside(path, roots):
    """True when ``path`` is one of ``roots`` or lives under one."""
    resolved = real(path)
    for root in roots:
        if resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def paths_in(tool_name, tool_input):
    """Every filesystem path a tool call is about to touch.

    Deliberately over-collects rather than under-collects: a path named here
    that turns out to be harmless costs one allowed check, while one missed
    is the hole this module exists to close.
    """
    tool_input = tool_input or {}
    found = [str(tool_input[key]) for key in PATH_KEYS if tool_input.get(key)]

    pattern = tool_input.get("pattern")
    if tool_name in ("Glob", "Grep") and pattern and _PATHLIKE.match(str(pattern)):
        # `Glob **/*.js` searches the working directory and is judged by it;
        # `Glob /other/repo/**` names its own root and is judged by that.
        found.append(_literal_prefix(str(pattern)))

    if tool_name == "Bash" and tool_input.get("command"):
        found.extend(_bash_paths(str(tool_input["command"])))

    return [path for path in found if path]


def _literal_prefix(pattern):
    """The part of a glob before the first magic character."""
    cut = len(pattern)
    for index, char in enumerate(pattern):
        if char in "*?[{":
            cut = index
            break
    head = pattern[:cut]
    return head if head.endswith(("/", "\\")) else os.path.dirname(head) or head


def _bash_paths(command):
    """Places a shell command names outright.

    ponytail: a heuristic with a known ceiling. A shell command cannot be
    parsed reliably - `$(cat f)`, aliases, a script that cd's on its own -
    and full Bash containment needs an OS sandbox rather than a hook. This
    catches the honest cases and the obvious escapes: an absolute path
    written into the command, `cd elsewhere`, and `git -C elsewhere`. If
    that ceiling starts mattering, the upgrade is a sandboxed executor, not
    a better regex.
    """
    found = []
    for match in _BASH_ELSEWHERE.finditer(command):
        found.append(match.group(1) or match.group(2))
    for token in command.replace("'", " ").replace('"', " ").split():
        if _PATHLIKE.match(token):
            found.append(token)
    return [token.rstrip(";&|") for token in found if token]


def verdict(tool_name, tool_input, roots, cwd=None):
    """The reason to refuse this tool call, or None to allow it.

    The message is written to be read by the agent that tripped it: what was
    refused, why, and what to do instead. A refusal an agent cannot act on
    just becomes a retry.
    """
    for path in paths_in(tool_name, tool_input):
        candidate = path if os.path.isabs(path) else os.path.join(str(cwd or ""), path)
        if not inside(candidate, roots):
            return (
                "goat-code: {} is outside this run's repository, so it was refused.\n"
                "A run may only read and write the repository it was started in, its "
                "worktrees, and goat-code's own files. Work from what is in the repo. "
                "If this path is genuinely needed, it belongs in guard.extra_roots in "
                ".goatcode/config.yaml - which is the user's call, not yours.".format(real(candidate))
            )
    return None


def enabled(config):
    """Whether the guard applies at all. Absent config means yes."""
    return bool(((config or {}).get("guard") or {}).get("enabled", True))


#: Where the hook records what it refused, inside the run directory.
LOG_NAME = "guard.jsonl"


def violations(run_dir):
    """Every refusal recorded for a run, oldest first.

    A line the hook wrote while a reader was mid-write is skipped rather than
    raised on: this is a report, and a torn last line must not stop the run
    it is reporting about.
    """
    path = pathlib.Path(run_dir) / LOG_NAME
    if not path.exists():
        return []

    found = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found
