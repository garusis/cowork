#!/usr/bin/env python3
"""Tests for cowork_capacity: M3 Package A provider-capacity contracts.

Run standalone:

    python3 -m unittest scripts/test_cowork_capacity.py -v
"""

import ast
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_capacity as capacity  # noqa: E402

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _binding(**overrides):
    binding = {
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": _HASH_A,
        "candidate_digest": _HASH_B,
        "artifact_hashes": {"manifest": _HASH_C},
    }
    binding.update(overrides)
    return binding


def _capacity_source(**overrides):
    source = {"kind": "provider_header", "sha256": _HASH_A}
    source.update(overrides)
    return source


def _wakeup_scheduled(**overrides):
    wakeup = {
        "lease_id": "lease-1",
        "automation_ref": "scheduler-ref-1",
        "not_before": "2026-08-23T00:00:00Z",
    }
    wakeup.update(overrides)
    return wakeup


def _wakeup_manual(**overrides):
    wakeup = {
        "lease_id": None,
        "automation_ref": "scheduler-ref-1",
        "not_before": None,
    }
    wakeup.update(overrides)
    return wakeup


def _manual_resume_inert(**overrides):
    manual_resume = {"condition": None, "accepted_source": None, "signal_journal_ref": None}
    manual_resume.update(overrides)
    return manual_resume


def _manual_resume_active(**overrides):
    manual_resume = {
        "condition": "authenticated capacity-available signal",
        "accepted_source": "top_level_authority_adapter",
        "signal_journal_ref": None,
    }
    manual_resume.update(overrides)
    return manual_resume


def _capacity_packet_scheduled(**overrides):
    packet = {
        "schema_version": 1,
        "package_id": "m3-a-contracts",
        "provider_capacity_class": "subscription_quota_exhausted",
        "provider": "anthropic",
        "resume_mode": "scheduled",
        "retry_after": "2026-08-23T00:00:00Z",
        "capacity_source": _capacity_source(),
        "binding": _binding(),
        "wakeup": _wakeup_scheduled(),
        "manual_resume": _manual_resume_inert(),
        "issued_at": "2026-08-22T23:00:00Z",
    }
    packet.update(overrides)
    return packet


def _capacity_packet_manual(**overrides):
    packet = {
        "schema_version": 1,
        "package_id": "m3-a-contracts",
        "provider_capacity_class": "subscription_quota_exhausted",
        "provider": "anthropic",
        "resume_mode": "manual_signal",
        "retry_after": None,
        "capacity_source": _capacity_source(kind="unknown"),
        "binding": _binding(),
        "wakeup": _wakeup_manual(),
        "manual_resume": _manual_resume_active(),
        "issued_at": "2026-08-22T23:00:00Z",
    }
    packet.update(overrides)
    return packet


def _pause_lease(**overrides):
    lease = {
        "schema_version": 1,
        "package_id": "m3-a-contracts",
        "lease_id": "lease-1",
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": _HASH_A,
        "candidate_digest": _HASH_B,
        "resume_mode": "scheduled",
        "not_before": "2026-08-23T00:00:00Z",
        "automation_ref": "scheduler-ref-1",
        "artifact_hashes": {"manifest": _HASH_C},
        "consumption_state": "unclaimed",
        "failed_wake_attempts": 0,
        "issued_at": "2026-08-22T23:00:00Z",
    }
    lease.update(overrides)
    return lease


def _invalidation_record(**overrides):
    record = {
        "schema_version": 1,
        "package_id": "m3-a-contracts",
        "invalidated_candidate_digest": _HASH_A,
        "invalidated_session_id": "sess-1",
        "invalidated_work_id": "work-1",
        "invalidating_principal": "top_level_authority_adapter",
        "reason": "duplicate paired-work replay detected",
        "evidence_refs": [{"path": "evidence/1.json", "sha256": _HASH_B}],
        "issued_at": "2026-08-22T23:00:00Z",
    }
    record.update(overrides)
    return record


def _manual_signal(**overrides):
    record = {
        "schema_version": 1,
        "package_id": "m3-a-contracts",
        "candidate_digest": _HASH_A,
        "role": "builder",
        "provider_session_id": "sess-1",
        "controller_policy_digest": _HASH_B,
        "signal_journal_ref": "journal-ref-1",
        "detached_signature": "3045022100" + "d" * 118,
        "signer_public_key_id": "key-1",
        "issued_at": "2026-08-22T23:00:00Z",
    }
    record.update(overrides)
    return record


