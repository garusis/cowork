# Backend eligibility gate

Cowork is eligible for a package only when integrity-verified, release-bound
receipts prove all six criteria below:

1. Paid dispatches preflight repository constraints, effective identity,
   artifacts, allowed actions, guard health, and runtime paths before model use.
2. Child work is blocked when ungoverned or correlated to its parent policy.
3. Success, denial, pause, abort, and process failure persist distinct outcomes.
4. Subscription capacity waits and once-only recovery use trustworthy external
   signals without credits, overage, invented resets, or same-provider repair.
5. Durable evidence distinguishes productive work, capacity wait, policy denial,
   crash, and stall without relying on terminal output.
6. Candidate-bound deterministic gates are persisted and supervisor-adjudicated;
   insufficient authority yields typed `needs_authority` with a resume token.

All receipts must identify the same release digest, be PASS, include a receipt
SHA-256, and carry a coherent validity window: both timestamps timezone-aware,
and issued strictly before expiry. A window that has already elapsed is recorded
as `validity_elapsed` and does not by itself disqualify verified immutable
evidence. Criterion 1 alone is the M1 result, not permission to use Cowork as a
generally trusted backend. The M4 milestone/global receipt proves criterion 5
only; it does not claim criteria 3/4 or the full gate, so M4 evidence alone
yields `blocked`. A future release qualifies only through its own complete,
release-bound receipt.

The compact input below is a selection summary, not a trust root. Before using
its positive result, validate that it was selected through the stable pointer
contract, that its file hash matches the pointer, that the pointer binds the
current repository HEAD/tree and accepted global adjudication, and that all six
receipt files match their recorded SHA-256 values. If any source is absent or
cannot be validated, the outcome is `blocked`.

The machine-readable input accepted by `scripts/select_backend.py` is:

```json
{
  "schema_version": 1,
  "release_digest": "64 lowercase hex characters",
  "issued_at": "ISO-8601 timestamp",
  "expires_at": "ISO-8601 timestamp",
  "criteria": {
    "1": {"status": "PASS", "receipt_sha256": "64 lowercase hex characters"},
    "2": {"status": "PASS", "receipt_sha256": "64 lowercase hex characters"},
    "3": {"status": "PASS", "receipt_sha256": "64 lowercase hex characters"},
    "4": {"status": "PASS", "receipt_sha256": "64 lowercase hex characters"},
    "5": {"status": "PASS", "receipt_sha256": "64 lowercase hex characters"},
    "6": {"status": "PASS", "receipt_sha256": "64 lowercase hex characters"}
  }
}
```

Absent files, invalid timestamps, an incoherent validity window (expiry at or
before issuance), future-issued evidence, missing criteria, non-PASS status,
malformed hashes, or extra criteria fail closed to `blocked`. Re-run selection
whenever binding inputs change.

## Selector output contract

`scripts/select_backend.py` emits `schema_version` 2:

- `backend` is exactly one of `cowork` or `blocked`. Any value other than
  `cowork` is not a routable backend and must stop the package.
- `cowork_eligible` is the same verdict as a boolean.
- `validity_elapsed` is `true` when a coherent window was parsed and has
  elapsed, `false` when a coherent window was parsed and has not, and `null`
  when no window could be parsed at all.
- `reason` is `all_backend_gate_criteria_verified` when eligible, otherwise the
  first entry of `failures`.
- `failures` lists every failed check.

The selector has no alternate backend to degrade to. A failed environmental
preflight of the runner also yields `blocked`: the package stops with that
reason rather than dispatching on an assumed PASS.

A `blocked` package stops. Only a real Cowork blocker (Cowork itself cannot run
the package) may be repaired through the `invoke-claude-agent` skill under
supervisor review, bounded to the repair and returning to Cowork afterwards.
An elapsed window, a failed gate, or a self-hosting shape never qualifies.
