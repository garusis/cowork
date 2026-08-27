# Backend eligibility gate

Cowork is eligible for a package only when current, release-bound receipts prove
all six criteria below:

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
SHA-256, and be within their validity window. Criterion 1 alone is the M1 result,
not permission to use Cowork as a generally trusted backend. The M4
milestone/global receipt proves criterion 5 only; it does not claim criteria 3/4
or the full gate, so M4 evidence alone selects `direct-claude`. A future release
qualifies only through its own complete, current release-bound receipt.

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

Absent files, invalid timestamps, future-issued or expired evidence, missing
criteria, non-PASS status, malformed hashes, or extra criteria fail closed to
`direct-claude`. Re-run selection whenever binding inputs change.
