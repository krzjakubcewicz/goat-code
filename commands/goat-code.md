---
description: Run the goat-code pipeline - grill, plan, parallel executors in worktrees, synthesize, verify
model: haiku
argument-hint: "\"add magic-link login\"  |  --spec docs/specs/auth.md  |  --clone <url> --spec <file>"
---

Run the goat-code feature pipeline for: $ARGUMENTS

Invoke the `goat-code:goat-code-orchestrator` skill and follow it exactly. You are
the orchestrator.

If `$ARGUMENTS` starts with `--spec`, the rest is a path to a markdown spec:
initialise with `--spec <path>`. Otherwise treat the whole of `$ARGUMENTS`
as the feature request and initialise with `--prompt`.

If `$ARGUMENTS` contains `--clone <url>`, this is a sandboxed run: pass
`--clone <url>` to `init` unchanged, along with `--spec <path>` or the rest
as `--prompt`. Do not clone anything yourself and do not `cd` into the clone;
every later command finds it from here.

If `$ARGUMENTS` is empty, ask the user what they want built before doing
anything else.
