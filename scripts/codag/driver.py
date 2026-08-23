"""The orchestrator loop, in Python.

`codag next` has always decided what happens; the only thing Claude Code
supplied was a reader for its five actions. This is that reader, so a run can
happen in a terminal with no session at all.

The loop is deliberately dumb. It performs the action it is given and asks
again. Phase transitions, model choice, retries, escalation and every cap
stay in ``machine``, which is where they are tested.

The agent backend is one callable, ``backend(entry, on_event=None) ->
Receipt``. ``claude_backend`` spawns a real ``claude`` process; the test
suite passes a fake. That is what lets the whole pipeline be proven end to
end without a model.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import pathlib
import sys
import threading
import time

from . import agentcli, machine
from .run import Run

#: Hard stop on loop iterations. The machine has its own caps - this only
#: catches a bug that would otherwise spin forever.
MAX_STEPS = 400


class DriverError(RuntimeError):
    """The run cannot continue, and it is not the pipeline's own failure."""


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


class Console:
    """Terminal output, safe to call from a wave of parallel agents."""

    def __init__(self, stream=None, quiet=False):
        self.stream = stream if stream is not None else sys.stdout
        self.quiet = quiet
        self._lock = threading.Lock()
        self.started = time.time()

    def write(self, text=""):
        with self._lock:
            self.stream.write(text + "\n")
            self.stream.flush()

    def step(self, action):
        """One line per action, before it is performed."""
        if self.quiet:
            return
        self.write("[{}] {:<10} {}".format(self._clock(), action["phase"], action["reason"]))

    def tool(self, _kind, label, detail):
        """One line per tool use inside an agent, as its stream arrives."""
        if self.quiet:
            return
        self.write("    {:<10} {}".format(label, detail))

    def receipt(self, receipt):
        cost = " ${:.2f}".format(receipt.cost_usd) if receipt.cost_usd else ""
        seconds = " {:.0f}s".format((receipt.duration_ms or 0) / 1000.0)
        self.write("  {} {}{}{}".format(
            "ok  " if receipt.ok else "FAIL", receipt.label, seconds, cost
        ))
        if not receipt.ok and receipt.text:
            self.write("       {}".format(receipt.text.splitlines()[0][:200]))
        if receipt.denials:
            self.write(
                "       {} tool call(s) denied. Set permission_mode: bypassPermissions in "
                ".codag/config.yaml to let agents run commands unattended.".format(
                    len(receipt.denials)
                )
            )

    def _clock(self):
        elapsed = int(time.time() - self.started)
        return "{:02d}:{:02d}".format(elapsed // 60, elapsed % 60)


# --------------------------------------------------------------------------
# answering
# --------------------------------------------------------------------------


class Prompter:
    """Puts the planner's questions and the approval gate to a human.

    ``yes`` takes every recommendation without asking, which is what makes an
    unattended run possible. Without it and without a terminal we stop rather
    than block forever on a read that will never return.
    """

    def __init__(self, console, yes=False, stdin=None):
        self.console = console
        self.yes = yes
        self.stdin = stdin if stdin is not None else sys.stdin

    def interactive(self):
        if self.yes:
            return False
        if not getattr(self.stdin, "isatty", bool)():
            raise DriverError(
                "this run needs an answer and there is no terminal to ask. "
                "Re-run with --yes to take the recommended option every time."
            )
        return True

    def answers(self, questions):
        """``[(id, answer, note)]`` for every question, in order."""
        if not self.interactive():
            return [(q["id"], recommended(q), None) for q in questions]

        out = []
        for question in questions:
            self.console.write("")
            self.console.write(question["question"])
            if question.get("context"):
                self.console.write("  ({})".format(question["context"]))
            for index, option in enumerate(question.get("options") or [], 1):
                self.console.write("  {}) {}".format(index, option["label"]))
                if option.get("description"):
                    self.console.write("     {}".format(option["description"]))
            answer, note = self._one(question)
            out.append((question["id"], answer, note))
        return out

    def _one(self, question):
        options = question.get("options") or []
        raw = self._read("[enter = recommended] > ")
        if not raw:
            return recommended(question), None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return plain(options[int(raw) - 1]["label"]), None
        # Anything else is the answer in the user's own words. It rides along
        # as a note against the recommendation, which is what --note is for.
        return recommended(question), raw

    def approval(self):
        """``("approve"|"revise"|"cancel", revision text or None)``."""
        if not self.interactive():
            return "approve", None
        while True:
            choice = self._read("[a]pprove / [r]evise / [c]ancel > ").lower()
            if choice in ("", "a", "approve"):
                return "approve", None
            if choice in ("r", "revise"):
                text = self._read("what should change? ")
                if text:
                    return "revise", text
            if choice in ("c", "cancel", "abort"):
                return "cancel", None

    def context(self, label, message):
        """An answer to a NEEDS_CONTEXT, or None to let the dispatch fail."""
        if self.yes or not getattr(self.stdin, "isatty", bool)():
            return None
        self.console.write("")
        self.console.write("{} needs context:".format(label))
        self.console.write(message.strip()[:1000])
        return self._read("> ") or None

    def _read(self, prompt):
        self.console.write(prompt)
        return (self.stdin.readline() or "").strip()


def recommended(question):
    """The option the planner marked, or the first one."""
    options = question.get("options") or []
    for option in options:
        if "(Recommended)" in option.get("label", ""):
            return plain(option["label"])
    return plain(options[0]["label"]) if options else ""


def plain(label):
    return label.replace(" (Recommended)", "")


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


class Driver:
    """Performs actions until the machine says stop."""

    def __init__(self, repo, backend, console=None, prompter=None, limit=MAX_STEPS):
        self.repo = repo
        self.backend = backend
        self.console = console or Console()
        self.prompter = prompter or Prompter(self.console)
        self.limit = limit
        #: Kept for the tests, and cheap enough to always record.
        self.phases = []
        self.waves = []
        self.receipts = []
        self._dead_rounds = 0

    def loop(self):
        """Run until the machine stops. Returns the final action."""
        for _ in range(self.limit):
            run = Run.load(self.repo)
            action = machine.next_action(run)
            self.phases.append(action["phase"])
            self.console.step(action)

            if action["action"] in ("stop", "escalate"):
                self.finish(run, action)
                return action
            self.perform(run, action)
        raise DriverError(
            "did not terminate in {} steps; last phases: {}".format(
                self.limit, ", ".join(self.phases[-6:])
            )
        )

    def perform(self, run, action):
        kind = action["action"]
        if kind == "run":
            return self.do_run(action)
        if kind == "dispatch":
            return self.do_dispatch(run, action)
        if kind == "ask":
            return self.do_ask(run, action)
        raise DriverError("unknown action {!r}".format(kind))

    # -- the three actions that do something -------------------------------

    def do_run(self, action):
        for command in action["commands"]:
            code = self.cli(command)
            # 1 is a real answer from several commands - a gate that failed,
            # a merge that conflicted. Anything above it is a broken tool.
            if code not in (0, 1):
                raise DriverError("{} exited {}".format(" ".join(str(c) for c in command[2:]), code))

    def do_dispatch(self, run, action):
        entries = action["dispatches"]
        batch = [e["slice"] for e in entries if e.get("slice")]
        if batch:
            self.waves.append(batch)

        if len(entries) == 1:
            results = [self.agent(entries[0])]
        else:
            # A wave goes out together. One at a time throws away the
            # parallelism the whole worktree design exists for.
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(entries)) as pool:
                results = list(pool.map(self.agent, entries))

        for receipt in results:
            self.console.receipt(receipt)
            self.receipts.append(receipt)
            if not receipt.ok:
                self.record_failure(run, receipt)

        self._guard_against_spinning(results)
        return results

    def record_failure(self, run, receipt):
        """Tell the machine what the agent would have told it.

        A dispatch whose process died never ran the reporting command, so
        without this the machine sees the slice unchanged and dispatches it
        again - identically, forever. Reported as BLOCKED, which the machine
        already knows how to retry once on a stronger model and then move
        past.
        """
        slice_id = receipt.entry.get("slice")
        if not slice_id:
            return
        reason = (receipt.text or "the agent process failed").splitlines()[0][:300]
        self.cli(machine.cli_argv(
            run, "report", "--slice", slice_id, "--status", "BLOCKED", "--reason", reason
        ))

    def _guard_against_spinning(self, results):
        """Stop when nothing is getting through at all.

        One agent failing is ordinary and the machine handles it. Every
        dispatch in two consecutive rounds failing means something outside
        the pipeline is wrong - no credentials, no network, a spent session
        limit - and retrying it 400 times helps nobody.
        """
        if any(receipt.ok for receipt in results):
            self._dead_rounds = 0
            return
        self._dead_rounds += 1
        if self._dead_rounds < 2:
            return
        detail = next((r.text for r in results if r.text), "")
        raise DriverError(
            "every agent failed twice in a row, so the run is stopping: {}".format(
                detail.splitlines()[0][:300] if detail else "no output from claude"
            )
        )

    def do_ask(self, run, action):
        for command in action.get("commands") or []:
            self.cli(command)
        if action["ask"].get("kind") == "approval":
            return self.do_approval(run, action)

        answers = self.prompter.answers(action["ask"]["questions"])
        args = ["answer"] + ["{}={}".format(qid, answer) for qid, answer, _ in answers]
        for qid, _answer, note in answers:
            if note:
                args += ["--note", "{}={}".format(qid, note)]
        self.cli(machine.cli_argv(run, *args))

    def do_approval(self, run, action):
        choice, text = self.prompter.approval()
        if choice == "revise":
            self.cli(machine.cli_argv(run, "approve", "--revise", text))
            return
        if choice == "cancel":
            self.cli(machine.cli_argv(run, "approve", "--abort"))
            raise DriverError("cancelled at the approval gate")
        self.cli(machine.cli_argv(run, "approve", "--yes"))

    def finish(self, run, action):
        """Print the closing message and run whatever the action asks for."""
        for command in action.get("commands") or []:
            self.cli(command)
        if action.get("finish"):
            self.cli(action["finish"])
        self.console.write("")
        self.console.write(action.get("message") or action["reason"])

    # -- the two things a driver actually does -----------------------------

    def agent(self, entry):
        """One dispatch, with a retry when the agent asks for context."""
        receipt = self.backend(entry, on_event=self.console.tool)
        if "NEEDS_CONTEXT" not in (receipt.text or ""):
            return receipt
        answer = self.prompter.context(receipt.label, receipt.text)
        if not answer:
            return receipt
        return self.backend(dict(entry, context=answer), on_event=self.console.tool)

    def cli(self, command):
        """Run one rendered codag command in this process.

        The machine renders a command line so agents can be told what to run.
        We are that CLI, so we call it directly - spawning it costs about a
        second, and a run makes hundreds of calls.
        """
        return cli_module().main([str(c) for c in list(command)[2:]])


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def claude_backend(repo, config=None):
    """A backend that runs each dispatch as a real ``claude`` process."""

    def backend(entry, on_event=None):
        return agentcli.dispatch(entry, repo, config, on_event)

    return backend


_CLI = None


def cli_module():
    """``scripts/codag.py``, however this process happens to have loaded it.

    It is a script beside a package of the same name, so it cannot simply be
    imported. When we were started by it, it is already ``__main__`` and
    loading a second copy would be waste.
    """
    global _CLI
    if _CLI is not None:
        return _CLI
    for name in ("__main__", "codag_cli"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "main") and hasattr(module, "build_parser"):
            _CLI = module
            return _CLI
    path = pathlib.Path(__file__).resolve().parent.parent / "codag.py"
    spec = importlib.util.spec_from_file_location("codag_cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["codag_cli"] = module
    spec.loader.exec_module(module)
    _CLI = module
    return _CLI
