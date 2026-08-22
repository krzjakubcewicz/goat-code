"""Stdlib-only YAML reader/writer for the cod-ag tasks.yaml dialect.

Why this exists: cod-ag must run on Windows, macOS and Linux with no pip
install step. PyYAML is not in the standard library, so we parse the small,
documented subset of YAML that tasks.yaml uses and reject everything else
with a precise line:col error instead of guessing.

Supported
    - block mappings, block sequences, arbitrary nesting
    - plain / single-quoted / double-quoted scalars
    - null (``null``, ``~``, empty), booleans (``true``/``false``), int, float
    - block scalars: ``|``, ``|-``, ``|+``, ``>``, ``>-``, ``>+``
    - single-line flow collections: ``[a, b]``, ``{a: 1}``
    - ``#`` comments, one leading ``---``, one trailing ``...``

Rejected (with line:col)
    anchors ``&``, aliases ``*``, tags ``!``, merge keys ``<<:``,
    multiple documents, tabs used for indentation
"""

from __future__ import annotations

import pathlib
import re

__all__ = ["YamlError", "loads", "load", "dumps", "dump"]

_NULLS = {"", "~", "null", "Null", "NULL"}
_TRUE = {"true", "True", "TRUE"}
_FALSE = {"false", "False", "FALSE"}
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]*\.[0-9]+|[0-9]+\.[0-9]*)(?:[eE][-+]?[0-9]+)?$")
_EXP_RE = re.compile(r"^[-+]?[0-9]+[eE][-+]?[0-9]+$")


class YamlError(ValueError):
    """Parse failure carrying a 1-based line and column."""

    def __init__(self, message, line, col):
        super().__init__("line {}:{}: {}".format(line, col, message))
        self.message = message
        self.line = line
        self.col = col


# --------------------------------------------------------------------------
# scanning helpers
# --------------------------------------------------------------------------


def _strip_comment(text):
    """Drop a trailing ``#`` comment, respecting quotes."""
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _find_key_colon(text):
    """Index of the ``:`` ending a mapping key, or -1. Quote and flow aware."""
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            if i + 1 == len(text) or text[i + 1] in " \t":
                return i
        i += 1
    return -1


class _Lines:
    """Indexed view over the source lines, with a dash-rewrite escape hatch."""

    def __init__(self, text):
        self.raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def __len__(self):
        return len(self.raw)

    def lineno(self, i):
        return i + 1

    def indent(self, i):
        raw = self.raw[i]
        n = 0
        while n < len(raw) and raw[n] == " ":
            n += 1
        if n < len(raw) and raw[n] == "\t":
            raise YamlError("tab used for indentation; use spaces", i + 1, n + 1)
        return n

    def content(self, i):
        return _strip_comment(self.raw[i].strip())

    def is_blank(self, i):
        stripped = self.raw[i].strip()
        return stripped == "" or stripped.startswith("#")

    def next_significant(self, i):
        """Index of the next non-blank, non-comment line, or len(self)."""
        while i < len(self.raw) and self.is_blank(i):
            i += 1
        return i


def _reject_unsupported(lines, i, content):
    ln = lines.lineno(i)
    col = lines.indent(i) + 1
    if content.startswith("&"):
        raise YamlError("anchors (&) are not supported", ln, col)
    if content.startswith("*"):
        raise YamlError("aliases (*) are not supported", ln, col)
    if content.startswith("!"):
        raise YamlError("tags (!) are not supported", ln, col)
    if content.startswith("<<:"):
        raise YamlError("merge keys are not supported", ln, col)


# --------------------------------------------------------------------------
# scalar parsing
# --------------------------------------------------------------------------


def _unescape_double(text, ln, col):
    out = []
    i = 0
    simple = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/"}
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(text):
            raise YamlError("dangling escape in double-quoted string", ln, col)
        nxt = text[i + 1]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt in "uU":
            width = 4 if nxt == "u" else 8
            hexpart = text[i + 2 : i + 2 + width]
            if len(hexpart) != width:
                raise YamlError("truncated unicode escape", ln, col)
            try:
                out.append(chr(int(hexpart, 16)))
            except ValueError:
                raise YamlError("bad unicode escape: {!r}".format(hexpart), ln, col)
            i += 2 + width
        else:
            raise YamlError("unknown escape sequence: backslash-{}".format(nxt), ln, col)
    return "".join(out)


def _split_flow(body, ln, col):
    """Split a flow body on top-level commas."""
    parts = []
    depth = 0
    quote = None
    start = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise YamlError("unbalanced flow collection", ln, col)
        elif ch == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
        i += 1
    if quote:
        raise YamlError("unterminated quoted string", ln, col)
    if depth != 0:
        raise YamlError("unbalanced flow collection", ln, col)
    tail = body[start:]
    if tail.strip() or parts:
        parts.append(tail)
    return [p.strip() for p in parts]


