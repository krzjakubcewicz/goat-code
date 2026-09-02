# Task Classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify every run's complexity and risk before planning, and route it to a cheaper or heavier workflow accordingly, with deterministic rules that can only ever raise risk.

**Architecture:** A new `classify` phase sits between `init` and `grill`. A `goat-code-classifier` agent writes `classification.json`; `goatcode classify` validates it against a strict schema and merges it with deterministic risk rules that may escalate but never lower. `workflow.py` turns the final classification into one of three workflows, and `machine.derive_phase` reads that workflow to decide whether to grill, whether to gate, and whether to run the verifier. Everything is derived from files on disk, as the rest of the machine already is, so the whole feature is drivable with no model.

**Tech Stack:** Python 3.9+, standard library only (no pip, ever). `miniyaml` for config, `osenv` for all I/O and subprocesses, pytest for tests.

**Spec:** `add-task-classifier-to-claude-code-plugin.md` (repo root)

## Global Constraints

Copied from the spec and from this repo's existing guarantees. Every task's requirements implicitly include this section.

- **Stdlib only.** No pip install anywhere. No new dependency, not even a JSON-schema library — validation is hand-written.
- **Python floor is 3.9.** CI runs windows/macos/ubuntu × 3.9 and 3.13. `Path.write_text(newline=...)` is 3.10+ and is banned by `test_no_module_uses_a_pathlib_argument_newer_than_the_floor`; use `path.open("w", encoding="utf-8", newline="\n")`.
- **No shell, ever.** Every subprocess takes an argument list with `shell=False`. Only `osenv` may `import subprocess` — enforced by `test_only_osenv_calls_subprocess_directly`.
- **All writes go through `osenv.write_text` / `osenv.write_json`.** They are atomic and LF-normalised.
- **The classifier is advisory; deterministic policy is authoritative.** The LLM may never lower a risk the rules raised, and may never transition execution state.
- **Fail safe.** Any classifier failure — timeout, malformed JSON, unknown enum, missing field — falls back to `NORMAL` complexity and the risk the deterministic rules found, never to a cheaper workflow.
- **Disabled means unchanged.** With `classifier.enabled: false` every existing run behaves exactly as it does today, including the grill, the approval gate and the verifier.
- **Naming.** Agents, skills and commands are `goat-code-*`; Python modules and paths are `goatcode`.
- **No secrets in the audit trail.** The ledger records factor names and rule ids, never matched file contents.

---

## File Structure

**Created:**

- `scripts/goatcode/classify.py` — the domain: enums, the `Classification` record, strict schema validation of the agent's JSON, the deterministic `RiskPolicy` rules, and the merge that lets rules escalate only.
- `scripts/goatcode/workflow.py` — `Workflow` names and `select()`, the one place routing lives.
- `agents/goat-code-classifier.md` — the agent definition.
- `scripts/tests/test_classify.py` — unit tests for the domain, schema and risk rules.
- `scripts/tests/test_workflow.py` — routing table tests.

**Modified:**

- `scripts/goatcode/run.py` — add `classify` to `PHASES`, classifier config defaults, and the `Run` predicates the machine asks (`wants_classification`, `classification`, `workflow`).
- `scripts/goatcode/machine.py` — the `_classify` handler, and the four points in `derive_phase` that consult the workflow.
- `scripts/goatcode/dispatch.py` — `classifier()` prompt renderer.
- `scripts/goatcode/report.py` — `record_classification`, and `classifier` in `ROLE_STATUSES`.
- `scripts/goatcode.py` — the `classify` CLI command.
- `scripts/tests/test_machine.py` — phase derivation and routing through the machine.
- `scripts/tests/test_cli_reporting.py` — the CLI surface and fallback behaviour.
- `templates/config.yaml` — the documented `classifier:` block.
- `skills/goat-code-conventions/SKILL.md` — the CLI table row and the classification contract.
- `docs/ARCHITECTURE.md` — why the classifier is advisory and the rules authoritative.
- `docs/PIPELINE.md` — the `classify` phase walkthrough.

Splitting `classify.py` from `workflow.py` is deliberate: the spec's central rule is that classification and routing are separate concerns, and a router that cannot see the LLM's raw output cannot be tempted to re-derive risk from it.

---

## Task 1: The classification domain and its schema

**Files:**
- Create: `scripts/goatcode/classify.py`
- Test: `scripts/tests/test_classify.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `COMPLEXITIES = ("SIMPLE", "NORMAL", "COMPLEX")`
  - `RISKS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")`
  - `class ClassifyError(RuntimeError)`
  - `parse(payload: dict) -> dict` — validates and returns a normalised classification dict with keys `complexity`, `risk`, `reasoning`, `risk_factors`, `complexity_factors`. Raises `ClassifyError` naming every problem at once.
  - `FALLBACK = {"complexity": "NORMAL", "risk": "MEDIUM", "reasoning": ..., "risk_factors": [], "complexity_factors": []}`
  - `risk_at_least(left: str, right: str) -> str` — the higher of two risk names.

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_classify.py
"""Classification: the advisory half, and the schema that keeps it honest.

The classifier is an LLM, so every one of these cases is a thing that will
happen in production, not a hypothetical.
"""

from __future__ import annotations

import pytest

from goatcode import classify
from goatcode.classify import ClassifyError


def payload(**overrides):
    base = {
        "complexity": "COMPLEX",
        "risk": "HIGH",
        "reasoning": "Changes authentication across two modules.",
        "riskFactors": ["authentication"],
        "complexityFactors": ["multiple modules"],
    }
    base.update(overrides)
    return base


def test_a_well_formed_payload_parses_to_normalised_keys():
    result = classify.parse(payload())
    assert result["complexity"] == "COMPLEX"
    assert result["risk"] == "HIGH"
    assert result["risk_factors"] == ["authentication"]
    assert result["complexity_factors"] == ["multiple modules"]
    assert result["reasoning"] == "Changes authentication across two modules."


def test_an_unknown_complexity_is_rejected():
    with pytest.raises(ClassifyError) as excinfo:
        classify.parse(payload(complexity="TRIVIAL"))
    assert "TRIVIAL" in str(excinfo.value)
    assert "SIMPLE" in str(excinfo.value), "the error must name what was allowed"


def test_an_unknown_risk_is_rejected():
    with pytest.raises(ClassifyError) as excinfo:
        classify.parse(payload(risk="SPICY"))
    assert "SPICY" in str(excinfo.value)


def test_a_missing_field_is_rejected():
    broken = payload()
    del broken["risk"]
    with pytest.raises(ClassifyError) as excinfo:
        classify.parse(broken)
    assert "risk" in str(excinfo.value)


def test_every_problem_is_named_at_once():
    with pytest.raises(ClassifyError) as excinfo:
        classify.parse({"complexity": "TRIVIAL", "risk": "SPICY"})
    message = str(excinfo.value)
    assert "TRIVIAL" in message and "SPICY" in message and "reasoning" in message


def test_factors_must_be_a_list_of_strings():
    with pytest.raises(ClassifyError):
        classify.parse(payload(riskFactors="authentication"))


def test_a_non_mapping_payload_is_rejected():
    with pytest.raises(ClassifyError):
        classify.parse(["COMPLEX"])


def test_lowercase_enums_are_accepted_and_normalised():
    """Models return `high` as readily as `HIGH`; that is not a failure."""
    assert classify.parse(payload(risk="high"))["risk"] == "HIGH"


def test_the_fallback_is_conservative_and_never_simple():
    assert classify.FALLBACK["complexity"] == "NORMAL"
    assert classify.FALLBACK["risk"] == "MEDIUM"


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("LOW", "HIGH", "HIGH"),
        ("HIGH", "LOW", "HIGH"),
        ("CRITICAL", "HIGH", "CRITICAL"),
        ("LOW", "LOW", "LOW"),
    ],
)
def test_risk_at_least_takes_the_higher(left, right, expected):
    assert classify.risk_at_least(left, right) == expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_classify.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'goatcode.classify'`

- [ ] **Step 3: Write the minimal implementation**

```python
# scripts/goatcode/classify.py
"""What kind of task this is, and how much machinery it deserves.

Two independent dimensions. A one-file change to authentication is SIMPLE by
size and HIGH by risk, and it is the risk that decides how much verification
the run gets.

The LLM half of this is advisory. Nothing here trusts it beyond what
``parse`` will accept, and ``apply_rules`` in this module is what makes the
deterministic half authoritative.
"""

from __future__ import annotations

COMPLEXITIES = ("SIMPLE", "NORMAL", "COMPLEX")
RISKS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

