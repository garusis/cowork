#!/usr/bin/env python3
"""Closed PhaseState taxonomy, pure transition reducer, causal fingerprint.

M2 Package A — pure contracts. This module is inert infrastructure: it
performs no file or network I/O, spawns nothing, and imports no runtime
module. Every later M2 package consumes it as a schema/validation layer only.

Public API:
    PHASE_STATES, PHASE_STATE_SET, TERMINAL_STATES, EVENTS, EVENT_SET
    advance(state, event, evidence=None, expected_candidate=None)
        -> (new_state, reason_code)
    fingerprint(role, config_digest, provider, candidate, reason) -> str

The reducer's event vocabulary is closed and entirely domain-typed
(`preflight_passed`, `gate_validated`, ...). It never includes a raw process
exit code, an EOF marker, or a status-file-present signal — translating those
low-level provider/OS signals into one of these typed events is a later
package's job (a caller-side boundary), never this reducer's.

Candidate identity rule (fail-closed): only a candidate-bound WorkUnit may be
`completed`. The sole edge into `completed` is `("awaiting_gate",
"gate_validated")`, and `_gate_evidence_valid` requires evidence naming a
real (non-null, hex64) `candidate_manifest_digest`, so `advance` can never
produce `completed` for a candidate-free unit. `cowork_workunit.
validate_work_unit` enforces the matching schema-level half of this same
rule: it rejects `lifecycle_state == "completed"` when
`candidate_manifest_digest` is null, so no WorkUnit record can claim a
`completed` state this reducer could never legally have produced.
"""

import hashlib
import json
import re

# ---------------------------------------------------------------------------
# Closed PhaseState taxonomy
# ---------------------------------------------------------------------------

PHASE_STATES = (
    "pending",
    "preflighting",
    "running",
    "awaiting_gate",
    "completed",
    "rejected_preflight",
    "needs_authority",
    "awaiting_capacity",
    "blocked",
    "failed",
    "cancelled",
    "aborted",
)
PHASE_STATE_SET = frozenset(PHASE_STATES)

# Terminal states have no legal outbound M2 transition. `needs_authority`,
# `awaiting_capacity` and `blocked` are excluded: the first two are paused
# (reachable, but frozen except for cancel/abort in M2; `awaiting_capacity`
# has no legal *inbound* transition either — see the module docstring below
# the transition table), and `blocked` legally resumes via
# `dependency_unblocked`. `blocked` is reachable only from `running` (see
# the transition table): this is deliberate, not incidental — a unit that
# already reached `awaiting_gate` (finished its turn) can never become
# `blocked`, so `blocked`'s single resume edge back to `running` can never
# be reached having skipped or discarded a completed, ungated turn.
TERMINAL_STATES = frozenset({
    "completed", "rejected_preflight", "failed", "cancelled", "aborted",
})

# ---------------------------------------------------------------------------
# Closed event vocabulary
# ---------------------------------------------------------------------------

EVENTS = (
    "preflight_started",
    "preflight_passed",
    "preflight_rejected",
    "capability_missing",
    "turn_completed",
    "gate_validated",
    "gate_rejected",
    "dependency_blocked",
    "dependency_unblocked",
    "capacity_reserved",
    "execution_failed",
    "cancelled",
    "aborted",
)
EVENT_SET = frozenset(EVENTS)

# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------
#
# (state, event) -> (new_state, reason_code). Any (state, event) pair absent
# from this table is an explicit rejection: advance() returns the unchanged
# state and reason_code "illegal_transition". The table is therefore total
# over PHASE_STATE_SET x EVENT_SET by construction (every pair is either a
# listed legal transition or an implicit, uniformly-handled rejection).
#
# `needs_authority` is targeted only by `capability_missing` transitions
# (from `preflighting` and `running`) — no other event in this table names it
# as a destination.
#
# `blocked` is targeted only by `("running", "dependency_blocked")` — no
# `("awaiting_gate", ...)` entry targets `blocked`. A unit that already
# finished its turn (reached `awaiting_gate`) cannot become `blocked`; this
# closes the ambiguity where `blocked`'s only resume edge,
# `("blocked", "dependency_unblocked") -> running`, would otherwise be
# reachable from two different origins (running or awaiting_gate) and could
# silently discard a completed turn still waiting on gate validation by
# forcing a second `turn_completed` (a second, budget-burning provider
# execution) to reach `awaiting_gate` again. See
# test_awaiting_gate_cannot_become_blocked_m2 in
# test_cowork_control_plane.py.
#
# `awaiting_capacity` is listed in PHASE_STATES/PHASE_STATE_SET (and
# `capacity_reserved` is listed in EVENTS/EVENT_SET) for future M3
# activation, but no (state, event) pair below targets `awaiting_capacity`,
# and `awaiting_capacity` itself originates no outbound entry. It is
# therefore fully unreachable and inert in M2; see
# test_awaiting_capacity_unreachable_m2 in test_cowork_control_plane.py.
TRANSITIONS = {
    ("pending", "preflight_started"): ("preflighting", "preflight_started"),
    ("pending", "cancelled"): ("cancelled", "cancelled"),
    ("pending", "aborted"): ("aborted", "aborted"),

    ("preflighting", "preflight_passed"): ("running", "preflight_passed"),
    ("preflighting", "preflight_rejected"): ("rejected_preflight", "preflight_rejected"),
    ("preflighting", "capability_missing"): ("needs_authority", "capability_missing"),
    ("preflighting", "cancelled"): ("cancelled", "cancelled"),
    ("preflighting", "aborted"): ("aborted", "aborted"),

    ("running", "turn_completed"): ("awaiting_gate", "turn_completed"),
    ("running", "dependency_blocked"): ("blocked", "dependency_blocked"),
    ("running", "capability_missing"): ("needs_authority", "capability_missing"),
    ("running", "execution_failed"): ("failed", "execution_failed"),
    ("running", "cancelled"): ("cancelled", "cancelled"),
    ("running", "aborted"): ("aborted", "aborted"),

    ("awaiting_gate", "gate_validated"): ("completed", "gate_validated"),
    ("awaiting_gate", "gate_rejected"): ("failed", "gate_rejected"),
    ("awaiting_gate", "cancelled"): ("cancelled", "cancelled"),
    ("awaiting_gate", "aborted"): ("aborted", "aborted"),
    # No ("awaiting_gate", "dependency_blocked") entry: see the `blocked`
    # note above the table. A unit already awaiting gate validation is
    # refused ("illegal_transition") rather than allowed to become
    # `blocked`, so it can never resume through `blocked`'s single
    # `dependency_unblocked` -> `running` edge and lose its completed turn.

    ("blocked", "dependency_unblocked"): ("running", "dependency_unblocked"),
    ("blocked", "cancelled"): ("cancelled", "cancelled"),
    ("blocked", "aborted"): ("aborted", "aborted"),

    ("needs_authority", "cancelled"): ("cancelled", "cancelled"),
    ("needs_authority", "aborted"): ("aborted", "aborted"),

    # completed, rejected_preflight, awaiting_capacity, failed, cancelled and
    # aborted originate no entries: the first five are terminal in M2 and the
    # sixth (awaiting_capacity) is reserved/inert, per the module docstring.
}

_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


def _gate_evidence_valid(evidence):
    """True only for well-shaped, passing, candidate-identified gate evidence.

    Required shape: {"gate_validation": {"candidate_manifest_digest": <64
    lowercase hex chars>, "candidate_index": <nonnegative int or None>,
    "verdict": "pass"}}. `candidate_manifest_digest` and `candidate_index`
    are named and typed identically to WorkUnit.candidate_manifest_digest /
    WorkUnit.candidate_index (cowork_workunit.py) — that shared name and
    type IS the declared mapping between gate evidence and WorkUnit candidate
    identity. Any other shape (missing block, wrong verdict, malformed
    digest/index) is invalid.

    This only checks that the evidence names *some* well-formed candidate;
    it does NOT check that candidate is the one the caller expects — see
    _gate_evidence_matches_candidate and advance()'s `expected_candidate`
    parameter for that binding.
    """
    gate = evidence.get("gate_validation")
    if not isinstance(gate, dict):
        return False
    digest = gate.get("candidate_manifest_digest")
    if not isinstance(digest, str) or not _HEX64_RE.match(digest):
        return False
    index = gate.get("candidate_index")
    if index is not None and (
        isinstance(index, bool) or not isinstance(index, int) or index < 0
    ):
        return False
    if gate.get("verdict") != "pass":
        return False
    return True