class ControllerOutcomeTaxonomyTest(unittest.TestCase):
    def test_taxonomy_is_closed_and_matches_spec(self):
        self.assertEqual(capacity.CONTROLLER_OUTCOMES, (
            "quota_limited", "overloaded", "authentication_failed",
            "policy_blocked", "guard_unavailable", "transport_failed",
            "malformed_output", "local_guard_exhausted", "unknown_provider_failure",
        ))
        self.assertEqual(capacity.CONTROLLER_OUTCOME_SET, frozenset(capacity.CONTROLLER_OUTCOMES))
        self.assertEqual(len(capacity.CONTROLLER_OUTCOMES), len(capacity.CONTROLLER_OUTCOME_SET))

    def test_capacity_eligible_outcomes_are_exactly_quota_and_overload(self):
        self.assertEqual(capacity.CAPACITY_ELIGIBLE_OUTCOMES, {"quota_limited", "overloaded"})
        self.assertTrue(capacity.CAPACITY_ELIGIBLE_OUTCOMES.issubset(capacity.CONTROLLER_OUTCOME_SET))

    def test_non_capacity_terminal_outcomes_excluded_from_eligible(self):
        self.assertEqual(
            capacity.NON_CAPACITY_TERMINAL_OUTCOMES,
            {"local_guard_exhausted", "unknown_provider_failure"})
        self.assertFalse(
            capacity.NON_CAPACITY_TERMINAL_OUTCOMES & capacity.CAPACITY_ELIGIBLE_OUTCOMES)

    def test_malformed_output_distinct_from_unknown_provider_failure(self):
        # malformed_output: a SUCCESSFUL turn with bad JSON.
        # unknown_provider_failure: a FAILED turn of an unrecognized shape.
        self.assertIn("malformed_output", capacity.CONTROLLER_OUTCOME_SET)
        self.assertIn("unknown_provider_failure", capacity.CONTROLLER_OUTCOME_SET)
        self.assertNotEqual("malformed_output", "unknown_provider_failure")

    def test_validate_controller_outcome(self):
        for outcome in capacity.CONTROLLER_OUTCOMES:
            self.assertTrue(capacity.validate_controller_outcome(outcome))
        for bad in ("not_an_outcome", None, 42, "", "quota_limited "):
            self.assertFalse(capacity.validate_controller_outcome(bad))


class TrustSourceClassificationTest(unittest.TestCase):
    def test_trust_source_classification_closed_set(self):
        for kind in ("provider_event", "provider_header", "provider_api"):
            self.assertEqual(capacity.classify_trust_source(kind), "trustworthy")
        for bad in (
            "unknown", "", None, 42, [], {}, "PROVIDER_EVENT",
            "provider_event ", "manual", True,
        ):
            self.assertEqual(
                capacity.classify_trust_source(bad), "untrustworthy",
                "expected untrustworthy for %r" % (bad,))

    def test_never_defaults_to_trustworthy_on_unrecognized_value(self):
        for bad in ("provider", "event", "header", "api", "trusted"):
            self.assertEqual(capacity.classify_trust_source(bad), "untrustworthy")

    def test_never_raises_on_malformed_input(self):
        for bad in (None, 1, 1.5, [], {}, (), object()):
            capacity.classify_trust_source(bad)  # must not raise


class RetryAfterParsingTest(unittest.TestCase):
    def test_parses_rfc3339_timestamp(self):
        for raw in ("2026-08-23T00:00:00Z", "2026-08-23T00:00:00.123Z", "2026-08-23T00:00:00+02:00"):
            result = capacity.parse_retry_after_text(raw)
            self.assertEqual(result, {"kind": "timestamp", "value": raw})

    def test_parses_duration_seconds(self):
        self.assertEqual(
            capacity.parse_retry_after_text("30"), {"kind": "duration_seconds", "value": 30.0})
        self.assertEqual(
            capacity.parse_retry_after_text("30s"), {"kind": "duration_seconds", "value": 30.0})
        self.assertEqual(
            capacity.parse_retry_after_text("30.5s"), {"kind": "duration_seconds", "value": 30.5})

    def test_malformed_text_returns_none_never_raises(self):
        for bad in (None, 42, "", "soon", "tomorrow", "P30S", "-30s", "2026-08-23"):
            self.assertIsNone(capacity.parse_retry_after_text(bad))

    def test_duration_within_horizon_accepted(self):
        result = capacity.parse_retry_after_text(str(capacity.MAX_RETRY_HORIZON_SECONDS))
        self.assertEqual(result, {"kind": "duration_seconds", "value": float(capacity.MAX_RETRY_HORIZON_SECONDS)})

    def test_duration_beyond_horizon_rejected(self):
        too_long = capacity.MAX_RETRY_HORIZON_SECONDS + 1
        self.assertIsNone(capacity.parse_retry_after_text(str(too_long)))
        self.assertIsNone(capacity.parse_retry_after_text("%ss" % too_long))


