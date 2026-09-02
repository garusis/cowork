---
name: cowork-refactor-orchestrator
description: Supervise bounded, agent-first engineering work packages for the Cowork workflow-refactor roadmap. Use when asked to orchestrate, prepare, launch, monitor, adjudicate gates, intervene in, collect, cancel, or dogfood a direct-Claude or Cowork-backed worker run for that refactor. Use for evidence packets, delegated-authority decisions, and recovery; do not use for ordinary Cowork CLI invocation, generic session forensics, routine coding, or a generic plan/build task.
---

# Cowork Refactor Orchestrator

Act as the durable supervisor for this agent-first refactor. The ordinary path
completes without human intervention: deterministic gates execute and
adjudicate automatically, and you resolve judgment calls inside delegated
policy. Keep architectural intent, authority, scope, and evidence separate
from worker conversations. Delegate detailed investigation, implementation,
debugging, and first-pass review to bounded workers.

Humans are optional top-level authority principals and emergency overrides,
never ordinary lifecycle actors. JSON/schema artifacts and the append-only
event journal are the control-plane interface. CLI and UI surfaces are
observability and emergency-control adapters; they must not be the only source
of phase truth or authority.

The normal path is: deterministic validation → supervisor-agent adjudication
within delegated authority → `completed`. Collection is evidence ingestion,
not an automatic pause for an external decision. A trustworthy provider
subscription-quota reset is another ordinary path: persist `awaiting_capacity`,
schedule its wake, and resume automatically. Use `needs_authority` only when
the required capability is absent from the delegated policy.

The default capacity policy is `subscription_only`: never enable or infer paid
overage. A controller-local guard such as `error_max_budget_usd` is not proof
of provider quota exhaustion or of a provider reset time. It only reports that
the package's local limit was consumed. It cannot authorize
billing. When billing mode or provider capacity is unknown, fail closed: do
not retry, do not claim a reset, and do not turn that uncertainty into paid
permission.

Treat the private issue tracker as scope/status authority. Treat the two
roadmap files in the target repo's `.cowork/` directory as sequencing guidance,
not as a replacement tracker.

## Boundaries

Own these decisions:

- select one work package and its success criteria;
- automatically adjudicate deterministic gates and policy-bounded plan,
  correction, and review decisions;
- choose backend, budget, isolation, and intervention;
- preserve findings, amendments, receipts, and candidate identity;
- decide whether evidence satisfies the package's delegated acceptance policy;
- emit a typed escalation only when the needed authority is not delegated.

Do not write the detailed implementation plan, make production edits, relay
raw terminal transcripts, or run an unbounded correction loop. Let workers do
those jobs.

Do not mistake a worker or reviewer assertion for a gate result. Bind every
gate to evidence and a candidate. Publication actions (commit, push, pull
request, external notification) are capability-controlled actions, not
intrinsically forbidden. A package with `no-publish` authority must not commit,
push, open a pull request, or publish externally; a later package may perform a
candidate-bound publication action only when its policy explicitly grants it.

Use `cowork-cli` only for an eligible Cowork backend launch or report. Use
`cowork-debug` only after compact evidence shows an inconsistency that requires
forensics. Do not duplicate either skill's command recipes or log-reading
workflow.

## Work-package lifecycle

Use one package for one coherent, independently reviewable outcome. Do not
combine a roadmap milestone with unrelated cleanup.

### Prepare

1. Refresh the relevant issue(s), roadmap milestone, and current repository
   state.
2. Create an isolated worktree and a durable package directory outside the
   target working tree by default.
3. Write a brief with objective, allowed and excluded paths, invariants,
   deterministic gates, delegated judgment/publish policy, authority links,
   and separate discovery/build/correction package limits, plus the verified
   provider capacity policy. Limits bound a package; they never grant credit,
   spending, overage, or permission to amend a limit.
4. Create an authority record that freezes the base/candidate identity and
   records findings or amendments.
5. Use `cowork-orchestrate` to validate the current release-bound gate and
   select the backend. A previous milestone number is never eligibility proof.
   Use the [self-hosting fallback](references/bootstrap-backend.md) when the
   package changes the gate, dispatch, phase-truth, guard, or recovery mechanism
   being relied on, or when current gate evidence is absent or invalid. Read
   [Cowork backend gate](references/cowork-backend-gate.md) for an eligible
   Cowork package.

Use a deterministic controller helper for `prepare` when it exists. Until the
helper exists, perform its filesystem and process steps explicitly and record
the same artifacts. Never make a worker responsible for controller state.

### Launch

Launch one bounded role at a time: investigator or planner, then builder, then
an independent reviewer. Give each worker the brief, relevant authority,
targeted artifacts, and a result schema—not previous chat transcripts. The
supervisor advances an `awaiting_gate` package automatically when all required
evidence validates and delegated policy permits the next transition.

Persist the exact backend, role, model/controller identity when available,
worktree, process/session identifier, start time, package limit, and
verified provider capacity policy before dispatch.
Reject an out-of-scope expansion; create a new or amended package instead.

Use a deterministic controller helper for `launch` when available. It must
record process lifecycle and must not silently reuse a different role or
candidate.

### Status and inspect

Use `status` for mechanical facts: live/dead/paused state, last meaningful
activity, artifact freshness, package-limit/capacity state, and checkpoint state. Use `inspect` for
judgment: the targeted diff, evidence receipts, unresolved blockers, and the
reviewer verdict.

