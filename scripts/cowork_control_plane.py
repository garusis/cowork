#!/usr/bin/env python3
"""Closed PhaseState taxonomy, pure transition reducer, causal fingerprint.

M2 Package A — pure contracts. This module is inert infrastructure: it
performs no file or network I/O, spawns nothing, and imports no runtime
module. Every later M2 package consumes it as a schema/validation layer only.

M3 Package A additively activates the already-reserved `awaiting_capacity`
phase: exactly two new EVENT_SET members (`capacity_wake_claimed`,
`capacity_wake_preflight_failed`) and exactly six new TRANSITIONS entries
(see the transition table below). Every pre-existing M2 entry, claim, and
public signature is unchanged; the new evidence validators
(`_capacity_evidence_valid`, `_capacity_wake_evidence_valid`,
`_capacity_wake_preflight_failure_evidence_valid`) mirror the existing
`_gate_evidence_valid` pattern.

M3A-REV-001-RESIDUAL: mandatory candidate binding. Unlike `gate_validated`
(which honors an omitted `expected_candidate` as a deliberate opt-out — see
advance()'s docstring), each of the three M3 capacity events
(`capacity_reserved`, `capacity_wake_claimed`, `capacity_wake_preflight_failed`)
REQUIRES a genuine, well-formed `expected_candidate`: absent, malformed, or
mismatched all fail closed, each with its own stable reason_code (see
`_expected_candidate_valid` and advance()'s docstring).

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

# Terminal states have no legal outbound transition. `needs_authority`,
# `awaiting_capacity` and `blocked` are excluded: all three are paused —
# `needs_authority` frozen except for cancel/abort; `awaiting_capacity`
# frozen except for cancel/abort/`capacity_wake_claimed` (its narrow M3
# resume edge back to `preflighting` — see the transition table below); and
# `blocked` legally resumes via `dependency_unblocked`. `blocked` is
# reachable only from `running` (see
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
    # M3 Package A additions (exactly two): narrow, additive activation of
    # the already-reserved awaiting_capacity phase. See TRANSITIONS below.
    "capacity_wake_claimed",
    "capacity_wake_preflight_failed",
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
# `awaiting_capacity` was listed in PHASE_STATES/PHASE_STATE_SET (and
# `capacity_reserved` in EVENTS/EVENT_SET) in M2 for future M3 activation,
# with no (state, event) pair targeting it and no outbound entry of its
# own — fully unreachable and inert in M2 (see the M2 baseline captured by
# test_cowork_control_plane_m3.py's set-difference proof). M3 Package A
# narrowly activates it with exactly six new entries below: two inbound
# (`capacity_reserved` from `running`/`preflighting`), one outbound resume
# (`capacity_wake_claimed` back to `preflighting`), one outbound
# binding-specific wake-failure return (`capacity_wake_preflight_failed`
# from `preflighting`, back to `awaiting_capacity` — see that event's note
# below), and the same `cancelled`/`aborted` pair every other paused state
# already carries. See test_awaiting_capacity_m3_narrowly_reachable in
# test_cowork_control_plane.py for the exhaustive reachability proof.
#
# `capacity_wake_preflight_failed` is legal ONLY from `preflighting` and
# ONLY represents a genuine binding-preservation failure of the wake's OWN
# PauseLease/CapacityPacket binding (role/session/controller/model/effort/
# policy/candidate mismatch, or a stale/already-consumed/cancelled lease
# reference) — see `_capacity_wake_preflight_failure_evidence_valid`. An
# ordinary environment preflight failure during a wake attempt (for example
# the guard broker becoming unreachable) is NOT this event: it still uses
# the pre-existing `preflight_rejected`/`capability_missing` edges, landing
# in `rejected_preflight`/`needs_authority` exactly as M2 already requires,
# never silently returning to `awaiting_capacity`.
TRANSITIONS = {
    ("pending", "preflight_started"): ("preflighting", "preflight_started"),
    ("pending", "cancelled"): ("cancelled", "cancelled"),
    ("pending", "aborted"): ("aborted", "aborted"),

    ("preflighting", "preflight_passed"): ("running", "preflight_passed"),
    ("preflighting", "preflight_rejected"): ("rejected_preflight", "preflight_rejected"),
    ("preflighting", "capability_missing"): ("needs_authority", "capability_missing"),
    ("preflighting", "cancelled"): ("cancelled", "cancelled"),
    ("preflighting", "aborted"): ("aborted", "aborted"),
    ("preflighting", "capacity_reserved"): ("awaiting_capacity", "capacity_reserved"),
    ("preflighting", "capacity_wake_preflight_failed"): (
        "awaiting_capacity", "capacity_wake_preflight_failed"),

    ("running", "turn_completed"): ("awaiting_gate", "turn_completed"),
    ("running", "dependency_blocked"): ("blocked", "dependency_blocked"),
    ("running", "capability_missing"): ("needs_authority", "capability_missing"),
    ("running", "execution_failed"): ("failed", "execution_failed"),
    ("running", "cancelled"): ("cancelled", "cancelled"),
    ("running", "aborted"): ("aborted", "aborted"),
    ("running", "capacity_reserved"): ("awaiting_capacity", "capacity_reserved"),

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

    ("awaiting_capacity", "capacity_wake_claimed"): ("preflighting", "capacity_wake_claimed"),
    ("awaiting_capacity", "cancelled"): ("cancelled", "cancelled"),
    ("awaiting_capacity", "aborted"): ("aborted", "aborted"),

    # completed, rejected_preflight, failed, cancelled and aborted originate
    # no entries: all five are terminal.
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


# ---------------------------------------------------------------------------
# M3 Package A: capacity evidence validators (mirror _gate_evidence_valid)
# ---------------------------------------------------------------------------
#
# Only `quota_limited`/`overloaded` may ever justify entering or resuming
# capacity — this is a private, local copy of the same closed pair named by
# `cowork_capacity.CAPACITY_ELIGIBLE_OUTCOMES`. It is intentionally
# duplicated rather than imported: this module stays stdlib-only (see
# ImportAndIOBoundaryTest.test_imports_are_stdlib_only), and
# `cowork_capacity` is the later, richer contracts module later packages
# consume — not a dependency of this foundational reducer. A dedicated
# cross-module test asserts the two sets stay identical.
_CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES = frozenset({"quota_limited", "overloaded"})

# Named per the frozen brief: these two ControllerOutcome members can never
# be accepted as capacity evidence for either new event, even though they
# are simply absent from _CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES above (kept
# here, too, purely so a reader/grep can find the explicit denial by name).
_NON_CAPACITY_TERMINAL_OUTCOMES = frozenset({
    "local_guard_exhausted", "unknown_provider_failure",
})

# Canonical RFC3339 -> UTC epoch-seconds conversion, duplicated (not
# imported) from cowork_capacity.rfc3339_to_epoch_seconds for the same
# stdlib-only-independence reason as _CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES
# above. Used ONLY to compare `not_before` against `current_clock` in
# `_capacity_wake_evidence_valid` — a raw string comparison of two
# differently-formatted-but-instant-equal (or misleadingly-ordered)
# RFC3339 strings can give the wrong answer, so every comparison here goes
# through this canonical numeric form instead. A dedicated cross-module
# test asserts this stays behaviorally identical to
# cowork_capacity.rfc3339_to_epoch_seconds on a shared set of sample
# timestamps, guarding against drift between the two independent copies.
_RFC3339_FULL_RE = re.compile(
    r'^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T'
    r'(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(?P<frac>\.\d+)?'
    r'(?P<offset>Z|[+-]\d{2}:\d{2})$')

_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in_month(year, month):
    if month == 2 and _is_leap_year(year):
        return 29
    return _DAYS_IN_MONTH[month - 1]


def _days_from_civil(year, month, day):
    """Howard Hinnant's days-from-civil algorithm; pure integer arithmetic,
    no imports — see the identical algorithm/rationale in
    cowork_capacity._days_from_civil."""
    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _rfc3339_epoch_seconds(text):
    """Pure, canonical RFC3339 -> UTC epoch-seconds (float), or None when
    `text` is not a well-formed RFC3339 timestamp. See
    cowork_capacity.rfc3339_to_epoch_seconds for the full rationale; this is
    an intentional, behavior-identical duplicate (not an import)."""
    if not isinstance(text, str):
        return None
    m = _RFC3339_FULL_RE.match(text)
    if not m:
        return None
    year = int(m.group("year"))
    month = int(m.group("month"))
    day = int(m.group("day"))
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    second = int(m.group("second"))
    frac = m.group("frac")
    frac_seconds = float(frac) if frac else 0.0
    if not (1 <= month <= 12):
        return None
    if not (1 <= day <= _days_in_month(year, month)):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 60):
        return None
    offset = m.group("offset")
    if offset == "Z":
        offset_seconds = 0
    else:
        sign = 1 if offset[0] == "+" else -1
        offset_seconds = sign * (int(offset[1:3]) * 3600 + int(offset[4:6]) * 60)
    days = _days_from_civil(year, month, day)
    return days * 86400 + hour * 3600 + minute * 60 + second + frac_seconds - offset_seconds


_BINDING_WAKE_FAILURE_KINDS = frozenset({
    "role_mismatch", "session_mismatch", "controller_policy_mismatch",
    "model_effort_mismatch", "candidate_mismatch",
    "stale_lease", "consumed_lease_reused", "cancelled_lease_reused",
})

# Detached-signature material must itself be signature-shaped — see the
# identical, independently-duplicated constant/rationale in
# cowork_capacity._SIGNATURE_HEX_RE.
_SIGNATURE_HEX_RE = re.compile(r'^[0-9a-f]{32,}$')


def _check_capacity_binding_fields(block):
    """Shared shape check for the identity fields every capacity-related
    evidence block below carries: `role`, `provider_session_id`,
    `controller_policy_digest` (required nonempty/hex64), and
    `candidate_manifest_digest` (required nonempty/hex64 — capacity
    evidence must genuinely BIND a candidate, never merely optionally name
    one) paired with `candidate_index` (optional nonnegative int; a
    candidate-bound record may still have a null index, matching
    `_gate_evidence_valid`'s WorkUnit-consistent candidate pair). Returns
    True iff every field is well-shaped."""
    for field in ("role", "provider_session_id"):
        value = block.get(field)
        if not isinstance(value, str) or not value:
            return False
    digest_policy = block.get("controller_policy_digest")
    if not isinstance(digest_policy, str) or not _HEX64_RE.match(digest_policy):
        return False
    digest = block.get("candidate_manifest_digest")
    if not isinstance(digest, str) or not _HEX64_RE.match(digest):
        return False
    index = block.get("candidate_index")
    if index is not None and (
        isinstance(index, bool) or not isinstance(index, int) or index < 0
    ):
        return False
    return True


def _capacity_binding_matches_candidate(block, expected_candidate):
    """True when a capacity-related evidence block's candidate identity
    PAIR (`candidate_manifest_digest` AND `candidate_index` — both fields,
    matching `_gate_evidence_matches_candidate`'s "pair truth", never digest
    alone) matches `expected_candidate`. Shared by all three M3 evidence-
    gated events; each caller in advance() has already confirmed
    `expected_candidate` is a well-formed dict via `_expected_candidate_valid`
    before calling this (M3A-REV-001-RESIDUAL: unlike `gate_validated`,
    these three events REQUIRE a genuine expected_candidate — there is no
    "expected_candidate omitted, skip the comparison" case here) and maps a
    False result to its OWN distinct mismatch reason_code — this function
    itself is reason-code agnostic."""
    return (
        block.get("candidate_manifest_digest") == expected_candidate.get("candidate_manifest_digest")
        and block.get("candidate_index") == expected_candidate.get("candidate_index")
    )


# M3A-REV-001-RESIDUAL: mandatory candidate binding. Unlike `gate_validated`
# (where a caller may deliberately omit `expected_candidate` to skip the
# comparison — see advance()'s docstring), each of the three M3 capacity
# events REQUIRES a genuine, well-formed `expected_candidate`: an absent
# (None), non-dict, wrong-key-set, or malformed-field `expected_candidate`
# fails closed exactly like a mismatched one, never silently accepting
# evidence for "any well-formed candidate" the way omitting it does for
# `gate_validated`. This is the one thing that keeps a caller from
# defeating the candidate-pair binding simply by forgetting (or being
# tricked into omitting) `expected_candidate` when advancing a capacity
# event for a specific, real WorkUnit.
_EXPECTED_CANDIDATE_KEYS = frozenset({"candidate_manifest_digest", "candidate_index"})


def _expected_candidate_valid(expected_candidate):
    """True only for a well-shaped, genuinely candidate-bound
    `expected_candidate`: a dict with exactly {"candidate_manifest_digest",
    "candidate_index"} keys, a required (non-null, hex64)
    `candidate_manifest_digest`, and an optional (nonnegative int or None)
    `candidate_index` — matching `_check_capacity_binding_fields`'s digest-
    required/index-optional shape. Returns False for None exactly like any
    other malformed shape (see the module-level note above)."""
    if not isinstance(expected_candidate, dict):
        return False
    if set(expected_candidate) != _EXPECTED_CANDIDATE_KEYS:
        return False
    digest = expected_candidate.get("candidate_manifest_digest")
    if not isinstance(digest, str) or not _HEX64_RE.match(digest):
        return False
    index = expected_candidate.get("candidate_index")
    if index is not None and (
        isinstance(index, bool) or not isinstance(index, int) or index < 0
    ):
        return False
    return True


def _capacity_evidence_valid(evidence):
    """True only for well-shaped `capacity_reserved` evidence naming a
    genuine capacity-eligible controller outcome and the full binding this
    package's invariants require: candidate, index, role, provider session,
    controller policy, resume_mode, model/effort where applicable, artifact
    hashes, and automation reference.

    Required shape: `{"capacity_evidence": {"controller_outcome": ...,
    "role": ..., "provider_session_id": ..., "controller_policy_digest":
    ..., "candidate_manifest_digest": <required, non-null, hex64>,
    "candidate_index": <optional nonnegative int>,
    "resume_mode": "scheduled"|"manual_signal", "model": <str or None>,
    "effort": <str or None>, "artifact_hashes": {<name>: <hex64>, ...},
    "automation_ref": <nonempty str>}}`. This only checks that the evidence
    names a well-formed, genuinely-bound candidate; it does NOT check that
    candidate is the one the caller expects — see
    _capacity_binding_matches_candidate and advance()'s `expected_candidate`
    parameter for that binding.

    `controller_outcome` MUST be a member of
    `_CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES` (`quota_limited`/`overloaded`
    only) — in particular, `local_guard_exhausted` and
    `unknown_provider_failure` evidence is always refused here, never
    silently accepted (see test_local_guard_exhausted_never_enters_capacity
    / test_unknown_provider_failure_never_enters_capacity in
    test_cowork_control_plane_m3.py)."""
    block = evidence.get("capacity_evidence")
    if not isinstance(block, dict):
        return False
    if block.get("controller_outcome") not in _CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES:
        return False
    if not _check_capacity_binding_fields(block):
        return False
    if block.get("resume_mode") not in ("scheduled", "manual_signal"):
        return False
    model = block.get("model")
    if model is not None and not isinstance(model, str):
        return False
    effort = block.get("effort")
    if effort is not None and not isinstance(effort, str):
        return False
    artifact_hashes = block.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict) or not artifact_hashes:
        return False
    for name, digest in artifact_hashes.items():
        if not isinstance(name, str) or not name:
            return False
        if not isinstance(digest, str) or not _HEX64_RE.match(digest):
            return False
    automation_ref = block.get("automation_ref")
    if not isinstance(automation_ref, str) or not automation_ref:
        return False
    return True


def _capacity_wake_evidence_valid(evidence):
    """True only for well-shaped `capacity_wake_claimed` evidence: either
    (a) trustworthy-reset evidence naming a consumed, non-reused `lease_id`
    and a `not_before` at or before the supplied `current_clock` — compared
    via canonical `_rfc3339_epoch_seconds`, never a raw lexicographic string
    comparison, since two differently-formatted RFC3339 strings can name
    the same or a misleadingly-ordered instant (both explicit evidence
    fields — this reducer never reads a wall clock), or (b) manual-signal
    evidence naming a `signal_journal_ref` bound to the same packet/
    candidate/session/policy identity and carrying detached signature
    material (`detached_signature` — a lowercase hex string of at least 32
    characters, see `_SIGNATURE_HEX_RE` — and `signer_public_key_id`) —
    never a plaintext boolean trust flag.

    Required common shape: `{"capacity_wake_evidence": {"kind":
    "trustworthy_reset"|"manual_signal", "lease_id": ..., "role": ...,
    "provider_session_id": ..., "controller_policy_digest": ...,
    "candidate_manifest_digest": ..., "candidate_index": ..., ...}}`, plus
    kind-specific fields documented below."""
    block = evidence.get("capacity_wake_evidence")
    if not isinstance(block, dict):
        return False
    lease_id = block.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return False
    if not _check_capacity_binding_fields(block):
        return False

    kind = block.get("kind")
    if kind == "trustworthy_reset":
        if block.get("consumption_state") != "consumed":
            return False
        not_before_epoch = _rfc3339_epoch_seconds(block.get("not_before"))
        current_clock_epoch = _rfc3339_epoch_seconds(block.get("current_clock"))
        if not_before_epoch is None or current_clock_epoch is None:
            return False
        return not_before_epoch <= current_clock_epoch
    if kind == "manual_signal":
        if block.get("authorized") is not None:
            # A plaintext trust flag is never accepted, present or absent —
            # its mere presence marks this evidence malformed.
            return False
        for field in ("signal_journal_ref", "signer_public_key_id"):
            value = block.get(field)
            if not isinstance(value, str) or not value:
                return False
        signature = block.get("detached_signature")
        if not isinstance(signature, str) or not _SIGNATURE_HEX_RE.match(signature):
            return False
        return True
    return False


def _capacity_wake_preflight_failure_evidence_valid(evidence):
    """True only for well-shaped `capacity_wake_preflight_failed` evidence
    naming the exact binding that failed and a genuine binding-preservation
    `failure_kind` — never an ordinary environment-preflight failure (those
    use `preflight_rejected`/`capability_missing` instead; see the
    TRANSITIONS table note above `capacity_wake_preflight_failed`).

    Required shape: `{"capacity_wake_preflight_failure": {"lease_id": ...,
    "role": ..., "provider_session_id": ..., "controller_policy_digest":
    ..., "candidate_manifest_digest": ..., "candidate_index": ...,
    "failure_kind": <member of _BINDING_WAKE_FAILURE_KINDS>}}`."""
    block = evidence.get("capacity_wake_preflight_failure")
    if not isinstance(block, dict):
        return False
    lease_id = block.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return False
    if not _check_capacity_binding_fields(block):
        return False
    if block.get("failure_kind") not in _BINDING_WAKE_FAILURE_KINDS:
        return False
    return True


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

    Three M3 events carry the same evidence-gated pattern, each with its own
    distinct mismatch reason_code, never sharing
    `gate_evidence_candidate_mismatch` — but, per M3A-REV-001-RESIDUAL,
    NONE of the three honor an omitted/malformed `expected_candidate` the
    way `gate_validated` does (see below): all three REQUIRE a genuine,
    well-formed `expected_candidate` and fail closed without one (see
    `_expected_candidate_valid`).
    `capacity_reserved` requires well-shaped capacity evidence naming a
    capacity-eligible controller outcome, a REQUIRED (non-null) candidate
    digest, and full binding (see _capacity_evidence_valid) — missing/
    malformed evidence refuses with reason_code "capacity_evidence_missing";
    an absent/malformed `expected_candidate` (checked next) refuses with
    reason_code "capacity_evidence_expected_candidate_required"; a well-
    shaped-but-wrong-candidate mismatch refuses with reason_code
    "capacity_evidence_candidate_mismatch".
    `capacity_wake_claimed` requires either trustworthy-reset or
    verified-manual-signal wake evidence (see _capacity_wake_evidence_valid)
    — missing/malformed evidence refuses with reason_code
    "capacity_wake_evidence_missing"; an absent/malformed
    `expected_candidate` refuses with reason_code
    "capacity_wake_evidence_expected_candidate_required"; a candidate
    mismatch refuses with reason_code
    "capacity_wake_evidence_candidate_mismatch".
    `capacity_wake_preflight_failed` requires evidence naming the exact
    binding that failed and a genuine binding-preservation failure_kind
    (see _capacity_wake_preflight_failure_evidence_valid) — missing/
    malformed evidence refuses with reason_code
    "capacity_wake_preflight_evidence_missing"; an absent/malformed
    `expected_candidate` refuses with reason_code
    "capacity_wake_preflight_evidence_expected_candidate_required"; a
    candidate mismatch refuses with reason_code
    "capacity_wake_preflight_evidence_candidate_mismatch".
    In every refusal case the state is left unchanged, exactly like
    "gate_evidence_missing" above.

    `expected_candidate` is the genuine candidate binding: pass the WorkUnit
    being advanced's own `{"candidate_manifest_digest": ..., "candidate_index":
    ...}` (see cowork_workunit.validate_work_unit) and the reducer refuses to
    complete evidence naming a different candidate, returning reason_code
    "gate_evidence_candidate_mismatch" instead. For `gate_validated` only,
    omitting it (leaving the default None) skips this comparison — evidence
    for ANY well-formed candidate is then accepted — so a caller advancing a
    specific WorkUnit toward `completed` MUST pass its candidate identity as
    `expected_candidate` to get a genuine, candidate-bound completion; this
    reducer has no other way to know which WorkUnit it is advancing. The
    three M3 capacity events above do NOT offer this opt-out: for them,
    `expected_candidate` is mandatory (see M3A-REV-001-RESIDUAL above) and
    an omitted/malformed one fails closed rather than skipping the check.

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
    elif event == "capacity_reserved":
        if not _capacity_evidence_valid(evidence):
            return state, "capacity_evidence_missing"
        if not _expected_candidate_valid(expected_candidate):
            return state, "capacity_evidence_expected_candidate_required"
        if not _capacity_binding_matches_candidate(
                evidence.get("capacity_evidence") or {}, expected_candidate):
            return state, "capacity_evidence_candidate_mismatch"
    elif event == "capacity_wake_claimed":
        if not _capacity_wake_evidence_valid(evidence):
            return state, "capacity_wake_evidence_missing"
        if not _expected_candidate_valid(expected_candidate):
            return state, "capacity_wake_evidence_expected_candidate_required"
        if not _capacity_binding_matches_candidate(
                evidence.get("capacity_wake_evidence") or {}, expected_candidate):
            return state, "capacity_wake_evidence_candidate_mismatch"
    elif event == "capacity_wake_preflight_failed":
        if not _capacity_wake_preflight_failure_evidence_valid(evidence):
            return state, "capacity_wake_preflight_evidence_missing"
        if not _expected_candidate_valid(expected_candidate):
            return state, "capacity_wake_preflight_evidence_expected_candidate_required"
        if not _capacity_binding_matches_candidate(
                evidence.get("capacity_wake_preflight_failure") or {}, expected_candidate):
            return state, "capacity_wake_preflight_evidence_candidate_mismatch"
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
