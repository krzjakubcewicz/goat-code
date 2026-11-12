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
# The status is checked after the guard runs, which is too late for the
# launcher's stderr: it is already in the hook's output, and Claude Code shows
# that to the agent as the reason the call was blocked. A "Python not found"
# glued to the guard's refusal made agents conclude the interpreter was
# broken instead of acting on the refusal. So each candidate first runs the
# cheapest program only a real interpreter completes, output discarded.
# </dev/null matters: the payload is on stdin exactly once and belongs to the
# guard. The status check stays for a candidate that passes and is still not
# the guard.
#
# Finding no working interpreter exits 2 - block. Without one nothing can say
# whether a call stays inside the run's repository, and carrying on unguarded
# is how a run writes outside it unnoticed. The message says what is wrong so
# the agent reports it instead of retrying.
#
# `python:` in .goatcode/config.yaml beats all of that: it is how a user whose
# PATH python is wrong gets a guarded run. The repository is found the way
# repo_guard.py finds it - GOATCODE_REPO, else walk up from here - and the key
# is read with sed, since the YAML parser lives on the other side of the
# interpreter this is choosing. A configured python that does not run stops
# the run rather than falling back to PATH, which would hide the mistake.
#
# Called as `repo_guard.sh post` from PostToolUse, to record a path the user
# was asked about and approved. Almost no call was ever asked about, so that
# mode leaves before any interpreter starts unless a request is waiting.
dir=${GOATCODE_REPO:-$PWD}
while [ -n "$dir" ] && [ ! -d "$dir/.goatcode" ]; do
  # A sandboxed run keeps its clone one level down from where the session stands.
  if [ -d "$dir/repo/.goatcode" ]; then
    dir=$dir/repo
    break
  fi
  parent=$(dirname "$dir")
  [ "$parent" = "$dir" ] && dir= || dir=$parent
done
if [ "$1" = post ]; then
  [ -n "$dir" ] && [ -f "$dir/.goatcode/guard.pending" ] || exit 0
fi
config="$dir/.goatcode/config.yaml"
if [ -n "$dir" ] && [ -f "$config" ]; then
  configured=$(tr -d '\r' <"$config" | sed -n 's/^python:[[:space:]]*//p' | head -n 1 |
    sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' -e "s/^['\"]//" -e "s/['\"]\$//")
  case "$configured" in
    "" | null | "~") ;;
    *)
      if "$configured" -c '' </dev/null >/dev/null 2>&1; then
        "$configured" "$(dirname "$0")/repo_guard.py" "$@"
        status=$?
        if [ "$status" -eq 0 ] || [ "$status" -eq 2 ]; then
          exit "$status"
        fi
      fi
      echo "goat-code: python in $config ($configured) does not run the repository guard, so the run is stopped. Fix or remove that setting. Report this to the user; do not work around it." >&2
      exit 2
      ;;
  esac
fi

# `python` first: it is the name rendered commands use, so the hook and the
# commands it guards run under the same interpreter.
for interpreter in python python3 py; do
  command -v "$interpreter" >/dev/null 2>&1 || continue
  "$interpreter" -c '' </dev/null >/dev/null 2>&1 || continue
  "$interpreter" "$(dirname "$0")/repo_guard.py" "$@"
  status=$?
  if [ "$status" -eq 0 ] || [ "$status" -eq 2 ]; then
    exit "$status"
  fi
done
echo "goat-code: no working Python (python, python3 or py) was found, so the repository guard cannot run and the run is stopped. Install Python 3.9+ and put it on PATH, then restart. Report this to the user; do not work around it." >&2
exit 2
