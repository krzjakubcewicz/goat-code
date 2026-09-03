"""What kind of task this is, and how much machinery it deserves.

Two independent dimensions. A one-file change to authentication is SIMPLE by
size and HIGH by risk, and it is the risk that decides how much verification
the run gets.

The LLM half of this is advisory. Nothing here trusts it beyond what
``parse`` will accept, and ``apply_rules`` in this module is what makes the
deterministic half authoritative.
"""

from __future__ import annotations

import re

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


#: Deterministic risk rules: (factor, minimum risk, pattern).
#:
#: Matched against the spec's prose and, once a plan exists, against the
#: paths its slices claim. Deliberately broad - a false "this is risky"
#: costs one extra verification pass, a false "this is safe" ships an
#: unreviewed change to authentication.
RULES = (
    ("authentication", "HIGH", r"\b(auth|authentication|authoriz|login|session|oauth|jwt|password|credential)"),
    ("cryptography", "HIGH", r"\b(crypto|encrypt|decrypt|signing|signature|cipher|tls|certificate)"),
    #: Handles plurals (secrets, tokens, keys) and .env. The trailing \b was removed
    #: from the original pattern because it prevented matching plurals; .env is outside
    #: the word-boundary group so it fires after a slash, space, or string start.
    ("secrets", "HIGH", r"\b(secrets?|tokens?|api[_ -]?keys?|private[_ -]?keys?)\b|\.env"),
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
