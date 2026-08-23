#!/usr/bin/env python3
"""M3 Package A tests for cowork_control_plane's narrow awaiting_capacity
activation: set-difference proof against the signed M2 baseline, binding-
specific wake-failure evidence, local-guard/unknown-provider unreachability,
and closed-trust-source/malformed-parser coverage that lives at the
control-plane evidence-validator layer.

Run standalone:

    python3 -m unittest scripts/test_cowork_control_plane_m3.py -v
"""

import itertools
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_capacity as capacity  # noqa: E402
import cowork_control_plane as cp  # noqa: E402

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64

# ---------------------------------------------------------------------------
# M2 baseline, captured verbatim from the signed base commit
# 7a2440c7571d01eb20251589bd82ab2caf620e4d's scripts/cowork_control_plane.py.
# This is a hardcoded constant, not a re-derivation from the current module
# — the set-difference proof below compares the CURRENT cp.EVENTS/
# cp.TRANSITIONS against this frozen snapshot plus exactly the M3 additions.
# ---------------------------------------------------------------------------

M2_BASELINE_EVENTS = (
    "preflight_started", "preflight_passed", "preflight_rejected",
    "capability_missing", "turn_completed", "gate_validated", "gate_rejected",
    "dependency_blocked", "dependency_unblocked", "capacity_reserved",
    "execution_failed", "cancelled", "aborted",
)

M2_BASELINE_TRANSITIONS = {
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

    ("blocked", "dependency_unblocked"): ("running", "dependency_unblocked"),
    ("blocked", "cancelled"): ("cancelled", "cancelled"),
    ("blocked", "aborted"): ("aborted", "aborted"),

    ("needs_authority", "cancelled"): ("cancelled", "cancelled"),
    ("needs_authority", "aborted"): ("aborted", "aborted"),
}

# The frozen brief's exact M3 additions.
M3_NEW_EVENTS = ("capacity_wake_claimed", "capacity_wake_preflight_failed")

M3_NEW_TRANSITIONS = {
    ("running", "capacity_reserved"): ("awaiting_capacity", "capacity_reserved"),
    ("preflighting", "capacity_reserved"): ("awaiting_capacity", "capacity_reserved"),
    ("awaiting_capacity", "capacity_wake_claimed"): ("preflighting", "capacity_wake_claimed"),
    ("preflighting", "capacity_wake_preflight_failed"): (
        "awaiting_capacity", "capacity_wake_preflight_failed"),
    ("awaiting_capacity", "cancelled"): ("cancelled", "cancelled"),
    ("awaiting_capacity", "aborted"): ("aborted", "aborted"),
}


def _capacity_evidence(**overrides):
    block = {
        "controller_outcome": "quota_limited",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": _HASH_A,
        "candidate_manifest_digest": _HASH_B,
        "candidate_index": 0,
        "resume_mode": "scheduled",
        "model": "claude-x",
        "effort": "high",
        "artifact_hashes": {"manifest": _HASH_C},
        "automation_ref": "scheduler-ref-1",
    }
    block.update(overrides)
    return {"capacity_evidence": block}


def _wake_evidence_trustworthy(**overrides):
    block = {
        "kind": "trustworthy_reset",
        "lease_id": "lease-1",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": _HASH_A,
        "candidate_manifest_digest": _HASH_B,
        "candidate_index": 0,
        "consumption_state": "consumed",
        "not_before": "2026-08-23T00:00:00Z",
        "current_clock": "2026-08-23T01:00:00Z",
    }
    block.update(overrides)
    return {"capacity_wake_evidence": block}


def _wake_evidence_manual(**overrides):
    block = {
        "kind": "manual_signal",
        "lease_id": "lease-1",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": _HASH_A,
        "candidate_manifest_digest": _HASH_B,
        "candidate_index": 0,
        "signal_journal_ref": "journal-ref-1",
        "detached_signature": "3045022100" + "d" * 118,
        "signer_public_key_id": "key-1",
    }
    block.update(overrides)
    return {"capacity_wake_evidence": block}