def _parse_scalar(text, ln, col):
    text = text.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlError("flow sequence must close on the same line", ln, col)
        return [_parse_scalar(p, ln, col) for p in _split_flow(text[1:-1], ln, col)]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise YamlError("flow mapping must close on the same line", ln, col)
        out = {}
        for part in _split_flow(text[1:-1], ln, col):
            if not part:
                continue
            idx = _find_key_colon(part)
            if idx < 0:
                raise YamlError("flow mapping entry needs 'key: value'", ln, col)
            key = _parse_scalar(part[:idx], ln, col)
            out[key] = _parse_scalar(part[idx + 1 :], ln, col)
        return out
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise YamlError("unterminated single-quoted string", ln, col)
        return text[1:-1].replace("''", "'")
    if text.startswith('"'):
        if len(text) < 2 or not text.endswith('"'):
            raise YamlError("unterminated double-quoted string", ln, col)
        return _unescape_double(text[1:-1], ln, col)
    if text[:1] == "&":
        raise YamlError("anchors (&) are not supported; quote the value if it is literal", ln, col)
    if text[:1] == "*":
        raise YamlError("aliases (*) are not supported; quote the value if it is literal", ln, col)
    if text[:1] == "!":
        raise YamlError("tags (!) are not supported; quote the value if it is literal", ln, col)
    if text in _NULLS:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text) or _EXP_RE.match(text):
        return float(text)
    return text


def _parse_block_scalar(lines, i, header, parent_indent):
    """Consume a ``|``/``>`` block. Returns (text, next_index)."""
    ln = lines.lineno(i)
    style = header[0]
    chomp = "clip"
    explicit_indent = None
    for ch in header[1:]:
        if ch == "-":
            chomp = "strip"
        elif ch == "+":
            chomp = "keep"
        elif ch.isdigit():
            explicit_indent = parent_indent + int(ch)
        else:
            raise YamlError("bad block scalar header {!r}".format(header), ln, len(header))

    i += 1
    body = []
    block_indent = explicit_indent
    while i < len(lines):
        raw = lines.raw[i]
        if raw.strip() == "":
            body.append("")
            i += 1
            continue
        ind = lines.indent(i)
        if ind <= parent_indent:
            break
        if block_indent is None:
            block_indent = ind
        if ind < block_indent:
            break
        body.append(raw[block_indent:])
        i += 1

    while body and body[-1] == "":
        body.pop()

    if style == "|":
        text = "\n".join(body)
    else:
        folded = []
        for line in body:
            if line == "":
                folded.append("\n")
            elif folded and not folded[-1].endswith("\n"):
                folded.append(" " + line)
            else:
                folded.append(line)
        text = "".join(folded)

    if chomp == "clip":
        if text:
            text += "\n"
    elif chomp == "keep":
        text += "\n"
    return text, i


# --------------------------------------------------------------------------
# block parsing
# --------------------------------------------------------------------------


def _parse_node(lines, i, indent):
    i = lines.next_significant(i)
    if i >= len(lines):
        return None, i
    content = lines.content(i)
    _reject_unsupported(lines, i, content)
    if content == "-" or content.startswith("- "):
        return _parse_sequence(lines, i, indent)
    return _parse_mapping(lines, i, indent)


def _parse_mapping(lines, i, indent):
    out = {}
    while True:
        i = lines.next_significant(i)
        if i >= len(lines):
            break
        ind = lines.indent(i)
        if ind < indent:
            break
        content = lines.content(i)
        if content in ("...", "---"):
            break
        ln = lines.lineno(i)
        col = ind + 1
        if ind > indent:
            raise YamlError("unexpected indent (expected {} spaces)".format(indent), ln, col)
        _reject_unsupported(lines, i, content)
        if content.startswith("- "):
            raise YamlError("sequence item where a mapping key was expected", ln, col)

        idx = _find_key_colon(content)
        if idx < 0:
            raise YamlError("expected 'key: value' but found {!r}".format(content), ln, col)
        key = _parse_scalar(content[:idx], ln, col)
        if key in out:
            raise YamlError("duplicate key {!r}".format(key), ln, col)
        rest = content[idx + 1 :].strip()

        if rest.startswith("|") or rest.startswith(">"):
            out[key], i = _parse_block_scalar(lines, i, rest, ind)
            continue
        if rest == "":
            nxt = lines.next_significant(i + 1)
            if nxt < len(lines) and lines.indent(nxt) > ind:
                out[key], i = _parse_node(lines, nxt, lines.indent(nxt))
            else:
                out[key] = None
                i += 1
            continue
        out[key] = _parse_scalar(rest, ln, col)
        i += 1
    return out, i


