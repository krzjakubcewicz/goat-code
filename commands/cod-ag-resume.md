---
description: Resume an interrupted cod-ag run from its ledger
argument-hint: "[run-id]"
---

Resume a cod-ag run.

1. Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/codag.py" resume $ARGUMENTS --json`.
2. **Trust that output and `git log` over anything you remember.** Slices
   the ledger marks complete are complete. Re-dispatching finished work is
   the most expensive mistake in this pipeline.
3. Invoke the `cod-ag:cod-ag-orchestrator` skill and pick up at the phase
   the resume output reports:
   - `grill` or `plan` -> step 2
   - `execute` -> step 5, dispatching only the slices `wave next` returns
   - `synthesize` -> step 6 (check the merge state first; it may be
     mid-conflict)
   - `verify` -> step 7
4. Tell the user where you are picking up and what is already done, before
   you dispatch anything.
