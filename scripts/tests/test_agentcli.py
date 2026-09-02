"""Running one dispatch as a headless `claude` process.

The process itself is stubbed here. What is worth testing is everything
around it: that the argv carries the right agent, model, tools and working
directory, and that a stream of JSON lines - including a malformed one, or
none at all - always produces a receipt rather than an exception.
"""

from __future__ import annotations

import io
import json

import pytest

from goatcode import agentcli, debuglog, osenv


def entry(agent="goat-code-executor", **fields):
    base = {"agent": agent, "model": "haiku", "slice": "S1", "prompt": "C:/p/S1.md", "cwd": None}
    base.update(fields)
    return base


def argv_value(argv, flag):
    """The value after ``flag``, or None when the flag is absent."""
    return argv[argv.index(flag) + 1] if flag in argv else None


# -- the agent definition --------------------------------------------------


def test_every_agent_has_a_definition():
    for name in (
        "goat-code-planner",
        "goat-code-executor",
        "goat-code-synthesizer",
        "goat-code-verifier",
        "goat-code-e2e",
        "goat-code-scribe",
        "goat-code-replanner",
    ):
        fields, body = agentcli.definition(name)
        assert fields["name"] == name
        assert len(body) > 200, "{} has no instructions".format(name)


def test_an_unknown_agent_is_an_error():
    with pytest.raises(agentcli.AgentError):
        agentcli.definition("goatcode-nonesuch")


def test_the_body_is_the_system_prompt_without_the_frontmatter():
    _fields, body = agentcli.definition("goat-code-verifier")
    assert not body.startswith("---")
    assert "description:" not in body.splitlines()[0]


# -- the invocation --------------------------------------------------------


def test_the_argv_carries_the_agent_and_the_model():
    argv = agentcli.build_argv(entry(model="sonnet"), "C:/repo")
    assert argv[0] == "claude"
    assert "--print" in argv
    assert argv_value(argv, "--model") == "sonnet"
    assert argv_value(argv, "--system-prompt").startswith("You implement")


def test_the_prompt_is_one_line_pointing_at_the_file():
    argv = agentcli.build_argv(entry(prompt="C:/p/S2.md"), "C:/repo")
    instruction = argv[2]
    assert instruction == "Read C:/p/S2.md and follow it."
    assert len(instruction.splitlines()) == 1, "no brief is ever inlined"


def test_tools_come_from_the_agents_own_frontmatter():
    argv = agentcli.build_argv(entry("goat-code-verifier"), "C:/repo")
    assert argv_value(argv, "--allowedTools") == "Read,Grep,Glob,Bash"


def test_a_wildcard_tool_list_means_no_restriction():
    argv = agentcli.build_argv(entry("goat-code-executor"), "C:/repo")
    assert "--allowedTools" not in argv, "executors declare tools: [*]"


def test_the_repo_is_always_reachable():
    """Worktrees live outside the repo, so the run state needs adding back."""
    argv = agentcli.build_argv(entry(cwd="C:/tmp/goatcode/ab12/S1"), "C:/repo")
    assert argv_value(argv, "--add-dir") == "C:/repo"


def test_the_permission_mode_defaults_to_accepting_edits_only():
    argv = agentcli.build_argv(entry(), "C:/repo")
    assert argv_value(argv, "--permission-mode") == "acceptEdits"


def test_the_permission_mode_is_configurable():
    argv = agentcli.build_argv(entry(), "C:/repo", {"permission_mode": "bypassPermissions"})
    assert argv_value(argv, "--permission-mode") == "bypassPermissions"


def test_a_cost_cap_is_passed_through_when_set():
    assert "--max-budget-usd" not in agentcli.build_argv(entry(), "C:/repo", {})
    argv = agentcli.build_argv(entry(), "C:/repo", {"max_cost_usd": 2.5})
    assert argv_value(argv, "--max-budget-usd") == "2.5"


def test_extra_arguments_are_appended():
    argv = agentcli.build_argv(entry(), "C:/repo", {"extra_args": ["--safe-mode"]})
    assert argv[-1] == "--safe-mode"


def test_the_binary_is_configurable():
    argv = agentcli.build_argv(entry(), "C:/repo", {"claude_bin": "C:/tools/claude.cmd"})
    assert argv[0] == "C:/tools/claude.cmd"


def test_a_context_answer_rides_along_with_the_same_prompt_file():
    argv = agentcli.build_argv(entry(context="the API base is /v2"), "C:/repo")
    assert argv[2].startswith("Read C:/p/S1.md and follow it.")
    assert "the API base is /v2" in argv[2]


@pytest.mark.parametrize(
    "fields,expected",
    [({"slice": "S3"}, "S3"), ({"slice": None, "agent": "goat-code-verifier"}, "verifier")],
)
def test_a_dispatch_is_named_by_its_slice_or_its_role(fields, expected):
    assert agentcli.label(dict(entry(), **fields)) == expected


# -- reading the stream ----------------------------------------------------


def line(**fields):
    return json.dumps(fields) + "\n"


def tool_line(name, **payload):
    return line(type="assistant", message={"content": [{"type": "tool_use", "name": name, "input": payload}]})


def result_line(**fields):
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "DONE"}
    base.update(fields)
    return line(**base)


