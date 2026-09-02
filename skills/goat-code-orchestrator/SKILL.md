---
name: goat-code-orchestrator
description: >
  Runs the goat-code feature pipeline end to end - grill the user, plan into
  vertical slices, dispatch parallel executors in isolated worktrees,
  synthesize, verify, and either report DONE or replan. Use when the user
  asks to implement a feature with goat-code, invokes /goat-code, or asks to
  resume or continue a goat-code run.
---

# goat-code Orchestrator

**You are the orchestrator** - you, the main thread, not a subagent. Only
you can spawn agents and ask the user questions.

You do **not** decide what happens next. `goatcode next` does. It reads the run
state off disk and returns one action; you perform it and call `next` again.
Phase transitions, model choice, retry and escalation policy, every cap, and
the exact text each agent receives are decided in Python and covered by
tests. Your job is to invoke the tool each action names, faithfully.

Announce once: "Running the goat-code pipeline." Then work.

`goat-code:goat-code-conventions` holds the artifact shapes and the CLI, if you
need them.

## Start the run

The only step outside the loop:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/goatcode.py" init --prompt "<the user request>"
python "${CLAUDE_PLUGIN_ROOT}/scripts/goatcode.py" init --spec <path>
```

`init` refuses on a dirty tree, a detached HEAD, a repo with no commits, or
a linked worktree. Relay the message; do not work around it. `--force`
exists, but tell the user what they are overriding.

## The loop

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/goatcode.py" next --json
```

Perform the action it returns, then run `next` again. Repeat until `stop`.

| `action` | What you do |
| --- | --- |
| `run` | Execute each argv in `commands`, exactly as given. |
| `dispatch` | Spawn every entry in `dispatches` **in a single message**, using its `agent` as the subagent type and its `model`. |
| `ask` | Put `ask.questions` to the user with `AskUserQuestion`, then record the result with `ask.record`. |
| `escalate` | Something needs a human. Show `message` and stop. |
| `stop` | Print `message` verbatim and finish. |

That is the whole pipeline. The rest of this file is detail on the actions
that are not obvious.

### dispatch

All entries go out in **one message**. One per message runs them in sequence
and throws away the parallelism the design exists for.

**Use each entry's `model` verbatim.** It is not a suggestion and not a
default to improve on: the machine resolved it from the run's config and its
escalation policy, and it is recorded in the ledger as the model this
dispatch ran on. Substituting a stronger one silently spends several times
the tokens the run was budgeted, and makes the escalation counters describe
something that did not happen. If a slice genuinely needs a stronger model,
that is what `BLOCKED` is for - the machine escalates it and says so.

Each prompt file already contains everything the agent needs, including the
command it runs to report back. So the prompt you pass is one line:

> Read `<prompt path>` and follow it.

**Never** paste a brief, a diff, a plan, or another agent report into a
dispatch or into your own output. Those move as files; anything you print
stays in your context for the rest of the run.

Agents record their own results through the CLI. You do not parse what they
return and you do not update `tasks.yaml` - call `next` again and it reads
what they wrote.

### ask

`ask.questions` is already shaped for `AskUserQuestion`: `question`,
`header`, and `options` with `label` and `description`, with the planner
recommendation already marked. At most 4 per call.

Record the answers with `ask.record`, substituting what the user chose:

```bash
... answer Q1="15-minute timer" Q2=Both --note Q1="match the session cookie"
```

Use the label they picked. Add `--note QID=text` for anything they typed.
Omit a question they skipped - the planner records an assumption for it.

For the approval gate, `ask.kind` is `approval` and `ask.record` lists all
three commands; run the one matching their choice.

### NEEDS_CONTEXT

If an agent says it needs context, give it what it asked for and dispatch it
again with the same prompt file plus your answer. Everything else is already
handled: a `BLOCKED` slice is retried once on a stronger model, then the run
moves on without it.

### stop

Print `message` verbatim. On a successful run, also execute the `finish`
command in the action, then tell the user in one short paragraph what was
built, which acceptance criteria the verifier confirmed, any assumptions it
surfaced, and any pre-existing failures the run did not cause.

Never claim success when `outcome` is `failed`.

## Resuming

After an interruption or a context compaction, just run `next`. The phase is
derived from what is on disk, so it is right regardless of what you
remember. Anything the ledger records as done **is** done.

`resume --json` prints the same picture in human terms if you want to tell
the user where you are picking up.

## Narration

One short line per step. The ledger and the run directory are the record.

## Never

- Dispatch a wave one agent per message.
- Paste briefs, diffs, plans or reports into a prompt or your own output.
- Decide the next phase yourself, skip a gate, or override a cap. If `next`
  says `stop`, the run is over.
- Edit `tasks.yaml`, `state.json` or any run artifact by hand.
- Merge the integration branch into the user branch. That is their call.
