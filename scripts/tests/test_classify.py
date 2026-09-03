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
    assert final["deterministic_overrides"] == ["authentication", "secrets"]
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


@pytest.mark.parametrize(
    "text",
    [
        "secrets are stored in vault",
        "API tokens are rotated daily",
        "private keys are stored securely",
        "rotate the signing secret",
        "backend/.env",
        "the .env file",
    ],
)
def test_the_secrets_rule_matches_the_plurals_of_its_own_vocabulary(text):
    """It carried a trailing \\b and so missed "secrets", "tokens", "keys"."""
    assert "secrets" in classify.evaluate(text, [])["factors"], text


@pytest.mark.parametrize("text", ["tokenizer for the parser", "tokenize the input"])
def test_the_secrets_rule_does_not_escalate_parser_work(text):
    """Over-matching is the preferred direction, but not to the point of
    routing every lexer change through the high-risk pipeline."""
    assert classify.evaluate(text, [])["factors"] == [], text
