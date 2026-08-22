"""The validation gauntlet. Every hard-fail rule gets a test, because each
one exists to prevent a specific expensive failure downstream.
"""

from __future__ import annotations

import copy

import pytest

from codag import schema


def plan(**overrides):
    doc = {
        "version": 1,
        "run_id": "20260822-114900-demo",
        "cycle": 1,
        "goal": "Users sign in with a magic link.",
        "global_constraints": ["Node >= 20"],
        "slices": [
            {
                "id": "S1",
                "title": "Token store",
                "intent": "Persist single-use tokens.",
                "depends_on": [],
                "owns": ["src/auth/tokens/**", "tests/auth/tokens/**"],
                "interfaces": ["createToken(email): Token"],
                "acceptance": [{"id": "A1", "text": "A token is consumable once."}],
                "tests": [{"path": "tests/auth/tokens/store.test.ts", "must_cover": ["single use"]}],
                "status": "pending",
            },
            {
                "id": "S2",
                "title": "Send the email",
                "intent": "Deliver the link.",
                "depends_on": ["S1"],
                "uses_interfaces": ["createToken(email): Token"],
                "owns": ["src/mail/**", "tests/mail/**"],
                "acceptance": [{"id": "A1", "text": "One email per request."}],
                "tests": ["tests/mail/send.test.ts"],
                "status": "pending",
            },
        ],
    }
    doc.update(overrides)
    return copy.deepcopy(doc)


def errors_of(doc):
    return schema.validate(doc).errors


# -- happy path ------------------------------------------------------------


def test_a_good_plan_validates():
    report = schema.validate(plan())
    assert report.ok, report.text()
    assert report.warnings == []
    assert "OK" in report.text()


def test_report_dict_shape():
    assert schema.validate(plan()).as_dict() == {"ok": True, "errors": [], "warnings": []}


# -- structure -------------------------------------------------------------


@pytest.mark.parametrize("key", ["version", "run_id", "cycle", "goal", "slices"])
def test_missing_top_level_key_is_an_error(key):
    doc = plan()
    del doc[key]
    assert any(key in e for e in errors_of(doc))


def test_top_level_must_be_a_mapping():
    assert any("mapping" in e for e in errors_of(["not", "a", "map"]))


def test_unsupported_version_is_rejected():
    assert any("version" in e for e in errors_of(plan(version=2)))


def test_empty_goal_is_rejected():
    assert any("goal" in e for e in errors_of(plan(goal="  ")))


def test_slices_must_be_a_non_empty_list():
    assert any("non-empty list" in e for e in errors_of(plan(slices=[])))


def test_assumptions_must_be_a_list():
    assert any("assumptions" in e for e in errors_of(plan(assumptions="none")))


# -- slice identity --------------------------------------------------------


def test_duplicate_ids_are_rejected():
    doc = plan()
    doc["slices"][1]["id"] = "S1"
    assert any("duplicate slice id" in e for e in errors_of(doc))


def test_bad_id_characters_are_rejected():
    doc = plan()
    doc["slices"][0]["id"] = "1 bad id"
    assert any("must start with a letter" in e for e in errors_of(doc))


def test_missing_id_is_rejected():
    doc = plan()
    del doc["slices"][0]["id"]
    assert any("non-empty 'id'" in e for e in errors_of(doc))


@pytest.mark.parametrize("key", ["title", "owns", "acceptance", "tests"])
def test_missing_required_slice_key(key):
    doc = plan()
    del doc["slices"][0][key]
    assert any(key in e for e in errors_of(doc))


def test_unknown_status_is_rejected():
    doc = plan()
    doc["slices"][0]["status"] = "wandering"
    assert any("status" in e for e in errors_of(doc))


def test_unknown_model_is_rejected():
    doc = plan()
    doc["slices"][0]["model"] = "gpt"
    assert any("model" in e for e in errors_of(doc))


def test_missing_intent_only_warns():
    doc = plan()
    del doc["slices"][0]["intent"]
    report = schema.validate(doc)
    assert report.ok
    assert any("intent" in w for w in report.warnings)


