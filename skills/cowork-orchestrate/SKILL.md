---
name: cowork-orchestrate
description: Route an engineering request into a bounded work package, choose Cowork or direct Claude from current backend-gate evidence, and supervise the package lifecycle. Use for delegated implementation, planning, review, or recovery that needs explicit authority, candidate-bound gates, and fail-closed backend selection; use cowork-cli for ordinary Cowork command mechanics and cowork-debug for session forensics.
---

# Cowork Orchestrate

Turn the request into one bounded package before dispatch. Record the objective,
base/candidate identity, allowed paths and actions, acceptance gates, provider and
spend policy, and stopping/recovery conditions. Preserve the user's authority;
do not infer permission to publish, merge, spend, or widen scope.

## Select the backend first

Read [references/backend-gate.md](references/backend-gate.md), then evaluate the
current release-bound receipts with `scripts/select_backend.py`. Accept Cowork
only when all six criteria are present, PASS, unexpired, and bound to one release
digest. Missing, malformed, partial, stale, or conflicting evidence selects
`direct-claude` with an explicit reason. Never silently fall back or treat an M1
criterion-1 receipt as full eligibility.

The M4 milestone/global receipt proves criterion 5 only; it does not claim
criteria 3/4 or the full gate. Treat an M4-only receipt as partial evidence and
select `direct-claude`. Future releases qualify only through their own complete,
current release-bound receipt passing this selector.

Use the selected backend for this package only. Re-evaluate after a release,
policy, candidate, configuration, or gate-evidence change.

## Run and supervise

- For `direct-claude`, create a fresh, scoped controller session and persist the
  requested and effective model identity before accepting work.
- For `cowork`, read and follow the `cowork-cli` skill for non-interactive launch,
  worktree, session, resume, and report mechanics. Do not duplicate those recipes.
- Keep deterministic gates supervisor-owned and candidate-bound. Collect once,
  execute each gate once per candidate, use the authorized reviewer policy, and
  persist the fixed-gate decision.
- On a typed capacity wait, preserve phase, candidate, policy, session identity,
  and completed evidence. Resume only from a trustworthy once-only signal.
- On failure, preserve evidence and use only the authorized correction/recovery
  path. Stop for `needs_authority` rather than widening scope.
- Use the `cowork-debug` skill only when artifacts, trace, session identity, or
  status conflict; it owns forensic reconstruction.

Report the selected backend and reason, package state, accepted candidate,
gate/reviewer outcome, and any remaining authority or eligibility gap.
