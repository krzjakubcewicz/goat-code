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