class Rfc3339CanonicalComparisonTest(unittest.TestCase):
    """M3A-REV correction: canonical/normalized RFC3339 comparison, not raw
    lexicographic string comparison."""

    def test_z_and_plus_zero_offset_are_the_same_instant(self):
        self.assertEqual(
            capacity.rfc3339_to_epoch_seconds("2026-08-23T00:00:00Z"),
            capacity.rfc3339_to_epoch_seconds("2026-08-23T00:00:00+00:00"))

    def test_fractional_second_formatting_does_not_change_instant(self):
        self.assertEqual(
            capacity.rfc3339_to_epoch_seconds("2026-08-23T00:00:00.100Z"),
            capacity.rfc3339_to_epoch_seconds("2026-08-23T00:00:00.1Z"))

    def test_different_offsets_can_name_the_same_instant(self):
        # 2026-08-23T02:00:00+02:00 == 2026-08-23T00:00:00Z
        self.assertEqual(
            capacity.rfc3339_to_epoch_seconds("2026-08-23T02:00:00+02:00"),
            capacity.rfc3339_to_epoch_seconds("2026-08-23T00:00:00Z"))

    def test_lexicographic_comparison_would_be_wrong_here(self):
        # "2026-08-23T00:30:00+02:00" (== 2026-08-22T22:30:00Z) sorts AFTER
        # "2026-08-23T00:00:00Z" lexicographically, but is actually the
        # EARLIER instant -- proving raw string comparison is unsafe and
        # canonical epoch comparison is required.
        later_string_earlier_instant = "2026-08-23T00:30:00+02:00"
        earlier_string_later_instant = "2026-08-23T00:00:00Z"
        self.assertGreater(later_string_earlier_instant, earlier_string_later_instant)
        self.assertLess(
            capacity.rfc3339_to_epoch_seconds(later_string_earlier_instant),
            capacity.rfc3339_to_epoch_seconds(earlier_string_later_instant))

    def test_invalid_calendar_date_rejected(self):
        for bad in ("2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-00-01T00:00:00Z"):
            self.assertIsNone(capacity.rfc3339_to_epoch_seconds(bad))

    def test_leap_year_february_29_accepted(self):
        self.assertIsNotNone(capacity.rfc3339_to_epoch_seconds("2028-02-29T00:00:00Z"))

    def test_non_leap_year_february_29_rejected(self):
        self.assertIsNone(capacity.rfc3339_to_epoch_seconds("2026-02-29T00:00:00Z"))

    def test_malformed_or_wrong_type_returns_none(self):
        for bad in (None, 42, "", "not-a-timestamp", "2026-08-23"):
            self.assertIsNone(capacity.rfc3339_to_epoch_seconds(bad))

    def test_epoch_reference_point(self):
        self.assertEqual(capacity.rfc3339_to_epoch_seconds("1970-01-01T00:00:00Z"), 0.0)


class CapacitySourceTest(unittest.TestCase):
    def test_valid_capacity_source_normalizes(self):
        normalized = capacity.validate_capacity_source(_capacity_source())
        self.assertEqual(normalized, _capacity_source())

    def test_missing_keys_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_capacity_source({"kind": "provider_event"})

    def test_extra_keys_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_capacity_source(_capacity_source(extra="nope"))

    def test_malformed_sha256_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_capacity_source(_capacity_source(sha256="short"))

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_capacity_source("not-a-dict")