# -- acceptance and tests --------------------------------------------------


def test_zero_acceptance_criteria_is_rejected():
    doc = plan()
    doc["slices"][0]["acceptance"] = []
    assert any("at least one acceptance criterion" in e for e in errors_of(doc))


def test_acceptance_without_text_is_rejected():
    doc = plan()
    doc["slices"][0]["acceptance"] = [{"id": "A1"}]
    assert any("checkable assertion" in e for e in errors_of(doc))


def test_acceptance_without_id_is_rejected():
    doc = plan()
    doc["slices"][0]["acceptance"] = [{"text": "something happens"}]
    assert any("short id" in e for e in errors_of(doc))


def test_duplicate_acceptance_ids_within_a_slice():
    doc = plan()
    doc["slices"][0]["acceptance"] = [
        {"id": "A1", "text": "one"},
        {"id": "A1", "text": "two"},
    ]
    assert any("duplicate acceptance id" in e for e in errors_of(doc))


def test_too_many_acceptance_criteria_warns():
    doc = plan()
    doc["slices"][0]["acceptance"] = [
        {"id": "A{}".format(i), "text": "check {}".format(i)} for i in range(9)
    ]
    report = schema.validate(doc)
    assert report.ok
    assert any("too fat" in w for w in report.warnings)


def test_zero_tests_is_rejected():
    doc = plan()
    doc["slices"][0]["tests"] = []
    assert any("test file path" in e for e in errors_of(doc))


def test_test_entry_without_path_is_rejected():
    doc = plan()
    doc["slices"][0]["tests"] = [{"must_cover": ["x"]}]
    assert any("'path'" in e for e in errors_of(doc))


def test_plain_string_test_paths_are_allowed():
    doc = plan()
    doc["slices"][0]["tests"] = ["tests/a.test.ts"]
    assert schema.validate(doc).ok


# -- dependencies ----------------------------------------------------------


def test_unknown_dependency_is_rejected():
    doc = plan()
    doc["slices"][1]["depends_on"] = ["S9"]
    assert any("unknown slice" in e for e in errors_of(doc))


def test_self_dependency_is_rejected():
    doc = plan()
    doc["slices"][0]["depends_on"] = ["S1"]
    assert any("depends on itself" in e for e in errors_of(doc))


def test_two_slice_cycle_is_rejected():
    doc = plan()
    doc["slices"][0]["depends_on"] = ["S2"]
    assert any("dependency cycle" in e for e in errors_of(doc))


def test_three_slice_cycle_is_rejected():
    doc = plan()
    doc["slices"].append(
        {
            "id": "S3",
            "title": "third",
            "depends_on": ["S2"],
            "owns": ["src/three/**"],
            "acceptance": [{"id": "A1", "text": "x"}],
            "tests": ["tests/three.test.ts"],
        }
    )
    doc["slices"][0]["depends_on"] = ["S3"]
    assert any("dependency cycle" in e for e in errors_of(doc))


def test_waves_are_computed_by_depth():
    layout = schema.waves(plan()["slices"])
    assert layout == [["S1"], ["S2"]]


def test_independent_slices_share_a_wave():
    doc = plan()
    doc["slices"][1]["depends_on"] = []
    doc["slices"][1]["uses_interfaces"] = []
    assert schema.waves(doc["slices"]) == [["S1", "S2"]]


def test_waves_is_empty_when_cyclic():
    doc = plan()
    doc["slices"][0]["depends_on"] = ["S2"]
    assert schema.waves(doc["slices"]) == []


def test_wide_wave_warns():
    doc = plan()
    doc["slices"] = [
        {
            "id": "S{}".format(i),
            "title": "slice {}".format(i),
            "intent": "x",
            "depends_on": [],
            "owns": ["src/mod{}/**".format(i)],
            "acceptance": [{"id": "A1", "text": "x"}],
            "tests": ["tests/mod{}.test.ts".format(i)],
        }
        for i in range(7)
    ]
    report = schema.validate(doc)
    assert report.ok
    assert any("consider splitting the wave" in w for w in report.warnings)


