"""OS abstraction: git invocation, temp roots, force-delete, atomic writes.

Named ``osenv`` rather than ``platform`` so it never shadows the stdlib
``platform`` module on the import path.

Rules enforced here (see docs/ARCHITECTURE.md "Cross-OS guarantees"):
    - subprocess is always called with an argument list, never ``shell=True``
    - every path is a ``pathlib.Path``; no string joins, no separators
    - git runs with long-path support on Windows and no optional locks
    - deleting a worktree survives read-only files and antivirus locks
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time

IS_WINDOWS = os.name == "nt"

#: Extra ``-c`` settings applied to every git invocation.
_GIT_CONFIG = ["-c", "core.quotepath=false"]
if IS_WINDOWS:
    _GIT_CONFIG += ["-c", "core.longpaths=true"]


class CommandError(RuntimeError):
    """A subprocess exited non-zero and the caller asked us to care."""

    def __init__(self, argv, returncode, stdout, stderr):
        super().__init__(
            "command failed ({}): {}\n{}".format(returncode, " ".join(argv), stderr.strip() or stdout.strip())
        )
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Result:
    """Captured output of one subprocess run."""

    __slots__ = ("argv", "returncode", "stdout", "stderr", "duration")

    def __init__(self, argv, returncode, stdout, stderr, duration):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration

    @property
    def ok(self):
        return self.returncode == 0

    @property
    def out(self):
        """stdout with trailing whitespace removed - the common case."""
        return self.stdout.strip()

    def as_dict(self):
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 3),
        }


def resolve_exe(name):
    """Absolute path to an executable, or None.

    Necessary on Windows: ``npm``, ``pnpm``, ``yarn`` and friends ship as
    ``.CMD`` shims, and without a shell ``CreateProcess`` cannot find or
    launch a bare ``npm``. ``shutil.which`` honours ``PATHEXT`` and returns
    the shim's real path, which does launch.
    """
    name = str(name)
    if pathlib.Path(name).is_absolute():
        return name if pathlib.Path(name).exists() else None
    return shutil.which(name)


def run(argv, cwd=None, check=False, timeout=None, env=None):
    """Run ``argv`` with no shell, capturing UTF-8 output."""
    argv = [str(a) for a in argv]
    if argv:
        resolved = resolve_exe(argv[0])
        if resolved:
            argv[0] = resolved
    merged = dict(os.environ)
    merged["GIT_OPTIONAL_LOCKS"] = "0"
    merged.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        merged.update({str(k): str(v) for k, v in env.items()})
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            shell=False,
            timeout=timeout,
            env=merged,
        )
    except FileNotFoundError:
        raise CommandError(argv, 127, "", "executable not found: {}".format(argv[0]))
    except subprocess.TimeoutExpired as exc:
        out = _decode(exc.stdout)
        err = _decode(exc.stderr)
        result = Result(argv, 124, out, err + "\ntimed out after {}s".format(timeout), time.time() - started)
        if check:
            raise CommandError(argv, 124, result.stdout, result.stderr)
        return result
    result = Result(
        argv,
        proc.returncode,
        _decode(proc.stdout),
        _decode(proc.stderr),
        time.time() - started,
    )
    if check and not result.ok:
        raise CommandError(argv, result.returncode, result.stdout, result.stderr)
    return result


def _decode(blob):
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    return blob.decode("utf-8", errors="replace")


def git(args, cwd=None, check=False, timeout=None):
    """Run git with the cod-ag standard config applied."""
    return run(["git"] + _GIT_CONFIG + list(args), cwd=cwd, check=check, timeout=timeout)


def git_out(args, cwd=None):
    """Run git, require success, return stripped stdout."""
    return git(args, cwd=cwd, check=True).out


def repo_root(start=None):
    """Absolute path of the enclosing git work tree, or None.

    Inside a linked worktree this returns the *worktree*, not the repository
    it belongs to. Use :func:`main_repo_root` when you need the place
    ``.codag/`` lives.
    """
    result = git(["rev-parse", "--show-toplevel"], cwd=start or pathlib.Path.cwd())
    if not result.ok:
        return None
    return pathlib.Path(result.out).resolve()


#: Resolved main work trees, keyed by the path we were asked about. Locating
#: the repository takes two git subprocesses, and every CLI command needs it
#: at least once - the orchestrator loop calls `next` over and over, and each
#: executor shells out to `report`. Keyed by path, so a test process handling
#: many repositories still gets the right answer for each.
_MAIN_ROOTS = {}


def clear_repo_cache():
    """Forget resolved roots. For a test that moves a repo under our feet."""
    _MAIN_ROOTS.clear()


def main_repo_root(start=None):
    """Absolute path of the *main* work tree, even from a linked worktree.

    Executor agents run inside their own worktree but must reach the run
    state in the main repository's ``.codag/``. ``--show-toplevel`` returns
    the worktree there, so it cannot be used; ``--git-common-dir`` points at
    the main ``.git``, whose parent is the real root.

    Memoised: the answer cannot change within one CLI invocation, and
    resolving it costs two subprocesses.
    """
    key = str(start) if start else str(pathlib.Path.cwd())
    if key in _MAIN_ROOTS:
        return _MAIN_ROOTS[key]
    resolved = _resolve_main_repo_root(start)
    _MAIN_ROOTS[key] = resolved
    return resolved


def _resolve_main_repo_root(start=None):
    where = pathlib.Path(start) if start else pathlib.Path.cwd()
    result = git(["rev-parse", "--git-common-dir"], cwd=where)
    if not result.ok:
        return None

    common = pathlib.Path(result.out)
    if not common.is_absolute():
        common = where / common
    try:
        common = common.resolve()
    except OSError:
        return repo_root(where)

    if common.name == ".git":
        candidate = common.parent
        # A submodule's common dir is <super>/.git/modules/<name>, so the
        # name check above already excludes one. When we are standing inside
        # the candidate, that settles it - no second subprocess needed, and
        # this is the common case for every CLI invocation.
        try:
            if where.resolve().is_relative_to(candidate):
                return candidate
        except (OSError, AttributeError):
            pass
        check = git(["rev-parse", "--show-toplevel"], cwd=candidate)
        if check.ok and pathlib.Path(check.out).resolve() == candidate:
            return candidate
    return repo_root(where)


def in_linked_worktree(start=None):
    """True when ``start`` is inside a linked worktree rather than the main one."""
    where = pathlib.Path(start) if start else pathlib.Path.cwd()
    here = repo_root(where)
    main = main_repo_root(where)
    return here is not None and main is not None and here != main


def temp_root():
    """Short, writable base directory for worktrees.

    Deliberately outside the repository: Windows caps paths at 260
    characters by default and a deep ``node_modules`` inside a repo-local
    worktree blows straight through it.
    """
    override = os.environ.get("CODAG_TEMP_ROOT")
    base = pathlib.Path(override) if override else pathlib.Path(tempfile.gettempdir())
    root = base / "codag"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_slug(run_id):
    """Stable 8-char directory name for a run, keeping temp paths short."""
    return hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:8]


def _on_rm_error(func, path, _exc):
    """shutil.rmtree handler: clear the read-only bit and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def rmtree_force(path):
    """Delete a tree, tolerating read-only files and transient locks.

    Returns True when the path is gone. Antivirus and editor handles on
    Windows can hold files open for a moment, so we retry with a short
    backoff before giving up.
    """
    path = pathlib.Path(path)
    for attempt in range(4):
        if not path.exists():
            return True
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=lambda f, p, e: _on_rm_error(f, p, e))
            else:
                shutil.rmtree(path, onerror=_on_rm_error)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.ENOTEMPTY, errno.EBUSY, errno.EPERM):
                raise
        if not path.exists():
            return True
        time.sleep(0.2 * (attempt + 1))
    return not path.exists()