#: Bumped whenever RULES or the classifier prompt change. Recorded with every
#: classification, so a run's routing can be explained months later by the
#: rules that were actually in force rather than the ones in force now.
VERSION = 1

#: Where classification lands when the classifier cannot be trusted or run.
#: Never SIMPLE: an unavailable classifier must not buy a cheaper workflow.
FALLBACK = {
    "complexity": "NORMAL",
    "risk": "MEDIUM",
    "reasoning": "classification unavailable; conservative default applied",
    "risk_factors": [],
    "complexity_factors": [],
}


class ClassifyError(RuntimeError):
    """A classification was rejected, with every reason it can act on."""


def risk_at_least(left, right):
    """The higher of two risk names."""
    return RISKS[max(RISKS.index(left), RISKS.index(right))]


def parse(payload):
    """Validate the agent's JSON. Returns a normalised dict.

    Strict on purpose: an unknown enum from a model is far more likely to be
    a hallucinated category than a category we forgot.
    """
    if not isinstance(payload, dict):
        raise ClassifyError("classification must be a JSON object, got {}".format(type(payload).__name__))

    problems = []
    complexity = _enum(payload.get("complexity"), COMPLEXITIES, "complexity", problems)
    risk = _enum(payload.get("risk"), RISKS, "risk", problems)
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        problems.append("'reasoning' must be a non-empty string saying why")

    risk_factors = _factors(payload.get("riskFactors"), "riskFactors", problems)
    complexity_factors = _factors(payload.get("complexityFactors"), "complexityFactors", problems)

    if problems:
        raise ClassifyError("; ".join(problems))

    return {
        "complexity": complexity,
        "risk": risk,
        "reasoning": reasoning.strip(),
        "risk_factors": risk_factors,
        "complexity_factors": complexity_factors,
    }


def _enum(value, allowed, field, problems):
    if value is None:
        problems.append("'{}' is missing; expected one of {}".format(field, ", ".join(allowed)))
        return None
    text = str(value).strip().upper()
    if text not in allowed:
        problems.append("'{}' is {!r}; expected one of {}".format(field, value, ", ".join(allowed)))
        return None
    return text


def _factors(value, field, problems):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        problems.append("'{}' must be a list of strings".format(field))
        return []
    return [v.strip() for v in value if v.strip()]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_classify.py -q`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/classify.py scripts/tests/test_classify.py
git commit -m "classify a task by complexity and risk, strictly"
```

---

## Task 2: Deterministic risk rules that can only escalate

**Files:**
- Modify: `scripts/goatcode/classify.py`
- Test: `scripts/tests/test_classify.py`

**Interfaces:**
- Consumes: `COMPLEXITIES`, `RISKS`, `risk_at_least` from Task 1.
- Produces:
  - `RULES` — a tuple of `(rule_id, minimum_risk, pattern)` triples.
  - `evaluate(text: str, paths: list) -> dict` with keys `risk` and `factors` (a list of rule ids). `text` is spec prose, `paths` are globs from a plan (empty at classify time).
  - `apply_rules(classification: dict, text: str, paths: list) -> dict` — returns a new classification whose `risk` is `risk_at_least(llm_risk, rule_risk)`, with `deterministic_overrides` listing the rule ids that raised it. Never lowers.

- [ ] **Step 1: Write the failing tests**

```python
# append to scripts/tests/test_classify.py

# -- the authoritative half ------------------------------------------------
#
# The spec's central rule: the LLM may say LOW about a task that touches
# authentication, and it must not matter.


def test_authentication_in_the_text_raises_risk_to_high():
    found = classify.evaluate("Rework the login and session token handling.", [])
    assert found["risk"] == "HIGH"
    assert "authentication" in found["factors"]


def test_a_migration_raises_risk():
    found = classify.evaluate("Add a database migration dropping the old column.", [])
    assert found["risk"] in ("HIGH", "CRITICAL")


def test_ordinary_prose_raises_nothing():
    found = classify.evaluate("Rename a button label on the settings screen.", [])
    assert found["risk"] == "LOW"
    assert found["factors"] == []


def test_a_sensitive_path_raises_risk_even_when_the_text_is_innocent():
    found = classify.evaluate("Tidy some helpers.", ["src/auth/**", "src/util/**"])
    assert found["risk"] == "HIGH"
    assert "authentication" in found["factors"]


def test_a_ci_config_path_raises_risk():
    found = classify.evaluate("Tidy some helpers.", [".github/workflows/**"])
    assert found["risk"] == "HIGH"


def test_rules_escalate_an_llm_that_said_low():
    said = classify.parse(payload(risk="LOW", complexity="SIMPLE"))
    final = classify.apply_rules(said, "Change the auth token expiry.", [])
    assert final["risk"] == "HIGH"
    assert final["deterministic_overrides"] == ["authentication"]
    assert final["complexity"] == "SIMPLE", "rules speak to risk, not to size"


def test_rules_never_lower_what_the_llm_raised():
    said = classify.parse(payload(risk="CRITICAL"))
    final = classify.apply_rules(said, "Rename a label.", [])
    assert final["risk"] == "CRITICAL"
    assert final["deterministic_overrides"] == []


def test_the_fallback_is_still_escalated_by_the_rules():
    """A classifier that never answered must not soften a sensitive task."""
    final = classify.apply_rules(dict(classify.FALLBACK), "Rotate the signing secret.", [])
    assert final["risk"] == "HIGH"


def test_matching_is_case_insensitive():
    assert classify.evaluate("REWORK THE LOGIN FLOW", [])["risk"] == "HIGH"


def test_a_factor_is_reported_once_however_many_times_it_matches():
    found = classify.evaluate("auth, authentication, login, oauth", [])
    assert found["factors"].count("authentication") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_classify.py -q -k "raises or escalate or never_lower or fallback_is_still or case_insensitive or reported_once"`
Expected: FAIL — `AttributeError: module 'goatcode.classify' has no attribute 'evaluate'`

- [ ] **Step 3: Write the minimal implementation**