def _wake_preflight_failure_evidence(**overrides):
    block = {
        "lease_id": "lease-1",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": _HASH_A,
        "candidate_manifest_digest": _HASH_B,
        "candidate_index": 0,
        "failure_kind": "stale_lease",
    }
    block.update(overrides)
    return {"capacity_wake_preflight_failure": block}


# M3A-REV-001-RESIDUAL: the candidate binding all three well-shaped evidence
# helpers above name (_HASH_B, index 0) — the well-formed `expected_candidate`
# every success-path test below must now supply, since all three M3 capacity
# events require a genuine expected_candidate (no "omitted, skip the check"
# opt-out unlike gate_validated).
_BOUND_CANDIDATE = {"candidate_manifest_digest": _HASH_B, "candidate_index": 0}
_BOUND_CANDIDATE_NULL_INDEX = {"candidate_manifest_digest": _HASH_B, "candidate_index": None}


class SetDifferenceProofTest(unittest.TestCase):
    """Explicit before/after set-difference proof: current EVENTS/
    TRANSITIONS equal the frozen M2 baseline plus exactly the M3 additions
    — never a hand-recount."""

    def test_events_equal_m2_baseline_plus_exactly_two_additions(self):
        self.assertEqual(len(M2_BASELINE_EVENTS), 13)
        self.assertEqual(len(M3_NEW_EVENTS), 2)

        current = set(cp.EVENTS)
        baseline = set(M2_BASELINE_EVENTS)
        added = current - baseline
        removed = baseline - current

        self.assertEqual(added, set(M3_NEW_EVENTS))
        self.assertEqual(removed, set())
        self.assertEqual(current, baseline | set(M3_NEW_EVENTS))
        self.assertEqual(len(cp.EVENTS), len(M2_BASELINE_EVENTS) + len(M3_NEW_EVENTS))

    def test_transitions_equal_m2_baseline_plus_exactly_six_additions(self):
        self.assertEqual(len(M2_BASELINE_TRANSITIONS), 23)
        self.assertEqual(len(M3_NEW_TRANSITIONS), 6)

        current = dict(cp.TRANSITIONS)
        baseline_keys = set(M2_BASELINE_TRANSITIONS)
        current_keys = set(current)
        added_keys = current_keys - baseline_keys
        removed_keys = baseline_keys - current_keys

        self.assertEqual(added_keys, set(M3_NEW_TRANSITIONS))
        self.assertEqual(removed_keys, set())

        # Every pre-existing M2 entry is byte-identical (same value, not
        # just same key).
        for key in baseline_keys:
            self.assertEqual(current[key], M2_BASELINE_TRANSITIONS[key],
                              "M2 entry %r drifted" % (key,))
        # Every new entry matches the frozen brief exactly.
        for key, value in M3_NEW_TRANSITIONS.items():
            self.assertEqual(current[key], value, "M3 entry %r drifted" % (key,))

        expected = dict(M2_BASELINE_TRANSITIONS)
        expected.update(M3_NEW_TRANSITIONS)
        self.assertEqual(current, expected)
        self.assertEqual(
            len(cp.TRANSITIONS), len(M2_BASELINE_TRANSITIONS) + len(M3_NEW_TRANSITIONS))


