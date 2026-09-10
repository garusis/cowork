---
name: cowork-orchestrate
description: Route an engineering request into a bounded work package, decide from integrity-verified backend-gate evidence whether it runs on Cowork or stops with an explicit blocked outcome, and supervise the package lifecycle. Use for delegated implementation, planning, review, or recovery that needs explicit authority, candidate-bound gates, and fail-closed routing; use cowork-cli for ordinary Cowork command mechanics and cowork-debug for session forensics.
---

# Cowork Orchestrate

Turn the request into one bounded package before dispatch. Record the objective,
base/candidate identity, allowed paths and actions, acceptance gates, provider and
spend policy, and stopping/recovery conditions. Preserve the user's authority;
do not infer permission to publish, merge, spend, or widen scope.

## Select the backend first

Read [references/backend-gate.md](references/backend-gate.md), then evaluate the
release-bound receipts with `scripts/select_backend.py`. Accept Cowork only when
all six criteria are present, PASS, integrity-verified, carrying a coherent
validity window, and bound to one release digest. Missing, malformed, partial,
incoherent, or conflicting evidence yields `blocked` with an explicit reason.
The selector has no second backend: never silently re-route, and never treat an
M1 criterion-1 receipt as full eligibility. A validity window that has merely
elapsed does not by itself disqualify otherwise verified immutable evidence; it
is reported as `validity_elapsed`.

Resolve the compact selector manifest from the stable repository pointer
described in [backend-gate pointer](references/backend-gate-pointer.md), or use
an explicitly supplied, independently accepted manifest. The selector checks
the compact manifest; the supervisor must also validate the pointer, manifest
hash, current HEAD/tree binding, global adjudication, and referenced receipt
hashes before accepting Cowork. A plausible JSON object with invented hashes is
not evidence.

The M4 milestone/global receipt proves criterion 5 only; it does not claim
criteria 3/4 or the full gate. Treat an M4-only receipt as partial evidence,
which yields `blocked`. Future releases qualify only through their own complete,
release-bound receipt passing this selector.

Use the routing outcome for this package only. Re-evaluate after a release,
policy, candidate, configuration, or gate-evidence change.

## Authorized exception

Cowork is the default and `blocked` is the routing outcome.
Only a real Cowork blocker (Cowork itself cannot run the package) may be
repaired through the `invoke-claude-agent` skill — a Claude session run outside
Cowork. That exception is supervisor-reviewed, is never chosen by the selector
or by a worker, is bounded to the repair, and returns to Cowork once the
blocker is repaired.
An elapsed window, a failed gate, or a self-hosting shape never qualifies.

## Run and supervise

- Before dispatch, run the real environmental preflight of the runner
  (`cowork --check`, per the `cowork-cli` skill). A failed environmental
  preflight yields `blocked` — never a re-route and never an assumed PASS.
- On `blocked`, stop the package with the named reason and do not dispatch. A
  blocked package never degrades into another backend; the only way out is the
  supervisor-reviewed repair route above, and only for a real Cowork blocker.
- For `cowork`, read and follow the `cowork-cli` skill for non-interactive launch,
  worktree, session, resume, and report mechanics. Do not duplicate those recipes.
- Keep deterministic gates supervisor-owned and candidate-bound. Collect once,
  execute each gate once per candidate, use the authorized reviewer policy, and
  persist the fixed-gate decision.
- For long-running work, schedule a 15-minute recurring wake unless durable
  state requests a later inspection. Query each active work once per wake,
  beginning with compact state/digest; end silently when healthy and
  non-terminal. Do not add a polling loop or normal-path log tail.
- On a typed capacity wait, preserve phase, candidate, policy, session identity,
  and completed evidence. Resume only from a trustworthy once-only signal.
- On failure, preserve evidence and use only the authorized correction/recovery
  path. Stop for `needs_authority` rather than widening scope.
- Use the `cowork-debug` skill only when artifacts, trace, session identity, or
  status conflict; it owns forensic reconstruction.

Report the routing outcome and reason, package state, accepted candidate,
gate/reviewer outcome, and any remaining authority or eligibility gap.