class FakeProc:
    def __init__(self, lines, returncode=0, stderr=""):
        self.stdout = io.StringIO("".join(lines))
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.args = None

    def wait(self):
        return self.returncode


@pytest.fixture
def spawn(monkeypatch):
    """Replace the process with canned output; hand back what argv it got."""
    seen = {}

    def install(lines, returncode=0, stderr=""):
        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["cwd"] = kwargs.get("cwd")
            return FakeProc(lines, returncode, stderr)

        monkeypatch.setattr(osenv.subprocess, "Popen", fake_popen)
        return seen

    return install


def test_a_finished_dispatch_reports_what_it_cost(spawn):
    spawn([result_line(total_cost_usd=0.31, duration_ms=4120)])
    receipt = agentcli.dispatch(entry(), "C:/repo")
    assert receipt.ok is True
    assert receipt.text == "DONE"
    assert receipt.cost_usd == 0.31
    assert receipt.duration_ms == 4120
    assert receipt.label == "S1"


def test_an_agent_runs_in_its_own_worktree(spawn):
    seen = spawn([result_line()])
    agentcli.dispatch(entry(cwd="C:/tmp/goatcode/ab12/S1"), "C:/repo")
    assert seen["cwd"] == "C:/tmp/goatcode/ab12/S1"


def test_an_agent_without_a_worktree_runs_in_the_repo(spawn):
    seen = spawn([result_line()])
    agentcli.dispatch(entry("goat-code-planner", cwd=None), "C:/repo")
    assert seen["cwd"] == "C:/repo"


def test_tool_uses_are_reported_as_they_arrive(spawn):
    spawn([
        tool_line("Read", file_path="src/auth.ts"),
        tool_line("Bash", command="npm test"),
        result_line(),
    ])
    events = []
    agentcli.dispatch(entry(), "C:/repo", on_event=lambda kind, label, detail: events.append((label, detail)))
    assert events == [("S1", "Read src/auth.ts"), ("S1", "Bash npm test")]


def test_text_the_agent_writes_is_not_reported(spawn):
    """Only tool calls. Prose is exactly what must not reach the caller."""
    spawn([
        line(type="assistant", message={"content": [{"type": "text", "text": "Let me think..."}]}),
        result_line(),
    ])
    events = []
    agentcli.dispatch(entry(), "C:/repo", on_event=lambda *a: events.append(a))
    assert events == []


def test_a_malformed_line_does_not_break_the_dispatch(spawn):
    spawn(["not json at all\n", "{broken\n", "\n", result_line()])
    assert agentcli.dispatch(entry(), "C:/repo").ok is True


def test_an_error_result_is_not_ok(spawn):
    spawn([result_line(is_error=True, subtype="error_during_execution", result="model refused")])
    receipt = agentcli.dispatch(entry(), "C:/repo")
    assert receipt.ok is False
    assert receipt.text == "model refused"


def test_a_nonzero_exit_is_not_ok(spawn):
    spawn([result_line()], returncode=1)
    assert agentcli.dispatch(entry(), "C:/repo").ok is False


def test_a_stream_that_stops_early_still_yields_a_receipt(spawn):
    """The process was killed. There is no result line to read."""
    spawn([tool_line("Read", file_path="a.ts")], returncode=137, stderr="Killed")
    receipt = agentcli.dispatch(entry(), "C:/repo")
    assert receipt.ok is False
    assert receipt.returncode == 137
    assert "Killed" in receipt.text


def test_denied_tool_calls_are_carried_back(spawn):
    """The headless default cannot grant a Bash permission, so it reports."""
    spawn([result_line(permission_denials=[{"tool_name": "Bash"}, {"tool_name": "Bash"}])])
    assert len(agentcli.dispatch(entry(), "C:/repo").denials) == 2


def test_a_missing_binary_says_what_to_do(monkeypatch):
    def boom(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(osenv.subprocess, "Popen", boom)
    with pytest.raises(agentcli.AgentError) as caught:
        agentcli.dispatch(entry(), "C:/repo")
    assert "claude_bin" in str(caught.value)


def test_a_dispatch_is_traced(spawn, tmp_path, monkeypatch):
    monkeypatch.setenv("GOATCODE_DEBUG", "1")
    debuglog.attach(tmp_path)
    spawn([result_line(total_cost_usd=0.5)])
    agentcli.dispatch(entry(), "C:/repo")
    assert any("agent" in ln and "goat-code-executor" in ln for ln in debuglog.read(tmp_path))


# -- the target shown for each tool ----------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"file_path": "a.ts"}, "a.ts"),
        ({"command": "npm test"}, "npm test"),
        ({"pattern": "TODO"}, "TODO"),
        ({}, ""),
        ({"command": "line one\nline two"}, "line one"),
    ],
)
def test_the_tool_target_is_one_short_line(payload, expected):
    assert agentcli._target(payload) == expected


def test_a_very_long_target_is_cut():
    assert len(agentcli._target({"command": "x" * 500})) == 100


# -- is it installed at all ------------------------------------------------


def test_availability_is_none_when_the_binary_is_missing():
    assert agentcli.available({"claude_bin": "definitely-not-claude-xyz"}) is None
