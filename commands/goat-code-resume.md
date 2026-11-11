---
description: Resume an interrupted goat-code run from its ledger
model: haiku
argument-hint: "[run-id]"
---

Resume a goat-code run.

1. Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/goatcode.py" resume $ARGUMENTS --json`.
2. **Trust that output and `git log` over anything you remember.** Slices
   it lists as completed are complete. Re-dispatching finished work is the
   most expensive mistake in this pipeline.
3. Tell the user where you are picking up and what is already done, before
   you dispatch anything.
4. Invoke the `goat-code:goat-code-orchestrator` skill and run its loop from
   `next`. Do not choose a phase or a slice yourself: `next` derives both
   from what is on disk, including a merge left mid-conflict.