# -- interfaces ------------------------------------------------------------


def test_using_an_unpublished_interface_is_rejected():
    doc = plan()
    doc["slices"][1]["uses_interfaces"] = ["deleteToken(id): void"]
    assert any("no slice provides" in e for e in errors_of(doc))


def test_using_an_interface_without_depending_on_it_is_rejected():
    doc = plan()
    doc["slices"][1]["depends_on"] = []
    assert any("does not depend on it" in e for e in errors_of(doc))


# -- ownership: the rule that makes parallelism safe -----------------------


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("src/auth/**", "src/auth/**", True),
        ("src/auth/**", "src/mail/**", False),
        ("src/**", "src/auth/**", True),
        ("src/auth/**", "src/**", True),
        ("src/auth/tokens.ts", "src/auth/tokens.ts", True),
        ("src/auth/tokens.ts", "src/auth/session.ts", False),
        ("src/auth/**", "src/auth/tokens.ts", True),
        ("src/auth/tokens.ts", "src/mail/**", False),
        ("tests/**/*.test.ts", "tests/**/*.spec.ts", False),
        ("tests/**/*.test.ts", "tests/auth/**", True),
        ("src/*.ts", "src/index.ts", True),
        ("src/*.ts", "src/deep/index.ts", False),
        ("src/db/migrations/", "src/db/migrations/001.sql", True),
        ("docs/**", "src/**", False),
    ],
)
def test_glob_overlap_detection(left, right, expected):
    assert schema.overlaps(left, right) is expected
    assert schema.overlaps(right, left) is expected


def test_windows_separators_are_normalised():
    assert schema.overlaps("src\\auth\\**", "src/auth/tokens.ts")


def test_same_wave_ownership_collision_is_an_error():
    doc = plan()
    doc["slices"][1]["depends_on"] = []
    doc["slices"][1]["uses_interfaces"] = []
    doc["slices"][1]["owns"] = ["src/auth/**"]
    assert any("same wave and both own" in e for e in errors_of(doc))


def test_cross_wave_ownership_overlap_only_warns():
    doc = plan()
    doc["slices"][1]["owns"] = ["src/auth/**"]
    report = schema.validate(doc)
    assert report.ok
    assert any("different waves" in w for w in report.warnings)


def test_empty_owns_is_rejected():
    doc = plan()
    doc["slices"][0]["owns"] = []
    assert any("at least one path" in e for e in errors_of(doc))


def test_shared_paths_must_not_be_owned_by_anyone():
    doc = plan()
    doc["slices"][0]["touches_shared"] = ["src/mail/registry.ts"]
    assert any("shared paths must not be owned" in e for e in errors_of(doc))


def test_shared_paths_may_be_shared_by_several_slices():
    doc = plan()
    doc["slices"][0]["touches_shared"] = ["src/db/migrations/"]
    doc["slices"][1]["touches_shared"] = ["src/db/migrations/"]
    assert schema.validate(doc).ok


# -- glob helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("src/**", "src/a/b.ts", True),
        ("src/**", "src", True),
        ("src/**/*.ts", "src/a/b.ts", True),
        ("src/**/*.ts", "src/b.ts", True),
        ("src/*.ts", "src/a/b.ts", False),
        ("src/a?.ts", "src/ab.ts", True),
        ("docs/**", "src/a.ts", False),
    ],
)
def test_matches(pattern, path, expected):
    assert schema.matches(pattern, path) is expected


def test_literal_prefix_and_suffix():
    assert schema.literal_prefix("src/auth/**/*.ts") == "src/auth/"
    assert schema.literal_prefix("src/index.ts") == "src/"
    assert schema.literal_suffix("tests/**/*.test.ts") == ".test.ts"
    assert schema.literal_suffix("src/index.ts") == "src/index.ts"