def _gate_evidence_matches_candidate(evidence, expected_candidate):
    """True when gate evidence's candidate identity matches `expected_candidate`.

    `expected_candidate` is None (no expectation supplied by the caller — see
    advance()'s docstring for why this is a caller obligation) or a dict
    `{"candidate_manifest_digest": <64 hex chars or None>, "candidate_index":
    <nonnegative int or None>}` — copied directly from the WorkUnit being
    advanced (cowork_workunit.validate_work_unit's fields of the same name).
    Both fields are compared for exact equality against
    evidence["gate_validation"]; this is the genuine binding, not merely a
    documented promise that a caller has already checked it.
    """
    if expected_candidate is None:
        return True
    gate = evidence.get("gate_validation") or {}
    return (
        gate.get("candidate_manifest_digest")
        == expected_candidate.get("candidate_manifest_digest")
        and gate.get("candidate_index") == expected_candidate.get("candidate_index")
    )


def advance(state, event, evidence=None, expected_candidate=None):
    """Pure reducer: (state, event, evidence, expected_candidate) ->
    (new_state, reason_code).

    `state` and `event` must be members of PHASE_STATE_SET / EVENT_SET;
    anything else is a caller contract violation and raises ValueError.
    `evidence` is an optional dict; when omitted it is treated as `{}`.

    A (state, event) pair absent from TRANSITIONS returns the unchanged
    `state` and reason_code "illegal_transition" — the reducer never raises
    for a business-level illegal transition, only for a malformed call.

    The `gate_validated` event additionally requires evidence carrying
    well-shaped, passing gate-validation evidence naming a candidate (see
    _gate_evidence_valid); when it is missing or malformed the transition to
    `completed` is refused and the reducer instead returns the unchanged
    `state` and reason_code "gate_evidence_missing". This is the only path to
    `completed`, so `completed` is unreachable without such evidence.

    `expected_candidate` is the genuine candidate binding: pass the WorkUnit
    being advanced's own `{"candidate_manifest_digest": ..., "candidate_index":
    ...}` (see cowork_workunit.validate_work_unit) and the reducer refuses to
    complete evidence naming a different candidate, returning reason_code
    "gate_evidence_candidate_mismatch" instead. Omitting it (leaving the
    default None) skips this comparison — evidence for ANY well-formed
    candidate is then accepted — so a caller advancing a specific WorkUnit
    toward `completed` MUST pass its candidate identity as
    `expected_candidate` to get a genuine, candidate-bound completion; this
    reducer has no other way to know which WorkUnit it is advancing.

    Performs no I/O and has no side effects; identical inputs always produce
    identical outputs.
    """
    if state not in PHASE_STATE_SET:
        raise ValueError("unknown state: %r" % (state,))
    if event not in EVENT_SET:
        raise ValueError("unknown event: %r" % (event,))
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("evidence must be a dict or None, got %r" % type(evidence))
    if expected_candidate is not None and not isinstance(expected_candidate, dict):
        raise ValueError(
            "expected_candidate must be a dict or None, got %r"
            % type(expected_candidate))
    evidence = evidence or {}

    transition = TRANSITIONS.get((state, event))
    if transition is None:
        return state, "illegal_transition"

    new_state, reason_code = transition
    if event == "gate_validated":
        if not _gate_evidence_valid(evidence):
            return state, "gate_evidence_missing"
        if not _gate_evidence_matches_candidate(evidence, expected_candidate):
            return state, "gate_evidence_candidate_mismatch"
    return new_state, reason_code


# ---------------------------------------------------------------------------
# Causal fingerprint
# ---------------------------------------------------------------------------

def fingerprint(role, config_digest, provider, candidate, reason):
    """Deterministic causal fingerprint used by the circuit breaker.

    A pure function of exactly these five inputs: identical inputs always
    produce an identical digest, and changing any single input changes the
    digest (modulo sha256 collision). `role`, `config_digest`, `provider` and
    `reason` must be nonempty strings; `candidate` may be a nonempty string,
    a JSON-serializable dict, or None (no candidate bound).

    Performs no I/O; the digest is computed entirely from its arguments.
    """
    for name, value in (
        ("role", role), ("config_digest", config_digest),
        ("provider", provider), ("reason", reason),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError("%s must be a nonempty string, got %r" % (name, value))
    if candidate is not None and not isinstance(candidate, (str, dict)):
        raise ValueError(
            "candidate must be a string, dict, or None, got %r" % type(candidate))
    if isinstance(candidate, str) and not candidate:
        raise ValueError("candidate must be nonempty when a string, got %r" % candidate)

    payload = {
        "role": role,
        "config_digest": config_digest,
        "provider": provider,
        "candidate": candidate,
        "reason": reason,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