Read the fixed-size digest first. Read a targeted artifact or diff only when it
can change a decision. Do not ingest raw worker/controller logs as normal
status. Escalate to `cowork-debug` only for a mismatch among persisted state,
Cowork artifacts, and the compact digest.

### Intervene

Intervene only for a recorded reason: failed checkpoint, blocked finding,
scope violation, provider/guard failure, silence/dead process, reviewer
dissent, package-limit threshold, or an explicit question.

Append a compact handoff containing the current candidate digest, authority,
unresolved findings, exact next action, remaining package limit, and
capacity/wakeup binding when applicable. Start a fresh
correction context by default. Resume an exact failed role only when recovery
state proves that the role, phase, model policy, and candidate are preserved.

Limit a package to one bounded builder correction before independent review or
a delegated-policy decision. Do not send a third same-provider retry after a
provider-health failure. For a verified subscription quota with trustworthy
retry/reset metadata, write `awaiting_capacity` and schedule exactly one
candidate-bound wake instead of consuming a repair turn. Without trustworthy
reset metadata, persist a durable manual-resume condition and wait for an
authenticated capacity-available signal from an external application or
top-level authority adapter. The fallback CLI must not accept a self-asserted
human identity, principal, or token, and the worker/orchestrator must never
fabricate or verify that signal. Until such an adapter journals it, remain
waiting and block generic launch/resume. The signal controls timing only; it
never changes credits, spending, or provider entitlement.

### Collect

Require a candidate-bound result, independent review, changed-path list,
verification receipts, unresolved findings, and known limitations. Validate
their schema and hashes before creating the final digest.

Return only the final evidence packet and an actor-neutral adjudication:
`completed`, `needs_correction`, `awaiting_gate`, `awaiting_capacity`,
`needs_authority`, `blocked`, or `cancelled`. Complete automatically only when deterministic gates pass and
the delegated policy permits it. Do not turn an accepted worker review into a
completion result without independent evidence validation.

### Cancel

Use `cancel` for a cancellation-capability invocation, an unrecoverable policy
violation, repeated identical recovery failure, or invalid authority. Stop the
worker safely, preserve artifacts/log references, write a terminal reason, and
leave the worktree unchanged except for the worker's existing uncommitted edits.
Never erase a worktree or session artifacts automatically.

## Backend selection

Select the backend from current release-bound evidence through
`cowork-orchestrate`; do not route by roadmap milestone number. Even after a
global PASS, use `direct-claude` for a self-hosting package whose changes would
invalidate or circularly rely on Cowork's own dispatch, phase-truth, backend
gate, guard, or recovery contract. Use separate fresh worker sessions for
planning, building, and review, and treat their output as advisory until the
supervisor validates it.

Use Cowork only after its backend gate passes for the task. Launch it through
the established `cowork-cli` skill with an explicit context file and saved
session anchor. Begin with one package/worktree and the smallest appropriate
profile; do not make a static six-role topology the default.

Read [Cowork backend gate](references/cowork-backend-gate.md) before using
Cowork and [self-hosting fallback](references/bootstrap-backend.md) before a
direct-Claude self-hosting package.

## Wake and escalation rules

Wake for a plan/review packet, failed required checkpoint, changed candidate,
out-of-scope write, guard or provider failure, silence/dead process,
package-limit threshold, reviewer disagreement, scope request, capacity
lease expiry, or final collection packet.

For long-running work, create a 15-minute recurring heartbeat unless durable
`next_inspection_at` requires a later wake. At each wake, read `digest.json` and
`state.json` first, record when either is absent, and query each active work
exactly once. Do not keep the turn open, poll, tail logs, or re-query unchanged
work. End silently when every active package is healthy and non-terminal;
report only a material anomaly, genuine authority need, or terminal result.

Treat an event tail as a signal, not proof of health. Inspect persisted
artifact state and the targeted diff at each wake. When scope, authority, risk
profile, controller policy, or a
destructive recovery action exceeds delegated policy, transition to
`needs_authority` and emit the typed escalation packet defined in [artifact
contract and schema](references/artifact-contract-schema.md). A verified
provider quota reset is not an authority event: transition to
`awaiting_capacity` with the typed capacity packet and wakeup reference. A
local safety guard must not be relabeled as provider capacity. Guard exhaustion
is neither a capacity signal nor an authority request: do not resume it, raise
it, or reinterpret it as permission to spend. It is terminal under every
authority amendment. Finish through an already-authorized alternate
evidence/backend route as a new attempt, or fail closed.
Do not use `awaiting_approval` or human-shaped normal-path states.

## Context and output limits

- Keep a brief at or below 6,000 words; keep an authority/handoff at or below
  2,000 words; keep a status digest at or below 6,000 characters.
- Store full logs on disk and place only paths, hashes, timestamps, and
  exceptional excerpts in the digest. Do not persist credentials or raw prompts
  in it.
- Send changed paths plus diff statistics first; load a file-level diff only
  for a decision.
- Record every context expansion with reason and package limit. Alert near 70%
  of a limit; stop before it is exceeded. Limits are immutable for an attempt
  and never appear in an authority escalation.
- Keep discovery, implementation, and correction limits distinct. Do not let
  a correction silently replay discovery or planning context.

Read [artifact contract and schema](references/artifact-contract-schema.md)
when creating, validating, or repairing package artifacts.