def write_text(path, text):
    """Atomically write UTF-8 text with LF endings."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp{}".format(os.getpid()))
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(path))


def read_text(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


def read_json(path):
    return json.loads(read_text(path))


class FileLock:
    """Cross-platform advisory lock built on atomic directory creation.

    ``os.mkdir`` is atomic on every filesystem we care about, which makes it
    a safer primitive than ``fcntl``/``msvcrt`` when several executor agents
    mutate tasks.yaml from separate processes.
    """

    def __init__(self, target, timeout=30.0, poll=0.05):
        self.path = pathlib.Path(str(target) + ".lock")
        self.timeout = timeout
        self.poll = poll
        self._held = False

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.path.mkdir(parents=True)
                self._held = True
                return self
            except FileExistsError:
                if self._stale():
                    rmtree_force(self.path)
                    continue
                if time.time() > deadline:
                    raise TimeoutError("timed out waiting for lock {}".format(self.path))
                time.sleep(self.poll)

    def _stale(self):
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age > max(self.timeout * 2, 60)

    def __exit__(self, *_exc):
        if self._held:
            rmtree_force(self.path)
            self._held = False
        return False


def require_python():
    """Fail loudly and usefully on an unsupported interpreter."""
    if sys.version_info < (3, 9):
        sys.stderr.write(
            "cod-ag needs Python 3.9 or newer; this is {}.{}.{}\n".format(*sys.version_info[:3])
        )
        raise SystemExit(2)
