"""The standalone loop's own behaviour.

The loop itself - phases, waves, replans, caps - is covered end to end in
test_pipeline_e2e.py, which now drives this same class. What is left here is
everything that only exists because there is a terminal on the other end:
answering questions, the approval gate, and what happens when nobody is
there to answer.
"""

from __future__ import annotations

import io

import pytest

from codag import agentcli, driver as drivermod


class Tty(io.StringIO):
    """Scripted stdin that claims to be a terminal."""

    def isatty(self):
        return True


def console():
    return drivermod.Console(stream=io.StringIO(), quiet=True)


def prompter(text="", yes=False, tty=True):
    stdin = Tty(text) if tty else io.StringIO(text)
    return drivermod.Prompter(console(), yes=yes, stdin=stdin)


QUESTION = {
    "id": "Q1",
    "question": "How long should the link stay valid?",
    "options": [
        {"label": "15 minutes (Recommended)", "description": "matches the session cookie"},
        {"label": "1 hour", "description": ""},
        {"label": "24 hours", "description": ""},
    ],
}


# -- picking an answer -----------------------------------------------------


def test_the_recommendation_is_found_wherever_it_sits():
    assert drivermod.recommended(QUESTION) == "15 minutes"


def test_the_first_option_stands_in_when_nothing_is_recommended():
    assert drivermod.recommended({"options": [{"label": "A"}, {"label": "B"}]}) == "A"


def test_a_question_with_no_options_does_not_crash():
    assert drivermod.recommended({"options": []}) == ""


def test_yes_takes_every_recommendation_without_reading_anything():
    answers = prompter(yes=True).answers([QUESTION])
    assert answers == [("Q1", "15 minutes", None)]


def test_a_number_picks_that_option():
    assert prompter("2\n").answers([QUESTION]) == [("Q1", "1 hour", None)]


def test_an_empty_line_takes_the_recommendation():
    assert prompter("\n").answers([QUESTION]) == [("Q1", "15 minutes", None)]


def test_words_become_a_note_against_the_recommendation():
    """Typing an answer must not be silently dropped."""
    answers = prompter("about a working day\n").answers([QUESTION])
    assert answers == [("Q1", "15 minutes", "about a working day")]


def test_an_out_of_range_number_is_treated_as_words():
    assert prompter("9\n").answers([QUESTION])[0][2] == "9"


def test_every_question_is_asked_in_order():
    second = dict(QUESTION, id="Q2", question="Send it by email?")
    answers = prompter("1\n2\n").answers([QUESTION, second])
    assert [a[0] for a in answers] == ["Q1", "Q2"]
    assert [a[1] for a in answers] == ["15 minutes", "1 hour"]


def test_the_options_are_shown_to_the_user():
    stream = io.StringIO()
    console_ = drivermod.Console(stream=stream)
    drivermod.Prompter(console_, stdin=Tty("1\n")).answers([QUESTION])
    shown = stream.getvalue()
    assert "How long should the link stay valid?" in shown
    assert "1) 15 minutes (Recommended)" in shown
    assert "matches the session cookie" in shown


# -- the approval gate -----------------------------------------------------


@pytest.mark.parametrize("typed", ["a\n", "approve\n", "\n"])
def test_approving(typed):
    assert prompter(typed).approval() == ("approve", None)


def test_revising_collects_the_feedback():
    assert prompter("r\nsplit S2 in two\n").approval() == ("revise", "split S2 in two")


def test_an_empty_revision_asks_again():
    assert prompter("r\n\nc\n").approval() == ("cancel", None)


def test_cancelling():
    assert prompter("c\n").approval() == ("cancel", None)


def test_nonsense_asks_again_rather_than_guessing():
    assert prompter("what?\nc\n").approval() == ("cancel", None)


def test_yes_approves_without_asking():
    assert prompter(yes=True).approval() == ("approve", None)


# -- nobody there to answer ------------------------------------------------


def test_no_terminal_and_no_yes_stops_instead_of_blocking():
    with pytest.raises(drivermod.DriverError) as caught:
        prompter(tty=False).answers([QUESTION])
    assert "--yes" in str(caught.value)


def test_no_terminal_with_yes_is_fine():
    assert prompter(yes=True, tty=False).answers([QUESTION])[0][1] == "15 minutes"


