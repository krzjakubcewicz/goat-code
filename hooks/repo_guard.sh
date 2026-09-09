#!/bin/sh
# Find an interpreter and hand the hook payload to repo_guard.py.
#
# Not a bare `python3` in hooks.json, the way most plugins wire this up:
# python3 does not exist on Windows, py does not exist anywhere else, and
# goat-code runs on three platforms or it does not run. `exec` keeps stdin,
# which is where the payload is.
#
# Finding no interpreter at all exits 0 - allow. A guard that cannot start
# must not block the run it was meant to guard.
for interpreter in python3 python py; do
  if command -v "$interpreter" >/dev/null 2>&1; then
    exec "$interpreter" "$(dirname "$0")/repo_guard.py"
  fi
done
exit 0
