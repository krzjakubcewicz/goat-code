"""miniyaml must handle everything an LLM plausibly writes into tasks.yaml,
and must refuse — precisely — everything it does not support.
"""

from __future__ import annotations

import pytest

from goatcode import miniyaml
from goatcode.miniyaml import YamlError, dumps, loads

TASKS_SAMPLE = """
version: 1
run_id: 20260822-114900-magic-link
cycle: 1
goal: Users sign in with a magic link emailed to them.
global_constraints:
  - "Node >= 20, no new runtime deps"
  - All copy in en-US
assumptions: []
slices:
  - id: S1
    title: Magic-link token store
    depends_on: []
    owns:
      - "src/auth/tokens/**"
      - "tests/auth/tokens/**"
    interfaces:
      - "createToken(email: string): Promise<Token>"
    acceptance:
      - id: A1
        text: "consumeToken returns the email once, then null."
    tests:
      - path: "tests/auth/tokens/store.test.ts"
        must_cover: [single use, expiry boundary]
    model: sonnet
    status: pending
    commits: {base: null, head: null}
  - id: S2
    title: Send the email
    depends_on: [S1]
    owns: ["src/mail/**"]
    acceptance:
      - id: A1
        text: An email is queued exactly once per request.
    status: pending
"""


def test_parses_a_realistic_tasks_file():
    data = loads(TASKS_SAMPLE)
    assert data["version"] == 1
    assert data["cycle"] == 1
    assert data["assumptions"] == []
    assert len(data["slices"]) == 2

    s1, s2 = data["slices"]
    assert s1["id"] == "S1"
    assert s1["owns"] == ["src/auth/tokens/**", "tests/auth/tokens/**"]
    assert s1["interfaces"] == ["createToken(email: string): Promise<Token>"]
    assert s1["acceptance"][0] == {"id": "A1", "text": "consumeToken returns the email once, then null."}
    assert s1["tests"][0]["must_cover"] == ["single use", "expiry boundary"]
    assert s1["commits"] == {"base": None, "head": None}
    assert s2["depends_on"] == ["S1"]
    assert s2["owns"] == ["src/mail/**"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a: 1", {"a": 1}),
        ("a: -2", {"a": -2}),
        ("a: 1.5", {"a": 1.5}),
        ("a: 1e3", {"a": 1000.0}),
        ("a: true", {"a": True}),
        ("a: False", {"a": False}),
        ("a: null", {"a": None}),
        ("a: ~", {"a": None}),
        ("a:", {"a": None}),
        ("a: 'it''s'", {"a": "it's"}),
        ('a: "tab\\there"', {"a": "tab\there"}),
        ('a: "\\u00e9"', {"a": "\u00e9"}),
        ("a: plain text", {"a": "plain text"}),
        ("a: [1, two, 'three']", {"a": [1, "two", "three"]}),
        ("a: {x: 1, y: two}", {"a": {"x": 1, "y": "two"}}),
        ("a: []", {"a": []}),
        ("a: {}", {"a": {}}),
    ],
)
def test_scalar_and_flow_forms(text, expected):
    assert loads(text) == expected


def test_comments_are_ignored_but_not_inside_strings():
    data = loads('# leading\na: 1  # trailing\nb: "has # hash"\nc: url#frag\n')
    assert data == {"a": 1, "b": "has # hash", "c": "url#frag"}


def test_nested_mappings_and_sequences():
    data = loads(
        "\n".join(
            [
                "root:",
                "  child:",
                "    - one",
                "    - two",
                "  other:",
                "    deep: value",
                "list:",
                "  - a: 1",
                "    b:",
                "      - x",
                "  - a: 2",
            ]
        )
    )
    assert data == {
        "root": {"child": ["one", "two"], "other": {"deep": "value"}},
        "list": [{"a": 1, "b": ["x"]}, {"a": 2}],
    }


def test_block_literal_keeps_newlines_and_chomping():
    data = loads("keep: |\n  line one\n  line two\nstrip: |-\n  only line\nfold: >-\n  a\n  b\n")
    assert data["keep"] == "line one\nline two\n"
    assert data["strip"] == "only line"
    assert data["fold"] == "a b"


def test_leading_document_marker_allowed():
    assert loads("---\na: 1\n") == {"a": 1}


def test_trailing_document_end_allowed():
    assert loads("a: 1\n...\n") == {"a": 1}


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("a: &anchor 1", "anchors"),
        ("a: 1\nb: *anchor", "aliases"),
        ("a: !!str 1", "tags"),
        ("a: 1\n<<: b", "merge keys"),
        ("a: 1\n---\nb: 2", "one document"),
        ("a:\n\tb: 1", "tab"),
        ("a: [1, 2", "flow sequence must close"),
        ("a: 'unterminated", "unterminated single-quoted"),
        ("a: 1\na: 2", "duplicate key"),
        ("just a scalar line", "key: value"),
        ("a: 1\n  b: 2", "unexpected indent"),
    ],
)
def test_rejects_unsupported_with_position(text, fragment):
    with pytest.raises(YamlError) as excinfo:
        loads(text)
    assert fragment in excinfo.value.message
    assert excinfo.value.line >= 1
    assert excinfo.value.col >= 1
    assert "line {}:{}".format(excinfo.value.line, excinfo.value.col) in str(excinfo.value)


def test_error_reports_the_offending_line():
    with pytest.raises(YamlError) as excinfo:
        loads("a: 1\nb: 2\nc: &x 3\n")
    assert excinfo.value.line == 3


def test_crlf_input_parses_identically():
    assert loads("a: 1\r\nb:\r\n  - x\r\n") == {"a": 1, "b": ["x"]}


def test_empty_document_is_none():
    assert loads("") is None
    assert loads("# only a comment\n") is None


def test_roundtrip_of_the_tasks_sample():
    once = loads(TASKS_SAMPLE)
    twice = loads(dumps(once))
    assert twice == once


@pytest.mark.parametrize(
    "value",
    [
        {"a": "yes"},
        {"a": "null"},
        {"a": "123"},
        {"a": "1.5"},
        {"a": "true"},
        {"a": ""},
        {"a": " padded "},
        {"a": "has: colon"},
        {"a": "- leading dash"},
        {"a": "trailing:"},
        {"a": "quote\"inside"},
        {"a": "back\\slash"},
        {"a": ["x", 1, None, True]},
        {"a": {"b": {"c": [1, {"d": "e"}]}}},
        {"a": []},
        {"a": {}},
        {"a": "multi\nline\ntext"},
        {"a": "multi\nline\ntext\n"},
        {"slices": [{"id": "S1", "owns": ["src/**"]}]},
    ],
)
def test_roundtrip_preserves_value(value):
    assert loads(dumps(value)) == value


def test_dumps_quotes_ambiguous_strings():
    text = dumps({"a": "true", "b": "123", "c": "plain"})
    assert '"true"' in text
    assert '"123"' in text
    assert "c: plain" in text


def test_dump_and_load_file_roundtrip(tmp_path):
    target = tmp_path / "tasks.yaml"
    payload = {"version": 1, "slices": [{"id": "S1", "owns": ["a/**"]}]}
    miniyaml.dump(payload, target)
    assert target.read_bytes().count(b"\r") == 0
    assert miniyaml.load(target) == payload
