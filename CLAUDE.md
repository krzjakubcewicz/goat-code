# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`goat-code` is a Claude Code **plugin** that runs a multi-agent feature
pipeline: grill → plan → parallel executors in git worktrees → synthesize →
verify → e2e → record. The plugin surface is markdown (`agents/`,
`commands/`, `skills/`); all the actual logic is a stdlib-only Python
package under `scripts/goatcode/`, driven by the CLI at `scripts/goatcode.py`.

Naming: `goat-code` is the plugin/agent/command name, `goatcode` is the Python
package, the CLI, the `.goatcode/` state directory and the `GOATCODE_*` env
prefix. Both spellings are load-bearing; don't unify them.

Read `docs/ARCHITECTURE.md` for *why* the design is shaped this way and
`docs/PIPELINE.md` for a phase-by-phase walkthrough. `skills/goat-code-conventions/SKILL.md`
is the artifact contract (run directory layout, tasks.yaml dialect, status codes).

## Commands

```bash
python -m pytest scripts/tests -v                       # full suite
python -m pytest scripts/tests/test_machine.py -v       # one file
python -m pytest scripts/tests/test_machine.py::test_name  # one test
python -m pytest scripts/tests -k classify              # by keyword
```

No build step, no lint config, no dependencies — `pip install pytest` is the
whole setup. Do **not** run the suite with `-n`/xdist; `pytest.ini` records the
measurement showing it is 5-8x slower on Windows.

Driving the pipeline itself (against a target repo):

```bash
python scripts/goatcode.py init --prompt "..."   # or --spec FILE
python scripts/goatcode.py next --json           # the state machine: one action
python scripts/goatcode.py run --prompt "..."    # the same loop, driven by Python
python scripts/goatcode.py --help
GOATCODE_DEBUG=1 python scripts/goatcode.py next # trace to runs/<id>/log.txt
```

## Architecture

**`machine.next_action(run)` is the contract.** It reads the run's files off
disk, derives the phase (`derive_phase` is pure), and returns exactly one
action: `run`, `dispatch`, `ask`, `escalate`, `stop`. `state.json` is a cache
of the evidence, not the source of truth, which is what makes resume-after-crash
work. No orchestrator — model or Python — decides anything about ordering,
caps, retries or model choice.

**Two front-ends, one loop.** `skills/goat-code-orchestrator/SKILL.md` (performed
by the Claude Code main thread) and `driver.Driver` (Python, spawning headless
`claude` processes via `agentcli`) perform the same actions from the same
machine. This is why `test_pipeline_e2e.py` can drive whole runs — happy path,
replan cycles, cycle cap, blocked-slice escalation, merge conflict — with a fake
agent and no LLM. **That suite is the proof the pipeline works; keep it green.**

**Script/judgement split.** Anything mechanical (git, worktrees, plan
validation, gates, merging, briefs, reports) lives in Python and is called by
agents through the CLI, never done by an agent directly. Agents report results
by *running a command* (`goatcode report --slice S1 --status DONE`), which is
verified — clean worktree, HEAD moved, declared test files exist — rather than
by returning prose.

**Classifier is advisory, rules are not.** `classify.apply_rules` takes the
*higher* of the model's risk and deterministic pattern rules (auth, crypto,
secrets, CI, infra, migrations, deletion); it can raise, never lower. It runs
twice — once over the request, once over the plan's path globs. `workflow.py`
is the only place routing lives: four predicates (`wants_grill`, `wants_gate`,
`wants_verifier`, `wants_approval`) over three workflows. Anything unreadable
or unknown falls to the safe middle or heavier, never to `DIRECT_DEVELOPMENT`.

Module map worth knowing: `run.py` (run dir + state), `schema.py` (tasks.yaml
validation, glob-overlap test), `miniyaml.py` (hand-written YAML subset),
`osenv.py` (the only subprocess boundary), `dispatch.py` (renders every agent
prompt), `gates.py` (build/typecheck/lint/test + baseline classification),
`stack.py` (stack detection).

## Invariants enforced by tests

Breaking one of these fails the suite, usually far from where you edited.

- **Stdlib only, Python 3.9+.** No pip install anywhere in the shipped path.
  `miniyaml` exists so there is no PyYAML.
- **No shell, one subprocess boundary.** `shell=True` never; only `osenv`
  may `import subprocess` (`test_osenv.py::test_only_osenv_calls_subprocess_directly`).
  `osenv.run` resolves `argv[0]` through `shutil.which` for Windows `.CMD` shims.
- **Markdown and code stay in sync.** `test_plugin.py` pins agent frontmatter,
  each agent's model against `templates/config.yaml` defaults *and* the README
  model table, the skills each agent may depend on, and that every
  `goatcode.py <cmd>` appearing in any markdown is a real subcommand. Adding a
  CLI command, renaming an agent, or changing a model means updating the docs
  in the same change.
- **Cross-OS.** CI runs ubuntu/macos/windows × Python 3.9 and 3.13. Worktrees
  go to `<tempdir>/goatcode/<hash>/<slice>` to stay under the Windows path
  limit; `.gitattributes` forces LF everywhere.
- **Never touch the user's repo state.** No commits to their branch, no HEAD
  moves, no pushes. `init` writes a `.gitignore` entry and leaves it uncommitted.

## Style

Prose in this repo — commit subjects, docstrings, docs — explains *why*, in
lowercase declarative sentences, and comments record the reasoning or the
measurement behind a decision rather than restating the code. Match it.