```python
# append to scripts/goatcode/classify.py

import re

#: Deterministic risk rules: (factor, minimum risk, pattern).
#:
#: Matched against the spec's prose and, once a plan exists, against the
#: paths its slices claim. Deliberately broad - a false "this is risky"
#: costs one extra verification pass, a false "this is safe" ships an
#: unreviewed change to authentication.
RULES = (
    ("authentication", "HIGH", r"\b(auth|authentication|authoriz|login|session|oauth|jwt|password|credential)"),
    ("cryptography", "HIGH", r"\b(crypto|encrypt|decrypt|signing|signature|cipher|tls|certificate)"),
    ("secrets", "HIGH", r"\b(secret|token|api[_ -]?key|private[_ -]?key|\.env)\b"),
    ("permissions", "HIGH", r"\b(permission|role|rbac|access[_ -]control|privilege)"),
    ("ci-cd", "HIGH", r"(\.github/workflows|\bci\b|\bcd\b|pipeline|deploy|release)"),
    ("infrastructure", "HIGH", r"\b(terraform|kubernetes|k8s|dockerfile|docker-compose|infra)"),
    ("database-migration", "HIGH", r"\b(migration|schema change|alter table|drop table|drop column)"),
    ("data-deletion", "CRITICAL", r"\b(delete all|purge|truncate|hard[_ -]delete|wipe)"),
    ("customer-data", "HIGH", r"\b(pii|personal data|customer data|gdpr)"),
    ("dependencies", "MEDIUM", r"\b(dependency|dependencies|upgrade|bump|lockfile|package\.json|requirements\.txt)"),
)

_COMPILED = tuple((factor, minimum, re.compile(pattern, re.IGNORECASE)) for factor, minimum, pattern in RULES)


def evaluate(text, paths=None):
    """The risk the deterministic rules find, and why.

    ``text`` is the spec's prose; ``paths`` are the globs a plan's slices
    claim, which do not exist yet at classify time. Both are matched the
    same way, so a plan that reaches into `src/auth/**` escalates a run whose
    description never mentioned authentication.
    """
    haystack = "\n".join([text or ""] + [str(p) for p in (paths or [])])
    risk = "LOW"
    factors = []
    for factor, minimum, pattern in _COMPILED:
        if pattern.search(haystack):
            risk = risk_at_least(risk, minimum)
            if factor not in factors:
                factors.append(factor)
    return {"risk": risk, "factors": factors}


def apply_rules(classification, text, paths=None):
    """Merge the advisory classification with the authoritative rules.

    Escalate-only. The LLM cannot talk risk down, which is the whole reason
    this layer exists; it can only ever be more cautious than the rules.
    """
    found = evaluate(text, paths)
    merged = dict(classification)
    raised = RISKS.index(found["risk"]) > RISKS.index(classification.get("risk", "LOW"))
    merged["risk"] = risk_at_least(classification.get("risk", "LOW"), found["risk"])
    merged["deterministic_overrides"] = list(found["factors"]) if raised else []
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_classify.py -q`
Expected: PASS (22 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/classify.py scripts/tests/test_classify.py
git commit -m "let deterministic rules raise risk, never lower it"
```

---

## Task 3: The workflow router

**Files:**
- Create: `scripts/goatcode/workflow.py`
- Test: `scripts/tests/test_workflow.py`

**Interfaces:**
- Consumes: nothing from earlier tasks at import time; `select` takes the dict Task 2 produces.
- Produces:
  - `WORKFLOWS = ("DIRECT_DEVELOPMENT", "PLANNED_DEVELOPMENT", "HIGH_RISK_DEVELOPMENT")`
  - `select(classification: dict) -> str`
  - `wants_grill(workflow)`, `wants_gate(workflow)`, `wants_verifier(workflow)`, `wants_approval(workflow)` — each `-> bool`. These four are the only questions the machine asks about a workflow.

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_workflow.py
"""Routing lives in exactly one place, so it can be read in one sitting."""

from __future__ import annotations

import pytest

from goatcode import workflow


def classification(complexity="NORMAL", risk="LOW"):
    return {"complexity": complexity, "risk": risk}


@pytest.mark.parametrize(
    "complexity,risk,expected",
    [
        ("SIMPLE", "LOW", "DIRECT_DEVELOPMENT"),
        ("NORMAL", "LOW", "DIRECT_DEVELOPMENT"),
        ("NORMAL", "MEDIUM", "DIRECT_DEVELOPMENT"),
        ("COMPLEX", "LOW", "PLANNED_DEVELOPMENT"),
        ("COMPLEX", "MEDIUM", "PLANNED_DEVELOPMENT"),
        ("SIMPLE", "HIGH", "HIGH_RISK_DEVELOPMENT"),
        ("SIMPLE", "CRITICAL", "HIGH_RISK_DEVELOPMENT"),
        ("COMPLEX", "HIGH", "HIGH_RISK_DEVELOPMENT"),
    ],
)
def test_the_routing_table(complexity, risk, expected):
    assert workflow.select(classification(complexity, risk)) == expected


def test_risk_outranks_size():
    """A one-file auth change is small and still gets the whole pipeline."""
    assert workflow.select(classification("SIMPLE", "HIGH")) == "HIGH_RISK_DEVELOPMENT"


def test_an_unknown_classification_routes_to_the_safe_middle():
    assert workflow.select({}) == "PLANNED_DEVELOPMENT"


@pytest.mark.parametrize(
    "name,grill,gate,verifier,approval",
    [
        ("DIRECT_DEVELOPMENT", False, False, False, False),
        ("PLANNED_DEVELOPMENT", True, True, True, False),
        ("HIGH_RISK_DEVELOPMENT", True, True, True, True),
    ],
)
def test_what_each_workflow_switches_on(name, grill, gate, verifier, approval):
    assert workflow.wants_grill(name) is grill
    assert workflow.wants_gate(name) is gate
    assert workflow.wants_verifier(name) is verifier
    assert workflow.wants_approval(name) is approval


def test_an_unknown_workflow_name_is_treated_as_the_heaviest():
    assert workflow.wants_verifier("NONSENSE") is True
    assert workflow.wants_grill("NONSENSE") is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_workflow.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'goatcode.workflow'`

- [ ] **Step 3: Write the minimal implementation**

```python
# scripts/goatcode/workflow.py
"""Which pipeline a classified task gets, decided in one place.

The spec's rule, and the reason this is its own module: routing must not be
scattered across the agents. Every phase question the machine asks about a
workflow is one of the four predicates below.
"""

from __future__ import annotations

WORKFLOWS = ("DIRECT_DEVELOPMENT", "PLANNED_DEVELOPMENT", "HIGH_RISK_DEVELOPMENT")

#: What each workflow switches on: grill, approval gate, verifier, human sign-off.
_SWITCHES = {
    "DIRECT_DEVELOPMENT": (False, False, False, False),
    "PLANNED_DEVELOPMENT": (True, True, True, False),
    "HIGH_RISK_DEVELOPMENT": (True, True, True, True),
}

#: Anything unrecognised gets the heaviest pipeline. Falling back to a
#: cheaper one would make an unreadable classification a way to buy less
#: verification.
_DEFAULT = "HIGH_RISK_DEVELOPMENT"


def select(classification):
    """The workflow a final classification earns.

    Risk outranks complexity: a one-file change to authentication is SIMPLE
    by size and still deserves the whole pipeline.
    """
    risk = str((classification or {}).get("risk", "")).upper()
    complexity = str((classification or {}).get("complexity", "")).upper()

    if risk in ("HIGH", "CRITICAL"):
        return "HIGH_RISK_DEVELOPMENT"
    if complexity == "COMPLEX":
        return "PLANNED_DEVELOPMENT"
    if complexity in ("SIMPLE", "NORMAL"):
        return "DIRECT_DEVELOPMENT"
    return "PLANNED_DEVELOPMENT"


def _switch(name, index):
    return _SWITCHES.get(name, _SWITCHES[_DEFAULT])[index]


def wants_grill(name):
    """Whether the planner may put questions to the user first."""
    return _switch(name, 0)


def wants_gate(name):
    """Whether the plan needs approving before any code is written."""
    return _switch(name, 1)


def wants_verifier(name):
    """Whether a model judges the merged diff, or the gates alone decide."""
    return _switch(name, 2)


def wants_approval(name):
    """Whether a human signs the finished work off."""
    return _switch(name, 3)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_workflow.py -q`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/workflow.py scripts/tests/test_workflow.py
git commit -m "route a classified task in one place"
```

---

## Task 4: Config, state and the `classify` phase

**Files:**
- Modify: `scripts/goatcode/run.py` (`PHASES` at line 41, `DEFAULT_CONFIG` at line 61)
- Modify: `templates/config.yaml`
- Test: `scripts/tests/test_run.py`

**Interfaces:**
- Consumes: `workflow.select`, `workflow.wants_*` from Task 3.
- Produces, on `Run`:
  - `wants_classification() -> bool` — the `classifier.enabled` config, default `True`.
  - `classification` (property) `-> dict | None` — `state["classification"]`.
  - `workflow` (property) `-> str` — the stored workflow, or `PLANNED_DEVELOPMENT` when classification is off or absent, which is exactly today's behaviour.
  - `set_classification(final: dict, selected: str)` — persists both and saves.

- [ ] **Step 1: Write the failing tests**

```python
# append to scripts/tests/test_run.py

# -- classification ---------------------------------------------------------


def test_classify_is_a_phase(git_repo):
    run = make_run(git_repo)
    run.set_phase("classify")
    assert run.phase == "classify"


def test_classification_is_on_by_default(git_repo):
    assert make_run(git_repo).wants_classification() is True


def test_classification_can_be_switched_off(git_repo):
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "classifier:\n  enabled: false\n")
    run = Run.create(git_repo, "x", "chat")
    assert run.wants_classification() is False


def test_an_unclassified_run_behaves_as_it_does_today(git_repo):
    """The whole backward-compatibility guarantee, in one assertion."""
    run = make_run(git_repo)
    assert run.classification is None
    assert run.workflow == "PLANNED_DEVELOPMENT"


def test_a_recorded_classification_is_read_back(git_repo):
    run = make_run(git_repo)
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    reloaded = Run.load(git_repo)
    assert reloaded.classification["complexity"] == "SIMPLE"
    assert reloaded.workflow == "DIRECT_DEVELOPMENT"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_run.py -q -k "classif or classify_is_a_phase"`
Expected: FAIL — `RunError: unknown phase 'classify'`, then `AttributeError: 'Run' object has no attribute 'wants_classification'`

- [ ] **Step 3: Write the minimal implementation**

In `scripts/goatcode/run.py`, add `"classify"` to `PHASES` immediately after `"init"`:

```python
PHASES = (
    "init",
    "classify",
    "grill",
    "ask",
    "plan",
    "approve",
    "execute",
    "synthesize",
    "verify",
    "e2e",
    "record",
    "replan",
    "done",
    "failed",
    "aborted",
)
```

Add to `DEFAULT_CONFIG`, immediately after the `"debug": False,` entry:

```python
    # Classify a run's complexity and risk before planning, and route it to a
    # cheaper or heavier workflow. Off means every run takes the full
    # pipeline, exactly as it did before this existed.
    "classifier": {
        "enabled": True,
        "model": "haiku",
    },
```

No `timeout_seconds` or `max_tokens`, though the spec's YAML sketch shows them: nothing in this codebase would read them. Dispatch timeouts belong to whatever performs the dispatch — the Claude Code session or `agentcli` — and a config key that no code consults is a promise the tool does not keep. Add them when there is an enforcement point.

Add these methods to `Run`, next to `wants_e2e` (around line 659):

```python
    def wants_classification(self):
        return bool((self.config.get("classifier") or {}).get("enabled", True))

    @property
    def classification(self):
        """The final classification, once one has been recorded."""
        return self.state.get("classification")

    @property
    def workflow(self):
        """Which pipeline this run gets.

        Absent classification means the full pipeline: that is what every run
        did before the classifier existed, and turning it off must not
        quietly buy less verification.
        """
        return self.state.get("workflow") or "PLANNED_DEVELOPMENT"

    def set_classification(self, final, selected):
        self.state["classification"] = dict(final)
        self.state["workflow"] = selected
        self.save()
```

In `templates/config.yaml`, add before the `# Model per role.` block:

```yaml
# Classify each run's complexity and risk before planning, and route it:
#   SIMPLE / NORMAL   - straight to the executors, judged by the gates
#   COMPLEX           - grill, approval gate, and a verifier verdict
#   HIGH_RISK         - all of that, plus your sign-off before it finishes
#
# The classifier is advisory. Deterministic rules may raise a run's risk -
# anything touching auth, crypto, secrets, CI, infra or migrations - and can
# never lower it, so a wrong answer from the model cannot buy less scrutiny.
#
# enabled: false makes every run take the full pipeline, exactly as it did
# before this existed.
classifier:
  enabled: true
  model: haiku
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_run.py scripts/tests/test_plugin.py -q`
Expected: PASS. `test_config_template_parses_and_matches_the_defaults` proves the template and `DEFAULT_CONFIG` agree.

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/run.py templates/config.yaml scripts/tests/test_run.py
git commit -m "add the classify phase and its config"
```

---

## Task 5: The classifier agent and its dispatch prompt

**Files:**
- Create: `agents/goat-code-classifier.md`
- Modify: `scripts/goatcode/dispatch.py`
- Test: `scripts/tests/test_dispatch.py`

**Interfaces:**
- Consumes: `run.spec_path`, `run.stack_path`, `dispatch.command` (existing).
- Produces: `dispatch.classifier(run) -> str` — the rendered prompt. Writes to `cycle-1/dispatch/classifier.md` via the existing `dispatch.write(run, "classifier", text)`.
- The agent writes `classification.json` to `run.root / "classification.json"` and then runs `goatcode classify --file <that path>`.

- [ ] **Step 1: Write the failing tests**

```python
# append to scripts/tests/test_dispatch.py


# -- classifier -------------------------------------------------------------


def test_the_classifier_prompt_names_its_inputs_and_output(run):
    text = dispatch.classifier(run)
    assert str(run.spec_path) in text
    assert str(run.root / "classification.json") in text
    assert "classify" in text


def test_the_classifier_prompt_carries_the_exact_schema(run):
    text = dispatch.classifier(run)
    for field in ("complexity", "risk", "riskFactors", "complexityFactors", "reasoning"):
        assert field in text
    assert "SIMPLE" in text and "CRITICAL" in text


def test_the_classifier_prompt_does_not_inline_the_repository(run):
    """Cost control: the classifier reads metadata, not the codebase."""
    text = dispatch.classifier(run)
    assert "do not read the whole repository" in text.lower()


def test_the_classifier_prompt_says_it_is_advisory(run):
    text = dispatch.classifier(run)
    assert "advisory" in text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_dispatch.py -q -k classifier`
Expected: FAIL — `AttributeError: module 'goatcode.dispatch' has no attribute 'classifier'`

- [ ] **Step 3: Write the minimal implementation**

Add to `scripts/goatcode/dispatch.py`, after `questions_path`:

```python
# --------------------------------------------------------------------------
# classifier
# --------------------------------------------------------------------------


def classifier(run):
    """Prompt for the agent that sizes a run before anything is planned."""
    lines = []
    add = lines.append
    target = run.root / "classification.json"

    add("# Classifier dispatch - {}".format(run.run_id))
    add("")
    add("Decide how much machinery this task deserves, before any of it runs.")
    add("")
    add("## Read only these")
    add("")
    add("- The request: `{}`".format(run.spec_path))
    add("- The detected stack: `{}`".format(run.stack_path))
    add("")
    add("**Do not read the whole repository.** You are sizing the request, not")
    add("solving it. Look at a file only when the request names one and you")
    add("genuinely cannot judge its blast radius without it.")
    add("")
    add("## Judge two independent things")
    add("")
    add("**complexity** - how much work this is:")
    add("`SIMPLE` one file, one obvious change; `NORMAL` a few files in one")
    add("module; `COMPLEX` several modules, architectural or API changes,")
    add("schema changes, concurrency, or requirements you cannot pin down.")
    add("")
    add("**risk** - what it could break:")
    add("`LOW` `MEDIUM` `HIGH` `CRITICAL`. Raise it for authentication,")
    add("authorization, cryptography, secrets, CI/CD, infrastructure,")
    add("dependency changes, migrations, data deletion, permissions, or")
    add("anything touching customer data.")
    add("")
    add("These are independent. A one-file change to a login check is SIMPLE")
    add("and HIGH, and that combination is the point of asking twice.")
    add("")
    add("## Write exactly this JSON to")
    add("")
    add("    {}".format(target))
    add("")
    add("```json")
    add("{")
    add('  "complexity": "SIMPLE | NORMAL | COMPLEX",')
    add('  "risk": "LOW | MEDIUM | HIGH | CRITICAL",')
    add('  "riskFactors": ["authentication"],')
    add('  "complexityFactors": ["multiple modules"],')
    add('  "reasoning": "One or two sentences on why."')
    add("}")
    add("```")
    add("")
    add("Then run:")
    add("")
    add("    {}".format(command(run, "classify", "--file", str(target))))
    add("")
    add("Your answer is **advisory**. Deterministic rules run over the same")
    add("request and may raise the risk you give - they never lower it - so")
    add("guessing HIGH to be safe buys nothing and costs the run a slower")
    add("pipeline. Say what you actually think.")
    add("")
    add("Return one line: the complexity and risk you chose.")
    return "\n".join(lines)
```

Create `agents/goat-code-classifier.md`:

```markdown
---
name: goat-code-classifier
description: >
  Sizes one goat-code run before anything is planned: how complex the task
  is, and how risky. Reads the request and the detected stack, never the
  whole repository. Advisory only - deterministic rules can raise the risk
  it reports and never lower it. Use only in the classify phase.
tools: [Read, Grep, Glob, Bash, Write]
model: haiku
---

You decide how much machinery one task deserves, before any of it runs.

Read `goat-code:goat-code-conventions` for the shared contracts.

Your dispatch names the request, the detected stack, the exact JSON to
write, and the command that records it. Follow it exactly.

## What you are judging

Two independent things, and keeping them independent is the job:

- **complexity** is how much work this is - files, modules, architectural
  reach, how clear the requirements are.
- **risk** is what it could break - authentication, authorization,
  cryptography, secrets, CI/CD, infrastructure, dependencies, migrations,
  data deletion, permissions, customer data.

A one-file change to a login check is `SIMPLE` and `HIGH`. Reporting it as
`SIMPLE`/`LOW` because it is small, or `COMPLEX`/`HIGH` because it is scary,
both lose the distinction the pipeline routes on.

## Cost discipline

You run on the cheapest model in the pipeline, on purpose, and you run
before anything else. Read the request and the stack profile. Do not explore
the repository to satisfy curiosity - if the request names a file and its
blast radius is genuinely unclear, read that file, and stop there.

## You are advisory

Deterministic rules evaluate the same request and take the higher risk of
the two. You cannot talk risk down, so there is nothing to gain by inflating
it: an honest `LOW` that the rules raise to `HIGH` costs the run nothing,
while a defensive `HIGH` on a trivial task spends a planner, a verifier and
a human's attention that the task did not need.

If you genuinely cannot tell, say `NORMAL` and `MEDIUM` and explain why in
`reasoning`. That is what the fallback would have chosen anyway.

## Report

Write the JSON your dispatch specifies, run the `classify` command it gives
you, then return one line - the complexity and risk you chose. Nothing else;
anything you print stays in the orchestrator's context for the whole run.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_dispatch.py scripts/tests/test_plugin.py -q`
Expected: PASS. `test_every_referenced_name_exists` proves the new agent name resolves; `test_config_defaults_agree_with_the_agent_files` proves the frontmatter model matches the config.

- [ ] **Step 5: Commit**

```bash
git add agents/goat-code-classifier.md scripts/goatcode/dispatch.py scripts/tests/test_dispatch.py
git commit -m "add the classifier agent and its prompt"
```

---

## Task 6: Recording a classification, with the fallback

**Files:**
- Modify: `scripts/goatcode/report.py`
- Modify: `scripts/goatcode.py`
- Test: `scripts/tests/test_cli_reporting.py`

**Interfaces:**
- Consumes: `classify.parse`, `classify.apply_rules`, `classify.FALLBACK` (Tasks 1–2); `workflow.select` (Task 3); `run.set_classification` (Task 4).
- Produces:
  - `report.record_classification(run, payload=None, reason=None) -> dict` with keys `classification`, `workflow`, `fallback`. `payload` is the agent's raw dict; `None` (or an invalid one) takes the fallback and records `reason`.
  - CLI: `goatcode classify --file <path>` and `goatcode classify --fallback "<reason>"`.

- [ ] **Step 1: Write the failing tests**

```python
# append to scripts/tests/test_cli_reporting.py


# -- classification ---------------------------------------------------------


def _classification(**overrides):
    base = {
        "complexity": "SIMPLE",
        "risk": "LOW",
        "reasoning": "One label change.",
        "riskFactors": [],
        "complexityFactors": [],
    }
    base.update(overrides)
    return base


def test_a_valid_classification_selects_a_workflow(capsys, node_repo):
    run = start(capsys, node_repo)
    result = report.record_classification(run, _classification())
    assert result["workflow"] == "DIRECT_DEVELOPMENT"
    assert Run.load(node_repo).workflow == "DIRECT_DEVELOPMENT"


def test_the_rules_escalate_what_the_model_said(capsys, node_repo):
    run = start(capsys, node_repo)
    osenv.write_text(run.spec_path, "Rework the login token expiry.\n")
    result = report.record_classification(run, _classification())
    assert result["classification"]["risk"] == "HIGH"
    assert result["workflow"] == "HIGH_RISK_DEVELOPMENT"
    assert "authentication" in result["classification"]["deterministic_overrides"]


def test_a_malformed_classification_falls_back_conservatively(capsys, node_repo):
    run = start(capsys, node_repo)
    result = report.record_classification(run, {"complexity": "TRIVIAL"})
    assert result["fallback"]
    assert result["classification"]["complexity"] == "NORMAL"
    assert result["workflow"] == "PLANNED_DEVELOPMENT"


def test_a_fallback_still_gets_the_high_risk_path_when_the_rules_say_so(capsys, node_repo):
    """The spec's hard requirement: no classifier, still no shortcut."""
    run = start(capsys, node_repo)
    osenv.write_text(run.spec_path, "Rotate the production signing secret.\n")
    result = report.record_classification(run, None, reason="timeout")
    assert result["workflow"] == "HIGH_RISK_DEVELOPMENT"


def test_the_classification_is_written_to_the_ledger(capsys, node_repo):
    run = start(capsys, node_repo)
    report.record_classification(run, _classification())
    assert any("classified SIMPLE/LOW" in e for e in ledger.entries(run))


def test_the_ledger_records_a_deterministic_override(capsys, node_repo):
    run = start(capsys, node_repo)
    osenv.write_text(run.spec_path, "Change the auth middleware.\n")
    report.record_classification(run, _classification())
    assert any("override" in e and "authentication" in e for e in ledger.entries(run))


def test_the_cli_records_a_classification_from_a_file(capsys, node_repo):
    run = start(capsys, node_repo)
    target = run.root / "classification.json"
    osenv.write_json(target, _classification())

    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "classify", "--file", str(target)
    )
    assert code == 0
    assert payload["workflow"] == "DIRECT_DEVELOPMENT"


def test_the_cli_falls_back_when_the_file_is_not_json(capsys, node_repo):
    run = start(capsys, node_repo)
    target = run.root / "classification.json"
    osenv.write_text(target, "not json at all")

    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "classify", "--file", str(target)
    )
    assert code == 0, "a broken classifier must not stop the run"
    assert payload["fallback"]


def test_the_cli_takes_an_explicit_fallback(capsys, node_repo):
    start(capsys, node_repo)
    code, payload, _err = invoke_json(
        capsys, "--repo", str(node_repo), "classify", "--fallback", "provider timeout"
    )
    assert code == 0
    assert payload["fallback"] == "provider timeout"
```

Add `ledger` and `report` to the imports at the top of `scripts/tests/test_cli_reporting.py`:

```python
from goatcode import ledger, osenv, report, worktree
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_cli_reporting.py -q -k "classif or fallback"`
Expected: FAIL — `AttributeError: module 'goatcode.report' has no attribute 'record_classification'`

- [ ] **Step 3: Write the minimal implementation**

Add to `scripts/goatcode/report.py`, after `record_role`:

```python
def record_classification(run, payload=None, reason=None):
    """Record how a run was sized, and which workflow that earns it.

    Never raises. A classifier that timed out, returned prose, or invented an
    enum must not stop the run - it costs the run its cheaper path, which is
    the safe direction to fail in.
    """
    from . import classify, workflow as workflowmod

    fallback = reason
    if payload is None:
        advisory = dict(classify.FALLBACK)
        fallback = fallback or "no classification produced"
    else:
        try:
            advisory = classify.parse(payload)
        except classify.ClassifyError as exc:
            advisory = dict(classify.FALLBACK)
            fallback = "invalid classification: {}".format(exc)

    spec = osenv.read_text(run.spec_path) if pathlib.Path(run.spec_path).exists() else ""
    final = classify.apply_rules(advisory, spec, [])
    # The spec's audit fields: which rules and which model produced this, so a
    # routing decision stays explicable after either has changed.
    final["classifier_version"] = classify.VERSION
    final["model"] = (run.config.get("classifier") or {}).get("model")
    final["fallback_reason"] = fallback
    selected = workflowmod.select(final)
    run.set_classification(final, selected)

    detail = "classified {}/{} -> {}".format(final["complexity"], final["risk"], selected)
    overrides = final.get("deterministic_overrides") or []
    if overrides:
        detail += "; deterministic override: {}".format(", ".join(overrides))
    if fallback:
        detail += "; fallback: {}".format(fallback)
    ledger.append(run, detail)

    return {"classification": final, "workflow": selected, "fallback": fallback}
```

Add `"classifier": ("DONE", "FAILED")` to `ROLE_STATUSES` so a driver can report the role failing:

```python
ROLE_STATUSES = {
    "classifier": ("DONE", "FAILED"),
    "synthesizer": ("CLEAN", "ESCALATE"),
    "e2e": ("PASS", "SKIPPED", "FAILED"),
    "scribe": ("WRITTEN", "SKIPPED"),
}
```

Add the command to `scripts/goatcode.py`, next to `cmd_report`:

```python
def cmd_classify(args):
    """Record how this run was sized. Never fails the run."""
    run = resolve_run(args)
    payload = None
    reason = args.fallback
    if args.file and not reason:
        path = pathlib.Path(args.file)
        if not path.exists():
            reason = "no classification file at {}".format(path)
        else:
            try:
                payload = json.loads(osenv.read_text(path))
            except ValueError as exc:
                reason = "classification file is not JSON: {}".format(exc)
    result = reportmod.record_classification(run, payload, reason=reason)
    text = "{}/{} -> {}".format(
        result["classification"]["complexity"],
        result["classification"]["risk"],
        result["workflow"],
    )
    if result["fallback"]:
        text += "  (fallback: {})".format(result["fallback"])
    emit(args, result, text)
    return EXIT_OK
```

And its parser, next to the `report` parser:

```python
    p = add(sub, "classify", help="record how this run was sized, and route it")
    p.add_argument("--file", help="the classification JSON the classifier wrote")
    p.add_argument("--fallback", help="why classification could not be produced")
    p.set_defaults(func=cmd_classify)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_cli_reporting.py scripts/tests/test_plugin.py -q`
Expected: PASS. `test_every_referenced_cli_command_exists` proves the dispatch's `classify` command is real.

- [ ] **Step 5: Commit**

```bash
git add scripts/goatcode/report.py scripts/goatcode.py scripts/tests/test_cli_reporting.py
git commit -m "record a classification, and fall back safely when there is none"
```

---

## Task 7: Wire the phase into the machine

**Files:**
- Modify: `scripts/goatcode/machine.py` (`derive_phase` at line 82, handler map at line 169)
- Test: `scripts/tests/test_machine.py`

**Interfaces:**
- Consumes: `run.wants_classification()`, `run.classification`, `run.workflow` (Task 4); `dispatch.classifier` (Task 5); `workflow.wants_*` (Task 3).
- Produces: `machine._classify(run, evidence, stack)` returning a `dispatch` action for `goat-code-classifier`.

- [ ] **Step 1: Write the failing tests**

```python
# append to scripts/tests/test_machine.py


# -- classification ---------------------------------------------------------


def test_a_fresh_run_classifies_before_it_grills(run):
    assert machine.derive_phase(run) == "classify"


def test_classification_dispatches_the_classifier_on_the_cheap_model(run):
    action = machine.next_action(run)
    assert action["action"] == "dispatch"
    assert action["dispatches"][0]["agent"] == "goat-code-classifier"
    assert action["dispatches"][0]["model"] == "haiku"


def test_a_classified_run_moves_on_to_grill(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT")
    assert machine.derive_phase(run) == "grill"


def test_a_direct_workflow_skips_the_grill_entirely(run):
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    assert machine.derive_phase(run) == "plan"


def test_a_direct_workflow_needs_no_approval(run):
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(run)
    assert machine.derive_phase(run) == "execute", "no approval gate on a direct run"


def test_a_planned_workflow_still_gates(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT")
    write_plan(run)
    assert machine.derive_phase(run) == "approve"


def test_classification_switched_off_behaves_exactly_as_before(git_repo):
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "classifier:\n  enabled: false\n")
    created = Run.create(git_repo, "magic link", "chat")
    created.set_phase("grill")
    assert machine.derive_phase(created) == "grill"


def test_a_direct_workflow_is_done_on_green_gates_without_a_verifier(run):
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}, "regressions": []})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")
    finished(run)

    assert machine.derive_phase(run) == "done", "gates alone decide a direct run"


def test_a_direct_workflow_fails_rather_than_replanning_on_a_red_gate(run):
    run.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}, "regressions": ["test"]})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")

    assert machine.derive_phase(run) == "failed"


def test_a_planned_workflow_still_dispatches_the_verifier(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "LOW"}, "PLANNED_DEVELOPMENT")
    write_plan(run)
    approved(run)
    for slice_id in ("S1", "S2", "S3"):
        tasks.set_status(run.tasks_path, slice_id, "done")
    run.state["merge"] = {"status": "clean", "worktree": "w", "merged": [], "pending": []}
    run.save()
    osenv.write_json(run.cycle_dir() / "gates.json", {"gates": {}})
    (run.cycle_dir() / "review.diff").write_text("diff", encoding="utf-8")

    action = machine.next_action(run)
    assert action["dispatches"][0]["agent"] == "goat-code-verifier"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_machine.py -q -k "classif or direct_workflow or planned_workflow"`
Expected: FAIL — the first asserts `classify` and gets `grill`.

- [ ] **Step 3: Write the minimal implementation**

In `scripts/goatcode/machine.py`, import the new modules on the existing `from . import` line:

```python
from . import debuglog, diffpkg, dispatch, gates, ledger, merge, miniyaml, osenv, progress, report, schema, tasks, workflow as workflowmod
```

In `derive_phase`, insert the classify check immediately after the terminal-phase check:

```python
    if run.phase in ("done", "failed", "aborted"):
        return run.phase

    # Sizing the task comes before deciding how much pipeline it gets.
    if run.wants_classification() and run.classification is None:
        return "classify"
```

Replace the plan/approval block so routing decides the grill and the gate:

```python
    if not evidence.has_plan:
        if not workflowmod.wants_grill(run.workflow):
            # A direct run gets a plan without a round of questions: the
            # planner still writes tasks.yaml, it just does not stop to ask.
            return "plan"
        return "ask" if evidence.questions.exists() else "grill"

    if not evidence.plan_valid:
        return "plan"

    if run.approval == "revise":
        return "grill"

    if workflowmod.wants_gate(run.workflow) and run.needs_approval():
        return "approve"
```

Replace the verdict block so a direct run is judged by gates alone:

```python
    if not workflowmod.wants_verifier(run.workflow):
        if evidence.merge_state.get("status") in ("clean", "empty"):
            report_path = run.cycle_dir() / "gates.json"
            if report_path.exists():
                blocking = gates.blocking(osenv.read_json(report_path) or {})
                if blocking:
                    # No replan on a direct run: it was routed here because it
                    # was small, and grinding a cycle budget on it is the cost
                    # the routing exists to avoid.
                    return "failed"
                if run.wants_e2e(evidence.doc) and not evidence.e2e.get("status"):
                    return "e2e"
                if run.wants_progress() and not evidence.progress.get("status"):
                    return "record"
                return "done"

    if evidence.verdict == "PASS":
```

The existing `verdict == "PASS"` block and everything below it stays exactly as it is — the new block above it returns before reaching them on a direct run, and falls through untouched on every other.

Add the handler to the map in `next_action`:

```python
    handler = {
        "classify": _classify,
        "grill": _grill,
        "ask": _ask,
        "plan": _plan,
        "approve": _approve,
        "execute": _execute,
        "synthesize": _synthesize,
        "verify": _verify,
        "e2e": _e2e,
        "record": _record,
        "replan": _replan,
    }.get(phase)
```

And the handler itself, before `_grill`:

```python
# -- classify ---------------------------------------------------------------


def _classify(run, _evidence, _stack):
    text = dispatch.classifier(run)
    path = dispatch.write(run, "classifier", text)
    return _action(
        run,
        "dispatch",
        "size the task before deciding how much pipeline it gets",
        "dispatch goat-code-classifier ({})".format(_model(run, "classifier")),
        dispatches=[_entry("goat-code-classifier", _model(run, "classifier"), path)],
    )
```

Add `"classifier": "haiku"` to the `models` map in `DEFAULT_CONFIG` (`scripts/goatcode/run.py`) and to `templates/config.yaml`'s `models:` block, so `_model(run, "classifier")` resolves and the plugin test stays green.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_machine.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite and fix regressions**

Run: `python -m pytest scripts/tests -q`
Expected: PASS. `test_pipeline_e2e.py` drives the real loop; if its fake agent now stalls at `classify`, teach it the phase in Task 8 rather than weakening the machine.

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/machine.py scripts/goatcode/run.py templates/config.yaml scripts/tests/test_machine.py
git commit -m "route each run by how it was classified"
```

---

## Task 8: End-to-end, with no model in the loop

**Files:**
- Modify: `scripts/tests/test_pipeline_e2e.py`

**Interfaces:**
- Consumes: everything above. The fake agent gains a `classifier` method, mirroring what the real one does.

- [ ] **Step 1: Write the failing tests**

In `scripts/tests/test_pipeline_e2e.py`, add to `FakeAgent` alongside `planner`:

```python
    def classifier(self, run, _dispatch):
        """What the real classifier does: write the JSON, run the command."""
        target = run.root / "classification.json"
        osenv.write_json(target, {
            "complexity": self.complexity,
            "risk": self.risk,
            "reasoning": "fixture",
            "riskFactors": [],
            "complexityFactors": [],
        })
        self.cli(run, "classify", "--file", str(target))
```

Add `complexity="COMPLEX"` and `risk="LOW"` parameters to `FakeAgent.__init__`, defaulting so every existing test keeps the full pipeline it asserts today.

Then add these tests:

```python
def test_a_simple_run_reaches_done_without_planner_questions_or_a_verifier(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], complexity="SIMPLE", risk="LOW")
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    dispatched = [entry["agent"] for entry in agent.dispatched]
    assert "goat-code-classifier" in dispatched
    assert "goat-code-verifier" not in dispatched


def test_a_complex_run_still_gets_the_whole_pipeline(started):
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], complexity="COMPLEX", risk="LOW")
    driver = make_driver(started, agent)
    final = driver.loop()

    assert final["outcome"] == "done"
    dispatched = [entry["agent"] for entry in agent.dispatched]
    assert "goat-code-verifier" in dispatched


def test_a_high_risk_run_is_routed_by_the_rules_not_the_model(started):
    """The model says SIMPLE/LOW; the spec text says authentication."""
    run = Run.load(started)
    osenv.write_text(run.spec_path, "Change the login token expiry.\n")
    agent = FakeAgent([slice_doc("S1", "src/s1/**")], complexity="SIMPLE", risk="LOW")
    driver = make_driver(started, agent)
    driver.loop()

    reloaded = Run.load(started)
    assert reloaded.workflow == "HIGH_RISK_DEVELOPMENT"
    assert "goat-code-verifier" in [entry["agent"] for entry in agent.dispatched]


def test_a_classifier_that_writes_nothing_falls_back_and_still_finishes(started):
    class Silent(FakeAgent):
        def classifier(self, run, _dispatch):
            self.cli(run, "classify", "--fallback", "fixture: no answer")

    agent = Silent([slice_doc("S1", "src/s1/**")])
    final = make_driver(started, agent).loop()

    assert final["outcome"] == "done"
    assert Run.load(started).workflow == "PLANNED_DEVELOPMENT"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_pipeline_e2e.py -q -k "simple_run or complex_run or high_risk_run or writes_nothing"`
Expected: FAIL — the driver has no `classifier` backend method wired.

- [ ] **Step 3: Wire the fake agent's dispatch table**

`driver.py` needs no change — its backend is one callable that takes any dispatch entry. The routing that needs the new name is the fixture's, in `FakeAgent` (around line 190):

```python
            "goat-code-planner": self.planner,
            "goat-code-classifier": self.classifier,
            "goat-code-scribe": self.scribe,
            "goat-code-replanner": self.replanner,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_pipeline_e2e.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest scripts/tests -q`
Expected: PASS, all 973 existing tests plus the new ones.

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/driver.py scripts/tests/test_pipeline_e2e.py
git commit -m "drive a classified run end to end with no model"
```

---

## Task 9: Document why the classifier cannot be trusted

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/PIPELINE.md`
- Modify: `skills/goat-code-conventions/SKILL.md`

- [ ] **Step 1: Add the architecture section**

Add to `docs/ARCHITECTURE.md`, after "Two halves: script and judgement":

```markdown
## The classifier is advisory, the rules are not

Sizing a task is a judgement call, so a model makes it. Acting on that
judgement is a security decision, so a model does not.

`goat-code-classifier` reads the request and returns a complexity and a
risk. `classify.apply_rules` then evaluates the same request against
deterministic patterns - authentication, cryptography, secrets, CI, infra,
migrations, data deletion - and takes the **higher** of the two risks. It
can raise what the model said. It can never lower it.

That asymmetry is the whole design. A model that hallucinates `LOW` on an
authentication change loses the pipeline nothing, because the rules raise it
back. A model that is unavailable, times out, or returns prose loses nothing
either: the fallback is `NORMAL`/`MEDIUM`, and the rules still run over the
spec text. There is no input to the classifier that buys a task less
scrutiny than the rules demand.

The rules run twice, because they see different evidence each time. At
classify time only the request exists. Once a plan exists, the same rules
run again over the paths its slices claim, so a plan that reaches into
`src/auth/**` escalates a run whose description never said so. The second
pass may only raise.

Routing lives in `workflow.py` and nowhere else. Four predicates - grill,
gate, verifier, approval - are the only questions the machine asks about a
workflow, so what a classification actually costs a run is readable in one
sitting rather than inferred from conditionals spread across the phases.
```

- [ ] **Step 2: Add the pipeline walkthrough**

Add to `docs/PIPELINE.md`, between `init` and `grill`:

```markdown
## `classify` → size the task

Unless `classifier.enabled` is false, the machine dispatches
`goat-code-classifier` on the cheapest configured model before anything else
runs. It reads `spec.md` and `stack.json` - not the repository - and writes
`classification.json`, then runs:

```bash
python scripts/goatcode.py classify --file .goatcode/runs/<id>/classification.json
```

That command validates the JSON, merges it with the deterministic risk
rules, records the result in `state.json` and the ledger, and selects one of
three workflows:

| Workflow | Grill | Approval gate | Verifier | Human sign-off |
| --- | --- | --- | --- | --- |
| `DIRECT_DEVELOPMENT` | no | no | no - gates decide | no |
| `PLANNED_DEVELOPMENT` | yes | yes | yes | no |
| `HIGH_RISK_DEVELOPMENT` | yes | yes | yes | yes |

A `DIRECT_DEVELOPMENT` run still gets a plan - the executors need briefs,
ownership and acceptance criteria - it just skips the questions and the
gate, and a red gate fails it rather than opening a replan cycle.

Invalid JSON, an unknown enum, a timeout or no answer at all all take the
same conservative fallback: `NORMAL`/`MEDIUM`, which routes to
`PLANNED_DEVELOPMENT`. The deterministic rules still run, so a fallback on a
task that touches authentication still lands in `HIGH_RISK_DEVELOPMENT`.
```

- [ ] **Step 3: Add the conventions entries**

Add the CLI row to the table in `skills/goat-code-conventions/SKILL.md`:

```markdown
| `classify --file F` / `classify --fallback "why"` | record how a run was sized, and route it |
```

And add `classification.json` to the run-directory listing, next to `stack.json`.

- [ ] **Step 4: Run the plugin tests**

Run: `python -m pytest scripts/tests/test_plugin.py -q`
Expected: PASS — `test_every_referenced_cli_command_exists` and `test_every_referenced_name_exists` check the docs against reality.

- [ ] **Step 5: Commit**

```bash
git add docs/ARCHITECTURE.md docs/PIPELINE.md skills/goat-code-conventions/SKILL.md
git commit -m "document why the classifier cannot be trusted"
```

---

## Task 10: Re-evaluate risk once the plan exists

**Files:**
- Modify: `scripts/goatcode/machine.py`
- Modify: `scripts/goatcode/report.py`
- Test: `scripts/tests/test_machine.py`

**Interfaces:**
- Consumes: `classify.apply_rules` (Task 2), `workflow.select` (Task 3), `run.set_classification` (Task 4).
- Produces: `report.reassess_classification(run, doc) -> dict | None` — returns the new classification when the plan's globs raised the risk, `None` when nothing changed.

- [ ] **Step 1: Write the failing tests**

```python
# append to scripts/tests/test_machine.py


def test_a_plan_that_reaches_into_sensitive_paths_escalates_the_run(run):
    """The second deterministic pass: the spec was innocent, the plan is not."""
    run.set_classification(
        {"complexity": "SIMPLE", "risk": "LOW", "deterministic_overrides": []},
        "DIRECT_DEVELOPMENT",
    )
    doc = copy.deepcopy(PLAN)
    doc["slices"][0]["owns"] = ["src/auth/**"]
    write_plan(run, doc)

    machine.next_action(run)
    reloaded = Run.load(run.repo)
    assert reloaded.classification["risk"] == "HIGH"
    assert reloaded.workflow == "HIGH_RISK_DEVELOPMENT"


def test_the_second_pass_never_lowers_a_run(run):
    run.set_classification(
        {"complexity": "COMPLEX", "risk": "CRITICAL", "deterministic_overrides": []},
        "HIGH_RISK_DEVELOPMENT",
    )
    write_plan(run)

    machine.next_action(run)
    reloaded = Run.load(run.repo)
    assert reloaded.workflow == "HIGH_RISK_DEVELOPMENT"


def test_the_escalation_is_recorded_in_the_ledger(run):
    run.set_classification(
        {"complexity": "SIMPLE", "risk": "LOW", "deterministic_overrides": []},
        "DIRECT_DEVELOPMENT",
    )
    doc = copy.deepcopy(PLAN)
    doc["slices"][0]["owns"] = [".github/workflows/**"]
    write_plan(run, doc)

    machine.next_action(run)
    assert any("re-classified" in e for e in ledger.entries(run))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_machine.py -q -k "sensitive_paths or never_lowers or escalation_is_recorded"`
Expected: FAIL — the run stays `DIRECT_DEVELOPMENT`.

- [ ] **Step 3: Write the minimal implementation**

Add to `scripts/goatcode/report.py`:

```python
def reassess_classification(run, doc):
    """Run the deterministic rules again, now that the plan names paths.

    At classify time only the request existed. A plan that claims
    `src/auth/**` is evidence the request never carried, so the same rules
    see it now. Escalate-only: this can cost a run its cheap path and can
    never buy one.
    """
    from . import classify, workflow as workflowmod

    current = run.classification
    if not current or not doc:
        return None

    paths = []
    for item in tasks.slices(doc):
        paths.extend(item.get("owns") or [])
        paths.extend(item.get("touches_shared") or [])

    spec = osenv.read_text(run.spec_path) if pathlib.Path(run.spec_path).exists() else ""
    updated = classify.apply_rules(current, spec, paths)
    if updated["risk"] == current.get("risk"):
        return None

    selected = workflowmod.select(updated)
    run.set_classification(updated, selected)
    ledger.append(
        run,
        "re-classified {}/{} -> {} after the plan claimed {}".format(
            updated["complexity"],
            updated["risk"],
            selected,
            ", ".join(updated.get("deterministic_overrides") or []),
        ),
    )
    return updated
```

In `machine.derive_phase`, call it once the plan is valid, immediately after the `plan_valid` check:

```python
    if not evidence.plan_valid:
        return "plan"

    # The plan names paths the request never did; the rules see them now.
    report.reassess_classification(run, evidence.doc)
```

`derive_phase` is documented as pure, and this writes. Move the call into `next_action` instead, immediately before `derive_phase` is consulted a second time — or, simpler and truer to the existing design, call it from `_plan`'s success path and from `next_action` right after `run.set_phase(phase)`. Prefer the latter:

```python
def next_action(run, stack_profile=None):
    evidence = Evidence(run)
    phase = derive_phase(run, evidence)
    if phase != run.phase:
        run.set_phase(phase)
    if evidence.plan_valid:
        if report.reassess_classification(run, evidence.doc):
            # The workflow changed under us; re-derive with the new routing.
            phase = derive_phase(run, evidence)
            run.set_phase(phase)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_machine.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest scripts/tests -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/machine.py scripts/goatcode/report.py scripts/tests/test_machine.py
git commit -m "escalate a run whose plan reaches somewhere the request did not"
```

---

## Task 11: Human sign-off on a high-risk run

**Files:**
- Modify: `scripts/goatcode/run.py`
- Modify: `scripts/goatcode/machine.py`
- Test: `scripts/tests/test_machine.py`

**Interfaces:**
- Consumes: `workflow.wants_gate`, `workflow.wants_approval` (Task 3); `run.workflow` (Task 4).
- Produces: `Run.gate_applies()` gains a classification override; the `stop` action's message carries a sign-off line for `HIGH_RISK_DEVELOPMENT`.

Two distinct approvals, and the spec asks for both: the plan gate before any code is written, and a sign-off before the work is taken. `wants_gate` covers the first but is currently defeated by `approval_gate: never`; `wants_approval` covers the second and is so far defined and unused.

- [ ] **Step 1: Write the failing tests**

```python
# append to scripts/tests/test_machine.py


def test_a_high_risk_run_is_gated_even_when_the_config_says_never(git_repo):
    """Deterministic policy outranks a config that waives review."""
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "approval_gate: never\n")
    created = Run.create(git_repo, "auth change", "chat")
    created.set_classification({"complexity": "SIMPLE", "risk": "HIGH"}, "HIGH_RISK_DEVELOPMENT")
    write_plan(created)

    assert machine.derive_phase(created) == "approve"


def test_a_direct_run_is_still_ungated_when_the_config_says_never(git_repo):
    config = git_repo / ".goatcode" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    osenv.write_text(config, "approval_gate: never\n")
    created = Run.create(git_repo, "label change", "chat")
    created.set_classification({"complexity": "SIMPLE", "risk": "LOW"}, "DIRECT_DEVELOPMENT")
    write_plan(created)

    assert machine.derive_phase(created) == "execute"


def test_a_high_risk_run_asks_for_sign_off_when_it_stops(run):
    run.set_classification({"complexity": "COMPLEX", "risk": "HIGH"}, "HIGH_RISK_DEVELOPMENT")
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)

    action = machine.next_action(run)
    assert action["action"] == "stop"
    assert "sign-off" in action["message"].lower()
    assert "HIGH_RISK" in action["message"]


def test_an_ordinary_run_stops_without_asking_for_sign_off(run):
    write_plan(run)
    approved(run)
    (run.cycle_dir() / "verdict.md").write_text("VERDICT: PASS\n", encoding="utf-8")
    finished(run)

    action = machine.next_action(run)
    assert action["action"] == "stop"
    assert "sign-off" not in action["message"].lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/tests/test_machine.py -q -k "sign_off or says_never"`
Expected: FAIL — the high-risk run derives `execute`, and the stop message has no sign-off line.

- [ ] **Step 3: Write the minimal implementation**

In `scripts/goatcode/run.py`, make `gate_applies` consult the classification:

```python
    def gate_applies(self):
        """Whether this cycle needs the user to approve the plan.

        A high-risk classification forces the gate on regardless of config:
        `approval_gate: never` is the user waiving review for ordinary work,
        not for a change the deterministic rules flagged as touching
        authentication, secrets or production.
        """
        from . import workflow as workflowmod

        if workflowmod.wants_approval(self.workflow) and self.cycle == 1:
            return True

        gate = self.config.get("approval_gate", "chat")
        if gate == "never":
            return False
        if gate == "always":
            return True
        return self.state.get("mode") == "chat" and self.cycle == 1
```

In `scripts/goatcode/machine.py`, add the sign-off line to `_stop`'s message. Find where the done message is assembled and append:

```python
    if workflowmod.wants_approval(run.workflow):
        lines.append("")
        lines.append(
            "This run was classified HIGH_RISK ({}). Review the diff before you"
            " merge it - the pipeline asks for your sign-off rather than"
            " assuming it.".format(
                ", ".join((run.classification or {}).get("risk_factors") or ["risk rules"])
            )
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/tests/test_machine.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest scripts/tests -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/goatcode/run.py scripts/goatcode/machine.py scripts/tests/test_machine.py
git commit -m "ask for sign-off on a run the rules called high risk"
```

---

## Final Validation

- [ ] `python -m pytest scripts/tests -q` — the whole suite, green.
- [ ] Disable and confirm nothing moved: set `classifier: {enabled: false}` in a scratch repo's `.goatcode/config.yaml`, run `goatcode init --prompt "add a thing" --no-baseline`, and confirm `goatcode next --json` returns a `goat-code-planner` dispatch, exactly as it does today.
- [ ] Prove the override by hand: `goatcode init --prompt "change the login token expiry"`, let the classifier report `SIMPLE`/`LOW`, and confirm `state.json` records `HIGH` and `HIGH_RISK_DEVELOPMENT` with `authentication` in `deterministic_overrides`.
- [ ] Prove the fallback: `goatcode classify --fallback "manual test"` and confirm the run lands in `PLANNED_DEVELOPMENT`, not a cheaper one.
- [ ] Confirm the ledger carries the audit fields the spec asks for — classification, workflow, overrides, fallback reason, timestamp — and no file contents.
- [ ] Push and confirm CI is green on all six matrix jobs, including Python 3.9.

---

## Deliberately not built

- **A second LLM client.** The classifier is dispatched through the same `_entry` / `agentcli` path as every other agent. The spec forbids a second client and this repo has exactly one.
- **A `TaskContext` object.** The spec's Java model assumes one; here the run directory already is the context, and `spec.md` plus `stack.json` are what the classifier reads.
- **Separate `CLASSIFYING`/`CLASSIFIED` states.** Phases are derived from files, not stored as transitions, so `classification is None` *is* the classifying state. Adding both would give the machine a second source of truth for one fact.
- **Repository exploration by the classifier.** The spec allows it "only when necessary"; the prompt permits reading a single named file and nothing more. If classification quality turns out to need more, that is a measured change, not a speculative one.
- **A `taskClass` field.** The spec's domain model carries complexity, risk *and* a combined `SIMPLE|NORMAL|COMPLEX|HIGH_RISK` class. That third field is derivable from the first two and would be a second, driftable answer to the same question — `workflow.select` derives the routing directly instead. The spec's own instruction is to adapt rather than introduce these classes blindly.
- **A configurable routing table.** The spec asks for routing "configurable rather than hardcoded throughout the codebase"; the sentence's target is the *throughout*, which `workflow.py` fixes by holding all of it in one readable table. Making the table itself user-editable would let a config file switch the verifier off for high-risk work, which is the one thing the deterministic layer exists to prevent.

---

## Open question for the first executor

`classifier.enabled` defaults to `True`, so this changes the default behaviour of every run: a `SIMPLE`/`NORMAL` task loses its grill, its approval gate and its verifier. That is the point of the feature, but it is a real behaviour change on the first run after merge.

If you would rather land it dark and switch it on deliberately, flip the default to `False` in Task 4 and set `classifier: {enabled: true}` in the target repo's `.goatcode/config.yaml` when you want it. Every test in this plan passes either way — they set the config explicitly rather than relying on the default — except `test_classification_is_on_by_default`, which inverts.
