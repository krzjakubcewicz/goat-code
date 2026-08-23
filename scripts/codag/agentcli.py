"""Run one dispatch as a headless ``claude -p`` process.

This is what makes a run possible without Claude Code. The plugin performs a
dispatch by spawning a subagent; standalone performs the same dispatch by
spawning the ``claude`` CLI with the agent's own definition file as its
system prompt. Same agents, same prompts, same models - only the thing
executing them differs.

Nothing here decides anything. The dispatch entry already carries the agent,
the model, the prompt path and the working directory; this module turns that
into an argv, runs it, and reports what happened.

Output is read as it arrives (``--output-format stream-json``) so a wave of
executors is visible while it works rather than silent for ten minutes.
"""

from __future__ import annotations

import json
import pathlib

from . import debuglog, miniyaml, osenv

#: Where the agent definitions live, resolved from this file so it is right
#: regardless of the working directory - the same trick as ``dispatch.CLI``.
AGENTS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "agents"

#: What we tell the agent. Everything it needs is in the prompt file; keeping
#: this to one line is what stops briefs and diffs entering anyone's context.
INSTRUCTION = "Read {} and follow it."


class AgentError(RuntimeError):
    """The ``claude`` CLI could not be started at all."""


class Receipt:
    """What came back from one dispatch."""

    __slots__ = ("entry", "ok", "text", "cost_usd", "duration_ms", "denials", "returncode")

    def __init__(self, entry, ok, text="", cost_usd=None, duration_ms=None, denials=(), returncode=0):
        self.entry = entry
        self.ok = ok
        self.text = text
        self.cost_usd = cost_usd
        self.duration_ms = duration_ms
        self.denials = list(denials)
        self.returncode = returncode

    @property
    def label(self):
        return label(self.entry)


def label(entry):
    """How a dispatch is named in output: its slice, or its role."""
    return entry.get("slice") or str(entry.get("agent", "agent")).replace("codag-", "")


def definition(agent):
    """The frontmatter and body of ``agents/<agent>.md``."""
    path = AGENTS_DIR / "{}.md".format(agent)
    if not path.exists():
        raise AgentError("no definition for agent {!r} at {}".format(agent, path))
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AgentError("{} has no frontmatter".format(path.name))
    _, header, body = text.split("---\n", 2)
    return miniyaml.loads(header), body.strip()


def build_argv(entry, repo, config=None):
    """The full ``claude`` invocation for one dispatch."""
    config = config or {}
    fields, body = definition(entry["agent"])

    instruction = INSTRUCTION.format(entry["prompt"])
    if entry.get("context"):
        # A re-dispatch after NEEDS_CONTEXT: same prompt file, plus the
        # answer to whatever the agent said it was missing.
        instruction += "\n\nYou asked for context. Here it is:\n{}".format(entry["context"])

    argv = [
        config.get("claude_bin") or "claude",
        "--print",
        instruction,
        "--model",
        entry["model"],
        "--system-prompt",
        body,
        "--permission-mode",
        config.get("permission_mode") or "acceptEdits",
        "--output-format",
        "stream-json",
        "--verbose",
        # Worktrees live outside the repository and the run state lives
        # inside it, so whichever of the two is not the working directory
        # still has to be reachable.
        "--add-dir",
        str(repo),
    ]

    tools = fields.get("tools")
    if isinstance(tools, list) and "*" not in tools:
        argv += ["--allowedTools", ",".join(str(t) for t in tools)]

    budget = config.get("max_cost_usd")
    if budget:
        argv += ["--max-budget-usd", str(budget)]

    argv += [str(a) for a in config.get("extra_args") or []]
    return argv


def dispatch(entry, repo, config=None, on_event=None):
    """Run one dispatch to completion. Returns a :class:`Receipt`.

    ``on_event(kind, receipt_label, detail)`` is called as the stream
    arrives, for a caller that wants to show progress. It is never given
    anything the orchestrator would have to hold: a tool name and its target,
    not the agent's prose.
    """
    argv = build_argv(entry, repo, config)
    name = label(entry)
    final = {}

    def consume(line):
        message = _parse(line)
        if message is None:
            return
        if message.get("type") == "result":
            final["result"] = message
        elif on_event:
            for kind, detail in _events(message):
                on_event(kind, name, detail)

    try:
        outcome = osenv.stream(argv, cwd=entry.get("cwd") or str(repo), on_line=consume)
    except osenv.CommandError:
        raise AgentError(
            "cannot run {!r}. Install the Claude Code CLI, or set claude_bin in "
            ".codag/config.yaml".format(argv[0])
        )

    receipt = _receipt(
        entry, final.get("result"), outcome.returncode, outcome.stderr, outcome.duration
    )
    debuglog.log(
        "agent",
        agent=entry.get("agent"),
        model=entry.get("model"),
        slice=entry.get("slice"),
        rc=receipt.returncode,
        ms=receipt.duration_ms,
        cost=receipt.cost_usd,
        denials=len(receipt.denials) or None,
    )
    return receipt


def _parse(line):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except ValueError:
        # A partial or non-JSON line must never take the run down. The
        # result message is what matters and it arrives last.
        return None


def _events(message):
    """Tool uses in one stream message, as ``(kind, detail)`` pairs."""
    if message.get("type") != "assistant":
        return
    content = (message.get("message") or {}).get("content") or []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        yield "tool", "{} {}".format(block.get("name", "?"), _target(block.get("input") or {}))


#: Where each tool keeps the thing it is acting on, most specific first.
_TARGET_KEYS = ("file_path", "path", "command", "pattern", "prompt", "url", "description")


def _target(payload):
    if not isinstance(payload, dict):
        return ""
    for key in _TARGET_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:100]
    return ""


def _receipt(entry, result, returncode, stderr, elapsed):
    if result is None:
        # No result message: the process died, was killed, or never started
        # a turn. stderr is the only thing left that explains it.
        return Receipt(
            entry,
            ok=False,
            text=(stderr or "").strip()[:500] or "no result from claude (exit {})".format(returncode),
            duration_ms=int(elapsed * 1000),
            returncode=returncode or 1,
        )
    return Receipt(
        entry,
        ok=(not result.get("is_error")) and returncode == 0,
        text=str(result.get("result") or "").strip(),
        cost_usd=result.get("total_cost_usd"),
        duration_ms=result.get("duration_ms") or int(elapsed * 1000),
        denials=result.get("permission_denials") or (),
        returncode=returncode,
    )


def available(config=None):
    """Whether the ``claude`` CLI can be run at all, and its version."""
    binary = (config or {}).get("claude_bin") or "claude"
    try:
        result = osenv.run([binary, "--version"], timeout=30)
    except osenv.CommandError:
        return None
    return result.out if result.ok else None
