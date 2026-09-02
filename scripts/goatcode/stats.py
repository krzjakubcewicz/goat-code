"""Where a run's time and cycles went.

Derived entirely from artifacts the pipeline always writes - the ledger's
timestamped lines, each cycle's ``verdict.md`` and ``gates.json``. Nothing
here depends on debug mode, which is the point: across eleven recorded runs
debug was never switched on, so ``log.txt`` never existed and no run could
say which phase had cost it a day.

Reading rather than measuring also means this works on runs that finished
long before it was written.
"""

from __future__ import annotations

import datetime
import re

from . import ledger, osenv, report as reportmod

#: ``[2026-09-02T10:00:00] cycle 1: phase grill -> execute``
ENTRY_RE = re.compile(r"^\[(?P<stamp>[0-9T:.-]+)\]\s+cycle\s+(?P<cycle>\d+):\s+(?P<text>.*)$")

PHASE_RE = re.compile(r"^phase (?P<was>\S+) -> (?P<now>\S+)$")
SLICE_RE = re.compile(r"^slice (?P<slice>\S+) (?P<status>\w+)")


def collect(run):
    """The whole picture for one run, as a plain dict."""
    events = _events(run)
    phases = _phases(events)
    cycles = max([run.cycle] + [e["cycle"] for e in events])

    return {
        "run_id": run.run_id,
        "cycles": cycles,
        "phases": phases,
        "totals": _totals(phases),
        "elapsed_seconds": _elapsed(events),
        "slices": _slices(events),
        "verdicts": [reportmod.read_verdict(run, cycle) for cycle in range(1, cycles + 1)],
        "gates": _gates(run, cycles),
    }


def render(report):
    """The same picture for a human, shortest useful form."""
    lines = ["run {}".format(report["run_id"])]
    lines.append(
        "  {} cycle{}, {} elapsed".format(
            report["cycles"], "" if report["cycles"] == 1 else "s", duration(report["elapsed_seconds"])
        )
    )

    totals = report["totals"]
    if totals:
        lines.append("")
        lines.append("  time by phase")
        for phase, seconds in sorted(totals.items(), key=lambda kv: -kv[1]):
            lines.append("    {:<10} {}".format(phase, duration(seconds)))

    running = [entry for entry in report["phases"] if entry["seconds"] is None]
    if running:
        lines.append("    {:<10} (still running)".format(running[-1]["phase"]))

    slices = report["slices"]
    lines.append("")
    lines.append(
        "  slices: {} in cycle 1, {} remedial".format(slices["first_cycle"], slices["remedial"])
    )

    verdicts = report["verdicts"]
    if any(verdicts):
        rendered = ", ".join(
            "cycle {} {}".format(i + 1, v or "no verdict") for i, v in enumerate(verdicts)
        )
        lines.append("  verdicts: {}".format(rendered))

    for entry in report["gates"]:
        if entry["regressions"]:
            lines.append(
                "  cycle {} gate regressions: {}".format(entry["cycle"], ", ".join(entry["regressions"]))
            )
    return "\n".join(lines)


def duration(seconds):
    """``900`` -> ``15m``. Coarse on purpose: nobody tunes a pipeline in seconds."""
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return "{}s".format(seconds)
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return "{}m".format(minutes) if rest < 30 else "{}m".format(minutes + 1)
    hours, minutes = divmod(minutes, 60)
    return "{}h{:02d}m".format(hours, minutes)


# -- internals -------------------------------------------------------------


def _events(run):
    out = []
    for entry in ledger.entries(run):
        match = ENTRY_RE.match(entry)
        if not match:
            continue
        try:
            stamp = datetime.datetime.fromisoformat(match.group("stamp"))
        except ValueError:
            continue
        out.append({"at": stamp, "cycle": int(match.group("cycle")), "text": match.group("text")})
    return out


def _phases(events):
    """One entry per phase entered, in order, with how long it then lasted.

    The phase a run is currently sitting in has no duration yet - reporting
    zero there would read as "instant" when it means "still going".
    """
    entered = []
    for event in events:
        match = PHASE_RE.match(event["text"])
        if match:
            entered.append({"phase": match.group("now"), "entered_at": event["at"], "seconds": None})

    for current, following in zip(entered, entered[1:]):
        current["seconds"] = int((following["entered_at"] - current["entered_at"]).total_seconds())

    for entry in entered:
        entry["entered_at"] = entry["entered_at"].isoformat()
    return entered


def _totals(phases):
    """Seconds per phase name, summed over every time it was entered."""
    out = {}
    for entry in phases:
        if entry["seconds"] is None:
            continue
        out[entry["phase"]] = out.get(entry["phase"], 0) + entry["seconds"]
    return out


def _elapsed(events):
    if not events:
        return 0
    return int((events[-1]["at"] - events[0]["at"]).total_seconds())


def _slices(events):
    """Executor dispatches that finished, split by first cycle versus rework.

    The ratio is the headline number: in the recorded runs 28 of 72 finished
    slices were remedial, every one of them because cycle 1 failed.
    """
    first = remedial = 0
    for event in events:
        if not SLICE_RE.match(event["text"]):
            continue
        if event["cycle"] <= 1:
            first += 1
        else:
            remedial += 1
    return {"first_cycle": first, "remedial": remedial}


def _gates(run, cycles):
    out = []
    for cycle in range(1, cycles + 1):
        path = run.cycle_dir(cycle) / "gates.json"
        if not path.exists():
            continue
        data = osenv.read_json(path) or {}
        out.append(
            {
                "cycle": cycle,
                "regressions": data.get("regressions") or [],
                "pre_existing": data.get("pre_existing") or [],
            }
        )
    return out
