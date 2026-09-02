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
