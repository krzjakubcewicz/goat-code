"""Did the executor actually work test-first?

Every executor is told to. Nothing checked, so "test-driven" was an honour
system. The slice's own commit range is evidence: walk it in order and find
the first commit that adds implementation while no test has been touched
yet. That is a provable violation, not a judgement call.

The rule is deliberately the lenient one. A slice may legitimately open with
a fixture, a config file or a package scaffold; only *code* counts as
implementation, and a commit that adds a test alongside its implementation
passes, because that is what a squashed red-green pair looks like.
"""

from __future__ import annotations

import posixpath

from . import osenv, schema

#: Filename markers that make a path a test wherever it sits.
TEST_MARKERS = (".test.", ".spec.", "_test.", "-test.", "_spec.", "-spec.")

#: Directory names that make everything under them a test.
TEST_DIRS = ("tests", "test", "spec", "specs", "__tests__", "e2e", "testing")

#: Extensions that are never implementation: data, config, docs, lockfiles.
#: Letting these open a slice is what keeps the check fair to scaffolding.
NON_CODE_SUFFIXES = (
    ".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".lock", ".gitignore", ".gitattributes", ".editorconfig", ".env", ".example",
    ".csv", ".sql", ".png", ".jpg", ".svg", ".ico",
)


def is_test_path(path, item=None, profile=None):
    """Whether ``path`` is a test file, for this slice and this stack."""
    normalised = str(path).replace("\\", "/").lstrip("./")
    name = posixpath.basename(normalised)

    for entry in (item or {}).get("tests") or []:
        declared = entry.get("path") if isinstance(entry, dict) else entry
        if declared and str(declared).replace("\\", "/").lstrip("./") == normalised:
            return True

    if name.startswith("test_") or name.startswith("test-"):
        return True
    if any(marker in name for marker in TEST_MARKERS):
        return True

    segments = normalised.split("/")[:-1]
    known = set(TEST_DIRS) | {
        str(d).strip("/").replace("\\", "/") for d in (profile or {}).get("test_dirs") or []
    }
    return any(segment in known for segment in segments)


def is_implementation(path, item, profile=None):
    """Whether ``path`` is code this slice owns, rather than a test or data."""
    normalised = str(path).replace("\\", "/").lstrip("./")
    if is_test_path(normalised, item, profile):
        return False
    if any(normalised.lower().endswith(suffix) for suffix in NON_CODE_SUFFIXES):
        return False
    owns = item.get("owns") or []
    return any(schema.matches(pattern, normalised) for pattern in owns)


def commit_files(repo, sha):
    """Paths touched by one commit, ignoring merges' second parents."""
    result = osenv.git(
        ["show", "--first-parent", "--name-only", "--format=", "--no-renames", sha], cwd=repo
    )
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def violations(repo, item, base, head, profile=None):
    """Implementation that landed before this slice touched any test.

    Returns a list of dicts (at most one - the first offence is the finding);
    empty means the history is consistent with working test-first.
    """
    if not base or not head or base == head:
        return []

    log = osenv.git(["log", "--reverse", "--format=%H%x1f%s", "{}..{}".format(base, head)], cwd=repo)
    if not log.ok:
        return []

    touched_a_test = False
    for line in log.stdout.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        files = commit_files(repo, sha)

        has_test = any(is_test_path(path, item, profile) for path in files)
        implementation = [path for path in files if is_implementation(path, item, profile)]

        if implementation and not touched_a_test and not has_test:
            return [
                {
                    "commit": sha,
                    "short": sha[:7],
                    "subject": subject,
                    "files": implementation[:5],
                }
            ]
        if has_test:
            touched_a_test = True
    return []


def check(repo, item, profile=None):
    """Human-readable problems for the slice, or an empty list."""
    commits = item.get("commits") or {}
    found = violations(repo, item, commits.get("base"), commits.get("head"), profile)
    out = []
    for entry in found:
        out.append(
            "implementation landed before any test: commit {} ({}) added {} "
            "with no test touched yet in this slice".format(
                entry["short"], entry["subject"], ", ".join(entry["files"])
            )
        )
    return out
