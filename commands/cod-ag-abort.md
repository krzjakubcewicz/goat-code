---
description: Stop a cod-ag run and clean up its worktrees
argument-hint: "[run-id] [--delete-branches]"
---

Abort a cod-ag run.

1. Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/codag.py" status $ARGUMENTS`
   and show the user what exists: which slices finished, whether an
   integration branch has work on it.
2. Warn them if aborting would discard committed work, and confirm before
   proceeding. Branches survive by default - only `--delete-branches`
   destroys the executors' commits, so never add that flag unless the user
   asked for it explicitly.
3. Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/codag.py" abort $ARGUMENTS`.
4. Report what was removed and which branches still hold work, so the user
   can inspect or delete them later.
