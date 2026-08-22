---
description: Run the cod-ag pipeline - grill, plan, parallel executors in worktrees, synthesize, verify
argument-hint: "\"add magic-link login\"  |  --spec docs/specs/auth.md"
---

Run the cod-ag feature pipeline for: $ARGUMENTS

Invoke the `cod-ag:cod-ag-orchestrator` skill and follow it exactly. You are
the orchestrator.

If `$ARGUMENTS` starts with `--spec`, the rest is a path to a markdown spec:
initialise with `--spec <path>`. Otherwise treat the whole of `$ARGUMENTS`
as the feature request and initialise with `--prompt`.

If `$ARGUMENTS` is empty, ask the user what they want built before doing
anything else.
