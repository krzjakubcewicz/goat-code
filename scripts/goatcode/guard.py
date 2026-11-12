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
import sys

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

#: Tools whose input is a shell command. PowerShell was missing from the hook
#: matcher and from here while executors, which may use any tool, ran it
#: dozens of times a run with nothing looking.
SHELL_TOOLS = ("Bash", "PowerShell")

#: Dependency caches, relative to the home directory. A build reads these
#: file by file, so one approval admits the cache rather than one artifact.
CACHE_ROOTS = (".m2", ".gradle", ".npm", ".cargo", "go/pkg/mod")


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

    # The interpreter running the guard is already trusted, and dispatch
    # prompts and `next` commands name it by absolute path - refusing it
    # refused goat-code's own commands, which taught agents to route around
    # refusals. Empty under an embedded interpreter, so skip rather than
    # admit the working directory.
    # The configured python is the one those commands name instead. A bare
    # name puts no path in the command, and its dirname would be the working
    # directory, so only an absolute path is admitted.
    for exe in (sys.executable, config.get("python")):
        if exe and os.path.isabs(str(exe)):
            roots.append(os.path.dirname(str(exe)))

    if config.get("sandbox"):
        # The clone's parent is the sandbox: the redirected temp dir and the
        # spec the run was started from both live there.
        roots.append(pathlib.Path(repo).parent)

    if state:
        # One entry covers every worktree of the run, the integration one
        # included, because they are all created under it. The recorded
        # paths go in too, in case a future layout stops being true.
        if state.get("temp_root"):
            roots.append(state["temp_root"])
        roots.extend((state.get("worktrees") or {}).values())

    for extra in (config.get("guard") or {}).get("extra_roots") or []:
        # Relative to the repository, which is the only stable thing a
        # config file can be written against. An absolute entry - what an
        # approved request records - survives the join unchanged.
        roots.append(pathlib.Path(repo) / os.path.expanduser(str(extra)))

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

    if tool_name in SHELL_TOOLS and tool_input.get("command"):
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


def outside(tool_name, tool_input, roots, cwd=None):
    """The first path this tool call reaches outside ``roots``, or None."""
    for path in paths_in(tool_name, tool_input):
        candidate = path if os.path.isabs(path) else os.path.join(str(cwd or ""), path)
        if not inside(candidate, roots):
            return real(candidate)
    return None


def verdict(tool_name, tool_input, roots, cwd=None):
    """The reason to refuse this tool call, or None to allow it.

    The message is written to be read by the agent that tripped it: what was
    refused, why, and what to do instead. A refusal an agent cannot act on
    just becomes a retry.
    """
    path = outside(tool_name, tool_input, roots, cwd)
    if path is None:
        return None
    return (
        "goat-code: {} is outside this run's repository, so it was refused.\n"
        "A run may only read and write the repository it was started in, its "
        "worktrees, and goat-code's own files. Work from what is in the repo. "
        "If this path is genuinely needed, it belongs in guard.extra_roots in "
        ".goatcode/config.yaml - which is the user's call, not yours.".format(path)
    )


def grant_root(path, home=None):
    """What approving ``path`` should admit from then on.

    A recognised dependency cache admits the whole cache. Anything else
    admits the directory holding the file - unless that is the home
    directory or a drive root, where one approved file would quietly open
    everything beside it; then only the path itself.
    """
    resolved = real(path)
    home = real(home or pathlib.Path.home())
    for cache in CACHE_ROOTS:
        root = real(os.path.join(home, cache))
        if inside(resolved, [root]):
            return root
    if os.path.isdir(resolved):
        return resolved
    parent = os.path.dirname(resolved)
    if parent == home or parent == os.path.dirname(parent):
        return resolved
    return parent


def ask_reason(path, root):
    """What the user is shown when a call outside the run needs their say."""
    return (
        "goat-code: {} is outside this run's repository. Allowing it adds {} to "
        "guard.extra_roots in .goatcode/config.yaml, so it is not asked again.".format(path, root)
    )


def enabled(config):
    """Whether the guard applies at all. Absent config means yes."""
    return bool(((config or {}).get("guard") or {}).get("enabled", True))


#: Where the hook records what it refused, inside the run directory.
LOG_NAME = "guard.jsonl"

#: Requests waiting on the user's answer, in ``.goatcode/``. Its existence is
#: what the PostToolUse shim tests before starting an interpreter at all.
PENDING_NAME = "guard.pending"

#: A request nobody approved in this long was denied; forget it.
PENDING_SECONDS = 600


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
