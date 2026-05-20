# Co-plan

Co-plan is a personal planning skill for running a two-agent planning workflow
through a shared Markdown chat file. It keeps planning separate from
implementation, records the conversation as an append-only audit trail, and
produces a current implementation plan that can be reviewed and signed off.

This repository hosts the skill as it is currently used. It is not yet packaged
or generalized for other users. The workflow intentionally refers to Marcos as
the human decision maker, and some goal-intake paths expect a ClickUp MCP server
when a ClickUp ticket is referenced.

## What It Does

- Creates a shared co-plan chat file with a `### [goal]` section.
- Coordinates two planning roles, `planner` and `advisor`, through deterministic
  turn-taking.
- Maintains a current plan file beside the chat file.
- Tracks open questions in a sidecar JSON ledger.
- Blocks consensus while material questions are unanswered.
- Requires planner and advisor to propose the same plan-file hash before
  sign-off.
- Records Marcos sign-off before writing final consensus.
- Enforces that the skill is planning-only: agents may read and plan, but must
  not implement code while using this workflow.

## Repository Layout

```text
.
|-- SKILL.md
|-- README.md
`-- scripts
    |-- co_plan_file.py
    `-- test_co_plan_file.py
```

`SKILL.md` contains the agent-facing workflow instructions.

`scripts/co_plan_file.py` is the deterministic helper used by the skill to
create chats, post turns, manage the question ledger, validate plan readiness,
and record sign-off.

`scripts/test_co_plan_file.py` contains the helper test suite.

## Requirements

- Python 3.10 or newer.
- No third-party Python packages are required.
- A skill-capable agent environment that can load `SKILL.md`.
- Optional: a ClickUp MCP server if the goal references ClickUp tickets.
- Optional manual installs: [`rtk`](https://github.com/rtk-ai/rtk) and
  [`caveman`](https://github.com/JuliusBrussee/caveman).

## Install

Clone this repository into the local skills directory used by the agent
environment:

```bash
git clone https://github.com/garusis/co-plan.git ~/.claude/skills/co-plan
```

If the skill is already present locally, update it with:

```bash
cd ~/.claude/skills/co-plan
git pull
```

## User Commands

The skill is invoked with `/co-plan` commands:

```text
/co-plan init [chat-file-path]
/co-plan planner [chat-file-path]
/co-plan advisor [chat-file-path]
/co-plan resolve "<decision text>" [chat-file-path]
/co-plan answer <Q-id> "<answer text>" [chat-file-path]
/co-plan signoff [chat-file-path]
```

When no chat file is provided, the default is `./agent-chat.md`.

## Generated Files

For a chat file named `agent-chat.md`, co-plan uses these files:

```text
agent-chat.md
agent-chat.plan.md
agent-chat.md.state.json
agent-chat.md.lock/
```

`agent-chat.md` is the append-only planning chat.

`agent-chat.plan.md` is the current implementation plan. It is rewritten during
planning and should contain only the latest plan.

`agent-chat.md.state.json` stores ledger questions, proposal hashes, sign-off
state, planner/advisor activity, and derived readiness checks.

`agent-chat.md.lock/` is a temporary lock directory used by the helper while it
updates files.

## Workflow

1. Initialize a chat with a planning goal.
2. Run the planner role. The first planner turn creates the plan file.
3. Run the advisor role. The advisor critiques the current plan file and scope
   coverage.
4. Ask material questions through the question ledger whenever an unresolved
   choice could change scope, behavior, data shape, rollout risk, or success
   criteria.
5. Answer ledger questions with `/co-plan answer`.
6. Continue planner and advisor turns until both roles agree the plan is ready.
7. Each role proposes consensus against the current plan file.
8. Marcos signs off.
9. The planner writes final consensus.

## Plan File Requirements

The final plan must include these sections:

```markdown
# <short plan title>

## Summary
## Goal Coverage
## Decisions
## Approach
## Implementation Changes
## Tests
## Risks and Verification
## Assumptions
```

The helper rejects consensus proposals when the plan contains unresolved
placeholder text, stale plan-history sections, open ledger questions, unknown
question references, or unanswered question references.

## Helper CLI

The skill normally calls the helper directly, but the CLI can be useful for
debugging or testing the workflow.

Initialize a chat:

```bash
python3 scripts/co_plan_file.py init --file ./agent-chat.md --goal "Plan the work."
```

Inspect chat state:

```bash
python3 scripts/co_plan_file.py inspect --file ./agent-chat.md
```

Inspect optional dependency status:

```bash
python3 scripts/co_plan_file.py deps-status
```

Inspect planner/advisor activity:

```bash
python3 scripts/co_plan_file.py status --file ./agent-chat.md
```

Ask a ledger question:

```bash
python3 scripts/co_plan_file.py question \
  --file ./agent-chat.md \
  --self planner \
  --question "Which rollout path should the plan assume?"
```

Answer a ledger question:

```bash
python3 scripts/co_plan_file.py answer-question \
  --file ./agent-chat.md \
  --id Q1 \
  --answer "Use the lower-risk staged rollout."
```

Validate a plan:

```bash
python3 scripts/co_plan_file.py validate-plan \
  --file ./agent-chat.md \
  --plan-file ./agent-chat.plan.md
```

## Development

Run the test suite with:

```bash
python3 -m unittest scripts/test_co_plan_file.py
```

The tests exercise the helper through subprocess calls to match how agents use
it in practice.

## Optional rtk and caveman

Co-plan can use `rtk` and `caveman` when they are already installed, but it does
not install or initialize them.

- If `rtk` is available, planner/advisor agents must prefer `rtk`-wrapped shell
  exploration commands where practical.
- If `caveman` is available, planner/advisor back-and-forth should use terse
  caveman style while preserving technical accuracy.
- If either tool is missing, co-plan continues normally.
- Marcos-facing messages stay normal: escalations, ledger answers, resolves,
  sign-off recaps, final consensus, and direct user status.

Use `deps-status` to see what co-plan detects:

```bash
python3 scripts/co_plan_file.py deps-status
```

## Operating Rules

- Treat the chat file as append-only after initialization.
- Do not edit the sidecar state file manually.
- Do not bypass the helper when posting turns, proposals, answers, or sign-off.
- Do not implement code while operating inside a co-plan session.
- Keep the plan file focused on the current plan, not prior revisions or chat
  summaries.
