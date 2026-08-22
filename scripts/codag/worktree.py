"""Executor worktrees: one isolated checkout per slice.

Worktrees live under the OS temp directory, not inside the repository.
Two reasons, both learned the hard way on Windows: a repo-local worktree
with a deep ``node_modules`` blows past the 260-character path limit, and a
cleanup that fails on a locked file litters the user's project.

Branches are created off the run's recorded base commit, never off HEAD, so
a run stays reproducible even if the user moves their branch mid-flight.
"""

from __future__ import annotations

import pathlib

from . import osenv

INTEGRATION_DIR = "_integration"


class WorktreeError(RuntimeError):
    """A worktree could not be created, found or removed."""


def branch_name(run_id, slice_id):
    return "codag/{}/{}".format(run_id, slice_id)


def path_for(run, slice_id):
    return run.temp_root / slice_id


def integration_path(run):
    return run.temp_root / INTEGRATION_DIR


def existing(repo):
    """Every worktree git knows about: ``{absolute path: branch or None}``."""
    result = osenv.git(["worktree", "list", "--porcelain"], cwd=repo)
    if not result.ok:
        return {}
    out = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = pathlib.Path(line[len("worktree ") :].strip())
            out[current] = None
        elif line.startswith("branch ") and current is not None:
            out[current] = line[len("branch ") :].strip().replace("refs/heads/", "")
    return out


def _registered(repo, path):
    target = pathlib.Path(path).resolve()
    for known in existing(repo):
        try:
            if known.resolve() == target:
                return True
        except OSError:
            continue
    return False


def create(run, slice_id, branch=None, start=None, setup=None, stack_profile=None):
    """Add a worktree for ``slice_id``. Idempotent for an existing one.

    Returns ``(path, branch, setup_result_or_None)``.
    """
    repo = run.repo
    path = path_for(run, slice_id)
    branch = branch or branch_name(run.run_id, slice_id)
    start = start or run.base_commit

    if path.exists() and _registered(repo, path):
        return path, branch, None
    if path.exists():
        # Left over from a crashed run: git does not know it, so it is junk.
        if not osenv.rmtree_force(path):
            raise WorktreeError("could not clear stale worktree directory {}".format(path))

    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if _branch_exists(repo, branch):
        args += [str(path), branch]
    else:
        args += ["-b", branch, str(path), start]
    result = osenv.git(args, cwd=repo)
    if not result.ok:
        raise WorktreeError(
            "git worktree add failed for {}: {}".format(slice_id, result.stderr.strip() or result.stdout.strip())
        )

    run.record_worktree(slice_id, path)

    setup_result = None
    if setup is None:
        setup = run.config.get("worktree_setup", True)
    if setup:
        setup_result = run_setup(run, path, stack_profile)
    return path, branch, setup_result


def create_integration(run, start=None):
    """Worktree holding the integration branch the synthesizer merges into."""
    repo = run.repo
    path = integration_path(run)
    branch = run.integration_branch
    start = start or run.base_commit

    if path.exists() and _registered(repo, path):
        return path, branch
    if path.exists() and not osenv.rmtree_force(path):
        raise WorktreeError("could not clear stale integration worktree {}".format(path))

    path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo, branch):
        args = ["worktree", "add", str(path), branch]
    else:
        args = ["worktree", "add", "-b", branch, str(path), start]
    result = osenv.git(args, cwd=repo)
    if not result.ok:
        raise WorktreeError("git worktree add failed for integration: {}".format(result.stderr.strip()))
    run.record_worktree(INTEGRATION_DIR, path)
    return path, branch


def run_setup(run, path, stack_profile=None):
    """Install dependencies inside a fresh worktree, if we know how."""
    profile = stack_profile
    if profile is None and pathlib.Path(run.stack_path).exists():
        profile = osenv.read_json(run.stack_path)
    command = (profile or {}).get("commands", {}).get("setup")
    if not command:
        return None
    timeout = run.config.get("setup_timeout_seconds", 1800)
    return osenv.run(command, cwd=path, timeout=timeout)


def remove(run, slice_id, delete_branch=False):
    """Remove one worktree. The branch survives unless asked otherwise.

    The branch holds the executor's commits, which the synthesizer still
    needs, so deleting it is opt-in.
    """
    repo = run.repo
    path = integration_path(run) if slice_id == INTEGRATION_DIR else path_for(run, slice_id)
    osenv.git(["worktree", "remove", "--force", str(path)], cwd=repo)
    if path.exists():
        osenv.rmtree_force(path)
    osenv.git(["worktree", "prune"], cwd=repo)
    if delete_branch:
        branch = (
            run.integration_branch if slice_id == INTEGRATION_DIR else branch_name(run.run_id, slice_id)
        )
        osenv.git(["branch", "-D", branch], cwd=repo)
    run.forget_worktree(slice_id)
    return not path.exists()


def reap(run, keep_integration=True, delete_branches=False):
    """Remove every worktree this run created. Returns the ids removed."""
    removed = []
    for slice_id in list(run.state.get("worktrees", {})):
        if keep_integration and slice_id == INTEGRATION_DIR:
            continue
        if remove(run, slice_id, delete_branch=delete_branches):
            removed.append(slice_id)
    if not run.state.get("worktrees"):
        osenv.rmtree_force(run.temp_root)
    return removed


def reap_orphans(repo):
    """Drop cod-ag worktrees git still lists but whose directory is gone.

    Called at ``init`` so a crashed earlier run cannot leave stale entries
    that make ``git worktree add`` fail later.
    """
    osenv.git(["worktree", "prune"], cwd=repo)
    pruned = []
    for path, branch in existing(repo).items():
        if branch and branch.startswith("codag/") and not path.exists():
            osenv.git(["worktree", "remove", "--force", str(path)], cwd=repo)
            pruned.append(str(path))
    if pruned:
        osenv.git(["worktree", "prune"], cwd=repo)
    return pruned


def _branch_exists(repo, branch):
    return osenv.git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], cwd=repo).ok


def head_commit(path):
    return osenv.git_out(["rev-parse", "HEAD"], cwd=path)


def commits_between(repo, base, head):
    """``base..head`` as a list of ``(sha, subject)``, oldest first."""
    result = osenv.git(["log", "--reverse", "--format=%H%x1f%s", "{}..{}".format(base, head)], cwd=repo)
    if not result.ok:
        return []
    out = []
    for line in result.stdout.splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            out.append((sha, subject))
    return out


def is_dirty(path):
    return bool(osenv.git(["status", "--porcelain"], cwd=path).out)