def test_a_context_request_is_skipped_when_nobody_can_answer():
    assert prompter(yes=True).context("S1", "which port?") is None
    assert prompter(tty=False).context("S1", "which port?") is None


def test_a_context_request_is_relayed_when_someone_can():
    assert prompter("port 5000\n").context("S1", "which port?") == "port 5000"


# -- output ----------------------------------------------------------------


def receipt(**fields):
    base = {"entry": {"slice": "S1"}, "ok": True, "duration_ms": 4000}
    base.update(fields)
    return agentcli.Receipt(**base)


def rendered(receipt_):
    stream = io.StringIO()
    drivermod.Console(stream=stream).receipt(receipt_)
    return stream.getvalue()


def test_a_receipt_names_the_slice_and_what_it_cost():
    shown = rendered(receipt(cost_usd=0.31))
    assert "S1" in shown and "4s" in shown and "$0.31" in shown


def test_a_failure_shows_its_first_line():
    shown = rendered(receipt(ok=False, text="BLOCKED\nthe test harness is missing"))
    assert "FAIL" in shown
    assert "BLOCKED" in shown
    assert "the test harness is missing" not in shown, "one line, not the whole report"


def test_denials_explain_the_setting_that_fixes_them():
    shown = rendered(receipt(denials=[{"tool_name": "Bash"}]))
    assert "bypassPermissions" in shown


def test_quiet_keeps_receipts_but_drops_tool_lines():
    stream = io.StringIO()
    quiet = drivermod.Console(stream=stream, quiet=True)
    quiet.tool("tool", "S1", "Read a.ts")
    quiet.step({"phase": "execute", "reason": "wave of 2"})
    assert stream.getvalue() == ""
    quiet.receipt(receipt())
    assert "S1" in stream.getvalue()


