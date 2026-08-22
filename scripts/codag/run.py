"""Run lifecycle: the ``.codag/`` directory, ``state.json``, and config.

A *run* is one pass of the pipeline over one feature request. Its state
lives in the target repository at ``.codag/runs/<run-id>/`` and is
git-ignored. Everything the orchestrator needs to resume after a crash or a
context compaction is on disk here, never only in conversation memory.
"""

from __future__ import annotations

import datetime
import pathlib
import re

from . import miniyaml, osenv

CODAG_DIR = ".codag"
STATE_VERSION = 1

PHASES = (
    "init",
    "grill",
    "plan",
    "approve",
    "execute",
    "synthesize",
    "verify",
    "done",
    "failed",
    "aborted",
)

DEFAULT_CONFIG = {
    "parallel": 3,
    "max_cycles": 3,
    "max_grill_rounds": 3,
    "max_plan_fix_attempts": 2,
    "models": {
        "planner": "opus",
        "executor": "sonnet",
        "executor_escalated": "opus",
        "synthesizer": "opus",
        "verifier": "opus",
        "replanner": "opus",
    },
    "gate_timeout_seconds": 1800,
    "setup_timeout_seconds": 1800,
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class RunError(RuntimeError):
    """Something is wrong with the repository or the run directory."""


def slugify(text, limit=32):
    """Lowercase, hyphenated, filesystem-safe fragment of ``text``."""
    slug = _SLUG_STRIP.sub("-", (text or "").lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rstrip("-")
    return slug or "run"


def new_run_id(title, now=None):
    """``YYYYMMDD-HHMMSS-<slug>`` - sortable and readable."""
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    return "{}-{}".format(stamp, slugify(title))


def codag_dir(repo):
    return pathlib.Path(repo) / CODAG_DIR


def runs_dir(repo):
    return codag_dir(repo) / "runs"


def ensure_gitignore(repo):
    """Make sure ``.codag/`` never lands in the user's commits.

    Returns True when the entry was added by this call.
    """
    repo = pathlib.Path(repo)
    ignore = repo / ".gitignore"
    entry = CODAG_DIR + "/"
    if ignore.exists():
        existing = ignore.read_text(encoding="utf-8")
        lines = [line.strip() for line in existing.splitlines()]
        if entry in lines or CODAG_DIR in lines:
            return False
        prefix = "" if existing.endswith("\n") or existing == "" else "\n"
        osenv.write_text(ignore, existing + prefix + entry + "\n")
        return True
    osenv.write_text(ignore, entry + "\n")
    return True


def load_config(repo):
    """Defaults overlaid with ``.codag/config.yaml`` if the user wrote one."""
    config = _deep_copy(DEFAULT_CONFIG)
    path = codag_dir(repo) / "config.yaml"
    if not path.exists():
        return config
    overrides = miniyaml.load(path) or {}
    if not isinstance(overrides, dict):
        raise RunError("{} must contain a mapping".format(path))
    _deep_merge(config, overrides)
    return config


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _deep_merge(base, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def preflight(repo=None):
    """Check the repository is fit to run a pipeline against.

    Returns ``(root, problems)``. ``root`` is None when there is no repo.
    """
    start = pathlib.Path(repo) if repo else pathlib.Path.cwd()
    root = osenv.repo_root(start)
    if root is None:
        return None, ["not inside a git repository (run 'git init' first)"]

    problems = []
    if not osenv.git(["rev-parse", "--verify", "HEAD"], cwd=root).ok:
        problems.append("the repository has no commits yet; make an initial commit first")

    status = osenv.git(["status", "--porcelain", "--untracked-files=normal"], cwd=root)
    dirty = [
        line
        for line in status.out.splitlines()
        if line.strip() and not line[3:].startswith(CODAG_DIR + "/")
    ]
    if dirty:
        preview = ", ".join(line[3:] for line in dirty[:5])
        more = "" if len(dirty) <= 5 else " (+{} more)".format(len(dirty) - 5)
        problems.append("working tree is not clean: {}{}".format(preview, more))

    branch = osenv.git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=root)
    if not branch.ok:
        problems.append("HEAD is detached; check out a branch before starting a run")

    return root, problems


class Run:
    """Handle on one pipeline run's on-disk state."""

    def __init__(self, repo, run_id, state):
        self.repo = pathlib.Path(repo)
        self.run_id = run_id
        self.state = state

    # -- construction ----------------------------------------------------

    @classmethod
    def create(cls, repo, title, mode, spec_text="", now=None):
        repo = pathlib.Path(repo)
        run_id = new_run_id(title, now=now)
        base_commit = osenv.git_out(["rev-parse", "HEAD"], cwd=repo)
        base_branch = osenv.git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=repo).out
        state = {
            "version": STATE_VERSION,
            "run_id": run_id,
            "mode": mode,
            "phase": "init",
            "cycle": 1,
            "grill_rounds": 0,
            "created_at": _now_iso(now),
            "updated_at": _now_iso(now),
            "repo": str(repo),
            "base_branch": base_branch or None,
            "base_commit": base_commit,
            "integration_branch": "codag/{}/integration".format(run_id),
            "temp_root": str(osenv.temp_root() / osenv.run_slug(run_id)),
            "worktrees": {},
            "config": load_config(repo),
        }
        run = cls(repo, run_id, state)
        run.root.mkdir(parents=True, exist_ok=True)
        run.cycle_dir().mkdir(parents=True, exist_ok=True)
        (run.cycle_dir() / "briefs").mkdir(exist_ok=True)
        (run.cycle_dir() / "reports").mkdir(exist_ok=True)
        osenv.write_text(run.spec_path, spec_text or _placeholder_spec(title))
        osenv.write_text(run.ledger_path, "# cod-ag ledger - {}\n\n".format(run_id))
        run.save()
        return run

    @classmethod
    def load(cls, repo, run_id=None):
        repo = pathlib.Path(repo)
        if run_id is None:
            run_id = latest_run_id(repo)
            if run_id is None:
                raise RunError("no runs found under {}".format(runs_dir(repo)))
        path = runs_dir(repo) / run_id / "state.json"
        if not path.exists():
            raise RunError("no such run: {}".format(run_id))
        return cls(repo, run_id, osenv.read_json(path))

    # -- paths -----------------------------------------------------------

    @property
    def root(self):
        return runs_dir(self.repo) / self.run_id

    @property
    def state_path(self):
        return self.root / "state.json"

    @property
    def spec_path(self):
        return self.root / "spec.md"

    @property
    def stack_path(self):
        return self.root / "stack.json"

    @property
    def tasks_path(self):
        return self.root / "tasks.yaml"

    @property
    def ledger_path(self):
        return self.root / "ledger.md"

    @property
    def baseline_path(self):
        return self.root / "baseline-gates.json"

    def cycle_dir(self, cycle=None):
        return self.root / "cycle-{}".format(cycle or self.cycle)

    def brief_path(self, slice_id):
        return self.cycle_dir() / "briefs" / "{}.md".format(slice_id)

    def report_path(self, slice_id):
        return self.cycle_dir() / "reports" / "{}.md".format(slice_id)

    # -- state -----------------------------------------------------------

    @property
    def cycle(self):
        return self.state.get("cycle", 1)

    @property
    def phase(self):
        return self.state.get("phase", "init")

    @property
    def config(self):
        return self.state.get("config", DEFAULT_CONFIG)

    @property
    def base_commit(self):
        return self.state["base_commit"]

    @property
    def integration_branch(self):
        return self.state["integration_branch"]

    @property
    def temp_root(self):
        return pathlib.Path(self.state["temp_root"])

    def save(self):
        self.state["updated_at"] = _now_iso()
        osenv.write_json(self.state_path, self.state)

    def set_phase(self, phase):
        if phase not in PHASES:
            raise RunError("unknown phase {!r}".format(phase))
        self.state["phase"] = phase
        self.save()

    def advance_cycle(self):
        """Move to the next cycle, creating its directory. Returns the number."""
        self.state["cycle"] = self.cycle + 1
        self.cycle_dir().mkdir(parents=True, exist_ok=True)
        (self.cycle_dir() / "briefs").mkdir(exist_ok=True)
        (self.cycle_dir() / "reports").mkdir(exist_ok=True)
        self.save()
        return self.cycle

    def cycles_exhausted(self):
        return self.cycle > self.config.get("max_cycles", 3)

    def record_worktree(self, slice_id, path):
        self.state.setdefault("worktrees", {})[slice_id] = str(path)
        self.save()

    def forget_worktree(self, slice_id):
        self.state.get("worktrees", {}).pop(slice_id, None)
        self.save()

    def append_spec(self, section):
        """Append a clarification section to the spec, keeping it the record."""
        existing = osenv.read_text(self.spec_path) if self.spec_path.exists() else ""
        prefix = "" if existing.endswith("\n\n") or existing == "" else "\n\n"
        osenv.write_text(self.spec_path, existing + prefix + section.rstrip() + "\n")

    def summary(self):
        """Compact dict for ``codag status``."""
        return {
            "run_id": self.run_id,
            "phase": self.phase,
            "cycle": self.cycle,
            "max_cycles": self.config.get("max_cycles", 3),
            "mode": self.state.get("mode"),
            "grill_rounds": self.state.get("grill_rounds", 0),
            "base_branch": self.state.get("base_branch"),
            "base_commit": (self.base_commit or "")[:7],
            "integration_branch": self.integration_branch,
            "worktrees": self.state.get("worktrees", {}),
            "updated_at": self.state.get("updated_at"),
        }


def latest_run_id(repo):
    """Most recently created run id, or None."""
    directory = runs_dir(repo)
    if not directory.is_dir():
        return None
    candidates = sorted(p.name for p in directory.iterdir() if (p / "state.json").exists())
    return candidates[-1] if candidates else None


def list_runs(repo):
    """All runs, oldest first, as summary dicts."""
    directory = runs_dir(repo)
    if not directory.is_dir():
        return []
    out = []
    for entry in sorted(directory.iterdir()):
        if not (entry / "state.json").exists():
            continue
        out.append(Run.load(repo, entry.name).summary())
    return out


def _now_iso(now=None):
    return (now or datetime.datetime.now()).replace(microsecond=0).isoformat()


def _placeholder_spec(title):
    return "\n".join(
        [
            "# {}".format(title),
            "",
            "## Goal",
            "",
            title,
            "",
            "## Requirements",
            "",
            "_Captured from the chat prompt; the planner will grill for the rest._",
            "",
        ]
    )
