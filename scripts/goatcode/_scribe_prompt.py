"""Prompt text for the scribe, kept out of dispatch.py to keep that file
readable. Imported and rendered by ``dispatch.scribe``.
"""

from __future__ import annotations

INTRO = """\
The run is finished. Write it up for whoever - or whatever - works on this
project next.

This is not a changelog. Git already records what changed. What git does not
record is what you had to find out to make the change work, and that is the
part a later run pays for twice if you leave it out.
"""

LEARNINGS = """\
## The learnings section is the point

Everything above it is recoverable from git. This part is not. Write what a
competent stranger would need to know before touching this area again:

- **Patterns.** How this codebase actually does things, where that is not
  obvious from one file. "Routes are registered in src/routes/index.ts, not
  by file convention." "Every model goes through the repository layer."
- **Gotchas.** What bit, or nearly bit, someone. "Changing the token schema
  means regenerating the client types." "The test suite needs the fixture
  server running." "Two slices both wanted to edit the registry, so it had
  to be an append-only shared path."
- **Useful context.** Where things live. "The evaluation panel is
  components/Eval.tsx." "Integration tests are the only ones that touch the
  database."

Draw them from what actually happened in this run - the reports, the
verdict, the conflicts, the cycles - not from generic advice. If a slice was
blocked, or a verdict failed, or the synthesizer had to reconcile something,
that is exactly where a learning lives.

Concrete and specific. "Be careful with state" helps nobody; "the store is
reset between tests by beforeEach in setup.ts, so a test that sets it in
describe() will leak" does.

Three to six bullets. If the run was genuinely uneventful, say so in one
line rather than padding - an honest short entry beats invented insight.
"""