class CapacityPacketTest(unittest.TestCase):
    def test_valid_scheduled_packet_normalizes(self):
        normalized = capacity.validate_capacity_packet(_capacity_packet_scheduled())
        self.assertEqual(normalized["resume_mode"], "scheduled")
        self.assertEqual(normalized["binding"]["artifact_hashes"], {"manifest": _HASH_C})
        self.assertEqual(normalized["wakeup"]["automation_ref"], "scheduler-ref-1")

    def test_valid_manual_signal_packet_normalizes(self):
        normalized = capacity.validate_capacity_packet(_capacity_packet_manual())
        self.assertEqual(normalized["resume_mode"], "manual_signal")
        self.assertIsNone(normalized["wakeup"]["lease_id"])
        self.assertIsNone(normalized["retry_after"])

    def test_artifact_hashes_required_and_nonempty(self):
        packet = _capacity_packet_scheduled(binding=_binding(artifact_hashes={}))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_automation_ref_required_even_for_manual_signal(self):
        packet = _capacity_packet_manual(wakeup=_wakeup_manual(automation_ref=""))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_provider_capacity_class_closed_to_subscription_only(self):
        packet = _capacity_packet_scheduled(provider_capacity_class="credits_exhausted")
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_scheduled_requires_retry_after(self):
        packet = _capacity_packet_scheduled(retry_after=None)
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_manual_signal_forbids_retry_after(self):
        packet = _capacity_packet_manual(retry_after="30s")
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_scheduled_requires_lease_id_and_not_before(self):
        packet = _capacity_packet_scheduled(wakeup=_wakeup_scheduled(lease_id=None))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)
        packet = _capacity_packet_scheduled(wakeup=_wakeup_scheduled(not_before=None))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_manual_signal_forbids_lease_id_and_not_before(self):
        packet = _capacity_packet_manual(wakeup=_wakeup_manual(lease_id="lease-1"))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)
        packet = _capacity_packet_manual(wakeup=_wakeup_manual(not_before="2026-08-23T00:00:00Z"))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_manual_signal_requires_accepted_source(self):
        packet = _capacity_packet_manual(manual_resume=_manual_resume_active(accepted_source=None))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_manual_resume_accepted_source_closed_set(self):
        packet = _capacity_packet_manual(
            manual_resume=_manual_resume_active(accepted_source="self_asserted_human"))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_missing_keys_rejected(self):
        packet = _capacity_packet_scheduled()
        del packet["issued_at"]
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_extra_keys_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(_capacity_packet_scheduled(extra="nope"))

    def test_bad_schema_version_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(_capacity_packet_scheduled(schema_version=2))

    def test_binding_candidate_digest_must_be_hex64(self):
        packet = _capacity_packet_scheduled(binding=_binding(candidate_digest="short"))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_never_mutates_input(self):
        import copy
        packet = _capacity_packet_scheduled()
        snapshot = copy.deepcopy(packet)
        capacity.validate_capacity_packet(packet)
        self.assertEqual(packet, snapshot)

    def test_returned_binding_artifact_hashes_is_independent_copy(self):
        # M3A-REV correction: deep-copy nested returns. Mutating the
        # returned normalized dict's nested artifact_hashes must never
        # mutate the caller's original input dict.
        packet = _capacity_packet_scheduled()
        normalized = capacity.validate_capacity_packet(packet)
        normalized["binding"]["artifact_hashes"]["manifest"] = "z" * 64
        self.assertEqual(packet["binding"]["artifact_hashes"]["manifest"], _HASH_C)
        self.assertNotEqual(
            normalized["binding"]["artifact_hashes"], packet["binding"]["artifact_hashes"])

    def test_retry_after_timestamp_beyond_horizon_rejected(self):
        far_future = "2027-08-23T00:00:00Z"  # > 7 days past issued_at
        packet = _capacity_packet_scheduled(
            retry_after=far_future, wakeup=_wakeup_scheduled(not_before=far_future))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_retry_after_timestamp_before_issued_at_rejected(self):
        before_issued = "2026-08-01T00:00:00Z"
        packet = _capacity_packet_scheduled(
            retry_after=before_issued, wakeup=_wakeup_scheduled(not_before=before_issued))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_wakeup_not_before_beyond_horizon_rejected_even_when_retry_after_is_fine(self):
        packet = _capacity_packet_scheduled(
            wakeup=_wakeup_scheduled(not_before="2027-08-23T00:00:00Z"))
        with self.assertRaises(ValueError):
            capacity.validate_capacity_packet(packet)

    def test_retry_after_at_horizon_boundary_accepted(self):
        # issued_at + MAX_RETRY_HORIZON_SECONDS exactly, expressed as an
        # RFC3339 timestamp -- must be accepted (delta <= horizon, not <).
        issued_epoch = capacity.rfc3339_to_epoch_seconds("2026-08-22T23:00:00Z")
        boundary_epoch = issued_epoch + capacity.MAX_RETRY_HORIZON_SECONDS
        # 2026-08-22T23:00:00Z + 7 days == 2026-08-29T23:00:00Z
        boundary_timestamp = "2026-08-29T23:00:00Z"
        self.assertEqual(capacity.rfc3339_to_epoch_seconds(boundary_timestamp), boundary_epoch)
        packet = _capacity_packet_scheduled(
            retry_after=boundary_timestamp, wakeup=_wakeup_scheduled(not_before=boundary_timestamp))
        normalized = capacity.validate_capacity_packet(packet)
        self.assertEqual(normalized["retry_after"], boundary_timestamp)


