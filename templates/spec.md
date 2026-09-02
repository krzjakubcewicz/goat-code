# <Feature name>

<!--
A goat-code spec. You do not need to make this perfect - the planner will grill
you about whatever is still ambiguous before it writes a plan. Fill in what
you know, delete the prompts you do not need, and leave a real blank rather
than a plausible guess: a guess gets treated as settled, a blank gets a
question.

Then run:  /goat-code --spec <this file>
-->

## Goal

<!-- One or two sentences. What can a user do after this ships that they
     cannot do now? Write it from the user's side, not the code's. -->

## Requirements

<!-- What must be true when this is done. One bullet each, concrete.
     Prefer "the link expires 15 minutes after it is sent" over
     "links should expire in a reasonable time". -->

-
-

## Acceptance criteria

<!-- How you will know it works. Each one an assertion someone can check
     from the diff and the tests. Include the exact values that matter:
     literal error strings, status codes, boundaries, limits.

     Good: "Submitting the same email twice within 60s queues one email."
     Bad:  "Handles duplicate submissions gracefully." -->

-
-

## Out of scope

<!-- Say what you do NOT want built. This is the cheapest line in the file:
     every item here is a scope violation the verifier will catch instead of
     something you discover in review. -->

-

## Constraints

<!-- Project-wide rules every slice must respect. These are copied verbatim
     into the plan's global_constraints, so write exact values:
     version floors, banned dependencies, naming and copy rules, platform
     requirements, performance budgets, authz rules. -->

-

## Edge cases and failure states

<!-- Optional but high value - the planner will ask about these otherwise.
     Bad input, empty collections, network or IO failure, concurrent
     requests, permission denied, partial writes. -->

-

## Existing code to reuse or follow

<!-- Optional. Paths to the patterns, utilities or modules this should fit
     alongside. Saves the planner a lot of exploring and keeps the result
     consistent with what you already have. -->

-

## Notes

<!-- Anything else: links to designs, prior discussion, decisions already
     made and why. -->
