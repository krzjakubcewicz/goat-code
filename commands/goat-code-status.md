---
description: Show what the current goat-code run is doing
argument-hint: "[run-id]  |  --all"
---

Report goat-code run state.

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/goatcode.py" status $ARGUMENTS` and
relay it: phase, cycle, slice counts, which slices are ready, and the last
few ledger entries.

If a plan exists, also run `plan show` and summarise the waves. Keep it to
what the user needs to see - do not paste the whole plan unless they ask.
