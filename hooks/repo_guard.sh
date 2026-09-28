#!/bin/sh
# Find an interpreter and hand the hook payload to repo_guard.py.
#
# Not a bare `python3` in hooks.json, the way most plugins wire this up:
# python3 does not exist on Windows, py does not exist anywhere else, and
# goat-code runs on three platforms or it does not run.
#
# `command -v` only proves a name is on PATH, not that it runs Python. The
# Microsoft Store app-execution alias and a pyenv shim with no version for
# this directory both resolve and then exit non-zero with a message, which
# Claude Code reports as a failed hook on every single tool call. So the
# status is what decides: repo_guard.py answers 0 (allow) or 2 (block) and
# nothing else, because it fails open. Any other status came from the
# launcher rather than the guard - try the next name.
#
# Finding no working interpreter exits 0 - allow. A guard that cannot start
# must not block the run it was meant to guard.
for interpreter in python3 python py; do
  command -v "$interpreter" >/dev/null 2>&1 || continue
  "$interpreter" "$(dirname "$0")/repo_guard.py"
  status=$?
  if [ "$status" -eq 0 ] || [ "$status" -eq 2 ]; then
    exit "$status"
  fi
done
exit 0