class CapacityReservedEvidenceTest(unittest.TestCase):
    def test_well_shaped_evidence_accepted_from_running(self):
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", _capacity_evidence(),
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_reserved")

    def test_well_shaped_evidence_accepted_from_preflighting(self):
        new_state, reason_code = cp.advance(
            "preflighting", "capacity_reserved", _capacity_evidence(),
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_reserved")

    def test_missing_evidence_refused(self):
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", None, expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "running")
        self.assertEqual(reason_code, "capacity_evidence_missing")

    def test_malformed_evidence_refused(self):
        missing_role = _capacity_evidence()["capacity_evidence"].copy()
        del missing_role["role"]
        bad_shapes = [
            {},
            {"capacity_evidence": None},
            {"capacity_evidence": missing_role},
            _capacity_evidence(role=""),
            _capacity_evidence(provider_session_id=None),
            _capacity_evidence(controller_policy_digest="short"),
            _capacity_evidence(candidate_manifest_digest="Z" * 64),
            _capacity_evidence(candidate_index=-1),
            _capacity_evidence(candidate_index=True),
            _capacity_evidence(resume_mode="eventually"),
            _capacity_evidence(artifact_hashes={}),
            _capacity_evidence(artifact_hashes={"x": "short"}),
            _capacity_evidence(automation_ref=""),
            _capacity_evidence(model=42),
            _capacity_evidence(effort=42),
            _capacity_evidence(candidate_manifest_digest=None),
        ]
        for evidence in bad_shapes:
            with self.subTest(evidence=evidence):
                # Even with a well-formed expected_candidate supplied,
                # malformed evidence is refused first (shape before binding).
                new_state, reason_code = cp.advance(
                    "running", "capacity_reserved", evidence,
                    expected_candidate=_BOUND_CANDIDATE)
                self.assertEqual(new_state, "running")
                self.assertEqual(reason_code, "capacity_evidence_missing")

    def test_null_candidate_digest_rejected(self):
        # M3A-REV correction: required candidate digest. Capacity evidence
        # must genuinely BIND a candidate; a null digest (even with a null
        # index) no longer satisfies the binding requirement.
        evidence = _capacity_evidence(candidate_manifest_digest=None, candidate_index=None)
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", evidence, expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "running")
        self.assertEqual(reason_code, "capacity_evidence_missing")

    def test_null_candidate_index_with_present_digest_accepted(self):
        # The digest is required; the index remains independently optional
        # (a candidate-bound record may still have no index).
        evidence = _capacity_evidence(candidate_index=None)
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", evidence,
            expected_candidate=_BOUND_CANDIDATE_NULL_INDEX)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_reserved")


class LocalGuardAndUnknownProviderUnreachabilityTest(unittest.TestCase):
    """Named per the gate: zero (state, event) pairs accept
    local_guard_exhausted/unknown_provider_failure-sourced evidence for
    either new event."""

    def test_local_guard_exhausted_never_enters_capacity(self):
        evidence = _capacity_evidence(controller_outcome="local_guard_exhausted")
        for state in ("running", "preflighting"):
            with self.subTest(state=state):
                new_state, reason_code = cp.advance(
                    state, "capacity_reserved", evidence, expected_candidate=_BOUND_CANDIDATE)
                self.assertEqual(new_state, state)
                self.assertEqual(reason_code, "capacity_evidence_missing")

    def test_unknown_provider_failure_never_enters_capacity(self):
        evidence = _capacity_evidence(controller_outcome="unknown_provider_failure")
        for state in ("running", "preflighting"):
            with self.subTest(state=state):
                new_state, reason_code = cp.advance(
                    state, "capacity_reserved", evidence, expected_candidate=_BOUND_CANDIDATE)
                self.assertEqual(new_state, state)
                self.assertEqual(reason_code, "capacity_evidence_missing")

    def test_local_guard_exhausted_never_resumes_capacity(self):
        # M3A-REV correction: repair the false local-guard test. The prior
        # version of this test set an extraneous, entirely-ignored
        # "controller_outcome": "local_guard_exhausted" key alongside
        # otherwise-valid wake evidence and asserted the transition still
        # SUCCEEDED — which was true, but proved nothing about
        # local_guard_exhausted's actual (in)ability to authorize a wake
        # (capacity_wake_claimed evidence has no controller_outcome concept
        # at all; the field was simply inert). The real, load-bearing
        # security property is that `kind` is a CLOSED set of exactly
        # {"trustworthy_reset", "manual_signal"}: a value that merely NAMES
        # local_guard_exhausted as the wake's kind is refused outright.
        evidence = _wake_evidence_trustworthy(kind="local_guard_exhausted")
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_evidence_missing")

    def test_wake_evidence_extraneous_field_does_not_invalidate(self):
        # Honest, narrowly-scoped replacement for what the old (false) test
        # actually exercised: capacity_wake_claimed's validator checks
        # required fields, not an exhaustive key set, so an unrelated extra
        # key genuinely has no bearing on an otherwise well-shaped success.
        evidence = _wake_evidence_trustworthy()
        evidence["capacity_wake_evidence"]["some_unused_extra_field"] = "whatever"
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_claimed")

    def test_eligible_outcomes_are_closed_and_match_capacity_module(self):
        # Cross-module drift guard: the control plane's private, duplicated
        # copy of the capacity-eligible outcome set must never diverge from
        # cowork_capacity.CAPACITY_ELIGIBLE_OUTCOMES.
        self.assertEqual(
            cp._CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES, capacity.CAPACITY_ELIGIBLE_OUTCOMES)
        self.assertEqual(
            cp._NON_CAPACITY_TERMINAL_OUTCOMES, capacity.NON_CAPACITY_TERMINAL_OUTCOMES)
        self.assertFalse(
            cp._CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES & cp._NON_CAPACITY_TERMINAL_OUTCOMES)

    def test_every_non_eligible_outcome_refused_as_capacity_evidence(self):
        for outcome in capacity.CONTROLLER_OUTCOME_SET - capacity.CAPACITY_ELIGIBLE_OUTCOMES:
            with self.subTest(outcome=outcome):
                evidence = _capacity_evidence(controller_outcome=outcome)
                new_state, reason_code = cp.advance(
                    "running", "capacity_reserved", evidence, expected_candidate=_BOUND_CANDIDATE)
                self.assertEqual(new_state, "running")
                self.assertEqual(reason_code, "capacity_evidence_missing")


