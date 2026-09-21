---
name: cowork-orchestrate
description: Turn an engineering request into a bounded work package, run it on Cowork, and supervise it to an explicit outcome. Use for delegated implementation, planning, review, or recovery that needs explicit authority, candidate-bound acceptance, and fail-closed stopping; use cowork-cli for ordinary Cowork command mechanics and cowork-debug for session forensics.
---

# Cowork Orchestrate

Turn the request into one bounded package before dispatch. Record the objective,
base/candidate identity, allowed paths and actions, acceptance checks, provider
and spend policy, and stop/recovery conditions. Preserve the user's authority:
do not infer permission to publish, merge, spend, or widen scope, and do not ask
again for routine steps the package already authorizes.

## Transport

Cowork is the agent transport. Run the package through the `cowork` CLI per the
`cowork-cli` skill; do not duplicate its recipes.

- Before dispatch, run `cowork --check`. A failed preflight stops the package
  as `blocked` with the named missing piece; it is never an assumed pass.
- A `blocked` package stops and is reported. It never degrades into another
  transport. Run work through a direct Claude session instead only when the
  user explicitly asks for that for this work.
- Choose the smallest team and controller configuration that supplies the
  discovery, planning, implementation, and assurance the risk needs. Teams are
  paired and every new session begins with `scout` and `scout-reviewer`; there
  is no standalone planner, builder, or reviewer session.

## Run and supervise

- Treat the final JSON run-result line as the outcome. Only rc 0 with
  `approved: true` is success; a stop, capacity pause, failure, or missing line
  is not.
- rc 4: answer a stop only within delegated authority, using the request-bound
  decision flags. A scope, risk, publication, or spending decision beyond it
  becomes `needs_authority` for the user.
- rc 5: preserve phase, candidate, policy, session identity, and completed
  evidence; resume only through the capacity `resume-trigger` path.
- rc 1/3/17: preserve evidence, then use only an authorized recovery (plain
  resume after fixing a cause, `--switch-controller`, or an authorized
  takeover). Never retry blindly or invent a reset, provider switch, or spend.
- For long-running work, schedule a 15-minute recurring wake unless durable
  state requests a later inspection. Query each active work once per wake from
  compact state; end silently when healthy and non-terminal. Do not add a
  polling loop or normal-path log tail.
- Use `cowork-debug` only when the run result, artifacts, trace, session
  identity, or status conflict.

## Accept

Cowork commits nothing. After an approved build, review the working-tree diff
against the package scope yourself and run verification proportional to the
risk: focused checks for a narrow change, broader suites when shared behavior
moved. Bind acceptance to the exact candidate you reviewed; a changed candidate
needs its affected checks re-run.

Tests that protect product behavior belong in the repository. Package and
milestone evidence (receipts, run results, review notes) stays outside Git
unless the user asks otherwise.

Report the package state, run outcome and reason, accepted candidate, checks
run and their results, and any remaining authority gap or unverified risk.
