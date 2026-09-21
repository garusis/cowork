# Artifact contract and schema

Use a configurable runtime root outside the target repository by default:

```text
~/.cowork/orchestrator/<repository-hash>/<package-id>/
  brief.md
  authority.json
  state.json
  events.jsonl
  escalation.json
  capacity.json
  plan.md
  result.json
  review.json
  evidence.json
  handoff.md
  digest.json
  logs/
```

Keep this directory separate from Cowork's own project-local
`.cowork/session*.json` anchor and `~/.cowork/sessions/<uuid>/` assets. The
supervisor may reference Cowork artifacts, but must not overwrite them.

## Ownership

Only the supervisor writes `state.json`, `events.jsonl`,
`digest.json`, `escalation.json`, `capacity.json`, and the accepted
authority/receipt bindings.
Append events; use atomic replacement for JSON snapshots. The event journal is
the durable transition record; CLI output is an adapter view, not authority.

Workers may propose `plan.md` and `result.json`. An independent reviewer may
write `review.json`. Validate every worker artifact before ingesting it into
controller state. Do not allow a worker to update its own budget, terminal
state, authority, or reviewer disposition.

## Required fields

Use a `schema_version` and `package_id` in every JSON artifact.

| Artifact | Required content |
| --- | --- |
| `brief.md` | objective; in/out scope; allowed paths; invariants; deterministic gates; delegated judgment/publish policy; `subscription_only` capacity policy; discovery/build/correction package limits |
| `authority.json` | base and current candidate digest; issue/decision references; immutable finding IDs; amendments; delegated capabilities/policy principal; authority status |
| `state.json` | backend (`cowork`; a package Cowork cannot run is `blocked` before dispatch); actor-neutral phase; role; controller/model identity when available; worktree; process/session ID; timestamps; last artifact hash; pause/recovery count; package-limit counters; verified provider-capacity policy and active capacity packet reference when applicable |
| `plan.md` | proposed steps, affected paths, checks, assumptions, risks, and finding mapping |
| `result.json` | candidate digest; changed paths; commands/checkpoints and exit facts; receipt references; remaining limitations; worker self-assessment |
| `review.json` | reviewed candidate digest; independent verdict; findings with severity and evidence; required corrections |
| `evidence.json` | receipt paths/hashes, command facts, artifact hashes, and verification dispositions |
| `handoff.md` | authority summary; candidate digest; unresolved findings; exact next action; remaining package limit; capacity/wakeup binding when applicable; context-expansion reason if any |
| `digest.json` | fixed-size status packet described below |
| `escalation.json` | typed authority request described below; present only while `needs_authority` is active |
| `capacity.json` | typed provider-capacity wait described below; present only while `awaiting_capacity` is active |

Require `result.json`, `review.json`, and `evidence.json` to bind to the same
candidate digest. Treat a missing or mismatched binding as `unverified`, not as
a pass.

## Gate and authority states

Use actor-neutral phase names. `awaiting_gate` means all inputs for a named
deterministic or delegated-policy gate are being collected or validated; the
supervisor must advance it automatically once it can adjudicate. Use
`needs_authority` only when a required action exceeds the currently delegated
capabilities. `awaiting_capacity` means provider capacity is unavailable. With
a trustworthy retry/reset time, the controller persists and schedules an
exact-role wake; without one, it persists a durable manual-resume condition.
Never use `awaiting_approval` as a normal workflow state.

The successful normal transition is deterministic validation → supervisor-agent
adjudication within delegated authority → `completed`. `collected` may exist
only as a short-lived evidence-ingestion checkpoint; it is not a terminal
normal-path state and does not itself require an external decision.

Every gate record in `events.jsonl` must identify the gate name, policy version,
input artifact hashes, candidate digest, adjudicator identity, disposition, and
the next state. A passing gate must be candidate-bound; a changed candidate
invalidates prior candidate-bound gate results unless the policy explicitly
allows reuse.