class CapacityWakeClaimedEvidenceTest(unittest.TestCase):
    def test_trustworthy_reset_accepted(self):
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", _wake_evidence_trustworthy(),
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_claimed")

    def test_manual_signal_accepted(self):
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", _wake_evidence_manual(),
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_claimed")

    def test_missing_evidence_refused(self):
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", None,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_evidence_missing")

    def test_neither_trustworthy_reset_nor_manual_signal_refused(self):
        bad_shapes = [
            {},
            {"capacity_wake_evidence": {}},
            {"capacity_wake_evidence": {"kind": "just_trust_me", "lease_id": "lease-1",
                                         "role": "builder", "provider_session_id": "sess-1",
                                         "controller_policy_digest": _HASH_A}},
            _wake_evidence_trustworthy(consumption_state="claimed"),
            _wake_evidence_trustworthy(not_before="2026-08-23T02:00:00Z",
                                        current_clock="2026-08-23T01:00:00Z"),
            _wake_evidence_trustworthy(lease_id=""),
            _wake_evidence_trustworthy(role=""),
            _wake_evidence_trustworthy(controller_policy_digest="short"),
            _wake_evidence_manual(signal_journal_ref=""),
            _wake_evidence_manual(detached_signature=""),
            _wake_evidence_manual(signer_public_key_id=None),
            _wake_evidence_trustworthy(candidate_manifest_digest=None),
            _wake_evidence_manual(candidate_manifest_digest=None),
            # M3A-REV correction: signature must be signature-shaped
            # (>= 32 lowercase hex chars), not merely any nonempty string.
            _wake_evidence_manual(detached_signature="not-a-hex-signature!!"),
            _wake_evidence_manual(detached_signature="ab" * 10),
        ]
        for evidence in bad_shapes:
            with self.subTest(evidence=evidence):
                new_state, reason_code = cp.advance(
                    "awaiting_capacity", "capacity_wake_claimed", evidence,
                    expected_candidate=_BOUND_CANDIDATE)
                self.assertEqual(new_state, "awaiting_capacity")
                self.assertEqual(reason_code, "capacity_wake_evidence_missing")

    def test_plaintext_authorized_flag_never_accepted(self):
        evidence = _wake_evidence_manual()
        evidence["capacity_wake_evidence"]["authorized"] = True
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_evidence_missing")

    def test_not_before_equal_to_current_clock_accepted(self):
        evidence = _wake_evidence_trustworthy(
            not_before="2026-08-23T00:00:00Z", current_clock="2026-08-23T00:00:00Z")
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_claimed")

    def test_canonical_comparison_accepts_differently_formatted_equal_instants(self):
        # "Z" and "+00:00" name the exact same instant -- not_before ==
        # current_clock under canonical comparison, so this must succeed.
        evidence = _wake_evidence_trustworthy(
            not_before="2026-08-23T00:00:00Z", current_clock="2026-08-23T00:00:00+00:00")
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_claimed")

    def test_canonical_comparison_where_lexicographic_would_be_wrong(self):
        # M3A-REV correction: canonical/normalized RFC3339 comparison.
        # not_before="2026-08-23T00:30:00+02:00" (== 2026-08-22T22:30:00Z)
        # is EARLIER than current_clock="2026-08-23T00:00:00Z", so the wake
        # must be accepted -- even though the raw not_before STRING sorts
        # lexicographically AFTER the raw current_clock string, which would
        # wrongly refuse it under a naive string comparison.
        not_before = "2026-08-23T00:30:00+02:00"
        current_clock = "2026-08-23T00:00:00Z"
        self.assertGreater(not_before, current_clock)  # wrong under raw string ordering
        evidence = _wake_evidence_trustworthy(not_before=not_before, current_clock=current_clock)
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_claimed")

    def test_canonical_comparison_still_refuses_genuinely_future_not_before(self):
        evidence = _wake_evidence_trustworthy(
            not_before="2026-08-23T02:00:00Z", current_clock="2026-08-23T01:00:00Z")
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence,
            expected_candidate=_BOUND_CANDIDATE)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_evidence_missing")

    def test_cross_module_rfc3339_epoch_conversion_matches_capacity_module(self):
        # Drift guard for the duplicated (not imported) canonical RFC3339
        # algorithm: both independent copies must agree on every sample.
        samples = (
            "2026-08-23T00:00:00Z", "2026-08-23T00:00:00+00:00",
            "2026-08-23T00:00:00.100Z", "2026-08-23T00:00:00.1Z",
            "2026-08-23T02:00:00+02:00", "1970-01-01T00:00:00Z",
            "2028-02-29T00:00:00Z", "2026-08-23T00:30:00+02:00",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(
                    cp._rfc3339_epoch_seconds(sample),
                    capacity.rfc3339_to_epoch_seconds(sample))
        for bad in (None, "", "not-a-timestamp", "2026-02-30T00:00:00Z"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    cp._rfc3339_epoch_seconds(bad), capacity.rfc3339_to_epoch_seconds(bad))
                self.assertIsNone(cp._rfc3339_epoch_seconds(bad))


class CapacityWakePreflightFailedEvidenceTest(unittest.TestCase):
    """Named per the gate: test_capacity_wake_preflight_failed_requires_binding_evidence."""

    def test_capacity_wake_preflight_failed_requires_binding_evidence(self):
        # Malformed/missing binding evidence is refused.
        bad_shapes = [
            None,
            {},
            {"capacity_wake_preflight_failure": {}},
            _wake_preflight_failure_evidence(lease_id=""),
            _wake_preflight_failure_evidence(role=None),
            _wake_preflight_failure_evidence(controller_policy_digest="short"),
            _wake_preflight_failure_evidence(candidate_manifest_digest="Z" * 64),
            _wake_preflight_failure_evidence(candidate_manifest_digest=None),
            _wake_preflight_failure_evidence(candidate_index=-1),
            _wake_preflight_failure_evidence(failure_kind="guard_broker_unreachable"),
            _wake_preflight_failure_evidence(failure_kind=None),
            _wake_preflight_failure_evidence(failure_kind="environment_error"),
        ]
        for evidence in bad_shapes:
            with self.subTest(evidence=evidence):
                new_state, reason_code = cp.advance(
                    "preflighting", "capacity_wake_preflight_failed", evidence,
                    expected_candidate=_BOUND_CANDIDATE)
                self.assertEqual(new_state, "preflighting")
                self.assertEqual(reason_code, "capacity_wake_preflight_evidence_missing")

        # Well-shaped evidence naming a genuine binding failure is accepted.
        for failure_kind in (
            "role_mismatch", "session_mismatch", "controller_policy_mismatch",
            "model_effort_mismatch", "candidate_mismatch", "stale_lease",
            "consumed_lease_reused", "cancelled_lease_reused",
        ):
            with self.subTest(failure_kind=failure_kind):
                evidence = _wake_preflight_failure_evidence(failure_kind=failure_kind)
                new_state, reason_code = cp.advance(
                    "preflighting", "capacity_wake_preflight_failed", evidence,
                    expected_candidate=_BOUND_CANDIDATE)
                self.assertEqual(new_state, "awaiting_capacity")
                self.assertEqual(reason_code, "capacity_wake_preflight_failed")

    def test_illegal_outside_preflighting(self):
        # capacity_wake_preflight_failed is legal ONLY from preflighting.
        evidence = _wake_preflight_failure_evidence()
        for state in cp.PHASE_STATES:
            if state == "preflighting":
                continue
            with self.subTest(state=state):
                new_state, reason_code = cp.advance(
                    state, "capacity_wake_preflight_failed", evidence)
                self.assertEqual(new_state, state)
                self.assertEqual(reason_code, "illegal_transition")

    def test_ordinary_environment_preflight_failure_uses_existing_edges(self):
        # A wake-triggered preflight failing for an unrelated environment
        # reason (e.g. guard broker unreachable) still uses the
        # pre-existing preflight_rejected/capability_missing edges to
        # rejected_preflight/needs_authority — never capacity_wake_preflight_failed.
        new_state, reason_code = cp.advance("preflighting", "preflight_rejected")
        self.assertEqual(new_state, "rejected_preflight")
        self.assertEqual(reason_code, "preflight_rejected")

        new_state, reason_code = cp.advance("preflighting", "capability_missing")
        self.assertEqual(new_state, "needs_authority")
        self.assertEqual(reason_code, "capability_missing")


class ExpectedCandidateBindingTest(unittest.TestCase):
    """M3A-REV correction: expected_candidate binding on all three new
    events, each with its OWN distinct mismatch reason_code, and requiring
    the full (digest, index) pair to match — not digest alone.

    M3A-REV-001-RESIDUAL: unlike gate_validated, all three of these events
    also REQUIRE a genuine, well-formed expected_candidate — an absent
    (omitted/None) or malformed one fails closed exactly like a mismatched
    one, never silently skipping the comparison the way gate_validated's
    own opt-out does."""

    def _candidate(self, digest=_HASH_B, index=0):
        return {"candidate_manifest_digest": digest, "candidate_index": index}

    # -- capacity_reserved -----------------------------------------------

    def test_capacity_reserved_matching_candidate_accepted(self):
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", _capacity_evidence(),
            expected_candidate=self._candidate())
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_reserved")

    def test_capacity_reserved_mismatched_digest_refused_with_distinct_reason(self):
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", _capacity_evidence(),
            expected_candidate=self._candidate(digest="d" * 64))
        self.assertEqual(new_state, "running")
        self.assertEqual(reason_code, "capacity_evidence_candidate_mismatch")

    def test_capacity_reserved_mismatched_index_refused(self):
        # Pair truth: index alone differing is also a mismatch, not just digest.
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", _capacity_evidence(),
            expected_candidate=self._candidate(index=7))
        self.assertEqual(new_state, "running")
        self.assertEqual(reason_code, "capacity_evidence_candidate_mismatch")

    def test_capacity_reserved_missing_expected_candidate_fails_closed(self):
        # M3A-REV-001-RESIDUAL: omitting expected_candidate no longer skips
        # the check (unlike gate_validated) — it fails closed instead.
        new_state, reason_code = cp.advance("running", "capacity_reserved", _capacity_evidence())
        self.assertEqual(new_state, "running")
        self.assertEqual(reason_code, "capacity_evidence_expected_candidate_required")

    def test_capacity_reserved_malformed_expected_candidate_fails_closed(self):
        bad_candidates = [
            {},
            {"candidate_manifest_digest": _HASH_B},
            {"candidate_manifest_digest": _HASH_B, "candidate_index": 0, "extra": 1},
            {"candidate_manifest_digest": "short", "candidate_index": 0},
            {"candidate_manifest_digest": None, "candidate_index": 0},
            {"candidate_manifest_digest": "Z" * 64, "candidate_index": 0},
            {"candidate_manifest_digest": _HASH_B, "candidate_index": -1},
            {"candidate_manifest_digest": _HASH_B, "candidate_index": True},
            {"candidate_manifest_digest": _HASH_B, "candidate_index": "0"},
        ]
        for bad in bad_candidates:
            with self.subTest(expected_candidate=bad):
                new_state, reason_code = cp.advance(
                    "running", "capacity_reserved", _capacity_evidence(),
                    expected_candidate=bad)
                self.assertEqual(new_state, "running")
                self.assertEqual(reason_code, "capacity_evidence_expected_candidate_required")

    # -- capacity_wake_claimed ---------------------------------------------

    def test_capacity_wake_claimed_matching_candidate_accepted(self):
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", _wake_evidence_trustworthy(),
            expected_candidate=self._candidate())
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_claimed")

    def test_capacity_wake_claimed_mismatched_digest_refused_with_distinct_reason(self):
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", _wake_evidence_trustworthy(),
            expected_candidate=self._candidate(digest="d" * 64))
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_evidence_candidate_mismatch")

    def test_capacity_wake_claimed_mismatched_index_refused(self):
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", _wake_evidence_trustworthy(),
            expected_candidate=self._candidate(index=7))
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_evidence_candidate_mismatch")

    def test_capacity_wake_claimed_missing_expected_candidate_fails_closed(self):
        new_state, reason_code = cp.advance(
            "awaiting_capacity", "capacity_wake_claimed", _wake_evidence_trustworthy())
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_evidence_expected_candidate_required")

    def test_capacity_wake_claimed_malformed_expected_candidate_fails_closed(self):
        bad_candidates = [
            None,
            {},
            {"candidate_manifest_digest": _HASH_B},
            {"candidate_manifest_digest": "short", "candidate_index": 0},
            {"candidate_manifest_digest": _HASH_B, "candidate_index": -1},
        ]
        for bad in bad_candidates:
            with self.subTest(expected_candidate=bad):
                new_state, reason_code = cp.advance(
                    "awaiting_capacity", "capacity_wake_claimed", _wake_evidence_trustworthy(),
                    expected_candidate=bad)
                self.assertEqual(new_state, "awaiting_capacity")
                self.assertEqual(
                    reason_code, "capacity_wake_evidence_expected_candidate_required")

    # -- capacity_wake_preflight_failed -------------------------------------

    def test_capacity_wake_preflight_failed_matching_candidate_accepted(self):
        new_state, reason_code = cp.advance(
            "preflighting", "capacity_wake_preflight_failed", _wake_preflight_failure_evidence(),
            expected_candidate=self._candidate())
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason_code, "capacity_wake_preflight_failed")

    def test_capacity_wake_preflight_failed_mismatched_digest_refused_with_distinct_reason(self):
        new_state, reason_code = cp.advance(
            "preflighting", "capacity_wake_preflight_failed", _wake_preflight_failure_evidence(),
            expected_candidate=self._candidate(digest="d" * 64))
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_preflight_evidence_candidate_mismatch")

    def test_capacity_wake_preflight_failed_mismatched_index_refused(self):
        new_state, reason_code = cp.advance(
            "preflighting", "capacity_wake_preflight_failed", _wake_preflight_failure_evidence(),
            expected_candidate=self._candidate(index=7))
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(reason_code, "capacity_wake_preflight_evidence_candidate_mismatch")

    def test_capacity_wake_preflight_failed_missing_expected_candidate_fails_closed(self):
        new_state, reason_code = cp.advance(
            "preflighting", "capacity_wake_preflight_failed", _wake_preflight_failure_evidence())
        self.assertEqual(new_state, "preflighting")
        self.assertEqual(
            reason_code, "capacity_wake_preflight_evidence_expected_candidate_required")

    def test_capacity_wake_preflight_failed_malformed_expected_candidate_fails_closed(self):
        bad_candidates = [
            None,
            {},
            {"candidate_manifest_digest": _HASH_B},
            {"candidate_manifest_digest": "short", "candidate_index": 0},
            {"candidate_manifest_digest": _HASH_B, "candidate_index": -1},
        ]
        for bad in bad_candidates:
            with self.subTest(expected_candidate=bad):
                new_state, reason_code = cp.advance(
                    "preflighting", "capacity_wake_preflight_failed",
                    _wake_preflight_failure_evidence(), expected_candidate=bad)
                self.assertEqual(new_state, "preflighting")
                self.assertEqual(
                    reason_code,
                    "capacity_wake_preflight_evidence_expected_candidate_required")

    # -- cross-event invariants ----------------------------------------

    def test_all_reason_codes_are_distinct_from_each_other_and_from_gate(self):
        reasons = {
            "capacity_evidence_candidate_mismatch",
            "capacity_wake_evidence_candidate_mismatch",
            "capacity_wake_preflight_evidence_candidate_mismatch",
            "capacity_evidence_expected_candidate_required",
            "capacity_wake_evidence_expected_candidate_required",
            "capacity_wake_preflight_evidence_expected_candidate_required",
            "gate_evidence_candidate_mismatch",
        }
        self.assertEqual(len(reasons), 7, "all seven reason codes must be distinct")

    def test_missing_evidence_reason_reported_before_candidate_is_compared(self):
        # Shape/evidence-missing takes priority over a candidate mismatch,
        # exactly like gate_validated's existing ordering.
        new_state, reason_code = cp.advance(
            "running", "capacity_reserved", None, expected_candidate=self._candidate())
        self.assertEqual(new_state, "running")
        self.assertEqual(reason_code, "capacity_evidence_missing")

    def test_missing_evidence_reason_reported_before_expected_candidate_is_checked(self):
        # Shape/evidence-missing also takes priority over an absent/malformed
        # expected_candidate — evidence shape is always checked first.
        new_state, reason_code = cp.advance("running", "capacity_reserved", None)
        self.assertEqual(new_state, "running")
        self.assertEqual(reason_code, "capacity_evidence_missing")

    def test_gate_validated_unaffected_still_skips_when_expected_candidate_omitted(self):
        # Preserve every other v1 correction/invariant: gate_validated's own
        # documented opt-out (omitting expected_candidate skips the
        # comparison) is untouched by this residual, which is scoped to
        # exactly the three M3 capacity events.
        new_state, reason_code = cp.advance("awaiting_gate", "gate_validated", {
            "gate_validation": {
                "candidate_manifest_digest": _HASH_A, "candidate_index": 0, "verdict": "pass"}})
        self.assertEqual(new_state, "completed")
        self.assertEqual(reason_code, "gate_validated")


class ReachabilityAndEvidenceExhaustivenessTest(unittest.TestCase):
    def _evidence_for(self, event):
        if event == "gate_validated":
            return {"gate_validation": {
                "candidate_manifest_digest": _HASH_A, "candidate_index": 0, "verdict": "pass"}}
        if event == "capacity_reserved":
            return _capacity_evidence()
        if event == "capacity_wake_claimed":
            return _wake_evidence_trustworthy()
        if event == "capacity_wake_preflight_failed":
            return _wake_preflight_failure_evidence()
        return None

    def test_full_matrix_never_raises(self):
        for state, event in itertools.product(cp.PHASE_STATES, cp.EVENTS):
            cp.advance(state, event, self._evidence_for(event))  # must not raise

    def test_awaiting_capacity_only_resumes_via_capacity_wake_claimed(self):
        for event in cp.EVENTS:
            if event == "capacity_wake_claimed":
                continue
            with self.subTest(event=event):
                new_state, _ = cp.advance("awaiting_capacity", event, self._evidence_for(event))
                self.assertIn(new_state, ("awaiting_capacity", "cancelled", "aborted"))


if __name__ == "__main__":
    unittest.main()
