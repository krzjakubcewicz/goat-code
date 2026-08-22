---
description: Show what the current cod-ag run is doing
argument-hint: "[run-id]  |  --all"
---

Report cod-ag run state.

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/codag.py" status $ARGUMENTS` and
relay it: phase, cycle, slice counts, which slices are ready, and the last
few ledger entries.

If a plan exists, also run `plan show` and summarise the waves. Keep it to
what the user needs to see - do not paste the whole plan unless they ask.