class PauseLeaseTest(unittest.TestCase):
    def test_valid_scheduled_lease_normalizes(self):
        normalized = capacity.validate_pause_lease(_pause_lease())
        self.assertEqual(normalized["consumption_state"], "unclaimed")
        self.assertEqual(normalized["failed_wake_attempts"], 0)

    def test_valid_manual_lease_normalizes(self):
        lease = _pause_lease(resume_mode="manual_signal", not_before=None)
        normalized = capacity.validate_pause_lease(lease)
        self.assertEqual(normalized["resume_mode"], "manual_signal")

    def test_scheduled_requires_not_before(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(not_before=None))

    def test_manual_forbids_not_before(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(
                _pause_lease(resume_mode="manual_signal", not_before="2026-08-23T00:00:00Z"))

    def test_consumption_state_closed_set(self):
        for state in capacity.CONSUMPTION_STATES:
            capacity.validate_pause_lease(_pause_lease(consumption_state=state))
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(consumption_state="active"))

    def test_artifact_hashes_required_nonempty(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(artifact_hashes={}))

    def test_automation_ref_required(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(automation_ref=""))

    def test_missing_keys_rejected(self):
        lease = _pause_lease()
        del lease["consumption_state"]
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(lease)

    def test_extra_keys_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(extra="nope"))

    def test_returned_artifact_hashes_is_independent_copy(self):
        lease = _pause_lease()
        normalized = capacity.validate_pause_lease(lease)
        normalized["artifact_hashes"]["manifest"] = "z" * 64
        self.assertEqual(lease["artifact_hashes"]["manifest"], _HASH_C)
        self.assertNotEqual(normalized["artifact_hashes"], lease["artifact_hashes"])

    def test_not_before_beyond_horizon_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(not_before="2027-08-23T00:00:00Z"))

    def test_not_before_before_issued_at_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(not_before="2026-08-01T00:00:00Z"))

    def test_not_before_at_horizon_boundary_accepted(self):
        boundary_timestamp = "2026-08-29T23:00:00Z"  # issued_at + 7 days
        normalized = capacity.validate_pause_lease(_pause_lease(not_before=boundary_timestamp))
        self.assertEqual(normalized["not_before"], boundary_timestamp)


