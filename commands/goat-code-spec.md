---
description: Scaffold a goat-code spec file you can hand to the pipeline
argument-hint: "[feature name]"
---

Create a spec file for: $ARGUMENTS

1. Read `${CLAUDE_PLUGIN_ROOT}/templates/spec.md`.
2. Choose the path: `docs/specs/<kebab-case-name>.md` in the current repo,
   creating the directory if needed. If a file is already there, ask before
   overwriting.
3. Fill in whatever you can infer from `$ARGUMENTS` and from a quick look at
   the repository - the goal, the obvious requirements, and the constraints
   the stack imposes. Leave the rest as the template's prompts so the user
   knows what to write.
4. Do not invent requirements. An honest blank is better than a plausible
   guess the planner will treat as settled.
5. Show the user the path and tell them: fill it in, then run
   `/goat-code --spec <path>`. The planner will grill them about whatever is
   still ambiguous, so they need not make it perfect.
