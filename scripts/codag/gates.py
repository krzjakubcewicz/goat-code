"""Deterministic quality gates: build, typecheck, lint, test.

The verifier agent must be able to tell "this pipeline broke it" from "it
was already red". So the same gates run once at ``init`` against the base
commit, and the result of that baseline run is what every later gate run is
compared against. Without it, a repo with one pre-existing lint warning
would fail every cycle forever.
"""

from __future__ import annotations

import datetime
import pathlib

from . import osenv, stack as stackmod

GATE_ORDER = ("build", "typecheck", "lint", "test")

#: How much command output to keep. Enough for a reviewer to diagnose,
#: bounded so a runaway test suite cannot produce a 40 MB artifact.
TAIL_LINES = 200


def available(profile):
    """Gate names this project actually has a command for."""
    commands = (profile or {}).get("commands") or {}
    return [name for name in GATE_ORDER if commands.get(name)]


def run_all(run, cwd, profile=None, only=None, ref=None):
    """Run every known gate in ``cwd`` and return the report dict."""
    profile = _profile(run, profile)
    commands = (profile or {}).get("commands") or {}
    timeout = run.config.get("gate_timeout_seconds", 1800)
    wanted = list(only) if only else list(GATE_ORDER)

    report = {
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "cwd": str(cwd),
        "ref": ref or _head(cwd),
        "gates": {},
    }

    for name in GATE_ORDER:
        if name not in wanted:
            continue
        command = commands.get(name)
        if not command:
            report["gates"][name] = {
                "command": None,
                "status": "missing",
                "returncode": None,
                "duration": 0.0,
                "output_tail": "",
                "note": "no {} command detected for this stack".format(name),
            }
            continue
        result = osenv.run(command, cwd=cwd, timeout=timeout)
        report["gates"][name] = {
            "command": list(command),
            "status": "pass" if result.ok else "fail",
            "returncode": result.returncode,
            "duration": round(result.duration, 2),
            "output_tail": _tail(result.stdout, result.stderr),
        }

    _summarise(report)
    return report


def capture_baseline(run, cwd, profile=None):
    """Run the gates against the base commit and persist the result."""
    report = run_all(run, cwd, profile=profile, ref=run.base_commit)
    report["is_baseline"] = True
    osenv.write_json(run.baseline_path, report)
    return report


def load_baseline(run):
    path = pathlib.Path(run.baseline_path)
    return osenv.read_json(path) if path.exists() else None


def classify(report, baseline):
    """Split failures into regressions and pre-existing breakage."""
    report["regressions"] = []
    report["pre_existing"] = []
    report["fixed"] = []
    if not baseline:
        report["baseline"] = None
        report["regressions"] = [n for n, g in report["gates"].items() if g["status"] == "fail"]
        return report

    before = {name: gate.get("status") for name, gate in (baseline.get("gates") or {}).items()}
    report["baseline"] = before
    for name, gate in report["gates"].items():
        was = before.get(name)
        now = gate["status"]
        if now == "fail" and was == "fail":
            report["pre_existing"].append(name)
            gate["pre_existing"] = True
        elif now == "fail":
            report["regressions"].append(name)
            gate["pre_existing"] = False
        elif now == "pass" and was == "fail":
            report["fixed"].append(name)
    return report


def run_and_classify(run, cwd, profile=None, only=None, out=None):
    """The verify-phase entry point: run gates, compare, persist."""
    report = run_all(run, cwd, profile=profile, only=only)
    classify(report, load_baseline(run))
    target = pathlib.Path(out) if out else run.cycle_dir() / "gates.json"
    osenv.write_json(target, report)
    report["path"] = str(target)
    return report


def blocking(report):
    """Gate names that must be fixed before this run can be called done.

    Pre-existing failures are excluded: the pipeline is not responsible for
    breakage it inherited, and blocking on it would make the run unfixable.
    """
    return list(report.get("regressions") or [])


def passed(report):
    return not blocking(report)


def render(report):
    """Human-readable gate summary."""
    lines = []
    lines.append("gates at {} ({})".format(report.get("ref", "?")[:12], report.get("cwd", "")))
    for name in GATE_ORDER:
        gate = (report.get("gates") or {}).get(name)
        if not gate:
            continue
        mark = {"pass": "PASS", "fail": "FAIL", "missing": "----"}.get(gate["status"], "?")
        detail = stackmod.command_text(gate.get("command")) if gate.get("command") else gate.get("note", "")
        suffix = ""
        if gate.get("pre_existing"):
            suffix = "  (pre-existing, not caused by this run)"
        lines.append("  {:<5} {:<10} {}{}".format(mark, name, detail, suffix))

    regressions = report.get("regressions") or []
    pre_existing = report.get("pre_existing") or []
    fixed = report.get("fixed") or []
    lines.append("")
    if regressions:
        lines.append("regressions caused by this run: {}".format(", ".join(regressions)))
    else:
        lines.append("no regressions")
    if pre_existing:
        lines.append("failing before this run too: {}".format(", ".join(pre_existing)))
    if fixed:
        lines.append("fixed by this run: {}".format(", ".join(fixed)))
    return "\n".join(lines)


# -- internals -------------------------------------------------------------


def _summarise(report):
    counts = {"pass": 0, "fail": 0, "missing": 0}
    for gate in report["gates"].values():
        counts[gate["status"]] = counts.get(gate["status"], 0) + 1
    report["summary"] = counts
    report["ok"] = counts["fail"] == 0
    return report


def _tail(stdout, stderr):
    text = (stdout or "").rstrip()
    err = (stderr or "").rstrip()
    if err:
        text = (text + "\n" if text else "") + err
    lines = text.splitlines()
    if len(lines) <= TAIL_LINES:
        return "\n".join(lines)
    dropped = len(lines) - TAIL_LINES
    return "\n".join(["... {} earlier lines omitted ...".format(dropped)] + lines[-TAIL_LINES:])


def _profile(run, profile):
    if profile is not None:
        return profile
    path = pathlib.Path(run.stack_path)
    return osenv.read_json(path) if path.exists() else {}


def _head(cwd):
    result = osenv.git(["rev-parse", "HEAD"], cwd=cwd)
    return result.out if result.ok else None