def _parse_sequence(lines, i, indent):
    out = []
    while True:
        i = lines.next_significant(i)
        if i >= len(lines):
            break
        ind = lines.indent(i)
        if ind < indent:
            break
        content = lines.content(i)
        if content in ("...", "---"):
            break
        ln = lines.lineno(i)
        col = ind + 1
        if ind > indent:
            raise YamlError("unexpected indent (expected {} spaces)".format(indent), ln, col)
        if content != "-" and not content.startswith("- "):
            break
        _reject_unsupported(lines, i, content)

        if content == "-":
            nxt = lines.next_significant(i + 1)
            if nxt < len(lines) and lines.indent(nxt) > ind:
                value, i = _parse_node(lines, nxt, lines.indent(nxt))
            else:
                value = None
                i += 1
            out.append(value)
            continue

        raw = lines.raw[i]
        dash = raw.index("-", ind)
        offset = dash + 1
        while offset < len(raw) and raw[offset] == " ":
            offset += 1
        item = content[1:].strip()

        if _find_key_colon(item) >= 0 and not item[:1] in ("[", "{", "'", '"'):
            # "- key: value" starts a mapping. Blank out the dash so the
            # mapping parser sees a normal block starting at column `offset`.
            lines.raw[i] = " " * offset + raw[offset:]
            value, i = _parse_mapping(lines, i, offset)
            out.append(value)
            continue

        if item.startswith("|") or item.startswith(">"):
            value, i = _parse_block_scalar(lines, i, item, ind)
            out.append(value)
            continue

        out.append(_parse_scalar(item, ln, col))
        i += 1
    return out, i


def loads(text):
    """Parse a YAML document from ``text``."""
    if not isinstance(text, str):
        raise TypeError("loads expects str")
    lines = _Lines(text)
    i = lines.next_significant(0)
    if i < len(lines) and lines.content(i) == "---":
        i = lines.next_significant(i + 1)
    if i >= len(lines):
        return None
    value, i = _parse_node(lines, i, lines.indent(i))
    i = lines.next_significant(i)
    if i < len(lines):
        content = lines.content(i)
        if content == "---":
            raise YamlError("only one document is supported", lines.lineno(i), 1)
        if content != "...":
            raise YamlError(
                "unexpected content after document end: {!r}".format(content),
                lines.lineno(i),
                lines.indent(i) + 1,
            )
    return value


def load(path):
    """Parse the YAML document stored at ``path``."""
    return loads(pathlib.Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_./@][^\n]*$")
_NEEDS_QUOTE_START = set("-?:,[]{}#&*!|>'\"%@`")


def _needs_quotes(text):
    if text == "":
        return True
    if text != text.strip():
        return True
    if text in _NULLS or text in _TRUE or text in _FALSE:
        return True
    if _INT_RE.match(text) or _FLOAT_RE.match(text) or _EXP_RE.match(text):
        return True
    if text[0] in _NEEDS_QUOTE_START:
        return True
    if ": " in text or text.endswith(":") or " #" in text:
        return True
    if not _PLAIN_SAFE.match(text):
        return True
    return False


def _quote(text):
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\t", "\\t").replace("\r", "\\r")
    return '"' + escaped + '"'


def _scalar(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _quote(value) if _needs_quotes(value) else value
    raise TypeError("cannot serialise a {}".format(type(value).__name__))


def _emit(value, indent, out):
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            keytext = _scalar(str(key))
            if isinstance(item, str) and "\n" in item:
                style = "|" if item.endswith("\n") else "|-"
                out.append("{}{}: {}".format(pad, keytext, style))
                for line in item.rstrip("\n").split("\n"):
                    out.append("{}  {}".format(pad, line) if line else "")
            elif isinstance(item, dict) and item:
                out.append("{}{}:".format(pad, keytext))
                _emit(item, indent + 2, out)
            elif isinstance(item, (list, tuple)) and item:
                out.append("{}{}:".format(pad, keytext))
                _emit(list(item), indent + 2, out)
            elif isinstance(item, dict):
                out.append("{}{}: {{}}".format(pad, keytext))
            elif isinstance(item, (list, tuple)):
                out.append("{}{}: []".format(pad, keytext))
            else:
                out.append("{}{}: {}".format(pad, keytext, _scalar(item)))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (dict, list, tuple)) and item:
                nested = []
                _emit(item if isinstance(item, dict) else list(item), indent + 2, nested)
                out.append("{}- {}".format(pad, nested[0].lstrip()))
                out.extend(nested[1:])
            elif isinstance(item, dict):
                out.append("{}- {{}}".format(pad))
            elif isinstance(item, (list, tuple)):
                out.append("{}- []".format(pad))
            elif isinstance(item, str) and "\n" in item:
                style = "|" if item.endswith("\n") else "|-"
                out.append("{}- {}".format(pad, style))
                for line in item.rstrip("\n").split("\n"):
                    out.append("{}    {}".format(pad, line) if line else "")
            else:
                out.append("{}- {}".format(pad, _scalar(item)))
        return
    out.append("{}{}".format(pad, _scalar(value)))


def dumps(value):
    """Serialise ``value`` to the tasks.yaml dialect."""
    if value is None:
        return "null\n"
    if isinstance(value, dict) and not value:
        return "{}\n"
    if isinstance(value, (list, tuple)) and not value:
        return "[]\n"
    out = []
    _emit(value, 0, out)
    return "\n".join(out) + "\n"


def dump(value, path):
    """Write ``value`` to ``path`` as UTF-8 with LF endings."""
    pathlib.Path(path).write_text(dumps(value), encoding="utf-8", newline="\n")