def test_parallel_agents_never_tear_each_others_lines():
    import threading

    stream = io.StringIO()
    shared = drivermod.Console(stream=stream)
    threads = [
        threading.Thread(target=lambda n=n: [shared.write("{}-{}".format(n, i)) for i in range(50)])
        for n in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    assert len(lines) == 200
    assert all("-" in ln and ln.count("-") == 1 for ln in lines)


# -- the loop's own edges --------------------------------------------------


class Backend:
    """Returns canned receipts and records what it was asked to run."""

    def __init__(self, *receipts):
        self.receipts = list(receipts)
        self.calls = []

    def __call__(self, entry, on_event=None):
        self.calls.append(entry)
        return self.receipts.pop(0) if self.receipts else receipt(entry=entry)


def driver_with(backend, **kwargs):
    return drivermod.Driver("C:/repo", backend, console=console(), **kwargs)


def test_a_broken_command_stops_the_run(monkeypatch):
    driver = driver_with(Backend())
    monkeypatch.setattr(driver, "cli", lambda command: 2)
    with pytest.raises(drivermod.DriverError):
        driver.do_run({"commands": [["py", "codag.py", "--repo", "x", "gates", "run"]]})


@pytest.mark.parametrize("code", [0, 1])
def test_an_expected_failure_is_not_a_broken_command(monkeypatch, code):
    """A failing gate exits 1. That is an answer, not a crash."""
    driver = driver_with(Backend())
    monkeypatch.setattr(driver, "cli", lambda command: code)
    driver.do_run({"commands": [["py", "codag.py", "--repo", "x", "gates", "run"]]})


def test_an_unknown_action_is_refused():
    with pytest.raises(drivermod.DriverError):
        driver_with(Backend()).perform(None, {"action": "teleport"})


def wave(*slices):
    return {"dispatches": [
        {"agent": "codag-executor", "model": "haiku", "slice": s, "prompt": "p"} for s in slices
    ]}


def test_a_wave_is_dispatched_as_one_batch():
    backend = Backend()
    driver = driver_with(backend)
    driver.do_dispatch(None, wave("S1", "S2"))
    assert driver.waves == [["S1", "S2"]]
    assert {c["slice"] for c in backend.calls} == {"S1", "S2"}


# -- when the agents themselves cannot run ---------------------------------


def failing(reason="You've hit your session limit"):
    return Backend(*[receipt(ok=False, text=reason) for _ in range(8)])


@pytest.fixture
def no_repo(monkeypatch):
    """These tests exercise the driver, not the CLI: stub both out."""
    monkeypatch.setattr(
        drivermod.machine, "cli_argv", lambda _run, *args: ["py", "codag.py"] + list(args)
    )
    recorded = []
    monkeypatch.setattr(drivermod.Driver, "cli", lambda _self, command: recorded.append(list(command)) or 0)
    return recorded


def test_a_failed_dispatch_is_reported_so_the_machine_can_react(no_repo):
    """The process died before it could run its own reporting command.

    Without this the machine sees the slice unchanged and dispatches it
    again, identically, until the step cap - which is exactly what a spent
    session limit did on the first real run.
    """
    driver = driver_with(failing())
    driver.do_dispatch(None, wave("S1"))
    assert no_repo == [[
        "py", "codag.py", "report", "--slice", "S1", "--status", "BLOCKED",
        "--reason", "You've hit your session limit",
    ]]


def test_a_role_agent_failure_records_nothing(no_repo):
    """There is no slice to block. The dead-round guard catches it instead."""
    entry = {"agent": "codag-verifier", "model": "opus", "slice": None, "prompt": "p"}
    backend = Backend(receipt(ok=False, text="boom", entry=entry))
    driver_with(backend).do_dispatch(None, {"dispatches": [entry]})
    assert no_repo == []


def test_two_dead_rounds_stop_the_run(no_repo):
    driver = driver_with(failing())
    driver.do_dispatch(None, wave("S1"))
    with pytest.raises(drivermod.DriverError) as caught:
        driver.do_dispatch(None, wave("S1"))
    assert "session limit" in str(caught.value), "say what actually went wrong"


def test_one_dead_round_is_not_enough_to_stop(no_repo):
    """A single agent failing is ordinary; the machine retries it."""
    driver = driver_with(failing())
    driver.do_dispatch(None, wave("S1"))


def test_a_success_clears_the_dead_round_count(no_repo):
    backend = Backend(receipt(ok=False, text="flake"), receipt(), receipt(ok=False, text="flake"))
    driver = driver_with(backend)
    driver.do_dispatch(None, wave("S1"))
    driver.do_dispatch(None, wave("S2"))
    driver.do_dispatch(None, wave("S3"))  # would be the second dead round without the reset


def test_a_partly_failed_wave_keeps_going(no_repo):
    backend = Backend(receipt(ok=False, text="one died"), receipt())
    driver = driver_with(backend)
    driver.do_dispatch(None, wave("S1", "S2"))
    assert driver._dead_rounds == 0


def test_needs_context_is_answered_and_the_agent_runs_again():
    backend = Backend(receipt(ok=False, text="NEEDS_CONTEXT which port?"), receipt())
    driver = driver_with(backend, prompter=prompter("port 5000\n"))
    result = driver.agent({"agent": "codag-executor", "model": "haiku", "slice": "S1", "prompt": "p"})
    assert result.ok is True
    assert len(backend.calls) == 2
    assert backend.calls[1]["context"] == "port 5000"
    assert backend.calls[1]["prompt"] == "p", "the same brief, not a new one"


def test_needs_context_with_nobody_to_ask_lets_the_dispatch_fail():
    """The machine already retries a BLOCKED slice on a stronger model."""
    backend = Backend(receipt(ok=False, text="NEEDS_CONTEXT which port?"))
    driver = driver_with(backend, prompter=prompter(yes=True))
    result = driver.agent({"agent": "codag-executor", "model": "haiku", "slice": "S1", "prompt": "p"})
    assert result.ok is False
    assert len(backend.calls) == 1


def test_the_loop_gives_up_rather_than_spinning(monkeypatch):
    driver = driver_with(Backend(), limit=3)
    monkeypatch.setattr(drivermod.Run, "load", lambda *_a, **_k: object())
    monkeypatch.setattr(
        drivermod.machine, "next_action",
        lambda *_a, **_k: {"action": "run", "phase": "execute", "reason": "again", "commands": []},
    )
    with pytest.raises(drivermod.DriverError) as caught:
        driver.loop()
    assert "3 steps" in str(caught.value)


def test_the_cli_module_resolves_to_the_real_entry_point():
    module = drivermod.cli_module()
    assert hasattr(module, "main") and hasattr(module, "build_parser")