class M3RN06FailedWakeAttemptsTest(unittest.TestCase):
    """M3R-N06: durable failed-wake-attempt counter/ceiling contract."""

    def test_initial_value_is_zero(self):
        lease = capacity.validate_pause_lease(_pause_lease())
        self.assertEqual(lease["failed_wake_attempts"], 0)

    def test_counter_rejects_negative(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(_pause_lease(failed_wake_attempts=-1))

    def test_counter_rejects_non_int(self):
        for bad in (1.5, "1", True, None):
            with self.assertRaises(ValueError):
                capacity.validate_pause_lease(_pause_lease(failed_wake_attempts=bad))

    def test_counter_rejects_value_beyond_ceiling(self):
        with self.assertRaises(ValueError):
            capacity.validate_pause_lease(
                _pause_lease(failed_wake_attempts=capacity.FAILED_WAKE_ATTEMPT_CEILING + 1))

    def test_counter_accepts_value_at_ceiling(self):
        lease = capacity.validate_pause_lease(
            _pause_lease(failed_wake_attempts=capacity.FAILED_WAKE_ATTEMPT_CEILING))
        self.assertEqual(lease["failed_wake_attempts"], capacity.FAILED_WAKE_ATTEMPT_CEILING)

    def test_increment_contract_adds_exactly_one(self):
        lease = _pause_lease(failed_wake_attempts=0)
        for expected in range(1, capacity.FAILED_WAKE_ATTEMPT_CEILING + 1):
            lease = capacity.record_failed_wake_attempt(lease)
            self.assertEqual(lease["failed_wake_attempts"], expected)

    def test_increment_never_mutates_input(self):
        lease = _pause_lease(failed_wake_attempts=0)
        capacity.record_failed_wake_attempt(lease)
        self.assertEqual(lease["failed_wake_attempts"], 0)

    def test_increment_refuses_once_at_ceiling(self):
        lease = _pause_lease(failed_wake_attempts=capacity.FAILED_WAKE_ATTEMPT_CEILING)
        with self.assertRaises(ValueError):
            capacity.record_failed_wake_attempt(lease)

    def test_boundary_value_not_exhausted_below_ceiling(self):
        lease = _pause_lease(failed_wake_attempts=capacity.FAILED_WAKE_ATTEMPT_CEILING - 1)
        self.assertFalse(capacity.wake_attempts_exhausted(lease))
        self.assertEqual(capacity.capacity_wake_decision(lease), "wake_retry_eligible")

    def test_boundary_value_exhausted_at_ceiling(self):
        lease = _pause_lease(failed_wake_attempts=capacity.FAILED_WAKE_ATTEMPT_CEILING)
        self.assertTrue(capacity.wake_attempts_exhausted(lease))
        self.assertEqual(capacity.capacity_wake_decision(lease), "wake_attempts_exhausted")

    def test_stop_after_ceiling_never_implies_further_retry(self):
        # Simulate repeated failed-wake cycling up to the ceiling; the
        # decision must stop (never keep returning "retry eligible") once
        # the ceiling is reached, and further increments are refused —
        # there is no path back to an unbounded cycle.
        lease = _pause_lease(failed_wake_attempts=0)
        decisions = []
        for _ in range(capacity.FAILED_WAKE_ATTEMPT_CEILING):
            decisions.append(capacity.capacity_wake_decision(lease))
            lease = capacity.record_failed_wake_attempt(lease)
        decisions.append(capacity.capacity_wake_decision(lease))

        self.assertEqual(
            decisions,
            ["wake_retry_eligible"] * capacity.FAILED_WAKE_ATTEMPT_CEILING
            + ["wake_attempts_exhausted"])
        with self.assertRaises(ValueError):
            capacity.record_failed_wake_attempt(lease)


class LeaseReplacementMonotonicityTest(unittest.TestCase):
    """M3A-REV correction: counter monotonic across replacement — a fresh
    lease for the same binding must never regress failed_wake_attempts
    below the old lease's count."""

    def test_carries_forward_higher_old_count(self):
        old = _pause_lease(failed_wake_attempts=3)
        new = _pause_lease(lease_id="lease-2", failed_wake_attempts=0)
        replaced = capacity.next_pause_lease_after_replacement(old, new)
        self.assertEqual(replaced["failed_wake_attempts"], 3)
        self.assertEqual(replaced["lease_id"], "lease-2")

    def test_keeps_higher_new_count_when_new_already_exceeds_old(self):
        old = _pause_lease(failed_wake_attempts=1)
        new = _pause_lease(lease_id="lease-2", failed_wake_attempts=4)
        replaced = capacity.next_pause_lease_after_replacement(old, new)
        self.assertEqual(replaced["failed_wake_attempts"], 4)

    def test_never_regresses_below_old_count(self):
        old = _pause_lease(failed_wake_attempts=capacity.FAILED_WAKE_ATTEMPT_CEILING)
        new = _pause_lease(lease_id="lease-2", failed_wake_attempts=0)
        replaced = capacity.next_pause_lease_after_replacement(old, new)
        self.assertEqual(replaced["failed_wake_attempts"], capacity.FAILED_WAKE_ATTEMPT_CEILING)
        self.assertTrue(capacity.wake_attempts_exhausted(replaced))

    def test_does_not_mutate_either_input(self):
        old = _pause_lease(failed_wake_attempts=3)
        new = _pause_lease(lease_id="lease-2", failed_wake_attempts=0)
        capacity.next_pause_lease_after_replacement(old, new)
        self.assertEqual(old["failed_wake_attempts"], 3)
        self.assertEqual(new["failed_wake_attempts"], 0)

    def test_repeated_replacement_chain_stays_monotonic(self):
        lease = _pause_lease(lease_id="lease-0", failed_wake_attempts=2)
        for i in range(1, 4):
            fresh = _pause_lease(lease_id="lease-%d" % i, failed_wake_attempts=0)
            lease = capacity.next_pause_lease_after_replacement(lease, fresh)
            self.assertEqual(lease["failed_wake_attempts"], 2)


class InvalidationRecordTest(unittest.TestCase):
    def test_valid_record_normalizes(self):
        normalized = capacity.validate_invalidation_record(_invalidation_record())
        self.assertEqual(normalized["evidence_refs"], [{"path": "evidence/1.json", "sha256": _HASH_B}])
        self.assertIsInstance(normalized["evidence_refs"], list)

    def test_evidence_refs_preserves_json_list_round_trip(self):
        # A record validated directly, and the same record round-tripped
        # through json.dumps/json.loads (which always decodes a JSON array
        # to a list, never a tuple), must normalize to the exact same type
        # and value -- a tuple-typed normalization would silently diverge
        # from the post-round-trip shape.
        import json
        direct = capacity.validate_invalidation_record(_invalidation_record())
        round_tripped = capacity.validate_invalidation_record(
            json.loads(json.dumps(_invalidation_record())))
        self.assertEqual(direct, round_tripped)
        self.assertIsInstance(round_tripped["evidence_refs"], list)

    def test_evidence_refs_required_nonempty(self):
        with self.assertRaises(ValueError):
            capacity.validate_invalidation_record(_invalidation_record(evidence_refs=[]))

    def test_evidence_refs_entry_shape_enforced(self):
        with self.assertRaises(ValueError):
            capacity.validate_invalidation_record(
                _invalidation_record(evidence_refs=[{"path": "x"}]))

    def test_missing_keys_rejected(self):
        record = _invalidation_record()
        del record["reason"]
        with self.assertRaises(ValueError):
            capacity.validate_invalidation_record(record)

    def test_extra_keys_rejected_no_edit_or_delete_fields(self):
        # The schema carries no "edited_fields"/"deleted"-style key at all —
        # any attempt to smuggle one in is refused like any other malformed
        # shape, keeping the record append-only by construction.
        with self.assertRaises(ValueError):
            capacity.validate_invalidation_record(_invalidation_record(edited_fields=["reason"]))

    def test_malformed_digest_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_invalidation_record(
                _invalidation_record(invalidated_candidate_digest="short"))


class ManualCapacitySignalTest(unittest.TestCase):
    def test_valid_signal_normalizes(self):
        normalized = capacity.validate_manual_capacity_signal(_manual_signal())
        self.assertEqual(normalized["signer_public_key_id"], "key-1")

    def test_rejects_plaintext_authorized_flag(self):
        record = _manual_signal()
        record["authorized"] = True
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(record)

    def test_missing_signature_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(_manual_signal(detached_signature=""))

    def test_non_hex_signature_rejected(self):
        # M3A-REV correction: signature must be signature-SHAPED, not
        # merely any nonempty string.
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(
                _manual_signal(detached_signature="not-a-hex-signature!!"))

    def test_too_short_signature_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(_manual_signal(detached_signature="ab" * 10))

    def test_uppercase_hex_signature_rejected(self):
        # Lowercase-only, matching this module's other hex-digest convention.
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(
                _manual_signal(detached_signature="AB" * 20))

    def test_minimum_length_hex_signature_accepted(self):
        normalized = capacity.validate_manual_capacity_signal(
            _manual_signal(detached_signature="a" * 32))
        self.assertEqual(normalized["detached_signature"], "a" * 32)

    def test_missing_signer_key_id_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(_manual_signal(signer_public_key_id=None))

    def test_missing_journal_ref_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(_manual_signal(signal_journal_ref=""))

    def test_extra_keys_rejected(self):
        with self.assertRaises(ValueError):
            capacity.validate_manual_capacity_signal(_manual_signal(extra="nope"))


class PauseUntilEligibleEquivalenceTest(unittest.TestCase):
    def test_pause_until_eligible_equivalence_declared(self):
        # The constant exists, is a nonempty string, and is imported (not
        # restated) by name — establishing the single source of truth every
        # later package must cite.
        self.assertIsInstance(capacity.PAUSE_UNTIL_ELIGIBLE_EQUIVALENCE, str)
        self.assertIn("pause_until_eligible", capacity.PAUSE_UNTIL_ELIGIBLE_EQUIVALENCE)
        self.assertIn("resume_mode", capacity.PAUSE_UNTIL_ELIGIBLE_EQUIVALENCE)
        self.assertIn("not_before", capacity.PAUSE_UNTIL_ELIGIBLE_EQUIVALENCE)

    def test_scheduled_packet_is_pause_until_eligible(self):
        self.assertTrue(
            capacity.is_pause_until_eligible(capacity_packet=_capacity_packet_scheduled()))

    def test_manual_packet_is_not_pause_until_eligible(self):
        self.assertFalse(
            capacity.is_pause_until_eligible(capacity_packet=_capacity_packet_manual()))

    def test_scheduled_lease_is_pause_until_eligible(self):
        self.assertTrue(capacity.is_pause_until_eligible(pause_lease=_pause_lease()))

    def test_manual_lease_is_not_pause_until_eligible(self):
        lease = _pause_lease(resume_mode="manual_signal", not_before=None)
        self.assertFalse(capacity.is_pause_until_eligible(pause_lease=lease))

    def test_agreeing_packet_and_lease_accepted(self):
        self.assertTrue(capacity.is_pause_until_eligible(
            capacity_packet=_capacity_packet_scheduled(), pause_lease=_pause_lease()))

    def test_disagreeing_packet_and_lease_raises(self):
        with self.assertRaises(ValueError):
            capacity.is_pause_until_eligible(
                capacity_packet=_capacity_packet_scheduled(),
                pause_lease=_pause_lease(resume_mode="manual_signal", not_before=None))

    def test_requires_at_least_one_argument(self):
        with self.assertRaises(ValueError):
            capacity.is_pause_until_eligible()

    def test_mismatched_identity_raises_even_when_booleans_agree(self):
        # M3A-REV correction: bind pause-equivalence identities. Two
        # unrelated records that happen to agree on the boolean
        # pause_until_eligible value must still be refused if they name
        # different bindings.
        packet = _capacity_packet_scheduled(binding=_binding(role="other-role"))
        lease = _pause_lease()  # role="builder" -- both are "eligible", but for different bindings
        self.assertTrue(capacity.is_pause_until_eligible(capacity_packet=packet))
        self.assertTrue(capacity.is_pause_until_eligible(pause_lease=lease))
        with self.assertRaises(ValueError):
            capacity.is_pause_until_eligible(capacity_packet=packet, pause_lease=lease)

    def test_mismatched_candidate_digest_raises(self):
        packet = _capacity_packet_scheduled(binding=_binding(candidate_digest="d" * 64))
        with self.assertRaises(ValueError):
            capacity.is_pause_until_eligible(capacity_packet=packet, pause_lease=_pause_lease())

    def test_mismatched_provider_session_id_raises(self):
        packet = _capacity_packet_scheduled(binding=_binding(provider_session_id="sess-2"))
        with self.assertRaises(ValueError):
            capacity.is_pause_until_eligible(capacity_packet=packet, pause_lease=_pause_lease())

    def test_mismatched_wakeup_lease_id_raises(self):
        packet = _capacity_packet_scheduled(wakeup=_wakeup_scheduled(lease_id="lease-999"))
        with self.assertRaises(ValueError):
            capacity.is_pause_until_eligible(capacity_packet=packet, pause_lease=_pause_lease())

    def test_matching_identity_with_manual_wakeup_lease_id_skips_lease_id_check(self):
        # A manual_signal packet's wakeup.lease_id is None -- that must not
        # be compared against the lease's own (always-required) lease_id.
        packet = _capacity_packet_manual()
        lease = _pause_lease(resume_mode="manual_signal", not_before=None)
        self.assertFalse(capacity.is_pause_until_eligible(capacity_packet=packet, pause_lease=lease))

    def test_it_is_not_a_second_phase_or_event(self):
        # pause_until_eligible must never appear as a PhaseState or Event
        # name anywhere in the control-plane taxonomy.
        import cowork_control_plane as cp
        self.assertNotIn("pause_until_eligible", cp.PHASE_STATE_SET)
        self.assertNotIn("pause_until_eligible", cp.EVENT_SET)


class ImportAndIOBoundaryTest(unittest.TestCase):
    """cowork_capacity.py imports no runtime module and performs no I/O."""

    _FORBIDDEN_RUNTIME_MODULES = frozenset({
        "cowork", "cowork_bridge", "cowork_state", "cowork_ledger",
        "cowork_preflight", "cowork_dispatch", "cowork_dispatch_manifest",
        "cowork_policy", "cowork_action_policy", "cowork_guard_broker",
        "cowork_trace", "cowork_measure",
    })

    def _module_path(self):
        return os.path.join(_HERE, "cowork_capacity.py")

    def _top_level_imports(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=self._module_path())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_imports_no_runtime_module(self):
        imported = self._top_level_imports()
        forbidden_hit = imported & self._FORBIDDEN_RUNTIME_MODULES
        self.assertFalse(forbidden_hit,
                          "cowork_capacity.py imports runtime module(s): %s" % sorted(forbidden_hit))

    def test_imports_are_stdlib_only(self):
        imported = self._top_level_imports()
        self.assertEqual(imported, {"re"})

    def test_source_contains_no_io_primitives(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=self._module_path())
        forbidden_calls = {"open", "socket"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls,
                                  "found forbidden I/O call: %s" % node.func.id)
            if isinstance(node, ast.Attribute) and node.attr in ("system", "popen"):
                self.fail("found forbidden I/O attribute access: %s" % node.attr)

    def test_module_has_no_os_or_time_import(self):
        imported = self._top_level_imports()
        self.assertNotIn("os", imported)
        self.assertNotIn("time", imported)
        self.assertNotIn("datetime", imported)


if __name__ == "__main__":
    unittest.main()
