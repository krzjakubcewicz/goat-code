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