`awaiting_capacity` is not an authority gate and must not contain a request for
paid overage. Its event records bind exact role, provider session,
controller/model/effort/policy snapshot, and candidate; name the normalized
capacity class; record the evidence source and either a trusted reset time or a
manual-resume condition. Scheduled recovery is `awaiting_capacity ->
preflighting -> running` after a once-only wake preflight verifies every
binding. Manual recovery requires a capacity-available signal, bound to the
same packet and journaled by an authenticated external application or top-level
authority adapter. An agent-operated CLI must reject self-asserted
human principals/tokens; workers and orchestrators may neither fabricate nor
verify the signal. Only `resume-trigger` replays the turn, and only once the
signed signal is journaled (see the manual adapter in the
[self-hosting runner](bootstrap-backend.md#recovery) reference). While the
capacity lease is live, the supervisor never runs a plain launch or resume of
that session: the runtime refuses one only when the paused turn holds a pending
decision (`decision_bindings`), and otherwise a plain resume can resend the
role despite the live lease. The signal authorizes timing only, never credit, spend, or overage.
Unknown or untrustworthy metadata must stay in manual mode, never claim a reset
or schedule a speculative retry.

## Typed escalation packet

When authority is insufficient, atomically write `escalation.json`, append an
`authority_requested` event, and set the package state to `needs_authority`.
The packet must include:

```json
{
  "schema_version": 1,
  "package_id": "string",
  "capability_required": "expand_scope|accept_risk|publish_external|destructive_recovery|other",
  "target_authority": {
    "role": "policy_principal_role",
    "principal": "stable-policy-or-authority-id"
  },
  "reason": "bounded explanation",
  "policy_version": "string",
  "candidate_binding": {
    "base_digest": "sha256-or-equivalent",
    "candidate_digest": "sha256-or-equivalent",
    "artifact_hashes": {"name": "hash"},
    "finding_ids": ["immutable-id"]
  },
  "evidence_refs": [{"path": "relative-path", "sha256": "hash"}],
  "requested_amendment": "bounded proposed policy or authority change",
  "resume_token": "durable opaque token",
  "issued_at": "RFC3339 timestamp"
}
```

`resume_token` must bind the package ID, authority/policy version, candidate
digest, requested capability, and packet hash. An authority amendment must
cite that token, be journaled, and invalidate stale gate results as needed
before the supervisor resumes. A human may be the addressed top-level policy
principal or emergency override, but is not implied by this schema.

A local controller guard (including a CLI `error_max_budget_usd` result) is
not provider-capacity evidence. It cannot create a capacity packet, assert a
reset time, be raised by an agent, or become an authority escalation. It may
only end that attempt. It remains terminal under every authority amendment; an
already-authorized alternate evidence route must be a distinct new attempt.

## Typed capacity packet

When provider capacity is exhausted, atomically write `capacity.json`, append
a capacity event, and set the package state to `awaiting_capacity`. Use
`scheduled` only with trustworthy retry/reset evidence; otherwise use
`manual_signal` and wait for a capacity-available signal from an authenticated
external application/top-level authority adapter. It must include:

```json
{
  "schema_version": 1,
  "package_id": "string",
  "provider_capacity_class": "subscription_quota_exhausted",
  "provider": "string",
  "resume_mode": "scheduled|manual_signal",
  "retry_after": "RFC3339 timestamp or duration; required for scheduled",
  "capacity_source": {"kind": "provider_event|provider_header|provider_api|unknown", "sha256": "hash"},
  "binding": {
    "role": "string",
    "provider_session_id": "string",
    "controller_policy_digest": "hash",
    "candidate_digest": "hash",
    "artifact_hashes": {"name": "hash"}
  },
  "wakeup": {"lease_id": "string", "automation_ref": "durable scheduler reference", "not_before": "RFC3339 timestamp"},
  "manual_resume": {"condition": "authenticated capacity-available signal", "accepted_source": "external_application|top_level_authority_adapter", "signal_journal_ref": "required before resume"},
  "issued_at": "RFC3339 timestamp"
}
```

The wake lease is consumed once only after a binding-preserving preflight.
`manual_signal` has no wake lease. The authenticated outer adapter journals the
signal once with source identity and packet/candidate/session/policy bindings
before preflight. An agent-operated CLI command that accepts a caller-supplied
human principal or token is not an accepted source. Workers/orchestrators cannot
write or validate this event. Duplicate observers may report the wait but may
not launch another resume. While it is absent the supervisor must not use
generic launch/resume; the runtime refuses that only when the paused turn holds
a pending decision.
A failed wake or absent signal stays `awaiting_capacity` or enters a truthful
non-capacity failure state; it does not become `needs_authority` merely because
time passed.

## Finding shape

Use immutable IDs. Record at least `id`, `severity`, `status`, `candidate_digest`,
`source`, `evidence_refs`, `required_resolution`, and `opened_at`. Permit a
resolution only when it cites a newer candidate and independent evidence.

Do not close a blocker because the builder says it is addressed. Preserve the
original finding and append a disposition or replacement finding.

## Digest shape

Keep `digest.json` below 6,000 characters. Include only:

- package ID, backend (`cowork`, since a blocked package never dispatches),
  phase, and terminal/non-terminal state;
- last meaningful activity and age;
- worktree and candidate digest;
- changed-path list or count plus diff statistics;
- checkpoint/receipt outcomes, package-limit counters, and capacity state;
- unresolved blocker IDs, reviewer verdict, active gate, or required authority;
- escalation packet path/hash and resume token when `needs_authority`;
- capacity packet path/hash, reset source, and wakeup reference when
  `awaiting_capacity`;
- log/artifact paths and hashes, never full transcript content.

Emit a new digest only after validating the inputs it summarizes. Use it as the
default orchestrator read surface.
