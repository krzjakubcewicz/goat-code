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

#: What cod-ag creates inside a target repo and therefore asks git to ignore.
#: ``.worktrees/`` is the repo-local fallback location, used only if someone
#: points ``CODAG_TEMP_ROOT`` inside the repository.
GITIGNORE_ENTRIES = (CODAG_DIR + "/", ".worktrees/")

#: Marks the block cod-ag manages, so preflight can tell its own edit apart
#: from a real change the user made.
GITIGNORE_HEADER = "# cod-ag run state (managed by cod-ag)"

#: Every phase the state machine can derive. Order is the happy path.
PHASES = (
    "init",
    "grill",
    "ask",
    "plan",
    "approve",
    "execute",
    "synthesize",
    "verify",
    "replan",
    "done",
    "failed",
    "aborted",
)

#: Phases from which no further action is taken.
TERMINAL_PHASES = ("done", "failed", "aborted")

DEFAULT_CONFIG = {
    "parallel": 3,
    "max_cycles": 3,
    "max_grill_rounds": 3,
    "max_plan_fix_attempts": 2,
    # chat: gate only a chat-mode run's first cycle. always: gate every
    # cycle. never: fully autonomous once the plan validates.
    "approval_gate": "chat",
    # Add cod-ag's entries to the project's .gitignore on the first run.
    # The change is left uncommitted for you to review. Turn off to rely on
    # .git/info/exclude alone, which cod-ag always writes either way.
    "manage_gitignore": True,
    "worktree_setup": True,
    "models": {
        "planner": "opus",
        "executor": "haiku",
        "executor_escalated": "sonnet",
        "synthesizer": "sonnet",
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


def ensure_ignored(repo):
    """Make sure ``.codag/`` never shows up in the user's git status.

    Written to ``.git/info/exclude`` rather than ``.gitignore``: run state
    is local, per-machine scratch, and editing a tracked ``.gitignore``
    would dirty the working tree the pipeline just promised not to touch.

    Returns True when the entry was added by this call.
    """
    repo = pathlib.Path(repo)
    entry = CODAG_DIR + "/"

    result = osenv.git(["rev-parse", "--git-common-dir"], cwd=repo)
    if not result.ok:
        return False
    git_dir = pathlib.Path(result.out)
    if not git_dir.is_absolute():
        git_dir = repo / git_dir

    tracked = repo / ".gitignore"
    if tracked.exists():
        lines = [line.strip() for line in tracked.read_text(encoding="utf-8").splitlines()]
        if entry in lines or CODAG_DIR in lines:
            return False

    exclude = git_dir / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if entry in [line.strip() for line in existing.splitlines()]:
        return False
    prefix = "" if existing.endswith("\n") or existing == "" else "\n"
    osenv.write_text(exclude, existing + prefix + entry + "\n")
    return True


def gitignore_block():
    return "\n".join([GITIGNORE_HEADER] + list(GITIGNORE_ENTRIES)) + "\n"


def strip_gitignore_block(text):
    """``text`` with cod-ag's managed block removed, as it was before.

    Used by preflight to decide whether a modified ``.gitignore`` differs
    from HEAD by nothing except cod-ag's own edit.
    """
    lines = text.splitlines()
    try:
        start = lines.index(GITIGNORE_HEADER)
    except ValueError:
        return text

    end = start + 1
    while end < len(lines) and lines[end].strip() in GITIGNORE_ENTRIES:
        end += 1

    # The blank line we inserted to separate the block from what came before.
    if start > 0 and lines[start - 1].strip() == "":
        start -= 1

    kept = lines[:start] + lines[end:]
    if not kept:
        return ""
    return "\n".join(kept) + "\n"


def ensure_gitignore(repo):
    """Add cod-ag's entries to the project's ``.gitignore``, creating it if
    absent. Returns True when this call changed the file.

    Deliberately left uncommitted: cod-ag does not commit to your branch.
    Preflight knows to tolerate exactly this change on the next run.
    """
    path = pathlib.Path(repo) / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    present = {line.strip() for line in existing.splitlines()}
    if all(entry in present or entry.rstrip("/") in present for entry in GITIGNORE_ENTRIES):
        return False

    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix.strip():
        prefix += "\n"
    osenv.write_text(path, prefix + gitignore_block())
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


def gitignore_change_is_ours(repo):
    """True when ``.gitignore`` differs from HEAD by nothing but our block.

    cod-ag writes that block on the first run and deliberately leaves it
    uncommitted. Without this, the very next run would fail its own
    clean-tree preflight on an edit cod-ag made itself.
    """
    path = pathlib.Path(repo) / ".gitignore"
    if not path.exists():
        return False
    current = path.read_text(encoding="utf-8")
    if GITIGNORE_HEADER not in current:
        return False

    committed = osenv.git(["show", "HEAD:.gitignore"], cwd=repo)
    baseline = committed.stdout if committed.ok else ""
    return strip_gitignore_block(current) == baseline


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

    # stdout, not .out: porcelain status is column-aligned and .out strips the
    # leading space off " M path", which would mis-slice the first entry.
    status = osenv.git(["status", "--porcelain", "--untracked-files=normal"], cwd=root)
    paths = [line[3:].strip('"') for line in status.stdout.splitlines() if line.strip()]
    dirty = [
        path
        for path in paths
        if not path.startswith(CODAG_DIR + "/")
        and not (path == ".gitignore" and gitignore_change_is_ours(root))
    ]
    if dirty:
        preview = ", ".join(dirty[:5])
        more = "" if len(dirty) <= 5 else " (+{} more)".format(len(dirty) - 5)
        problems.append("working tree is not clean: {}{}".format(preview, more))

    branch = osenv.git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=root)
    if not branch.ok:
        problems.append("HEAD is detached; check out a branch before starting a run")

    if osenv.in_linked_worktree(root):
        problems.append(
            "this is a linked worktree; start the run from the main work tree at {}".format(
                osenv.main_repo_root(root)
            )
        )

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
        run_id = _unique_run_id(repo, new_run_id(title, now=now))
        base_commit = osenv.git_out(["rev-parse", "HEAD"], cwd=repo)
        base_branch = osenv.git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=repo).out
        state = {
            "version": STATE_VERSION,
            "run_id": run_id,
            "mode": mode,
            "phase": "init",
            "cycle": 1,
            "grill_rounds": 0,
            "plan_fix_attempts": 0,
            "approval": None,
            "escalations": {},
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

    # -- counters, all capped in code rather than in prose ---------------

    @property
    def grill_rounds(self):
        return self.state.get("grill_rounds", 0)

    def bump_grill_round(self):
        """Count a completed question round. Returns the new total."""
        self.state["grill_rounds"] = self.grill_rounds + 1
        self.save()
        return self.state["grill_rounds"]

    def grill_exhausted(self):
        return self.grill_rounds >= self.config.get("max_grill_rounds", 3)

    @property
    def plan_fix_attempts(self):
        return self.state.get("plan_fix_attempts", 0)

    def bump_plan_fix(self):
        self.state["plan_fix_attempts"] = self.plan_fix_attempts + 1
        self.save()
        return self.state["plan_fix_attempts"]

    def plan_fixes_exhausted(self):
        return self.plan_fix_attempts >= self.config.get("max_plan_fix_attempts", 2)

    def escalations(self, slice_id=None):
        recorded = self.state.setdefault("escalations", {})
        return recorded if slice_id is None else recorded.get(slice_id, 0)

    def escalate(self, slice_id):
        """Record one model escalation for a slice. Returns the new count."""
        recorded = self.state.setdefault("escalations", {})
        recorded[slice_id] = recorded.get(slice_id, 0) + 1
        self.save()
        return recorded[slice_id]

    # -- the approval gate -----------------------------------------------

    @property
    def approval(self):
        return self.state.get("approval")

    def set_approval(self, decision):
        if decision not in ("approved", "revise", "aborted", None):
            raise RunError("unknown approval decision {!r}".format(decision))
        self.state["approval"] = decision
        self.save()
        return decision

    def gate_applies(self):
        """Whether this cycle needs the user to approve the plan."""
        gate = self.config.get("approval_gate", "chat")
        if gate == "never":
            return False
        if gate == "always":
            return True
        return self.state.get("mode") == "chat" and self.cycle == 1

    def needs_approval(self):
        return self.gate_applies() and self.approval != "approved"

    def advance_cycle(self):
        """Move to the next cycle, creating its directory. Returns the number."""
        self.state["cycle"] = self.cycle + 1
        # Per-cycle counters start over; grill_rounds does not, because
        # replan cycles never grill.
        self.state["plan_fix_attempts"] = 0
        self.state["approval"] = None
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
            "grill_rounds": self.grill_rounds,
            "approval": self.approval,
            "base_branch": self.state.get("base_branch"),
            "base_commit": (self.base_commit or "")[:7],
            "integration_branch": self.integration_branch,
            "worktrees": self.state.get("worktrees", {}),
            "updated_at": self.state.get("updated_at"),
        }


def _unique_run_id(repo, run_id):
    """Two runs started in the same second must not share a directory."""
    directory = runs_dir(repo)
    if not (directory / run_id).exists():
        return run_id
    for suffix in range(2, 100):
        candidate = "{}-{}".format(run_id, suffix)
        if not (directory / candidate).exists():
            return candidate
    raise RunError("cannot allocate a run id for {}".format(run_id))


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
