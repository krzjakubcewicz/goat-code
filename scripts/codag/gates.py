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
import re

from . import osenv, stack as stackmod

GATE_ORDER = ("build", "typecheck", "lint", "test")

#: How much command output to keep. Enough for a reviewer to diagnose,
#: bounded so a runaway test suite cannot produce a 40 MB artifact.
TAIL_LINES = 200


#: A path that looks like a test file. Deliberately broad - a false positive
#: costs one advisory line, a false negative costs the finding.
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)(test_[^/]+|[^/]+[._](test|spec))\.[a-z]+$")

#: `.count() >= 1`, `len(x) > 0`, `assert x.count() >= n` - an assertion that
#: passes while the behaviour is wrong. The single most repeated defect in the
#: recorded runs' verdicts.
_LOOSE_COUNT_RE = re.compile(r"\b(count\(\)|len\([^)]*\))\s*(>=|>|!=)")

#: `assert foo() == foo()` - a test that cannot fail.
_SELF_COMPARE_RE = re.compile(r"assert\s+(?P<left>[^=<>!\n]{2,60}?)\s*==\s*(?P=left)\s*$")

#: A test function's opening line, in the languages the pipeline meets.
_TEST_FUNC_RE = re.compile(
    r"^\s*(?:async\s+)?(?:def\s+(?P<py>test_\w+)|(?:it|test)\s*\(\s*[\"'`])"
)

_ASSERT_RE = re.compile(r"\b(assert|expect|assertEqual|assertTrue|should)\b")


def is_test_path(path):
    return bool(_TEST_PATH_RE.search(str(path).replace("\\", "/")))


def weak_assertions(root, paths):
    """Assertions that pass while the behaviour is wrong, in changed tests.

    Advisory, never blocking. The gates caught a real regression in 1 of 21
    recorded cycles; the verifier failed 13 of 21, essentially all of them on
    assertion strength. This points the cheap deterministic pass at the
    failure that actually happens, and hands the same lead to the executor,
    the replanner and the verifier, who all read gates.json.

    Heuristic on purpose: it reports suspicion, it does not rule. Judging
    whether an assertion really proves its criterion stays the verifier's job.
    """
    root = pathlib.Path(root)
    findings = []
    for relpath in paths or []:
        if not is_test_path(relpath):
            continue
        target = root / relpath
        if not target.is_file():
            continue
        try:
            lines = osenv.read_text(target).splitlines()
        except OSError:
            continue
        findings.extend(_scan(relpath, lines))
    return findings


def _scan(relpath, lines):
    out = []
    for number, line in enumerate(lines, start=1):
        if _LOOSE_COUNT_RE.search(line):
            out.append(_finding(relpath, number, line, "count/length compared with an inequality; assert the exact value"))
        elif _SELF_COMPARE_RE.search(line.rstrip()):
            out.append(_finding(relpath, number, line, "both sides are the same expression, so it cannot fail"))
    out.extend(_bodies_without_assertions(relpath, lines))
    return out


def _bodies_without_assertions(relpath, lines):
    """Test functions whose body never asserts anything."""
    out = []
    opened = None
    body = []
    for number, line in enumerate(lines, start=1):
        if _TEST_FUNC_RE.match(line):
            if opened and not any(_ASSERT_RE.search(b) for b in body):
                out.append(_finding(relpath, opened[0], opened[1], "test body contains no assertion"))
            opened, body = (number, line), []
            continue
        if opened is not None:
            body.append(line)
    if opened and not any(_ASSERT_RE.search(b) for b in body):
        out.append(_finding(relpath, opened[0], opened[1], "test body contains no assertion"))
    return out


def _finding(relpath, number, line, reason):
    return {"path": relpath, "line": number, "reason": reason, "source": line.strip()[:160]}


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
    where = _project_cwd(cwd, profile)

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
        # A tool missing from this host - it often only exists inside the
        # project's container - is not the same as a failing gate, and must
        # never take down the command that asked for it. `_run_one` reports
        # that as "missing".
        report["gates"][name] = _run_one(command, where, timeout)

    _run_siblings(report, run, cwd, profile, wanted, timeout)
    _summarise(report)
    return report


def sibling_gate_name(name, directory):
    return "{} [{}]".format(name, directory)


def _run_siblings(report, run, cwd, profile, wanted, timeout):
    """Gate the other halves of a monorepo, not just the one we detected.

    Gating one half and calling it a safety net is how a run ships with its
    other half untested - across eleven recorded runs the frontend suite was
    never gated once, and the verifier ran it by hand instead.

    A sibling whose directory is absent from this worktree is skipped rather
    than reported missing: the baseline and every cycle then agree, so an
    absent half never reads as a regression.
    """
    for sibling in (profile or {}).get("sibling_projects") or []:
        directory = sibling.get("dir")
        where = pathlib.Path(cwd) / directory if directory else None
        if not directory or not where.is_dir():
            continue
        for name in GATE_ORDER:
            if name not in wanted:
                continue
            command = (sibling.get("commands") or {}).get(name)
            if not command:
                continue
            report["gates"][sibling_gate_name(name, directory)] = _run_one(command, where, timeout)


def _run_one(command, where, timeout):
    try:
        result = osenv.run(command, cwd=where, timeout=timeout)
    except osenv.CommandError as exc:
        return {
            "command": list(command),
            "status": "missing",
            "returncode": exc.returncode,
            "duration": 0.0,
            "output_tail": "",
            "note": exc.stderr.strip() or "could not run {}".format(command[0]),
        }
    return {
        "command": list(command),
        "status": "pass" if result.ok else "fail",
        "returncode": result.returncode,
        "duration": round(result.duration, 2),
        "output_tail": _tail(result.stdout, result.stderr),
    }


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


def run_and_classify(run, cwd, profile=None, only=None, out=None, changed=None):
    """The verify-phase entry point: run gates, compare, persist.

    ``changed`` are the paths this run touched; their test files get the
    weak-assertion scan. Advisory only - it never enters ``blocking``.
    """
    report = run_all(run, cwd, profile=profile, only=only)
    classify(report, load_baseline(run))
    report["weak_assertions"] = weak_assertions(cwd, changed)
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
    gates_map = report.get("gates") or {}
    extra = [name for name in gates_map if name not in GATE_ORDER]
    for name in list(GATE_ORDER) + sorted(extra):
        gate = gates_map.get(name)
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


def _project_cwd(cwd, profile):
    """Where the gate commands run: the detected project dir, else ``cwd``.

    ``stack.detect`` sets ``project_dir`` when the build system sits one
    level down (an app under ``backend/``). The git ref still comes from
    ``cwd`` - the worktree root is the thing being judged.
    """
    override = (profile or {}).get("commands_cwd")
    # "" is a real answer - the repo root - so only None defers to project_dir.
    rel = override if override is not None else (profile or {}).get("project_dir")
    if not rel:
        return cwd
    candidate = pathlib.Path(cwd) / rel
    return candidate if candidate.is_dir() else cwd


def _head(cwd):
    result = osenv.git(["rev-parse", "HEAD"], cwd=cwd)
    return result.out if result.ok else None
