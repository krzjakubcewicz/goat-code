"""Which pipeline a classified task gets, decided in one place.

The spec's rule, and the reason this is its own module: routing must not be
scattered across the agents. Every phase question the machine asks about a
workflow is one of the four predicates below.
"""

from __future__ import annotations

WORKFLOWS = ("DIRECT_DEVELOPMENT", "PLANNED_DEVELOPMENT", "HIGH_RISK_DEVELOPMENT")

#: Mirrors classify.RISKS / classify.COMPLEXITIES. Kept local on purpose: the
#: router must not import the classifier it routes for, and these are a
#: contract between the two rather than an implementation detail of either.
RISKS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
COMPLEXITIES = ("SIMPLE", "NORMAL", "COMPLEX")

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

    A value this function does not recognise is treated as unreadable, never
    as low. Reading an unknown risk as "not HIGH" would let a malformed
    classification buy the cheapest pipeline, which is the one thing routing
    must never sell.
    """
    risk = str((classification or {}).get("risk", "")).upper()
    complexity = str((classification or {}).get("complexity", "")).upper()

    if risk not in RISKS or complexity not in COMPLEXITIES:
        return "PLANNED_DEVELOPMENT"
    if risk in ("HIGH", "CRITICAL"):
        return "HIGH_RISK_DEVELOPMENT"
    if complexity == "COMPLEX":
        return "PLANNED_DEVELOPMENT"
    return "DIRECT_DEVELOPMENT"


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
